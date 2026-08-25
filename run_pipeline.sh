#!/bin/bash
# run_pipeline.sh
# Nightly automation: refresh meeting inventory, transcribe new videos,
# rebuild site, and push to GitHub Pages (riverheadtranscripts.org).
#
# Triggered by launchd at 1am (com.riverhead.transcripts). Mac must be plugged in.
# Can also be run manually:  bash run_pipeline.sh
#
# AUTH: this script pushes over SSH. launchd jobs have no terminal, so an HTTPS
#       remote cannot prompt for credentials and the push silently fails with
#       "could not read Username ... Device not configured". The one-time SSH
#       setup is documented in riverhead_git_commands.txt.
#
# Logs:        ~/Library/Logs/riverhead/pipeline.log
# On failure:  writes a FAILED banner to the log AND shows a macOS notification,
#              so a broken push can never look like a success again.

set -euo pipefail

# Never let git or ssh hang waiting for input. Under launchd there is no
# terminal, so a prompt would either hang forever or die confusingly. With
# these set, auth problems fail fast and loud instead.
export GIT_TERMINAL_PROMPT=0
export GIT_SSH_COMMAND="ssh -o BatchMode=yes"

# launchd runs with a minimal PATH (/usr/bin:/bin:/usr/sbin:/sbin), so tools you
# installed yourself (pagefind, ffmpeg, node, ...) are invisible to it and fail
# with "command not found". Prepend the usual install locations so the job behaves
# under launchd exactly as it does in your interactive shell.
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/local/sbin:$HOME/.cargo/bin:$HOME/.local/bin:$HOME/.npm-global/bin:$PATH"

# Resolve the repo location from this script's own path, so the pipeline keeps
# working no matter where the repo lives (e.g. after moving it out of iCloud).
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$HOME/Library/Logs/riverhead"
LOG_FILE="$LOG_DIR/pipeline.log"

mkdir -p "$LOG_DIR"

# Prints a timestamped message to both terminal and log file.
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# Shows a macOS desktop notification (no-op if osascript is unavailable).
notify() {
    if command -v osascript >/dev/null 2>&1; then
        osascript -e "display notification \"$1\" with title \"Riverhead Pipeline\"" >/dev/null 2>&1 || true
    fi
}

# Runs on EVERY exit. If the pipeline died for any reason, make it loud.
on_exit() {
    ec=$?
    set +e
    if [ "$ec" -ne 0 ]; then
        log "=========================================="
        log "PIPELINE FAILED (exit code $ec). See $LOG_FILE"
        log "=========================================="
        notify "FAILED (exit $ec). Check pipeline.log"
    fi
}
trap on_exit EXIT

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
# Preflight: confirm required external tools are reachable before doing work,
# so the job fails early and loudly (printing PATH) instead of dying mid-build.
# ---------------------------------------------------------------------------
missing=""
for tool in python3 git pagefind; do
    command -v "$tool" >/dev/null 2>&1 || missing="$missing $tool"
done
if [ -n "$missing" ]; then
    log "ERROR: required tools not found on PATH:$missing"
    log "PATH=$PATH"
    log "Fix: add their install directory to the PATH= line near the top of this script."
    exit 1
fi
# ffmpeg is needed only for transcription (mlx-whisper decodes audio with it).
# Warn but do not abort, so the site still publishes even if transcription cannot.
command -v ffmpeg >/dev/null 2>&1 || log "WARNING: ffmpeg not on PATH; new meetings will not transcribe until it is added."

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
# Step 3b: Generate AI meeting summaries
# Non-fatal by design: if the key is missing or the API call fails, log a
# warning and keep going so the site still publishes. Only meetings without a
# current summary are processed, so this is cheap on a normal night.
# ---------------------------------------------------------------------------
log ""
log "--- Step 3b: Generating AI meeting summaries ---"
# launchd jobs have no shell profile, so load the API key from a gitignored
# env file (riverhead.env) if the variable is not already in the environment.
if [ -f "$PROJECT_DIR/riverhead.env" ]; then
    set -a; . "$PROJECT_DIR/riverhead.env"; set +a
fi
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    log "WARNING: ANTHROPIC_API_KEY not set; skipping AI summaries."
    log "         Create $PROJECT_DIR/riverhead.env with: ANTHROPIC_API_KEY=sk-ant-..."
elif run python3 riverhead_summarize.py --workers 4; then
    log "Step 3b complete."
else
    log "WARNING: summary generation failed; continuing so the site still publishes."
fi

# ---------------------------------------------------------------------------
# Step 3c: Extract decisions & roll-call votes (Town Board meetings)
# Non-fatal like Step 3b: a failure logs a warning and the site still
# publishes. Idempotent, so only new meetings are processed each night.
# ---------------------------------------------------------------------------
log ""
log "--- Step 3c: Extracting decisions and votes ---"
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    log "WARNING: ANTHROPIC_API_KEY not set; skipping vote extraction."
elif run python3 riverhead_extract_votes.py --days 14 --workers 2; then
    log "Step 3c complete."
else
    log "WARNING: vote extraction failed; continuing so the site still publishes."
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
# Step 5: Commit and push to GitHub  (the part that kept failing silently)
# ---------------------------------------------------------------------------
log ""
log "--- Step 5: Commit and push to GitHub ---"

# Guard: an HTTPS remote cannot authenticate under launchd. Warn loudly.
remote_url="$(git remote get-url origin)"
case "$remote_url" in
    https://*)
        log "WARNING: origin is HTTPS ($remote_url)."
        log "WARNING: automated push will fail under launchd. Switch origin to SSH."
        ;;
esac

# Clear a leftover .git/index.lock from a crashed/killed git process or a prior
# interrupted run; it blocks every commit. A real git op holds it for well under
# a second, so a lock older than 2 minutes is stale and safe to remove.
if [ -f .git/index.lock ]; then
    if [ -z "$(find .git/index.lock -mmin -2 2>/dev/null)" ]; then
        log "Clearing stale .git/index.lock (older than 2 min)."
        rm -f .git/index.lock
    else
        log "WARNING: a fresh .git/index.lock exists; another git process may be active. Leaving it."
    fi
fi

git add -A 2>&1 | tee -a "$LOG_FILE"

if git diff --cached --quiet; then
    log "Nothing new to commit."
else
    git commit -m "Add new transcripts" 2>&1 | tee -a "$LOG_FILE"
fi

# Refresh our view of origin so the ahead/behind math is accurate.
git fetch origin main 2>&1 | tee -a "$LOG_FILE"

pushed=0
ahead="$(git rev-list --count origin/main..HEAD 2>/dev/null || echo 0)"
if [ "$ahead" -gt 0 ]; then
    log "Local is ahead of origin by $ahead commit(s). Pushing."
    if git push origin main 2>&1 | tee -a "$LOG_FILE"; then
        pushed=1
    else
        # Non-fast-forward or transient failure: integrate origin, then retry once.
        log "Push was rejected. Pulling (no-rebase) and retrying."
        GIT_EDITOR=true git pull --no-rebase origin main 2>&1 | tee -a "$LOG_FILE"
        git push origin main 2>&1 | tee -a "$LOG_FILE"
        pushed=1
    fi
else
    log "origin/main is already level with local. Nothing to push."
fi

# Verify the push actually landed. This is the check the old script never did.
git fetch origin main 2>&1 | tee -a "$LOG_FILE"
local_sha="$(git rev-parse HEAD)"
remote_sha="$(git rev-parse origin/main)"
if [ "$local_sha" = "$remote_sha" ]; then
    log "Verified: origin/main == HEAD ($local_sha)."
    if [ "$pushed" -eq 1 ]; then
        notify "Published. Live shortly at riverheadtranscripts.org"
    fi
else
    log "ERROR: push did not land. HEAD=$local_sha origin/main=$remote_sha"
    exit 1
fi

log ""
log "=========================================="
log "Pipeline finished successfully."
log "=========================================="
