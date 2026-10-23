"""The sign-in window's paste add-on, ``src/uber_mcp/paste.js``, in a real browser.

The other tests read the add-on as text or serve it. These cases load it as the
module ``vnc.html`` loads, in headless Chromium, against stand-ins for the three
noVNC modules it imports: a connected RFB that records the keys it is asked to
send, and keysym tables for plain ASCII. A synthetic origin serves every file and
any other request is refused, so nothing leaves the browser.

Each Ctrl+V types its text once: from the browser's paste event, or, when none
comes in time, from ``navigator.clipboard.readText()``, stubbed here. Synthetic
key events have no default action, so a paste event fires only where a case
dispatches one. Skipped without patchright or its Chromium (the gate installs
neither); run with ``uv run --extra login pytest tests/test_paste_dom.py``.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

import pytest

sync_api = pytest.importorskip("patchright.sync_api")

ADD_ON = Path(__file__).resolve().parents[1] / "src/uber_mcp/paste.js"
ORIGIN = "https://viewer.example"
ADDRESS = "user@example.com"
PHRASE = "correct horse"

# Shared through the DOM, which the page and patchright's isolated evaluation
# world both see: the text the stand-in RFB typed, how often the clipboard was
# read, and what that read returns or whether it is refused.
VIEWER = """<!DOCTYPE html>
<html><head>
<script type="module">
  import UI from "./app/ui.js";
  const state = document.documentElement.dataset;
  Object.assign(state, { typed: "", reads: "0", clipboard: "", refused: "" });
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: {
      readText() {
        state.reads = String(Number(state.reads) + 1);
        return state.refused
          ? Promise.reject(new DOMException("Read permission denied.", "NotAllowedError"))
          : Promise.resolve(state.clipboard);
      },
    },
  });
  UI.rfb = {
    _rfbConnectionState: "connected",
    viewOnly: false,
    _keyboard: {
      _keyDownList: {},
      _altGrArmed: false,
      _interruptAltGrSequence() {},
      _sendKeyEvent(keysym, code, down) {
        if (!down) delete this._keyDownList[code];
      },
    },
    // A press and release, as the add-on sends each character.
    sendKey(keysym, code, down) {
      if (down === undefined) state.typed += String.fromCodePoint(keysym);
    },
  };
</script>
<script type="module" src="paste.js"></script>
</head><body></body></html>
"""

MODULES = {
    "/app/ui.js": "export default { rfb: null };",
    "/core/input/keysym.js": """export default {
  XK_Return: 0xff0d, XK_Tab: 0xff09, XK_Shift_L: 0xffe1, XK_Control_L: 0xffe3,
  XK_Control_R: 0xffe4, XK_Alt_L: 0xffe9, XK_Super_L: 0xffeb, XK_Super_R: 0xffec,
};""",
    # Latin-1 keysyms are the code points themselves.
    "/core/input/keysymdef.js": "export default { lookup: cp => cp };",
}

CTRL_V = """() => document.body.dispatchEvent(new KeyboardEvent("keydown", {
  key: "v", code: "KeyV", ctrlKey: true, bubbles: true, cancelable: true,
}))"""

PASTE = """text => {
  const clipboardData = new DataTransfer();
  clipboardData.setData("text/plain", text);
  document.body.dispatchEvent(new ClipboardEvent("paste", {
    clipboardData, bubbles: true, cancelable: true,
  }));
}"""

# While the add-on types, it keeps every key from the page; once it is done, a
# key goes through again.
IDLE = """() => document.body.dispatchEvent(new KeyboardEvent("keyup", {
  key: "Shift", code: "ShiftLeft", bubbles: true, cancelable: true,
}))"""

# Well past the add-on's 300 ms wait for a paste event, so a fallback that would
# read the clipboard has done so.
SETTLE_MS = 1000


@pytest.fixture
def viewer():
    files = {
        "/vnc.html": ("text/html", VIEWER),
        "/paste.js": ("text/javascript", ADD_ON.read_text(encoding="utf-8")),
        **{path: ("text/javascript", body) for path, body in MODULES.items()},
    }

    def serve(route) -> None:
        url = urlsplit(route.request.url)
        found = files.get(url.path) if url.netloc == "viewer.example" else None
        if found is None:
            route.abort()
            return
        content_type, body = found
        route.fulfill(content_type=content_type, body=body)

    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chromium", headless=True)
            page = browser.new_page()
        except Exception as exc:  # browser binary missing
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            page.route("**/*", serve)
            page.goto(f"{ORIGIN}/vnc.html")
            yield page
        finally:
            browser.close()


def state(page, name: str) -> str:
    return page.evaluate(f"document.documentElement.dataset.{name}")


def set_clipboard(page, text: str, *, refused: bool = False) -> None:
    page.evaluate(
        """([text, refused]) => Object.assign(document.documentElement.dataset, {
          clipboard: text, refused: refused ? "1" : "",
        })""",
        [text, refused],
    )


def settle(page) -> None:
    page.wait_for_timeout(SETTLE_MS)
    page.wait_for_function(IDLE)


def test_a_paste_right_after_a_fallback_is_typed(viewer):
    """An email typed by the fallback, then a password pasted within seconds."""
    set_clipboard(viewer, ADDRESS)
    viewer.evaluate(CTRL_V)  # no paste event: the clipboard is read
    viewer.wait_for_function(f"document.documentElement.dataset.typed === '{ADDRESS}'")
    viewer.wait_for_function(IDLE)

    set_clipboard(viewer, PHRASE)
    viewer.evaluate(CTRL_V)
    viewer.evaluate(PASTE, PHRASE)
    settle(viewer)

    assert state(viewer, "typed") == ADDRESS + PHRASE
    # The second Ctrl+V's own paste event cancelled its fallback.
    assert state(viewer, "reads") == "1"


@pytest.mark.parametrize("next_ctrl_v", [True, False], ids=["next", "same"])
def test_a_refused_clipboard_read_holds_back_no_paste(viewer, next_ctrl_v):
    """A refused read typed nothing, so the paste event after it still types.

    Either the next Ctrl+V's, or this Ctrl+V's own when it comes late.
    """
    set_clipboard(viewer, "", refused=True)
    viewer.evaluate(CTRL_V)
    viewer.wait_for_function("document.documentElement.dataset.reads === '1'")

    if next_ctrl_v:
        viewer.evaluate(CTRL_V)
    viewer.evaluate(PASTE, PHRASE)
    settle(viewer)

    assert state(viewer, "typed") == PHRASE


def test_a_late_paste_event_after_the_fallback_is_not_typed_again(viewer):
    set_clipboard(viewer, PHRASE)
    viewer.evaluate(CTRL_V)
    viewer.wait_for_function(f"document.documentElement.dataset.typed === '{PHRASE}'")
    viewer.wait_for_function(IDLE)

    # The same Ctrl+V's paste event, arriving after its fallback typed the text.
    viewer.evaluate(PASTE, PHRASE)
    settle(viewer)

    assert state(viewer, "typed") == PHRASE


def test_a_paste_with_no_ctrl_v_before_it_is_typed(viewer):
    """The browser's own Paste menu, after a Ctrl+V whose paste event came."""
    viewer.evaluate(CTRL_V)
    viewer.evaluate(PASTE, ADDRESS)
    viewer.wait_for_function(IDLE)

    viewer.evaluate(PASTE, PHRASE)
    settle(viewer)

    assert state(viewer, "typed") == ADDRESS + PHRASE
    assert state(viewer, "reads") == "0"
