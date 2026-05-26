#!/bin/bash
# run_pipeline.sh
# Nightly automation: refresh meeting inventory, transcribe new videos, rebuild site.
# Triggered by launchd at 1am. Mac must be plugged in with lid closed.
#
# Logs are written to ~/Library/Logs/riverhead/pipeline.log

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

# Step 1: Refresh meeting inventory from CivicClerk API
log "Step 1: Refreshing meeting inventory..."
if python3 riverhead_inventory.py >> "$LOG_FILE" 2>&1; then
    log "Step 1 complete."
else
    log "ERROR: riverhead_inventory.py failed. Aborting."
    exit 1
fi

# Step 2: Transcribe any new meetings (last 14 days, skips already-transcribed)
log "Step 2: Transcribing new meetings..."
if python3 riverhead_transcribe.py --days 14 >> "$LOG_FILE" 2>&1; then
    log "Step 2 complete."
else
    log "ERROR: riverhead_transcribe.py failed. Aborting."
    exit 1
fi

# Step 3: Rebuild the site and search index
log "Step 3: Rebuilding site..."
if bash build.sh >> "$LOG_FILE" 2>&1; then
    log "Step 3 complete."
else
    log "ERROR: build.sh failed."
    exit 1
fi

log "Pipeline finished successfully."
log "=========================================="
