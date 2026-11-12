"""Offline artifact identity rejects runtime labels and unstamped installed packages."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from types import SimpleNamespace

import pytest

from uber_mcp import __version__, identity

ROOT = Path(__file__).resolve().parents[1]


def installed(monkeypatch, direct=None, version=__version__):
    package = SimpleNamespace(version=version, read_text=lambda name: direct)
    monkeypatch.setattr(identity, "distribution", lambda name: package if name == "uber-mcp" else None)
    return package


def stamp(path, **changes):
    fields = {"service": "uber", "version": __version__, "source_revision": "a" * 40, **changes}
    path.write_text(json.dumps(fields))
    return fields


@pytest.mark.parametrize("length", [40, 64])
def test_installed_stamp_uses_distribution_version_and_ignores_runtime_labels(tmp_path, monkeypatch, length):
    installed(monkeypatch)
    path = tmp_path / "build_identity.json"
    fields = stamp(path, source_revision="b" * length)
    for name in ("VERSION", "VCS_REF", "SOURCE_REVISION", "SERVICE", "INSTANCE"):
        monkeypatch.setenv(name, "forged")
    actual = identity.load_identity(path)
    assert actual == identity.BuildIdentity(**fields)
    with pytest.raises(FrozenInstanceError):
        actual.version = "forged"
    installed(monkeypatch, version="1.2.3")
    with pytest.raises(ValueError, match="Invalid embedded"):
        identity.load_identity(path)


@pytest.mark.parametrize(
    "changes",
    [
        {"service": "other"},
        {"version": "v" + __version__},
        {"source_revision": "unknown"},
        {"source_revision": "A" * 40},
        {"source_revision": "a" * 12},
        {"source_revision": "g" * 40},
        {"source_revision": "a" * 41},
        {"source_revision": 40},
        {"version": None},
        {"extra": "not-a-stamp-field"},
    ],
)
def test_installed_stamp_rejects_wrong_or_extra_fields(tmp_path, monkeypatch, changes):
    installed(monkeypatch)
    path = tmp_path / "build_identity.json"
    stamp(path, **changes)
    with pytest.raises(ValueError, match="Invalid embedded"):
        identity.load_identity(path)


@pytest.mark.parametrize(
    "content",
    [
        "{",
        "[]",
        "null",
        "{}",
        '{"service":"uber"}',
        '{"service":"uber","service":"uber","version":"1.2.3","source_revision":"' + "a" * 40 + '"}',
    ],
)
def test_installed_stamp_rejects_incomplete_or_nonobject_metadata(tmp_path, monkeypatch, content):
    installed(monkeypatch)
    path = tmp_path / "build_identity.json"
    path.write_text(content)
    with pytest.raises(ValueError, match="Invalid embedded"):
        identity.load_identity(path)


@pytest.mark.parametrize(
    "direct",
    [
        None,
        "{",
        "[]",
        "{}",
        '{"dir_info":{"editable":false}}',
        '{"dir_info":{"editable":true},"url":"file:///unrelated/project"}',
        '{"dir_info":{"editable":true},"url":"https://example.invalid/project"}',
        '{"dir_info":{"editable":"true"}}',
    ],
)
def test_installed_unstamped_package_requires_matching_editable_metadata(tmp_path, monkeypatch, direct):
    installed(monkeypatch, direct)
    with pytest.raises(ValueError, match="Missing embedded"):
        identity.load_identity(tmp_path / "missing.json")


def test_matching_editable_checkout_can_report_unknown_revision(tmp_path, monkeypatch):
    root = identity._source_root()
    assert root == ROOT
    installed(monkeypatch, json.dumps({"url": root.as_uri(), "dir_info": {"editable": True}}))
    assert identity.load_identity(tmp_path / "missing.json") == identity.BuildIdentity("uber", __version__, "unknown")
    monkeypatch.setattr(identity, "_source_root", lambda: None)
    with pytest.raises(ValueError, match="Missing embedded"):
        identity.load_identity(tmp_path / "missing.json")


def test_uninstalled_source_needs_its_own_project_marker(tmp_path, monkeypatch):
    def absent(name):
        raise PackageNotFoundError(name)

    monkeypatch.setattr(identity, "distribution", absent)
    assert identity.load_identity(tmp_path / "missing.json").source_revision == "unknown"
    monkeypatch.setattr(identity, "_source_root", lambda: None)
    with pytest.raises(ValueError, match="Missing embedded"):
        identity.load_identity(tmp_path / "missing.json")


def test_process_instance_is_stable_across_concurrent_first_calls():
    with ThreadPoolExecutor(max_workers=16) as pool:
        observed = list(pool.map(lambda _: identity.process_instance(), range(128)))
    assert len(set(observed)) == 1
    assert len(observed[0]) == 32
    assert identity.log_fields() == {
        "service": "uber",
        "version": identity.build_identity().version,
        "source_revision": identity.build_identity().source_revision,
        "instance": observed[0],
    }


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX fork behavior")
def test_child_gets_its_own_instance_even_before_first_public_read():
    code = """
import json, os
from uber_mcp import identity
read, write = os.pipe()
pid = os.fork()
if pid == 0:
    os.close(read)
    first = identity.process_instance()
    os.write(write, json.dumps([first, identity.process_instance()]).encode())
    os.close(write)
    os._exit(0)
os.close(write)
child = json.loads(os.read(read, 4096))
os.close(read)
_, status = os.waitpid(pid, 0)
assert status == 0
print(json.dumps([identity.process_instance(), child]))
"""
    result = subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True, timeout=15)
    parent, child = json.loads(result.stdout)
    assert child[0] == child[1] and child[0] != parent
    assert parent != identity.process_instance()
