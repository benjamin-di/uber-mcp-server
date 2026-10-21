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
    commands.add_parser("status", help="show the saved session, today's budget and the booking caps")
    health = commands.add_parser("health", help="check the HTTP server's /health")
    health.add_argument("--url", default="http://127.0.0.1:8000/health")
    admin = commands.add_parser("admin", help="server operations: sign-in, freeze, audit")
    admin_commands = admin.add_subparsers(dest="admin", required=True)
    admin_login = admin_commands.add_parser("login", help="sign in through the login service")
    admin_login.add_argument("--layout", choices=("desktop", "phone"), default="desktop")
    admin_login.add_argument("--minutes", type=int, default=20, help="how long to wait for the person")
    freeze = admin_commands.add_parser("freeze", help="stop all booking now")
    freeze.add_argument("reason", nargs="*")
    admin_commands.add_parser("unfreeze", help="allow booking again (within the caps)")
    audit = admin_commands.add_parser("audit", help="the last booking attempts")
    audit.add_argument("-n", type=int, default=20)
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
        return _status(settings)
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


def _status(settings: Settings) -> int:
    from .server import UberService

    out = UberService(settings).status(check=False)
    out["state_dir"] = str(settings.state_dir)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def _admin(settings: Settings, options: argparse.Namespace) -> int:
    from .booking import Ledger

    ledger = Ledger(settings.state_dir)
    if options.admin == "freeze":
        dropped = ledger.freeze(" ".join(options.reason) or "frozen by the operator")
        print(f"Booking is frozen; {dropped} pending preview(s) dropped. `uber-mcp admin unfreeze` resumes.")
        return 0
    if options.admin == "unfreeze":
        ledger.freeze(None)
        print("Booking is allowed again, within the caps." if settings.booking_enabled else
              "Unfrozen, but booking stays off: UBER_MAX_FARE and UBER_MAX_FARE_24H are not set.")  # fmt: skip
        return 0
    if options.admin == "audit":
        for entry in ledger.recent_audit(max(1, min(options.n, 500))):
            print(json.dumps(entry, ensure_ascii=False))
        return 0
    return _admin_login(settings, options.layout, options.minutes)


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
