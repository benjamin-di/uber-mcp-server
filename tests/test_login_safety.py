"""The login service's boundary, checked statically: it shows Uber's sign-in page to a person and automates nothing
on it. No click, keystroke, fill or form call exists in its code; only Uber (and Google or Apple, for their sign-in
buttons) can be the page shown."""

import ast
from pathlib import Path

LOGIN = Path(__file__).resolve().parents[1] / "src" / "uber_login"
INPUT_CALLS = {
    "click", "dblclick", "tap", "fill", "type", "press", "press_sequentially", "check", "uncheck",
    "select_option", "set_input_files", "set_checked", "dispatch_event", "drag_to", "focus", "hover",
    "insert_text", "down", "up", "move", "wheel",
}  # fmt: skip


def test_the_login_service_never_acts_on_the_page():
    for f in sorted(LOGIN.glob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr not in ("keyboard", "mouse", "touchscreen"), f"{node.attr} in {f.name}"
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in INPUT_CALLS, f".{node.func.attr}() in {f.name}"


def test_the_window_shows_only_uber_google_or_apple():
    from uber_login.window import DOCUMENT_HOSTS, LOGIN_URL, allowed_document

    assert DOCUMENT_HOSTS == ("uber.com", "accounts.google.com", "appleid.apple.com")
    assert LOGIN_URL.startswith("https://m.uber.com/go/login-redirect")
    for url in ("https://auth.uber.com/v2/", "https://m.uber.com/go/home", "https://accounts.google.com/o/oauth2"):
        assert allowed_document(url), url
    refused = ("http://auth.uber.com/", "https://uber.com.evil.example/", "https://evil.example/",
               "https://eviluber.com/", "https://google.com/", "javascript:alert(1)", "file:///etc/passwd")  # fmt: skip
    for url in refused:
        assert not allowed_document(url), url


def test_the_view_needs_its_own_token_and_the_api_the_bearer():
    """The service is signin-window's (tested there); this checks uber-login wires it with its bearer."""
    from signin_window.service import Windows, build_app
    from starlette.testclient import TestClient

    async def browser(window):  # never starts a real browser
        window.waiting("auth.uber.com")

    windows = Windows(browser, slots=1, xvfb="true", x11vnc="sleep")
    windows.start_display = lambda window: None  # type: ignore[method-assign]
    client = TestClient(build_app(windows, token="t" * 40, novnc_dir=Path("/nonexistent")))
    assert client.get("/health").status_code == 200
    assert client.post("/logins", json={}).status_code == 401
    started = client.post("/logins", json={}, headers={"Authorization": "Bearer " + "t" * 40}).json()
    assert started["view_token"] and started["id"]
    assert client.get(f"/view/{started['id']}").status_code == 404
    assert client.get(f"/view/{started['id']}?t=wrong").status_code == 404
    assert client.get(f"/view/{started['id']}?t={started['view_token']}").status_code == 200
    assert client.get(f"/logins/{started['id']}").status_code == 401
    assert client.post(f"/logins/{started['id']}/result").status_code == 401


def test_the_runner_opens_ubers_page_on_the_windows_display():
    import asyncio

    from signin_window.service import Window

    from uber_login import app

    seen = {}

    async def sign_in(**kwargs):
        seen.update(kwargs)
        kwargs["on_page"]("auth.uber.com")
        return {"account": {"uuid": "u"}, "cookies": []}

    window = Window("w" * 24, "phone", 2, 0.0, 60.0)
    original, app.sign_in = app.sign_in, sign_in
    installed, app.policy_installed = app.policy_installed, lambda: True
    try:
        assert asyncio.run(app.run_browser(window)) == {"account": {"uuid": "u"}, "cookies": []}
    finally:
        app.sign_in, app.policy_installed = original, installed
    assert seen["env"]["DISPLAY"] == ":92" and seen["layout"] == "phone" and seen["profile"] in window.profiles
    assert seen["channel"] == "chrome"
    assert "--enable-unsafe-swiftshader" in seen["extra_args"]
    assert window.state == "waiting" and window.host == "auth.uber.com"
    assert seen["locked"] is True  # kiosk mode, Ctrl+T/Ctrl+N closed (signin-window's browser)


def test_phone_layout_uses_genuine_browser_identity_and_matches_xvfb_size(monkeypatch, tmp_path):
    import asyncio
    import sys
    import types

    from uber_login import window
    from uber_login.window import LAYOUTS

    assert LAYOUTS["phone"] == {"size": (500, 900), "user_agent": None}
    assert LAYOUTS["desktop"] == {"size": (1280, 860), "user_agent": None}

    # Verify effective launch_persistent_context arguments for local vs hosted launches:
    # Patchright's macOS driver appends --enable-unsafe-swiftshader by default, so local CLI
    # launches must explicitly filter it via ignore_default_args while hosted container runs allow it.
    calls: list[dict] = []

    class StopLaunch(RuntimeError):
        pass

    class FakeChromium:
        async def launch_persistent_context(self, profile, **kwargs):
            calls.append({"profile": profile, **kwargs})
            raise StopLaunch()

    class FakePlaywright:
        chromium = FakeChromium()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    fake_pkg = types.ModuleType("patchright")
    fake_async = types.ModuleType("patchright.async_api")
    fake_async.async_playwright = FakePlaywright
    fake_pkg.async_api = fake_async
    monkeypatch.setitem(sys.modules, "patchright", fake_pkg)
    monkeypatch.setitem(sys.modules, "patchright.async_api", fake_async)

    try:
        asyncio.run(window.sign_in(profile=str(tmp_path / "local"), layout="desktop"))
    except StopLaunch:
        pass
    try:
        asyncio.run(
            window.sign_in(
                profile=str(tmp_path / "hosted"),
                layout="phone",
                channel="chrome",
                extra_args=("--window-position=0,0", "--enable-unsafe-swiftshader"),
                locked=True,
            )
        )
    except StopLaunch:
        pass

    assert len(calls) == 2
    local_call, hosted_call = calls
    assert "--enable-unsafe-swiftshader" not in local_call["args"]
    assert "--enable-unsafe-swiftshader" in (local_call["ignore_default_args"] or ())
    assert local_call["user_agent"] is None
    assert "--enable-unsafe-swiftshader" in hosted_call["args"]
    assert hosted_call["ignore_default_args"] is None
    assert hosted_call["channel"] == "chrome"
    assert hosted_call["user_agent"] is None


def test_chrome_policy_check_requires_google_chrome_policy_dir(tmp_path, monkeypatch):
    from signin_window.browser import install_policy

    from uber_login import app

    cft_dir = tmp_path / "chrome_for_testing"
    chrome_dir = tmp_path / "chrome"
    install_policy((cft_dir,))
    monkeypatch.setattr(app, "CHROME_POLICY_DIR", chrome_dir)
    # Chrome for Testing policy alone must not satisfy Google Chrome's policy requirement.
    assert app.policy_installed() is False
    install_policy((chrome_dir,))
    assert app.policy_installed() is True


def test_no_window_opens_without_the_managed_policy():
    import asyncio

    import pytest
    from signin_window.service import Window

    from uber_login import app

    installed, app.policy_installed = app.policy_installed, lambda: False
    try:
        with pytest.raises(app.PolicyMissing):
            asyncio.run(app.run_browser(Window("w" * 24, "desktop", 1, 0.0, 60.0)))
    finally:
        app.policy_installed = installed
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile.login").read_text()
    assert "python -m signin_window.browser install-policy" in dockerfile
