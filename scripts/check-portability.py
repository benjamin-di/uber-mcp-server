#!/usr/bin/env python3
"""Optional Windows/macOS stdio/path/locking suite; run on the actual target OS."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    artifacts = ROOT / ".artifacts"
    artifacts.mkdir(exist_ok=True)
    invocation = Path(tempfile.mkdtemp(prefix="portability-", dir=artifacts))
    commands = [
        ("sync", ["uv", "sync", "--locked"]),
        (
            "tests",
            [
                "uv",
                "run",
                "--frozen",
                "python",
                "-m",
                "pytest",
                f"--junitxml={invocation / 'junit.xml'}",
                "-o",
                "junit_suite_name=portability",
                *sys.argv[1:],
            ],
        ),
    ]
    for name, command in commands:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/ci-log.py"), str(invocation / f"{name}.log"), *command],
            cwd=ROOT,
        )
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
