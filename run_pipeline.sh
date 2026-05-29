#!/bin/bash
# run_pipeline.sh
# Nightly automation: refresh meeting inventory, transcribe new videos,
# rebuild site, and push to GitHub.
# Triggered by launchd at 1am. Mac must be plugged in.
#
# Can also be run manually:
#   bash run_pipeline.sh
#
# Logs: ~/Library/Logs/riverhead/pipeline.log

set -euo pipefail

PROJECT_DIR="/Users/lawrencemanfredi/Documents/1 Projects/riverhead-transcripts"
LOG_DIR="$HOME/Library/Logs/riverhead"
LOG_FILE="$LOG_DIR/pipeline.log"

mkdir -p "$LOG_DIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

log "=========================================="
log "Riverhead pipeline starting"
log "=========================================="

cd "$PROJECT_DIR"

# Step 1: Refresh meeting inventory from CivicClerk portal
log "Step 1: Refreshing meeting inventory..."
if python3 riverhead_inventory.py >> "$LOG_FILE" 2>&1; then
    log "Step 1 complete."
else
    log "ERROR: riverhead_inventory.py failed. Aborting."
    exit 1
fi

# Step 2: Backfill metadata on existing transcripts
log "Step 2: Backfilling transcript metadata..."
if python3 riverhead_backfill.py >> "$LOG_FILE" 2>&1; then
    log "Step 2 complete."
else
    log "ERROR: riverhead_backfill.py failed. Aborting."
    exit 1
fi

# Step 3: Transcribe any new meetings (last 14 days, skips already-done)
# caffeinate -i keeps the Mac awake during the (potentially long) transcription.
log "Step 3: Transcribing new meetings..."
if caffeinate -i python3 riverhead_transcribe.py --days 14 >> "$LOG_FILE" 2>&1; then
    log "Step 3 complete."
else
    log "ERROR: riverhead_transcribe.py failed. Aborting."
    exit 1
fi

# Step 4: Rebuild the static site and search index
log "Step 4: Rebuilding site..."
if python3 riverhead_build_site.py >> "$LOG_FILE" 2>&1; then
    log "Step 4a complete (HTML)."
else
    log "ERROR: riverhead_build_site.py failed. Aborting."
    exit 1
fi

if pagefind --site docs --output-subdir _pagefind >> "$LOG_FILE" 2>&1; then
    log "Step 4b complete (Pagefind index)."
else
    log "ERROR: pagefind failed. Aborting."
    exit 1
fi

# Step 5: Commit and push to GitHub
log "Step 5: Pushing to GitHub..."
git add -A >> "$LOG_FILE" 2>&1
if git diff --cached --quiet; then
    log "Step 5: Nothing new to commit."
else
    git commit -m "Add new transcripts" >> "$LOG_FILE" 2>&1
    git push >> "$LOG_FILE" 2>&1
    log "Step 5 complete."
fi

log "Pipeline finished successfully."
log "=========================================="
