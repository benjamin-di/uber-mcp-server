"""Owner-authenticated connection management; provider credentials and booking records stay application-owned."""

from __future__ import annotations

import html
import json
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import anyio.to_thread
from signin_window.oidc import BrowserAuth, OIDCConfig
from signin_window.state import PrivateStateBusyError, exclusive_lock
from starlette.applications import Starlette
from starlette.requests import HTTPConnection
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from .accounts import valid_id
from .config import ConfigError
from .connect import TEXT, ConnectFlow
from .errors import UberError
from .state import iso


@dataclass(frozen=True)
class ConnectionsConfig:
    oidc: OIDCConfig
    bindings: dict[str, str]
    account_claim: str | None = None

    def __post_init__(self):
        if bool(self.bindings) == bool(self.account_claim):
            raise ConfigError("Choose subject bindings or an administrator-controlled account claim")
        if self.account_claim and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", self.account_claim):
            raise ConfigError("Invalid connections account claim")
        if any(not isinstance(sub, str) or not sub or not valid_id(account) for sub, account in self.bindings.items()):
            raise ConfigError("Connections bindings must map immutable subjects to configured account ids")
        if len(set(self.bindings.values())) != len(self.bindings):
            raise ConfigError("Each connection has one designated owner")

    @classmethod
    def from_env(cls, settings, env=None):
        env = os.environ if env is None else env
        try:
            claim = env.get("UBER_CONNECTIONS_ACCOUNT_CLAIM") or None
            path = env.get("UBER_CONNECTIONS_BINDINGS_FILE")
            bindings = json.loads(Path(path).read_text()) if path else {}
            if not isinstance(bindings, dict) or not env.get("UBER_CONNECTIONS_GROUP"):
                raise ValueError("bindings or group")
            secret_path = env.get("UBER_CONNECTIONS_CLIENT_SECRET_FILE")
            return cls(
                OIDCConfig(
                    issuer=env["UBER_CONNECTIONS_ISSUER"].rstrip("/"),
                    client_id=env["UBER_CONNECTIONS_CLIENT_ID"],
                    public_url=settings.public_url,
                    state_dir=settings.state_dir / "browser-sessions",
                    cookie_name="__Host-uber-connections",
                    required_group=env["UBER_CONNECTIONS_GROUP"],
                    claim_names=(claim,) if claim else (),
                    client_secret=Path(secret_path).read_text().strip() if secret_path else None,
                ),
                bindings,
                claim,
            )
        except (KeyError, ValueError, OSError, TypeError):
            raise ConfigError("Configure connections OIDC and its private account-owner bindings") from None


class Connections:
    def __init__(self, registry, client, config, *, clock=None, oidc_transport=None, flow_factory=ConnectFlow):
        self.registry, self.config = registry, config
        if config.oidc.state_dir.resolve() != (registry.settings.state_dir / "browser-sessions").resolve():
            raise ConfigError("Browser state must use this application's configured private state directory")
        self.prefix = urlsplit(config.oidc.public_url).path.rstrip("/")
        self.path = self.prefix + "/connections/"
        self.auth = BrowserAuth(config.oidc, **({"clock": clock} if clock else {}), transport=oidc_transport)
        self.flow = flow_factory(
            registry,
            client,
            config.oidc.public_url,
            authorize=self.authorize,
            **({"clock": clock} if clock else {}),
            text=replace(TEXT, reconnect=True, return_url=self.path),
        )

    def identity(self, request):
        person = self.auth.identity(request)
        if person is None:
            return None
        account = (
            self.config.bindings.get(person["sub"])
            if self.config.bindings
            else person["claims"].get(self.config.account_claim)
        )
        if not valid_id(account):
            return None
        try:
            service = self.registry.get(account, allow_disconnected=True)
            owner = service.session.connection.read().get("owner")
        except UberError:
            return None
        if owner is not None and owner != {"issuer": person["issuer"], "sub": person["sub"]}:
            return None
        return {**person, "account": account}

    async def authorize(self, request, link, operation):
        person = self.identity(request)
        if not person or not link.context or person["account"] != link.context.owner:
            return False
        try:
            connection = self.registry.get(person["account"], allow_disconnected=True).session.connection
            if operation == "start":
                connection.bind_owner(person["issuer"], person["sub"])
            control = connection.read()
            return control["epoch"] == link.context.target and control["state"] != "disconnecting"
        except UberError:
            return False

    def status(self, person):
        service = self.registry.get(person["account"], allow_disconnected=True)
        control = service.session.connection.read()
        session = service.session.status()
        if control["state"] == "disconnecting":
            return {
                "state": control["state"],
                "revision": control["epoch"],
                "label": "Disconnect needs attention" if control["state"] == "disconnecting" else "Disconnected",
                "account": session.get("account"),
                "saved_at": session.get("saved_at"),
            }
        pending = [
            link
            for link in self.flow.relay.links(person["account"])
            if not link.closed
            and link.state in ("ready", "starting", "waiting", "saving")
            and link.context
            and link.context.target == control["epoch"]
        ]
        if pending:
            link = pending[-1]
            saving = link.state == "saving"
            return {
                "state": "saving" if saving else "connecting",
                "revision": control["epoch"],
                "label": "Saving connection" if saving else "Sign-in in progress",
                "url": link.url,
                "account": session.get("account"),
            }
        if control["state"] == "disconnected":
            return {
                "state": "disconnected",
                "revision": control["epoch"],
                "label": "Disconnected",
                "account": session.get("account"),
                "saved_at": session.get("saved_at"),
            }
        present = bool(session.get("generation"))
        usable = present and session.get("state") not in ("dead", "session_invalid")
        return {
            "state": "connected" if usable else "sign_in_needed",
            "revision": control["epoch"],
            "label": "Connection saved" if usable else "Sign-in needed",
            "account": session.get("account"),
            "saved_at": session.get("saved_at"),
            "verified_at": session.get("verified_at"),
        }

    def form(self, action, label, *, revision=None):
        field = ""
        if action != "logout":
            if revision is None:
                raise ValueError("Account forms require an observed revision")
            field = f'<input type=hidden name=revision value="{html.escape(revision, quote=True)}">'
        return f'<form method=post action="{self.path}{action}">{field}<button>{html.escape(label)}</button></form>'

    @staticmethod
    async def expected_revision(request):
        if request.headers.get("content-type", "").split(";")[0] != "application/x-www-form-urlencoded":
            raise ValueError("Reload the connection before changing it")
        body = bytearray()
        async for part in request.stream():
            body.extend(part)
            if len(body) > 4096:
                raise ValueError("Bounded connection form")
        values = parse_qs(body.decode("ascii"), max_num_fields=8).get("revision", [])
        if len(values) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", values[0]):
            raise ValueError("A connection revision is required")
        return values[0]

    async def index(self, request):
        person = self.identity(request)
        if person is None:
            return HTMLResponse(
                self.page(
                    "My connections",
                    "<p>Sign in to manage your Uber connection.</p>"
                    f'<a href="{self.path}login">Continue with your identity provider</a>',
                )
            )
        state = self.status(person)
        if state["state"] == "connecting":
            controls = f'<a href="{html.escape(state["url"], quote=True)}">Continue sign-in</a>'
        elif state["state"] == "saving":
            controls = "<p>You can close this page while the connection is saved.</p>"
        elif state["state"] == "disconnecting":
            controls = self.form("disconnect", "Retry disconnect", revision=state["revision"])
        else:
            controls = self.form(
                "start", "Reconnect" if state["state"] == "connected" else "Connect", revision=state["revision"]
            )
        if state["state"] not in ("disconnected", "disconnecting"):
            controls += self.form("disconnect", "Disconnect Uber", revision=state["revision"])
        body = (
            f"<p>Signed in as {html.escape(person['name'])}</p><article><h2>Uber</h2>"
            f"<p role=status>{html.escape(state['label'])}</p>"
            f"<p>{html.escape(state.get('account') or 'No verified rider yet')}</p>"
            f"<p>Last saved: {html.escape(iso(state.get('saved_at')) or 'Not saved yet')}</p>"
            f"<p>Last verified: {html.escape(iso(state.get('verified_at')) or 'Not verified in this run')}</p>"
            "<p>Saved credentials are checked when Uber is used.</p>" + controls + "</article>"
            "<p>Connecting Uber does not grant an agent access or change booking limits and confirmations. "
            "Manage agent permissions separately.</p>" + self.form("logout", "Sign out of this page")
        )
        return HTMLResponse(
            self.page("My connections", body),
            headers={"Refresh": "2"} if state["state"] in ("connecting", "saving") else {},
        )

    async def state(self, request):
        person = self.identity(request)
        return JSONResponse(self.status(person)) if person else Response("Sign-in required", status_code=401)

    def recovery(self, message, *, status_code):
        body = f'<p>{html.escape(message)}</p><p><a href="{html.escape(self.path, quote=True)}">My connections</a></p>'
        return HTMLResponse(self.page("My connections", body), status_code=status_code)

    async def start(self, request):
        return await self.mutate(request, "start")

    async def disconnect(self, request):
        return await self.mutate(request, "disconnect")

    async def mutate(self, request, operation):
        person = self.identity(request)
        if person is None:
            return Response("Sign-in required", status_code=401)
        if not self.auth.origin_ok(request):
            return Response("Request refused", status_code=403)
        service = self.registry.get(person["account"], allow_disconnected=True)
        connection = service.session.connection
        try:
            expected = await self.expected_revision(request)
            with exclusive_lock(connection.path.with_suffix(".lifecycle.lock"), timeout_seconds=0):
                if connection.read()["epoch"] != expected:
                    raise ValueError("Connection changed; reload")
                connection.bind_owner(person["issuer"], person["sub"])
                if operation == "start":
                    link = self.flow.create(service, expected_revision=expected)
                    return RedirectResponse(link["url"], status_code=303)
                await anyio.to_thread.run_sync(lambda: connection.disconnect(service.session))
                for link in self.flow.relay.links(person["account"]):
                    await anyio.to_thread.run_sync(lambda held=link: self.flow.relay.cancel(held))
        except (ValueError, PrivateStateBusyError):
            return self.recovery(
                "Connections changed or another action is running. Return to My connections to check its state.",
                status_code=409,
            )
        except UberError:
            return self.recovery(
                "The connection operation needs attention. Return to My connections to check its state.",
                status_code=409 if operation == "start" else 503,
            )
        except OSError:
            return self.recovery(
                "The connection operation needs attention. Return to My connections to check its state.",
                status_code=503,
            )
        return RedirectResponse(self.path, status_code=303)

    def app(self):
        app = Starlette(
            routes=[
                Route("/", self.index),
                Route("/status", self.state),
                Route("/start", self.start, methods=["POST"]),
                Route("/disconnect", self.disconnect, methods=["POST"]),
                *self.auth.routes(prefix=""),
            ]
        )
        return self.auth.middleware(app)

    def viewer_app(self):
        app, portal = self.flow.app(), self

        async def protected(scope, receive, send):
            request = HTTPConnection(scope)
            if scope["type"] == "http" and scope.get("method") == "GET" and portal.auth.identity(request) is None:
                path = scope.get("path", "")
                link_id = path.rsplit("/", 1)[-1]
                if re.fullmatch(r"[A-Za-z0-9_-]{32,64}", link_id):
                    target = portal.prefix + "/connect/" + link_id
                    return await RedirectResponse(
                        portal.path + "login?next=" + quote(target, safe=""), status_code=303
                    )(scope, receive, send)
            await app(scope, receive, send)

        return protected

    @staticmethod
    def page(title, body):
        return (
            '<!doctype html><meta name=viewport content="width=device-width,initial-scale=1"><title>'
            + title
            + (
                "</title><style>body{font:17px system-ui;margin:0;background:#f5f6f8;color:#16202a}"
                "main{max-width:620px;margin:32px auto;padding:20px}article{background:white;border:1px solid #ddd;"
                "padding:22px;border-radius:16px}button,a{display:inline-block;margin:8px 0;padding:12px;"
                "font:inherit}form{display:inline}p{overflow-wrap:anywhere}</style><main><h1>"
                + title
                + "</h1>"
                + body
                + "</main>"
            )
        )
