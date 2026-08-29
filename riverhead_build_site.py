#!/usr/bin/env python3
"""
riverhead_build_site.py
=======================
Reads all JSON transcript files and generates a static HTML site
suitable for GitHub Pages + Pagefind full-text search.

Output structure:
  docs/
    index.html
    meetings/<category>/<date>_<id>.html
    assets/style.css

Usage:
    python3 riverhead_build_site.py
    pagefind --site docs
"""

import json, os, glob, re, shutil
import html as htmlmod
from datetime import datetime
from collections import defaultdict

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TRANSCRIPTS_DIR = "transcripts"
OUTPUT_DIR      = "docs"
SITE_TITLE      = "Riverhead Town Meeting Transcripts"
SITE_DESC       = "Searchable public record of Riverhead Town government meetings."
PORTAL_BASE     = "https://riverheadny.portal.civicclerk.com"
PARAGRAPH_PAUSE = 3.0
BUILD_TIME      = datetime.now().strftime("%b %-d, %Y at %-I:%M %p")

# --- Failed-transcription guard -------------------------------------------
# Whisper emits a stub like "Thank you." when handed silence or a bad audio
# extraction, so a failed transcription looks like a successful tiny one. On
# 2026-08-18 a 172-minute Town Board meeting transcribed to 2 words and would
# have published as the official-looking record of that meeting.
#
# Real meetings run 100-200 words per minute. A floor of 5 wpm is a 20x+ margin
# below normal speech, so it only catches recordings that are effectively silent,
# not quiet or sparsely-attended meetings. When duration is missing or
# unparseable we fall back to a small absolute floor instead.
MIN_WORDS_PER_MINUTE = 5
MIN_WORDS_NO_DURATION = 25

# Two different problems, which need different treatment:
#
#   "incomplete" — the recording we transcribed is far shorter than the meeting
#     CivicClerk describes, so the transcript cannot be the record of it. This is
#     the 2026-08-18 case: a 2.9-second stub served for a 172-minute meeting.
#     Suppress it; a partial record presented as complete is worse than none.
#
#   "thin" — the recording IS full length, there was just very little speech in
#     it. Usually legitimate: a session that opens and immediately goes into
#     executive session. Show the text, with a caveat so nobody reads three
#     sentences as the whole meeting.
MIN_COVERAGE_RATIO = 0.5

SUPPORT_CONFIG_FILE = "support_config.json"
SUPPORT_PAGE        = "support.html"
SUPPORT_LABEL       = "Support this project"
CONTACT_EMAIL       = "riverheadtranscripts@gmail.com"

THANKS_PAGE     = "thanks.html"

PRIVACY_PAGE    = "privacy.html"
# Bump these by hand whenever the privacy policy text changes. Deliberately NOT
# tied to BUILD_TIME, which would make the date churn on every nightly build.
# Two dates because the policy text itself depends on CF_ANALYTICS_TOKEN below.
# With no token the page says exactly what it has said since launch, so it keeps
# the launch date instead of claiming a change that did not happen.
PRIVACY_UPDATED_BASE      = "August 16, 2026"
PRIVACY_UPDATED_ANALYTICS = "August 29, 2026"

# --- Analytics -------------------------------------------------------------
# Cloudflare Web Analytics. Cookieless, sets no identifiers, needs no consent
# banner. GitHub Pages exposes no server logs, so without this the site has zero
# visibility into real traffic. (GitHub's Insights > Traffic graph counts views
# of the repository page on github.com, NOT visits to riverheadtranscripts.org.)
#
# To switch on: Cloudflare dashboard > Analytics & Logs > Web Analytics > Add a
# site > riverheadtranscripts.org, then paste the token out of the snippet it
# hands you between the quotes below and rebuild. The token is public by design;
# it ships in the HTML of every page, so it does not belong in riverhead.env.
#
# Empty string = no beacon on any page AND a privacy policy that says the site
# runs no analytics. Emptying it again cleanly reverses both.
CF_ANALYTICS_TOKEN = ""


def analytics_on():
    """True when a Cloudflare beacon token is configured."""
    return bool(CF_ANALYTICS_TOKEN.strip())

# Set by main() once, then read by html_header() on every page.
SUPPORT_ENABLED = False


def load_support_config(path=SUPPORT_CONFIG_FILE):
    """Return the resolved Stripe link block, or None to disable the feature.

    Returning None on any problem is deliberate: the nightly unattended build must
    never fail because of a support-config typo. Worst case the button disappears.
    """
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (ValueError, OSError) as e:
        print("  WARNING: could not read {} ({}). Support page disabled.".format(path, e))
        return None

    if raw.get("enabled") is False:
        print("  Support page disabled via \"enabled\": false in {}.".format(path))
        return None

    mode = raw.get("mode", "test")
    block = raw.get(mode)
    if not isinstance(block, dict):
        print("  WARNING: {} has no '{}' block. Support page disabled.".format(path, mode))
        return None

    cfg = dict(block)
    cfg["mode"] = mode
    cfg["default_frequency"] = raw.get("default_frequency", "annual")
    if cfg["default_frequency"] not in ("once", "monthly", "annual"):
        cfg["default_frequency"] = "annual"

    # Count tiers that actually have a URL, so the page can warn instead of
    # rendering dead buttons.
    configured = 0
    for key in ("once", "monthly", "annual"):
        for tier in cfg.get(key) or []:
            if isinstance(tier, dict) and tier.get("url"):
                configured += 1
    if (cfg.get("custom") or {}).get("url"):
        configured += 1
    cfg["configured_count"] = configured
    return cfg

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def slugify(text):
    text = (text or "uncategorized").lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text.strip("-") or "uncategorized"

def format_date_display(date_str):
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").strftime("%B %-d, %Y")
    except Exception:
        return date_str

def format_timestamp(seconds):
    try:
        s = int(float(seconds))
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        return "{:d}:{:02d}:{:02d}".format(h,m,sec) if h else "{:d}:{:02d}".format(m,sec)
    except Exception:
        return ""

GAP_MARKER = "[transcription gap]"

def sanitize_segment_text(text, min_repeat=8, max_unit=25):
    """Detect and truncate Whisper hallucination repetition loops.

    Whisper sometimes gets stuck repeating a token hundreds of times
    (e.g. "headheadheadhead...").  This finds the first occurrence of any
    3-25 character substring repeated 8+ times consecutively and truncates
    the text just before it.  Returns GAP_MARKER if nothing useful remains.
    """
    if not text:
        return text
    # Also pass through any gap markers already in the JSON from prior cleaning
    if text == GAP_MARKER:
        return text
    pattern = re.compile(r'(.{3,' + str(max_unit) + r'})\1{' + str(min_repeat - 1) + r',}')
    m = pattern.search(text)
    if m:
        truncated = text[:m.start()].strip().rstrip(',').strip()
        return truncated if len(truncated) >= 8 else GAP_MARKER
    return text

def group_into_paragraphs(segments, pause=PARAGRAPH_PAUSE):
    paragraphs, current, prev_end = [], [], 0.0
    prev_text = None
    for seg in segments:
        text = sanitize_segment_text((seg.get("text") or "").strip())
        if not text or text in (".", "..", "..."):
            continue
        # Collapse consecutive identical segments (Whisper hallucination loops
        # and [transcription gap] runs) to a single occurrence.
        if text.lower() == prev_text:
            continue
        prev_text = text.lower()
        start = float(seg.get("start", 0))
        end   = float(seg.get("end", start))
        if current and (start - prev_end) > pause:
            paragraphs.append(current)
            current = []
        current.append(seg)
        prev_end = end
    if current:
        paragraphs.append(current)
    return paragraphs

def transcript_word_count(record):
    """Count usable words in a transcript, after hallucination sanitizing.

    Gap markers and empty segments do not count, so a transcript that is mostly
    truncated hallucination loops scores near zero rather than looking healthy.
    """
    segments = record.get("segments") or []
    if segments:
        words = 0
        for seg in segments:
            text = sanitize_segment_text((seg.get("text") or "").strip())
            if not text or text == GAP_MARKER:
                continue
            words += len(text.split())
        return words
    # Fall back to the flat transcript field if segments are absent.
    return len((record.get("transcript") or "").split())


def transcript_duration_minutes(record):
    """Recording length in minutes, best effort.

    Only a minority of records carry meta.duration_minutes (34 of 302 as of
    Aug 2026), so fall back to the end timestamp of the last segment. That
    under-states length when transcription stopped early, which biases toward
    NOT flagging. That is the safe direction: a missed failure is recoverable,
    a wrongly suppressed transcript is not.
    """
    raw = record.get("meta", {}).get("duration_minutes", "")
    try:
        d = float(raw)
        if d > 0:
            return d
    except (TypeError, ValueError):
        pass

    end = 0.0
    for seg in record.get("segments") or []:
        try:
            end = max(end, float(seg.get("end", 0)))
        except (TypeError, ValueError):
            continue
    return end / 60.0 if end > 0 else 0.0


def transcript_span_minutes(record):
    """How much wall-clock time the transcript's segments actually cover."""
    end = 0.0
    for seg in record.get("segments") or []:
        try:
            end = max(end, float(seg.get("end", 0)))
        except (TypeError, ValueError):
            continue
    return end / 60.0


def stated_duration_minutes(record):
    """The meeting length CivicClerk reports, or None. Never inferred."""
    try:
        v = float(record.get("meta", {}).get("duration_minutes", ""))
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def transcript_quality(record):
    """Classify a transcript. Returns (level, reason).

    level is None (fine), "incomplete" (suppress), or "thin" (show with caveat).
    See MIN_COVERAGE_RATIO for why these are treated differently.
    """
    words  = transcript_word_count(record)
    stated = stated_duration_minutes(record)
    span   = transcript_span_minutes(record)

    # The recording itself does not cover the meeting.
    if stated and span < stated * MIN_COVERAGE_RATIO:
        return ("incomplete",
                "The recording transcribed here runs about {:.0f} minute{}, but "
                "this meeting is listed as {:.0f} minutes long.".format(
                    span, "" if round(span) == 1 else "s", stated))

    # Full-length recording, but almost nothing was said on it.
    duration = transcript_duration_minutes(record)
    if duration > 0:
        if words < duration * MIN_WORDS_PER_MINUTE:
            return ("thin",
                    "This {:.0f}-minute recording produced only {} word{} of "
                    "speech.".format(duration, words,
                                     "" if words == 1 else "s"))
    elif words < MIN_WORDS_NO_DURATION:
        return ("thin",
                "Only {} word{} of speech could be transcribed from this "
                "recording.".format(words, "" if words == 1 else "s"))
    return (None, None)


def load_all_transcripts(d):
    records = []
    for path in sorted(glob.glob(os.path.join(d, "**", "*.json"), recursive=True)):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["_source_path"] = path
            records.append(data)
        except Exception as e:
            print("  WARNING: {}: {}".format(path, e))
    return records

# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

CSS = """
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: Georgia, serif; font-size: 18px; line-height: 1.7; color: #1a1a1a; background: #fafaf8; }
a { color: #1a5c8a; text-decoration: none; }
a:hover { text-decoration: underline; }

.site-header { background: #1a3a5c; color: white; padding: 1.5rem 2rem;
  display: flex; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; gap: .5rem; }
.site-header .header-left { flex: 1; }
.site-header h1 { font-size: 1.5rem; font-weight: normal; letter-spacing: .02em; }
.site-header p { font-size: .9rem; opacity: .8; margin-top: .25rem; }
.site-header a { color: white; }
.site-header .build-stamp { font-size: .75rem; opacity: .5; white-space: nowrap; padding-top: .3rem; }
.site-header .header-right { display: flex; flex-direction: column; align-items: flex-end; gap: .5rem; }
.support-btn { display: inline-block; padding: .5rem 1.15rem; font-size: .9rem;
  font-weight: bold; letter-spacing: .01em; border-radius: 4px; white-space: nowrap;
  background: #c62828; color: #fff !important; border: 1px solid rgba(255,255,255,.55);
  box-shadow: 0 1px 4px rgba(0,0,0,.35); transition: background .15s, box-shadow .15s; }
.support-btn:hover { background: #d1342b; border-color: #fff; text-decoration: none;
  box-shadow: 0 2px 7px rgba(0,0,0,.4); }

.container { max-width: 860px; margin: 0 auto; padding: 2rem 1.5rem; }

#search { margin: 2rem 0; }
.pagefind-ui__search-input { font-family: inherit !important; font-size: 1.1rem !important; }
.pagefind-ui__result-title a { color: #1a5c8a !important; }

.category-section { margin-bottom: 2.5rem; }
.category-section h2 { font-size: 1rem; font-weight: bold; text-transform: uppercase;
  letter-spacing: .08em; color: #555; border-bottom: 2px solid #ddd;
  padding-bottom: .4rem; margin-bottom: 1rem; }
.meeting-list { list-style: none; }
.meeting-list li { display: flex; justify-content: space-between; align-items: baseline;
  padding: .4rem 0; border-bottom: 1px solid #eee; gap: 1rem; flex-wrap: wrap; }
.meeting-list .date { font-size: .85rem; color: #666; white-space: nowrap; }
.badge { display: inline-block; padding: .1rem .4rem; border-radius: 3px;
  background: #e8f0f8; color: #1a5c8a; font-size: .75rem; margin-left: .3rem; }
a.badge-link { cursor: pointer; }
a.badge-link:hover { background: #1a5c8a; color: #fff; text-decoration: none; }

.meeting-meta { background: #f0f4f8; border-left: 4px solid #1a5c8a;
  padding: 1rem 1.5rem; margin-bottom: 1.5rem; font-size: .95rem; }
.meeting-meta h1 { font-size: 1.4rem; margin-bottom: .5rem; }
.meeting-meta .meta-row { margin-top: .4rem; color: #444; }
.doc-links { margin-top: .75rem; }
.doc-links a { display: inline-block; margin-right: .75rem; font-size: .85rem;
  padding: .25rem .6rem; border: 1px solid #1a5c8a; border-radius: 3px; }
.doc-links a:hover { background: #1a5c8a; color: white; }

.video-wrap { margin-bottom: 1.5rem; background: #000; border-radius: 4px; overflow: hidden; }
.video-wrap video { width: 100%; display: block; max-height: 480px; }

/* Timestamped view — sits directly below video */
.transcript-timestamped { margin-bottom: 2.5rem; }
.transcript-timestamped h2,
.transcript-readable h2 { font-size: 1rem; text-transform: uppercase;
  letter-spacing: .08em; color: #888; margin-bottom: 1.2rem; }

.toggle-btn { display: inline-block; margin: 0 0 1.5rem; padding: .4rem 1rem;
  font-size: .85rem; font-family: inherit; background: #f0f4f8;
  border: 1px solid #aac; border-radius: 3px; cursor: pointer; color: #1a5c8a; }
.toggle-btn:hover { background: #dce8f4; }

#timestamped-view { display: none; }
#timestamped-view.open { display: block; }

.segment { display: flex; gap: 1rem; margin-bottom: .5rem; align-items: flex-start; }
.segment .ts { font-family: monospace; font-size: .78rem; color: #bbb;
  white-space: nowrap; padding-top: .3rem; flex-shrink: 0; width: 3.5rem; text-align: right; }
.segment .ts a { color: #bbb; cursor: pointer; }
.segment .ts a:hover { color: #1a5c8a; }
.segment.active .text { background: #fff8dc; border-radius: 3px; padding: 0 4px; }

/* Readable transcript — sits below timestamped */
.transcript-readable { margin-top: 1rem; }
.transcript-readable p { margin-bottom: 1.1rem; }

.breadcrumb { font-size: .85rem; margin-bottom: 1.5rem; color: #888; }
.breadcrumb a { color: #1a5c8a; }

.site-footer { margin-top: 4rem; padding: 1.5rem 2rem; border-top: 1px solid #ddd;
  font-size: .85rem; color: #888; text-align: center; }

.meeting-list-hidden { display: none; }
.meeting-list-hidden.open { display: contents; }
.expand-btn { display: inline-flex; align-items: center; gap: .4rem;
  margin-top: .5rem; padding: .35rem .8rem; font-family: inherit;
  font-size: .85rem; color: #1a5c8a; background: #f0f4f8;
  border: 1px solid #aac; border-radius: 3px; cursor: pointer; }
.expand-btn:hover { background: #dce8f4; }
.expand-btn .chevron { font-size: .7rem; transition: transform .2s; display: inline-block; }
.expand-btn .chevron.up { transform: rotate(180deg); }

.gap-marker { color: #bbb; font-style: italic; font-size: .9em; }

/* Failed-transcription notice — replaces the transcript body when Whisper
   returned effectively nothing for a meeting of real length. */
.transcript-missing { background: #fdf6e3; border: 1px solid #e8d9a8;
  border-left: 4px solid #b8860b; border-radius: 4px;
  padding: 1.1rem 1.5rem 1.2rem; margin-bottom: 2rem; }
.transcript-missing h2 { font-size: 1rem; text-transform: uppercase;
  letter-spacing: .08em; color: #8a6d1f; margin-bottom: .7rem; }
.transcript-missing p { margin-bottom: .6rem; }
.transcript-missing p:last-child { margin-bottom: 0; }
.transcript-missing .why { font-size: .85rem; color: #6b6b6b; }
.badge-warn { background: #fdf6e3; color: #8a6d1f; }

/* AI summary — sits at the top of the page, above the video */
.transcript-summary { background: #f7f9fc; border: 1px solid #d6e2ef;
  border-left: 4px solid #1a5c8a; border-radius: 4px;
  padding: 1.1rem 1.5rem 1.3rem; margin-bottom: 1.75rem; }
.transcript-summary .summary-head { display: flex; align-items: center;
  justify-content: space-between; gap: 1rem; margin-bottom: .6rem; }
.transcript-summary h2 { font-size: 1rem; text-transform: uppercase;
  letter-spacing: .08em; color: #1a5c8a; }
.transcript-summary .ai-tag { font-size: .6rem; text-transform: uppercase;
  letter-spacing: .06em; background: #1a5c8a; color: #fff;
  padding: .12rem .4rem; border-radius: 3px; margin-left: .4rem; vertical-align: middle; }
.transcript-summary .summary-toggle { font-size: .78rem; font-family: inherit;
  background: none; border: none; color: #1a5c8a; cursor: pointer; padding: 0; }
.transcript-summary .summary-toggle:hover { text-decoration: underline; }
.transcript-summary .tldr { font-size: 1.05rem; margin-bottom: 1rem; }
.transcript-summary h3 { font-size: .8rem; text-transform: uppercase;
  letter-spacing: .05em; color: #555; margin: 1rem 0 .4rem; }
.transcript-summary ul { margin: 0 0 .3rem 1.2rem; }
.transcript-summary li { margin-bottom: .35rem; }
.transcript-summary .summary-disclaimer { font-size: .75rem; color: #888;
  font-style: italic; margin-top: 1rem; border-top: 1px solid #e3e3e3; padding-top: .6rem; }
#summary-body.collapsed { display: none; }

/* Decisions & votes — below the AI summary, above the video */
.transcript-decisions { background: #f6faf7; border: 1px solid #cfe3d4;
  border-left: 4px solid #2e7d32; border-radius: 4px;
  padding: 1.1rem 1.5rem 1.3rem; margin-bottom: 1.75rem; }
.transcript-decisions .summary-head { display: flex; align-items: center;
  justify-content: space-between; gap: 1rem; margin-bottom: .6rem; }
.transcript-decisions h2 { font-size: 1rem; text-transform: uppercase;
  letter-spacing: .08em; color: #2e7d32; }
.transcript-decisions .ai-tag { font-size: .6rem; text-transform: uppercase;
  letter-spacing: .06em; background: #2e7d32; color: #fff;
  padding: .12rem .4rem; border-radius: 3px; margin-left: .4rem; vertical-align: middle; }
.transcript-decisions .summary-toggle { font-size: .78rem; font-family: inherit;
  background: none; border: none; color: #2e7d32; cursor: pointer; padding: 0; }
.transcript-decisions .summary-toggle:hover { text-decoration: underline; }
.transcript-decisions .summary-disclaimer { font-size: .75rem; color: #888;
  font-style: italic; margin-top: 1rem; border-top: 1px solid #dde8e0; padding-top: .6rem; }
#decisions-body.collapsed { display: none; }
.decision-item { padding: .65rem 0; border-bottom: 1px solid #e2ede5; }
.decision-item:last-child { border-bottom: none; }
.decision-num { font-family: monospace; font-size: .82rem; color: #555;
  margin-right: .5rem; }
.outcome-badge { display: inline-block; font-size: .68rem; text-transform: uppercase;
  letter-spacing: .05em; padding: .12rem .45rem; border-radius: 3px;
  margin-right: .5rem; vertical-align: middle; }
.outcome-adopted { background: #e3f2e6; color: #1e5c28; }
.outcome-defeated { background: #fdeaea; color: #a02020; }
.outcome-tabled, .outcome-withdrawn, .outcome-amended { background: #fdf6e3; color: #8a6d1f; }
.outcome-held, .outcome-unknown { background: #ececec; color: #666; }
.decision-votes { font-size: .82rem; color: #555; margin-top: .3rem; }
.decision-votes .v-yes { color: #1e5c28; }
.decision-votes .v-no { color: #a02020; font-weight: bold; }
.decision-votes .v-other { color: #8a6d1f; }
.decision-votes .no-rollcall { font-style: italic; color: #888; }
.decision-jump { font-size: .78rem; margin-left: .5rem; white-space: nowrap; }
.decision-jump-untimed { color: #8a6d1f; font-style: italic; white-space: normal; }

/* Global decisions page */
.decisions-page .filters { display: flex; gap: .75rem; flex-wrap: wrap;
  margin-bottom: 1.75rem; }
.decisions-page .filters input[type=text] { flex: 1 1 260px; padding: .45rem .7rem;
  font-family: inherit; font-size: .9rem; border: 1px solid #bbb; border-radius: 3px; }
.decisions-page .filters select { padding: .45rem .5rem; font-family: inherit;
  font-size: .9rem; border: 1px solid #bbb; border-radius: 3px; background: #fff; }
.decisions-page .meeting-group h2 { font-size: 1rem; margin: 1.8rem 0 .4rem; }
.decisions-page .meeting-group h2 a { color: #1a5c8a; }
.decisions-page .decision-item.hidden { display: none; }
.decisions-page .meeting-group.hidden { display: none; }
.decisions-page .count-line { font-size: .85rem; color: #888; margin-bottom: .5rem; }

.nav-link { font-size: .85rem; margin-right: 1rem; color: #1a5c8a; }

/* Support page and other prose pages */
.support-page h1, .text-page h1 { font-size: 1.8rem; margin-bottom: .5rem; }
.support-page .lede, .text-page .lede { font-size: 1.1rem; color: #444; margin-bottom: 1.5rem; }
.support-page h2, .text-page h2 { font-size: 1rem; text-transform: uppercase;
  letter-spacing: .08em; color: #555; margin: 2rem 0 .8rem; }
.support-page p, .text-page p { margin-bottom: 1rem; }
.text-page ul { margin: 0 0 1rem 1.3rem; }
.text-page li { margin-bottom: .4rem; }
.text-page .updated { font-size: .85rem; color: #888; margin-bottom: 1.5rem; }

.support-box { background: #f7f9fc; border: 1px solid #d6e2ef;
  border-radius: 4px; padding: 1.5rem; margin: 1.5rem 0 2rem; }
.freq-toggle { display: flex; gap: .5rem; flex-wrap: wrap; margin-bottom: 1.25rem; }
.freq-btn { flex: 1 1 auto; padding: .55rem 1rem; font-family: inherit; font-size: .9rem;
  background: #fff; border: 1px solid #aac; border-radius: 3px; cursor: pointer;
  color: #1a5c8a; }
.freq-btn:hover { background: #dce8f4; }
.freq-btn.active { background: #1a3a5c; border-color: #1a3a5c; color: #fff; }

.amount-panel { display: none; }
.amount-panel.active { display: block; }
.amount-grid { display: flex; gap: .6rem; flex-wrap: wrap; }
.amount-btn { flex: 1 1 6rem; text-align: center; padding: .9rem .5rem;
  font-size: 1.15rem; border: 2px solid #1a5c8a; border-radius: 4px;
  background: #fff; color: #1a5c8a; }
.amount-btn:hover { background: #1a5c8a; color: #fff; text-decoration: none; }
.amount-btn .per { display: block; font-size: .7rem; text-transform: uppercase;
  letter-spacing: .05em; opacity: .7; margin-top: .15rem; }
.amount-btn.disabled { border-color: #ccc; color: #aaa; background: #f5f5f5;
  cursor: not-allowed; pointer-events: none; }
.amount-btn.featured { background: #1a5c8a; color: #fff; }
.amount-btn.featured:hover { background: #14476b; }
.amount-btn .suggested { display: block; font-size: .6rem; text-transform: uppercase;
  letter-spacing: .07em; opacity: .85; margin-bottom: .15rem; }
.amount-custom { display: block; margin-top: .6rem; text-align: center;
  padding: .8rem .5rem; font-size: 1.05rem; border: 2px dashed #9bb8cf;
  border-radius: 4px; background: #fff; color: #1a5c8a; }
.amount-custom:hover { border-style: solid; background: #1a5c8a; color: #fff;
  text-decoration: none; }
.support-note { font-size: .85rem; color: #666; margin-top: 1rem; }
.support-warning { background: #fff8dc; border: 1px solid #e0d18a; border-radius: 4px;
  padding: .8rem 1rem; font-size: .85rem; color: #6b5a1a; margin-bottom: 1.25rem; }

.support-faq dt { font-weight: bold; margin-top: 1.2rem; }
.support-faq dd { margin-left: 0; color: #444; }

/* Contribute ask, placed between the meeting summary and the full transcript.
   It renders inside .container, so it needs no width wrapper of its own. */
.support-cta { background: #f7f9fc; border: 1px solid #d6e2ef; border-radius: 6px;
  padding: 1.4rem 1.6rem; margin: 2rem 0 2.5rem; }
.support-cta h2 { font-size: 1.05rem; margin: 0 0 .6rem; }
.support-cta p { margin: 0 0 .85rem; color: #444; font-size: .95rem; line-height: 1.55; }
.support-cta p:last-child { margin-bottom: 0; }
.cta-btn { display: inline-block; padding: .55rem 1.3rem; background: #c62828;
  color: #fff; border-radius: 4px; font-weight: bold; font-size: .95rem; }
.cta-btn:hover { background: #d1342b; color: #fff; text-decoration: none; }

@media (max-width: 600px) {
  body { font-size: 16px; }
  .site-header { padding: 1.2rem 1.25rem; flex-direction: column;
    align-items: stretch; gap: .75rem; }
  .site-header .header-left { flex: none; min-width: 0; }
  .site-header h1 { font-size: 1.3rem; }
  .site-header .header-right { flex-direction: row; flex-wrap: wrap;
    align-items: center; width: 100%; min-width: 0;
    justify-content: flex-start; column-gap: 1rem; row-gap: .6rem; }
  .site-header .nav-link { margin-right: 0; }
  .site-header .build-stamp { padding-top: 0; white-space: normal;
    flex: 1 1 100%; }
  .support-btn { flex: 0 0 auto; }
  .amount-btn { flex: 1 1 100%; }
  .support-cta { padding: 1.2rem 1.1rem; margin: 1.5rem 0 2rem; }
  .cta-btn { display: block; text-align: center; }

  /* Stack each timestamp above its line instead of hiding it: the
     timestamped view is useless without the timestamps, and a 3.5rem
     gutter is too wide to keep beside the text on a phone. */
  .segment { flex-direction: column; gap: .1rem; margin-bottom: .9rem; }
  .segment .ts { width: auto; text-align: left; padding-top: 0;
    font-size: .72rem; }
  .segment .ts a { color: #1a5c8a; }
}
"""

# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def analytics_beacon():
    """Cloudflare's beacon snippet, or "" when no token is configured.

    Cloudflare's docs suggest placing this just before </body>. It is deferred,
    so it executes after the document is parsed wherever it sits, and <head> is
    the one hook every page on this site shares.
    """
    if not analytics_on():
        return ""
    return ('\n  <!-- Cloudflare Web Analytics (cookieless, no personal data) -->'
            '\n  <script defer src="https://static.cloudflareinsights.com/beacon.min.js"'
            ' data-cf-beacon=\'{"token": "' + CF_ANALYTICS_TOKEN.strip() + '"}\'></script>')


def html_head(title, depth=0):
    rel = "../" * depth
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{t} — {s}</title>
  <link rel="stylesheet" href="{rel}assets/style.css">
  <link href="{rel}_pagefind/pagefind-ui.css" rel="stylesheet">
  <script src="{rel}_pagefind/pagefind-ui.js"></script>{beacon}
</head>""".format(t=title, s=SITE_TITLE, rel=rel, beacon=analytics_beacon())

def html_header(depth=0, show_support=True):
    rel = "../" * depth
    support_btn = ""
    if SUPPORT_ENABLED and show_support:
        support_btn = ('<a class="support-btn" href="{rel}{page}">{label}</a>'
                       .format(rel=rel, page=SUPPORT_PAGE, label=SUPPORT_LABEL))
    return """<header class="site-header">
  <div class="header-left">
    <a href="{rel}index.html"><h1>{t}</h1></a>
    <p>{d}</p>
  </div>
  <div class="header-right">
    <a class="nav-link" href="{rel}decisions/index.html">Decisions &amp; Votes</a>
    {btn}
    <div class="build-stamp">Updated {bt}</div>
  </div>
</header>""".format(rel=rel, t=SITE_TITLE, d=SITE_DESC, bt=BUILD_TIME, btn=support_btn)

def html_footer(depth=0):
    rel = "../" * depth
    return """<footer class="site-footer">
  <p>Transcripts generated by volunteers using OpenAI Whisper. Not an official record.
  Source videos: <a href="https://riverheadny.portal.civicclerk.com/">CivicClerk portal</a>.</p>
  <p>Questions, suggestions, or bugs? Email <a href="mailto:{e}">{e}</a></p>
  <p><a href="{rel}{p}">Privacy policy</a></p>
</footer>""".format(rel=rel, p=PRIVACY_PAGE, e=CONTACT_EMAIL)

VIDEO_JS = """
<script>
function seekTo(seconds) {
  var video = document.getElementById('meeting-video');
  if (!video) return;
  video.currentTime = seconds;
  video.play();
  video.scrollIntoView({ behavior: 'smooth', block: 'center' });
  document.querySelectorAll('.segment').forEach(function(s) { s.classList.remove('active'); });
  var seg = event.target.closest('.segment');
  if (seg) seg.classList.add('active');
}
function toggleTimestamps(btn) {
  var view = document.getElementById('timestamped-view');
  var open = view.classList.toggle('open');
  btn.textContent = open ? 'Hide timestamped view' : 'Show timestamped view';
}
</script>"""

SUMMARY_JS = """
<script>
function toggleSummary(btn) {
  var body = document.getElementById('summary-body');
  if (!body) return;
  var collapsed = body.classList.toggle('collapsed');
  btn.textContent = collapsed ? 'Show' : 'Hide';
}
</script>"""

DECISIONS_JS = """
<script>
function toggleDecisions(btn) {
  var body = document.getElementById('decisions-body');
  if (!body) return;
  var collapsed = body.classList.toggle('collapsed');
  btn.textContent = collapsed ? 'Show' : 'Hide';
}
</script>"""

SUPPORT_JS = """
<script>
function showFreq(btn, key) {
  var i;
  var btns = document.querySelectorAll('.freq-btn');
  for (i = 0; i < btns.length; i++) { btns[i].classList.remove('active'); }
  btn.classList.add('active');
  var panels = document.querySelectorAll('.amount-panel');
  for (i = 0; i < panels.length; i++) { panels[i].classList.remove('active'); }
  var target = document.getElementById('panel-' + key);
  if (target) { target.classList.add('active'); }
}
</script>"""

# ---------------------------------------------------------------------------
# End-of-transcript contribute ask
# ---------------------------------------------------------------------------

# The header button sits above the fold, before the reader has gotten anything.
# The other natural moment is the break between the meeting summary and the full
# transcript: the summary is what most readers came for and where most of them
# stop, and almost nobody reaches the end of a 170-minute transcript. So this
# block renders after the timestamped-view toggle and before the transcript body.
#
# It sits INSIDE <main>, which carries data-pagefind-body, so data-pagefind-ignore
# is load-bearing here, not decorative: without it this copy would be indexed into
# every meeting's search excerpt.
#
# No running-cost figure by design. Naming a small annual number invites the reader
# to anchor on it and undercuts the recurring ask.

def support_cta(depth=0):
    """Contribute ask for a content page. "" when support is off."""
    if not SUPPORT_ENABLED:
        return ""
    rel = "../" * depth
    return """<aside class="support-cta" data-pagefind-ignore>
  <h2>Was this useful?</h2>
  <p>The Town does not publish searchable transcripts of its meetings, so this site does.
  Every meeting here is transcribed, summarized, and posted by one person, and nobody is
  paid for any of it.</p>
  <p>If this saved you an hour of scrubbing through meeting video, chipping in keeps it
  going.</p>
  <p><a class="cta-btn" href="{rel}{page}">Contribute</a></p>
</aside>""".format(rel=rel, page=SUPPORT_PAGE)


# ---------------------------------------------------------------------------
# Support page
# ---------------------------------------------------------------------------

FREQ_LABELS = [
    ("once",    "One time",  ""),
    ("monthly", "Monthly",   "per month"),
    ("annual",  "Annually",  "per year"),
]


def render_amount_panel(key, tiers, custom, per_label, active):
    buttons = []
    for tier in tiers or []:
        label = htmlmod.escape(str(tier.get("label", "")))
        url   = (tier.get("url") or "").strip()
        per   = ('<span class="per">{}</span>'.format(per_label)) if per_label else ""
        featured = tier.get("featured") is True
        cls = "amount-btn featured" if featured else "amount-btn"
        tag = '<span class="suggested">Suggested</span>' if featured else ""
        if url:
            buttons.append('<a class="{c}" href="{u}" rel="noopener">{t}{l}{p}</a>'
                           .format(c=cls, u=htmlmod.escape(url, quote=True),
                                   t=tag, l=label, p=per))
        else:
            buttons.append('<span class="amount-btn disabled">{l}{p}</span>'
                           .format(l=label, p=per))

    # The custom-amount Stripe link is one-time only (Stripe cannot do
    # custom recurring), but it must be VISIBLE on every panel so nobody
    # concludes there is no choose-your-own-amount option.
    custom_html = ""
    curl = ((custom or {}).get("url") or "").strip()
    clabel = htmlmod.escape(str((custom or {}).get("label") or "Other amount"))
    if curl:
        suffix = "" if key == "once" else " (one-time)"
        custom_html = ('<a class="amount-custom" href="{u}" rel="noopener">{l}{s} &rsaquo;</a>'
                       .format(u=htmlmod.escape(curl, quote=True), l=clabel, s=suffix))

    if key == "once":
        note = ""
    else:
        note = ('<p class="support-note">Renews automatically. Cancel any time.</p>')

    return """<div class="amount-panel{act}" id="panel-{k}">
  <div class="amount-grid">
    {b}
  </div>
  {c}
  {n}
</div>""".format(act=(" active" if active else ""), k=key,
                 b="\n    ".join(buttons), c=custom_html, n=note)


def build_support_page(cfg, output_path):
    default_freq = cfg.get("default_frequency", "annual")

    toggle = []
    panels = []
    for key, label, per in FREQ_LABELS:
        active = (key == default_freq)
        toggle.append('<button class="freq-btn{act}" onclick="showFreq(this, \'{k}\')">{l}</button>'
                      .format(act=(" active" if active else ""), k=key, l=label))
        panels.append(render_amount_panel(key, cfg.get(key), cfg.get("custom"), per, active))

    warning = ""
    if cfg.get("configured_count", 0) == 0:
        warning = ('<div class="support-warning"><strong>Not yet live.</strong> '
                   'Payment links have not been configured in <code>support_config.json</code>, '
                   'so the amounts below are placeholders and are not clickable. '
                   'This page is here for layout and copy review only.</div>')
    elif cfg.get("mode") == "test":
        warning = ('<div class="support-warning"><strong>Stripe test mode.</strong> '
                   'These buttons use test payment links. No real money moves. '
                   'Set <code>"mode": "live"</code> in <code>support_config.json</code> before launch.'
                   '</div>')

    portal = (cfg.get("portal_url") or "").strip()
    if portal:
        cancel_answer = ('Use the <a href="{}" rel="noopener">contribution management portal</a> '
                         'to update your card or cancel at any time. You can also email '
                         '<a href="mailto:{e}">{e}</a>.'.format(
                             htmlmod.escape(portal, quote=True), e=CONTACT_EMAIL))
    else:
        cancel_answer = ('Email <a href="mailto:{e}">{e}</a> and it will be cancelled. '
                         'A self-service portal is coming.'.format(e=CONTACT_EMAIL))

    body = """<main class="container support-page" data-pagefind-ignore>
  <nav class="breadcrumb"><a href="index.html">Home</a> &rsaquo; Support</nav>

  <h1>Support this project</h1>
  <p class="lede">Riverhead Town meetings are public. Finding out what actually happened
  at one should not require watching three hours of video.</p>

  <h2>Choose an amount</h2>

  <div class="support-box">
    {warning}
    <div class="freq-toggle">
      {toggle}
    </div>
    {panels}
  </div>

  <h2>Why contribute?</h2>

  <p>This site transcribes every Riverhead Town Board, Planning Board, Zoning Board and
  related public meeting, makes the full text searchable, and adds a plain-language
  summary at the top of each one. It is free, it has no ads, and nothing is behind a
  paywall. That is not going to change.</p>

  <p>It takes money and steady work to run. Every meeting gets machine-transcribed and
  then summarized, the summaries are generated through a paid API that bills per meeting,
  there is a domain to renew, and someone has to keep all of it running every night.
  If this is useful to you, chipping in covers the costs and supports the work.</p>

  <h2>Questions</h2>
  <dl class="support-faq">
    <dt>Is my contribution tax-deductible?</dt>
    <dd>No. This project is not a registered nonprofit and has no 501(c)(3) status,
    so contributions are not tax-deductible and no deduction receipt can be issued.
    Please contribute only if you want to support the work itself.</dd>

    <dt>What does the money actually pay for?</dt>
    <dd>Direct costs first: per-meeting AI summary generation, the riverheadtranscripts.org
    domain, and the compute that turns meeting video into searchable text. Beyond that,
    contributions support the time it takes to build, maintain, and improve the site.
    This is an independent project run by one person, not a nonprofit.</dd>

    <dt>Does contributing get me any influence over what is published?</dt>
    <dd>No. Every meeting posted by the Town gets transcribed the same way regardless of
    who has contributed. Contributors get no say in what is transcribed, summarized, or
    left out, and no contributor list is published.</dd>

    <dt>How do I change or cancel a recurring contribution?</dt>
    <dd>{cancel}</dd>

    <dt>Is my card information safe?</dt>
    <dd>Payments are processed entirely by Stripe on Stripe's own pages. Card numbers
    never touch this site, and this site stores no personal information about you.</dd>

    <dt>I would rather help in another way.</dt>
    <dd>Corrections, missing meetings, and bug reports are genuinely valuable. Email
    <a href="mailto:{email}">{email}</a>.</dd>
  </dl>

  <p class="support-note">Transcripts are machine-generated and are not an official record
  of any Town proceeding. Contributions do not change that.</p>
</main>""".format(warning=warning,
                  toggle="\n      ".join(toggle),
                  panels="\n    ".join(panels),
                  cancel=cancel_answer,
                  email=CONTACT_EMAIL)

    html = """{head}
<body>
{header}
{body}
{footer}
{js}
</body>
</html>""".format(head=html_head("Support", depth=0),
                  header=html_header(depth=0, show_support=False),
                  body=body,
                  footer=html_footer(depth=0),
                  js=SUPPORT_JS)

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

# ---------------------------------------------------------------------------
# Post-payment thank-you page
# ---------------------------------------------------------------------------
# Every live Stripe Payment Link redirects here after a successful payment
# ("After payment" setting on each link), so supporters land back on the site
# instead of on Stripe's dead-end confirmation screen. This page must always
# be generated when the links are live, or the redirect 404s.

def build_thanks_page(portal_url, output_path):
    portal_line = ""
    if (portal_url or "").strip():
        portal_line = ('<p>To update your card or cancel a recurring contribution at any '
                       'time, use the <a href="{u}" rel="noopener">contribution management '
                       'portal</a>.</p>'
                       .format(u=htmlmod.escape(portal_url.strip(), quote=True)))

    body = """<main class="container text-page" data-pagefind-ignore>
  <h1>Thank you</h1>
  <p class="lede">Your contribution goes directly toward keeping every Riverhead Town
  meeting transcribed, searchable, and free for everyone.</p>

  <p>A receipt from Stripe is on its way to your email.</p>
  {portal}
  <p>Questions about your contribution? Email
  <a href="mailto:{email}">{email}</a>.</p>

  <p style="margin-top:2rem;"><a class="toggle-btn" href="index.html">&lsaquo; Back to the
  transcripts</a></p>
</main>""".format(portal=portal_line, email=CONTACT_EMAIL)

    html = """{head}
<body>
{header}
{body}
{footer}
</body>
</html>""".format(head=html_head("Thank you", depth=0),
                  header=html_header(depth=0, show_support=False),
                  body=body,
                  footer=html_footer(depth=0))

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

# ---------------------------------------------------------------------------
# Privacy policy page
# ---------------------------------------------------------------------------

def build_privacy_page(output_path):
    # The policy has to describe the site that actually shipped, so this text is
    # driven by CF_ANALYTICS_TOKEN rather than hand-maintained. Turning the token
    # off restores the original "no analytics at all" wording automatically.
    if analytics_on():
        updated = PRIVACY_UPDATED_ANALYTICS
        lede = ("Short version: this site does not identify you. There are no accounts, "
                "no cookies, and no advertising. Page views are counted in aggregate, "
                "without cookies and without anything that singles out a person.")
        collects = ("Nothing that identifies you. Reading transcripts on "
                    "riverheadtranscripts.org requires no account and sets no cookies. "
                    "There are no advertising networks and no cross-site tracking pixels "
                    "anywhere on the site. The one measurement script is described under "
                    "Analytics below.")
        analytics_section = """
  <h2>Analytics</h2>
  <p>This site uses Cloudflare Web Analytics to count page views. It is here for one
  reason: GitHub Pages gives me no server logs at all, so without it I have no way to
  know whether anyone is using the site.</p>

  <p>It sets no cookies, stores nothing on your device, and does not fingerprint your
  browser. It does not follow you to other sites, does not link one visit to another,
  and does not build a profile of you. What I see is aggregate counts: how many times a
  page was loaded, the general region a visit came from, and which site linked here. I
  cannot tell who you are and I cannot pick an individual visitor out of it. Cloudflare
  processes this as my service provider under its own privacy policy.</p>
"""
        no_ads_bullet = ("<li>I do not run ads or embed advertising code, and I use no "
                         "cross-site tracking beyond the cookieless page counting "
                         "described above.</li>")
    else:
        updated = PRIVACY_UPDATED_BASE
        lede = ("Short version: this site does not track you. There are no accounts, "
                "no cookies set by this site, no analytics, and no advertising.")
        collects = ("Nothing. Reading transcripts on riverheadtranscripts.org requires no "
                    "account and sets no cookies. There are no analytics scripts, no "
                    "tracking pixels, and no advertising networks anywhere on the site.")
        analytics_section = ""
        no_ads_bullet = ("<li>I do not run ads or embed third-party advertising or "
                         "tracking code.</li>")

    body = """<main class="container text-page" data-pagefind-ignore>
  <nav class="breadcrumb"><a href="index.html">Home</a> &rsaquo; Privacy</nav>

  <h1>Privacy policy</h1>
  <p class="updated">Last updated: {updated}</p>

  <p class="lede">{lede}</p>

  <h2>What this site collects</h2>
  <p>{collects}</p>

  <h2>Hosting</h2>
  <p>The site is served by GitHub Pages. Like any web host, GitHub receives standard
  request information, including your IP address and browser type, in order to deliver
  the page. That data is handled under GitHub's privacy statement, not by me. I have no
  access to server logs and cannot see who visits.</p>
{analytics}
  <h2>Search</h2>
  <p>Search runs entirely inside your browser using Pagefind. The search index is
  downloaded to your device and queried locally. What you type into the search box is
  never transmitted anywhere and is not logged by me or by anyone else.</p>

  <h2>Meeting video and documents</h2>
  <p>Meeting video is streamed directly from the Town of Riverhead's CivicClerk portal
  and its content delivery network at <code>cpmedia.azureedge.net</code>. Agenda and
  minutes links point to <code>riverheadny.portal.civicclerk.com</code>. If you play a
  video or open one of those documents, those providers receive your IP address under
  their own privacy practices. They are the Town's vendors, not mine.</p>

  <h2>Contributions</h2>
  <p>Payments are processed entirely by Stripe on Stripe's own pages. Card numbers never
  touch this site and I never see them. Stripe shows me a contributor's name, email
  address, amount, and date, which I use only to process the contribution and to reply if
  you contact me. I do not sell or share it, and there is no mailing list. Stripe's
  handling of your information is governed by Stripe's privacy policy.</p>

  <h2>Email</h2>
  <p>If you email <a href="mailto:{email}">{email}</a>, I keep that message so I can
  respond and track corrections. That mailbox is Gmail, so Google's privacy policy
  applies to it.</p>

  <h2>AI summaries</h2>
  <p>The plain-language summary at the top of each meeting page is generated by sending
  the meeting transcript and the Town's published agenda to Anthropic's API. Only public
  government records are sent. No information about site visitors is involved at any
  point.</p>

  <h2>Names in transcripts</h2>
  <p>These transcripts are records of public government meetings and include the names
  and statements of people who spoke on the record at them. That is public information.
  Transcripts are machine-generated and can contain errors, including misheard names. If
  you find something transcribed incorrectly, email me and I will correct it.</p>

  <h2>What I do not do</h2>
  <ul>
    <li>I do not sell, rent, or share personal information.</li>
    {no_ads}
    <li>I do not build profiles of visitors or maintain a mailing list.</li>
    <li>I do not use cookies to identify or follow you.</li>
  </ul>

  <h2>Changes</h2>
  <p>If this policy changes in a meaningful way, the date at the top of this page will be
  updated.</p>

  <h2>Contact</h2>
  <p>Questions about any of this: <a href="mailto:{email}">{email}</a></p>
</main>""".format(updated=updated, email=CONTACT_EMAIL, lede=lede,
                  collects=collects, analytics=analytics_section,
                  no_ads=no_ads_bullet)

    html = """{head}
<body>
{header}
{body}
{footer}
</body>
</html>""".format(head=html_head("Privacy policy", depth=0),
                  header=html_header(depth=0),
                  body=body,
                  footer=html_footer(depth=0))

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

# ---------------------------------------------------------------------------
# Meeting page
# ---------------------------------------------------------------------------

def render_summary(summary):
    """Render the stored AI summary dict into an HTML block, or '' if none/unusable.

    Expected shape:
      {"status":"ok","tldr":"...","sections":[{"heading":"...","items":["...",...]}, ...]}
    Any other status (too_short, error, missing) renders nothing.
    Model-generated text is HTML-escaped.
    """
    if not isinstance(summary, dict) or summary.get("status") != "ok":
        return ""
    tldr = (summary.get("tldr") or "").strip()
    sections = summary.get("sections") or []
    parts = []
    if tldr:
        parts.append('<p class="tldr">{}</p>'.format(htmlmod.escape(tldr)))
    for sec in sections:
        if not isinstance(sec, dict):
            continue
        heading = (sec.get("heading") or "").strip()
        items = [str(i).strip() for i in (sec.get("items") or []) if str(i).strip()]
        if not heading or not items:
            continue
        lis = "".join("<li>{}</li>".format(htmlmod.escape(i)) for i in items)
        parts.append("<h3>{}</h3>\n      <ul>{}</ul>".format(htmlmod.escape(heading), lis))
    if not parts:
        return ""
    body = "\n      ".join(parts)
    return """<section class="transcript-summary" id="summary" data-pagefind-ignore>
    <div class="summary-head">
      <h2>Summary <span class="ai-tag">AI</span></h2>
      <button class="summary-toggle" onclick="toggleSummary(this)">Hide</button>
    </div>
    <div id="summary-body">
      {body}
      <p class="summary-disclaimer">Auto-generated from an unofficial, machine-made transcript.
      It may misstate names, figures, or votes. Verify against the agenda and the full transcript below.</p>
    </div>
  </section>""".format(body=body)


OUTCOME_LABELS = {
    "adopted": "Adopted", "defeated": "Defeated", "tabled": "Tabled",
    "amended": "Amended", "withdrawn": "Withdrawn", "held": "Hearing held",
    "unknown": "Outcome unclear",
}

def render_decision_item(it, video_url="", anchor=None):
    """One decision as HTML. Shared by the meeting page and the global page."""
    outcome = it.get("outcome", "unknown")
    badge = '<span class="outcome-badge outcome-{o}">{l}</span>'.format(
        o=htmlmod.escape(outcome), l=OUTCOME_LABELS.get(outcome, outcome))
    num = ('<span class="decision-num">{}</span>'.format(htmlmod.escape(it["number"]))
           if it.get("number") else "")
    title = htmlmod.escape(it.get("title") or "(untitled action)")

    votes = it.get("votes") or []
    if votes:
        yes = sum(1 for v in votes if v["vote"] == "yes")
        no  = sum(1 for v in votes if v["vote"] == "no")
        other = len(votes) - yes - no
        tally = "{}&ndash;{}".format(yes, no)
        if other:
            # Without this, a 3-1 with one abstention reads as a 4-member board.
            tally += " ({} abstained)".format(other) if other == 1 else \
                     " ({} did not vote)".format(other)
        if it.get("unanimous"):
            tally += " unanimous"
        cls = {"yes": "v-yes", "no": "v-no"}
        detail = ", ".join(
            '<span class="{c}">{m}: {v}</span>'.format(
                c=cls.get(v["vote"], "v-other"),
                m=htmlmod.escape(v["member"].split()[-1]),
                v=htmlmod.escape(v["vote"]))
            for v in votes)
        votes_html = ('<div class="decision-votes">Roll call {t} &mdash; {d}</div>'
                      .format(t=tally, d=detail))
    else:
        votes_html = ('<div class="decision-votes"><span class="no-rollcall">'
                      'Voice vote or roll call not clearly audible in the recording.'
                      '</span></div>')

    jump = ""
    ts = it.get("timestamp_s")
    if ts is not None and video_url:
        jump = ('<a class="decision-jump" href="#" onclick="seekTo({s}); return false;">'
                'watch vote ({t})</a>'.format(s=int(ts), t=format_timestamp(ts)))
    elif video_url:
        # No timestamp matched. Send the reader to the recording anyway rather
        # than dead-ending them on the item they are most likely checking.
        jump = ('<a class="decision-jump decision-jump-untimed" href="#video">'
                'timestamp unavailable &mdash; watch full meeting</a>')

    aid = ' id="{}"'.format(anchor) if anchor else ""
    return ('<div class="decision-item"{aid} data-outcome="{o}">'
            '{badge}{num}{title}{jump}{votes}</div>'.format(
                aid=aid, o=htmlmod.escape(outcome), badge=badge, num=num,
                title=title, jump=jump, votes=votes_html))

def decision_anchor(it, i):
    n = re.sub(r"[^\w-]", "", it.get("number") or "")
    return "decision-{}".format(n or i)

def render_decisions(record, video_url=""):
    """Render the stored decisions dict into an HTML block, or '' if none.

    Expected shape (written by riverhead_extract_votes.py):
      {"status":"ok","items":[{kind,number,title,outcome,votes,unanimous,
                               confidence,timestamp_s}, ...]}
    """
    d = record.get("decisions")
    if not isinstance(d, dict) or d.get("status") != "ok":
        return ""
    items = d.get("items") or []
    if not items:
        return ""
    rows = "\n      ".join(
        render_decision_item(it, video_url, anchor=decision_anchor(it, i))
        for i, it in enumerate(items))
    grounded = d.get("grounded_in")
    grounding_note = (" Resolution numbers and titles are grounded in the official {}."
                      .format(str(grounded).lower()) if grounded else "")
    return """<section class="transcript-decisions" id="decisions" data-pagefind-ignore>
    <div class="summary-head">
      <h2>Decisions &amp; Votes <span class="ai-tag">AI</span></h2>
      <button class="summary-toggle" onclick="toggleDecisions(this)">Hide</button>
    </div>
    <div id="decisions-body">
      {rows}
      <p class="summary-disclaimer">Extracted automatically from an unofficial,
      machine-made transcript; votes are recorded only where the roll call is
      audible.{gn} May contain errors &mdash; verify against the full transcript
      below and the town&rsquo;s official record.</p>
    </div>
  </section>""".format(rows=rows, gn=grounding_note)


def build_meeting_page(record, output_path, depth=2):
    meta      = record.get("meta", {})
    segments  = record.get("segments", [])
    rel       = "../" * depth
    summary_html = render_summary(record.get("summary"))
    decisions_html = render_decisions(record, meta.get("video_url", ""))
    quality_level, quality_reason = transcript_quality(record)

    event_id    = str(meta.get("event_id", ""))
    event_date  = meta.get("event_date", "")
    category    = meta.get("category") or "Uncategorized"
    title       = (meta.get("title") or category).strip()
    duration    = meta.get("duration_minutes", "")
    agenda_url  = meta.get("agenda_pdf_url", "")
    minutes_url = meta.get("minutes_pdf_url", "")
    video_url   = meta.get("video_url", "")
    display_date = format_date_display(event_date)
    page_title   = "{} — {}".format(display_date, category)

    # Meta row
    meta_parts = []
    if duration:
        meta_parts.append("{} min".format(duration))
    if event_id:
        meta_parts.append('<a href="{}/event/{}" target="_blank">CivicClerk page</a>'.format(
                          PORTAL_BASE, event_id))

    # Doc links
    links = []
    if agenda_url:
        links.append('<a href="{}" target="_blank">Agenda PDF</a>'.format(agenda_url))
    if minutes_url:
        links.append('<a href="{}" target="_blank">Minutes PDF</a>'.format(minutes_url))
    doc_links = '<div class="doc-links">{}</div>'.format("".join(links)) if links else ""

    # Video player
    video_block = """<div class="video-wrap" id="video">
  <video id="meeting-video" controls preload="metadata">
    <source src="{}" type="video/mp4">
  </video>
</div>""".format(video_url) if video_url else ""

    # Timestamped segments
    segs_html = []
    prev_seg_text = None
    for seg in segments:
        text = sanitize_segment_text((seg.get("text") or "").strip())
        if not text or text in (".", ".."):
            continue
        # Collapse consecutive identical segments (cross-segment hallucination loops)
        if text.lower() == prev_seg_text:
            continue
        prev_seg_text = text.lower()
        start  = seg.get("start", 0)
        ts_str = format_timestamp(start)
        try:
            secs = int(float(start))
        except Exception:
            secs = 0
        ts_html = (
            '<a href="#" onclick="seekTo({s}); return false;" title="Jump to {ts}">'
            '{ts}</a>'.format(s=secs, ts=ts_str)
        ) if video_url else ts_str
        if text == GAP_MARKER:
            text_html = '<em class="gap-marker">{}</em>'.format(GAP_MARKER)
        else:
            text_html = text
        segs_html.append(
            '<div class="segment">'
            '<span class="ts">{}</span>'
            '<span class="text">{}</span>'
            '</div>'.format(ts_html, text_html))

    # Timestamped section (below video, above readable)
    timestamped_section = """<div class="transcript-timestamped">
  <button class="toggle-btn" onclick="toggleTimestamps(this)">Show timestamped view</button>
  <div id="timestamped-view">
    <h2>Timestamped Transcript</h2>
    <p style="font-size:.85rem;color:#888;margin-bottom:1rem;">
      Click any timestamp to jump the video to that moment.
    </p>
    {}
  </div>
</div>""".format("\n    ".join(segs_html) if segs_html else "<p><em>No segments.</em></p>")

    # Readable paragraphs (below timestamped)
    paragraphs = group_into_paragraphs(segments)
    para_html  = []
    for para in paragraphs:
        parts = []
        for s in para:
            t = sanitize_segment_text((s.get("text") or "").strip())
            if t:
                parts.append(t)
        if not parts:
            continue
        # If the paragraph is entirely a gap marker, render it as one italic line
        if all(p == GAP_MARKER for p in parts):
            para_html.append('<p><em class="gap-marker">{}</em></p>'.format(GAP_MARKER))
        else:
            # Inline any gap markers within otherwise good text
            rendered = " ".join(
                '<em class="gap-marker">{}</em>'.format(p) if p == GAP_MARKER else p
                for p in parts
            )
            para_html.append("<p>{}</p>".format(rendered))

    readable_section = """<div class="transcript-readable" id="transcript">
  <h2>Full Transcript</h2>
  {}
</div>""".format("\n  ".join(para_html) if para_html else "<p><em>No transcript available.</em></p>")

    # The recording does not cover the meeting, so whatever text exists cannot be
    # its record. Withhold it rather than present a fragment as the whole.
    if quality_level == "incomplete":
        summary_html        = ""
        decisions_html      = ""
        timestamped_section = ""
        readable_section    = """<div class="transcript-missing" data-pagefind-ignore>
  <h2>Transcript unavailable</h2>
  <p><strong>The published recording for this meeting is incomplete</strong>, so no
  transcript of it can be shown.</p>
  <p class="why">{reason} Presenting a fragment as though it were the full meeting
  would be misleading, so the partial text is withheld. Nothing has been edited or
  censored. The agenda linked above remains the best available record until a
  complete recording can be transcribed.</p>
</div>""".format(reason=htmlmod.escape(quality_reason))

    # Full-length recording with very little speech. Often legitimate, so show
    # the text, but make sure nobody reads three sentences as the whole meeting.
    elif quality_level == "thin":
        thin_notice = """<div class="transcript-missing" data-pagefind-ignore>
  <h2>Very little speech in this recording</h2>
  <p>{reason} That is normal when a meeting opens and moves quickly into executive
  session, which is not recorded, or when the room is silent for long stretches. It
  can also mean the audio was poor.</p>
  <p class="why">Everything that could be transcribed appears below. Nothing has
  been omitted or edited.</p>
</div>""".format(reason=htmlmod.escape(quality_reason))
        readable_section = thin_notice + "\n" + readable_section

    html = """{head}
<body>
{header}
<main class="container" data-pagefind-body
      data-pagefind-meta="date:{date}, category:{cat}">
  <nav class="breadcrumb"><a href="{rel}index.html">Home</a> &rsaquo; {cat}</nav>
  <div class="meeting-meta">
    <h1 data-pagefind-meta="title:{pt}">{dd} &mdash; {cat}</h1>
    {tl}
    {mr}
    {dl}
  </div>
  {summary}
  {decisions}
  {video}
  {timestamped}
  {cta}
  {readable}
</main>
{footer}
{js}
</body>
</html>""".format(
        head       = html_head(page_title, depth=depth),
        header     = html_header(depth=depth),
        cta        = support_cta(depth=depth),
        footer     = html_footer(depth=depth),
        js         = ((VIDEO_JS if video_url else "")
                      + (SUMMARY_JS if summary_html else "")
                      + (DECISIONS_JS if decisions_html else "")),
        summary    = summary_html,
        decisions  = decisions_html,
        rel        = rel,
        date       = event_date,
        cat        = category,
        pt         = page_title,
        dd         = display_date,
        tl         = '<div class="meta-row"><strong>{}</strong></div>'.format(title)
                     if title and title != category else "",
        mr         = '<div class="meta-row">{}</div>'.format(
                     " &nbsp;·&nbsp; ".join(meta_parts)) if meta_parts else "",
        dl         = doc_links,
        video      = video_block,
        timestamped= timestamped_section,
        readable   = readable_section,
    )

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

# ---------------------------------------------------------------------------
# Global Decisions & Votes page
# ---------------------------------------------------------------------------

DECISIONS_PAGE_JS = """
<script>
function filterDecisions() {
  var q = document.getElementById('dq').value.toLowerCase();
  var o = document.getElementById('do').value;
  var shown = 0;
  document.querySelectorAll('.meeting-group').forEach(function(g) {
    var any = false;
    g.querySelectorAll('.decision-item').forEach(function(it) {
      var okText = !q || it.textContent.toLowerCase().indexOf(q) !== -1;
      var okOutcome = !o || it.getAttribute('data-outcome') === o;
      var ok = okText && okOutcome;
      it.classList.toggle('hidden', !ok);
      if (ok) { any = true; shown++; }
    });
    g.classList.toggle('hidden', !any);
  });
  document.getElementById('dcount').textContent =
    shown + ' decision' + (shown === 1 ? '' : 's') + ' shown';
}
</script>"""

def build_decisions_page(all_records, output_path, depth=1):
    """One browsable page of every extracted decision, newest meeting first."""
    rel = "../" * depth
    withdec = [r for r in all_records
               if isinstance(r.get("decisions"), dict)
               and r["decisions"].get("status") == "ok"
               and (r["decisions"].get("items") or [])]
    withdec.sort(key=lambda r: r["meta"].get("event_date", ""), reverse=True)

    total = 0
    groups = []
    for r in withdec:
        meta  = r["meta"]
        cat   = meta.get("category") or "Uncategorized"
        date  = meta.get("event_date", "")
        eid   = str(meta.get("event_id", ""))
        href  = "{}meetings/{}/{}_{}.html".format(rel, slugify(cat), date, eid)
        items = r["decisions"]["items"]
        total += len(items)
        # No video element on this page: each item links to its anchor on the
        # meeting page, where the seek link lives.
        link_rows = []
        for i, it in enumerate(items):
            row = render_decision_item(it, video_url="")
            row = row.replace(
                "</div></div>",
                '<a class="decision-jump" href="{h}#{a}">meeting page &rsaquo;</a>'
                "</div></div>".format(h=href, a=decision_anchor(it, i)))
            link_rows.append(row)
        rows = "\n    ".join(link_rows)
        groups.append(
            '<section class="meeting-group">'
            '<h2><a href="{href}">{dd} &mdash; {cat}</a></h2>'
            '{rows}</section>'.format(
                href=href, dd=format_date_display(date),
                cat=htmlmod.escape(cat), rows=rows))

    body = ("\n".join(groups) if groups else
            "<p><em>No decisions extracted yet. They appear here as Town Board "
            "meetings are processed.</em></p>")

    html = """{head}
<body>
{header}
<main class="container decisions-page" data-pagefind-ignore>
  <nav class="breadcrumb"><a href="{rel}index.html">Home</a> &rsaquo; Decisions &amp; Votes</nav>
  <h1 style="font-size:1.5rem;margin-bottom:.4rem;">Decisions &amp; Votes</h1>
  <p style="color:#555;margin-bottom:1.2rem;">Every resolution, motion, and public
  hearing action extracted from Town Board meeting transcripts, with roll-call
  votes where the roll call is audible in the recording. Auto-generated from
  unofficial transcripts; grounded in the town&rsquo;s published agendas and
  minutes where available. Verify anything important against the
  <a href="https://riverheadny.portal.civicclerk.com/" target="_blank" rel="noopener">official record</a>.</p>
  <div class="filters">
    <input id="dq" type="text" placeholder="Filter by keyword, address, resolution number..."
           oninput="filterDecisions()">
    <select id="do" onchange="filterDecisions()">
      <option value="">All outcomes</option>
      <option value="adopted">Adopted</option>
      <option value="defeated">Defeated</option>
      <option value="tabled">Tabled</option>
      <option value="amended">Amended</option>
      <option value="withdrawn">Withdrawn</option>
      <option value="held">Hearing held</option>
      <option value="unknown">Outcome unclear</option>
    </select>
  </div>
  <p class="count-line" id="dcount">{total} decisions shown</p>
  {body}
</main>
{footer}
{js}
</body>
</html>""".format(
        head=html_head("Decisions & Votes", depth=depth),
        header=html_header(depth=depth),
        footer=html_footer(depth=depth),
        js=DECISIONS_PAGE_JS,
        rel=rel, total=total, body=body)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    return len(withdec), total

# ---------------------------------------------------------------------------
# Index page
# ---------------------------------------------------------------------------

def build_index(all_records, output_path):
    by_category = defaultdict(list)
    for r in all_records:
        cat = r["meta"].get("category") or "Uncategorized"
        by_category[cat].append(r)
    for cat in by_category:
        by_category[cat].sort(key=lambda r: r["meta"].get("event_date",""), reverse=True)

    PRIORITY = ["Town Board","Town Board Work Session","Planning Board",
                "Zoning Board of Appeals","Architectural Review Board",
                "Community Development Agency","Conservation Advisory Council",
                "Pre-Submission Conferences"]
    ordered = [c for c in PRIORITY if c in by_category]
    ordered += sorted(c for c in by_category if c not in PRIORITY)

    INITIAL_SHOW = 10

    sections = []
    for idx, cat in enumerate(ordered):
        records = by_category[cat]
        items = []
        for r in records:
            meta    = r["meta"]
            date    = meta.get("event_date","")
            display = format_date_display(date)
            title   = (meta.get("title") or cat).strip()
            eid     = str(meta.get("event_id",""))
            href    = "meetings/{}/{}_{}.html".format(slugify(cat), date, eid)
            badges  = ""
            agenda_url  = meta.get("agenda_pdf_url")
            minutes_url = meta.get("minutes_pdf_url")
            if agenda_url:
                badges += ('<a class="badge badge-link" href="{}" target="_blank" '
                           'rel="noopener" title="Open agenda PDF">Agenda</a>').format(agenda_url)
            if minutes_url:
                badges += ('<a class="badge badge-link" href="{}" target="_blank" '
                           'rel="noopener" title="Open minutes PDF">Minutes</a>').format(minutes_url)
            # Section deep links, rendered only when that meeting has the
            # section. The title link is the transcript, so no transcript badge.
            summary = r.get("summary")
            if isinstance(summary, dict) and summary.get("status") == "ok":
                badges += ('<a class="badge badge-link" href="{}#summary" '
                           'title="Jump to the AI summary">Summary</a>').format(href)
            decisions = r.get("decisions")
            if (isinstance(decisions, dict) and decisions.get("status") == "ok"
                    and (decisions.get("items") or [])):
                badges += ('<a class="badge badge-link" href="{}#decisions" '
                           'title="Jump to decisions and roll-call votes">Votes</a>').format(href)
            if meta.get("video_url"):
                badges += ('<a class="badge badge-link" href="{}#video" '
                           'title="Jump to the meeting video">Video</a>').format(href)
            level, why = transcript_quality(r)
            if level == "incomplete":
                badges += ('<span class="badge badge-warn" title="{}">'
                           'Transcript unavailable</span>'.format(htmlmod.escape(why)))
            elif level == "thin":
                badges += ('<span class="badge badge-warn" title="{}">'
                           'Little speech</span>'.format(htmlmod.escape(why)))
            items.append(
                '<li><a href="{}">{}</a>'
                '<span class="date">{}{}</span></li>'.format(
                    href, title, display, badges))

        visible = "\n".join(items[:INITIAL_SHOW])
        hidden_count = len(items) - INITIAL_SHOW

        if hidden_count > 0:
            hidden = "\n".join(items[INITIAL_SHOW:])
            expand_id = "expand-{}".format(idx)
            list_html = (
                '<ul class="meeting-list">{visible}'
                '<span id="{eid}" class="meeting-list-hidden">{hidden}</span>'
                '</ul>'
                '<button class="expand-btn" onclick="toggleCategory(this, \'{eid}\')">'
                '<span class="chevron">&#9660;</span> Show {n} more'
                '</button>'.format(
                    visible=visible, hidden=hidden, eid=expand_id, n=hidden_count))
        else:
            list_html = '<ul class="meeting-list">{}</ul>'.format(visible)

        sections.append(
            '<section class="category-section">'
            '<h2>{} <small style="font-weight:normal;font-size:.8em;color:#aaa;">({n})</small></h2>'
            '{list_html}'
            '</section>'.format(cat, n=len(records), list_html=list_html))

    html = """{head}
<body>
{header}
<main class="container">
  <div id="search"></div>
  <script>
    window.addEventListener('DOMContentLoaded', function() {{
      new PagefindUI({{ element: '#search', showSubResults: true,
        showImages: false, excerptLength: 40, resetStyles: false }});
    }});
  </script>
  <script>
    function toggleCategory(btn, id) {{
      var el = document.getElementById(id);
      var chevron = btn.querySelector('.chevron');
      var open = el.classList.toggle('open');
      chevron.classList.toggle('up', open);
      var n = btn.getAttribute('data-count') || btn.textContent.match(/\\d+/)[0];
      if (!btn.getAttribute('data-count')) btn.setAttribute('data-count', n);
      btn.innerHTML = open
        ? '<span class="chevron up">&#9660;</span> Show fewer'
        : '<span class="chevron">&#9660;</span> Show ' + n + ' more';
    }}
  </script>
  <h2 style="font-size:.9rem;text-transform:uppercase;letter-spacing:.08em;
             color:#aaa;margin:2rem 0 1.5rem;">
    All Meetings &mdash; {total} transcripts
  </h2>
  {sections}
</main>
{footer}
</body>
</html>""".format(
        head=html_head(SITE_TITLE, depth=0), header=html_header(depth=0),
        footer=html_footer(depth=0), total=len(all_records),
        sections="\n".join(sections))

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global SUPPORT_ENABLED

    print("=" * 60)
    print("Riverhead Site Builder")
    print("=" * 60)

    support_cfg = load_support_config()
    SUPPORT_ENABLED = support_cfg is not None
    if SUPPORT_ENABLED:
        print("Support page: enabled ({} mode, {} link(s) configured).".format(
            support_cfg.get("mode"), support_cfg.get("configured_count", 0)))
    else:
        print("Support page: disabled (no usable {}).".format(SUPPORT_CONFIG_FILE))

    records = load_all_transcripts(TRANSCRIPTS_DIR)
    print("Loaded {} transcripts.".format(len(records)))
    if not records:
        print("No transcripts found.")
        return

    # Preserve _pagefind/ across rebuilds
    pagefind_dir = os.path.join(OUTPUT_DIR, "_pagefind")
    backup = None
    if os.path.exists(pagefind_dir):
        import tempfile
        backup = tempfile.mkdtemp()
        shutil.move(pagefind_dir, os.path.join(backup, "_pagefind"))

    if os.path.exists(OUTPUT_DIR):
        shutil.rmtree(OUTPUT_DIR)
    os.makedirs(OUTPUT_DIR)

    if backup:
        shutil.move(os.path.join(backup, "_pagefind"), pagefind_dir)
        shutil.rmtree(backup)

    assets_dir = os.path.join(OUTPUT_DIR, "assets")
    os.makedirs(assets_dir, exist_ok=True)
    with open(os.path.join(assets_dir, "style.css"), "w", encoding="utf-8") as f:
        f.write(CSS)
    open(os.path.join(OUTPUT_DIR, ".nojekyll"), "w").close()
    with open(os.path.join(OUTPUT_DIR, "CNAME"), "w") as f:
        f.write("riverheadtranscripts.org\n")

    print("Building meeting pages ...")
    built = 0
    incomplete_transcripts = []
    thin_transcripts       = []
    for record in records:
        level, reason = transcript_quality(record)
        if level:
            rmeta = record.get("meta", {})
            entry = (rmeta.get("event_date", "no-date"),
                     rmeta.get("category") or "Uncategorized",
                     str(rmeta.get("event_id", "unknown")),
                     reason)
            (incomplete_transcripts if level == "incomplete"
             else thin_transcripts).append(entry)
        meta     = record.get("meta", {})
        category = meta.get("category") or "uncategorized"
        date     = meta.get("event_date", "no-date")
        eid      = str(meta.get("event_id", "unknown"))
        out_path = os.path.join(OUTPUT_DIR, "meetings", slugify(category),
                                "{}_{}.html".format(date, eid))
        build_meeting_page(record, out_path, depth=2)
        built += 1
        if built % 25 == 0:
            print("  ... {}".format(built))
    print("  Built {} pages.".format(built))

    if incomplete_transcripts:
        print()
        print("!" * 60)
        print("INCOMPLETE RECORDING for {} meeting(s). Transcript WITHHELD:".format(
              len(incomplete_transcripts)))
        for date, cat, eid, reason in sorted(incomplete_transcripts):
            print("  {}  {}  (event {})".format(date, cat, eid))
            print("      {}".format(reason))
        print("Delete the transcript files for these and let the pipeline retry;")
        print("the source may since have published the full recording.")
        print("!" * 60)
        print()

    if thin_transcripts:
        print()
        print("-" * 60)
        print("Full-length but very little speech, {} meeting(s). Text SHOWN "
              "with a caveat:".format(len(thin_transcripts)))
        for date, cat, eid, reason in sorted(thin_transcripts):
            print("  {}  {}  (event {}): {}".format(date, cat, eid, reason))
        print("Usually legitimate (executive session, silence). No action needed.")
        print("-" * 60)
        print()

    print("Building index ...")
    build_index(records, os.path.join(OUTPUT_DIR, "index.html"))

    print("Building decisions page ...")
    n_meetings, n_items = build_decisions_page(
        records, os.path.join(OUTPUT_DIR, "decisions", "index.html"))
    print("  {} decisions across {} meetings.".format(n_items, n_meetings))

    if SUPPORT_ENABLED:
        print("Building support page ...")
        build_support_page(support_cfg, os.path.join(OUTPUT_DIR, SUPPORT_PAGE))

    # Always built, even with support disabled: the live Stripe links redirect
    # here after payment, and a raw link shared before launch must not 404.
    print("Building thanks page ...")
    build_thanks_page((support_cfg or {}).get("portal_url", ""),
                      os.path.join(OUTPUT_DIR, THANKS_PAGE))

    print("Building privacy page ...")
    build_privacy_page(os.path.join(OUTPUT_DIR, PRIVACY_PAGE))

    print()
    print("Done! Next:")
    print("  pagefind --site {}".format(OUTPUT_DIR))
    print("  cd docs && python3 -m http.server 8000")

if __name__ == "__main__":
    main()
