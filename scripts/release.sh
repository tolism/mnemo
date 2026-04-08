#!/usr/bin/env bash
# Release script for mneme.
#
# Usage:
#   scripts/release.sh test     # upload to TestPyPI
#   scripts/release.sh prod     # upload to real PyPI
#
# Prerequisites:
#   pip install -e ".[release]"
#   ~/.pypirc configured with API tokens for [pypi] and [testpypi],
#   or set TWINE_USERNAME=__token__ and TWINE_PASSWORD=<token> in your env.

set -euo pipefail

TARGET="${1:-test}"

cd "$(dirname "$0")/.."

echo "==> Cleaning previous build artifacts"
rm -rf build dist ./*.egg-info mneme.egg-info

echo "==> Building sdist + wheel"
python -m build

echo "==> Verifying package metadata + README rendering"
twine check dist/*

case "$TARGET" in
  test)
    echo "==> Uploading to TestPyPI"
    twine upload --repository testpypi dist/*
    echo
    echo "Test install with:"
    echo "  pip install --index-url https://test.pypi.org/simple/ \\"
    echo "      --extra-index-url https://pypi.org/simple/ mneme-cli"
    ;;
  prod)
    echo "==> Uploading to PyPI (production)"
    read -r -p "Are you sure you want to publish to real PyPI? [y/N] " confirm
    if [[ "${confirm,,}" != "y" && "${confirm,,}" != "yes" ]]; then
      echo "Aborted."
      exit 0
    fi
    twine upload dist/*
    echo
    echo "Done. Verify at https://pypi.org/project/mneme-cli/"
    ;;
  *)
    echo "Unknown target: $TARGET (use 'test' or 'prod')"
    exit 1
    ;;
esac
