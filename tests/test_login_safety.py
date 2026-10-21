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
    from starlette.testclient import TestClient

    from uber_login.app import build_app
    from uber_login.logins import Logins

    async def browser(login, logins):  # never starts a real browser
        login.state = "waiting"

    logins = Logins(slots=1, browser=browser, xvfb="true", x11vnc="sleep")
    logins._start_display = lambda login: None  # type: ignore[method-assign]
    client = TestClient(build_app(logins, token="t" * 40, novnc_dir=Path("/nonexistent")))
    assert client.get("/health").status_code == 200
    assert client.post("/logins", json={}).status_code == 401
    started = client.post("/logins", json={}, headers={"Authorization": "Bearer " + "t" * 40}).json()
    assert started["view_token"] and started["id"]
    assert client.get(f"/view/{started['id']}").status_code == 404
    assert client.get(f"/view/{started['id']}?t=wrong").status_code == 404
    assert client.get(f"/view/{started['id']}?t={started['view_token']}").status_code == 200
    assert client.get(f"/logins/{started['id']}").status_code == 401
    assert client.post(f"/logins/{started['id']}/result").status_code == 401
