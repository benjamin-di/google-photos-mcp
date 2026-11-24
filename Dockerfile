# syntax=docker/dockerfile:1.7
# photos-mcp: source on top of photos-mcp-deps (inlined as the default `deps` stage, or passed via
# PHOTOS_MCP_DEPS_IMAGE when built from Dockerfile.deps).
ARG PHOTOS_MCP_DEPS_IMAGE=deps
FROM ghcr.io/astral-sh/uv:0.9.26@sha256:9a23023be68b2ed09750ae636228e903a54a05ea56ed03a934d00fe9fbeded4b AS uv

FROM python:3.12.14-slim-trixie@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS deps

COPY runtime-installer.py /tmp/runtime-installer.py
RUN python /tmp/runtime-installer.py && rm /tmp/runtime-installer.py

COPY --from=uv /uv /usr/local/bin/uv

COPY requirements-runtime.lock /tmp/requirements-runtime.lock
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system --compile-bytecode --require-hashes -r /tmp/requirements-runtime.lock \
    && rm -f /tmp/requirements-runtime.lock \
    && python -c 'import curl_cffi, PIL, fastmcp, tzdata; assert fastmcp.__version__ == "4.0.10"'

FROM ${PHOTOS_MCP_DEPS_IMAGE}

WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE THIRD_PARTY_NOTICES ./
COPY src ./src
COPY scripts/embed-build-identity.py /tmp/embed-build-identity.py
ARG VERSION
ARG VCS_REF
RUN --mount=type=cache,target=/root/.cache/uv \
    python /tmp/embed-build-identity.py "$VERSION" "$VCS_REF" \
    && uv pip install --system --compile-bytecode --no-deps . \
    && rm -rf /app/src

# /session: the read-only snapshot. /state: budgets, breaker and the thumbnail cache. Runs as
# uid 1000 and works on a read-only root filesystem.
RUN mkdir -p /session /state && chown 1000:1000 /state

ARG BUILD_DATE=unknown
ARG SOURCE_URL=unknown
LABEL org.opencontainers.image.title="photos-mcp" \
      org.opencontainers.image.description="MCP server for Google Photos libraries: search, view, upload, trash, albums" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.created="${BUILD_DATE}"

ENV PYTHONUNBUFFERED=1 \
    PHOTOS_SESSION_DIR=/session \
    PHOTOS_STATE_DIR=/state
USER 1000:1000
EXPOSE 8000
# `docker run IMAGE` serves HTTP on 0.0.0.0:8000 inside the container (publish the port on
# 127.0.0.1 or behind a TLS proxy); `docker run -i IMAGE --transport stdio` speaks stdio.
ENTRYPOINT ["photos-mcp"]
CMD ["--host", "0.0.0.0"]
