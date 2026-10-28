#!/usr/bin/env python3
"""AIS Journal Watcher agent — Claude Sonnet 5.5 runs the weekly workflow.

Sonnet drives the loop through tools: fetch new papers, screen every abstract
against interests.json, confirm picks with the user, download (Unpaywall →
Elsevier API → aisel → real browser with saved sign-ins), summarize each PDF,
write a weekly report.

Usage (from the project root, inside .venv):
    python .agents/skills/ais-journal-watcher/scripts/agent.py            # interactive
    python .agents/skills/ais-journal-watcher/scripts/agent.py --auto     # no questions
    python .agents/skills/ais-journal-watcher/scripts/agent.py --resume   # reuse last fetch
    ... --max-papers 5    # cap downloads this run (default 10)

First time: `python .agents/skills/ais-journal-watcher/scripts/browser.py login`.
"""
import csv
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone

import anthropic
from anthropic import beta_tool

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)
import download as dl  # noqa: E402
import paths  # noqa: E402
import pipeline as pl  # noqa: E402

BASE, ROOT, STATE, PAPERS = dl.BASE, dl.ROOT, dl.STATE, dl.PAPERS
INBOX = os.path.join(PAPERS, "inbox")
JUDGED = os.path.join(STATE, "judged.json")

MODEL = "claude-sonnet-5-5"
BETAS = ["server-side-fallback-2026-07-01"]
# $ per million tokens (Claude Sonnet 5.5)
PRICE = {"in": 2.00, "out": 10.00, "cache_write": 2.50, "cache_read": 0.20}

ARGS = sys.argv[1:]
AUTO = "--auto" in ARGS
RESUME = "--resume" in ARGS
MAX_PAPERS = int(ARGS[ARGS.index("--max-papers") + 1]) if "--max-papers" in ARGS else 10

USAGE = {"in": 0, "out": 0, "cache_write": 0, "cache_read": 0}
DOWNLOADED = []  # ids downloaded this run (for the --max-papers cap)


def add_usage(u):
    USAGE["in"] += u.input_tokens or 0
    USAGE["out"] += u.output_tokens or 0
    USAGE["cache_write"] += u.cache_creation_input_tokens or 0
    USAGE["cache_read"] += u.cache_read_input_tokens or 0


def cost():
    return sum(USAGE[k] * PRICE[k] for k in USAGE) / 1e6


def load_json(path, default):
    return dl.load_json(path, default)


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=1)


def all_papers():
    """id -> paper, from the latest fetch (scored.json carries keyword scores)."""
    seen = load_json(os.path.join(STATE, "seen.json"), {})
    pool = {}
    for src in ("new_papers.json", "scored.json"):
        for p in load_json(os.path.join(STATE, src), []):
            # entries dropped from seen.json (e.g. the old mis-configured DSS
            # feed, which was really Computers & OR) are not real candidates
            if p.get("id") and p["id"] not in seen:
                continue
            pool.setdefault(p["id"], {}).update(p)
    return pool


def find_paper(key):
    pool = all_papers()
    if key in pool:
        return pool[key]
    k = key.lower()
    for p in pool.values():
        if k == (p.get("doi") or "").lower():
            return p
    return None


def index_rows():
    idx = os.path.join(PAPERS, "_index.csv")
    if not os.path.exists(idx):
        return []
    with open(idx, newline="") as f:
        return list(csv.DictReader(f))


def pdf_for(p):
    for r in index_rows():
        if r["status"] == "ok" and r["file"] and dl.index_key(r) == dl.index_key(
                {"doi": p.get("doi"), "title": p.get("title")}):
            path = os.path.join(ROOT, r["file"])
            if os.path.exists(path):
                return path
    return None


def with_topics(p):
    j = load_json(JUDGED, {}).get(p["id"], {})
    p = dict(p)
    if j.get("topics"):
        p["topics"] = j["topics"]
    return p


def log(msg):
    print("  · " + msg, flush=True)


# ---------------------------------------------------------------- tools

@beta_tool
def fetch_new_papers() -> str:
    """Fetch this week's papers from all 7 journals (RSS + Crossref), dedupe against
    everything seen before, and keyword-score them. Takes 3-5 minutes. Returns counts
    per journal. Call once at the start of a weekly run (not needed with --resume)."""
    log("fetching feeds (3-5 min)…")
    r = subprocess.run(paths.cmd("fetch_new.py"),
                       cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        return "fetch_new.py failed:\n" + r.stderr[-2000:]
    subprocess.run(paths.cmd("score.py"), cwd=ROOT,
                   capture_output=True, text=True)
    lines = [l for l in r.stdout.splitlines() if l.strip().startswith("[") or "NEW:" in l]
    return "\n".join(lines)


@beta_tool
def list_new_papers(offset: int = 0, limit: int = 40) -> str:
    """List papers from the latest fetch for screening, highest keyword score first.

    Args:
        offset: Start index (page through until `remaining` is 0).
        limit: Max papers to return (max 60).
    """
    papers = sorted(all_papers().values(), key=lambda p: -(p.get("score") or 0))
    judged = load_json(JUDGED, {})
    page = papers[offset:offset + min(limit, 60)]
    out = []
    for p in page:
        out.append({
            "id": p["id"], "journal": p.get("abbr"), "title": p.get("title"),
            "authors": (p.get("authors") or [])[:3], "date": p.get("pub_date"),
            "kw_score": p.get("score"), "kw_topics": list((p.get("topics_hit") or {}).keys()),
            "keywords": p.get("keywords"),
            "abstract": (p.get("abstract") or "")[:1500] or None,
            "already_judged": judged.get(p["id"], {}).get("verdict"),
        })
    return json.dumps({"total": len(papers), "offset": offset,
                       "remaining": max(0, len(papers) - offset - len(page)),
                       "papers": out}, ensure_ascii=False)


@beta_tool
def record_verdicts(verdicts: list[dict]) -> str:
    """Save your screening verdicts. Call after each page of list_new_papers.

    Args:
        verdicts: List of {"id": str, "verdict": "download"|"borderline"|"skip",
            "topics": [interest labels], "reason": one short sentence}. You may
            include only download/borderline papers; unlisted papers count as skip.
    """
    judged = load_json(JUDGED, {})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    n = 0
    for v in verdicts:
        if not isinstance(v, dict) or not v.get("id"):
            continue
        judged[v["id"]] = {"verdict": v.get("verdict", "skip"), "topics": v.get("topics", []),
                           "reason": v.get("reason", ""), "date": today}
        n += 1
    save_json(JUDGED, judged)
    log("recorded %d verdicts" % n)
    return "saved %d verdicts" % n


@beta_tool
def download_papers(ids: list[str]) -> str:
    """Download PDFs for the given paper ids (or DOIs) into Papers/YYYY-MM/<Journal>/.
    Tries Unpaywall, Elsevier API, AIS eLibrary, then a real browser using the user's
    saved publisher sign-ins. A note `needs_login:<host>` means the user must run
    `browser.py login` and sign in to that publisher.

    Args:
        ids: Paper ids from list_new_papers (DOIs also accepted).
    """
    env = dl.load_env()
    results = []
    todo = []
    for i in ids:
        p = find_paper(i)
        if not p:
            results.append({"id": i, "ok": False, "note": "unknown id"})
        elif pdf_for(p):
            results.append({"id": p["id"], "ok": True, "file": os.path.relpath(pdf_for(p), ROOT),
                            "note": "already downloaded"})
        elif len(DOWNLOADED) + len(todo) >= MAX_PAPERS:
            results.append({"id": p["id"], "ok": False,
                            "note": "skipped: --max-papers %d reached" % MAX_PAPERS})
        else:
            todo.append(p)
    if todo:
        with dl.open_browser() as br:
            for p in todo:
                log("downloading [%s] %s" % (p.get("abbr"), (p.get("title") or "")[:60]))
                ok, path, note = dl.download_paper(with_topics(p), env, browser=br)
                if ok:
                    DOWNLOADED.append(p["id"])
                results.append({"id": p["id"], "ok": ok, "note": note,
                                "file": os.path.relpath(path, ROOT) if ok else None,
                                "manual_url": None if ok else dl.browser_pdf_url(p)})
    return json.dumps(results, ensure_ascii=False)


@beta_tool
def list_inbox() -> str:
    """List PDFs the user saved manually into Papers/inbox/."""
    os.makedirs(INBOX, exist_ok=True)
    return json.dumps([f for f in sorted(os.listdir(INBOX)) if f.lower().endswith(".pdf")])


@beta_tool
def file_inbox_pdf(filename: str, paper_id: str) -> str:
    """Move a PDF from Papers/inbox/ to its standard location and mark it downloaded.

    Args:
        filename: File name inside Papers/inbox/.
        paper_id: Id (or DOI) of the paper this PDF is.
    """
    src = os.path.join(INBOX, os.path.basename(filename))
    p = find_paper(paper_id)
    if not os.path.exists(src):
        return "no such file in inbox"
    if not p:
        return "unknown paper id"
    with open(src, "rb") as f:
        if not dl.is_pdf(f.read()):
            return "that file is not a full PDF"
    dest = dl.out_path(p)
    shutil.move(src, dest)
    dl.save_index(with_topics(p), os.path.relpath(dest, ROOT), "ok", "inbox")
    return "filed -> " + os.path.relpath(dest, ROOT)


@beta_tool
def summarize_pdf(paper_id: str) -> str:
    """Read a downloaded paper's full PDF and write a markdown summary next to it
    (same base name, .md). Returns the summary.

    Args:
        paper_id: Id (or DOI) of a downloaded paper.
    """
    p = find_paper(paper_id)
    if not p:
        return "unknown paper id"
    path = pdf_for(p)
    if not path:
        return "no downloaded PDF for this paper"
    if not os.path.exists(path[:-4] + ".md"):
        log("summarizing [%s] %s" % (p.get("abbr"), (p.get("title") or "")[:60]))
    try:
        return pl.summarize(p, path, _UsageAdapter())
    except RuntimeError as e:
        return str(e)


class _UsageAdapter:
    add = staticmethod(add_usage)


@beta_tool
def write_weekly_report(markdown: str) -> str:
    """Save the weekly report (screening table + key points per downloaded paper).

    Args:
        markdown: Full report in markdown.
    """
    now = datetime.now()
    d = os.path.join(PAPERS, now.strftime("%Y-%m"))
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "_weekly_report_%s.md" % now.strftime("%Y-%m-%d"))
    with open(path, "w") as f:
        f.write(markdown.rstrip() + "\n")
    return "saved " + os.path.relpath(path, ROOT)


TOOLS = [fetch_new_papers, list_new_papers, record_verdicts, download_papers,
         list_inbox, file_inbox_pdf, summarize_pdf, write_weekly_report]

# ---------------------------------------------------------------- prompt

SYSTEM = """You run a weekly literature watch for an Information Systems researcher.
They may hold AIS and INFORMS memberships, and their university library may cover Wiley, Elsevier and
Taylor & Francis.

Journals watched: MIS Quarterly, Information Systems Research, Journal of MIS, Journal of the AIS,
Information Systems Journal, Decision Support Systems, Management Science.

Research interests (with the keyword lists used for a rough pre-score):
{interests}

Workflow:
1. Unless told the fetch is already done, call fetch_new_papers.
2. Screen EVERY paper: page through list_new_papers until remaining is 0. Judge each from title,
   abstract and keywords — the keyword score is only a hint and misses papers whose abstract is
   missing or that use different vocabulary. Verdicts: "download" (clearly central to an interest),
   "borderline" (adjacent; worth a look), "skip". Record download/borderline verdicts with
   record_verdicts after each page. Editorials, errata and "Editorial Board" items are skip.
3. Present a compact table of download + borderline papers: title, journal, matched interests,
   one-line reason, verdict. {confirm}
4. Call download_papers for the chosen ids. If any note says needs_login:<host>, tell the user
   to run `python .agents/skills/ais-journal-watcher/scripts/browser.py login`, sign in to that
   publisher, and reply — then retry. For other failures give the manual_url and say to save the
   PDF into Papers/inbox/; use list_inbox and file_inbox_pdf when they reply.
5. Call summarize_pdf for each downloaded paper and give 3-4 key points per paper in chat.
6. Call write_weekly_report with the screening table, download status and key points.

Keep chat output concise. Never print values from .env."""


def build_system():
    lines = []
    for t in INTERESTS["topics"]:
        lines.append("- %s: %s" % (t.get("label", t["name"]), ", ".join(t["terms"][:25])))
    confirm = ("This run is non-interactive (--auto): do not ask questions; download every "
               "'download' paper and end with the report." if AUTO else
               "Then end your turn and ask the user which to download (e.g. 'all download', "
               "or a list). Wait for their reply before downloading.")
    return SYSTEM.format(interests="\n".join(lines), confirm=confirm)


def kickoff():
    if RESUME:
        return ("Resume the weekly watch: the fetch for this batch is already done (state from "
                "the last run). Start at screening. Download cap this run: %d papers." % MAX_PAPERS)
    return "Run this week's journal watch. Download cap this run: %d papers." % MAX_PAPERS


# ---------------------------------------------------------------- main

def run():
    messages = [{"role": "user", "content": kickoff()}]
    while True:
        runner = CLIENT.beta.messages.tool_runner(
            model=MODEL, max_tokens=16000, betas=BETAS, fallbacks="default",
            thinking={"type": "adaptive"}, output_config={"effort": "medium"},
            cache_control={"type": "ephemeral"},
            system=build_system(), tools=TOOLS, messages=messages,
        )
        last = None
        for msg in runner:
            last = msg
            add_usage(msg.usage)
            for b in msg.content:
                if b.type == "text" and b.text.strip():
                    print("\n" + b.text.strip() + "\n", flush=True)
                elif b.type == "tool_use":
                    print("  → %s" % b.name, flush=True)
            messages.append({"role": "assistant", "content": msg.content})
            tool_response = runner.generate_tool_call_response()
            if tool_response is not None:
                messages.append(tool_response)
        if last is not None and last.stop_reason == "refusal":
            print("[model declined this turn: %s]" % getattr(last, "stop_details", None))
        if last is not None and last.stop_reason == "max_tokens":
            messages.append({"role": "user", "content": "Continue."})
            continue
        if AUTO:
            break
        try:
            reply = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if reply.lower() in ("", "q", "quit", "exit"):
            break
        messages.append({"role": "user", "content": reply})
    print("\nTokens: in %(in)d · out %(out)d · cache write %(cache_write)d · cache read %(cache_read)d"
          % USAGE + " · est. cost $%.2f" % cost())


INTERESTS = load_json(os.path.join(BASE, "interests.json"), {"topics": []})
for k, v in dl.load_env().items():
    os.environ.setdefault(k, v)
if not os.environ.get("ANTHROPIC_API_KEY"):
    sys.exit("ANTHROPIC_API_KEY missing — add it to the project-root .env")
CLIENT = anthropic.Anthropic()

if __name__ == "__main__":
    run()
