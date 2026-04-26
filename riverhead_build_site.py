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

def group_into_paragraphs(segments, pause=PARAGRAPH_PAUSE):
    paragraphs, current, prev_end = [], [], 0.0
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text or text in (".", "..", "..."):
            continue
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

.site-header { background: #1a3a5c; color: white; padding: 1.5rem 2rem; }
.site-header h1 { font-size: 1.5rem; font-weight: normal; letter-spacing: .02em; }
.site-header p { font-size: .9rem; opacity: .8; margin-top: .25rem; }
.site-header a { color: white; }

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

@media (max-width: 600px) {
  body { font-size: 16px; }
  .segment .ts { display: none; }
}
"""

# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

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
  <script src="{rel}_pagefind/pagefind-ui.js"></script>
</head>""".format(t=title, s=SITE_TITLE, rel=rel)

def html_header(depth=0):
    rel = "../" * depth
    return """<header class="site-header">
  <a href="{rel}index.html"><h1>{t}</h1></a>
  <p>{d}</p>
</header>""".format(rel=rel, t=SITE_TITLE, d=SITE_DESC)

def html_footer():
    return """<footer class="site-footer">
  <p>Transcripts generated by volunteers using OpenAI Whisper. Not an official record.
  Source videos: <a href="https://riverheadny.portal.civicclerk.com/">CivicClerk portal</a>.</p>
  <p>Questions, suggestions, or bugs? Email <a href="mailto:riverheadtranscripts@gmail.com">riverheadtranscripts@gmail.com</a></p>
</footer>"""

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

# ---------------------------------------------------------------------------
# Meeting page
# ---------------------------------------------------------------------------

def build_meeting_page(record, output_path, depth=2):
    meta      = record.get("meta", {})
    segments  = record.get("segments", [])
    rel       = "../" * depth

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
    video_block = """<div class="video-wrap">
  <video id="meeting-video" controls preload="metadata">
    <source src="{}" type="video/mp4">
  </video>
</div>""".format(video_url) if video_url else ""

    # Timestamped segments
    segs_html = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text or text in (".", ".."):
            continue
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
        segs_html.append(
            '<div class="segment">'
            '<span class="ts">{}</span>'
            '<span class="text">{}</span>'
            '</div>'.format(ts_html, text))

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
        text = " ".join((s.get("text") or "").strip() for s in para if (s.get("text") or "").strip())
        if text:
            para_html.append("<p>{}</p>".format(text))

    readable_section = """<div class="transcript-readable">
  <h2>Full Transcript</h2>
  {}
</div>""".format("\n  ".join(para_html) if para_html else "<p><em>No transcript available.</em></p>")

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
  {video}
  {timestamped}
  {readable}
</main>
{footer}
{js}
</body>
</html>""".format(
        head       = html_head(page_title, depth=depth),
        header     = html_header(depth=depth),
        footer     = html_footer(),
        js         = VIDEO_JS if video_url else "",
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

    sections = []
    for cat in ordered:
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
            if meta.get("agenda_pdf_url"):  badges += '<span class="badge">Agenda</span>'
            if meta.get("minutes_pdf_url"): badges += '<span class="badge">Minutes</span>'
            items.append(
                '<li><a href="{}">{}</a>'
                '<span class="date">{}{}</span></li>'.format(
                    href, title, display, badges))
        sections.append(
            '<section class="category-section">'
            '<h2>{} <small style="font-weight:normal;font-size:.8em;color:#aaa;">({n})</small></h2>'
            '<ul class="meeting-list">{items}</ul>'
            '</section>'.format(cat, n=len(records), items="\n".join(items)))

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
        footer=html_footer(), total=len(all_records),
        sections="\n".join(sections))

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("Riverhead Site Builder")
    print("=" * 60)

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
    for record in records:
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

    print("Building index ...")
    build_index(records, os.path.join(OUTPUT_DIR, "index.html"))

    print()
    print("Done! Next:")
    print("  pagefind --site {}".format(OUTPUT_DIR))
    print("  cd docs && python3 -m http.server 8000")

if __name__ == "__main__":
    main()
