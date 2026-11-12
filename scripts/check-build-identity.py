"""Verify a noneditable installed artifact offline before promotion."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
from importlib.metadata import distribution

from fastmcp import Client

from uber_mcp import logs
from uber_mcp.config import Settings
from uber_mcp.connections import Connections
from uber_mcp.identity import build_identity, log_fields
from uber_mcp.server import UberService, create_server


def check(product_version: str, revision: str) -> None:
    installed = distribution("uber-mcp")
    direct = json.loads(installed.read_text("direct_url.json") or "{}")
    assert not direct.get("dir_info", {}).get("editable"), "Probe requires an installed release artifact"
    identity = build_identity()
    assert identity.service == "uber"
    assert identity.version == installed.version == product_version
    assert identity.source_revision == revision
    portal = Connections.__new__(Connections)
    portal.path = "/connections/"
    page = portal.page("Uber connection", "<p>Offline acceptance</p>")
    assert f'data-version="{product_version}"' in page and f'data-source-revision="{revision}"' in page

    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    root.handlers, root.level = [handler], logging.DEBUG
    logs.setup()
    logging.getLogger("artifact.acceptance").debug("offline artifact acceptance")
    event = json.loads(output.getvalue())
    assert event.items() >= log_fields().items()
    assert event["level"] == "DEBUG" and event["component"] == "artifact.acceptance"

    with tempfile.TemporaryDirectory() as directory:
        server = create_server(UberService(Settings.from_env({"UBER_STATE_DIR": directory})))

        async def native_version():
            for mode in ("legacy", "auto"):
                async with Client(server, mode=mode) as client:
                    assert client.server_info.version == product_version

        asyncio.run(native_version())
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("UBER_", "MCP_", "PYTHONPATH")) and key not in {"VERSION", "VCS_REF"}
        }
        environment.update(UBER_STATE_DIR=directory, VERSION="0.0.0", VCS_REF="f" * 40)
        for module, arguments, expected_exit in (
            ("uber_mcp", ["--invalid=synthetic-private-argument"], 2),
            ("uber_login", [], 1),
        ):
            result = subprocess.run(
                [sys.executable, "-m", module, *arguments],
                cwd=directory,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
                timeout=30,
            )
            assert result.returncode == expected_exit and not result.stdout
            assert "synthetic-private-argument" not in result.stderr
            lines = result.stderr.splitlines()
            assert lines, f"Missing startup diagnostic for {module}"
            for line in lines:
                assert "service=uber " in line and f"version={product_version} " in line
                assert f"source_revision={revision} " in line and "instance=" in line


if __name__ == "__main__":
    check(*sys.argv[1:])
