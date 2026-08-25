#!/usr/bin/env python3
"""
riverhead_extract_votes.py
==========================
Extracts every formal action (resolutions, motions, hearing actions) with
outcomes and roll-call votes from Town Board meeting transcripts, and stores
the result inside the transcript JSON under the "decisions" key. The static
site builder (riverhead_build_site.py) renders whatever it finds there.

Design notes
------------
- Scope: Town Board regular meetings only (category == "Town Board"). Work
  sessions and other boards are v2; their vote language differs.
- Grounding: the CivicClerk API's publishedFiles carry the official Minutes
  and Agenda for each event (a DIFFERENT id space than the portal URL's
  agendaId). When available and passing a date check, the official document's
  numbered resolution list grounds the extraction, so numbers and titles come
  from the town's own record while the transcript supplies outcomes and the
  per-member roll call (the published minutes record no votes at all).
- Idempotent: a transcript that already has a current decisions block is
  skipped unless --force is passed, so the nightly pipeline only processes
  new meetings and a re-run resumes a partial backfill.
- Honesty over coverage: the model is instructed to never invent votes. An
  action without an audible roll call is stored with an empty vote list and
  confidence "low"; the site says so instead of guessing.

Usage
-----
    python3 riverhead_extract_votes.py                 # backfill: all missing
    python3 riverhead_extract_votes.py --days 14       # only recent meetings
    python3 riverhead_extract_votes.py --limit 3       # pilot on 3 meetings
    python3 riverhead_extract_votes.py --force         # regenerate everything
    python3 riverhead_extract_votes.py --dry-run       # list work, no API calls

Requires: pip install anthropic  (key in riverhead.env, same as summaries)
"""

import argparse
import glob
import json
import os
import re
import sys
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, date

import riverhead_summarize as rs

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TRANSCRIPTS_DIR = rs.TRANSCRIPTS_DIR
CATEGORY        = "Town Board"
PROMPT_VERSION  = "1"                      # bump to invalidate old extractions
MIN_WORDS       = 300                      # below this, nothing to extract

MAX_TOKENS_OUT  = 16000                    # raised for the Sonnet 5 tokenizer (~30% more)

# USD per 1M tokens (input, output). Longest matching prefix wins.
MODEL_PRICING = {
    "claude-opus-5":    (15.00, 75.00),
    "claude-fable-5":   (15.00, 75.00),
    "claude-sonnet-5":  ( 3.00, 15.00),
    "claude-sonnet-4":  ( 3.00, 15.00),
    "claude-haiku-4-5": ( 1.00,  5.00),
    "claude-haiku":     ( 0.80,  4.00),
}
FALLBACK_PRICING = (3.00, 15.00)

def model_pricing(model):
    """(in_per_mtok, out_per_mtok, matched_name) for a model id."""
    best = None
    for name, rates in MODEL_PRICING.items():
        if model.startswith(name) and (best is None or len(name) > len(best)):
            best = name
    if best:
        return MODEL_PRICING[best][0], MODEL_PRICING[best][1], best
    return FALLBACK_PRICING[0], FALLBACK_PRICING[1], None
CHUNK_TOKENS    = rs.PER_REQUEST_TOKEN_MAX # transcripts above this split
CHUNK_OVERLAP   = 2000                     # chars; a vote never straddles unseen

OUTCOMES = ("adopted", "defeated", "tabled", "amended", "withdrawn",
            "held", "unknown")
VOTE_VALUES = ("yes", "no", "abstain", "absent", "recused")

# Board rosters by era. Used to normalize garbled roll-call names, never to
# invent votes. Supervisor changed January 2026 (Halpin succeeded Hubbard);
# the four council members are unchanged across the site's 2024+ window.
ROSTERS = [
    {"until": "2025-12-31", "supervisor": "Tim Hubbard",
     "council": ["Joann Waski", "Denise Merrifield", "Bob Kern", "Ken Rothwell"]},
    {"until": "9999-12-31", "supervisor": "Jerry Halpin",
     "council": ["Joann Waski", "Denise Merrifield", "Bob Kern", "Ken Rothwell"]},
]

GARBLE_HINTS = (
    "'Waske', 'Waskey', 'Wasky' -> Waski; 'Merryfield' -> Merrifield; "
    "'Curn', 'Kurn' -> Kern; 'Rathwell' -> Rothwell; 'Halpen', 'Hallpin' -> Halpin; "
    "'Hubbert' -> Hubbard"
)

# Same map, applied in the validator as a backstop in case the model passes a
# garbled name through unmapped.
GARBLE_MAP = {
    "waske": "waski", "waskey": "waski", "wasky": "waski",
    "merryfield": "merrifield", "curn": "kern", "kurn": "kern",
    "rathwell": "rothwell", "halpen": "halpin", "hallpin": "halpin",
    "hubbert": "hubbard",
}

def roster_for(event_date):
    for r in ROSTERS:
        if (event_date or "9999") <= r["until"]:
            return r
    return ROSTERS[-1]

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

SYSTEM = (
    "You extract formal Town Board actions from a Riverhead, NY town meeting "
    "transcript produced by speech-to-text. You output structured data for a "
    "public record, so accuracy beats coverage.\n\n"
    "Extract ONLY items the board formally acted on in THIS text: resolutions "
    "moved and voted, motions (including amendments and motions to table or "
    "adjourn hearings), and public hearings formally opened, held, or closed. "
    "Skip discussion that led to no action, announcements, proclamations read "
    "without a vote, and public comment. The Community Development Agency (CDA) "
    "sometimes convenes during the meeting: record CDA actions as kind "
    "\"motion\", never \"resolution\" (Town Board resolution numbers belong to "
    "the Town Board alone).\n\n"
    "Rules:\n"
    "- NEVER invent or infer a vote. Record a per-member vote only when the "
    "roll call is audible in the transcript. If an item passes by voice vote "
    "or the roll call is garbled/missing, return an empty votes list and "
    "confidence \"low\".\n"
    "- Roll-call names are frequently garbled by speech-to-text. Map variants "
    "to the roster provided (examples: {garbles}). Never attribute a vote to "
    "anyone not on the roster unless the transcript is unambiguous.\n"
    "- Resolution numbers: prefer the OFFICIAL DOCUMENT list when provided. If "
    "only the transcript is available and it says e.g. 'resolution 765', "
    "format the number as '<year>-765' using the meeting year. If no number "
    "is stated anywhere, use null.\n"
    "- Titles: prefer the official document's wording; otherwise a concise "
    "title from the transcript. Do not guess spellings of names/addresses; "
    "describe generically if unsure.\n"
    "- outcome must be one of: adopted, defeated, tabled, amended, withdrawn, "
    "held (public hearing held/closed), unknown.\n"
    "- anchor_quote: 8 to 12 words copied VERBATIM from the transcript at the "
    "moment of the vote or action (used to locate the video timestamp). Copy "
    "exactly, including any transcription errors.\n"
    "- Be terse: titles under 15 words, no commentary fields. Meetings can "
    "have 50+ items; compactness keeps the full list intact.\n"
    "- Output MUST be a single valid JSON object and nothing else. votes is an "
    "OBJECT mapping each roster LAST NAME to their vote:\n"
    "{{\"items\": [{{\"kind\": \"resolution|motion|hearing_action\", "
    "\"number\": \"2026-765 or null\", \"title\": \"...\", \"outcome\": \"adopted\", "
    "\"votes\": {{\"Waski\": \"yes\", \"Merrifield\": \"yes\", \"Kern\": \"yes\", "
    "\"Rothwell\": \"yes\", \"Halpin\": \"yes\"}}, "
    "\"confidence\": \"high|low\", \"anchor_quote\": \"...\"}}]}}\n"
    "If this text contains no formal actions, output {{\"items\": []}}."
).format(garbles=GARBLE_HINTS)

def build_user_prompt(meta, official_text, official_kind, part_label, text):
    r = roster_for(meta.get("event_date", ""))
    return (
        "MEETING\n"
        "Board: Town Board\n"
        "Date: {date}\n\n"
        "BOARD ROSTER ON THIS DATE (normalize roll-call names to these)\n"
        "Supervisor: {sup}\n"
        "Council members: {council}\n\n"
        "OFFICIAL DOCUMENT ({kind}; authoritative for resolution numbers and "
        "titles; records no votes; may be empty)\n"
        "{official}\n\n"
        "TRANSCRIPT {part}(auto-generated; may contain errors)\n"
        "{text}\n"
    ).format(
        date=meta.get("event_date", ""),
        sup=r["supervisor"],
        council=", ".join(r["council"]),
        kind=official_kind or "none",
        official=official_text or "(none available)",
        part=part_label,
        text=text,
    )

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _norm_number(number, event_date):
    if number is None:
        return None
    s = str(number).strip().lstrip("#").replace("Resolution", "").replace("No.", "").strip()
    if not s or s.lower() in ("null", "none"):
        return None
    m = re.fullmatch(r"(\d{1,4})", s)
    if m and event_date[:4].isdigit():
        return "{}-{}".format(event_date[:4], m.group(1))
    m = re.fullmatch(r"(\d{4})\s*[-–]\s*(\d{1,4})", s)
    if m:
        return "{}-{}".format(m.group(1), m.group(2))
    return s[:20]

def validate_items(obj, meta):
    """Coerce model output into the stored item shape; drop unusable items."""
    if not isinstance(obj, dict):
        raise ValueError("not a JSON object")
    roster = roster_for(meta.get("event_date", ""))
    full_by_last = {n.split()[-1].lower(): n
                    for n in roster["council"] + [roster["supervisor"]]}
    items = []
    for it in (obj.get("items") or []):
        if not isinstance(it, dict):
            continue
        kind = str(it.get("kind", "")).strip().lower()
        if kind not in ("resolution", "motion", "hearing_action"):
            kind = "motion"
        title = str(it.get("title") or "").strip()
        outcome = str(it.get("outcome", "")).strip().lower()
        if outcome not in OUTCOMES:
            outcome = "unknown"
        if not title and it.get("number") is None:
            continue
        raw_votes = it.get("votes") or []
        if isinstance(raw_votes, dict):
            raw_votes = [{"member": m, "vote": v} for m, v in raw_votes.items()]
        votes = []
        for v in raw_votes:
            if not isinstance(v, dict):
                continue
            member = str(v.get("member") or "").strip()
            val = str(v.get("vote", "")).strip().lower()
            if val in ("aye", "yea"):
                val = "yes"
            if val in ("nay",):
                val = "no"
            if not member or val not in VOTE_VALUES:
                continue
            last = member.split()[-1].lower()
            last = GARBLE_MAP.get(last, last)
            member = full_by_last.get(last, member)
            votes.append({"member": member, "vote": val})
        # dedupe members, keep first occurrence
        seen, uniq = set(), []
        for v in votes:
            if v["member"] not in seen:
                seen.add(v["member"])
                uniq.append(v)
        confidence = str(it.get("confidence", "")).strip().lower()
        if confidence not in ("high", "low"):
            confidence = "high" if uniq else "low"
        if not uniq:
            confidence = "low"
        items.append({
            "kind": kind,
            "number": _norm_number(it.get("number"), meta.get("event_date", "")),
            "title": title[:300],
            "outcome": outcome,
            "votes": uniq,
            "unanimous": bool(uniq) and all(v["vote"] == "yes" for v in uniq),
            "confidence": confidence,
            "anchor_quote": str(it.get("anchor_quote") or "").strip()[:200],
        })
    return items

# ---------------------------------------------------------------------------
# Timestamp resolution (anchor_quote -> segment start time)
# ---------------------------------------------------------------------------

def _norm_text(s):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", "", (s or "").lower())).strip()

class SegmentIndex:
    def __init__(self, segments):
        self.starts, self.times, parts, pos = [], [], [], 0
        for seg in segments:
            t = _norm_text(rs.sanitize_segment_text((seg.get("text") or "").strip()))
            if not t or t == _norm_text(rs.GAP_MARKER):
                continue
            self.starts.append(pos)
            self.times.append(seg.get("start", 0))
            parts.append(t)
            pos += len(t) + 1
        self.text = " ".join(parts)

    def locate(self, quote):
        """Best-effort character offset -> timestamp.

        Whisper mishears names constantly ("Kern?" -> "Karen?"), so an exact
        match and a fixed 5-word prefix both fail on exactly the roll calls we
        most want to link: the contested ones, where members speak between
        votes. Fall back to any 4-word window, then to a longest-common-run
        match, before giving up.
        """
        q = _norm_text(quote)
        if not q:
            return None
        idx = self.text.find(q)
        words = q.split()
        if idx < 0 and len(words) >= 5:
            idx = self.text.find(" ".join(words[:5]))
        if idx < 0:
            # Any distinctive window, widest first; the first hit wins. Three
            # words is the floor: shorter is common enough to land anywhere.
            for width in (4, 3):
                if len(words) < width:
                    continue
                for w in range(len(words) - width + 1):
                    idx = self.text.find(" ".join(words[w:w + width]))
                    if idx >= 0:
                        break
                if idx >= 0:
                    break
        if idx < 0 and len(q) >= 20:
            # Last resort: longest shared run of characters, long enough that a
            # stopword phrase cannot satisfy it.
            from difflib import SequenceMatcher
            m = SequenceMatcher(None, self.text, q, autojunk=False)
            blk = m.find_longest_match(0, len(self.text), 0, len(q))
            if blk.size >= 20:
                idx = blk.a
        if idx < 0:
            return None
        i = bisect_right(self.starts, idx) - 1
        try:
            return int(float(self.times[max(i, 0)]))
        except Exception:
            return None

# ---------------------------------------------------------------------------
# Truncation salvage: recover every complete item from a cut-off response
# ---------------------------------------------------------------------------

def salvage_items_json(raw):
    """Parse as many complete objects as possible out of a truncated
    {"items": [...]} response. A response that hits the output-token cap dies
    mid-item; everything before the cut is still good, and the chunk overlap
    plus the official list mean a lost tail usually reappears elsewhere."""
    dec = json.JSONDecoder()
    i = raw.find('"items"')
    j = raw.find("[", i) if i >= 0 else -1
    if j < 0:
        raise ValueError("no items array found")
    items, k = [], j + 1
    while k < len(raw):
        while k < len(raw) and raw[k] in " \t\r\n,":
            k += 1
        if k >= len(raw) or raw[k] == "]":
            break
        try:
            obj, end = dec.raw_decode(raw, k)
        except json.JSONDecodeError:
            break
        items.append(obj)
        k = end
    if not items:
        raise ValueError("no complete items recovered")
    return {"items": items}

# ---------------------------------------------------------------------------
# Merge (chunked extractions overlap; dedupe by number, then title)
# ---------------------------------------------------------------------------

def _title_tokens(title):
    return set(_norm_text(title).split())

def merge_items(lists):
    """Dedupe across overlapping chunks. Numbered items key on the resolution
    number. Unnumbered items (hearings, motions) fuzzy-match on title token
    overlap, because the model phrases the same action differently in
    different chunks ('Monroe Balancing Test Public Hearing opened and
    closed' vs 'Public Hearing - Monroe Balancing Test opened and closed').
    On a duplicate, the copy with the fuller roll call wins."""
    merged, pos_by_num = [], {}
    for items in lists:
        for it in items:
            if it["number"]:
                i = pos_by_num.get(it["number"])
                if i is None:
                    pos_by_num[it["number"]] = len(merged)
                    merged.append(it)
                elif len(it["votes"]) > len(merged[i]["votes"]):
                    merged[i] = it
                continue
            toks = _title_tokens(it["title"])
            match = None
            for j, prev in enumerate(merged):
                if prev["number"] or prev["kind"] != it["kind"]:
                    continue
                ptoks = _title_tokens(prev["title"])
                union = len(toks | ptoks) or 1
                if len(toks & ptoks) / union >= 0.5:
                    match = j
                    break
            if match is None:
                merged.append(it)
            elif len(it["votes"]) > len(merged[match]["votes"]):
                merged[match] = it
    return merged

# ---------------------------------------------------------------------------
# Per-meeting work
# ---------------------------------------------------------------------------

def has_current_decisions(record):
    d = record.get("decisions")
    return (isinstance(d, dict)
            and d.get("status") in ("ok", "too_short")
            and d.get("prompt_version") == PROMPT_VERSION)

def extract_file(path, client, model, dry_run, limiter=None, usage=None):
    with open(path, "r", encoding="utf-8") as f:
        record = json.load(f)
    meta = record.get("meta", {})
    text = rs.readable_text(record)
    words = len(text.split())

    if words < MIN_WORDS:
        record["decisions"] = {
            "status": "too_short", "prompt_version": PROMPT_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        if not dry_run:
            rs._atomic_write(path, record)
        return "too_short"
    if dry_run:
        return "extracted"

    # Minutes preferred over Agenda: published after the meeting, they are the
    # authoritative numbered resolution list (they record no votes; the roll
    # call always comes from the transcript).
    official_text, official_kind = rs.fetch_official_text(
        meta, prefer=("Minutes", "Agenda"))

    # Budget each request: chunk + official doc + system prompt must fit under
    # the per-request cap, so subtract the fixed parts from the chunk span.
    fixed = rs._estimate_tokens(official_text) + rs._estimate_tokens(SYSTEM) + 500
    span = max(20_000, (CHUNK_TOKENS - fixed) * 4)
    if rs._estimate_tokens(text) + fixed > CHUNK_TOKENS:
        chunks, i = [], 0
        while i < len(text):
            chunks.append(text[i:i + span])
            i += span - CHUNK_OVERLAP
    else:
        chunks = [text]

    all_items = []
    truncated = False
    for idx, chunk in enumerate(chunks, 1):
        part = "PART {}/{} ".format(idx, len(chunks)) if len(chunks) > 1 else ""
        prompt = build_user_prompt(meta, official_text, official_kind, part, chunk)
        if usage is not None:
            usage["in"] += rs._estimate_tokens(SYSTEM) + rs._estimate_tokens(prompt)
        items = None
        last = None
        for _ in range(2):
            raw = rs._create(client, model, SYSTEM, prompt,
                             limiter, MAX_TOKENS_OUT)
            if usage is not None:
                usage["out"] += rs._estimate_tokens(raw)
            try:
                items = validate_items(rs.extract_json(raw), meta)
                break
            except (json.JSONDecodeError, ValueError) as e:
                last = e
            # Truncated output (response hit the token cap): keep every
            # complete item rather than discarding the whole chunk.
            try:
                items = validate_items(salvage_items_json(raw), meta)
                truncated = True
                rs.log("    WARNING {}: response hit the {}-token output cap; "
                       "salvaged {} item(s), record marked partial and WILL be "
                       "re-extracted on the next run".format(
                       os.path.basename(path), MAX_TOKENS_OUT, len(items)))
                break
            except (json.JSONDecodeError, ValueError):
                pass
            prompt += "\n\nReturn ONLY the JSON object. No prose, no code fences."
        if items is None:
            raise RuntimeError("model did not return valid JSON: {}".format(last))
        all_items.append(items)

    items = merge_items(all_items)

    seg_index = SegmentIndex(record.get("segments", []))
    unlocated = 0
    for it in items:
        quote = it.pop("anchor_quote", "") or ""
        it["timestamp_s"] = seg_index.locate(quote)
        if it["timestamp_s"] is None:
            # Retain the quote ONLY on a miss, so the failure can be diagnosed
            # and re-located later without paying for another extraction.
            it["anchor_quote_unmatched"] = quote
            unlocated += 1
    if unlocated:
        rs.log("    NOTE {}: {} of {} item(s) could not be timestamped".format(
               os.path.basename(path), unlocated, len(items)))

    record["decisions"] = {
        "status": "partial" if truncated else "ok",
        "truncated": truncated,
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "grounded_in": official_kind or None,
        "items": items,
    }
    rs._atomic_write(path, record)
    return "partial" if truncated else "extracted"

# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def select_files(args):
    if args.files:
        paths = list(args.files)
    else:
        paths = sorted(glob.glob(os.path.join(TRANSCRIPTS_DIR, "**", "*.json"),
                                 recursive=True), reverse=True)
    selected = []
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8") as f:
                rec = json.load(f)
        except Exception as e:
            rs.log("  WARNING: cannot read {}: {}".format(p, e))
            continue
        meta = rec.get("meta", {})
        if (meta.get("category") or "").strip() != args.category:
            continue
        if args.days is not None:
            d = meta.get("event_date", "")
            try:
                age = (date.today() - datetime.strptime(d, "%Y-%m-%d").date()).days
                if age > args.days:
                    continue
            except Exception:
                continue
        if not args.force and has_current_decisions(rec):
            continue
        selected.append(p)
    if args.limit:
        selected = selected[:args.limit]
    return selected

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Extract decisions and roll-call votes from Town Board transcripts.")
    ap.add_argument("files", nargs="*", help="Specific transcript JSON files.")
    ap.add_argument("--category", default=CATEGORY,
                    help="Meeting category to process (default: %s)." % CATEGORY)
    ap.add_argument("--force", action="store_true", help="Regenerate existing extractions.")
    ap.add_argument("--days", type=int, default=None, help="Only meetings within N days.")
    ap.add_argument("--limit", type=int, default=None, help="Process at most N transcripts.")
    ap.add_argument("--workers", type=int, default=2, help="Concurrent API calls (default 2).")
    ap.add_argument("--tpm", type=int, default=rs.DEFAULT_TPM,
                    help="Input tokens/min ceiling (default %d)." % rs.DEFAULT_TPM)
    ap.add_argument("--model", default=rs.DEFAULT_MODEL,
                    help="Claude model (default %s)." % rs.DEFAULT_MODEL)
    ap.add_argument("--dry-run", action="store_true",
                    help="List what would be processed; no API calls or writes.")
    args = ap.parse_args()

    selected = select_files(args)
    rs.log("Riverhead vote extractor")
    rs.log("  model:      {}".format(args.model))
    rs.log("  category:   {}".format(args.category))
    rs.log("  to process: {} transcript(s){}".format(
           len(selected), "  [DRY RUN]" if args.dry_run else ""))
    if args.dry_run:
        for p in selected:
            rs.log("    {}".format(p))
    if not selected:
        rs.log("Nothing to do.")
        return 0

    client = None
    if not args.dry_run:
        try:
            import anthropic
            from anthropic import Anthropic
        except ImportError:
            rs.log("ERROR: the 'anthropic' package is not installed. Run: pip3 install anthropic")
            return 2
        api_key, key_source = rs.resolve_api_key()
        if not api_key:
            rs.log("ERROR: no API key found. Add it to riverhead.env as ANTHROPIC_API_KEY=sk-ant-...")
            return 2
        rs.log("  key source: {}".format(key_source))
        client = Anthropic(api_key=api_key)
        try:
            client.messages.create(model=args.model, max_tokens=1,
                                   messages=[{"role": "user", "content": "ping"}])
        except anthropic.AuthenticationError:
            rs.log("ERROR: the API key was rejected (401). Check riverhead.env.")
            return 2
        except Exception as e:
            msg = str(e)
            low = msg.lower()
            if "credit balance" in low or "plans & billing" in low:
                rs.log("ERROR: the API refused this model for billing reasons. "
                       "Aborting before processing {} file(s).".format(len(selected)))
                rs.log("  model:  {}".format(args.model))
                rs.log("  detail: {}".format(msg))
                rs.log("  Check console.claude.com credits, then confirm this model "
                       "is available to your org. A model the org cannot use returns "
                       "this same credit-balance message.")
                return 3
            pass

    limiter = rs._RateLimiter(args.tpm) if not args.dry_run else None
    usage = {"in": 0, "out": 0}
    counts = {"extracted": 0, "partial": 0, "too_short": 0, "error": 0}
    done, total = 0, len(selected)

    def work(path):
        return path, extract_file(path, client, args.model, args.dry_run, limiter, usage)

    workers = 1 if args.dry_run else max(1, args.workers)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(work, p): p for p in selected}
        for fut in as_completed(futures):
            path = futures[fut]
            try:
                _, status = fut.result()
                counts[status] = counts.get(status, 0) + 1
            except Exception as e:
                counts["error"] += 1
                rs.log("  ERROR {}: {}".format(os.path.basename(path), e))
                status = "error"
            done += 1
            if status != "error":
                rs.log("  [{}/{}] {}  {}".format(done, total, status, os.path.basename(path)))

    rs.log("")
    rs.log("Done. extracted={extracted}  partial={partial}  too_short={too_short}  "
           "errors={error}".format(**counts))
    if counts["partial"]:
        rs.log("WARNING: {} file(s) hit the output cap and were saved as partial. "
               "Re-run to complete them.".format(counts["partial"]))
    if not args.dry_run:
        pin, pout, matched = model_pricing(args.model)
        note = "" if matched else "  [unknown model, Sonnet rates assumed]"
        rs.log("Approx tokens: {:,} in / {:,} out  (est. cost at {} rates: ${:.2f}){}".format(
               usage["in"], usage["out"], args.model,
               usage["in"] / 1e6 * pin + usage["out"] / 1e6 * pout, note))
    return 0

if __name__ == "__main__":
    sys.exit(main())
