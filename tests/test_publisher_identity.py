"""Publisher identity checks use synthetic stamps and real offline distributions."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("embed_identity", ROOT / "scripts/embed-build-identity.py")
EMBED = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EMBED)


def test_stamping_validates_inputs_before_writing(tmp_path):
    (tmp_path / "src/uber_mcp").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text('[project]\nversion="1.2.3"\n')
    target = tmp_path / "src/uber_mcp/build_identity.json"
    for product_version, revision in (("9.9.9", "a" * 40), ("1.2.3", "unknown"), ("1.2.3", "a" * 12)):
        with pytest.raises(ValueError):
            EMBED.embed(tmp_path, product_version, revision)
        assert not target.exists()
    EMBED.embed(tmp_path, "v1.2.3", "A" * 40)
    assert json.loads(target.read_text()) == {
        "service": "uber",
        "version": "1.2.3",
        "source_revision": "a" * 40,
    }


def test_distributions_carry_identity_and_installed_loader_requires_it(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE", "THIRD_PARTY_NOTICES"):
        shutil.copy2(ROOT / name, source / name)
    shutil.copytree(ROOT / "src", source / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    product_version = tomllib.loads((source / "pyproject.toml").read_text())["project"]["version"]
    revision = "b" * 40
    EMBED.embed(source, product_version, revision)
    expected = {"service": "uber", "version": product_version, "source_revision": revision}
    output = tmp_path / "dist"
    built = subprocess.run(
        ["uv", "build", "--offline", "--no-sources", "--out-dir", str(output), str(source)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert built.returncode == 0, built.stderr
    wheel = next(output.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        assert json.loads(archive.read("uber_mcp/build_identity.json")) == expected
    with tarfile.open(next(output.glob("*.tar.gz"))) as archive:
        member = next(name for name in archive.getnames() if name.endswith("/src/uber_mcp/build_identity.json"))
        assert json.load(archive.extractfile(member)) == expected

    installed = tmp_path / "installed"
    result = subprocess.run(
        ["uv", "pip", "install", "--offline", "--no-deps", "--target", str(installed), str(wheel)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    code = (
        "import json,sys;sys.path.insert(0,sys.argv[1]);"
        "from uber_mcp.identity import log_fields;print(json.dumps(log_fields()))"
    )
    command = [sys.executable, "-I", "-c", code, str(installed)]
    probe = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, timeout=10, check=False)
    assert probe.returncode == 0, probe.stderr
    assert json.loads(probe.stdout).items() >= expected.items()
    (installed / "uber_mcp/build_identity.json").unlink()
    missing = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, timeout=10, check=False)
    assert missing.returncode != 0 and not missing.stdout


def test_public_distribution_publisher_stamps_the_public_revision_before_building():
    path = ROOT / "publish/overlay/.github/workflows/pypi.yml"
    if not path.is_file():
        path = ROOT / ".github/workflows/pypi.yml"
    workflow = path.read_text()
    stamp = 'python3 scripts/embed-build-identity.py "$version" "$GITHUB_SHA"'
    assert workflow.index(stamp) < workflow.index("uv build")
    assert 'scripts/check-build-identity.py "$version" "$GITHUB_SHA"' in workflow
