#!/usr/bin/env bash
# The gate: lint, format, lock consistency, then the test suite, with a private,
# source-bound receipt under .artifacts/.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
umask 077
mkdir -p .artifacts
invocation="$(mktemp -d "$ROOT/.artifacts/check-XXXXXXXX")"
# The receipt names the git tree it checked; outside a git checkout (an archive) it is empty.
identity() {
    if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        python3 scripts/source-identity.py
    else
        echo '{}'
    fi
}
identity > "$invocation/source.json"
finish() {
    result=$?
    if ! identity > "$invocation/final-source.json" || \
       ! cmp -s "$invocation/source.json" "$invocation/final-source.json"; then
        echo "Source changed or could not be identified during verification; rerun against the final tree" >&2
        # Preserve an existing command failure (including pytest's collection/usage codes).
        [ "$result" -ne 0 ] || result=1
    fi
    printf '%s\n' "$result" > "$invocation/exit-code.txt" || true
    exit "$result"
}
trap finish EXIT
logged() {
    local label="$1"
    shift
    set +e
    "$@" 2>&1 | python3 scripts/ci-log.py "$invocation/$label.log"
    local statuses=("${PIPESTATUS[@]}")
    set -e
    [ "${statuses[1]}" -eq 0 ] || echo "Command log unavailable for $label" >&2
    return "${statuses[0]}"
}
printf 'Evidence: %s\n' "$invocation"
logged bootstrap ./scripts/bootstrap.sh
logged lint .venv/bin/ruff check src tests scripts
logged format .venv/bin/ruff format --check src tests scripts
# The images install requirements-runtime.lock (server) and requirements-login.lock (login service); each must be
# exactly uv.lock's set.
logged lock-runtime uv export --frozen --no-dev --no-emit-project --no-header --quiet -o "$invocation/requirements-runtime.lock"
logged lock-login uv export --frozen --no-dev --extra login --no-emit-project --no-header --quiet -o "$invocation/requirements-login.lock"
for lock in requirements-runtime.lock requirements-login.lock; do
    if ! logged "$lock" diff -u "$lock" "$invocation/$lock"; then
        echo "$lock is stale: run make lock" >&2
        exit 1
    fi
done
logged tests .venv/bin/python -m pytest --junitxml="$invocation/junit.xml" "$@"
