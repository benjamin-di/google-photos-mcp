#!/usr/bin/env bash
# The retained Connections check, in a disposable Python 3.13 container. Only
# downloads and pytest reports persist; profiles, identity state and screenshots die with it.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
umask 077
mkdir -p .artifacts
report="$(mktemp -d "$ROOT/.artifacts/connections-XXXXXXXX")"
# Only synthetic JUnit output crosses this mount from the container's root user.
chmod 755 "$report"
container="photos-connections-$(basename "$report")"
finish() {
    result=$?
    timeout 30 docker rm --force "$container" >/dev/null 2>&1 || true
    printf '%s\n' "$result" > "$report/exit-code.txt" || true
    exit "$result"
}
trap finish EXIT
pocket="$(realpath "${POCKET_ID_BIN:-/tmp/pocket-id}")"
uv_binary="$(realpath "$(command -v uv)")"
case "$("$uv_binary" --version)" in
    'uv 0.13.5'|'uv 0.13.5 '*) ;;
    *) echo 'Connections acceptance requires uv 0.13.5' >&2; exit 1 ;;
esac
# A digest pin for the same Python minor/distribution as the retained job.
image='python:3.13-slim-trixie@sha256:ae14d4ce8771606477a81446e7ef429edd6762305fc50c84507c1041fa8285d1'
# CI prepares the bounded container-owned buckets; local runs stay disposable.
cache_mounts=()
cache_root=''
if [ -n "${FLEET_TOOLS:-}" ] && ! cache_root="$(python3 "$FLEET_TOOLS/native.py" container-cache)"; then
    echo 'Download cache unavailable; using disposable downloads' >&2
    cache_root=''
fi
if [ -n "$cache_root" ]; then
    cache_mounts=(--mount "type=bind,src=$cache_root/uv,dst=/tmp/uv-cache"
                  --mount "type=bind,src=$cache_root/pip,dst=/tmp/pip-cache"
                  --mount "type=bind,src=$cache_root/browsers,dst=/tmp/browsers")
fi
printf 'Connections evidence: %s\n' "$report"
set +e
timeout --signal=TERM --kill-after=30s 900 docker run --rm --init --name "$container" \
    "${cache_mounts[@]}" \
    --workdir /workspace \
    -v "$ROOT:/workspace:ro" -v "$report:/reports" \
    -v "$pocket:/tmp/pocket-id:ro" -v "$uv_binary:/usr/local/bin/uv:ro" \
    -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONUNBUFFERED=1 \
    -e PLAYWRIGHT_SKIP_BROWSER_GC=1 -e PLAYWRIGHT_BROWSERS_PATH=/tmp/browsers \
    -e XDG_CONFIG_HOME=/tmp/browser-settings -e UV_LINK_MODE=copy \
    -e UV_PROJECT_ENVIRONMENT=/tmp/venv -e UV_CACHE_DIR=/tmp/uv-cache -e PIP_CACHE_DIR=/tmp/pip-cache \
    -e POCKET_ID_BIN=/tmp/pocket-id -e PHOTOS_LOGIN_NOVNC=/tmp/novnc \
    "$image" bash -euo pipefail -c '
        umask 022
        apt-get update -q
        apt-get install -y -q --no-install-recommends ca-certificates curl git xvfb x11vnc
        curl --connect-timeout 15 --max-time 180 -fsSL \
            https://github.com/novnc/noVNC/archive/refs/tags/v1.6.0.tar.gz -o /tmp/novnc.tar.gz
        printf "%s  /tmp/novnc.tar.gz\n" \
            5066103959ef4e9b10f37e5a148627360dd8414e4cf8a7db92bdbd022e728aaa | sha256sum --check --status
        mkdir /tmp/novnc
        tar -xzf /tmp/novnc.tar.gz -C /tmp/novnc --strip-components=1
        uv sync --locked --all-extras
        uv run --frozen patchright install --with-deps chromium
        uv run --frozen python -m signin_window.browser install-policy
        export PHOTOS_TEST_CHROME="$(uv run --frozen python -c '\''from patchright.sync_api import sync_playwright; p=sync_playwright().start(); print(p.chromium.executable_path); p.stop()'\'')"
        exec uv run --frozen python -m pytest -p no:cacheprovider --tb=short \
            -m browser --junitxml=/reports/junit.xml tests/test_connections_acceptance.py
    ' 2>&1 | python3 scripts/ci-log.py "$report/command.log"
statuses=("${PIPESTATUS[@]}")
set -e
[ "${statuses[1]}" -eq 0 ] || echo 'Connections command log unavailable' >&2
exit "${statuses[0]}"
