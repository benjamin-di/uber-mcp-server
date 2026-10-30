"""Sign-in links: a person connects their own Uber account from their assistant (README "Signing in with a link").

``uber_connect`` makes a link for the CALLER's account only: ``UBER_PUBLIC_URL/connect/<id>``, an unguessable id that
works for a short time and one successful sign-in. The page shows Uber's own sign-in page, running in the login
service (``uber_login``) and viewed through noVNC, both relayed by this server (signin-window's relay). Once the
login service has proven the session, ``_adopt`` stores it as that account's session, and nowhere else. The link can
only put a session into its account; nothing here shows, returns or reads one. Nothing here is behind the server's
bearer: every route requires the authenticated account owner in addition to the attempt id.
"""

from __future__ import annotations

import logging
import secrets
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from signin_window.client import LoginClient
from signin_window.page import PageText
from signin_window.relay import AttemptContext, Link, LinkLimited, Relay
from starlette.applications import Starlette

from . import accounts
from .errors import UberError
from .fsio import locked
from .session import Session, from_jar, mask_name

if TYPE_CHECKING:
    from .server import Accounts, UberService

log = logging.getLogger(__name__)

TEXT = PageText(
    service="Uber",
    title="Connect your Uber account",
    help_html=(
        "Sign in to Uber in the remote browser below, using your password or a code from Uber. "
        "Device passkeys do not work here. Paste with Ctrl+V or Cmd+V. This application keeps the signed-in session; "
        "connecting does not change assistant permissions or booking approvals."
    ),
    intro_html=("Connect Uber for <b>{label}</b>. Sign in to the Uber account you want this application to use."),
    button="Continue to Uber's sign-in",
    secure_host=r"(^|\.)uber\.com$",
    host_label="🔒 uber.com",
    starting="Opening Uber…",
).with_outcomes(
    connected=(
        "<p class=ok>✓</p><h1>Connection saved</h1><p>Your Uber sign-in is saved. You can close this page "
        "and return to your assistant.</p>"
    ),
    wrong_account=(
        "<h1>That is a different Uber account</h1><p>Sign in to the Uber account already linked to this connection. "
        "Disconnecting does not change which Uber account is linked.</p>"
    ),
    in_use=(
        "<h1>That Uber account is someone else's here</h1><p>It is already connected for another person on this "
        "server. Sign in with your own Uber account.</p>"
    ),
    expired="<h1>This link has expired</h1><p>Return to Connections to start a new Uber sign-in.</p>",
)


class ConnectFlow:
    def __init__(
        self, registry: Accounts, client: LoginClient, public_url: str, *, authorize, clock=time.time, text=TEXT
    ) -> None:
        self.registry = registry
        # The relay runs one hook at a time: the rider checks and the write below go together.
        self.relay = Relay(
            client,
            public_url.rstrip("/") + "/connect",
            text=text,
            on_signed_in=self._adopt,
            before_start=self._before_start,
            authorize=authorize,
            clock=clock,
        )

    def create(self, service: UberService, *, expected_revision=None) -> dict[str, Any]:
        """Create only after owner intent; each accepted Start invalidates older forms."""
        try:
            connection = service.session.connection
            with connection.locked() as control:
                if control["state"] == "disconnecting":
                    raise UberError("connection_disconnected")
                if expected_revision is not None and control["epoch"] != expected_revision:
                    raise ValueError("Connection changed; reload before starting")
                epoch = secrets.token_urlsafe(24)
                link = self.relay.create(
                    service.account_id, context=AttemptContext(owner=service.account_id, target=epoch)
                )
                connection.save({**control, "epoch": epoch})
        except LinkLimited:
            raise UberError("connect_limited", log_detail="link rate") from None
        log.info("connect: a sign-in link for account %s", service.account_id)
        return {
            "url": link.url,
            "expires_at": link.expires_at,
            "account_id": service.account_id,
            "signed_in": bool(service.session.status().get("signed_in")),
            "next_step": (
                "Give the person this link and nothing else; it is for them only. It opens Uber's own sign-in page "
                "after they authenticate with their own identity. "
                f"and works once, for {self.relay.link_ttl // 60} minutes; a newer link replaces it. After they say "
                "they are done, uber_status shows signed_in true."
            ),
        }

    def _adopt(self, link: Link, result: dict[str, Any]) -> str:
        """The login service proved a sign-in: store it for the link's account, unless it is another rider's."""
        rider = result.get("account") if isinstance(result.get("account"), dict) else {}
        uuid = str(rider.get("uuid") or "")
        if not uuid:
            return "failed"
        try:
            service = self.registry.get(link.key, allow_disconnected=True)
        except UberError:
            return "failed"  # the account was removed meanwhile
        if service.session.status().get("state") == "session_invalid":
            log.warning("connect: account %s has an unreadable session; not replaced", link.key)
            return "failed"
        current = service.session.account
        control = service.session.connection.read()
        if not link.context or link.context.target != control["epoch"] or control["state"] == "disconnecting":
            return "failed"
        expected = control["rider"] or (current or {}).get("uuid")
        if expected and expected != uuid:
            log.warning("connect: account %s signed in as a different Uber rider; refused", link.key)
            return "wrong_account"
        if self._elsewhere(link.key, uuid):
            log.warning("connect: account %s signed in as a rider another account has; refused", link.key)
            return "in_use"
        # Verify the candidate independently. An expired, challenged, malformed
        # or temporarily unverifiable candidate never replaces the accepted one.
        from .server import UberService

        try:
            with tempfile.TemporaryDirectory(prefix=".connection-", dir=service.settings.state_dir) as folder:
                settings = replace(service.settings, state_dir=Path(folder))
                staged = UberService(
                    settings, transport=service.uber.transport, shared=service.shared, account=service.account_id
                )
                staged.session.write(rider, str(result.get("user_agent") or ""), result.get("cookies") or [])
                staged.keepalive()
                _, jar, user_agent = staged.session.checkout()
                with locked(self.registry.settings.state_dir / ".connections-adoption.lock"):
                    if self._elsewhere(link.key, uuid):
                        return "in_use"
                    generation = service.session.write(
                        rider, user_agent, from_jar(jar), expected_revision=link.context.target, reconnect=True
                    )
                service.session.checkout()
                service.session.mark("live", generation)
                service.state.record("last_keepalive_at", service.state.now())
                service.state.session_proven()
        except (UberError, OSError, TypeError, ValueError):
            return "failed"
        link.show(rider=mask_name(str(rider.get("name") or "")))
        log.info("connect: account %s signed in", link.key)
        return "connected"

    def _before_start(self, link):
        try:
            service = self.registry.get(link.key, allow_disconnected=True)
            control = service.session.connection.read()
            if link.context and link.context.target == control["epoch"] and control["state"] != "disconnecting":
                return None
        except UberError:
            pass
        return "failed"

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
            bound = Session(other_settings.session_path, other_settings.session_key).connection.read()["rider"]
            if bound == uuid or (held is not None and held.get("uuid") == uuid):
                return True
        return False

    def app(self) -> Starlette:
        return self.relay.app()
