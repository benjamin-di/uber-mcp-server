"""Forward host metadata only through renderer APIs that explicitly accept it."""

from __future__ import annotations

import html
from collections.abc import Callable
from inspect import Parameter, signature
from typing import Any

from .identity import build_identity


def identity_kwargs(renderer: Callable[..., Any]) -> dict[str, Any]:
    try:
        parameter = signature(renderer).parameters.get("identity")
    except (TypeError, ValueError):
        return {}
    if parameter is None or parameter.kind not in (Parameter.POSITIONAL_OR_KEYWORD, Parameter.KEYWORD_ONLY):
        return {}

    from signin_window import connections

    identity_type = getattr(connections, "PageIdentity", None)
    if identity_type is None:
        return {}
    build = build_identity()
    return {"identity": identity_type(build.service, build.version, build.source_revision)}


def legacy_footer() -> str:
    """Trusted body markup for the connection renderer before its identity API."""
    build = build_identity()
    service = html.escape(build.service, quote=True)
    version = html.escape(build.version, quote=True)
    revision = html.escape(build.source_revision, quote=True)
    return (
        f'<footer id="connection-identity" class="muted" data-service="{service}" data-version="{version}" '
        f'data-source-revision="{revision}" title="Source revision: {revision}" '
        'style="font-size:.8rem;margin-top:1.5rem;user-select:text;overflow-wrap:anywhere">'
        '<details style="border:0;margin:0;padding:0"><summary style="font-size:inherit;font-weight:400;'
        'min-height:24px;padding:.2rem 0">'
        f"Uber v{version} · {html.escape(build.source_revision[:12])}</summary>"
        '<p style="margin:.25rem 0 0">Source revision</p>'
        f'<code style="display:block;white-space:normal;user-select:all">{revision}</code></details></footer>'
    )
