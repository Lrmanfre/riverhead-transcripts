#!/bin/bash
# build.sh
# Builds the full Riverhead transcript site and search index.
# Run this any time you add new transcripts.
#
# Usage:
#   chmod +x build.sh
#   ./build.sh

set -e

echo "==> Building HTML site..."
python3 riverhead_build_site.py

echo ""
echo "==> Building Pagefind search index..."
pagefind --site docs --output-subdir _pagefind

echo ""
echo "==> Done! To preview locally:"
echo "    cd docs && python3 -m http.server 8000"
echo "    Then open http://localhost:8000"
