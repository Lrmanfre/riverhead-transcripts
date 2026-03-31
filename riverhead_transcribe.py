#!/usr/bin/env python3
"""
riverhead_transcribe.py
=======================
Downloads each meeting video from riverhead_meetings.csv and transcribes
it using mlx-whisper (large-v3) on Apple Silicon.

Outputs per meeting (organized by category):
  transcripts/<category>/<date>_<event_id>.txt   — plain text transcript
  transcripts/<category>/<date>_<event_id>.json  — timestamped segments

Resumable: skips any meeting whose .txt already exists.
Videos are deleted after transcription to save disk space.

Usage:
    pip3 install mlx-whisper --break-system-packages

    # Process only meetings from the last 14 days (default):
    python3 riverhead_transcribe.py

    # Process meetings from the last N days:
    python3 riverhead_transcribe.py --days 30

    # Process meetings on or after a specific date:
    python3 riverhead_transcribe.py --since 2026-01-01

    # Process all meetings (full backfill):
    python3 riverhead_transcribe.py --all

Requirements: mlx-whisper, Python 3.6+
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

INPUT_CSV      = "riverhead_meetings_reversed.csv"
OUTPUT_DIR     = "transcripts"
TEMP_VIDEO_DIR = "tmp_videos"
MODEL          = "mlx-community/whisper-large-v3-mlx"
LANGUAGE       = "en"
TIMEOUT        = 120    # seconds for HTTP connections

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def slugify(text):
    """Convert a string to a safe directory/filename component."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text.strip("-") or "uncategorized"


def transcript_paths(category, event_date, event_id):
    """Return (txt_path, json_path) for a given event."""
    cat_dir  = os.path.join(OUTPUT_DIR, slugify(category or "uncategorized"))
    os.makedirs(cat_dir, exist_ok=True)
    stem = "{}_{}".format(event_date or "no-date", event_id)
    return (
        os.path.join(cat_dir, stem + ".txt"),
        os.path.join(cat_dir, stem + ".json"),
    )


def already_done(txt_path):
    return os.path.exists(txt_path) and os.path.getsize(txt_path) > 0


def download_video(url, dest_path):
    """Download a video file with progress reporting."""
    print("    Downloading: {}".format(url[:80]))
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "riverhead-transparency-bot/1.0"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        downloaded = 0
        chunk_size = 1024 * 1024   # 1 MB
        with open(dest_path, "wb") as f:
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = downloaded * 100 // total
                    print("    {} / {} MB  ({}%)".format(
                          downloaded // (1024*1024),
                          total // (1024*1024), pct), end="\r")
    print()   # newline after progress


def transcribe(video_path):
    """Run mlx-whisper and return the result dict."""
    import mlx_whisper
    print("    Transcribing with {} ...".format(MODEL))
    result = mlx_whisper.transcribe(
        video_path,
        path_or_hf_repo=MODEL,
        language=LANGUAGE,
        verbose=False,
        word_timestamps=False,
    )
    return result


def save_transcript(result, txt_path, json_path, meta):
    """Write plain-text and JSON transcript files."""
    # Plain text
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("RIVERHEAD TOWN MEETING TRANSCRIPT\n")
        f.write("=" * 50 + "\n")
        f.write("Date     : {}\n".format(meta.get("event_date", "")))
        f.write("Category : {}\n".format(meta.get("category", "")))
        f.write("Title    : {}\n".format(meta.get("title", "")))
        f.write("Event ID : {}\n".format(meta.get("event_id", "")))
        f.write("Video URL: {}\n".format(meta.get("video_url", "")))
        f.write("=" * 50 + "\n\n")
        f.write(result.get("text", "").strip())
        f.write("\n")

    # JSON with timestamps
    output = {
        "meta": meta,
        "transcript": result.get("text", "").strip(),
        "segments": [
            {
                "start": round(seg.get("start", 0), 2),
                "end":   round(seg.get("end", 0), 2),
                "text":  seg.get("text", "").strip(),
            }
            for seg in result.get("segments", [])
        ],
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_meetings(csv_path, since_date=None):
    """Read CSV and return rows that have a video URL.

    Args:
        since_date: datetime.date or None. If set, only rows with
                    event_date >= since_date are returned.
    """
    meetings = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if not row.get("video_url", "").strip():
                continue
            if since_date is not None:
                raw = row.get("event_date", "").strip()
                try:
                    row_date = datetime.strptime(raw, "%Y-%m-%d").date()
                except ValueError:
                    continue   # skip rows with unparseable dates
                if row_date < since_date:
                    continue
            meetings.append(row)
    return meetings


def parse_args():
    parser = argparse.ArgumentParser(
        description="Transcribe Riverhead town meeting videos.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--days",
        type=int,
        default=14,
        metavar="N",
        help="Process meetings from the last N days (default: 14).",
    )
    group.add_argument(
        "--since",
        metavar="YYYY-MM-DD",
        help="Process meetings on or after this date.",
    )
    group.add_argument(
        "--all",
        action="store_true",
        dest="all_meetings",
        help="Process ALL meetings (full backfill — slow).",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    # Determine the date filter
    if args.all_meetings:
        since_date = None
        date_label = "all dates"
    elif args.since:
        try:
            since_date = datetime.strptime(args.since, "%Y-%m-%d").date()
        except ValueError:
            print("ERROR: --since must be YYYY-MM-DD, got: {}".format(args.since))
            sys.exit(1)
        date_label = "on or after {}".format(since_date)
    else:
        since_date = (datetime.now() - timedelta(days=args.days)).date()
        date_label = "last {} days (since {})".format(args.days, since_date)

    # Verify mlx_whisper is importable before doing any work
    try:
        import mlx_whisper  # noqa: F401
    except ImportError:
        print("ERROR: mlx-whisper is not installed.")
        print("Run:  pip3 install mlx-whisper --break-system-packages")
        sys.exit(1)

    if not os.path.exists(INPUT_CSV):
        print("ERROR: {} not found. Run riverhead_inventory.py first.".format(INPUT_CSV))
        sys.exit(1)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(TEMP_VIDEO_DIR, exist_ok=True)

    meetings = load_meetings(INPUT_CSV, since_date=since_date)
    total    = len(meetings)

    print("=" * 60)
    print("Riverhead Meeting Transcription Pipeline")
    print("Model  : {}".format(MODEL))
    print("Filter : {}".format(date_label))
    print("Meetings with video in range: {}".format(total))
    print("Output : {}".format(OUTPUT_DIR))
    print("=" * 60)
    print()

    if total == 0:
        print("No meetings found for the specified date range.")
        print("Try a wider range with --days 30 or --all.")
        return

    done_count    = 0
    skipped_count = 0
    error_count   = 0
    t_start       = time.time()

    for i, row in enumerate(meetings, 1):
        event_id   = row.get("event_id", "unknown")
        event_date = row.get("event_date", "")
        category   = row.get("category", "uncategorized")
        title      = row.get("title", "")
        video_url  = row.get("video_url", "").strip()

        txt_path, json_path = transcript_paths(category, event_date, event_id)

        print("[{}/{}] {} | {} | {}".format(i, total, event_date, category[:30], title[:35]))

        # Skip if already transcribed
        if already_done(txt_path):
            print("    Already done — skipping.")
            skipped_count += 1
            continue

        # Download
        ext        = os.path.splitext(video_url.split("?")[0])[1] or ".mp4"
        video_path = os.path.join(TEMP_VIDEO_DIR, "event_{}{}" .format(event_id, ext))

        try:
            download_video(video_url, video_path)
        except Exception as e:
            print("    ERROR downloading: {}".format(e))
            error_count += 1
            continue

        # Transcribe
        t0 = time.time()
        try:
            result = transcribe(video_path)
        except Exception as e:
            print("    ERROR transcribing: {}".format(e))
            error_count += 1
            if os.path.exists(video_path):
                os.remove(video_path)
            continue
        elapsed = time.time() - t0
        print("    Transcribed in {:.0f}s — {} segments".format(
              elapsed, len(result.get("segments", []))))

        # Save
        meta = {
            "event_id":   event_id,
            "event_date": event_date,
            "category":   category,
            "title":      title,
            "video_url":  video_url,
            "transcribed_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        }
        save_transcript(result, txt_path, json_path, meta)
        print("    Saved: {}".format(txt_path))

        # Delete video
        os.remove(video_path)

        done_count += 1

        # ETA
        elapsed_total = time.time() - t_start
        rate = done_count / elapsed_total if elapsed_total > 0 else 0
        remaining = total - i
        eta_secs = remaining / rate if rate > 0 else 0
        eta_hrs  = eta_secs / 3600
        print("    ETA: ~{:.1f} hours remaining\n".format(eta_hrs))

    # Cleanup temp dir if empty
    try:
        os.rmdir(TEMP_VIDEO_DIR)
    except OSError:
        pass

    total_time = (time.time() - t_start) / 3600
    print("=" * 60)
    print("Done!")
    print("  Transcribed : {}".format(done_count))
    print("  Skipped     : {} (already existed)".format(skipped_count))
    print("  Errors      : {}".format(error_count))
    print("  Total time  : {:.1f} hours".format(total_time))
    print("  Transcripts : {}".format(OUTPUT_DIR))
    print("=" * 60)


if __name__ == "__main__":
    main()
