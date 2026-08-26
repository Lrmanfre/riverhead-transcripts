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
DEFAULT_MODEL   = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
PROMPT_VERSION  = "1"           # bump to invalidate old summaries with --force
GAP_MARKER      = "[transcription gap]"

MIN_WORDS         = 300         # below this, transcript is too thin to summarize
MAX_TRANSCRIPT_CH = 400_000     # safety cap (~100k tokens); longest meeting fits
MAX_AGENDA_CH     = 15_000
MAX_AGENDA_PAGES  = 15
MAX_TOKENS        = 2500       # raised for the Sonnet 5 tokenizer (~30% more tokens)
RETRY_SLEEPS      = [5, 15, 40] # backoff between attempts on transient errors

# Agenda grounding. The portal URL's agenda_id is a DIFFERENT id space than
# GetMeetingFileStream's fileId (fileId=<agenda_id> returns the wrong, often
# years-off document). Fixed August 2026: the correct fileIds come from the
# Events entity's publishedFiles array (resolve_event_files below), and every
# fetched document must pass a date check before it is trusted, so a mismatched
# file is discarded instead of grounding on the wrong meeting. See AI_SUMMARIES.md.
GROUND_IN_AGENDA  = True
CIVICCLERK_API    = "https://riverheadny.api.civicclerk.com/v1"

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

def _curl_json(url):
    """Fetch JSON via macOS system curl (avoids LibreSSL TLS issues, matching
    riverhead_inventory.py)."""
    r = subprocess.run(
        ["/usr/bin/curl", "-s", "--fail", "-H", "Accept: application/json", url],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        raise RuntimeError("curl exit {}".format(r.returncode))
    return json.loads(r.stdout)

def resolve_event_files(event_id, cache_dir=AGENDA_CACHE):
    """Return the event's publishedFiles list [{fileId, type, name}, ...].

    The portal URL's agendaId is a different id space than the file-stream
    fileId; the publishedFiles array on the Events entity carries the real
    fileIds (verified August 2026: event 6454 -> agendaId 6017 but agenda
    fileId 12254). Note: Events(<id>) direct addressing 404s on this API;
    the $filter form works. Cached in agenda_cache/<eid>_files.json.
    """
    eid = re.sub(r"[^\w.-]", "_", str(event_id))
    cache_path = os.path.join(cache_dir, eid + "_files.json")
    if os.path.exists(cache_path):
        try:
            return json.load(open(cache_path, encoding="utf-8"))
        except Exception:
            pass
    url = "{}/Events?$filter=id%20eq%20{}".format(CIVICCLERK_API, event_id)
    try:
        data = _curl_json(url)
        events = data.get("value") or []
        files = (events[0].get("publishedFiles") or []) if events else []
        files = [{"fileId": f.get("fileId"), "type": (f.get("type") or "").strip(),
                  "name": (f.get("name") or "").strip()}
                 for f in files if f.get("fileId")]
        os.makedirs(cache_dir, exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(files, f)
        return files
    except Exception as e:
        log("    publishedFiles lookup failed ({}): {}".format(event_id, e))
        return []

def _date_variants(event_date):
    """Strings an official document for this date should contain."""
    try:
        d = datetime.strptime(event_date, "%Y-%m-%d")
    except Exception:
        return []
    return [
        d.strftime("%B") + " {}, {}".format(d.day, d.year),
        d.strftime("%B %d, %Y"),
        "{}/{}/{}".format(d.month, d.day, d.year),
        "{:02d}/{:02d}/{}".format(d.month, d.day, d.year),
        "{}/{}/{}".format(d.month, d.day, d.strftime("%y")),
        d.strftime("%Y-%m-%d"),
    ]

def text_matches_event_date(text, event_date):
    head = (text or "")[:4000]
    return any(v in head for v in _date_variants(event_date))

def fetch_official_text(meta, prefer=("Agenda",), cache_dir=AGENDA_CACHE,
                        max_chars=MAX_AGENDA_CH):
    """Plain text of the first available official document in `prefer` order
    (types as they appear in publishedFiles, e.g. "Agenda", "Minutes").
    Returns (text, type) or ("", "").

    Every fetched document must contain the event date near the top or it is
    discarded, so a wrong-id document can never ground a summary. Cached per
    fileId under agenda_cache/file_<fileId>.txt.
    """
    event_id   = meta.get("event_id")
    event_date = meta.get("event_date", "")
    files = resolve_event_files(event_id, cache_dir)
    ranked = [f for want in prefer
              for f in files if f["type"].lower() == want.lower()]
    for f in ranked:
        fid = f["fileId"]
        cache_path = os.path.join(cache_dir, "file_{}.txt".format(fid))
        text = ""
        if os.path.exists(cache_path):
            try:
                text = open(cache_path, encoding="utf-8").read()
            except Exception:
                text = ""
        if not text.strip():
            url = "{}/Meetings/GetMeetingFileStream(fileId={},plainText=true)".format(
                  CIVICCLERK_API, fid)
            try:
                r = subprocess.run(
                    ["/usr/bin/curl", "-sL", "--fail", "-H", "Accept: text/plain", url],
                    capture_output=True, text=True, timeout=60,
                )
                text = r.stdout if r.returncode == 0 else ""
            except Exception:
                text = ""
            head = text.lstrip()[:200].lower()
            if head.startswith("<!doctype") or "<html" in head:
                text = ""
            if text.strip():
                try:
                    os.makedirs(cache_dir, exist_ok=True)
                    with open(cache_path, "w", encoding="utf-8") as fh:
                        fh.write(text)
                except Exception:
                    pass
        if text.strip() and text_matches_event_date(text, event_date):
            return text[:max_chars], f["type"]
        if text.strip():
            log("    official doc fileId={} failed date check for {}; discarded".format(
                fid, event_date))
    return "", ""

def fetch_agenda_text(meta, cache_dir):
    """Agenda text for summary grounding, or '' (kept as the summarizer's
    entry point). Prefers the Agenda, falls back to Minutes (same resolution
    list, published after the meeting)."""
    if not GROUND_IN_AGENDA:
        return ""
    text, _kind = fetch_official_text(meta, prefer=("Agenda", "Minutes"),
                                      cache_dir=cache_dir)
    return text

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

# Sonnet 5 turns adaptive thinking ON by default; Sonnet 4.6 and earlier did not.
# Thinking tokens count against max_tokens, so an unchanged request can burn the
# entire budget on reasoning and return zero text. These are structured-JSON
# extraction calls, so we explicitly opt out to restore the 4.6 behaviour.
THINKING_OFF = {"type": "disabled"}
_NO_THINKING_PARAM = set()   # models that reject the parameter outright

def _send(client, kwargs):
    """Issue one request and return the assembled Message.

    Streaming is not optional here. The SDK refuses a non-streaming request
    whose max_tokens implies it could run past 10 minutes, which a large
    extraction does. get_final_message() reassembles the same Message object,
    so stop_reason, content blocks and usage all behave as before.
    """
    with client.messages.stream(**kwargs) as stream:
        return stream.get_final_message()

def _create(client, model, system, user, limiter, max_tokens):
    """One messages.create with rate-limit pacing and retries; returns text."""
    import anthropic
    last = None
    for attempt in range(6):
        if limiter:
            limiter.acquire(_estimate_tokens(system) + _estimate_tokens(user))
        try:
            kwargs = dict(model=model, max_tokens=max_tokens, system=system,
                          messages=[{"role": "user", "content": user}])
            if model not in _NO_THINKING_PARAM:
                kwargs["thinking"] = THINKING_OFF
            try:
                resp = _send(client, kwargs)
            except anthropic.BadRequestError as e:
                if "thinking" in str(e).lower() and "thinking" in kwargs:
                    # Model does not accept the parameter; remember and retry.
                    _NO_THINKING_PARAM.add(model)
                    kwargs.pop("thinking")
                    resp = _send(client, kwargs)
                else:
                    raise
            text = "".join(b.text for b in resp.content
                           if getattr(b, "type", "") == "text")
            if not text.strip():
                # No text block came back. Silently returning "" here used to
                # surface downstream as a bogus "did not return valid JSON".
                # Report what the API actually said instead.
                blocks = [getattr(b, "type", "?") for b in resp.content] or ["<none>"]
                u = getattr(resp, "usage", None)
                raise RuntimeError(
                    "empty text response from {}: stop_reason={} blocks={} "
                    "in={} out={} (max_tokens={})".format(
                        model, getattr(resp, "stop_reason", None), ",".join(blocks),
                        getattr(u, "input_tokens", "?"), getattr(u, "output_tokens", "?"),
                        max_tokens))
            return text
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
