# syntax=docker/dockerfile:1.7
# photos-mcp-login: the login service (src/photos_login). Each account's own browser profile, shown to its person
# through noVNC on a sign-in link's page, and the keeper that refreshes each signed-in profile's snapshot. A virtual
# display (Xvfb), a remote-desktop server on loopback (x11vnc), patchright's Chromium and CJK fonts (Google shows the
# person's language). It never automates the sign-in (tests/test_login_service.py).
FROM ghcr.io/astral-sh/uv:0.9.26@sha256:9a23023be68b2ed09750ae636228e903a54a05ea56ed03a934d00fe9fbeded4b AS uv

FROM node:24.22.1-bookworm-slim@sha256:55fb5f5a527ea2c5c4a95f963e48ef90aa38c7b346054fdc8ee9957a2b55d205 AS browser-node

FROM python:3.12.14-slim-trixie@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

COPY runtime-installer.py /tmp/runtime-installer.py
RUN python /tmp/runtime-installer.py && rm /tmp/runtime-installer.py


# Security-patched Node for the browser driver's JavaScript transport.
COPY --from=browser-node /usr/local/bin/node /usr/local/bin/browser-node
ENV PLAYWRIGHT_NODEJS_PATH=/usr/local/bin/browser-node
RUN test "$(browser-node --version)" = v24.22.1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-patchright \
    HOME=/home/login \
    PHOTOS_LOGIN_NOVNC=/opt/novnc \
    PHOTOS_LOGIN_PROFILES=/profiles \
    PHOTOS_LOGIN_SESSIONS=/sessions

COPY --from=uv /uv /usr/local/bin/

RUN apt-get update && apt-get upgrade -y --no-install-recommends \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      dumb-init \
      fonts-noto-cjk \
      x11vnc \
      xvfb \
 && rm -rf /var/lib/apt/lists/*

# noVNC's own viewer (static files only; the WebSocket bridge is the service's, behind each window's view token).
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
 && install -d -o 1000 -g 1000 -m 0700 /home/login /profiles /sessions

WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE THIRD_PARTY_NOTICES ./
COPY src ./src
# The managed Chromium policy (signin-window's) that keeps a sign-in window from local files, DevTools, downloads and
# offers to save the password (every account's profile and snapshot lives in this container); the service opens no
# window without it (src/photos_login/window.py).
RUN uv pip install --system --compile-bytecode --no-deps . \
 && python -m signin_window.browser install-policy \
 && rm -rf /app/src

RUN python -c "from pathlib import Path; import patchright; p = Path(patchright.__file__).parent / 'driver' / 'node'; p.unlink(); p.symlink_to('/usr/local/bin/browser-node')" \
 && python -m patchright --version

ARG VCS_REF=unknown
ARG BUILD_DATE=unknown
ARG SOURCE_URL=unknown
LABEL org.opencontainers.image.title="photos-mcp-login" \
      org.opencontainers.image.description="Sign-in windows and the session keeper for photos-mcp's further accounts" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}"

# /profiles: each account's browser profile (its Google sign-in). /sessions: the snapshots photos-mcp reads.
EXPOSE 8080
USER 1000:1000
HEALTHCHECK --interval=60s --timeout=5s --start-period=2m --start-interval=2s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/health || exit 1
ENTRYPOINT ["dumb-init", "--"]
CMD ["python", "-m", "photos_login"]
