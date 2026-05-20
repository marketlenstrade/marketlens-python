#!/usr/bin/env bash
# Push a release tag to GitLab and create the corresponding Release object,
# pulling notes from CHANGELOG.md.
#
# Usage:
#   ./scripts/release.sh              releases the current pyproject.toml version
#   ./scripts/release.sh --backfill   creates a Release for every v* tag that
#                                     has a CHANGELOG entry and no Release yet
#
# Requires .env at repo root containing:
#   GITLAB_TOKEN=glpat-xxxxxxxxxxxxxxxxxxxx
# Token needs `api` scope. Project Access Token (Developer role + api) works too.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ ! -f .env ]]; then
  echo "error: .env not found at $REPO_ROOT/.env" >&2
  echo "add a line:  GITLAB_TOKEN=glpat-xxx" >&2
  exit 1
fi
# shellcheck disable=SC1091
set -a; source .env; set +a

if [[ -z "${GITLAB_TOKEN:-}" ]]; then
  echo "error: GITLAB_TOKEN not set in .env" >&2
  exit 1
fi

PROJECT_PATH="mktlns%2Fmarketlens-python"
API="https://gitlab.com/api/v4/projects/$PROJECT_PATH/releases"

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
  local code
  code=$(curl -sS -o /dev/null -w '%{http_code}' \
    -H "PRIVATE-TOKEN: $GITLAB_TOKEN" \
    "$API/$tag")
  [[ "$code" == "200" ]]
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
  local payload
  payload=$(NOTES="$notes" TAG="$tag" python3 -c '
import json, os
tag = os.environ["TAG"]
print(json.dumps({
    "tag_name":    tag,
    "name":        f"Release {tag}",
    "description": os.environ["NOTES"].strip(),
}))')
  if curl -sS --fail \
       -H "PRIVATE-TOKEN: $GITLAB_TOKEN" \
       -H "Content-Type: application/json" \
       -d "$payload" \
       "$API" > /dev/null; then
    echo "  created $tag"
  else
    echo "  FAILED $tag" >&2
    return 1
  fi
}

case "${1:-}" in
  --backfill)
    echo "Backfilling GitLab Releases for every v* tag..."
    for tag in $(git tag --sort=taggerdate -l 'v*'); do
      echo "$tag"
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
