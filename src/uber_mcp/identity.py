"""Offline artifact identity and a process-local instance, independent of runtime labels."""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import asdict, dataclass
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from . import __version__


@dataclass(frozen=True)
class BuildIdentity:
    service: str
    version: str
    source_revision: str


def _source_root() -> Path | None:
    package = Path(__file__).resolve().parent
    root = package.parent.parent
    if package.parent.name != "src":
        return None
    try:
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    except (OSError, ValueError, KeyError):
        return None
    return root if project.get("name") == "uber-mcp" else None


def _matching_editable(installed: object) -> bool:
    root = _source_root()
    if root is None:
        return False
    try:
        direct = json.loads(installed.read_text("direct_url.json") or "{}")
        if (
            not isinstance(direct, dict)
            or not isinstance(direct.get("dir_info"), dict)
            or direct["dir_info"].get("editable") is not True
            or not isinstance(direct.get("url"), str)
        ):
            return False
        url = urlsplit(direct["url"])
        return (
            url.scheme == "file"
            and url.netloc in {"", "localhost"}
            and not url.query
            and not url.fragment
            and Path(unquote(url.path)).resolve() == root
        )
    except (OSError, TypeError, ValueError):
        return False


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    data = dict(pairs)
    if len(data) != len(pairs):
        raise ValueError("Duplicate embedded build identity fields")
    return data


def load_identity(path: Path | None = None) -> BuildIdentity:
    """Validate the installed stamp; only a matching development checkout may lack it."""
    try:
        installed = distribution("uber-mcp")
    except PackageNotFoundError:
        installed = None
    version = installed.version if installed is not None else __version__
    artifact = path if path is not None else Path(__file__).with_name("build_identity.json")
    try:
        data = json.loads(artifact.read_text(encoding="utf-8"), object_pairs_hook=_unique_fields)
    except FileNotFoundError:
        development = _matching_editable(installed) if installed is not None else _source_root() is not None
        if not development:
            raise ValueError("Missing embedded build identity in installed Uber package") from None
        return BuildIdentity("uber", version, "unknown")
    except (ValueError, UnicodeError) as error:
        raise ValueError("Invalid embedded Uber build identity") from error
    if (
        not isinstance(data, dict)
        or set(data) != {"service", "version", "source_revision"}
        or any(not isinstance(value, str) for value in data.values())
        or data["service"] != "uber"
        or data["version"] != version
        or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", data["source_revision"])
    ):
        raise ValueError("Invalid embedded Uber build identity")
    return BuildIdentity(**data)


@lru_cache(maxsize=1)
def build_identity() -> BuildIdentity:
    return load_identity()


_INSTANCE = uuid4().hex


def _after_fork() -> None:
    global _INSTANCE
    _INSTANCE = uuid4().hex


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def process_instance() -> str:
    return _INSTANCE


def log_fields() -> dict[str, str]:
    return {**asdict(build_identity()), "instance": process_instance()}
