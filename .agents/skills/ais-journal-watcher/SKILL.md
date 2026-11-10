---
name: ais-journal-watcher
display-name: AIS Journal Watcher
description: Weekly scan of Information Systems journals (MISQ, ISR, JMIS, JAIS, ISJ, DSS, Management Science and more) for papers that match your research interests. Screens, downloads and summarizes with an AI model of your choice.
---

# AIS Journal Watcher

Checks 14 sources for new papers, screens them against the user's research interests,
downloads matches into `Papers/YYYY-MM/<Journal>/`, and summarizes each downloaded paper.

**Sources**: MIS Quarterly, Information Systems Research, Journal of MIS, Journal of the AIS,
Information Systems Journal, Decision Support Systems, Management Science, MSOM, Organization Science,
Communications of the ACM, Science, PNAS, NBER Working Papers, Harvard Business Review.

**Interests**: whatever you put in `interests.json` (user-editable; the starter file has three example topics).

## Dashboard (main way to use it)

Double-click **`Journal Watcher.command`** in the checkout (or use the Mac app). It sets up `.venv` on first run, starts
`dashboard/server.py` on `localhost:8765`, and opens the browser. Close the Terminal window to stop it.

- **This week**: fetch the newest papers, minus everything in `seen.json`, then Sonnet 5.5 screens them against all interests.
- **Search archive**: pick a year range, topics (none = all), an optional focus phrase, and journals. Every article
  comes from Crossref (`fetch_new.crossref_journal_range`) and is screened for those topics only. This mode never touches `seen.json`.
- **Journal tiles**: each run opens as one rounded tile per journal, showing a live status bar (waiting → reading feed /
  querying Crossref → looking up → screening n/m → done) and found / download / borderline counts. Click a tile to see
  that journal's papers and verdicts; "View every paper in one list" shows them all.
- **Main method** (search archive): e.g. *Econometrics*, on its own (any topic) or combined with topics. Methods are
  defined in `interests.json` → `methods` (label, description, example terms); add more there and they appear as chips.
  Sonnet also tags every paper's main method (shown on the cards).
- Papers stream in live, then get a verdict (download / borderline / skip), a relevance score and a one-line reason.
  Filter, tick, then **Download & summarize**. Per-paper status is live, and summaries open in the side drawer.
- **Publisher logins** button = `browser.py login`. **Library** = everything downloaded. **Past runs** reopen saved runs
  (`state/runs/*.json`).
- Engine: `scripts/pipeline.py` (fetch / screen / download / summarize with an `emit` callback). Screening goes through the AI layer (`ai.chat("screen", …)`: model set in Settings → AI, monthly budget, usage ledger) in batches of 25,
  4 in parallel, structured JSON output at effort `low`, about $0.0015 per paper (a full year of all 7 journals is ~900 papers, ~$1.40).
- JMIS and DSS have no abstracts in Crossref. Add a free `OPENALEX_API_KEY` to `.env` to fill them; otherwise those papers
  are screened on title and tagged "title only".

## Chat agent (CLI alternative)

From the checkout root:

```
source .venv/bin/activate                                      # deps: requirements.txt
python .agents/skills/ais-journal-watcher/scripts/agent.py     # weekly run, interactive
```

`scripts/agent.py` is a **Claude Sonnet 5.5** agent (Anthropic SDK tool runner). It:
1. fetches new papers (`fetch_new.py` + `score.py`, ~3-5 min),
2. screens **every** new paper from title/abstract/keywords (the keyword score is only a hint),
   saving verdicts to `state/judged.json`,
3. shows a table of download/borderline papers and asks which to download,
4. downloads them (`download.py` cascade, below),
5. reads each full PDF and writes `<same-name>.md` beside it,
6. writes `Papers/YYYY-MM/_weekly_report_<date>.md`.

Flags: `--auto` (no questions; downloads all "download" verdicts), `--resume` (skip the
fetch and reuse the last batch), `--max-papers N` (download cap per run, default 10).
At the end it prints token usage and an estimated cost.

Spending: the agent's own conversation with Claude (steps 2, 3 and 6: the screening, the table,
the report) is sent straight to the Anthropic API with `ANTHROPIC_API_KEY`. The monthly budget in
Settings → AI does **not** stop it, and it is **not** in the usage record (Settings → Usage); the
figure printed at the end is the only account of it. The summaries of step 5 go through the app's
AI layer (`ai.py`), so the budget applies to them and they are in the usage record. The dashboard's
weekly check does the same work with everything inside the budget. The script says this when it starts.

## Downloads run in the background
`browser.py` uses Chromium's new headless mode with a normal user agent, which passes the Cloudflare checks on
INFORMS, AIS, Wiley, PNAS and others without showing a window. If a site insists on an interactive
"Verify you are human" checkbox (ScienceDirect does), a visible window opens for that one page, the dashboard
shows a banner, and once you tick it the run continues in the background (clearance is kept in the profile).
The checkbox is never clicked automatically. HBR articles are web pages, so they are saved as PDFs of the page,
with cookie banners removed. NBER PDFs are free over plain HTTP. `AIS_BROWSER_VISIBLE=1` shows every download.

## Library
The dashboard's Library lists every downloaded paper as journal tiles; open one to see its papers grouped by
year, each with its authors, summary and PDF.

## Publisher access (one-time)

INFORMS, Wiley, T&F, Elsevier and AIS eLibrary sit behind Cloudflare and paywalls. Plain HTTP gets
403; headless browsers get stuck on the Turnstile check. A **headed** Chromium passes, so:

```
python .agents/skills/ais-journal-watcher/scripts/browser.py login
```

This opens one tab per publisher. Sign in to INFORMS with the member account (covers ISR + MS),
AIS eLibrary with the AIS member login (MISQ, JAIS), and Wiley / ScienceDirect / T&F through
"institutional login" → choose your own university. Then close the window (or use Settings → Library access). Cookies persist in
`state/browser-profile/` (gitignored), so later runs download automatically. A Chromium window
appears briefly during downloads; that's expected. Re-run `login` when a download note says
`needs_login:<host>`.

## Download cascade (`scripts/download.py`)
1. Unpaywall OA (needs `UNPAYWALL_EMAIL` in `.env`; nothing in the app writes that key, so add the line by hand)
2. Elsevier Article API (if `ELSEVIER_API_KEY` is set; DSS)
3. Direct HTTP (aisel content link / publisher; usually 403)
4. Real browser (`scripts/browser.py`) with the saved sign-ins (it follows a landing page's PDF link only when it is this article's: `Browser._pdf_link`); when the publisher wants a login and the library is OpenAthens or EZproxy and signed in (Settings → Library access), the same address is asked for through the library's sign-in (`signins.via`); a library session that has ended or was never opened gives the note `needs_library_login` (the card offers the library sign-in)
5. The library's own copy (EBSCO), for the journals `library.json` lists under `via_ebsco`
6. A free copy elsewhere (`scripts/open_copies.py`: arXiv, Semantic Scholar, Europe PMC, preprint servers)
7. Manual: user saves the PDF into `Papers/inbox/`; the agent files it (`file_inbox_pdf`).

**Every PDF a route returns is checked against the paper** (`scripts/identity.py`: its DOI, or its title and authors, must be on the first pages). A different work is refused
(nothing is written, the note says `wrong paper via <route>: …`, the next route is tried; the paper ends as `wrong_paper` in the index and in the run); a PDF that cannot be
compared (a scan, no title on record) is kept and noted `identity not checked`. `python identity.py audit` lists the PDFs in a library that do not look like their paper (read-only).

Files: `Papers/YYYY-MM/<Journal>/<YYYY-MM>_<ABBR>_<doi>_<slug>.pdf`; one row per paper in
`Papers/_index.csv` (upserted, never duplicated).

The scripts also run standalone: `download.py --pick <doi,...>`, `download.py --no-browser`,
`browser.py get <url> <out.pdf>`, `fetch_new.py --doi <doi>`.

## Configuration
- **`.env` in your data folder** (the Settings page writes the AI keys; add `UNPAYWALL_EMAIL` and `ELSEVIER_API_KEY` by hand if you want them): `ANTHROPIC_API_KEY`, `UNPAYWALL_EMAIL`, `ELSEVIER_API_KEY` (optional).
  Never print or commit these values.
- `interests.json`: topics and terms (fed to the agent's system prompt and the keyword pre-score).
- `journals.json`: feeds/ISSNs; if a feed breaks, switch that journal to `"source": "crossref"`
  using its `issn` (Management Science already does).
- `state/seen.json`: makes runs incremental.

## Known quirks
- The DSS feed (ScienceDirect RSS) rotates; very fresh items may lack abstracts and DOIs.
- ISR's feed only carries "Ahead of Print" stubs; real abstracts come from Crossref enrichment.
- Some aisel items have no DOI → filenames get a `nodei` segment.
- ISR/MS author lists from Crossref are sometimes partial.
