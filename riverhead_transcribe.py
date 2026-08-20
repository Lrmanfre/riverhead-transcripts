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
import subprocess
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

# --- Bad-asset detection ---------------------------------------------------
# On 2026-08-18 CivicClerk served a 2.9-second, 678 KB stub at the URL the
# inventory had recorded, while the real 174-minute recording sat at a different
# URL. Whisper transcribed the stub as "Thank you." and the pipeline published
# it as the record of a 172-minute Town Board meeting.
#
# Comparing what we downloaded against the meeting length CivicClerk reports is
# what catches this. A file under half the reported length is not the meeting.
DOWNLOAD_ATTEMPTS  = 3
RETRY_BACKOFF      = 20     # seconds, multiplied by attempt number
MIN_MEDIA_SECONDS  = 60     # anything shorter is a stub, not a meeting
MIN_DURATION_RATIO = 0.5    # of CivicClerk's reported duration_minutes

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


def parse_duration_minutes(raw):
    """CivicClerk's reported meeting length in minutes, or None if absent.

    Only a minority of CSV rows carry this, so callers must handle None.
    """
    try:
        v = float(str(raw).strip())
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


class BadAsset(Exception):
    """The file at the source URL is not the meeting recording we expected.

    Distinct from a network error: the download succeeded, the server just
    served something that is not the meeting.
    """


def _download_once(url, dest_path):
    """Single download attempt. Raises IOError on a short read."""
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

    # A dropped connection makes resp.read() return b"" and the loop above exit
    # normally, with no exception raised. Without this comparison a half-received
    # video is indistinguishable from a complete one.
    if total and downloaded != total:
        raise IOError("incomplete download: got {} of {} bytes ({:.1f}%)".format(
                      downloaded, total, downloaded * 100.0 / total))


def download_video(url, dest_path, attempts=DOWNLOAD_ATTEMPTS):
    """Download a video, verifying it arrived whole. Retries transient failures."""
    print("    Downloading: {}".format(url[:80]))
    last = None
    for attempt in range(1, attempts + 1):
        try:
            _download_once(url, dest_path)
            return
        except Exception as e:
            last = e
            if os.path.exists(dest_path):
                os.remove(dest_path)
            if attempt < attempts:
                wait = RETRY_BACKOFF * attempt
                print("    Attempt {}/{} failed ({}). Retrying in {}s ...".format(
                      attempt, attempts, e, wait))
                time.sleep(wait)
    raise IOError("download failed after {} attempts: {}".format(attempts, last))


def probe_media(path):
    """Return (duration_seconds, has_audio) via ffprobe.

    Returns (None, True) when ffprobe is missing or the file is unreadable, so an
    absent tool degrades to the old permissive behaviour instead of blocking the
    whole run.
    """
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "format=duration",
             "-show_entries", "stream=codec_type",
             "-of", "default=noprint_wrappers=1",
             path],
            capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError):
        return (None, True)
    if out.returncode != 0:
        return (None, True)

    duration, has_audio = None, False
    for line in out.stdout.splitlines():
        line = line.strip()
        if line.startswith("duration=") and duration is None:
            try:
                duration = float(line.split("=", 1)[1])
            except ValueError:
                pass
        elif line == "codec_type=audio":
            has_audio = True
    return (duration, has_audio)


def verify_media(path, expected_minutes):
    """Raise BadAsset if the downloaded file is clearly not the full meeting.

    Returns the measured duration in seconds, or None if ffprobe was unavailable.
    """
    duration, has_audio = probe_media(path)

    if not has_audio:
        raise BadAsset("file contains no audio stream")

    if duration is None:
        return None   # ffprobe unavailable: nothing to compare against

    if duration < MIN_MEDIA_SECONDS:
        raise BadAsset("file is only {:.1f}s long".format(duration))

    if expected_minutes:
        expected_s = expected_minutes * 60.0
        if duration < expected_s * MIN_DURATION_RATIO:
            raise BadAsset(
                "file is {:.1f} min but CivicClerk reports a {:.0f} min meeting "
                "({:.0f}% of expected)".format(
                    duration / 60.0, expected_minutes,
                    duration / expected_s * 100))
    return duration


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
        condition_on_previous_text=False,   # prevents hallucination repetition loops
        compression_ratio_threshold=2.0,    # reject segments with abnormal repetition
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
    bad_assets    = []
    thin_results  = []
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

        # Verify we actually got the meeting before spending an hour on Whisper.
        #
        # Nothing is written when this fails. That is deliberate: already_done()
        # only checks that the .txt exists, so writing a transcript here would
        # mark the meeting permanently complete and it would never be retried.
        # Leaving no file means the next run picks it up again, and the --days
        # window naturally stops the retries once the meeting ages out.
        try:
            expected = parse_duration_minutes(row.get("duration_minutes"))
            measured = verify_media(video_path, expected)
            if measured:
                print("    Media verified: {:.1f} min{}".format(
                      measured / 60.0,
                      " (CivicClerk reports {:.0f})".format(expected) if expected else ""))
        except BadAsset as e:
            print("    SKIPPING: source is not the meeting recording — {}".format(e))
            print("    No transcript written, so this meeting will be retried.")
            bad_assets.append((event_date, category, event_id, str(e)))
            os.remove(video_path)
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
        # The media verified as full length, so this transcript is saved even if
        # it is sparse: a long recording that is mostly silence is a real record,
        # not a failure, and re-downloading 3 GB nightly would not improve it.
        # Flag it so a human can look, and let build_site caveat it for readers.
        words = len((result.get("text") or "").split())
        if measured and words < (measured / 60.0) * 5:
            print("    NOTE: only {} words from {:.0f} min of audio. Saving anyway "
                  "(recording is full length), but flagging for review.".format(
                      words, measured / 60.0))
            thin_results.append((event_date, category, event_id, words,
                                 measured / 60.0))

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
    print("  Bad assets  : {} (will retry next run)".format(len(bad_assets)))

    if bad_assets:
        print()
        print("!" * 60)
        print("SOURCE DID NOT SERVE THE MEETING RECORDING for {} meeting(s):".format(
              len(bad_assets)))
        for date, cat, eid, why in bad_assets:
            print("  {}  {}  (event {})".format(date, cat, eid))
            print("      {}".format(why))
        print()
        print("No transcripts were written, so these retry automatically on the")
        print("next run. If one persists past the --days window it will stop being")
        print("retried and the meeting will be missing from the site entirely.")
        print("!" * 60)

    if thin_results:
        print()
        print("-" * 60)
        print("FULL-LENGTH but very little speech ({} meeting(s)):".format(
              len(thin_results)))
        for date, cat, eid, words, mins in thin_results:
            print("  {}  {}  (event {}): {} words from {:.0f} min".format(
                  date, cat, eid, words, mins))
        print("These were saved. Often legitimate (executive session, silence).")
        print("-" * 60)
    print("  Total time  : {:.1f} hours".format(total_time))
    print("  Transcripts : {}".format(OUTPUT_DIR))
    print("=" * 60)


if __name__ == "__main__":
    main()
