#!/bin/bash
# Check code-graph-rag cache staleness
# Usage: ./scripts/check-cache-staleness.sh [repo-path]
#
# Exit codes:
#   0 - Cache is fresh (within threshold)
#   1 - Cache is stale (commits behind)
#   2 - No cache metadata found
#
# Output: JSON with staleness info

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CGR_ROOT="$(dirname "$SCRIPT_DIR")"
# Default to coding repo (2 levels up from scripts dir in integrations/code-graph-rag)
DEFAULT_REPO_PATH="${CODING_TOOLS_PATH:-${CODING_REPO:-$(dirname "$(dirname "$CGR_ROOT")")}}"
REPO_PATH="${1:-$DEFAULT_REPO_PATH}"
STALENESS_THRESHOLD="${2:-50}"  # commits behind threshold

METADATA_FILE="$CGR_ROOT/shared-data/cache-metadata.json"

# Check metadata exists
if [[ ! -f "$METADATA_FILE" ]]; then
    cat << EOF
{
  "status": "no_cache",
  "message": "No cache metadata found - indexing required",
  "cached_commit": null,
  "current_commit": null,
  "commits_behind": null,
  "is_stale": true
}
EOF
    exit 2
fi

# Read cached commit
CACHED_COMMIT=$(jq -r '.commit_hash // empty' "$METADATA_FILE" 2>/dev/null)
CACHED_DATE=$(jq -r '.indexed_at // empty' "$METADATA_FILE" 2>/dev/null)
REPO_NAME=$(jq -r '.repo_name // "unknown"' "$METADATA_FILE" 2>/dev/null)

if [[ -z "$CACHED_COMMIT" ]]; then
    cat << EOF
{
  "status": "invalid_cache",
  "message": "Cache metadata missing commit hash",
  "cached_commit": null,
  "current_commit": null,
  "commits_behind": null,
  "is_stale": true
}
EOF
    exit 2
fi

# Get current commit
if [[ ! -d "$REPO_PATH/.git" ]]; then
    cat << EOF
{
  "status": "no_repo",
  "message": "Repository not found at $REPO_PATH",
  "cached_commit": "$CACHED_COMMIT",
  "current_commit": null,
  "commits_behind": null,
  "is_stale": true
}
EOF
    exit 2
fi

CURRENT_COMMIT=$(cd "$REPO_PATH" && git rev-parse HEAD)
CURRENT_SHORT=$(cd "$REPO_PATH" && git rev-parse --short HEAD)
CACHED_SHORT="${CACHED_COMMIT:0:7}"

# Check if commits match
if [[ "$CACHED_COMMIT" == "$CURRENT_COMMIT" ]]; then
    cat << EOF
{
  "status": "fresh",
  "message": "Cache is up to date",
  "repo_name": "$REPO_NAME",
  "cached_commit": "$CACHED_SHORT",
  "current_commit": "$CURRENT_SHORT",
  "commits_behind": 0,
  "indexed_at": "$CACHED_DATE",
  "is_stale": false
}
EOF
    exit 0
fi

# Count commits behind
COMMITS_BEHIND=$(cd "$REPO_PATH" && git rev-list --count "$CACHED_COMMIT".."$CURRENT_COMMIT" 2>/dev/null || echo "unknown")

if [[ "$COMMITS_BEHIND" == "unknown" ]]; then
    # Cached commit not in history (force pushed or rebased)
    cat << EOF
{
  "status": "diverged",
  "message": "Cache commit not in current history - reindex recommended",
  "repo_name": "$REPO_NAME",
  "cached_commit": "$CACHED_SHORT",
  "current_commit": "$CURRENT_SHORT",
  "commits_behind": null,
  "indexed_at": "$CACHED_DATE",
  "is_stale": true
}
EOF
    exit 1
fi

# Determine if stale based on threshold
IS_STALE="false"
STATUS="fresh"
MESSAGE="Cache is $COMMITS_BEHIND commits behind"

if [[ "$COMMITS_BEHIND" -gt "$STALENESS_THRESHOLD" ]]; then
    IS_STALE="true"
    STATUS="stale"
    MESSAGE="Cache is $COMMITS_BEHIND commits behind (threshold: $STALENESS_THRESHOLD)"
fi

cat << EOF
{
  "status": "$STATUS",
  "message": "$MESSAGE",
  "repo_name": "$REPO_NAME",
  "cached_commit": "$CACHED_SHORT",
  "current_commit": "$CURRENT_SHORT",
  "commits_behind": $COMMITS_BEHIND,
  "threshold": $STALENESS_THRESHOLD,
  "indexed_at": "$CACHED_DATE",
  "is_stale": $IS_STALE
}
EOF

if [[ "$IS_STALE" == "true" ]]; then
    exit 1
else
    exit 0
fi
