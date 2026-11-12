"""Real subprocess and pytest failures retain their status and isolated reports."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LOGGER = ROOT / "scripts" / "ci-log.py"


@pytest.mark.parametrize("status", [0, 7])
def test_bounded_log_keeps_command_exit_and_tail(tmp_path, status):
    log = tmp_path / "suite.log"
    result = subprocess.run(
        [
            sys.executable,
            str(LOGGER),
            str(log),
            sys.executable,
            "-c",
            f"import sys; print('x' * 90000); print('last diagnostic'); sys.exit({status})",
        ],
        capture_output=True,
    )
    assert result.returncode == status
    assert len(log.read_bytes()) == 65536
    assert log.read_bytes().endswith(b"last diagnostic\n")
    assert len(result.stdout) > 90000  # live output is not truncated with the artifact


def test_log_write_failure_does_not_mask_command_status(tmp_path):
    result = subprocess.run(
        [sys.executable, str(LOGGER), str(tmp_path), sys.executable, "-c", "raise SystemExit(9)"],
        capture_output=True,
    )
    assert result.returncode == 9
    assert b"command status is unchanged" in result.stderr


@pytest.mark.skipif(os.name == "nt", reason="POSIX signal exit convention")
def test_command_signal_remains_a_signal_status(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(LOGGER),
            str(tmp_path / "signal.log"),
            sys.executable,
            "-c",
            "import os, signal; os.kill(os.getpid(), signal.SIGTERM)",
        ],
        capture_output=True,
    )
    assert result.returncode == 128 + signal.SIGTERM


@pytest.fixture
def gate_project(tmp_path):
    if os.name == "nt" or shutil.which("bash") is None:
        pytest.skip("the project gate requires bash")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("test.sh", "ci-log.py", "source-identity.py", "check-connections.sh", "check-portability.py"):
        shutil.copy2(ROOT / "scripts" / name, scripts / name)
    venv = tmp_path / ".venv" / "bin"
    venv.mkdir(parents=True)
    # Run the actual test interpreter; only dependency downloads and lint are stubbed.
    runner = venv / "python"
    runner.write_text(f'#!/usr/bin/env bash\nexec "{sys.executable}" "$@"\n')
    runner.chmod(0o755)
    lint = venv / "ruff"
    lint.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$1" >> .artifacts/commands.txt\nexit "${LINT_STATUS:-0}"\n')
    lint.chmod(0o755)
    tools = tmp_path / "tools"
    tools.mkdir()
    uv = tools / "uv"
    uv.write_text(
        f"#!{sys.executable}\n"
        "import os, pathlib, shutil, sys\n"
        "if sys.argv[1] == '--version': print('uv 0.13.5 (synthetic-platform)'); sys.exit(0)\n"
        "with pathlib.Path('.artifacts/commands.txt').open('a') as log: log.write(sys.argv[1] + '\\n')\n"
        "if sys.argv[1] == 'run': os.execv(sys.executable, [sys.executable, *sys.argv[sys.argv.index('-m'):]])\n"
        "if sys.argv[1] == 'export':\n"
        " name = 'requirements-login.lock' if '--extra' in sys.argv else 'requirements-runtime.lock'\n"
        " shutil.copyfile(name, sys.argv[sys.argv.index('-o') + 1])\n"
    )
    uv.chmod(0o755)
    for name in ("requirements-runtime.lock", "requirements-login.lock"):
        (tmp_path / name).write_text("# synthetic locked runtime\n")
    (tmp_path / "test_synthetic.py").write_text(
        'import os\ndef test_result():\n    assert os.environ.get("FAIL_CASE") != "1", "synthetic failure"\n'
    )
    environment = dict(
        os.environ,
        PATH=str(tools) + os.pathsep + os.environ["PATH"],
        PYTEST_ADDOPTS=f"--junitxml={tmp_path / 'stage-default.xml'}",
    )
    environment.pop("FLEET_TOOLS", None)
    return tmp_path, environment


def test_gate_reports_two_real_pytest_runs_without_overwrite(gate_project):
    root, environment = gate_project
    for fail, status in (("1", 1), ("0", 0)):
        result = subprocess.run(
            ["bash", "scripts/test.sh", "test_synthetic.py"],
            cwd=root,
            env=dict(environment, FAIL_CASE=fail),
            capture_output=True,
        )
        assert result.returncode == status, result.stdout + result.stderr
    invocations = list((root / ".artifacts").glob("check-*"))
    assert len(invocations) == 2
    reports = [ET.parse(path / "junit.xml").getroot() for path in invocations]
    assert sorted(int(report.find("testsuite").get("failures")) for report in reports) == [0, 1]
    assert sorted((path / "exit-code.txt").read_text().strip() for path in invocations) == ["0", "1"]
    assert not (root / "stage-default.xml").exists()
    for path in invocations:
        assert json.loads((path / "source.json").read_text()) == json.loads((path / "final-source.json").read_text())
        assert (path / "tests.log").is_file()
    assert (root / ".artifacts/commands.txt").read_text().splitlines() == [
        "sync",
        "check",
        "format",
        "export",
        "export",
        "sync",
        "check",
        "format",
        "export",
        "export",
    ]


def test_lint_failure_stops_before_tests_and_keeps_original_status(gate_project):
    root, environment = gate_project
    result = subprocess.run(
        ["bash", "scripts/test.sh"],
        cwd=root,
        env=dict(environment, LINT_STATUS="7"),
        capture_output=True,
    )
    assert result.returncode == 7
    (receipt,) = (root / ".artifacts").glob("check-*")
    assert (receipt / "exit-code.txt").read_text().strip() == "7"
    assert (root / ".artifacts/commands.txt").read_text().splitlines() == ["sync", "check"]
    assert not (receipt / "junit.xml").exists()


def test_generated_reports_do_not_change_source_archive_identity(tmp_path):
    spec = importlib.util.spec_from_file_location("source_identity", ROOT / "scripts/source-identity.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / "source.py").write_text("# synthetic source\n")
    initial = module.source_identity(tmp_path)
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports/junit.xml").write_text("<testsuites />")
    assert module.source_identity(tmp_path) == initial


@pytest.mark.parametrize(("lint_status", "expected"), [("0", 1), ("7", 7)])
def test_source_change_refuses_success_without_masking_a_failed_command(gate_project, lint_status, expected):
    root, environment = gate_project
    lint = root / ".venv/bin/ruff"
    lint.write_text(
        lint.read_text().replace(
            'exit "${LINT_STATUS:-0}"', 'printf "# changed\\n" > changed.py\nexit "${LINT_STATUS:-0}"'
        )
    )
    result = subprocess.run(
        ["bash", "scripts/test.sh", "test_synthetic.py"],
        cwd=root,
        env=dict(environment, LINT_STATUS=lint_status),
        capture_output=True,
    )
    assert result.returncode == expected
    assert b"Source changed during verification" in result.stderr


def test_portability_keeps_real_per_test_failures_and_invocation_paths(gate_project):
    root, environment = gate_project
    for fail, status in (("1", 1), ("0", 0)):
        result = subprocess.run(
            [sys.executable, "scripts/check-portability.py", "test_synthetic.py"],
            cwd=root,
            env=dict(environment, FAIL_CASE=fail),
            capture_output=True,
        )
        assert result.returncode == status, result.stdout + result.stderr
    receipts = list((root / ".artifacts").glob("portability-*"))
    assert len(receipts) == 2
    assert sorted(
        int(ET.parse(path / "junit.xml").getroot().find("testsuite").get("failures")) for path in receipts
    ) == [0, 1]


@pytest.mark.parametrize("cached", [False, True])
def test_connections_status_and_cleanup_survive_missing_or_malformed_reports(gate_project, cached):
    root, environment = gate_project
    cache = root / "container-downloads"
    if cached:
        (root / "tools/native.py").write_text(f"print({str(cache)!r})\n")
        environment["FLEET_TOOLS"] = str(root / "tools")
    environment["UV_CACHE_DIR"] = str(root / "host-cache")
    docker = root / "tools/docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "pathlib.Path('.artifacts').mkdir(exist_ok=True)\n"
        "with pathlib.Path('.artifacts/docker.jsonl').open('a') as log: log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1] == 'rm':\n"
        " with pathlib.Path('.artifacts/cleanup.txt').open('a') as log: log.write('cleanup\\n')\n"
        " sys.exit(0)\n"
        "mount = next(arg for arg in sys.argv if arg.endswith(',dst=/reports'))\n"
        "destination = pathlib.Path(mount.split(',src=', 1)[1].rsplit(',dst=', 1)[0])\n"
        "if os.environ['REPORT_MODE'] == 'malformed': (destination / 'junit.xml').write_text('<broken')\n"
        "print('synthetic container diagnostic')\n"
        "sys.exit(int(os.environ['CONTAINER_STATUS']))\n"
    )
    docker.chmod(0o755)
    for mode, status in (("missing", 7), ("malformed", 8), ("missing", 0)):
        result = subprocess.run(
            ["bash", "scripts/check-connections.sh"],
            cwd=root,
            env=dict(
                environment,
                POCKET_ID_BIN=str(docker),
                CI_BROWSER_CACHE=str(root / "browser-cache"),
                REPORT_MODE=mode,
                CONTAINER_STATUS=str(status),
            ),
            capture_output=True,
        )
        assert result.returncode == status, result.stdout + result.stderr
    receipts = list((root / ".artifacts").glob("connections-*"))
    assert len(receipts) == 3
    assert all(b"synthetic container diagnostic" in (path / "connections.log").read_bytes() for path in receipts)
    assert (root / ".artifacts/cleanup.txt").read_text().splitlines() == ["cleanup"] * 3

    calls = [json.loads(line) for line in (root / ".artifacts/docker.jsonl").read_text().splitlines()]
    for run in (call for call in calls if call[0] == "run"):
        for bucket in ("uv", "pip"):
            assert (f"type=bind,src={cache / bucket},dst=/tmp/{bucket}-cache" in run) == cached
        browser = cache / "browsers" if cached else root / "browser-cache"
        assert f"type=bind,src={browser},dst=/browsers" in run
        assert any(arg.endswith(",dst=/usr/local/bin/uv,readonly") for arg in run)
        assert not any("host-cache" in arg for arg in run)


def test_connections_falls_back_to_disposable_downloads_when_cache_preparation_fails(gate_project):
    root, environment = gate_project
    (root / "tools/native.py").write_text("import sys\nsys.exit('Foreign download-cache entry refused')\n")
    environment["FLEET_TOOLS"] = str(root / "tools")
    docker = root / "tools/docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        "pathlib.Path('.artifacts').mkdir(exist_ok=True)\n"
        "with pathlib.Path('.artifacts/docker.jsonl').open('a') as log: log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
    )
    docker.chmod(0o755)
    result = subprocess.run(
        ["bash", "scripts/check-connections.sh"],
        cwd=root,
        env=dict(environment, POCKET_ID_BIN=str(docker), CI_BROWSER_CACHE=str(root / "browser-cache")),
        capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert b"using disposable downloads" in result.stderr
    calls = [json.loads(line) for line in (root / ".artifacts/docker.jsonl").read_text().splitlines()]
    run = next(call for call in calls if call[0] == "run")
    assert not any(arg.endswith(("dst=/tmp/uv-cache", "dst=/tmp/pip-cache")) for arg in run)
    assert not any(arg.endswith("dst=/browsers") for arg in run)
    assert not any("browser-cache" in arg for arg in run)
    assert not (root / "browser-cache").exists()
