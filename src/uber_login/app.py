"""The login service's API. uber-mcp (``uber-mcp admin login``) holds its bearer; the person, one view token.

POST   /logins               {"layout": "phone"|"desktop"} -> 201 {id, state, view_token, ...} | 503 busy  (bearer)
GET    /logins/{id}          -> {id, state, host, error, layout}                                     (bearer)
POST   /logins/{id}/result   -> the proven session once {account, user_agent, cookies} | 409 not yet   (bearer)
DELETE /logins/{id}          -> 204                                                                   (bearer)
GET    /view/{id}?t=TOKEN    -> the person's sign-in page: Uber's page through noVNC                  (view token)
GET    /view/{id}/state?t=   -> {state, host}                                                         (view token)
WS     /logins/{id}/vnc?t=   -> the login's remote-desktop stream (RFB), for noVNC                    (view token)
GET    /novnc/...            -> noVNC's own files (the viewer)                                        (open)
GET    /health               -> open
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocket, WebSocketDisconnect

from .logins import Busy, Logins

log = logging.getLogger("uber-login")

VIEW_PAGE = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<meta name=referrer content=no-referrer>
<title>Sign in to Uber</title>
<style>
 html,body{margin:0;height:100%;font:15px system-ui;background:#f4f5f4}
 header{padding:.6em 1em;background:#fff;border-bottom:1px solid #ddd}
 header b{font-size:1.05em} .url{color:#2a7a2a;font-size:.85em;margin-top:.2em}
 #screen{position:absolute;top:4.2em;bottom:2.4em;left:0;right:0;background:#fff}
 footer{position:absolute;bottom:0;left:0;right:0;padding:.5em 1em;font-size:.8em;color:#555;background:#fff;
  border-top:1px solid #ddd}
 #done{display:none;padding:3em 1em;text-align:center;font-size:1.2em}
</style>
<header><b>Sign in to Uber for uber-mcp</b><div class=url id=url>auth.uber.com</div></header>
<div id=screen></div><div id=done></div>
<footer id=status>Sign in as you normally do (code, Google, Apple or QR code). This page finishes by itself.</footer>
<script type=module>
import RFB from '/novnc/core/rfb.js';
const id = location.pathname.split('/')[2], t = new URLSearchParams(location.search).get('t');
history.replaceState(null, '', location.pathname);  // keep the token out of the address bar
const ws = (location.protocol === 'https:' ? 'wss' : 'ws') + '://' + location.host;
const rfb = new RFB(document.getElementById('screen'), `${ws}/logins/${id}/vnc?t=${encodeURIComponent(t)}`);
rfb.scaleViewport = true; rfb.resizeSession = false; rfb.focusOnClick = true;
const status = document.getElementById('status');
async function poll() {
  let s;
  try { s = await (await fetch(`/view/${id}/state?t=${encodeURIComponent(t)}`)).json(); } catch (e) { s = {}; }
  if (s.host) document.getElementById('url').textContent = s.host;
  if (s.state === 'signed_in' || s.state === 'closed') {
    rfb.disconnect(); document.getElementById('screen').remove();
    const d = document.getElementById('done'); d.style.display = 'block';
    d.textContent = 'Signed in. You can close this page.'; status.textContent = ''; return;
  }
  if (['failed', 'expired', 'unknown'].includes(s.state)) {
    status.textContent = 'That did not work (' + s.state + ').'; return;
  }
  setTimeout(poll, 1000);
}
poll();
</script>"""


class BearerOnly:
    """Every route but /health, /novnc and the person's view (its own token) needs the bearer, checked before any
    WebSocket handshake too."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app, self.expected = app, f"Bearer {token}".encode()

    @staticmethod
    def _open(scope: Scope) -> bool:
        path = scope.get("path") or ""
        if scope["type"] == "websocket":
            return path.startswith("/logins/") and path.endswith("/vnc")  # the view token is checked there
        return path == "/health" or path.startswith(("/novnc/", "/view/"))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket") and not self._open(scope):
            given = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(given, self.expected):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 4401})
                else:
                    await Response(status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_app(logins: Logins, *, token: str, novnc_dir: Path) -> Starlette:
    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "healthy"})

    async def start(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except ValueError:
            body = {}
        layout = body.get("layout") if isinstance(body, dict) else None
        try:
            login = await logins.start(layout if layout in ("phone", "desktop") else "desktop")
        except Busy:
            return JSONResponse({"error": "busy"}, status_code=503)
        return JSONResponse({**login.public(), "view_token": login.view_token}, status_code=201)

    async def state(request: Request) -> JSONResponse:
        login = logins.get(request.path_params["id"])
        if login is None:
            return JSONResponse({"error": "unknown"}, status_code=404)
        return JSONResponse(login.public())

    async def result(request: Request) -> JSONResponse:
        login_id = request.path_params["id"]
        if logins.get(login_id) is None:
            return JSONResponse({"error": "unknown"}, status_code=404)
        session = logins.take(login_id)
        if session is None:
            return JSONResponse({"error": "not_signed_in"}, status_code=409)
        return JSONResponse(session)

    async def close(request: Request) -> Response:
        await logins.close(request.path_params["id"])
        return Response(status_code=204)

    def _viewable(login_id: str, view_token: str):  # type: ignore[no-untyped-def]
        login = logins.get(login_id)
        return login if login is not None and login.view_ok(view_token) else None

    async def view(request: Request) -> Response:
        if _viewable(request.path_params["id"], request.query_params.get("t", "")) is None:
            return Response(status_code=404)
        return HTMLResponse(VIEW_PAGE, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})

    async def view_state(request: Request) -> JSONResponse:
        login = _viewable(request.path_params["id"], request.query_params.get("t", ""))
        if login is None:
            return JSONResponse({"state": "unknown"})
        return JSONResponse({"state": login.state, "host": login.host})

    async def vnc(ws: WebSocket) -> None:
        login = _viewable(ws.path_params["id"], ws.query_params.get("t", ""))
        if login is None or login.state not in ("starting", "waiting"):
            await ws.close(code=4404)
            return
        await ws.accept(subprotocol="binary" if "binary" in ws.scope.get("subprotocols", []) else None)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", login.vnc_port)
        except OSError:
            await ws.close(code=1011)
            return

        async def upstream() -> None:
            try:
                while True:
                    message = await ws.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    writer.write(message.get("bytes") or (message.get("text") or "").encode())
                    await writer.drain()
            except (WebSocketDisconnect, ConnectionError):
                return

        async def downstream() -> None:
            try:
                while data := await reader.read(65536):
                    await ws.send_bytes(data)
            except (WebSocketDisconnect, ConnectionError, RuntimeError):
                return

        tasks = [asyncio.create_task(upstream()), asyncio.create_task(downstream())]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in tasks:
            task.cancel()
        writer.close()

    routes = [
        Route("/health", health),
        Route("/logins", start, methods=["POST"]),
        Route("/logins/{id}", state, methods=["GET"]),
        Route("/logins/{id}", close, methods=["DELETE"]),
        Route("/logins/{id}/result", result, methods=["POST"]),
        Route("/view/{id}", view, methods=["GET"]),
        Route("/view/{id}/state", view_state, methods=["GET"]),
        WebSocketRoute("/logins/{id}/vnc", vnc),
    ]
    if novnc_dir.is_dir():
        routes.append(Mount("/novnc", app=StaticFiles(directory=novnc_dir)))
    return Starlette(routes=routes, middleware=[Middleware(BearerOnly, token=token)])


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    token = (os.environ.get("UBER_LOGIN_TOKEN") or "").strip()
    if len(token) < 32:
        raise SystemExit("UBER_LOGIN_TOKEN (at least 32 characters) is required")
    logins = Logins(
        slots=int(os.environ.get("UBER_LOGIN_SLOTS") or 1),
        minutes=int(os.environ.get("UBER_LOGIN_MINUTES") or 20),
        xvfb=os.environ.get("UBER_LOGIN_XVFB") or "Xvfb",
        x11vnc=os.environ.get("UBER_LOGIN_X11VNC") or "x11vnc",
    )
    novnc = Path(os.environ.get("UBER_LOGIN_NOVNC") or "/usr/share/novnc")
    app = build_app(logins, token=token, novnc_dir=novnc)
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT") or 8080), log_level="warning")
