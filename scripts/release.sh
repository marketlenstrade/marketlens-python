#!/usr/bin/env bash
# Push a release tag to GitHub and create the corresponding Release,
# pulling notes from CHANGELOG.md.
#
# Usage:
#   ./scripts/release.sh              releases the current pyproject.toml version
#   ./scripts/release.sh --backfill   creates a Release for every v* tag that
#                                     has a CHANGELOG entry and no Release yet
#
# Requires the gh CLI, authenticated (gh auth login) with push access to the repo.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

REPO="pawelsibyl/marketlens-python"

if ! command -v gh >/dev/null 2>&1; then
  echo "error: gh CLI not found (brew install gh)" >&2
  exit 1
fi

extract_notes() {
  local version="$1"
  awk -v header="## [$version]" '
    index($0, header) == 1 { capture = 1; next }
    capture && /^## \[/    { exit }
    capture                { print }
  ' CHANGELOG.md
}

release_exists() {
  local tag="$1"
  gh release view "$tag" --repo "$REPO" >/dev/null 2>&1
}

create_release() {
  local tag="$1"
  local version="${tag#v}"
  local notes
  notes=$(extract_notes "$version")
  if [[ -z "${notes//[$'\n\t ']}" ]]; then
    echo "  skip $tag: no CHANGELOG entry"
    return 0
  fi
  if release_exists "$tag"; then
    echo "  skip $tag: release already exists"
    return 0
  fi
  if gh release create "$tag" --repo "$REPO" \
       --title "Release $tag" \
       --notes "$notes" \
       --verify-tag > /dev/null; then
    echo "  created $tag"
  else
    echo "  FAILED $tag" >&2
    return 1
  fi
}

case "${1:-}" in
  --backfill)
    echo "Backfilling GitHub Releases for every v* tag..."
    for tag in $(git tag --sort=taggerdate -l 'v*'); do
      echo "$tag"
      git push origin "$tag" >/dev/null 2>&1 || true
      create_release "$tag"
    done
    ;;
  '')
    version=$(awk -F\" '/^version = / { print $2; exit }' pyproject.toml)
    tag="v$version"
    notes=$(extract_notes "$version")
    if [[ -z "${notes//[$'\n\t ']}" ]]; then
      echo "error: no CHANGELOG entry for [$version]. Add one before releasing." >&2
      exit 1
    fi
    echo "Releasing $tag"
    if ! git rev-parse "$tag" >/dev/null 2>&1; then
      echo "  creating local tag $tag at HEAD"
      git tag -a "$tag" -m "Release $tag"
    fi
    echo "  pushing tag"
    git push origin "$tag"
    create_release "$tag"
    ;;
  *)
    echo "usage: $0 [--backfill]" >&2
    exit 2
    ;;
esac
