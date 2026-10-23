"""Sign-in links: a person connects their own Uber account from their assistant (README "Signing in with a link").

``uber_connect`` makes a link for the CALLER's account only: ``UBER_PUBLIC_URL/connect/<id>``, an unguessable id that
works for a short time and one successful sign-in. The page shows Uber's own sign-in page, running in the login
service (``uber_login``) and viewed through noVNC, both relayed by this server (signin-window's relay). Once the
login service has proven the session, ``_adopt`` stores it as that account's session, and nowhere else. The link can
only put a session into its account; nothing here shows, returns or reads one. Nothing here is behind the server's
bearer: the id is the credential.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from signin_window.client import LoginClient
from signin_window.page import PageText
from signin_window.relay import Link, LinkLimited, Relay
from starlette.applications import Starlette

from . import accounts
from .errors import UberError
from .session import Session, mask_name

if TYPE_CHECKING:
    from .server import Accounts, UberService

log = logging.getLogger(__name__)

TEXT = PageText(
    service="Uber",
    title="Connect your Uber account",
    help_html=(
        "Sign in on Uber's own page below, as you usually do: with a code Uber sends you or your password (passkeys "
        "do not work in this window). Pasting works too. The page runs in a browser on the server your assistant "
        "uses; once you are signed in, your assistant can look up rides and, where it is allowed, book them after "
        "asking you."
    ),
    intro_html=(
        "This link connects Uber for <b>{label}</b> on this assistant. Continue only if that is you: whoever signs "
        "in here, their Uber account is the one the assistant will use."
    ),
    button="Continue to Uber's sign-in",
    secure_host=r"(^|\.)uber\.com$",
    host_label="🔒 uber.com",
    starting="Opening Uber…",
).with_outcomes(
    connected=(
        "<p class=ok>✓</p><h1>Connected</h1><p>Your Uber account ({rider}) is connected. You can close this page "
        "and go back to your assistant.</p>"
    ),
    wrong_account=(
        "<h1>That's a different Uber account</h1><p>This assistant is already connected to another Uber account. "
        "Sign in with that one, or ask the person who set it up to disconnect it first.</p>"
    ),
    in_use=(
        "<h1>That Uber account is someone else's here</h1><p>It is already connected for another person on this "
        "server. Sign in with your own Uber account.</p>"
    ),
    expired="<h1>This link has expired</h1><p>Ask your assistant to connect Uber again for a new link.</p>",
)


class ConnectFlow:
    def __init__(self, registry: Accounts, client: LoginClient, public_url: str, *, clock=time.time) -> None:
        self.registry = registry
        # The relay runs one hook at a time: the rider checks and the write below go together.
        self.relay = Relay(client, public_url.rstrip("/") + "/connect", text=TEXT, on_signed_in=self._adopt,
                           clock=clock)  # fmt: skip

    def create(self, service: UberService) -> dict[str, Any]:
        """A sign-in link for ``service``'s account (the caller's): replaces any open link of that account."""
        try:
            link = self.relay.create(service.account_id)
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
            service = self.registry.get(link.key)
        except UberError:
            return "failed"  # the account was removed meanwhile
        if service.session.status().get("state") == "session_invalid":
            log.warning("connect: account %s has an unreadable session; not replaced", link.key)
            return "failed"
        current = service.session.account
        if current is not None and current.get("uuid") != uuid:
            log.warning("connect: account %s signed in as a different Uber rider; refused", link.key)
            return "wrong_account"
        if self._elsewhere(link.key, uuid):
            log.warning("connect: account %s signed in as a rider another account has; refused", link.key)
            return "in_use"
        try:
            service.session.write(rider, str(result.get("user_agent") or ""), result.get("cookies") or [])
        except (UberError, TypeError, ValueError):
            return "failed"
        try:
            service.keepalive()  # proves it with Uber, as the rider
        except UberError as exc:
            if exc.code in ("session_expired", "not_signed_in", "session_invalid"):
                log.warning("connect: account %s: the new session did not work (%s)", link.key, exc.code)
                return "failed"
            # Budget, pause or network: the session is stored; the keep-alive proves it later.
        link.show(rider=mask_name(str(rider.get("name") or "")))
        log.info("connect: account %s signed in", link.key)
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

    def app(self) -> Starlette:
        return self.relay.app()
