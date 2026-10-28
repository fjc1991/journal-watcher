# Reader roadmap — what is done and what is left

Sizes: **S** < ½ day · **M** 1–2 days · **L** several days. ✨ = uses GenAI. Inspiration: Zotero 7, Readwise Reader, Semantic Scholar's Reader,
SciSpace, Elicit, NotebookLM, MarginNote, Heptabase, scite, Research Rabbit / Connected Papers / Litmaps, Consensus, and LiquidText.
Rules that stay: paper organisation never changes (`Papers/YYYY-MM/<Journal>/`); new data lives in sidecars next to a PDF or in underscore
files under `Papers/` (`_tags.json`, `_status.json`, `_folders/`).

**Decisions:** priorities = reading well, literature reviews, staying current, writing, **plus** brainstorming research ideas from a folder of papers ·
tools = Dropbox / Dropbox Paper, Word + EndNote/Mendeley, LaTeX/Overleaf · "folder" = virtual folder in the app · current events = provider web search.

## Done
Reader (highlights, tags, notes canvas, search + collapse, Ask with citations, AI layer for Claude/OpenAI/Gemini/OpenRouter/Ollama) — see `HANDOFF.md §2b`.
**Round 1a — Library** (`HANDOFF.md §2c`): search + filters over papers *and your own highlights/notes* (L1) · reading status, star, takeaway, continue-reading shelf (L2) ·
virtual folders (L3) · add your own PDF or DOI (L4) · highlights hub (L6) · library-wide tag manager with merge/delete (L7, partly) · BibTeX + RIS export (W1) ·
annotated-PDF export (W2) · Markdown + Word notes export, per paper / folder / selection (W3, partly) · offline tests + headless-browser smoke test (P5, partly).

## Done — the app and the new look (2026-10-27)
**Journal Watcher.app + a DMG** (`app/build_app.sh`, PyInstaller + pywebview; data in a folder you choose; see `HANDOFF.md §2k`) · **the "Paper" design**: light first, warm, a left sidebar with the folders as pages, flat pages, pastel pills, black pill buttons, a lighter dark theme (Settings → Appearance: Light / Dark / Match my computer) · API-key field in Settings (no `.env` editing).
**Install and update like any app (2026-10-28):** a drag-to-Applications disk image (arrow picture, Applications link), **update notices** (the app asks a feed; "Update to x.y.z" in the top bar with what is new; Update now downloads, checks and swaps itself in; `app/release.sh` publishes a version), and a **welcome guide** for first-time users (API keys, publisher sign-ins, a map of the app).
Left: signing with an Apple Developer ID and notarisation (hooks are in `build_app.sh`, untested: the app is ad-hoc signed, so a DMG that arrives through a browser still needs Privacy & Security → Open Anyway the first time), choosing where updates are published (Dropbox / GitHub / a site: `app/release.conf`), signing the update feed, an Intel/universal build, bundling Chromium so the first sign-in needs no download.

## Done — Round 1b (reader + AI)
R1 R2 R3 R4 R7 · N1 N2 · G1 G5 G11 · P1 P2 P3 — see `HANDOFF.md §2d`. Not done from the list: the citation-card TL;DR is from Semantic Scholar (not generated), and `web_search` exists in `ai.py` but no feature uses it yet.

## Done — Round 2 (fourth part): the skim layer
**G3** — reader button **⚡ Skim**: one model call per paper finds the sentences that matter for seven lenses (research question, hypothesis, theory, sample & data, finding, limitation, contribution), drawn as ghost highlights;
a density slider (key points / more / everything), lens chips, next/previous, click a ghost to highlight or dismiss it, "Highlight all shown" (one undo step). Accepted sentences become real highlights coloured by lens and tagged
`lens: finding` etc. Saved beside the PDF (`<name>.skim.json`). See `HANDOFF.md §2h`. Not done: your own lenses, skimming a whole folder at once.

## Done — Round 2 (third part): related and missing papers
**Q6** — folder tab and Library tab *Discover*: papers cited by several of your papers that you don't have (OpenAlex reference lists; the DOIs in a PDF's own bibliography stand in when OpenAlex has none),
what OpenAlex calls related (filtered by closeness), the library papers closest to a folder; the reader's *Related* tab for one paper; tick → **Download** goes straight into the download queue as a "Picks" run
(open access first, then publisher sign-ins / library access, then a summary); "not interested" hides a paper for good. See `HANDOFF.md §2g`.

## Done — Round 2 (second half): ask a folder, comparison matrix
**Q1 Ask a folder / the library** (folder tab *Ask* and Library tab *Ask*; scopes: folder, journal, year, tag, status; read the closest passages or the cards; conversation saved; every citation opens its paper at the place) ·
**Q2 comparison matrix** (folder tab *Matrix*; columns from the cards plus your own questions answered per paper with page + quote; edit cells; CSV / Word / LaTeX with `\citet` keys / Markdown). See `HANDOFF.md §2f`.

## Done — Round 2 (first half): paper cards, folder evidence, Research Ideas Lab
**G4 paper cards** (a Card tab in the reader; `<paper>.card.json`; every line cites its pages; editable) · **folder Evidence view** (topics / theories / methods, limitations and future-research suggestions
grouped by meaning — the useful part of Q4) · **Q5 Research Ideas Lab** (brief → current events by web search you can untick → ideas with evidence ids → editor scores → OpenAlex closest-work check → board with
star / shortlist / notes / "develop this idea" / Markdown + Word export) · `web_search` is now used (events) · cards are found by Library search. See `HANDOFF.md §2e`.
Not done from Q5's design: the novelty check uses OpenAlex only (no Semantic Scholar), "regenerate with new constraints" is Generate again with *Replace*, and the board is a list (no canvas).

## (original Round 1b list, kept for reference)
| # | Feature | Size |
|---|---|---|
| R1 | Clickable in-text links (`[14]`, Table 3, Figure 2) with a back button | S |
| R2 | Citation hover card: bibliography entry, DOI, "in my library?", send to download; ✨ TL;DR | M |
| R3 | Page thumbnails, reading progress and time left | S |
| R4 | Area / figure / table excerpt → image card on the notes canvas | M |
| R7 | Copy with citation + deep link to a passage | S |
| N1 | Undo / redo | S |
| N2 | Underline, strikethrough, sticky note, area highlight; editable colour legend (claim / method / finding) | M |
| G1 ✨ | Explain selection (plain words, define, why it matters, translate) + your own one-click prompts | S–M |
| G5 ✨ | Ask upgrades: history, follow-ups, suggested questions, compare two models | S–M |
| G11 ✨ | Summary templates; move the pipeline's summaries onto `ai.py` | S–M |
| P1 | Settings page (providers, models per feature, export folder, legend) | S–M |
| P2 | Usage & cost panel + monthly budget warning | S |
| P3 ✨ | AI plumbing: prompt library, result cache, web-search flag + image input in `ai.py`, citation validator, "local only" mode | M |

## Left — Round 2 (research power)
~~G4 paper cards~~ done → ~~G3 skim layer~~ done ·
~~Q1 ask a folder / the library~~ done · ~~Q2 comparison matrix~~ done · ~~Q5 Research Ideas Lab~~ done (MVP, below) · ~~Q6 related & missing papers~~ done ·
~~N3 outline view, Markdown cards~~ done · ~~N4 arrows and group frames~~ done · ~~N5 canvas zoom/pan, note on the paper~~ done · ~~X1 folder workspace~~ done (folder tab *Workspace*) · ~~X2 cross-paper search~~ done (Library: *In the PDFs*) ·
~~G2 explain figure/table from the page image~~ done (hover "Table 3" → ✨ Explain it; equations via ▭ Area) · ~~G10 flashcards + daily review~~ done (Library tab *Review*) · ~~Q8 folder digest~~ done (the Synthesis overview; not a weekly "what changed") · ~~L5 reading queue~~ done (Library *Up next*) · ~~L7 saved searches~~ done.

## Left — Round 3 (writing & automation)
~~W4 related-work drafter with citations~~ done (`HANDOFF.md §2j`; not done: reordering themes by dragging, a BibTeX/LaTeX export of the section) · ~~W5 claim check~~ done · ~~Q3 synthesis map~~ done · ~~Q4 gap finder~~ done (folder tab *Synthesis*) · ~~Q7 personalised screening~~ done as a fit label + sort + folder suggestion (auto-tagging not done) ·
~~G6 Reviewer-2 critique~~ done (Claim check tab) · ~~G7 methods & measures extractor~~ done (matrix preset) · ~~G8 smart citation context~~ done (hover card: where it is cited + ✨ how it is used) · ~~G9 note assistant~~ done (`//` links a note to a card, highlight or page; no AI needed) · ~~P4 MCP server for your library~~ done (`scripts/mcp_server.py`) ·
~~R5 side-by-side papers~~ done (⧉ Beside opens a second window) · ~~R6 reflow mode~~ done (¶ Text) · ~~G12 audio overview~~ done (🎧 Listen; the browser's voices read the script) · ~~W6 RIS/BibTeX import~~ done · ~~LaTeX `\\citep` snippet~~ done · ~~folder as a real directory~~ done (export folder = Settings → Reader) ·
~~P6 accessibility~~ done (contrast, names, focus, skip link, reduced motion; screen-reader pass of the canvas not done) · ~~background indexing~~ done · `git init` + backups (not done on purpose: the library is ~190 MB of PDFs, decide what to track first).

## Research Ideas Lab (Q5, flagship) — built; the design below is what it follows
Folder view → **Brainstorm ideas**. (1) Inputs: a folder, optional focus, constraints (methods, data access, horizon, journals), number of ideas, ambition. (2) Evidence from paper cards:
topics/constructs/theories/methods, **clusters of limitations** and **future-research suggestions**, tensions between papers, under-used methods/contexts. (3) Current events via the provider's
web search from the folder's topics (regulation, launches, incidents), shown as a dated list with URLs you can untick before generation. (4) Generate (JSON schema): title, research question,
**why now** (event + source), the gap it fills with ⤴ links to the papers' limitation/future-research passages, lens, method + identification strategy, data, contribution, fitting journals,
risks, first steps, closest papers. (5) Critique + novelty check against OpenAlex/Semantic Scholar; every claim labelled *grounded in your papers* or *speculative*. (6) Ideas board you can rank
and regenerate, "Develop this idea" → hypotheses/design/outline, export to Markdown/Word in a Dropbox folder, saved as `Papers/_folders/<name>.ideas.json`; cost estimated first.
Needs: folders ✅, paper cards (G4), P3 web-search flag + cache, Q1, Q6.
