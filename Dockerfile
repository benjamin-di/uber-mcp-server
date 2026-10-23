# syntax=docker/dockerfile:1.7
# uber-mcp: the MCP server over streamable HTTP at :8000/mcp (needs MCP_BEARER_TOKEN). It runs as uid 1000 and
# works on a read-only root filesystem; /state (a volume) keeps the session (sealed under UBER_SESSION_KEY), the
# budget, the pause, booking previews, the 24-hour spend and the audit log.
FROM ghcr.io/astral-sh/uv:0.9.26@sha256:9a23023be68b2ed09750ae636228e903a54a05ea56ed03a934d00fe9fbeded4b AS uv

FROM python:3.12.14-slim-trixie@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

COPY runtime-installer.py /tmp/runtime-installer.py
RUN python /tmp/runtime-installer.py && rm /tmp/runtime-installer.py


ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    UBER_STATE_DIR=/state

# Locked dependencies first, so a code-only release reuses this layer.
COPY requirements-runtime.lock /tmp/requirements-runtime.lock
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system --compile-bytecode --require-hashes -r /tmp/requirements-runtime.lock \
 && rm /tmp/requirements-runtime.lock

WORKDIR /app
COPY pyproject.toml README.md LICENSE THIRD_PARTY_NOTICES ./
COPY src ./src
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    uv pip install --system --compile-bytecode --no-deps . \
 && rm -rf /app/src \
 && install -d -o 1000 -g 1000 -m 0700 /state

ARG VCS_REF=unknown
ARG BUILD_DATE=unknown
ARG SOURCE_URL=https://github.com/benjamin-di/uber-mcp-server
LABEL org.opencontainers.image.title="uber-mcp" \
      org.opencontainers.image.description="Unofficial MCP server for Uber rides" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}"

EXPOSE 8000
USER 1000:1000
HEALTHCHECK --interval=60s --timeout=5s --start-period=2m --start-interval=2s CMD ["uber-mcp", "health"]
ENTRYPOINT ["uber-mcp"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
