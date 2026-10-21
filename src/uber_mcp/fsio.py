"""Files that survive a crash and are shared between processes, on Linux, macOS and Windows.

Every write goes to a private temporary file in the target directory (0600 where the platform has modes), is
flushed and fsynced, then replaces the target with ``os.replace``, so readers see the old file or the new one,
never half of either. ``locked`` serialises read-modify-write cycles across processes: several MCP clients on
one computer each start their own server, and they share the state directory.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

if os.name == "nt":  # pragma: no cover - exercised by the Windows CI job
    import msvcrt
else:
    import fcntl

NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
BINARY = getattr(os, "O_BINARY", 0)


def _fsync_dir(directory: Path) -> None:
    """Make a completed rename durable; best effort (Windows and some filesystems cannot open a directory)."""
    if os.name == "nt":  # pragma: no cover
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data``, readable only by this user."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".tmp-", suffix=path.suffix, dir=path.parent)
    tmp = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_bytes(path, (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode())


def read_private(path: Path, limit: int) -> bytes:
    """Read at most ``limit`` + 1 bytes of a regular file, refusing a symlink at the last component."""
    fd = os.open(path, os.O_RDONLY | NOFOLLOW | BINARY)
    with os.fdopen(fd, "rb") as stream:
        return stream.read(limit + 1)


@contextlib.contextmanager
def locked(lock_path: Path) -> Iterator[None]:
    """An exclusive lock on ``lock_path`` (created if missing), held across processes and threads' callers."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | BINARY, 0o600)
    try:
        if os.name == "nt":  # pragma: no cover
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)  # retries for ~10 s, then raises
                    break
                except OSError:
                    continue
        else:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        if os.name == "nt":  # pragma: no cover
            with contextlib.suppress(OSError):
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        os.close(fd)


def file_key(path: Path) -> tuple[int, int, int] | None:
    """What changes when another process replaces the file (inode, mtime, size); None when it is missing."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_ino, st.st_mtime_ns, st.st_size)
