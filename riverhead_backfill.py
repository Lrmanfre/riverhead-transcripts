#!/usr/bin/env python3
"""
riverhead_backfill.py
=====================
Updates the metadata in existing transcript JSON files using the
corrected riverhead_meetings.csv.

What it does:
  - Reads riverhead_meetings.csv to build a lookup by event_id
  - Finds all .json transcript files under the transcripts/ folder
  - Updates the "meta" block in each file with correct values for:
      category, title, agenda_name, duration_minutes,
      has_agenda, agenda_pdf_url, minutes_pdf_url, location
  - Does NOT touch the transcript text or segments — only metadata

Safe to run multiple times.

Usage:
    python3 riverhead_backfill.py
"""

import csv
import json
import os
import glob

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

INPUT_CSV      = "riverhead_meetings.csv"
TRANSCRIPTS_DIR = "transcripts"

# Fields to update in the meta block (from CSV column name -> meta key)
META_FIELDS = {
    "category":         "category",
    "title":            "title",
    "agenda_name":      "agenda_name",
    "duration_minutes": "duration_minutes",
    "has_agenda":       "has_agenda",
    "agenda_pdf_url":   "agenda_pdf_url",
    "minutes_pdf_url":  "minutes_pdf_url",
    "location":         "location",
    "status":           "status",
    "video_url":        "video_url",
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_csv_lookup(csv_path):
    """Build a dict of event_id -> row for fast lookup."""
    lookup = {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            eid = str(row.get("event_id", "")).strip()
            if eid:
                lookup[eid] = row
    print("  Loaded {} events from CSV.".format(len(lookup)))
    return lookup


def find_json_files(transcripts_dir):
    """Recursively find all .json transcript files."""
    pattern = os.path.join(transcripts_dir, "**", "*.json")
    files = glob.glob(pattern, recursive=True)
    return sorted(files)


def backfill_file(json_path, lookup):
    """
    Update the meta block in a single JSON file.
    Returns: 'updated', 'skipped' (no match), or 'error'
    """
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print("  ERROR reading {}: {}".format(json_path, e))
        return "error"

    meta = data.get("meta", {})
    event_id = str(meta.get("event_id", "")).strip()

    if not event_id:
        # Try to extract event_id from filename (e.g. 2024-01-01_1223.json)
        basename = os.path.splitext(os.path.basename(json_path))[0]
        parts = basename.split("_")
        if len(parts) >= 2:
            event_id = parts[-1]

    if not event_id or event_id not in lookup:
        return "skipped"

    row = lookup[event_id]
    changed = False

    for csv_col, meta_key in META_FIELDS.items():
        new_val = row.get(csv_col, "")
        if meta.get(meta_key) != new_val:
            meta[meta_key] = new_val
            changed = True

    if not changed:
        return "skipped"

    data["meta"] = meta

    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print("  ERROR writing {}: {}".format(json_path, e))
        return "error"

    return "updated"


def main():
    print("=" * 60)
    print("Riverhead Transcript Metadata Backfill")
    print("CSV    : {}".format(INPUT_CSV))
    print("Dir    : {}".format(TRANSCRIPTS_DIR))
    print("=" * 60)
    print()

    if not os.path.exists(INPUT_CSV):
        print("ERROR: {} not found.".format(INPUT_CSV))
        return

    if not os.path.exists(TRANSCRIPTS_DIR):
        print("ERROR: {} directory not found.".format(TRANSCRIPTS_DIR))
        return

    print("Loading CSV ...")
    lookup = load_csv_lookup(INPUT_CSV)
    print()

    print("Finding transcript files ...")
    json_files = find_json_files(TRANSCRIPTS_DIR)
    print("  Found {} JSON files.".format(len(json_files)))
    print()

    if not json_files:
        print("No transcript files found. Nothing to do.")
        return

    print("Backfilling metadata ...")
    updated = 0
    skipped = 0
    errors  = 0

    for i, json_path in enumerate(json_files, 1):
        result = backfill_file(json_path, lookup)
        short  = os.path.relpath(json_path, TRANSCRIPTS_DIR)

        if result == "updated":
            print("  [{}/{}] UPDATED  {}".format(i, len(json_files), short))
            updated += 1
        elif result == "skipped":
            print("  [{}/{}] skipped  {}".format(i, len(json_files), short))
            skipped += 1
        else:
            print("  [{}/{}] ERROR    {}".format(i, len(json_files), short))
            errors += 1

    print()
    print("=" * 60)
    print("Done!")
    print("  Updated : {}".format(updated))
    print("  Skipped : {} (already correct or no CSV match)".format(skipped))
    print("  Errors  : {}".format(errors))
    print("=" * 60)


if __name__ == "__main__":
    main()
