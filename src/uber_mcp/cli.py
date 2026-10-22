"""The ``uber-mcp`` command.

uber-mcp                    serve over stdio, for an MCP client on this computer (Claude Desktop, Claude Code,
                            Cursor, ...); the client starts it and talks to it on stdin/stdout
uber-mcp serve              serve over streamable HTTP at http://HOST:PORT/mcp (needs MCP_BEARER_TOKEN)
uber-mcp login              sign in to Uber in a browser window on this computer; the session is saved
uber-mcp logout             delete the saved session and the window's browser profile
uber-mcp status             the saved session, today's budget and the booking caps (asks Uber nothing)
uber-mcp health             exit 0 when the HTTP server's /health answers (container health checks)
uber-mcp admin login        on a server: start a sign-in in the login service and wait for the person
uber-mcp admin freeze [WHY] stop all booking now (pending previews are dropped); admin unfreeze resumes
uber-mcp admin audit [-n N] the last booking attempts
uber-mcp admin account ...  more accounts on one server: list, add ID, caps ID, remove ID (README "Accounts")

status, admin login, freeze, unfreeze and audit take --account ID (default: the default account; freeze without it
freezes every account).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import time
import urllib.request

from . import __version__
from .config import ConfigError, Settings


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="uber-mcp", description="Unofficial MCP server for Uber rides.")
    parser.add_argument("--version", action="version", version=f"uber-mcp {__version__}")
    commands = parser.add_subparsers(dest="command")
    serve = commands.add_parser("serve", help="serve over streamable HTTP (needs MCP_BEARER_TOKEN)")
    serve.add_argument("--host", default="127.0.0.1", help="address to listen on (default 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8000, help="port to listen on (default 8000)")
    login = commands.add_parser("login", help="sign in to Uber in a browser window")
    login.add_argument(
        "--browser",
        choices=("chrome", "chromium"),
        default="chrome",
        help="your installed Google Chrome (default) or the Chromium that `patchright install chromium` adds",
    )
    commands.add_parser("logout", help="delete the saved session")
    status = commands.add_parser("status", help="show the saved session, today's budget and the booking caps")
    status.add_argument("--account", help="an account id (default: the default account)")
    health = commands.add_parser("health", help="check the HTTP server's /health")
    health.add_argument("--url", default="http://127.0.0.1:8000/health")
    admin = commands.add_parser("admin", help="server operations: sign-in, freeze, audit")
    admin_commands = admin.add_subparsers(dest="admin", required=True)
    admin_login = admin_commands.add_parser("login", help="sign in through the login service")
    admin_login.add_argument("--layout", choices=("desktop", "phone"), default="desktop")
    admin_login.add_argument("--minutes", type=int, default=20, help="how long to wait for the person")
    admin_login.add_argument("--account", help="the account to sign in (default: the default account)")
    freeze = admin_commands.add_parser("freeze", help="stop all booking now")
    freeze.add_argument("reason", nargs="*")
    freeze.add_argument("--account", help="only this account (default: every account)")
    unfreeze = admin_commands.add_parser("unfreeze", help="allow booking again (within the caps)")
    unfreeze.add_argument("--account", help="only this account (default: every account)")
    audit = admin_commands.add_parser("audit", help="the last booking attempts")
    audit.add_argument("-n", type=int, default=20)
    audit.add_argument("--account", help="an account id (default: the default account)")
    account = admin_commands.add_parser("account", help="more accounts on one server")
    account_commands = account.add_subparsers(dest="account_command", required=True)
    account_commands.add_parser("list", help="every account, signed in or not, with its caps")
    account_add = account_commands.add_parser("add", help="add an account (read-only until it has caps)")
    account_caps = account_commands.add_parser("caps", help="set an account's caps (none: read-only)")
    for sub in (account_add, account_caps):
        sub.add_argument("id")
        sub.add_argument("--max-fare", help="the most one ride may cost, e.g. 'USD 80'")
        sub.add_argument("--max-fare-24h", help="the most all rides in any 24 hours may cost, same currency")
    account_remove = account_commands.add_parser("remove", help="delete an account: session, ledger, audit log")
    account_remove.add_argument("id")
    account_remove.add_argument("--yes", action="store_true", help="really delete it")
    options = parser.parse_args(args)

    if options.command == "health":
        return _health(options.url)
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"uber-mcp: {exc}", file=sys.stderr)
        return 2
    if options.command == "login":
        return _login(settings, options.browser)
    if options.command == "logout":
        return _logout(settings)
    if options.command == "status":
        return _status(settings, options.account)
    if options.command == "admin":
        return _admin(settings, options)

    from .auth import AuthConfigError
    from .logs import setup
    from .server import serve_http, serve_stdio

    setup()
    try:
        if options.command == "serve":
            serve_http(settings, host=options.host, port=options.port)
        else:
            serve_stdio(settings)
    except AuthConfigError as exc:
        print(f"uber-mcp: {exc}", file=sys.stderr)
        return 1
    return 0


def _health(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - a local URL the operator passes
            return 0 if response.status == 200 else 1
    except OSError:
        return 1


def _save(settings: Settings, result: dict) -> int:
    from .errors import UberError
    from .session import Session, mask_name

    try:
        generation = Session(settings.session_path, settings.session_key).write(
            result["account"], result["user_agent"], result["cookies"]
        )
    except (UberError, KeyError, TypeError):
        print("uber-mcp: Uber's answer did not contain a usable session; try again.", file=sys.stderr)
        return 1
    name = mask_name(str((result.get("account") or {}).get("name") or ""))
    print(f"Signed in as {name}. Saved to {settings.session_path} (generation {generation}).")
    print("A running server picks it up with its next call; no restart is needed.")
    return 0


def _login(settings: Settings, browser: str) -> int:
    try:
        from uber_login.window import NotProven, WindowClosed, sign_in
    except ImportError:
        print(
            "uber-mcp: signing in needs the browser extra. Run it as\n"
            '  uvx --from "uber-mcp[login] @ git+https://github.com/benjamin-di/uber-mcp-server" uber-mcp login\n'
            'or install it with: pip install "uber-mcp[login] @ git+https://github.com/benjamin-di/uber-mcp-server"',
            file=sys.stderr,
        )
        return 2
    profile = settings.state_dir / "browser"
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    print("A browser window opens on Uber's sign-in page. Sign in as usual; it closes once you are signed in.")
    try:
        result = asyncio.run(sign_in(profile=str(profile), channel="chrome" if browser == "chrome" else None))
    except WindowClosed:
        print("uber-mcp: the window was closed before signing in; nothing was saved.", file=sys.stderr)
        return 1
    except NotProven:
        print("uber-mcp: Uber's pages said signed in but the account could not be read; try again.", file=sys.stderr)
        return 1
    except Exception as exc:  # patchright reports a missing browser as a plain Error
        print(f"uber-mcp: the browser could not start ({type(exc).__name__}).", file=sys.stderr)
        if browser == "chrome":
            print(
                "Install Google Chrome, or use --browser chromium after `patchright install chromium`.", file=sys.stderr
            )
        else:
            print("Run `patchright install chromium` first.", file=sys.stderr)
        return 1
    return _save(settings, result)


def _logout(settings: Settings) -> int:
    from .session import Session

    removed = Session(settings.session_path, settings.session_key).clear()
    profile = settings.state_dir / "browser"
    if profile.exists():
        shutil.rmtree(profile)
        removed = True
    print("Signed out: the saved session and the browser profile are deleted." if removed else "Nothing was saved.")
    return 0


def _account_settings(settings: Settings, account_id: str | None) -> Settings | None:
    """The settings of ``account_id`` (None: the default account), or None after printing why not."""
    from . import accounts
    from .errors import UberError

    if account_id is None:
        return settings
    try:
        return accounts.settings_for(settings, account_id)
    except UberError:
        print(f"uber-mcp: no account {account_id} (`uber-mcp admin account list`).", file=sys.stderr)
        return None


def _status(settings: Settings, account_id: str | None = None) -> int:
    from .server import Accounts

    account = _account_settings(settings, account_id)
    if account is None:
        return 2
    out = Accounts.from_settings(settings).get(account_id).status(check=False)
    out["state_dir"] = str(account.state_dir)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def _admin(settings: Settings, options: argparse.Namespace) -> int:
    from . import accounts
    from .booking import Ledger

    if options.admin == "account":
        return _admin_account(settings, options)
    if options.admin in ("freeze", "unfreeze"):
        ids = [options.account] if options.account else accounts.configured(settings)
        for account_id in ids:
            account = _account_settings(settings, account_id)
            if account is None:
                return 2
            ledger = Ledger(account.state_dir)
            if options.admin == "freeze":
                dropped = ledger.freeze(" ".join(options.reason) or "frozen by the operator")
                print(f"{account_id}: booking is frozen; {dropped} pending preview(s) dropped. "
                      "`uber-mcp admin unfreeze` resumes.")  # fmt: skip
            else:
                ledger.freeze(None)
                print(f"{account_id}: booking is allowed again, within the caps." if account.booking_enabled else
                      f"{account_id}: unfrozen, but booking stays off: it has no caps.")  # fmt: skip
        return 0
    account = _account_settings(settings, options.account)
    if account is None:
        return 2
    if options.admin == "audit":
        for entry in Ledger(account.state_dir).recent_audit(max(1, min(options.n, 500))):
            print(json.dumps(entry, ensure_ascii=False))
        return 0
    return _admin_login(account, options.layout, options.minutes)


def _admin_account(settings: Settings, options: argparse.Namespace) -> int:
    from . import accounts
    from .errors import UberError
    from .session import Session

    try:
        if options.account_command == "list":
            for account_id in accounts.configured(settings):
                try:
                    account = accounts.settings_for(settings, account_id)
                except UberError:
                    print(f"{account_id}: account.json unreadable (fix or remove it)")
                    continue
                session = Session(account.session_path, account.session_key).status()
                caps = (f"{account.max_fare.display()} a ride, {account.max_fare_24h.display()} a day"
                        if account.max_fare and account.max_fare_24h else "none (read-only)")  # fmt: skip
                default = " (default)" if account_id == settings.default_account else ""
                # A saved session is "unverified" in a fresh process until a read proves it ("live").
                print(f"{account_id}{default}: session={session.get('state')} caps: {caps}")
            return 0
        if options.account_command == "add":
            accounts.add(settings, options.id, options.max_fare, options.max_fare_24h)
            read_only = "" if options.max_fare else " It is read-only until it has caps (`admin account caps`)."
            print(f"Added account {options.id}.{read_only} Sign it in with `uber-mcp admin login --account "
                  f"{options.id}`.")  # fmt: skip
            return 0
        if options.account_command == "caps":
            caps = accounts.set_caps(settings, options.id, options.max_fare, options.max_fare_24h)
            print(f"{options.id}: caps {caps['max_fare']} a ride, {caps['max_fare_24h']} a day; the server applies "
                  "them on its next call." if caps else f"{options.id}: no caps; it is read-only now.")  # fmt: skip
            return 0
        if not options.yes:
            print(f"uber-mcp: this deletes account {options.id}'s session, ledger and audit log; add --yes.",
                  file=sys.stderr)  # fmt: skip
            return 2
        accounts.remove(settings, options.id)
        print(f"Removed account {options.id}: its session, ledger and audit log are deleted.")
        return 0
    except accounts.AccountConfigError as exc:
        print(f"uber-mcp: {exc}", file=sys.stderr)
        return 2


def _admin_login(settings: Settings, layout: str, minutes: int) -> int:
    from .login_client import LoginClient, LoginUnavailable

    if not settings.login_url or not settings.login_token:
        print("uber-mcp: UBER_LOGIN_URL and UBER_LOGIN_TOKEN are not set (no login service).", file=sys.stderr)
        return 2
    client = LoginClient(settings.login_url, settings.login_token)
    try:
        login = client.start(layout)
    except LoginUnavailable as exc:
        print(f"uber-mcp: the login service did not start a sign-in ({exc}).", file=sys.stderr)
        return 1
    print("Ask the person to open this page and sign in to Uber there (it works once, for this sign-in only):")
    print(f"  {settings.login_view_url}/view/{login['id']}?t={login['view_token']}", flush=True)
    deadline = time.monotonic() + max(1, minutes) * 60
    try:
        while time.monotonic() < deadline:
            state = client.state(login["id"]).get("state")
            if state == "signed_in":
                result = client.result(login["id"])
                if result is None:
                    print("uber-mcp: the sign-in finished but its session was already taken.", file=sys.stderr)
                    return 1
                return _save(settings, result)
            if state in ("failed", "expired", "closed", "unknown"):
                print(f"uber-mcp: the sign-in ended without a session ({state}).", file=sys.stderr)
                return 1
            time.sleep(2)
    except LoginUnavailable as exc:
        print(f"uber-mcp: lost the login service ({exc}).", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass
    finally:
        client.close(login["id"])
    print("uber-mcp: no sign-in within the time limit; nothing was saved.", file=sys.stderr)
    return 1
