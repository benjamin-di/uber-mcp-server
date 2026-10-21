"""Logging: one stderr stream (stdout belongs to the MCP protocol over stdio) and one line per tool call.

The tool line carries the tool's name, its outcome and how long it took, never its arguments or an error's text:
arguments are places and routes, and error text can echo them.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware

log = logging.getLogger("uber.tools")


def setup() -> None:
    """Configure the root logger once, at process start. ``LOG_LEVEL`` sets the level (default INFO)."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
    for noisy in ("uvicorn.access", "httpx", "httpcore", "mcp.server.lowlevel.server"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class ToolCallLog(Middleware):
    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        tool = getattr(context.message, "name", "?")
        started = time.monotonic()
        try:
            result = await call_next(context)
        except ToolError as exc:
            # The tool's own classified answer (already logged with its code by server.call): one line, no text.
            # A ToolError wrapping a crash keeps its traceback.
            if exc.__cause__ is not None:
                log.exception("%s crashed after %d ms", tool, _ms(started))
            else:
                log.log(min(exc.log_level or logging.WARNING, logging.WARNING), "%s error %d ms", tool, _ms(started))
            raise
        except Exception:
            log.exception("%s crashed after %d ms", tool, _ms(started))
            raise
        failed = bool(getattr(result, "is_error", False))
        log.log(
            logging.WARNING if failed else logging.INFO, "%s %s %d ms", tool, "error" if failed else "ok", _ms(started)
        )
        return result


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
