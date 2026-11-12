#!/usr/bin/env python3
"""Stream one project check and retain its last 64 KiB without masking its exit."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

LIMIT = 64 * 1024


def run(log: Path, command: list[str]) -> int:
    tail = bytearray()
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError:
        print("Project check could not start", file=sys.stderr)
        return 127
    assert process.stdout is not None
    with process.stdout:
        while chunk := process.stdout.read1(8192):
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
            tail.extend(chunk)
            del tail[:-LIMIT]
    status = process.wait()
    try:
        log.write_bytes(tail)
    except OSError:
        print("Bounded project log could not be retained; command status is unchanged", file=sys.stderr)
    return status if status >= 0 else 128 - status


if __name__ == "__main__":
    raise SystemExit(run(Path(sys.argv[1]), sys.argv[2:]))
