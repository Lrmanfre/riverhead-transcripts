#!/usr/bin/env python3
"""
riverhead_relink_votes.py

Re-extract ONLY the meetings that have decision items with no timestamp.

Why this exists
---------------
`SegmentIndex.locate()` originally matched an anchor quote by exact substring
or a fixed 5-word prefix. Whisper mishears names constantly ("Kern?" becomes
"Karen?"), and on contested votes members speak between roll-call responses, so
the quote spans mangled speech. The result was that ~11% of items lost their
"watch vote" link, and they were disproportionately the CONTESTED votes, the
ones a resident most wants to verify against the recording.

`locate()` now falls back through 4-word windows, 3-word windows, and a
longest-common-run match. That only helps on a fresh extraction, because the
anchor quote was discarded after use and cannot be re-matched offline.

This script finds the affected meetings and re-runs just those, instead of
forcing all 80 and paying for work that is already correct.

Usage
-----
  python3 riverhead_relink_votes.py                 # report only, no API calls
  python3 riverhead_relink_votes.py --run           # re-extract affected meetings
  python3 riverhead_relink_votes.py --run --limit 5 # try a few first
  python3 riverhead_relink_votes.py --run --min-unlinked 3
"""

import argparse
import glob
import json
import os
import subprocess
import sys

TRANSCRIPTS_DIR = "transcripts"
EXTRACTOR = "riverhead_extract_votes.py"


def scan():
    """Return {path: {'total':n, 'unlinked':n, 'contested':[...]}} for meetings
    that have at least one decision item without a timestamp."""
    out = {}
    for path in sorted(glob.glob(os.path.join(TRANSCRIPTS_DIR, "**", "*.json"),
                                 recursive=True)):
        try:
            with open(path, "r", encoding="utf-8") as f:
                rec = json.load(f)
        except Exception:
            continue
        items = ((rec.get("decisions") or {}).get("items")) or []
        if not items:
            continue
        unlinked = [it for it in items if it.get("timestamp_s") is None]
        if not unlinked:
            continue
        contested = []
        for it in unlinked:
            dissent = [v for v in (it.get("votes") or []) if v.get("vote") != "yes"]
            if dissent:
                contested.append({
                    "number": it.get("number"),
                    "title": (it.get("title") or "")[:60],
                    "dissent": ["{}:{}".format(v["member"].split()[-1], v["vote"])
                                for v in dissent],
                })
        out[path] = {"total": len(items), "unlinked": len(unlinked),
                     "contested": contested}
    return out


def totals(report):
    return (sum(v["total"] for v in report.values()),
            sum(v["unlinked"] for v in report.values()))


def print_report(report, header):
    _, unlinked = totals(report)
    print("\n{}".format(header))
    print("  meetings affected : {}".format(len(report)))
    print("  unlinked items    : {}".format(unlinked))
    if not report:
        return
    print("\n  {:<26} {:>7} {:>9}  {}".format("meeting", "items", "unlinked", "contested"))
    for path, v in sorted(report.items(), key=lambda kv: -kv[1]["unlinked"]):
        print("  {:<26} {:>7} {:>9}  {}".format(
            os.path.basename(path), v["total"], v["unlinked"],
            len(v["contested"]) or ""))
    contested = [(os.path.basename(p), c)
                 for p, v in report.items() for c in v["contested"]]
    if contested:
        print("\n  Unlinked items that were NOT unanimous (highest value to recover):")
        for name, c in contested:
            print("    {}  {}  {}  [{}]".format(
                name, c["number"] or "-", c["title"], ", ".join(c["dissent"])))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", action="store_true",
                    help="Actually re-extract. Without this, report only.")
    ap.add_argument("--limit", type=int, default=None,
                    help="Re-extract at most N meetings (most unlinked first).")
    ap.add_argument("--min-unlinked", type=int, default=1,
                    help="Only touch meetings with at least N unlinked items.")
    ap.add_argument("--contested-only", action="store_true",
                    help="Only meetings holding an unlinked non-unanimous vote.")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--model", default=None,
                    help="Override the extractor model (default: its own).")
    args = ap.parse_args()

    if not os.path.exists(EXTRACTOR):
        print("ERROR: run this from the repo root; {} not found.".format(EXTRACTOR))
        return 2

    before = scan()
    print_report(before, "=== BEFORE ===")

    targets = [p for p, v in before.items() if v["unlinked"] >= args.min_unlinked]
    if args.contested_only:
        targets = [p for p in targets if before[p]["contested"]]
    targets.sort(key=lambda p: -before[p]["unlinked"])
    if args.limit:
        targets = targets[:args.limit]

    if not targets:
        print("\nNothing matches the filters. Done.")
        return 0

    if not args.run:
        print("\n{} meeting(s) would be re-extracted. Re-run with --run.".format(len(targets)))
        print("Estimated cost at Sonnet 5 rates: ${:.2f} to ${:.2f}".format(
              len(targets) * 0.12, len(targets) * 0.22))
        return 0

    cmd = [sys.executable, EXTRACTOR, "--force", "--workers", str(args.workers)]
    if args.model:
        cmd += ["--model", args.model]
    cmd += targets
    print("\n=== RE-EXTRACTING {} meeting(s) ===".format(len(targets)))
    rc = subprocess.call(cmd)
    if rc != 0:
        print("\nExtractor exited {}. Stopping before the after-report.".format(rc))
        return rc

    after = {p: v for p, v in scan().items() if p in targets or p in before}
    print_report(after, "=== AFTER ===")

    b_items = sum(before[p]["unlinked"] for p in targets)
    a_items = sum(after.get(p, {}).get("unlinked", 0) for p in targets)
    recovered = b_items - a_items
    print("\n=== RECOVERY ===")
    print("  unlinked before : {}".format(b_items))
    print("  unlinked after  : {}".format(a_items))
    print("  recovered       : {} ({:.0f}%)".format(
          recovered, (recovered / b_items * 100) if b_items else 0))
    print("\nStill unlinked means the model's quote is genuinely absent from the")
    print("transcript. Those items now carry 'anchor_quote_unmatched' so you can")
    print("see what it looked for without paying for another extraction.")
    print("\nNext: ./build.sh, then commit and push.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
