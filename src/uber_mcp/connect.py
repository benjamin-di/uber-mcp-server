"""Sign-in links: a person connects their own Uber account from their assistant (README "Signing in with a link").

``uber_connect`` makes a link for the CALLER's account only: ``UBER_PUBLIC_URL/connect/<id>``, an unguessable id that
works for ``LINK_TTL`` and one successful sign-in. The page shows Uber's own sign-in page, running in the login service
(``uber_login``) and viewed through noVNC, both relayed by this server. Once the login service has proven the session,
this server stores it as that account's session, and nowhere else. The link can only put a session into its account;
nothing here shows, returns or reads one. Nothing here is behind the server's bearer: the id is the credential.

  GET  /connect/{id}               the page
  POST /connect/{id}/start         open a sign-in window (the layout follows the person's screen)
  GET  /connect/{id}/state         progress; stores the session once the login service says signed in
  POST /connect/{id}/cancel        close the window (the page sends it when it is closed)
  GET  /connect/{id}/novnc/...     the viewer's files (noVNC, from the login service, plus paste.js)
  WS   /connect/{id}/vnc           the live view of Uber's page
"""

from __future__ import annotations

import collections
import html
import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from importlib import resources
from typing import TYPE_CHECKING, Any

import anyio.to_thread
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket

from . import accounts
from .errors import UberError
from .login_client import LoginBusy, LoginClient, LoginUnavailable
from .session import Session, mask_name
from .state import iso

if TYPE_CHECKING:
    from .server import Accounts, UberService

log = logging.getLogger(__name__)

LINK_TTL = 20 * 60  # from the moment the link is made, whatever happens on the page
MAX_LINKS = 50  # open links on the server; the oldest goes first
LINKS_PER_HOUR = 6  # per account
MAX_STARTS = 4  # sign-in windows per link (a reload or "try again" opens a new one)
MISS_WINDOW, MISSES_PER_ADDRESS, MISSES_TOTAL = 10 * 60, 20, 200  # requests for ids that do not exist
LINK_ID = re.compile(r"[A-Za-z0-9_-]{32,64}\Z")
ACTIVE = ("starting", "waiting")
FINAL = ("connected",)
HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}
PAGE_HEADERS = {
    **HEADERS,
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; frame-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
        "form-action 'none'"
    ),
}
PASTE_TAG = b'<script type="module" src="paste.js"></script>'


@dataclass
class Link:
    id: str
    account: str
    created: float
    state: str = "ready"  # ready, starting, waiting, connected, wrong_account, in_use, busy, failed, cancelled
    login_id: str | None = None
    view_token: str | None = field(default=None, repr=False)
    host: str = ""
    rider: str | None = None
    starts: int = 0
    closed: bool = False  # replaced by a newer link or expired: it opens nothing any more
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def public(self) -> dict[str, Any]:
        return {"state": self.state, "host": self.host, "rider": self.rider}


class Misses:
    """Requests for link ids that do not exist, per address and in total: guessing is slowed long before it matters
    (an id has 192 random bits)."""

    def __init__(self) -> None:
        self.by_address: dict[str, collections.deque] = {}
        self.total: collections.deque = collections.deque()

    def allowed(self, address: str, now: float) -> bool:
        if len(self.by_address) > 10_000:
            self.by_address.clear()
        for queue in (self.total, self.by_address.setdefault(address, collections.deque())):
            while queue and now - queue[0] > MISS_WINDOW:
                queue.popleft()
        return len(self.total) < MISSES_TOTAL and len(self.by_address[address]) < MISSES_PER_ADDRESS

    def record(self, address: str, now: float) -> None:
        self.total.append(now)
        self.by_address.setdefault(address, collections.deque()).append(now)


class ConnectFlow:
    def __init__(self, registry: Accounts, client: LoginClient, public_url: str, *, clock=time.time) -> None:
        self.registry, self.client, self.public_url = registry, client, public_url.rstrip("/")
        self._clock = clock
        self._links: dict[str, Link] = {}
        self._made: dict[str, collections.deque] = {}
        self._misses = Misses()
        self._lock = threading.Lock()
        self._adopting = threading.Lock()  # one session stored at a time: the rider checks and the write go together

    # -- links --------------------------------------------------------------------------------------------------

    def create(self, service: UberService) -> dict[str, Any]:
        """A sign-in link for ``service``'s account (the caller's): replaces any open link of that account."""
        account, now = service.account_id, self._clock()
        with self._lock:
            made = self._made.setdefault(account, collections.deque())
            while made and now - made[0] > 3600:
                made.popleft()
            if len(made) >= LINKS_PER_HOUR:
                raise UberError("connect_limited", log_detail="link rate")
            made.append(now)
            self._expire(now)
            replaced = [link for link in self._links.values() if link.account == account]
            for link in replaced:
                self._links.pop(link.id, None)
                link.closed = True
            while len(self._links) >= MAX_LINKS:
                oldest = self._links.pop(min(self._links.values(), key=lambda link: link.created).id)
                oldest.closed = True
                replaced.append(oldest)
            link = Link(secrets.token_urlsafe(24), account, now)
            self._links[link.id] = link
        for old in replaced:
            threading.Thread(target=self._retire, args=(old,), daemon=True).start()
        log.info("connect: a sign-in link for account %s", account)
        return {
            "url": f"{self.public_url}/connect/{link.id}",
            "expires_at": iso(now + LINK_TTL),
            "account_id": account,
            "signed_in": bool(service.session.status().get("signed_in")),
            "next_step": (
                "Give the person this link and nothing else; it is for them only. It opens Uber's own sign-in page "
                f"and works once, for {LINK_TTL // 60} minutes; a newer link replaces it. After they say they are "
                "done, uber_status shows signed_in true."
            ),
        }

    def _expire(self, now: float) -> None:
        for link in [x for x in self._links.values() if now - x.created > LINK_TTL]:
            self._links.pop(link.id, None)
            link.closed = True
            threading.Thread(target=self._retire, args=(link,), daemon=True).start()

    def find(self, link_id: str, address: str) -> Link | str:
        """The open link with this id, else "expired", or "limited" once this address asked for too many ids that do
        not exist. A real link always works: misses never lock anyone out of their own link."""
        now = self._clock()
        with self._lock:
            self._expire(now)
            link = self._links.get(link_id) if LINK_ID.match(link_id or "") else None
            if link is not None:
                return link
            allowed = self._misses.allowed(address, now)
            self._misses.record(address, now)
            return "expired" if allowed else "limited"

    def _retire(self, link: Link) -> None:
        """Close a replaced or expired link's window, once any start or poll on it has finished."""
        with link.lock:
            self._close(link)

    def _close(self, link: Link) -> None:
        """Close the link's window, if one is open (hold ``link.lock``)."""
        if link.login_id:
            try:
                self.client.close(link.login_id)
            except LoginUnavailable:
                pass
        link.login_id, link.view_token = None, None
        if link.state in ACTIVE:
            link.state = "cancelled"

    # -- the sign-in --------------------------------------------------------------------------------------------

    def usable(self, link: Link) -> bool:
        return link.state not in FINAL and (link.starts < MAX_STARTS or link.state in ACTIVE)

    def start(self, link: Link, layout: str) -> None:
        with link.lock:
            if link.closed or link.state in ACTIVE or link.state in FINAL or link.starts >= MAX_STARTS:
                return
            link.starts += 1
            try:
                login = self.client.start(layout if layout in ("phone", "desktop") else "desktop")
            except LoginBusy:
                link.state = "busy"
                return
            except LoginUnavailable:
                link.state = "failed"
                return
            link.login_id, link.view_token, link.state, link.host = login["id"], login["view_token"], "starting", ""
            log.info("connect: account %s opened a sign-in window", link.account)

    def advance(self, link: Link) -> None:
        """Follow the sign-in; once the login service has proven it, store the session for the link's account."""
        if not link.lock.acquire(blocking=False):
            return  # another poll is already on it
        try:
            if link.state not in ACTIVE or link.login_id is None:
                return
            try:
                login = self.client.state(link.login_id)
            except LoginUnavailable:
                link.state = "failed"
                return
            link.host = str(login.get("host") or "")[:100]
            state = login.get("state")
            if state in ACTIVE:
                link.state = state
                return
            if state != "signed_in":
                link.state = "failed"
                return
            try:
                result = self.client.result(link.login_id)
            except LoginUnavailable:
                result = None
            link.login_id, link.view_token = None, None
            if result is None:
                link.state = "failed"
                return
            with self._adopting:
                link.state = self._adopt(link, result)
        finally:
            link.lock.release()

    def _adopt(self, link: Link, result: dict[str, Any]) -> str:
        rider = result.get("account") if isinstance(result.get("account"), dict) else {}
        uuid = str(rider.get("uuid") or "")
        if not uuid:
            return "failed"
        try:
            service = self.registry.get(link.account)
        except UberError:
            return "failed"  # the account was removed meanwhile
        mine = service.session.status()
        if mine.get("state") == "session_invalid":
            log.warning("connect: account %s has an unreadable session; not replaced", link.account)
            return "failed"
        current = service.session.account
        if current is not None and current.get("uuid") != uuid:
            log.warning("connect: account %s signed in as a different Uber rider; refused", link.account)
            return "wrong_account"
        if self._elsewhere(link.account, uuid):
            log.warning("connect: account %s signed in as a rider another account has; refused", link.account)
            return "in_use"
        try:
            service.session.write(rider, str(result.get("user_agent") or ""), result.get("cookies") or [])
        except (UberError, TypeError, ValueError):
            return "failed"
        try:
            service.keepalive()  # proves it with Uber, as the rider
        except UberError as exc:
            if exc.code in ("session_expired", "not_signed_in", "session_invalid"):
                log.warning("connect: account %s: the new session did not work (%s)", link.account, exc.code)
                return "failed"
            # Budget, pause or network: the session is stored; the keep-alive proves it later.
        link.rider = mask_name(str(rider.get("name") or ""))
        log.info("connect: account %s signed in", link.account)
        return "connected"

    def _elsewhere(self, account: str, uuid: str) -> bool:
        """Whether another account on this server holds a session for this Uber rider."""
        settings = self.registry.settings
        for other in accounts.configured(settings):
            if other == account:
                continue
            try:
                other_settings = accounts.settings_for(settings, other)
            except UberError:
                continue
            held = Session(other_settings.session_path, other_settings.session_key).account
            if held is not None and held.get("uuid") == uuid:
                return True
        return False

    def cancel(self, link: Link) -> None:
        with link.lock:
            if link.state in ACTIVE:
                self._close(link)

    # -- HTTP ---------------------------------------------------------------------------------------------------

    def app(self) -> Starlette:
        def address(request: Request | WebSocket) -> str:
            # The reverse proxy in front must set X-Forwarded-For to the client's address (Caddy does); without one,
            # every client shares one bucket.
            return (request.headers.get("x-forwarded-for") or "unknown").split(",")[0].strip()[:64]

        async def lookup(request: Request) -> Link | Response:
            link = self.find(request.path_params["id"], address(request))
            if isinstance(link, Link):
                return link
            return JSONResponse({"state": link}, status_code=429 if link == "limited" else 404, headers=HEADERS)

        async def body(request: Request) -> dict[str, Any]:
            try:
                data = await request.json()
            except ValueError:
                return {}
            return data if isinstance(data, dict) else {}

        def reply(link: Link) -> JSONResponse:
            return JSONResponse(link.public(), headers=HEADERS)

        async def page(request: Request) -> Response:
            link = self.find(request.path_params["id"], address(request))
            if not isinstance(link, Link) or not self.usable(link):
                return HTMLResponse(_page(None), status_code=429 if link == "limited" else 404, headers=PAGE_HEADERS)
            return HTMLResponse(_page(link), headers=PAGE_HEADERS)

        async def start(request: Request) -> Response:
            link = await lookup(request)
            if isinstance(link, Response):
                return link
            layout = str((await body(request)).get("layout"))
            await anyio.to_thread.run_sync(self.start, link, layout)
            return reply(link)

        async def state(request: Request) -> Response:
            link = await lookup(request)
            if isinstance(link, Response):
                return link
            await anyio.to_thread.run_sync(self.advance, link)
            return reply(link)

        async def cancel(request: Request) -> Response:
            link = await lookup(request)
            if not isinstance(link, Response):
                await anyio.to_thread.run_sync(self.cancel, link)
            return Response(status_code=204, headers=HEADERS)

        async def novnc(request: Request) -> Response:
            link = await lookup(request)
            if isinstance(link, Response):
                return Response(status_code=404, headers=HEADERS)
            path = request.path_params["path"]
            if path == "paste.js":
                return Response(PASTE_JS, media_type="text/javascript", headers=HEADERS)
            status, content, content_type = await anyio.to_thread.run_sync(self.client.static, path)
            if path == "vnc.html" and status == 200:
                content = _with_paste(content)
            headers = {**HEADERS, "X-Frame-Options": "SAMEORIGIN"}
            return Response(content, status_code=status, media_type=content_type or None, headers=headers)

        async def vnc(ws: WebSocket) -> None:
            link = self.find(ws.path_params["id"], address(ws))
            if not isinstance(link, Link) or link.state not in ACTIVE or not link.login_id or not link.view_token:
                await ws.close(code=4404)
                return
            await ws.accept(subprotocol="binary" if "binary" in ws.scope.get("subprotocols", []) else None)
            try:
                await self.client.bridge(link.login_id, link.view_token, ws)
            except LoginUnavailable:
                pass

        return Starlette(
            routes=[
                Route("/{id}", page, methods=["GET"]),
                Route("/{id}/start", start, methods=["POST"]),
                Route("/{id}/state", state, methods=["GET"]),
                Route("/{id}/cancel", cancel, methods=["POST"]),
                Route("/{id}/novnc/{path:path}", novnc, methods=["GET"]),
                WebSocketRoute("/{id}/vnc", vnc),
            ]
        )


PASTE_JS = resources.files("uber_mcp").joinpath("paste.js").read_bytes()


def _with_paste(html: bytes) -> bytes:
    """noVNC's own vnc.html, unchanged but for the paste add-on (paste.js) loaded beside its UI."""
    if b"</head>" not in html:
        return html
    return html.replace(b"</head>", PASTE_TAG + b"\n</head>", 1)


def _page(link: Link | None) -> str:
    if link is None:
        body = """<main class=msg><h1>This link has expired</h1>
<p>Sign-in links work once, for a short time. Ask your assistant to connect Uber again for a new one.</p></main>"""
        return PAGE.replace("{{BODY}}", body).replace("{{SCRIPT}}", "")
    body = BODY.replace("{{ACCOUNT}}", html.escape(link.account))
    return PAGE.replace("{{BODY}}", body).replace("{{SCRIPT}}", SCRIPT)


BODY = """<header><h1>Connect your Uber account</h1>
<p>Sign in on Uber's own page below, as you usually do: with a code Uber sends you or your password (passkeys do not
work in this window). Pasting works too. The page runs in a browser on the server your assistant uses; once you are
signed in, your assistant can look up rides and, where it is allowed, book them after asking you.</p>
<div class=bar hidden><span id=host>🔒 uber.com</span><span id=status></span></div></header>
<main id=intro class=msg><p>This link connects Uber for <b>{{ACCOUNT}}</b> on this assistant. Continue only if
that is you: whoever signs in here, their Uber account is the one the assistant will use.</p>
<button id=go>Continue to Uber's sign-in</button></main>
<div id=view></div>
<main id=done class=msg hidden></main>"""

PAGE = """<!doctype html><html lang=en><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name=referrer content=no-referrer>
<title>Connect Uber</title>
<style>
:root{color-scheme:light;font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;color:#141414}
html,body{margin:0;height:100%;background:#f4f4f4}
body{display:flex;flex-direction:column}
header{padding:12px 16px 8px;background:#fff;border-bottom:1px solid #ddd}
h1{font-size:18px;margin:0 0 4px}
header p{margin:0 0 8px;font-size:13px;color:#4b4b4b;max-width:62em}
.bar{display:flex;justify-content:space-between;gap:12px;font-size:13px}
#host{color:#1a7f37;font-weight:600}#status{color:#4b4b4b;text-align:right}
#view{flex:1;min-height:0;display:flex}
#view iframe{flex:1;border:0;width:100%;height:100%;background:#fff}
.msg{margin:auto;padding:28px 20px;text-align:center;max-width:26em;width:100%;box-sizing:border-box}
.msg h1{font-size:22px}.ok{font-size:44px;color:#1a7f37;margin:0}
button{font:inherit;font-weight:600;margin-top:14px;width:100%;padding:12px 18px;border-radius:8px;
 border:1px solid #141414;background:#141414;color:#fff}
[hidden]{display:none!important}
@media (max-width:600px){header p{font-size:12px}}
</style>
{{BODY}}
{{SCRIPT}}
</html>"""

SCRIPT = """<script>
const base = location.pathname.replace(/\\/+$/, "");
const $ = id => document.getElementById(id);
const esc = t => String(t ?? "").replace(/[&<>"']/g, c => `&#${c.charCodeAt(0)};`);
const post = (path, data) => fetch(`${base}/${path}`, {method: "POST", headers: {"content-type": "application/json"},
                                                      body: JSON.stringify(data || {})}).then(r => r.json());
const TEXT = {starting: "Opening Uber…", waiting: "Sign in as you usually do. This page notices when you're in."};
const AGAIN = `<button onclick="location.reload()">Try again</button>`;
const STOP = {
  connected: s => `<p class=ok>✓</p><h1>Connected</h1><p>Your Uber account (${esc(s.rider)}) is connected.
    You can close this page and go back to your assistant.</p>`,
  wrong_account: () => `<h1>That's a different Uber account</h1><p>This assistant is already connected to another
    Uber account. Sign in with that one, or ask the person who set it up to disconnect it first.</p>${AGAIN}`,
  in_use: () => `<h1>That Uber account is someone else's here</h1><p>It is already connected for another person on
    this server. Sign in with your own Uber account.</p>${AGAIN}`,
  busy: () => `<h1>Someone else is signing in right now</h1><p>Please try again in a few minutes.</p>${AGAIN}`,
  expired: () => `<h1>This link has expired</h1><p>Ask your assistant to connect Uber again for a new link.</p>`,
  limited: () => `<h1>Too many tries</h1><p>Please wait ten minutes and try again.</p>`,
  failed: () => `<h1>Something went wrong</h1><p>Nothing was saved.</p>${AGAIN}`,
  cancelled: () => `<h1>Cancelled</h1>${AGAIN}`,
};
function finish(html) {
  $("view").hidden = true;
  $("intro").hidden = true;
  document.querySelector("header .bar").hidden = true;
  const done = $("done"); done.innerHTML = html; done.hidden = false;
}
let viewer = false, over = false;
function show(s) {
  if (STOP[s.state]) {
    over = true;
    finish(STOP[s.state](s));
    return true;
  }
  if (s.host) $("host").textContent = (/(^|\\.)uber\\.com$/.test(s.host) ? "🔒 " : "") + s.host;
  $("status").textContent = TEXT[s.state] || "";
  if (s.state === "waiting" && !viewer) {
    viewer = true;
    const frame = document.createElement("iframe");
    const q = new URLSearchParams({autoconnect: 1, resize: "scale", reconnect: 1, reconnect_delay: 1500,
                                   path: "../vnc", show_dot: 1});  // relative to the viewer's own address
    frame.src = `${base}/novnc/vnc.html?${q}`;
    frame.title = "Uber sign-in";
    frame.allow = "clipboard-read";
    $("view").appendChild(frame);
  }
  return false;
}
async function poll() {
  try {
    const r = await fetch(`${base}/state`, {cache: "no-store"});
    if (show(await r.json())) return;
  } catch (e) {}
  setTimeout(poll, 1500);
}
async function begin() {
  $("intro").hidden = true;
  document.querySelector("header .bar").hidden = false;
  $("status").textContent = TEXT.starting;
  const layout = Math.min(innerWidth, screen.width) < 700 ? "phone" : "desktop";
  try {
    if (show(await post("start", {layout}))) return;
  } catch (e) {}
  poll();
}
$("go").addEventListener("click", begin);
addEventListener("pagehide", () => { if (!over && navigator.sendBeacon) navigator.sendBeacon(`${base}/cancel`); });
</script>"""
