#!/bin/bash
# Re-index repository AND update cache metadata
# Usage: ./scripts/reindex-with-metadata.sh [repo-path] [repo-name]
#
# This script:
# 1. Runs graph-code load-index (with progress tracking)
# 2. Updates cache-metadata.json with current commit hash

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CGR_ROOT="$(dirname "$SCRIPT_DIR")"
# Default to coding repo (2 levels up from scripts dir in integrations/code-graph-rag)
DEFAULT_REPO_PATH="${CODING_TOOLS_PATH:-${CODING_REPO:-$(dirname "$(dirname "$CGR_ROOT")")}}"
REPO_PATH="${1:-$DEFAULT_REPO_PATH}"
REPO_NAME="${2:-coding}"
PROGRESS_FILE="$CGR_ROOT/shared-data/reindex-progress.json"
LOG_FILE="$CGR_ROOT/shared-data/reindex.log"

# Colors
GREEN='\033[0;32m'
CYAN='\033[0;36m'
RED='\033[0;31m'
NC='\033[0m'

# Function to write progress
write_progress() {
    local phase="$1"
    local step="$2"
    local total_steps="$3"
    local message="$4"
    local status="${5:-running}"

    cat > "$PROGRESS_FILE" << EOF
{
  "status": "$status",
  "phase": "$phase",
  "step": $step,
  "totalSteps": $total_steps,
  "message": "$message",
  "startedAt": "$START_TIME",
  "updatedAt": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
  "elapsedSeconds": $(($(date +%s) - START_EPOCH))
}
EOF
}

# Cleanup function
cleanup() {
    if [ "$1" = "error" ]; then
        write_progress "error" 0 5 "Indexing failed - check reindex.log" "failed"
    fi
}
trap 'cleanup error' ERR

# Initialize
START_TIME="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
START_EPOCH=$(date +%s)
mkdir -p "$CGR_ROOT/shared-data"

echo -e "${CYAN}Starting CGR re-index for: ${REPO_NAME}${NC}" | tee "$LOG_FILE"
echo "  Repository: $REPO_PATH" | tee -a "$LOG_FILE"
echo "  CGR Root: $CGR_ROOT" | tee -a "$LOG_FILE"

# Step 1: Initialize
write_progress "init" 1 5 "Initializing indexing process..."
cd "$CGR_ROOT"

# Step 2: Parse repository (protobuf loading)
write_progress "parsing" 2 5 "Parsing repository structure..."
echo -e "\n${CYAN}Step 2: Running graph-code load-index...${NC}" | tee -a "$LOG_FILE"

# Run indexer and capture output
# The indexer outputs progress to stdout which we can parse
{
    uv run graph-code load-index "$REPO_PATH" 2>&1 | while IFS= read -r line; do
        echo "$line" >> "$LOG_FILE"

        # Parse key progress messages from the indexer
        if [[ "$line" == *"Exporting protobuf to CSV"* ]]; then
            write_progress "exporting" 2 5 "Exporting code graph to CSV..."
        elif [[ "$line" == *"CSV export completed"* ]]; then
            write_progress "loading" 3 5 "Loading data into Memgraph..."
        elif [[ "$line" == *"Cleaning database"* ]]; then
            write_progress "loading" 3 5 "Cleaning database..."
        elif [[ "$line" == *"Loading via LOAD CSV"* ]]; then
            write_progress "loading" 4 5 "Bulk loading into Memgraph..."
        elif [[ "$line" == *"Index loaded successfully"* ]]; then
            write_progress "finalizing" 5 5 "Finalizing..."
        fi
    done
} || {
    echo -e "${RED}Error: Indexing failed${NC}" | tee -a "$LOG_FILE"
    write_progress "error" 0 5 "Indexing failed" "failed"
    exit 1
}

echo -e "${GREEN}Indexing completed successfully${NC}" | tee -a "$LOG_FILE"

# Step 3: Update cache metadata
write_progress "metadata" 5 5 "Updating cache metadata..."
echo -e "\n${CYAN}Step 3: Updating cache metadata...${NC}" | tee -a "$LOG_FILE"

# Get commit info from the indexed repo
if [[ -d "$REPO_PATH/.git" ]]; then
    COMMIT_HASH=$(cd "$REPO_PATH" && git rev-parse HEAD)
    COMMIT_SHORT=$(cd "$REPO_PATH" && git rev-parse --short HEAD)
    COMMIT_DATE=$(cd "$REPO_PATH" && git log -1 --format=%ci)
else
    echo -e "${RED}Warning: Repository not found at $REPO_PATH${NC}" | tee -a "$LOG_FILE"
    COMMIT_HASH="unknown"
    COMMIT_SHORT="unknown"
    COMMIT_DATE="unknown"
fi

# Calculate stats from shared-data
NODE_COUNT=$(wc -l < "$CGR_ROOT/shared-data/nodes/function.csv" 2>/dev/null | tr -d ' ' || echo "0")
REL_COUNT=$(wc -l < "$CGR_ROOT/shared-data/relationships.csv" 2>/dev/null | tr -d ' ' || echo "0")
TOTAL_SIZE=$(du -sh "$CGR_ROOT/shared-data" 2>/dev/null | cut -f1 || echo "0")

# Subtract 1 for header rows
NODE_COUNT=$((NODE_COUNT > 0 ? NODE_COUNT - 1 : 0))
REL_COUNT=$((REL_COUNT > 0 ? REL_COUNT - 1 : 0))

# Write metadata file
METADATA_FILE="$CGR_ROOT/shared-data/cache-metadata.json"

cat > "$METADATA_FILE" << EOF
{
  "repo_name": "$REPO_NAME",
  "repo_path": "$REPO_PATH",
  "commit_hash": "$COMMIT_HASH",
  "commit_short": "$COMMIT_SHORT",
  "commit_date": "$COMMIT_DATE",
  "indexed_at": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
  "stats": {
    "functions": $NODE_COUNT,
    "relationships": $REL_COUNT,
    "total_size": "$TOTAL_SIZE"
  },
  "version": "1.0.0"
}
EOF

# Final progress update - completed
ELAPSED=$(($(date +%s) - START_EPOCH))
cat > "$PROGRESS_FILE" << EOF
{
  "status": "completed",
  "phase": "complete",
  "step": 5,
  "totalSteps": 5,
  "message": "Re-index complete! Indexed $NODE_COUNT functions and $REL_COUNT relationships.",
  "startedAt": "$START_TIME",
  "completedAt": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
  "elapsedSeconds": $ELAPSED,
  "stats": {
    "functions": $NODE_COUNT,
    "relationships": $REL_COUNT,
    "commitShort": "$COMMIT_SHORT"
  }
}
EOF

echo -e "${GREEN}Cache metadata updated:${NC}" | tee -a "$LOG_FILE"
cat "$METADATA_FILE" | tee -a "$LOG_FILE"

echo -e "\n${GREEN}Re-index complete!${NC}" | tee -a "$LOG_FILE"
echo "  Commit: $COMMIT_SHORT" | tee -a "$LOG_FILE"
echo "  Functions: $NODE_COUNT" | tee -a "$LOG_FILE"
echo "  Relationships: $REL_COUNT" | tee -a "$LOG_FILE"
echo "  Elapsed: ${ELAPSED}s" | tee -a "$LOG_FILE"
