#!/usr/bin/env python3
"""Stamp the application's immutable source identity before packaging."""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path


def embed(root: Path, version: str, revision: str) -> None:
    declared = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    normalized = version.removeprefix("v")
    if not normalized or normalized != declared:
        raise ValueError("VERSION must match the application version in pyproject.toml")
    if re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", revision) is None:
        raise ValueError("VCS_REF must be a full 40- or 64-character source revision")
    (root / "src/uber_mcp/build_identity.json").write_text(
        json.dumps({"service": "uber", "version": declared, "source_revision": revision.lower()}) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    embed(Path.cwd(), *sys.argv[1:])
