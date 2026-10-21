# syntax=docker/dockerfile:1.7
# uber-mcp-login: the browser a person signs in to Uber with (src/uber_login), shown to them through noVNC on its
# view page. A virtual display (Xvfb), a remote-desktop server on loopback (x11vnc), patchright's Chromium and CJK
# fonts (Uber shows the rider's language). It never automates the sign-in (tests/test_login_safety.py).
FROM ghcr.io/astral-sh/uv:0.9.26@sha256:9a23023be68b2ed09750ae636228e903a54a05ea56ed03a934d00fe9fbeded4b AS uv

FROM python:3.12.13-slim-trixie@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-patchright \
    HOME=/home/login \
    UBER_LOGIN_NOVNC=/opt/novnc

COPY --from=uv /uv /usr/local/bin/

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      dumb-init \
      fonts-noto-cjk \
      x11vnc \
      xvfb \
 && rm -rf /var/lib/apt/lists/*

# noVNC's own viewer (static files only; the WebSocket bridge is the service's, behind each sign-in's view token).
ARG NOVNC_VERSION=1.6.0
ARG NOVNC_SHA256=5066103959ef4e9b10f37e5a148627360dd8414e4cf8a7db92bdbd022e728aaa
RUN curl --fail --silent --show-error --location \
      "https://github.com/novnc/noVNC/archive/refs/tags/v${NOVNC_VERSION}.tar.gz" -o /tmp/novnc.tar.gz \
 && printf '%s  /tmp/novnc.tar.gz\n' "${NOVNC_SHA256}" | sha256sum --check --status \
 && mkdir -p /opt/novnc \
 && tar -xzf /tmp/novnc.tar.gz -C /opt/novnc --strip-components=1 \
      "noVNC-${NOVNC_VERSION}/app" "noVNC-${NOVNC_VERSION}/core" "noVNC-${NOVNC_VERSION}/vendor" \
      "noVNC-${NOVNC_VERSION}/vnc.html" \
 && rm /tmp/novnc.tar.gz

# Locked dependencies first, so a code-only release reuses the Chromium layer.
COPY requirements-login.lock /tmp/requirements-login.lock
RUN uv pip install --system --compile-bytecode --require-hashes -r /tmp/requirements-login.lock \
 && patchright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/* /tmp/requirements-login.lock \
 && useradd --uid 1000 --home-dir /home/login --shell /usr/sbin/nologin login \
 && install -d -o 1000 -g 1000 -m 0700 /home/login

WORKDIR /app
COPY pyproject.toml README.md LICENSE THIRD_PARTY_NOTICES ./
COPY src ./src
RUN uv pip install --system --compile-bytecode --no-deps . && rm -rf /app/src

ARG VCS_REF=unknown
ARG BUILD_DATE=unknown
ARG SOURCE_URL=https://github.com/benjamin-di/uber-mcp-server
LABEL org.opencontainers.image.title="uber-mcp-login" \
      org.opencontainers.image.description="The browser a person signs in to Uber with, for uber-mcp on a server" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}"

EXPOSE 8080
USER 1000:1000
ENTRYPOINT ["dumb-init", "--"]
CMD ["python", "-m", "uber_login"]
