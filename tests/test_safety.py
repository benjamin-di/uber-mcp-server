"""The safety boundary, checked statically and at runtime. A new Uber host or operation, a network import outside
``uber`` (and the login service client), or a money operation sent from anywhere but ``booking`` is a deliberate
change here, never a side effect."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from uber_mcp import uber
from uber_mcp.errors import UberError

SRC = Path(__file__).resolve().parents[1] / "src" / "uber_mcp"
NETWORK_MODULES = {"urllib.request", "http.client", "socket", "httpx", "requests", "aiohttp", "ssl"}


def test_hosts_and_operations_are_pinned():
    assert {"m.uber.com", "riders.uber.com"} == uber.HOSTS
    assert set(uber.READ_OPERATIONS) == {
        "Activities", "CurrentUserRidersWeb", "GetArrears", "GetTrip", "CancellationInformation", "GetStatus",
        "Products", "PudoLocationSearch", "PudoResolveLocationPudoFragment", "RiderCompletedTripsCount", "SavedPlaces",
    }  # fmt: skip
    assert uber.MONEY_OPERATIONS == {"TripRequest": "rides", "TripCancel": "rides"}
    documents = json.loads((SRC / "operations.json").read_text())["operations"]
    assert set(documents) == set(uber.READ_OPERATIONS) | set(uber.MONEY_OPERATIONS)
    for name, document in documents.items():
        kind = "mutation" if name in uber.MONEY_OPERATIONS else "query"
        assert document.startswith(f"{kind} {name}"), name


def _imports(tree: ast.AST) -> set[str]:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_only_the_uber_module_and_the_login_client_do_network_io():
    allowed = {"uber.py", "login_client.py", "cli.py"}  # cli: the local health check only
    for f in sorted(SRC.glob("*.py")):
        found = _imports(ast.parse(f.read_text())) & NETWORK_MODULES
        if f.name not in allowed:
            assert not found, f"{f.name} imports {found}"


def test_only_booking_sends_money_operations():
    for f in sorted(SRC.glob("*.py")):
        tree = ast.parse(f.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and any(k.arg == "money" for k in node.keywords):
                assert f.name == "booking.py", f"money=... in {f.name}"
            if isinstance(node, ast.Constant) and node.value in uber.MONEY_OPERATIONS:
                assert f.name in ("booking.py", "uber.py"), f"{node.value} named in {f.name}"


def test_money_operations_need_the_money_flag(service, fake):
    for op in uber.MONEY_OPERATIONS:
        with pytest.raises(UberError) as exc:
            service.uber.graphql(op, {})
        assert exc.value.code == "internal_error"
    with pytest.raises(UberError):
        service.uber.graphql("GetStatus", {}, money=True)  # and the flag reaches nothing else
    assert fake.sent == []


def test_the_transport_refuses_other_hosts(service):
    with pytest.raises(UberError):
        service.uber.transport.post(None, "https://auth.uber.com/graphql", b"{}", {})  # type: ignore[arg-type]


def test_errors_never_carry_exception_text():
    payload = UberError("upstream_error", log_detail="secret cookie=abc").payload()
    assert "secret" not in json.dumps(payload) and set(payload) == {"code", "next_step", "retryable"}
    refused = UberError("uber_refused", "<script>").payload()
    assert "uber_error" not in refused


def test_money_tools_are_marked_destructive(service):
    import asyncio

    from uber_mcp.server import create_server

    tools = {t.name: t for t in asyncio.run(create_server(service).list_tools())}
    for name in ("uber_request_ride", "uber_cancel_ride"):
        assert tools[name].annotations.destructive_hint is True and tools[name].annotations.read_only_hint is False
    # uber_connect makes a sign-in link (state on this server, nothing at Uber): not read-only, never destructive.
    link = tools["uber_connect"].annotations
    assert link.read_only_hint is False and link.destructive_hint is False and link.open_world_hint is False
    for name, tool in tools.items():
        if name not in ("uber_request_ride", "uber_cancel_ride", "uber_connect"):
            assert tool.annotations.read_only_hint is True, name
