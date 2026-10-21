"""The bearer that guards the HTTP transport.

Over HTTP the tool surface needs a token: ``MCP_BEARER_TOKEN`` holds one or more (comma-separated, so one can be
rotated while the other still works), and each of them acts for the signed-in Uber account. The server refuses to serve
HTTP without one. ``/health`` stays open (FastMCP does not guard custom routes). Over stdio there is no token: the
client that started the process is the person whose computer it runs on.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

BEARER_CLIENT = "bearer"  # client_id of every static bearer


class AuthConfigError(Exception):
    """No bearer for an HTTP server; raised at startup only."""


def bearer_tokens(env: Mapping[str, str] | None = None) -> list[str]:
    env = os.environ if env is None else env
    raw = env.get("MCP_BEARER_TOKEN") or ""
    return [token for token in (part.strip() for part in raw.split(",")) if token]


def static_verifier(tokens: list[str]) -> Any:
    """FastMCP's StaticTokenVerifier for these tokens, or None when there are none."""
    if not tokens:
        return None
    from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

    return StaticTokenVerifier(tokens={token: {"client_id": BEARER_CLIENT, "scopes": []} for token in tokens})


def require_bearer(env: Mapping[str, str] | None = None) -> Any:
    tokens = bearer_tokens(env)
    if any(len(token) < 16 for token in tokens):
        raise AuthConfigError("every MCP_BEARER_TOKEN must be at least 16 characters")
    verifier = static_verifier(tokens)
    if verifier is None:
        raise AuthConfigError("MCP_BEARER_TOKEN is not set: refusing to serve HTTP without a bearer")
    return verifier
