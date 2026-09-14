#!/usr/bin/env bash
# The product checks. CI runs exactly this script; run it locally before a PR.
#
# Every check below either passes on a real input or is proven to fail on a
# fixture built to fail. A check that cannot fail is not a check.
set -euo pipefail
cd "$(dirname "$0")/.."

export PYTHONPATH=src

must_fail() {
  # Run a check that is expected to fail; succeed only if it does.
  local label=$1; shift
  if "$@" >/dev/null 2>&1; then
    echo "FAIL: '$label' passed but was built to fail" >&2
    exit 1
  fi
  echo "ok: '$label' fails as built"
}

echo "== lint"
ruff check .
ruff format --check .

echo "== unit and fake-CLI tests"
pytest -q

echo "== manifest check"
python -m omakron.checks manifest .

echo "== fixtures built to fail"
must_fail "broken manifest"   python -m omakron.checks manifest tests/fixtures/plugins/missing-entrypoint
must_fail "usage error"       python -m omakron.checks

echo "== plugin folder hygiene"
if link=$(find . \( -name .git -o -name .venv \) -prune -o -type l -print -quit) && [[ -n $link ]]; then
  echo "FAIL: symlink in plugin folder: $link" >&2; exit 1
fi

echo "all checks passed"
