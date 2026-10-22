# syntax=docker/dockerfile:1.7
# photos-mcp-login: the login service (src/photos_login). Each account's own browser profile, shown to its person
# through noVNC on a sign-in link's page, and the keeper that refreshes each signed-in profile's snapshot. A virtual
# display (Xvfb), a remote-desktop server on loopback (x11vnc), patchright's Chromium and CJK fonts (Google shows the
# person's language). It never automates the sign-in (tests/test_login_service.py).
FROM ghcr.io/astral-sh/uv:0.9.26@sha256:9a23023be68b2ed09750ae636228e903a54a05ea56ed03a934d00fe9fbeded4b AS uv

FROM python:3.12.13-slim-trixie@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-patchright \
    HOME=/home/login \
    PHOTOS_LOGIN_NOVNC=/opt/novnc \
    PHOTOS_LOGIN_PROFILES=/profiles \
    PHOTOS_LOGIN_SESSIONS=/sessions

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
# The managed Chromium policy that keeps a sign-in window from local files, DevTools and downloads (every account's
# profile and snapshot lives in this container); the service opens no window without it (src/photos_login/window.py).
RUN uv pip install --system --compile-bytecode --no-deps . \
 && for dir in /etc/opt/chrome_for_testing/policies/managed /etc/chromium/policies/managed; do \
      install -d -m 0755 "$dir" \
      && install -m 0644 src/photos_login/chromium-policy.json "$dir/photos-mcp-login.json"; \
    done \
 && rm -rf /app/src

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
