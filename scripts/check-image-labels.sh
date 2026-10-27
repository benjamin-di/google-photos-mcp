#!/usr/bin/env bash
# Check every Linux manifest's immutable application provenance before promotion.
set -euo pipefail
image="${1:?immutable image reference required}"
source_url="${2:?source URL required}"
revision="${3:?source revision required}"
version="${4:?application version required}"
docker buildx imagetools inspect "$image" --format '{{json .Image}}' \
  | jq -e --arg source "$source_url" --arg revision "$revision" --arg version "$version" '
    (if has("config") then [.] else
      [to_entries[] | select(.key | startswith("linux/")) | .value] end) as $images |
    ($images | length) > 0 and all($images[];
      .config.Labels["org.opencontainers.image.source"] == $source and
      .config.Labels["org.opencontainers.image.revision"] == $revision and
      .config.Labels["org.opencontainers.image.version"] == $version)
  ' >/dev/null
