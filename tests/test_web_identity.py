"""Pinned and compatible renderers retain the host's identity and page boundaries."""

import subprocess
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from html.parser import HTMLParser

import pytest
from signin_window import connections as renderer
from signin_window.service import build_app
from starlette.testclient import TestClient

from uber_mcp import connections, web_identity
from uber_mcp.identity import BuildIdentity


class Elements(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.tags = []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def test_connections_footer_keeps_full_host_revision_and_existing_form(monkeypatch):
    build = BuildIdentity("uber", "1.2.3", "a" * 40)
    monkeypatch.setattr(web_identity, "build_identity", lambda: build)
    portal = connections.Connections.__new__(connections.Connections)
    portal.path = "/connections/"
    form = '<form><input name="revision" value="synthetic-form-state"></form>'
    page = portal.page("Uber connection", form, refresh=True)
    assert form in page and "data-refresh" in page
    footers = [attrs for tag, attrs in Elements(page).tags if tag == "footer"]
    assert len(footers) == 1
    assert footers[0]["data-service"] == build.service
    assert footers[0]["data-version"] == build.version
    assert footers[0]["data-source-revision"] == build.source_revision
    assert "user-select:text" in page
    assert "uber v1.2.3 · " + "a" * 12 in page.lower()


def test_connections_footer_escapes_host_metadata(monkeypatch):
    build = BuildIdentity("uber", '"><script>synthetic</script>&', "unknown")
    monkeypatch.setattr(web_identity, "build_identity", lambda: build)
    portal = connections.Connections.__new__(connections.Connections)
    portal.path = "/connections/"
    page = portal.page("Uber connection", "<p>safe</p>")
    footer = next(attrs for tag, attrs in Elements(page).tags if tag == "footer")
    assert footer["data-version"] == build.version
    assert "<script>synthetic</script>" not in page


def test_only_an_explicit_keyword_and_public_identity_type_enable_forwarding(monkeypatch):
    @dataclass
    class PageIdentity:
        service: str
        version: str
        source_revision: str

    def keyword(*, identity=None):
        return identity

    def positional(identity=None):
        return identity

    def positional_only(identity=None, /):
        return identity

    def catch_all(**kwargs):
        return kwargs

    build = BuildIdentity("uber", "1.2.3", "a" * 40)
    monkeypatch.setattr(web_identity, "build_identity", lambda: build)
    monkeypatch.setattr(renderer, "PageIdentity", PageIdentity, raising=False)
    for factory in (keyword, positional):
        assert factory(**web_identity.identity_kwargs(factory)) == PageIdentity("uber", "1.2.3", "a" * 40)
    assert web_identity.identity_kwargs(positional_only) == {}
    assert web_identity.identity_kwargs(catch_all) == {}
    monkeypatch.delattr(renderer, "PageIdentity")
    assert web_identity.identity_kwargs(keyword) == {}


def test_owned_page_legacy_fallback_never_passes_an_unsupported_keyword(monkeypatch):
    original = connections.connection_page

    def legacy(title, body, *, refresh, login_url):
        return original(title, body, refresh=refresh, login_url=login_url)

    monkeypatch.setattr(connections, "connection_page", legacy)
    monkeypatch.setattr(web_identity, "build_identity", lambda: BuildIdentity("uber", "1.2.3", "a" * 40))
    portal = connections.Connections.__new__(connections.Connections)
    portal.path = "/connections/"
    markup = portal.page("Uber connection", "<p>Existing body</p>")
    assert markup.count('<footer id="connection-identity"') == 1
    assert "<p>Existing body</p>" in markup
    assert 'data-source-revision="' + "a" * 40 + '"' in markup


@pytest.mark.skipif(not web_identity.identity_kwargs(build_app), reason="Pinned renderer has no host identity API")
def test_login_entrypoint_forwards_host_identity_to_its_real_direct_view(monkeypatch, tmp_path):
    import signin_window.service
    import uvicorn

    from uber_login import app

    class OfflineWindows:
        def __init__(self, runner, **kwargs):
            assert runner is app.run_browser
            self.window = signin_window.service.Window("w" * 24, "desktop", 0, 0, 1)
            self.window.view_token = "synthetic-view-token"

        def get(self, window_id):
            return self.window if window_id == self.window.id else None

        @asynccontextmanager
        async def lifespan(self):
            yield

    seen = {}
    vendor = "<html><body>Synthetic vendor viewer</body></html>"
    (tmp_path / "vnc.html").write_text(vendor)
    build = BuildIdentity("uber", '1.2.3" <dev>', "a" * 40)
    monkeypatch.setattr(web_identity, "build_identity", lambda: build)
    monkeypatch.setattr(signin_window.service, "Windows", OfflineWindows)
    monkeypatch.setattr(app, "policy_installed", lambda: True)
    monkeypatch.setenv("UBER_LOGIN_TOKEN", "t" * 40)
    monkeypatch.setenv("UBER_LOGIN_NOVNC", str(tmp_path))
    monkeypatch.setattr(uvicorn, "run", lambda asgi_app, **kwargs: seen.update(app=asgi_app))
    app._serve()
    with TestClient(seen["app"]) as client:
        path = "/view/" + "w" * 24
        assert client.get(path).status_code == 404
        assert client.get(path + "?t=wrong").status_code == 404
        response = client.get(path + "?t=synthetic-view-token")
        assert response.status_code == 200
        footer = [
            attrs
            for tag, attrs in Elements(response.text).tags
            if tag == "footer" and attrs.get("id") == "page-identity"
        ]
        assert len(footer) == 1
        assert footer[0]["data-service"] == "uber"
        assert footer[0]["data-version"] == build.version
        assert footer[0]["data-source-revision"] == build.source_revision
        assert "<dev>" not in response.text and "user-select:text" in response.text
        assert "<code>" + build.source_revision + "</code>" in response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-frame-options"] == "DENY"
        assert client.get("/novnc/vnc.html").text == vendor
        assert client.get("/logins/" + "w" * 24).status_code == 401


@pytest.mark.skipif(not hasattr(renderer, "PageIdentity"), reason="Pinned renderer has no host identity API")
def test_default_connect_text_stamps_real_relay_intro_and_expired_pages():
    code = """
from starlette.testclient import TestClient
from signin_window.relay import AttemptContext
from uber_mcp import identity
def unavailable():
    raise ValueError("synthetic-invalid-artifact")
identity.build_identity = unavailable
from uber_mcp.connect import ConnectFlow
from uber_mcp import web_identity
web_identity.build_identity = lambda: identity.BuildIdentity("uber", "1.2.3", "a" * 40)
flow = ConnectFlow(None, object(), "https://example.com/connect", authorize=lambda *_: True)
link = flow.relay.create("synthetic-account", context=AttemptContext(owner="synthetic-account", target="attempt"))
with TestClient(flow.app(), base_url="https://example.com") as client:
    for target, status in [("/" + link.id, 200), ("/" + "x" * 32, 404)]:
        response = client.get(target)
        assert response.status_code == status
        assert response.text.count('<footer id="page-identity"') == 1
        assert 'data-service="uber"' in response.text
        assert 'data-version="1.2.3"' in response.text
        assert 'data-source-revision="' + "a" * 40 + '"' in response.text
        assert "<code>" + "a" * 40 + "</code>" in response.text
        assert "user-select:text" in response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-frame-options"] == "DENY"
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
