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

# Prints a timestamped message to both terminal and log file.
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# Runs a command, streaming all output to both terminal and log file.
# Returns the command's exit code even through the pipe.
run() {
    "$@" 2>&1 | tee -a "$LOG_FILE"
    return "${PIPESTATUS[0]}"
}

log "=========================================="
log "Riverhead pipeline starting"
log "=========================================="

cd "$PROJECT_DIR"

# ---------------------------------------------------------------------------
# Step 1: Refresh meeting inventory from CivicClerk portal
# ---------------------------------------------------------------------------
log ""
log "--- Step 1: Refreshing meeting inventory ---"
if run python3 riverhead_inventory.py; then
    log "Step 1 complete."
else
    log "ERROR: riverhead_inventory.py failed. Aborting."
    exit 1
fi

# ---------------------------------------------------------------------------
# Step 2: Backfill metadata on existing transcripts
# ---------------------------------------------------------------------------
log ""
log "--- Step 2: Backfilling transcript metadata ---"
if run python3 riverhead_backfill.py; then
    log "Step 2 complete."
else
    log "ERROR: riverhead_backfill.py failed. Aborting."
    exit 1
fi

# ---------------------------------------------------------------------------
# Step 3: Transcribe new meetings (caffeinate keeps Mac awake)
# ---------------------------------------------------------------------------
log ""
log "--- Step 3: Transcribing new meetings (last 14 days) ---"
if run caffeinate -i python3 riverhead_transcribe.py --days 14; then
    log "Step 3 complete."
else
    log "ERROR: riverhead_transcribe.py failed. Aborting."
    exit 1
fi

# ---------------------------------------------------------------------------
# Step 4: Rebuild static site and search index
# ---------------------------------------------------------------------------
log ""
log "--- Step 4a: Rebuilding HTML site ---"
if run python3 riverhead_build_site.py; then
    log "Step 4a complete."
else
    log "ERROR: riverhead_build_site.py failed. Aborting."
    exit 1
fi

log ""
log "--- Step 4b: Building Pagefind search index ---"
if run pagefind --site docs --output-subdir _pagefind; then
    log "Step 4b complete."
else
    log "ERROR: pagefind failed. Aborting."
    exit 1
fi

# ---------------------------------------------------------------------------
# Step 5: Commit and push to GitHub
# ---------------------------------------------------------------------------
log ""
log "--- Step 5: Pushing to GitHub ---"
git add -A 2>&1 | tee -a "$LOG_FILE"
if git diff --cached --quiet; then
    log "Step 5: Nothing new to commit."
else
    git commit -m "Add new transcripts" 2>&1 | tee -a "$LOG_FILE"
    git push 2>&1 | tee -a "$LOG_FILE"
    log "Step 5 complete."
fi

log ""
log "=========================================="
log "Pipeline finished successfully."
log "=========================================="
