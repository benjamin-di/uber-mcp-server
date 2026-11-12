#!/usr/bin/env bash
# The server release contract, against the exact candidate digest supplied by the publisher.
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
docker run --rm --network none --read-only --tmpfs /tmp --tmpfs /state:uid=1000 \
    -e UBER_SESSION_KEY=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA= \
    -e UBER_MAX_FARE='USD 80' -e UBER_MAX_FARE_24H='USD 200' \
    --entrypoint python "$image" -c 'from uber_mcp.server import UberService, create_server; from uber_mcp.config import Settings; s = Settings.from_env(); assert s.booking_enabled and create_server(UberService(s)).name == "uber"'
rc=0
timeout 120 docker run --rm --network none --read-only --tmpfs /tmp --tmpfs /state:uid=1000 "$image" || rc=$?
[ "$rc" = 1 ] || { echo "ERROR: expected a refusal without MCP_BEARER_TOKEN, got exit $rc" >&2; exit 1; }
name="smoke-server-$$-$RANDOM"
cleanup() { timeout 20 docker rm -f "$name" >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker run -d --name "$name" --network none --read-only --tmpfs /tmp --tmpfs /state:uid=1000 \
    -e MCP_BEARER_TOKEN=smoke-token-0123456789abcdef "$image" >/dev/null
for _ in $(seq 30); do docker exec "$name" uber-mcp health && break; sleep 1; done
# No unrestricted container logs: a fixed health failure is sufficient here.
docker exec "$name" uber-mcp health
