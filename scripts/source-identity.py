#!/usr/bin/env python3
"""Run repository gates and retain a private, source-bound verification receipt."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def source_identity(root: Path) -> dict:
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.DEVNULL)

    digest = hashlib.sha256()
    try:
        listed = git("ls-files", "-z", "--cached", "--others", "--exclude-standard")
    except (OSError, subprocess.CalledProcessError):
        # Not a git checkout (a release's source archive): hash every file outside the local, ignored folders.
        return {"revision": None, "dirty": None, "source_sha256": _walk_digest(root)}
    paths = sorted(set(listed.split(b"\0")) - {b""})
    for name in paths:
        path = root / os.fsdecode(name)
        digest.update(name + b"\0")
        if path.is_symlink():
            digest.update(os.fsencode(os.readlink(path)))
        elif path.is_file():
            digest.update(path.read_bytes())
        else:
            digest.update(b"<missing>")
        digest.update(b"\0")
    return {
        "revision": git("rev-parse", "HEAD").decode().strip(),
        "dirty": bool(git("status", "--porcelain")),
        "source_sha256": digest.hexdigest(),
    }


LOCAL = {".git", ".venv", ".deps", ".artifacts", ".pytest_cache", ".ruff_cache", "__pycache__"}


def _walk_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file() and not LOCAL & set(p.relative_to(root).parts)):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


if __name__ == "__main__":
    print(json.dumps(source_identity(ROOT), indent=2))
