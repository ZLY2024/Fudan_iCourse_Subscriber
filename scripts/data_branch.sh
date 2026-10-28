#!/usr/bin/env bash
# Fetch a database snapshot, then publish only if that snapshot is still current.
set -euo pipefail

case "${1:-}" in
  fetch)
    # Exit 2 means the branch does not exist. All other failures must abort;
    # a network/authentication error is never permission to start a fresh DB.
    status=0
    git ls-remote --exit-code --heads origin refs/heads/data >/dev/null || status=$?
    if [ "$status" -eq 2 ]; then
      echo "DATA_BASE_SHA=" >> "${GITHUB_ENV:?GITHUB_ENV is required}"
      echo "No data branch, starting fresh."
    elif [ "$status" -ne 0 ]; then
      echo "::error::Unable to inspect the data branch."
      exit "$status"
    else
      git fetch --no-tags --depth=1 origin "+refs/heads/data:refs/remotes/origin/data"
      base=$(git rev-parse --verify refs/remotes/origin/data)
      echo "DATA_BASE_SHA=$base" >> "${GITHUB_ENV:?GITHUB_ENV is required}"
    fi
    ;;
  push)
    # An empty expected SHA allows creation ONLY while the branch is absent.
    # Use the SHA from the initial read, never a newly fetched tracking ref.
    : "${DATA_BASE_SHA?Fetch the data branch before publishing}"
    if ! git push --force-with-lease="refs/heads/data:${DATA_BASE_SHA}" \
        origin HEAD:refs/heads/data; then
      echo "::error::Database publish failed. The remote may have changed; refusing to overwrite it. Re-run the workflow against the latest database."
      exit 1
    fi
    ;;
  *)
    echo "Usage: bash scripts/data_branch.sh {fetch|push}" >&2
    exit 2
    ;;
esac
