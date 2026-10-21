# syntax=docker/dockerfile:1.7
# photos-mcp: source on top of photos-mcp-deps (Dockerfile.deps). The release workflow builds
# both stages as one chain; locally:
#   docker build -f Dockerfile.deps -t photos-mcp-deps . && docker build -t photos-mcp .
ARG PHOTOS_MCP_DEPS_IMAGE=photos-mcp-deps:latest
FROM ${PHOTOS_MCP_DEPS_IMAGE}

WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE THIRD_PARTY_NOTICES ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system --compile-bytecode --no-deps . \
    && rm -rf /app/src

# /session: the read-only snapshot. /state: budgets, breaker and the thumbnail cache. Runs as
# uid 1000 and works on a read-only root filesystem.
RUN mkdir -p /session /state && chown 1000:1000 /state

ARG VCS_REF=unknown
ARG BUILD_DATE=unknown
ARG SOURCE_URL=unknown
LABEL org.opencontainers.image.title="photos-mcp" \
      org.opencontainers.image.description="Read-only MCP server for one Google Photos library" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.revision="${VCS_REF}" \
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
