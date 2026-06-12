#!/usr/bin/env python3
"""
riverhead_summarize.py
======================
Generates a concise, resident-facing AI summary for each meeting transcript and
stores it inside the transcript JSON under the "summary" key. The static site
builder (riverhead_build_site.py) renders whatever summary it finds, so this
script is the only thing that talks to the Claude API.

Design notes
------------
- Audience: residents who want the gist fast. Length scales with meeting size
  and is hard-capped so the summary never competes with the full transcript.
- Grounding: when an agenda PDF exists, its text is fed to the model so it can
  fix the proper nouns, addresses, and resolution numbers that speech-to-text
  routinely garbles. The transcript says what happened; the agenda says how to
  spell it.
- Idempotent: a transcript that already has a usable summary is skipped unless
  --force is passed, so the nightly pipeline only summarizes new meetings and a
  re-run resumes a partial backfill.
- Output is structured JSON ({tldr, sections:[{heading, items[]}]}) so the page
  rendering stays consistent and can be restyled without regenerating anything.

Usage
-----
    export ANTHROPIC_API_KEY=sk-ant-...        # or put it in riverhead.env
    python3 riverhead_summarize.py             # backfill: summarize all missing
    python3 riverhead_summarize.py --days 14   # only recent meetings
    python3 riverhead_summarize.py --force     # regenerate everything
    python3 riverhead_summarize.py --limit 3   # process at most 3 (testing)
    python3 riverhead_summarize.py --dry-run   # show what would run, no API calls

Requires: pip install anthropic
"""

import argparse
import glob
import json
import os
import re
import shutil
import sys
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, date

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TRANSCRIPTS_DIR = "transcripts"
AGENDA_CACHE    = "agenda_cache"
DEFAULT_MODEL   = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
PROMPT_VERSION  = "1"           # bump to invalidate old summaries with --force
GAP_MARKER      = "[transcription gap]"

MIN_WORDS         = 300         # below this, transcript is too thin to summarize
MAX_TRANSCRIPT_CH = 400_000     # safety cap (~100k tokens); longest meeting fits
MAX_AGENDA_CH     = 15_000
MAX_AGENDA_PAGES  = 15
MAX_TOKENS        = 1500
RETRY_SLEEPS      = [5, 15, 40] # backoff between attempts on transient errors

# Agenda grounding is OFF until the agenda's API file-stream id is resolved. The
# portal URL's agenda_id is a DIFFERENT id space than GetMeetingFileStream's fileId
# (fileId=<agenda_id> returns the wrong, often years-off document), so grounding on
# it would feed wrong agendas into summaries. Transcript-only output tested accurate.
# See AI_SUMMARIES.md. Flip to True only once the lookup + a date check are wired.
GROUND_IN_AGENDA  = False

# Rate limiting. Tier 1 Sonnet allows 30k input tokens/min, so the script paces
# itself under a ceiling and splits any meeting whose transcript alone would exceed
# a single request's budget into chunks (map-reduce) instead of failing with a 429.
DEFAULT_TPM           = 26000   # input tokens/min the script paces itself to
PER_REQUEST_TOKEN_MAX = 20000   # transcripts larger than this are summarized in chunks

_print_lock = threading.Lock()

def log(msg):
    with _print_lock:
        print(msg, flush=True)

def resolve_api_key(env_path="riverhead.env"):
    """Return (key, source). The dedicated key file wins over the environment so a
    stale ANTHROPIC_API_KEY left in the shell cannot shadow the real one."""
    if os.path.exists(env_path):
        try:
            with open(env_path) as f:
                for line in f:
                    s = line.strip()
                    if s.startswith("ANTHROPIC_API_KEY="):
                        v = s.split("=", 1)[1].strip().strip('"').strip("'")
                        if v:
                            return v, env_path
        except Exception:
            pass
    v = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if v:
        return v, "environment"
    return "", None

# ---------------------------------------------------------------------------
# Transcript cleaning (mirrors the sanitizer in riverhead_build_site.py)
# ---------------------------------------------------------------------------

def sanitize_segment_text(text, min_repeat=8, max_unit=25):
    """Truncate Whisper hallucination repetition loops (e.g. 'headheadhead...')."""
    if not text or text == GAP_MARKER:
        return text
    pattern = re.compile(r'(.{3,' + str(max_unit) + r'})\1{' + str(min_repeat - 1) + r',}')
    m = pattern.search(text)
    if m:
        truncated = text[:m.start()].strip().rstrip(',').strip()
        return truncated if len(truncated) >= 8 else GAP_MARKER
    return text

def readable_text(record):
    """Collapse segments into clean prose, dropping gaps and consecutive dups."""
    out, prev = [], None
    for seg in record.get("segments", []):
        t = sanitize_segment_text((seg.get("text") or "").strip())
        if not t or t in (".", "..", "...") or t == GAP_MARKER:
            continue
        low = t.lower()
        if low == prev:
            continue
        prev = low
        out.append(t)
    text = " ".join(out)
    return text[:MAX_TRANSCRIPT_CH]

# ---------------------------------------------------------------------------
# Length tiers (word count is the reliable size signal; duration is sparse)
# ---------------------------------------------------------------------------

def length_tier(words):
    if words < 3000:
        return 120, "Short meeting. TL;DR plus up to 3 bullets total."
    if words < 15000:
        return 280, "Typical meeting. TL;DR plus 4 to 8 bullets total."
    if words < 30000:
        return 440, "Long meeting. TL;DR plus 8 to 12 bullets total."
    return 500, "Very long meeting. TL;DR plus up to 12 bullets; group minor items."

# ---------------------------------------------------------------------------
# Agenda grounding
# ---------------------------------------------------------------------------

def agenda_api_url(agenda_pdf_url):
    """Turn the stored portal URL into the API text-stream endpoint, or '' if it
    does not match the expected shape.

    Portal (SPA shell, serves HTML):
      https://riverheadny.portal.civicclerk.com/event/<eid>/files/agenda/<fileId>
    API (returns the agenda as plain text):
      https://riverheadny.api.civicclerk.com/v1/Meetings/GetMeetingFileStream(fileId=<fileId>,plainText=true)
    """
    m = re.search(r"/files/agenda/(\d+)", agenda_pdf_url or "")
    if not m:
        return ""
    file_id = m.group(1)
    host = agenda_pdf_url.split("/event/")[0].replace(".portal.civicclerk.com",
                                                       ".api.civicclerk.com")
    return "{}/v1/Meetings/GetMeetingFileStream(fileId={},plainText=true)".format(host, file_id)

def fetch_agenda_text(meta, cache_dir):
    """Fetch the agenda as plain text from the CivicClerk API, or '' on failure.

    The stored agenda_pdf_url is the portal's single-page-app route (serves HTML),
    so we derive the API GetMeetingFileStream endpoint and ask for plainText=true.
    Uses macOS system curl, matching the TLS approach in riverhead_inventory.py.
    Extracted text is cached under agenda_cache/<event_id>.txt.
    """
    if not GROUND_IN_AGENDA:
        return ""
    api_url = agenda_api_url(meta.get("agenda_pdf_url"))
    if not api_url:
        return ""
    eid = re.sub(r"[^\w.-]", "_", str(meta.get("event_id", "x")))
    cache_path = os.path.join(cache_dir, eid + ".txt")
    if os.path.exists(cache_path):
        try:
            cached = open(cache_path, encoding="utf-8").read()
            if cached.strip():
                return cached[:MAX_AGENDA_CH]
        except Exception:
            pass
    try:
        os.makedirs(cache_dir, exist_ok=True)
        r = subprocess.run(
            ["/usr/bin/curl", "-sL", "--fail", "-H", "Accept: text/plain", api_url],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0 or not (r.stdout or "").strip():
            log("    agenda fetch empty/failed ({}): curl exit {}".format(eid, r.returncode))
            return ""
        text = r.stdout
        # Guard against an HTML error page sneaking through.
        head = text.lstrip()[:200].lower()
        if head.startswith("<!doctype") or "<html" in head:
            return ""
        with open(cache_path, "w", encoding="utf-8") as f:
            f.write(text)
        return text[:MAX_AGENDA_CH]
    except Exception as e:
        log("    agenda fetch error ({}): {}".format(eid, e))
        return ""

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

SYSTEM = (
    "You write concise, accurate, plain-English summaries of Riverhead, NY town "
    "government meetings for residents who want the gist quickly.\n\n"
    "Rules:\n"
    "- Be factual and neutral. No opinions, praise, or editorializing.\n"
    "- Use ONLY information supported by the transcript and agenda provided. Do not "
    "invent or infer names, numbers, vote counts, dollar amounts, or outcomes.\n"
    "- The transcript is auto-generated by speech-to-text and frequently garbles proper "
    "nouns, names, addresses, and technical or legal terms (for example 'SEQRA' may appear "
    "as 'secret', a tax map number as 'Mac number'). When an agenda is provided, prefer it "
    "for correct spellings of names, addresses, resolution numbers, and item titles.\n"
    "- If you are unsure of a name or figure, describe it generically (for example 'a board "
    "member', 'a local business', 'a setback variance') rather than guessing.\n"
    "- Prefer concrete outcomes: what was decided, approved, denied, tabled, or scheduled, "
    "and anything that directly affects residents.\n"
    "- Write each bullet as one plain sentence. Skip procedural filler (pledge, roll call, "
    "adjournment) unless it is the only content.\n"
    "- Output MUST be a single valid JSON object and nothing else."
)

def build_user_prompt(meta, agenda_text, transcript_text, max_words, tier_label):
    display_date = meta.get("event_date", "")
    return (
        "MEETING\n"
        "Board: {cat}\n"
        "Date: {date}\n"
        "Title: {title}\n\n"
        "LENGTH TARGET\n"
        "{tier} Keep the whole summary at or under {mw} words. Include only sections "
        "that have real content. Choose headings from: \"Key actions\", \"Money\", "
        "\"Public hearings & comment\", \"Discussed\", \"Affects residents\". Omit any "
        "heading with nothing to report.\n\n"
        "OUTPUT JSON SHAPE (return exactly this structure, no prose, no code fences)\n"
        "{{\"tldr\": \"one or two sentences\", \"sections\": "
        "[{{\"heading\": \"Key actions\", \"items\": [\"one sentence\", \"...\"]}}]}}\n\n"
        "AGENDA (authoritative for names, addresses, and item titles; may be empty)\n"
        "{agenda}\n\n"
        "TRANSCRIPT (auto-generated; may contain errors)\n"
        "{transcript}\n"
    ).format(
        cat=meta.get("category") or "Meeting",
        date=display_date,
        title=(meta.get("title") or meta.get("category") or "").strip(),
        tier=tier_label, mw=max_words,
        agenda=agenda_text or "(no agenda available)",
        transcript=transcript_text,
    )

# ---------------------------------------------------------------------------
# JSON extraction / validation
# ---------------------------------------------------------------------------

def extract_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    a, b = text.find("{"), text.rfind("}")
    if a != -1 and b != -1 and b > a:
        return json.loads(text[a:b + 1])
    return json.loads(text)

ALLOWED_HEADINGS = ["Key actions", "Money", "Public hearings & comment",
                    "Discussed", "Affects residents"]

def validate_summary(obj, max_items=14):
    """Coerce the model output into the stored shape; raise ValueError if unusable."""
    if not isinstance(obj, dict):
        raise ValueError("not a JSON object")
    tldr = str(obj.get("tldr", "")).strip()
    raw_sections = obj.get("sections") or []
    sections, total = [], 0
    for sec in raw_sections:
        if not isinstance(sec, dict):
            continue
        heading = str(sec.get("heading", "")).strip()
        items = [str(i).strip() for i in (sec.get("items") or []) if str(i).strip()]
        if not heading or not items:
            continue
        # Trim to the global item cap so a runaway response cannot bloat the page.
        room = max_items - total
        if room <= 0:
            break
        items = items[:room]
        total += len(items)
        sections.append({"heading": heading, "items": items})
    if not tldr and not sections:
        raise ValueError("empty summary")
    return tldr, sections

# ---------------------------------------------------------------------------
# Per-meeting work
# ---------------------------------------------------------------------------

def has_usable_summary(record):
    s = record.get("summary")
    return (isinstance(s, dict)
            and s.get("status") in ("ok", "too_short")
            and s.get("prompt_version") == PROMPT_VERSION)

def summarize_file(path, client, model, use_agenda, dry_run, limiter=None):
    """Returns one of: 'generated', 'too_short', 'error'. Writes the JSON in place."""
    with open(path, "r", encoding="utf-8") as f:
        record = json.load(f)
    meta = record.get("meta", {})
    text = readable_text(record)
    words = len(text.split())

    if words < MIN_WORDS:
        record["summary"] = {
            "status": "too_short",
            "prompt_version": PROMPT_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "words": words,
        }
        if not dry_run:
            _atomic_write(path, record)
        return "too_short"

    if dry_run:
        return "generated"

    max_words, tier_label = length_tier(words)
    agenda_text = fetch_agenda_text(meta, AGENDA_CACHE) if use_agenda else ""

    # Meetings whose transcript alone exceeds a single request's budget are summarized
    # in chunks (extract facts per chunk, then summarize the notes), so even 4-hour
    # Town Board meetings fit under the rate limit with full coverage.
    chunked = _estimate_tokens(text) > PER_REQUEST_TOKEN_MAX
    source = _chunked_notes(client, model, text, limiter) if chunked else text
    if not source.strip():
        source = text[:PER_REQUEST_TOKEN_MAX * 4]
    user_prompt = build_user_prompt(meta, agenda_text, source, max_words, tier_label)
    tldr, sections = _summarize_text(client, model, user_prompt, limiter)

    record["summary"] = {
        "status": "ok",
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "agenda_grounded": bool(agenda_text),
        "chunked": chunked,
        "tldr": tldr,
        "sections": sections,
    }
    _atomic_write(path, record)
    return "generated"

CHUNK_SYSTEM = (
    "You extract concrete facts from one part of a Riverhead, NY town meeting "
    "transcript produced by speech-to-text (it may garble names and terms). List, as "
    "short plain bullet lines, every decision, vote, resolution number, application "
    "(with address), dollar amount, public hearing, and notable public comment in THIS "
    "part. No preamble and no conclusions, just the bullet lines. If this part has "
    "nothing substantive, output nothing."
)

def _estimate_tokens(s):
    return len(s) // 4   # ~4 chars/token for English; good enough for budgeting

def _retry_after(exc):
    try:
        ra = exc.response.headers.get("retry-after")
        return max(int(float(ra)), 5) if ra else None
    except Exception:
        return None

class _RateLimiter:
    """Coarse fixed-window limiter so workers stay under an input tokens-per-minute
    ceiling. Approximate; any residual 429 is still caught and retried."""
    def __init__(self, tpm):
        self.tpm = max(1000, tpm)
        self.lock = threading.Lock()
        self.window = time.monotonic()
        self.used = 0
    def acquire(self, est):
        with self.lock:
            now = time.monotonic()
            if now - self.window >= 60:
                self.window, self.used = now, 0
            if self.used > 0 and self.used + est > self.tpm:
                time.sleep(max(0.0, 60 - (now - self.window)))
                self.window, self.used = time.monotonic(), 0
            self.used += est

def _create(client, model, system, user, limiter, max_tokens):
    """One messages.create with rate-limit pacing and retries; returns text."""
    import anthropic
    last = None
    for attempt in range(6):
        if limiter:
            limiter.acquire(_estimate_tokens(system) + _estimate_tokens(user))
        try:
            resp = client.messages.create(
                model=model, max_tokens=max_tokens, system=system,
                messages=[{"role": "user", "content": user}],
            )
            return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        except anthropic.RateLimitError as e:
            last = e
            time.sleep(_retry_after(e) or 60)
        except (anthropic.APIConnectionError, anthropic.InternalServerError) as e:
            last = e
            time.sleep(RETRY_SLEEPS[min(attempt, len(RETRY_SLEEPS) - 1)])
        except anthropic.APIStatusError as e:
            if getattr(e, "status_code", 0) >= 500:
                last = e
                time.sleep(RETRY_SLEEPS[min(attempt, len(RETRY_SLEEPS) - 1)])
            else:
                raise
    raise RuntimeError("API call failed after retries: {}".format(last))

def _summarize_text(client, model, user_prompt, limiter):
    """Call the model for a structured summary, with one JSON-repair retry."""
    last = None
    for _ in range(2):
        raw = _create(client, model, SYSTEM, user_prompt, limiter, MAX_TOKENS)
        try:
            return validate_summary(extract_json(raw))
        except (json.JSONDecodeError, ValueError) as e:
            last = e
            user_prompt += "\n\nReturn ONLY the JSON object. No prose, no code fences."
    raise RuntimeError("model did not return valid JSON: {}".format(last))

def _chunked_notes(client, model, text, limiter):
    """For meetings too large for one request: extract facts chunk by chunk and return
    the concatenated notes, which a final pass summarizes. Preserves full coverage."""
    span = PER_REQUEST_TOKEN_MAX * 4   # characters per chunk
    chunks = [text[i:i + span] for i in range(0, len(text), span)]
    notes = []
    for idx, chunk in enumerate(chunks, 1):
        user = "PART {}/{} of the meeting transcript:\n\n{}".format(idx, len(chunks), chunk)
        out = _create(client, model, CHUNK_SYSTEM, user, limiter, 1200)
        if out.strip():
            notes.append(out.strip())
    return "\n".join(notes)

def _atomic_write(path, record):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def select_files(args):
    if args.files:
        paths = list(args.files)
    else:
        paths = sorted(glob.glob(os.path.join(TRANSCRIPTS_DIR, "**", "*.json"), recursive=True))
    selected = []
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8") as f:
                rec = json.load(f)
        except Exception as e:
            log("  WARNING: cannot read {}: {}".format(p, e))
            continue
        if args.days is not None:
            d = rec.get("meta", {}).get("event_date", "")
            try:
                age = (date.today() - datetime.strptime(d, "%Y-%m-%d").date()).days
                if age > args.days:
                    continue
            except Exception:
                continue
        if not args.force and has_usable_summary(rec):
            continue
        selected.append(p)
    if args.limit:
        selected = selected[:args.limit]
    return selected

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Generate AI summaries for Riverhead meeting transcripts.")
    ap.add_argument("files", nargs="*", help="Specific transcript JSON files (default: all under transcripts/).")
    ap.add_argument("--force", action="store_true", help="Regenerate even if a summary already exists.")
    ap.add_argument("--days", type=int, default=None, help="Only meetings within the last N days.")
    ap.add_argument("--limit", type=int, default=None, help="Process at most N transcripts.")
    ap.add_argument("--workers", type=int, default=2, help="Concurrent API calls (default 2).")
    ap.add_argument("--tpm", type=int, default=DEFAULT_TPM,
                    help="Input tokens/min ceiling to pace under (default %d; raise for higher API tiers)." % DEFAULT_TPM)
    ap.add_argument("--model", default=DEFAULT_MODEL, help="Claude model (default %s)." % DEFAULT_MODEL)
    ap.add_argument("--no-agenda", action="store_true", help="Skip agenda PDF grounding.")
    ap.add_argument("--dry-run", action="store_true", help="List what would be processed; no API calls or writes.")
    args = ap.parse_args()

    selected = select_files(args)
    log("Riverhead summarizer")
    log("  model:     {}".format(args.model))
    log("  to process: {} transcript(s){}".format(len(selected), "  [DRY RUN]" if args.dry_run else ""))
    if not selected:
        log("Nothing to do.")
        return 0

    client = None
    if not args.dry_run:
        try:
            import anthropic
            from anthropic import Anthropic
        except ImportError:
            log("ERROR: the 'anthropic' package is not installed. Run: pip3 install anthropic")
            return 2
        api_key, key_source = resolve_api_key()
        if not api_key:
            log("ERROR: no API key found. Add it to riverhead.env as")
            log("       ANTHROPIC_API_KEY=sk-ant-...   (or export ANTHROPIC_API_KEY).")
            return 2
        log("  key source: {}  (length {}, starts {}...)".format(
            key_source, len(api_key), api_key[:10]))
        client = Anthropic(api_key=api_key)
        # Preflight one tiny call so a bad key fails immediately and clearly,
        # instead of erroring once per meeting.
        try:
            client.messages.create(model=args.model, max_tokens=1,
                                    messages=[{"role": "user", "content": "ping"}])
        except anthropic.AuthenticationError:
            log("ERROR: the API key was rejected (401 invalid x-api-key).")
            log("       It loaded from: {}.".format(key_source))
            log("       Fix: confirm it is a current key at console.anthropic.com, then open a")
            log("       NEW terminal (to drop any stale exported key) and run again.")
            return 2
        except anthropic.APIStatusError as e:
            if getattr(e, "status_code", 0) == 401:
                log("ERROR: API key rejected (401). Check riverhead.env, then retry in a new terminal.")
                return 2
        except Exception:
            pass  # non-auth errors will surface per meeting

    limiter = _RateLimiter(args.tpm) if not args.dry_run else None

    counts = {"generated": 0, "too_short": 0, "error": 0}
    done = 0
    total = len(selected)

    def work(path):
        return path, summarize_file(path, client, args.model, not args.no_agenda, args.dry_run, limiter)

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
                log("  ERROR {}: {}".format(os.path.basename(path), e))
                status = "error"
            done += 1
            if status != "error":
                log("  [{}/{}] {}  {}".format(done, total, status, os.path.basename(path)))

    log("")
    log("Done. generated={generated}  too_short={too_short}  errors={error}".format(**counts))
    return 0

if __name__ == "__main__":
    sys.exit(main())
