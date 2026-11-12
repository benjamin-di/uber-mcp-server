#!/usr/bin/env bash
# The browser/display, refusal and offline clipboard acceptance of the login image.
set -euo pipefail
image="${1:?image digest required}"
[[ "$image" =~ @sha256:[a-f0-9]{64}$ ]] || exit 2
docker pull -q "$image" >/dev/null
revision="$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$image")"
version="$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.version"}}' "$image")"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
docker run --rm --network none --read-only --tmpfs /tmp \
    --mount "type=bind,src=$ROOT/scripts/check-build-identity.py,dst=/identity-acceptance.py,readonly" \
    --entrypoint python "$image" -I /identity-acceptance.py "$version" "$revision"
docker run --rm --network none --entrypoint sh "$image" -c '
    set -e
    [ "$(id -u)" = 1000 ]
    python -c "import uber_login.app, uber_login.window, signin_window.service, patchright"
    command -v Xvfb >/dev/null && command -v x11vnc >/dev/null
    ls /ms-patchright | grep -q chromium
    [ -f /opt/novnc/vnc.html ] && [ -f /opt/novnc/core/rfb.js ]
    ls /usr/share/fonts/opentype/noto/ | grep -q CJK'
rc=0
timeout 60 docker run --rm --network none "$image" || rc=$?
[ "$rc" = 1 ] || { echo "ERROR: expected a refusal without UBER_LOGIN_TOKEN, got exit $rc" >&2; exit 1; }
name="smoke-login-$$-$RANDOM"
cleanup() { timeout 20 docker rm -f "$name" >/dev/null 2>&1 || true; }
trap cleanup EXIT
timeout --signal=TERM --kill-after=20 300 docker run --rm --name "$name" --network none \
    --mount "type=bind,src=$ROOT/scripts/paste_e2e.py,dst=/tmp/paste_e2e.py,readonly" \
    --entrypoint python "$image" /tmp/paste_e2e.py
