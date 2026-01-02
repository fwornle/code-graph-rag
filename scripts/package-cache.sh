#!/bin/bash
# Package code-graph-rag cache for GitHub Release
# Usage: ./scripts/package-cache.sh [repo-name] [repo-path]
#
# This creates a tarball with:
# - shared-data/ (CSV files for LOAD CSV)
# - cache-metadata.json (commit hash, timestamp, stats)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CGR_ROOT="$(dirname "$SCRIPT_DIR")"
REPO_NAME="${1:-coding}"
REPO_PATH="${2:-/Users/q284340/Agentic/coding}"

# Colors
GREEN='\033[0;32m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${CYAN}Packaging code-graph-rag cache for: ${REPO_NAME}${NC}"

# Check shared-data exists
if [[ ! -d "$CGR_ROOT/shared-data" ]]; then
    echo "Error: shared-data/ directory not found. Run indexing first."
    exit 1
fi

# Get commit hash of indexed repo
COMMIT_HASH=""
if [[ -d "$REPO_PATH/.git" ]]; then
    COMMIT_HASH=$(cd "$REPO_PATH" && git rev-parse HEAD)
    COMMIT_SHORT=$(cd "$REPO_PATH" && git rev-parse --short HEAD)
    COMMIT_DATE=$(cd "$REPO_PATH" && git log -1 --format=%ci)
fi

# Calculate stats
NODE_COUNT=$(wc -l < "$CGR_ROOT/shared-data/nodes/function.csv" 2>/dev/null || echo "0")
REL_COUNT=$(wc -l < "$CGR_ROOT/shared-data/relationships.csv" 2>/dev/null || echo "0")
TOTAL_SIZE=$(du -sh "$CGR_ROOT/shared-data" | cut -f1)

# Create metadata file
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
    "functions": $((NODE_COUNT - 1)),
    "relationships": $((REL_COUNT - 1)),
    "total_size": "$TOTAL_SIZE"
  },
  "version": "1.0.0"
}
EOF

echo -e "${GREEN}Created metadata:${NC}"
cat "$METADATA_FILE"

# Create tarball
OUTPUT_DIR="$CGR_ROOT/dist"
mkdir -p "$OUTPUT_DIR"
TARBALL="$OUTPUT_DIR/cgr-cache-${REPO_NAME}-${COMMIT_SHORT:-latest}.tar.gz"

echo -e "\n${CYAN}Creating tarball: $TARBALL${NC}"
cd "$CGR_ROOT"
tar -czvf "$TARBALL" shared-data/

echo -e "\n${GREEN}Cache packaged successfully!${NC}"
echo "  File: $TARBALL"
echo "  Size: $(du -h "$TARBALL" | cut -f1)"
echo ""
echo "To create a GitHub release:"
echo "  gh release create v1.0.0-cache-${REPO_NAME} $TARBALL --title 'CGR Cache: ${REPO_NAME}' --notes 'Pre-indexed cache for ${REPO_NAME} repo (commit: ${COMMIT_SHORT})'"
