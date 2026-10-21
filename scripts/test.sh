#!/usr/bin/env bash
# The gate: lint, format, lock consistency, then the test suite, with a private,
# source-bound receipt under .artifacts/.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
./scripts/bootstrap.sh
umask 077
mkdir -p .artifacts
invocation="$(mktemp -d "$ROOT/.artifacts/check-XXXXXXXX")"
# The receipt names the git tree it checked; outside a git checkout (an archive) it is empty.
identity() {
    if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        .venv/bin/python scripts/source-identity.py
    else
        echo '{}'
    fi
}
identity > "$invocation/source.json"
finish() {
    result=$?
    identity > "$invocation/final-source.json" || result=1
    if ! cmp -s "$invocation/source.json" "$invocation/final-source.json"; then
        echo "Source changed during verification; rerun against the final tree" >&2
        result=1
    fi
    printf '%s\n' "$result" > "$invocation/exit-code.txt"
    exit "$result"
}
trap finish EXIT
printf 'Evidence: %s\n' "$invocation"
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
# The image installs requirements-runtime.lock; it must be exactly uv.lock's runtime set.
uv export --frozen --no-dev --no-emit-project --no-header --quiet -o "$invocation/requirements-runtime.lock"
if ! diff -u requirements-runtime.lock "$invocation/requirements-runtime.lock"; then
    echo "requirements-runtime.lock is stale: run make lock" >&2
    exit 1
fi
.venv/bin/python -m pytest --junitxml="$invocation/junit.xml" "$@" 2>&1 | tee "$invocation/tests.log"
