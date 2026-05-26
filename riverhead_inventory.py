#!/usr/bin/env python3
"""
riverhead_inventory.py
======================
Fetches all Riverhead Town meeting events from the CivicClerk public API
(newest-first) and writes a CSV manifest for the transcription pipeline.

Stops collecting once it reaches events before 2024-01-01.
Skips future placeholder events beyond today's date.

Output: riverhead_meetings.csv

Usage:
    python3 riverhead_inventory.py
"""

import csv
import json
import subprocess
from datetime import datetime

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API_BASE    = "https://riverheadny.api.civicclerk.com/v1"
PORTAL_BASE = "https://riverheadny.portal.civicclerk.com"
OUTPUT_CSV  = "riverhead_meetings.csv"
START_DATE  = datetime(2024, 1, 1)
END_DATE    = datetime.now()
PAGE_SIZE   = 50   # hint only; server may enforce a smaller page size

CSV_FIELDS = [
    "event_id", "event_date", "category", "title", "agenda_name",
    "status", "has_video", "video_url", "duration_minutes",
    "has_agenda", "agenda_pdf_url", "minutes_pdf_url", "location",
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fetch_json(url):
    """Fetch JSON via macOS system curl to avoid LibreSSL TLS compatibility issues."""
    result = subprocess.run(
        ["/usr/bin/curl", "-s", "--fail", "-H", "Accept: application/json", url],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError("curl failed (exit {}): {}".format(result.returncode, result.stderr.strip()))
    return json.loads(result.stdout)


def parse_date(date_str):
    """Parse ISO 8601 date string. Returns None if missing or bogus (year < 1990)."""
    if not date_str:
        return None
    try:
        dt = datetime.strptime(date_str[:19], "%Y-%m-%dT%H:%M:%S")
        return None if dt.year < 1990 else dt
    except Exception:
        return None


def get_video_url(event):
    CDN_BASE = "https://cpmedia.azureedge.net/riverheadny/"

    def normalize(url):
        url = (url or "").strip()
        if not url:
            return ""
        if url.startswith("http"):
            return url
        if "RIVERHEADNY/" in url:
            filename = url.split("RIVERHEADNY/")[-1]
            return CDN_BASE + filename
        return ""

    url = normalize(event.get("mediaSourcePathMp4"))
    if url:
        return url

    eid = event["id"]
    try:
        data = fetch_json("{}/Events({})/GetEventMediaSummary".format(API_BASE, eid))
        url = normalize(data.get("mediaSourcePathMp4"))
        if url:
            return url
    except Exception:
        pass
    return ""


def get_duration(event):
    """
    Return meeting duration in minutes as a string, or '' if unavailable.
    Rejects bogus timestamps (year < 1990) and implausibly long durations.
    """
    start = parse_date(event.get("liveStartTime", ""))
    end   = parse_date(event.get("liveEndTime", ""))
    if start and end and end > start:
        minutes = int((end - start).total_seconds() / 60)
        if 0 < minutes < 600:   # 1 min – 10 hours sanity check
            return str(minutes)
    return ""


def get_location(event):
    """Format the eventLocation object into a single readable string."""
    loc = event.get("eventLocation") or {}
    parts = [
        loc.get("address1", ""),
        loc.get("address2", ""),
        loc.get("city", ""),
        loc.get("state", ""),
    ]
    return ", ".join(p for p in parts if p and p.strip())


def build_agenda_url(event_id, agenda_id):
    return "{}/event/{}/files/agenda/{}".format(PORTAL_BASE, event_id, agenda_id)


def build_minutes_url(event_id, minutes_id):
    return "{}/event/{}/files/minutes/{}".format(PORTAL_BASE, event_id, minutes_id)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    events = []
    url = "{}/Events?$top={}&$orderby=eventDate%20desc".format(API_BASE, PAGE_SIZE)
    page_num   = 0
    total_skip = 0
    stop       = False

    while url and not stop:
        page_num += 1
        print("Fetching page {}  (skip={})...".format(page_num, total_skip))

        try:
            data = fetch_json(url)
        except Exception as e:
            print("  ERROR fetching page: {}".format(e))
            break

        page = data.get("value", [])
        if not page:
            print("  Empty page — done.")
            break

        for event in page:
            raw_date = event.get("eventDate", "")
            dt = parse_date(raw_date)

            # Skip unparseable dates
            if dt is None:
                continue
            # Skip future placeholder meetings
            if dt > END_DATE:
                continue
            # Stop once we reach events before our collection window
            if dt < START_DATE:
                print("  Reached pre-2024 events — stopping.")
                stop = True
                break

            event_id    = event.get("id", "")
            event_date  = raw_date[:10]
            category    = (event.get("categoryName") or "").strip()
            title       = (event.get("eventName") or "").strip()
            agenda_name = (event.get("agendaName") or "").strip()
            status      = (event.get("isPublished") or "").strip()
            location    = get_location(event)
            duration    = get_duration(event)

            # Video
            has_video = bool(event.get("hasMedia", False))
            video_url = get_video_url(event) if has_video else ""

            # Agenda PDF URL
            has_agenda = bool(event.get("hasAgenda", False))
            agenda_id  = int(event.get("agendaId") or 0)
            agenda_pdf_url = (
                build_agenda_url(event_id, agenda_id)
                if has_agenda and agenda_id > 0 else ""
            )

            # Minutes PDF URL
            minutes_file = event.get("minutesFile") or {}
            minutes_id   = int(minutes_file.get("minutesId") or 0)
            minutes_pdf_url = (
                build_minutes_url(event_id, minutes_id)
                if minutes_id > 0 else ""
            )

            events.append({
                "event_id":        event_id,
                "event_date":      event_date,
                "category":        category,
                "title":           title,
                "agenda_name":     agenda_name,
                "status":          status,
                "has_video":       "True" if has_video else "False",
                "video_url":       video_url,
                "duration_minutes": duration,
                "has_agenda":      "True" if has_agenda else "False",
                "agenda_pdf_url":  agenda_pdf_url,
                "minutes_pdf_url": minutes_pdf_url,
                "location":        location,
            })

        if not stop:
            print("  Collected {} events so far.".format(len(events)))

        # Advance to next page
        total_skip += len(page)
        next_link = data.get("@odata.nextLink")
        if next_link:
            url = next_link
        elif page:
            url = "{}/Events?$top={}&$orderby=eventDate%20desc&$skip={}".format(
                API_BASE, PAGE_SIZE, total_skip)
        else:
            break

    # Write CSV (newest-first, as returned by the API)
    print("\nTotal events collected: {}".format(len(events)))
    print("Writing {}...".format(OUTPUT_CSV))
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(events)

    # Write reversed CSV (oldest-first, used by the transcription pipeline)
    reversed_csv = OUTPUT_CSV.replace(".csv", "_reversed.csv")
    print("Writing {}...".format(reversed_csv))
    with open(reversed_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(reversed(events))

    # Summary stats
    with_video   = sum(1 for e in events if e["has_video"] == "True")
    with_agenda  = sum(1 for e in events if e["agenda_pdf_url"])
    with_minutes = sum(1 for e in events if e["minutes_pdf_url"])
    print("  With video:   {}".format(with_video))
    print("  With agenda:  {}".format(with_agenda))
    print("  With minutes: {}".format(with_minutes))
    print("Done.")


if __name__ == "__main__":
    main()
