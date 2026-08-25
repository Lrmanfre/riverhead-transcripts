# Decisions & Votes Extraction

Structured record of every formal Town Board action: resolutions, motions, and
public hearing actions, with outcomes and per-member roll-call votes where the
roll call is audible in the recording. Extracted by `riverhead_extract_votes.py`
(Claude API), stored inside each transcript JSON under a `decisions` key, and
rendered by `riverhead_build_site.py` as a "Decisions & Votes" block on each
meeting page plus a site-wide browsable page at `/decisions/`.

Since the town publishes no minutes with vote records on the portal (0 of 542
events as of August 2026), this is the only browsable vote record anywhere.

## How it works

1. **Selection.** Only transcripts with `meta.category == "Town Board"`.
   Idempotent: a transcript with a current `decisions` block is skipped unless
   `--force` is passed, so the nightly job only processes new meetings.
2. **Grounding.** The CivicClerk API's `publishedFiles` array carries the real
   fileIds for each event's official documents (a different id space than the
   portal URL's agendaId, the bug that originally disabled agenda grounding).
   The extractor fetches the official Minutes (preferred) or Agenda as plain
   text, date-checks it, and feeds it to the model as the authoritative list
   of resolution numbers and titles. The published minutes record no votes;
   the roll call always comes from the transcript.
3. **Extraction.** Long transcripts are split into overlapping chunks so a
   vote spanning a chunk boundary is never lost; results are merged and
   deduplicated by resolution number. A truncated model response (output token
   cap) is salvaged item by item rather than discarded.
4. **Roster normalization.** Roll-call names garbled by speech-to-text
   ("Waske", "Waskey") are mapped to the board roster for the meeting's date,
   both in the prompt and again in the validator. Rosters live in `ROSTERS`
   in `riverhead_extract_votes.py`: Supervisor Tim Hubbard through 2025,
   Jerry Halpin from January 2026; council Waski, Merrifield, Kern, Rothwell
   throughout. **Update `ROSTERS` after each election.**
5. **Honesty rules.** The model is forbidden from inventing votes. An action
   without an audible roll call gets an empty vote list, confidence "low",
   and the site renders "Voice vote or roll call not clearly audible."
6. **Video links.** The model returns a short verbatim quote at the moment of
   each vote; the script fuzzy-matches it against the timestamped segments to
   produce a "watch vote" deep link into the meeting video.

## Usage

```
python3 riverhead_extract_votes.py                # backfill all missing
python3 riverhead_extract_votes.py --days 14      # nightly mode (Step 3c)
python3 riverhead_extract_votes.py --limit 3      # pilot / testing
python3 riverhead_extract_votes.py --force        # regenerate everything
python3 riverhead_extract_votes.py --dry-run      # list work, no API calls
```

Key setup is shared with the summarizer: `riverhead.env` holds
`ANTHROPIC_API_KEY`, gitignored. The script imports shared utilities from
`riverhead_summarize.py` (rate limiting, retries, API key resolution), so the
two must stay in the same directory.

## Pipeline

`run_pipeline.sh` Step 3c runs the extractor nightly after summaries with
`--days 14 --workers 2`. Non-fatal by design: a missing key or API failure
logs a warning and the site still publishes.

## Stored shape

```json
"decisions": {
  "status": "ok",
  "model": "claude-sonnet-4-6",
  "prompt_version": "1",
  "generated_at": "...",
  "grounded_in": "Minutes",
  "items": [{
    "kind": "resolution",
    "number": "2026-765",
    "title": "Budget transfer for 2026 legal fees",
    "outcome": "adopted",
    "votes": [{"member": "Joann Waski", "vote": "yes"}],
    "unanimous": true,
    "confidence": "high",
    "timestamp_s": 2396
  }]
}
```

`outcome` is one of adopted, defeated, tabled, amended, withdrawn, held,
unknown. `vote` is one of yes, no, abstain, absent, recused.

## Tuning

- **Prompt and rules:** `SYSTEM` in `riverhead_extract_votes.py`.
- **Rosters and garble map:** `ROSTERS`, `GARBLE_HINTS`, `GARBLE_MAP` in the
  same file.
- **Regenerate after a prompt change:** bump `PROMPT_VERSION`, then run with
  `--force`.
- **Rendering:** `render_decisions` / `build_decisions_page` and the
  `.transcript-decisions` / `.decisions-page` CSS in `riverhead_build_site.py`.

## Known limitations

- Mover and seconder are usually not attributable from the transcript alone
  ("So moved. Second." with no name); that waits for speaker diarization.
- Minutes are published days after a meeting, so the first nightly extraction
  of a new meeting usually grounds on the Agenda or nothing; the numbers are
  still spoken aloud in the transcript. Re-run with `--force` on a specific
  file to re-ground later if needed.
- Extraction covers Town Board regular meetings only. ZBA and Planning Board
  use different vote language and are a planned v2.
- Like the AI summaries, the decisions blocks carry a visible auto-generated
  disclaimer and are excluded from the Pagefind search index.
