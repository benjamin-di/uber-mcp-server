#!/usr/bin/env bash
# Synthetic acceptance only: disposable provider, browser profiles and configuration.
set -euo pipefail
if [ "${1:-}" = container ]; then
    export PLAYWRIGHT_SKIP_BROWSER_GC=1 XDG_CONFIG_HOME=/tmp/browser-settings UV_LINK_MODE=copy
    export PLAYWRIGHT_BROWSERS_PATH=/browsers UBER_LOGIN_NOVNC=/tmp/novnc
    export POCKET_ID_BIN=/tmp/pocket-id
    unset PYTEST_ADDOPTS
    apt-get update -q
    apt-get install -y -q --no-install-recommends ca-certificates curl git nodejs xvfb x11vnc
    curl --connect-timeout 15 --max-time 180 -fsSL \
        https://github.com/novnc/noVNC/archive/refs/tags/v1.6.0.tar.gz -o /tmp/novnc.tar.gz
    printf '%s  /tmp/novnc.tar.gz\n' 5066103959ef4e9b10f37e5a148627360dd8414e4cf8a7db92bdbd022e728aaa | sha256sum --check --status
    mkdir /tmp/novnc
    tar -xzf /tmp/novnc.tar.gz -C /tmp/novnc --strip-components=1
    # Keep the host virtualenv, source and generated fixture state out of this container's work area.
    mkdir /work
    cp -a /source/{pyproject.toml,uv.lock,README.md,LICENSE,THIRD_PARTY_NOTICES,src,scripts,tests} /work/
    cd /work
    uv sync --locked --extra login
    uv run --frozen patchright install --with-deps chromium
    uv run --frozen python -m signin_window.browser install-policy
    export UBER_TEST_CHROME
    UBER_TEST_CHROME="$(uv run --frozen python -c 'from patchright.sync_api import sync_playwright; p=sync_playwright().start(); print(p.chromium.executable_path); p.stop()')"
    exec uv run --frozen python -m pytest scripts/test_connections_e2e.py \
        --junitxml=/reports/junit.xml -o junit_suite_name=connections
fi
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
: "${POCKET_ID_BIN:=/tmp/pocket-id}"
test -x "$POCKET_ID_BIN"
# Same Python base already pinned in the project images; no mutable test-container tag.
image=python:3.12.14-slim-trixie@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
uv_binary="$(realpath "$(command -v uv)")"
case "$("$uv_binary" --version)" in
    'uv 0.13.5'|'uv 0.13.5 '*) ;;
    *) echo 'Connections acceptance requires uv 0.13.5' >&2; exit 1 ;;
esac
cache_mounts=()
browser_cache="${CI_BROWSER_CACHE:-/tmp/browsers}"
cache_root=''
if [ -n "${FLEET_TOOLS:-}" ] && ! cache_root="$(python3 "$FLEET_TOOLS/native.py" container-cache)"; then
    echo 'Download cache unavailable; using disposable downloads' >&2
    cache_root=''
    browser_cache=''
fi
if [ -n "$cache_root" ]; then
    cache_mounts=(--mount "type=bind,src=$cache_root/uv,dst=/tmp/uv-cache"
                  --mount "type=bind,src=$cache_root/pip,dst=/tmp/pip-cache")
    browser_cache="$cache_root/browsers"
fi
mkdir -p .artifacts
if [ -n "$browser_cache" ]; then
    mkdir -p "$browser_cache"
    cache_mounts+=(--mount "type=bind,src=$(cd "$browser_cache" && pwd),dst=/browsers")
fi
umask 077
invocation="$(mktemp -d "$ROOT/.artifacts/connections-XXXXXXXX")"
name="uber-connections-$(basename "$invocation")-$$"
cleanup() { timeout 20 docker rm -f "$name" >/dev/null 2>&1 || true; }
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
# Only dependency and browser downloads persist; identity and test state are disposable.
python3 scripts/ci-log.py "$invocation/connections.log" timeout --signal=TERM --kill-after=30 900 \
    docker run --rm --name "$name" \
    "${cache_mounts[@]}" \
    --mount "type=bind,src=$uv_binary,dst=/usr/local/bin/uv,readonly" \
    -e UV_CACHE_DIR=/tmp/uv-cache -e PIP_CACHE_DIR=/tmp/pip-cache \
    --mount "type=bind,src=$ROOT,dst=/source,readonly" \
    --mount "type=bind,src=$POCKET_ID_BIN,dst=/tmp/pocket-id,readonly" \
    --mount "type=bind,src=$invocation,dst=/reports" \
    "$image" bash /source/scripts/check-connections.sh container
