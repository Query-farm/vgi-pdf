#!/usr/bin/env bash
# Assert the release tag matches the version in pyproject.toml, so a `vX.Y.Z` release can
# never publish a wheel built from a mismatched version. Called with the tag as $1
# (e.g. "v0.1.0"). Dependency-free (no jq/uv required).
set -euo pipefail

tag="${1:?usage: check-version.sh <tag>}"
want="${tag#v}"  # strip a leading 'v'
have="$(grep -m1 '^version' pyproject.toml | sed -E 's/.*"([^"]+)".*/\1/')"

if [ "$want" != "$have" ]; then
  echo "Version mismatch: tag ${tag} (-> ${want}) != pyproject.toml version ${have}" >&2
  exit 1
fi
echo "Version OK: pyproject.toml ${have} matches tag ${tag}"
