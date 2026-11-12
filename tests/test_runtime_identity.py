"""Offline checks of the installed entry points, SDK handshake and login bootstrap."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastmcp import Client

from uber_mcp import cli, server
from uber_mcp.identity import build_identity, log_fields

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = "synthetic-private-value"


def invocation(module, *arguments, env=None, code=None):
    isolated = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("UBER_", "MCP_", "FASTMCP_")) and key not in {"PORT", "LOG_LEVEL"}
    }
    isolated.update(env or {})
    command = [sys.executable, "-c", code] if code else [sys.executable, "-m", module, *arguments]
    return subprocess.run(command, cwd=ROOT, env=isolated, capture_output=True, text=True, timeout=20)


def attributed(output):
    assert output.strip()
    for line in output.splitlines():
        assert "service=uber " in line
        assert f"version={build_identity().version} " in line
        assert "source_revision=" in line and "instance=" in line
    assert PRIVATE not in output and "Traceback" not in output


def test_cli_configuration_refusal_preserves_exit_and_stdout(tmp_path):
    result = invocation("uber_mcp", "status", env={"UBER_STATE_DIR": str(tmp_path), "UBER_SESSION_KEY": PRIVATE})
    assert result.returncode == 2 and result.stdout == ""
    attributed(result.stderr)
    assert "UBER_SESSION_KEY must be base64" in result.stderr


def test_cli_unexpected_failure_is_class_only(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("UBER_STATE_DIR", str(tmp_path))

    def fail(*args):
        raise RuntimeError(PRIVATE)

    monkeypatch.setattr(cli, "_status", fail)
    assert cli.main(["status"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    attributed(captured.err)
    assert "RuntimeError" in captured.err


def test_cli_argument_error_does_not_echo_the_rejected_value():
    result = invocation("uber_mcp", "serve", "--port", PRIVATE)
    assert result.returncode == 2 and result.stdout == ""
    attributed(result.stderr)


def test_cli_help_and_version_stay_on_stdout():
    version = invocation("uber_mcp", "--version")
    assert version.returncode == 0 and version.stdout == f"uber-mcp {build_identity().version}\n"
    assert version.stderr == ""
    help_result = invocation("uber_mcp", "--help")
    assert help_result.returncode == 0 and help_result.stdout.startswith("usage: uber-mcp")
    assert "source_revision" not in help_result.stdout and help_result.stderr == ""


@pytest.mark.parametrize("mode", ["legacy", "auto"])
def test_initialize_uses_product_version_and_stamps_late_sdk_handler(service, monkeypatch, mode):
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(logging.Formatter("SDK %(levelname)s %(message)s"))
    logger = logging.getLogger("fastmcp.synthetic.runtime")
    monkeypatch.setattr(logger, "handlers", [handler])
    monkeypatch.setattr(logger, "propagate", False)
    monkeypatch.setattr(logger, "level", logging.WARNING)
    monkeypatch.setattr(logger, "disabled", False)
    mcp = server.create_server(service)

    async def initialize():
        async with Client(mcp, mode=mode) as client:
            logger.warning("synthetic SDK warning")
            return client.server_info, (await client.call_tool("uber_status", {})).data

    info, status = asyncio.run(initialize())
    assert (info.name, info.version) == ("uber", build_identity().version)
    assert "source_revision" not in status and "instance" not in status
    record = json.loads(output.getvalue())
    assert {key: record[key] for key in log_fields()} == log_fields()
    assert record["msg"] == "SDK WARNING synthetic SDK warning"
    assert handler.level == logging.NOTSET and logger.level == logging.WARNING and logger.propagate is False


@pytest.mark.parametrize("protocol", ["2025-03-26", "2025-11-25"])
def test_native_http_initialize_remains_compatible(service, protocol):
    from starlette.testclient import TestClient

    app = server.create_server(service).http_app(**server.HTTP_OPTIONS)
    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream", "mcp-protocol-version": protocol},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": protocol,
                    "capabilities": {},
                    "clientInfo": {"name": "synthetic-client", "version": "1"},
                },
            },
        )
    assert response.status_code == 200
    info = response.json()["result"]["serverInfo"]
    assert info["name"] == "uber" and info["version"] == build_identity().version


@pytest.mark.parametrize(
    "env,expected",
    [
        ({}, "UBER_LOGIN_TOKEN"),
        ({"UBER_LOGIN_TOKEN": "t" * 40, "UBER_LOGIN_SLOTS": PRIVATE}, "ValueError"),
        ({"UBER_LOGIN_TOKEN": "t" * 40, "UBER_LOGIN_MINUTES": PRIVATE}, "ValueError"),
        ({"UBER_LOGIN_TOKEN": "t" * 40, "PORT": PRIVATE}, "ValueError"),
    ],
)
def test_login_refusal_precedes_heavy_imports(env, expected):
    code = """
import importlib.abc
import runpy
import sys
class RefuseRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'signin_window', 'uvicorn', 'fastmcp'}:
            raise RuntimeError('synthetic-private-value')
sys.meta_path.insert(0, RefuseRuntime())
runpy.run_module('uber_login', run_name='__main__')
"""
    result = invocation("uber_login", env=env, code=code)
    assert result.returncode == 1 and result.stdout == ""
    attributed(result.stderr)
    assert expected in result.stderr


def test_login_runtime_import_failure_is_attributed():
    code = """
import importlib.abc
import runpy
import sys
class RefuseRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'uvicorn':
            raise RuntimeError('synthetic-private-value')
sys.meta_path.insert(0, RefuseRuntime())
runpy.run_module('uber_login', run_name='__main__')
"""
    result = invocation("uber_login", env={"UBER_LOGIN_TOKEN": "t" * 40}, code=code)
    assert result.returncode == 1 and result.stdout == ""
    attributed(result.stderr)
    assert "RuntimeError" in result.stderr


def test_login_keeps_uvicorn_policy_and_dependency_api(monkeypatch):
    import signin_window.service
    import uvicorn

    from uber_login import app

    seen = {}
    monkeypatch.setenv("UBER_LOGIN_TOKEN", "t" * 40)
    monkeypatch.setenv("PORT", "8123")
    monkeypatch.setenv("UBER_LOGIN_SLOTS", "2")
    monkeypatch.setenv("UBER_LOGIN_MINUTES", "17")
    monkeypatch.setattr(app, "policy_installed", lambda: True)
    monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: seen.update(logging=kwargs))

    class Windows:
        def __init__(self, browser, **kwargs):
            assert browser is app.run_browser
            seen["windows"] = kwargs

    def build_app(windows, *, token, novnc_dir, title):
        assert isinstance(windows, Windows) and token == "t" * 40
        assert title == "Sign in to Uber for uber-mcp" and isinstance(novnc_dir, Path)
        return object()

    def run(asgi_app, **kwargs):
        seen["run"] = kwargs
        config = uvicorn.Config(asgi_app, **kwargs)
        assert config.log_level == "warning" and config.access_log is True

    monkeypatch.setattr(signin_window.service, "Windows", Windows)
    monkeypatch.setattr(signin_window.service, "build_app", build_app)
    monkeypatch.setattr(uvicorn, "run", run)
    app.main()
    assert seen["logging"] == {"level": logging.INFO, "format": "%(asctime)s %(name)s %(levelname)s %(message)s"}
    assert seen["windows"]["slots"] == 2 and seen["windows"]["minutes"] == 17
    assert seen["run"]["host"] == "0.0.0.0" and seen["run"]["port"] == 8123
    assert seen["run"]["log_config"]["incremental"] is True


def test_http_server_binds_public_uvicorn_logging_config(monkeypatch, tmp_path):
    seen = {}

    class Recorder:
        def run(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(server, "create_server", lambda target, auth=None: Recorder())
    monkeypatch.setattr(server, "_start_keepalive", lambda target: None)
    monkeypatch.setenv("MCP_BEARER_TOKEN", "t" * 40)
    server.serve_http(server.Settings(state_dir=tmp_path), host="127.0.0.1", port=8123)
    assert seen["uvicorn_config"]["access_log"] is False
    assert seen["uvicorn_config"]["log_config"]["incremental"] is True
    assert seen["show_banner"] is False


@pytest.mark.parametrize("entrypoint", ["cli", "login"])
def test_invalid_build_identity_refuses_without_recursing(monkeypatch, capsys, entrypoint):
    from uber_login import app

    module = cli if entrypoint == "cli" else app

    def invalid():
        raise ValueError(PRIVATE)

    monkeypatch.setattr(module, "build_identity", invalid)
    monkeypatch.setattr(module, "log_fields", invalid)
    if entrypoint == "cli":
        assert module.main(["status"]) == 1
    else:
        with pytest.raises(SystemExit) as failure:
            module.main()
        assert failure.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "service=uber identity=unavailable startup refused: build identity unavailable\n"


def test_cli_runtime_import_failure_is_attributed():
    code = """
import importlib.abc
import runpy
import sys
class RefuseRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'fastmcp':
            raise RuntimeError('synthetic-private-value')
sys.meta_path.insert(0, RefuseRuntime())
sys.argv = ['uber-mcp']
runpy.run_module('uber_mcp', run_name='__main__')
"""
    result = invocation("uber_mcp", code=code)
    assert result.returncode == 1 and result.stdout == ""
    attributed(result.stderr)
    assert "RuntimeError" in result.stderr


def test_cli_login_provider_failure_does_not_include_provider_text(monkeypatch, tmp_path, capsys):
    from signin_window import client

    def fail(*args):
        raise client.LoginUnavailable(PRIVATE)

    monkeypatch.setattr(client.LoginClient, "start", fail)
    settings = server.Settings(state_dir=tmp_path, login_url="http://example:8080", login_token="t" * 40)
    assert cli._admin_login(settings, "desktop", 1) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    attributed(captured.err)
    assert "LoginUnavailable" in captured.err
