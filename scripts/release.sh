#!/usr/bin/env bash
# Release script for mneme (uv-native).
#
# Usage:
#   scripts/release.sh test     # upload to TestPyPI
#   scripts/release.sh prod     # upload to real PyPI
#   scripts/release.sh check    # build + twine check + clean-venv install (no upload)
#
# Prerequisites:
#   - uv installed: https://docs.astral.sh/uv/
#   - For uploads: ~/.pypirc with API tokens for [pypi] and [testpypi],
#     OR set TWINE_USERNAME=__token__ and TWINE_PASSWORD=<token> in your env.
#
# Notes:
#   - Build / check / install all run via `uv` (no global pip required).
#   - Uploads use `uv tool run twine` which fetches twine on demand.

set -euo pipefail

TARGET="${1:-check}"

cd "$(dirname "$0")/.."

if ! command -v uv >/dev/null 2>&1; then
  echo "Error: uv is not installed. Install it from https://docs.astral.sh/uv/" >&2
  exit 1
fi

echo "==> Cleaning previous build artifacts"
rm -rf build dist ./*.egg-info mneme.egg-info

echo "==> Building sdist + wheel with uv"
uv build

echo "==> Verifying package metadata + README rendering"
uv tool run twine check dist/*

WHEEL=$(ls dist/*.whl | head -n 1)
CHECK_VENV="${TMPDIR:-/tmp}/mneme-release-check"

echo "==> Smoke-testing wheel in a clean uv venv ($CHECK_VENV)"
rm -rf "$CHECK_VENV"
uv venv "$CHECK_VENV"
uv pip install --python "$CHECK_VENV" "$WHEEL"

# Resolve the venv's mneme entry point cross-platform
if [ -x "$CHECK_VENV/bin/mneme" ]; then
  MNEME_BIN="$CHECK_VENV/bin/mneme"
elif [ -x "$CHECK_VENV/Scripts/mneme.exe" ]; then
  MNEME_BIN="$CHECK_VENV/Scripts/mneme.exe"
else
  echo "Error: could not locate mneme entry point in $CHECK_VENV" >&2
  exit 1
fi

"$MNEME_BIN" --version

case "$TARGET" in
  check)
    echo
    echo "Pre-flight passed. Run 'scripts/release.sh test' to upload to TestPyPI."
    exit 0
    ;;
  test)
    echo "==> Uploading to TestPyPI"
    uv tool run twine upload --repository testpypi dist/*
    echo
    echo "Test install with:"
    echo "  uv pip install --index-url https://test.pypi.org/simple/ \\"
    echo "      --extra-index-url https://pypi.org/simple/ mneme-cli"
    ;;
  prod)
    echo "==> Uploading to PyPI (production)"
    read -r -p "Are you sure you want to publish to real PyPI? [y/N] " confirm
    if [[ "${confirm,,}" != "y" && "${confirm,,}" != "yes" ]]; then
      echo "Aborted."
      exit 0
    fi
    uv tool run twine upload dist/*
    echo
    echo "Done. Verify at https://pypi.org/project/mneme-cli/"
    ;;
  *)
    echo "Unknown target: $TARGET (use 'check', 'test', or 'prod')" >&2
    exit 1
    ;;
esac
