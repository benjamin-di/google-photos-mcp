#!/usr/bin/env bash
# Exercise installed maintenance code, with synthetic tests and no network.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
image="${1:?usage: check-maintenance-image.sh IMAGE}"
deps="$(mktemp -d)"
trap 'rm -rf "$deps"' EXIT
version="$(python3 -c 'import tomllib; print(next(p["version"] for p in tomllib.load(open("uv.lock", "rb"))["package"] if p["name"] == "pytest"))')"
python3 -m pip install --quiet --disable-pip-version-check --target "$deps" "pytest==$version"
# Only disposable test dependencies are shared, never account or profile files.
chmod -R a+rX "$deps"
timeout 300 docker run --rm --init --network none --read-only --user 1000:1000 \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --tmpfs /tmp:rw,exec,mode=1777,size=1g \
  -v "$deps:/acceptance/deps:ro" -v "$PWD/tests:/acceptance/tests:ro" \
  -e PYTHONPATH=/acceptance/deps:/acceptance/tests -e PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  --entrypoint sh "$image" -c \
  'exec python -m pytest -p no:cacheprovider -q -o "markers=browser: native Chromium acceptance" /acceptance/tests/test_maintenance*.py'
