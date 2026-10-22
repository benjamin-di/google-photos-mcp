#!/usr/bin/env bash
# Build and push one image as an immutable candidate, or reuse the candidate a previous run of the same commit
# pushed (its revision label must be that commit).
#   [PLATFORMS=linux/amd64,linux/arm64] [SOURCE_URL=...] build-image.sh CANDIDATE DOCKERFILE VCS_REF BUILD_DATE CACHE_REF
set -euo pipefail
candidate="${1:?candidate ref required}"
dockerfile="${2:?dockerfile required}"
vcs_ref="${3:?revision required}"
build_date="${4:?build date required}"
cache="${5:?cache ref required}"
lookup_error="$(mktemp)"
trap 'rm -f "$lookup_error"' EXIT
if docker manifest inspect "$candidate" >/dev/null 2>"$lookup_error"; then
    docker pull -q "$candidate" >/dev/null
    revision="$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$candidate")"
    [ "$revision" = "$vcs_ref" ] || { echo "ERROR: $candidate records unexpected revision $revision" >&2; exit 1; }
    echo "Reusing $candidate"
    exit 0
fi
# A registry or authentication failure is not evidence that an immutable tag is absent.
grep -Eqi 'manifest unknown|not found' "$lookup_error" || { cat "$lookup_error" >&2; exit 1; }
docker buildx build --provenance=false --output "${ZSTD_PUSH:?}" -f "$dockerfile" \
    --platform "${PLATFORMS:-linux/amd64}" \
    --build-arg VCS_REF="$vcs_ref" \
    ${SOURCE_URL:+--build-arg "SOURCE_URL=$SOURCE_URL"} \
    --build-arg BUILD_DATE="$build_date" \
    --cache-from "type=registry,ref=$cache" \
    --cache-to "type=registry,ref=$cache,mode=max" \
    -t "$candidate" .
