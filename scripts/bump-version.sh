#!/usr/bin/env bash
# Stamp ONE version across every file that carries it.
#
# semantic-release is the source of truth: it derives the next version from Conventional
# Commits, calls this to write it into the tree, then commits and tags. The orchestrator
# reads its own package metadata for the OpenAPI version, so a version living only in a git
# tag would leave /docs reporting something that was never released.
#
#   scripts/bump-version.sh 0.2.0
#
# Wired into .releaserc.json via @semantic-release/exec; the git plugin commits the result.
# The SDK tracks the same version: it ships from this repo, and a second lineage is one more
# number to keep honest for no benefit to anyone reading it.
set -euo pipefail

V="${1:?usage: bump-version.sh <version>   e.g. 0.2.0}"
[[ "$V" =~ ^[0-9]+\.[0-9]+\.[0-9]+ ]] || { echo "not a semver: $V" >&2; exit 1; }
cd "$(dirname "$0")/.."

# Only the FIRST `version =` line in each file — that's the [project]/[package] one, never a
# dependency pin further down.
for f in pyproject.toml sdk/pyproject.toml warden/Cargo.toml; do
    sed -i "0,/^version = /s|^version = .*|version = \"$V\"|" "$f"
done
# Cargo.lock records the warden crate's own version, and a stale entry fails the
# `cargo test --locked` gate in CI.
if command -v cargo >/dev/null 2>&1; then
    (cd warden && cargo update --workspace --quiet) || true
fi

echo "stamped $V:"
grep -m1 '^version' pyproject.toml sdk/pyproject.toml warden/Cargo.toml
