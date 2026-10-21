#!/usr/bin/env bash
# The gate: lint, format, lock consistency, then the test suite, with a source-bound receipt under .artifacts/
# (what was tested, and that the tree did not change while it ran).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
uv sync --locked
mkdir -p .artifacts
umask 077
invocation="$(mktemp -d "$ROOT/.artifacts/check-XXXXXXXX")"
.venv/bin/python scripts/source-identity.py > "$invocation/source.json"
finish() {
    result=$?
    .venv/bin/python scripts/source-identity.py > "$invocation/final-source.json" || result=1
    if ! cmp -s "$invocation/source.json" "$invocation/final-source.json"; then
        echo "Source changed during verification; rerun against the final tree" >&2
        result=1
    fi
    printf '%s\n' "$result" > "$invocation/exit-code.txt"
    exit "$result"
}
trap finish EXIT
printf 'Evidence: %s\n' "$invocation"
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
# The images install the hash locks; they must be exactly uv.lock's runtime set (and plus the login extra).
uv export --frozen --no-dev --no-emit-project --no-header --quiet -o "$invocation/requirements-runtime.lock"
if ! diff -u requirements-runtime.lock "$invocation/requirements-runtime.lock"; then
    echo "requirements-runtime.lock is stale: run make lock" >&2
    exit 1
fi
uv export --frozen --no-dev --extra login --no-emit-project --no-header --quiet -o "$invocation/requirements-login.lock"
if ! diff -u requirements-login.lock "$invocation/requirements-login.lock"; then
    echo "requirements-login.lock is stale: run make lock" >&2
    exit 1
fi
.venv/bin/python -m pytest --junitxml="$invocation/junit.xml" "$@" 2>&1 | tee "$invocation/tests.log"
