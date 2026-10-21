"""State that survives restarts, written atomically on every change (``UBER_STATE_DIR/state.json``): the daily
request budget, the pause after Uber pushed back, unresolved errors, keep-alive and canary results."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import NEXT_STEP, UberError
from .fsio import atomic_write_json, file_key, locked

log = logging.getLogger(__name__)

# A first pushback pauses briefly; another within PUSHBACK_MEMORY pauses for long: a single hiccup costs minutes,
# while real blocking repeats and meets the long pause on its second sign.
FIRST_PAUSE, BLOCK_PAUSE, PUSHBACK_MEMORY = 5 * 60, 30 * 60, 2 * 3600
# Failures a successful keep-alive disproves: it read both of Uber's surfaces as the signed-in rider.
SESSION_CODES = frozenset({"session_expired", "session_invalid", "not_signed_in"})


def iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds") if ts else None


class StateStore:
    def __init__(
        self, state_dir: Path, *, budget: int, clock: Callable[[], float] = time.time, name: str = "state.json"
    ) -> None:
        self.path = Path(state_dir) / name
        self.budget = budget
        self._clock = clock
        self._lock = threading.RLock()
        # Several processes may share the directory (each MCP client on a computer starts its own server):
        # changes happen under a file lock after re-reading what another process wrote.
        self._lock_path = self.path.with_name(f".{name}.lock")
        self._key = file_key(self.path)
        self._data: dict[str, Any] = self._load()

    def _sync(self) -> None:
        key = file_key(self.path)
        if key != self._key:
            self._key, self._data = key, self._load()

    @contextmanager
    def _change(self) -> Iterator[None]:
        with self._lock, locked(self._lock_path):
            self._sync()
            yield
            self._save()

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                # A bare "last_error" cannot be attributed to a tool, so it is dropped, as are unknown codes.
                data.pop("last_error", None)
                errors = data.get("errors")
                data["errors"] = {
                    op: e
                    for op, e in (errors.items() if isinstance(errors, dict) else ())
                    if isinstance(e, dict) and e.get("code") in NEXT_STEP and isinstance(e.get("at"), int | float)
                }
                return data
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            log.warning("state.json unreadable (%s); starting from defaults", type(exc).__name__)
        return {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self.path, self._data)
            self._key = file_key(self.path)
        except OSError as exc:  # the in-memory state still applies
            log.error("could not persist state.json (%s)", type(exc).__name__)

    # -- budget ---------------------------------------------------------------------------

    def _today(self) -> str:
        return datetime.fromtimestamp(self._clock(), UTC).date().isoformat()

    def take(self) -> None:
        """Spend one Uber request, or raise budget_exhausted."""
        with self._change():
            day = self._data.get("budget")
            if not isinstance(day, dict) or day.get("day") != self._today():
                day = {"day": self._today(), "used": 0}
                self._data["budget"] = day
            if day["used"] >= self.budget:
                raise UberError("budget_exhausted", log_detail=f"budget {self.budget} spent")
            day["used"] += 1

    # -- block pause ----------------------------------------------------------------------

    def check_pause(self) -> None:
        with self._lock:
            self._sync()
            if (self._data.get("paused_until") or 0) > self._clock():
                raise UberError("blocked", log_detail="pause active")

    def pause(self, reason: str) -> None:
        with self._change():
            now = self._clock()
            repeat = now - (self._data.get("last_pushback_at") or 0) < PUSHBACK_MEMORY
            seconds = BLOCK_PAUSE if repeat else FIRST_PAUSE
            self._data.update(paused_until=now + seconds, pause_reason=reason, last_pushback_at=now)
        log.warning("Uber pushed back (%s); pausing %d s", reason, seconds)

    # -- unresolved errors ------------------------------------------------------------------
    # One entry per operation (tool name or "keepalive"), cleared when that operation next succeeds,
    # so status reports a failure only while nothing has disproved it.

    def fail(self, op: str, code: str) -> None:
        with self._change():
            self._data.setdefault("errors", {})[op] = {"code": code, "at": self._clock()}

    def succeed(self, op: str) -> None:
        with self._lock:
            self._sync()
            if op not in self._data.get("errors", {}):
                return
        with self._change():
            self._data.get("errors", {}).pop(op, None)

    def session_proven(self) -> None:
        with self._lock:
            self._sync()
            if not any(
                op == "keepalive" or e["code"] in SESSION_CODES for op, e in self._data.get("errors", {}).items()
            ):
                return
        with self._change():
            errors = self._data.get("errors", {})
            for op in [op for op, e in errors.items() if op == "keepalive" or e["code"] in SESSION_CODES]:
                del errors[op]

    def _last_error(self) -> dict[str, Any] | None:
        errors = self._data.get("errors", {})
        if not errors:
            return None
        op, e = max(errors.items(), key=lambda item: item[1]["at"])
        return {"code": e["code"], "at": iso(e["at"]), "tool": op}

    # -- records --------------------------------------------------------------------------

    def record(self, key: str, value: Any) -> None:
        with self._change():
            self._data[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            self._sync()
            return self._data.get(key, default)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._sync()
            day = self._data.get("budget") or {}
            paused = self._data.get("paused_until") or 0
            return {
                "budget": {"limit": self.budget, "used": day.get("used", 0) if day.get("day") == self._today() else 0},
                "paused_until": iso(paused) if paused > self._clock() else None,
                "last_error": self._last_error(),
                "last_keepalive_at": iso(self._data.get("last_keepalive_at")),
            }

    def now(self) -> float:
        return self._clock()
