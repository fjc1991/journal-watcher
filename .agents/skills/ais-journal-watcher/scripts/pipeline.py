"""Shared engine for the dashboard (and agent.py): runs, fetching, Sonnet
screening, downloading and summarizing — all reporting progress through an
`emit(event, data)` callback so a UI can show it live.

Run kinds:
  weekly  — latest items from all 7 journals, minus everything in seen.json
  search  — every article of chosen journals in a year range (Crossref);
            never touches seen.json
"""
import base64
import csv
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
import shutil

import anthropic

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)
import download as dl  # noqa: E402
import fetch_new as fx  # noqa: E402
import notes_store as ns  # noqa: E402
import folders as fo  # noqa: E402
import authorcache as ac  # noqa: E402
import readstate as rs  # noqa: E402

BASE, ROOT, STATE, PAPERS = dl.BASE, dl.ROOT, dl.STATE, dl.PAPERS
RUNS = os.path.join(STATE, "runs")

MODEL = "claude-sonnet-5-5"
BETAS = ["server-side-fallback-2026-07-01"]
# $ per million tokens (Claude Sonnet 5.5)
PRICE = {"in": 2.00, "out": 10.00, "cache_write": 2.50, "cache_read": 0.20}
EST_SCREEN_COST_PER_PAPER = 0.0015  # measured-ish; shown in the UI before a search

for _k, _v in dl.load_env().items():
    os.environ.setdefault(_k, _v)
_client = None


def client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def load_json(path, default):
    return dl.load_json(path, default)


def journals():
    return load_json(os.path.join(BASE, "journals.json"), {"journals": []})["journals"]


def interests():
    return load_json(os.path.join(BASE, "interests.json"), {"topics": []})


def topic_labels():
    return [t.get("label", t["name"]) for t in interests()["topics"]]


def methods():
    return interests().get("methods", [])


def method_labels():
    return [m.get("label", m["name"]) for m in methods()]


# ---------------------------------------------------------------- usage

class Usage:
    def __init__(self, d=None):
        self.d = dict(d or {"in": 0, "out": 0, "cache_write": 0, "cache_read": 0})
        self.lock = threading.Lock()

    def add(self, u):
        with self.lock:
            self.d["in"] += u.input_tokens or 0
            self.d["out"] += u.output_tokens or 0
            self.d["cache_write"] += u.cache_creation_input_tokens or 0
            self.d["cache_read"] += u.cache_read_input_tokens or 0

    def add_result(self, r):
        """Add an ai.Result (any provider). Claude Sonnet 5.5 tokens go in the usual buckets; anything else is added as dollars."""
        if getattr(r, "cached", False):
            return
        with self.lock:
            if r.provider == "anthropic" and r.model == MODEL:
                for k in ("in", "out", "cache_write", "cache_read"):
                    self.d[k] += r.usage.get(k, 0)
            else:
                self.d["extra"] = self.d.get("extra", 0) + (r.cost or 0)

    def cost(self):
        return sum(self.d[k] * PRICE[k] for k in PRICE) / 1e6 + self.d.get("extra", 0)


# ---------------------------------------------------------------- downloaded files

def downloaded_files():
    """index_key -> relative file path for every paper with a PDF on disk."""
    idx = os.path.join(PAPERS, "_index.csv")
    out = {}
    if os.path.exists(idx):
        with open(idx, newline="") as f:
            for r in csv.DictReader(f):
                if r["status"] == "ok" and r["file"] and os.path.exists(os.path.join(ROOT, r["file"])):
                    out[dl.index_key(r)] = r["file"]
    return out


def paper_key(p):
    return dl.index_key({"doi": p.get("doi"), "title": p.get("title")})


def library():
    idx = os.path.join(PAPERS, "_index.csv")
    rows = []
    state, member = rs.load(), fo.membership()   # reading status (Papers/_status.json) and virtual folders (Papers/_folders/)
    if os.path.exists(idx):
        with open(idx, newline="") as f:
            for r in csv.DictReader(f):
                if r["status"] != "ok" or not r["file"]:
                    continue
                md = r["file"][:-4] + ".md"
                r["summary"] = md if os.path.exists(os.path.join(ROOT, md)) else None
                r["notes"] = ns.counts(os.path.join(ROOT, r["file"]))  # reader highlights/cards, None if none
                r["card"] = os.path.exists(os.path.join(ROOT, r["file"][:-4] + ".card.json"))   # paper card (papercard.py)
                r["skim"] = os.path.exists(os.path.join(ROOT, r["file"][:-4] + ".skim.json"))   # skim layer (skim.py)
                key = dl.index_key(r)
                r["key"], r["state"], r["folders"] = key, state.get(key) or {}, member.get(key, [])
                # Papers/YYYY-MM/<Journal>/... -> publication year for grouping
                m = re.match(r"Papers/(\d{4})-\d{2}/", r["file"])
                r["year"] = m.group(1) if m else ""
                rows.append(r)
    return ac.apply(rows)[::-1]        # authors the index lacks come from the Crossref cache (authorcache.py)


# ---------------------------------------------------------------- runs

def week_of(iso_ts=None):
    """ISO week id ("2026-W43") of a timestamp, in local time (Mon–Sun)."""
    dt = datetime.fromisoformat(iso_ts).astimezone() if iso_ts else datetime.now().astimezone()
    y, w, _ = dt.isocalendar()
    return "%d-W%02d" % (y, w)


def week_label(wid):
    y, w = wid.split("-W")
    mon = date.fromisocalendar(int(y), int(w), 1)
    sun = mon + timedelta(days=6)
    if mon.month == sun.month:
        rng = "%d–%d %s %d" % (mon.day, sun.day, sun.strftime("%b"), sun.year)
    elif mon.year == sun.year:
        rng = "%d %s – %d %s %d" % (mon.day, mon.strftime("%b"), sun.day, sun.strftime("%b"), sun.year)
    else:
        rng = "%d %s %d – %d %s %d" % (mon.day, mon.strftime("%b"), mon.year, sun.day, sun.strftime("%b"), sun.year)
    return "Week %d · %s" % (int(w), rng)


class Run:
    """One dashboard run, persisted to state/runs/<id>.json."""

    def __init__(self, kind, params, rid=None):
        self.kind, self.params = kind, params
        self.id = rid or "%s_%s" % (datetime.now().strftime("%Y%m%d-%H%M%S"), self.slug())
        self.created = datetime.now(timezone.utc).isoformat()
        self.stage, self.status = "created", "running"
        self.papers = {}   # id -> paper dict (+ verdict, relevance, reason, dl_status, file)
        self.order = []
        self.usage = Usage()
        self.errors = []
        self.jstate = {}   # abbr -> {state, label, done, total} for the journal tiles
        self.week = week_of(self.created) if kind == "weekly" else None
        self.checks = []   # weekly: timestamps of every check merged into this week
        self._last_save = 0

    def slug(self):
        if self.kind == "weekly":
            return "weekly"
        if self.kind == "picks":
            return "picks"
        p = self.params
        bits = ["%s-%s" % (p.get("year_from"), p.get("year_to"))]
        bits += [re.sub(r"[^a-z0-9]+", "", t.lower())[:12]
                 for t in (p.get("topics") or [])[:2] + (p.get("methods") or [])[:1]]
        return "search_" + "_".join(bits)

    def title(self):
        if self.kind == "weekly":
            return week_label(self.week or week_of(self.created))
        if self.kind == "picks":        # papers chosen from the Discover lists (related.py), downloaded like any other
            return "Picks · " + (self.params.get("label") or "related papers")
        p = self.params
        yrs = str(p["year_from"]) if p["year_from"] == p["year_to"] else "%s–%s" % (p["year_from"], p["year_to"])
        parts = [yrs]
        if p.get("topics"):
            parts.append(", ".join(p["topics"]))
        if p.get("methods"):
            parts.append("method: " + ", ".join(p["methods"]))
        if not p.get("topics") and not p.get("methods"):
            parts.append("all interests")
        if p.get("query"):
            parts.append('"%s"' % p["query"])
        return " · ".join(parts)

    def add_paper(self, p):
        if p["id"] in self.papers:
            return False
        self.papers[p["id"]] = p
        self.order.append(p["id"])
        return True

    def to_dict(self, full=True):
        d = {"id": self.id, "kind": self.kind, "params": self.params, "title": self.title(),
             "created": self.created, "stage": self.stage, "status": self.status,
             "usage": self.usage.d, "cost": round(self.usage.cost(), 4),
             "week": self.week, "checks": self.checks,
             "updated": max(self.checks) if self.checks else self.created,
             "n_papers": len(self.order), "errors": self.errors[-20:], "jstate": self.jstate,
             "n_download": sum(1 for p in self.papers.values() if p.get("verdict") == "download"),
             "n_borderline": sum(1 for p in self.papers.values() if p.get("verdict") == "borderline")}
        if full:
            d["papers"] = [self.papers[i] for i in self.order]
        return d

    def save(self, force=False):
        if not force and time.time() - self._last_save < 2:
            return
        self._last_save = time.time()
        os.makedirs(RUNS, exist_ok=True)
        tmp = os.path.join(RUNS, self.id + ".json.tmp")
        with open(tmp, "w") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False)
        os.replace(tmp, os.path.join(RUNS, self.id + ".json"))

    @classmethod
    def load(cls, rid):
        d = load_json(os.path.join(RUNS, os.path.basename(rid) + ".json"), None)
        if not d:
            return None
        r = cls(d["kind"], d["params"], d["id"])
        r.created, r.stage, r.status = d["created"], d["stage"], d["status"]
        if r.status == "running":  # server stopped mid-run
            r.status, r.stage = "stopped", "ready"
        r.usage = Usage(d.get("usage"))
        r.errors = d.get("errors", [])
        r.jstate = d.get("jstate", {})
        r.week = d.get("week") or (week_of(r.created) if r.kind == "weekly" else None)
        r.checks = d.get("checks") or ([r.created] if r.kind == "weekly" else [])
        for p in d.get("papers", []):
            if p.get("dl_status") in ("downloading", "summarizing"):
                # interrupted (dashboard closed mid-download): ready to retry
                p["dl_status"] = "ok" if p.get("file") and os.path.exists(os.path.join(ROOT, p["file"])) else None
                p["dl_note"] = "interrupted — press Download again"
            r.add_paper(p)
        return r


    def merge(self, other):
        """Fold another run of the same week into this one: union of papers
        (keeping verdicts and downloads from either), summed usage, all checks."""
        for p in other.order:
            q = other.papers[p]
            if p not in self.papers:
                self.add_paper(q)
            else:
                self.papers[p] = merge_paper(self.papers[p], q)
        for k in self.usage.d:
            self.usage.d[k] += other.usage.d.get(k, 0)
        self.errors += other.errors
        self.checks = sorted(set(self.checks + (other.checks or [other.created])))
        self.created = min(self.created, other.created)
        if other.checks and max(other.checks) >= max(self.checks or [""]):
            self.jstate = other.jstate or self.jstate


def merge_paper(a, b):
    """Combine two records of the same paper; non-empty newer fields win, but a
    finished download/summary is never lost."""
    m = dict(a)
    m.update({k: v for k, v in b.items() if v not in (None, "", [])})
    for src in (a, b):
        if src.get("dl_status") == "ok":
            for k in ("dl_status", "file", "summary", "dl_note"):
                if src.get(k):
                    m[k] = src[k]
    return m


def weekly_run(current=None):
    """This week's run: the existing one (in memory or on disk) or a new one."""
    wid = week_of()
    rid = wid + "_weekly"
    if current is not None and current.id == rid:
        return current
    r = Run.load(rid) or Run("weekly", {"topics": topic_labels(), "query": ""}, rid)
    r.week = wid
    return r


def migrate_weekly():
    """One run per week: merge older per-check weekly files into <week>_weekly.json
    (originals are moved to state/runs/_merged/)."""
    if not os.path.isdir(RUNS):
        return []
    groups = {}
    for fn in os.listdir(RUNS):
        if fn.endswith(".json"):
            d = load_json(os.path.join(RUNS, fn), None)
            if d and d.get("kind") == "weekly":
                groups.setdefault(d.get("week") or week_of(d["created"]), []).append(fn[:-5])
    merged = []
    for wid, ids in groups.items():
        canon = wid + "_weekly"
        others = sorted((i for i in ids if i != canon), key=lambda i: Run.load(i).created)
        if not others:
            continue
        base = Run.load(canon) if canon in ids else None
        if base is None:
            first = Run.load(others.pop(0))
            base = Run("weekly", first.params, canon)
            base.created, base.checks = first.created, []
            base.merge(first)
            others_done = [first.id]
        else:
            others_done = []
        for i in others:
            base.merge(Run.load(i))
            others_done.append(i)
        base.week, base.status, base.stage = wid, "done", "ready"
        base.save(force=True)
        os.makedirs(os.path.join(RUNS, "_merged"), exist_ok=True)
        for i in others_done:
            shutil.move(os.path.join(RUNS, i + ".json"), os.path.join(RUNS, "_merged", i + ".json"))
        merged.append((canon, others_done))
    return merged


def list_runs():
    """Weekly runs newest week first, then archive searches newest first."""
    out = []
    if os.path.isdir(RUNS):
        for fn in os.listdir(RUNS):
            if fn.endswith(".json"):
                d = load_json(os.path.join(RUNS, fn), None)
                if d:
                    d.pop("papers", None)
                    if d["kind"] == "weekly":
                        d.setdefault("week", week_of(d["created"]))
                        d["title"] = week_label(d["week"])
                        d.setdefault("checks", [d["created"]])
                    out.append(d)
    out.sort(key=lambda d: d.get("week") or "", reverse=True)
    out.sort(key=lambda d: d["created"] if d["kind"] != "weekly" else "", reverse=True)
    out.sort(key=lambda d: d["kind"] != "weekly")
    return out


def slim(p):
    """Paper fields the UI needs."""
    keys = ("id", "journal", "abbr", "title", "authors", "doi", "url", "pub_date", "abstract",
            "keywords", "publisher", "verdict", "relevance", "topics", "method", "reason", "dl_status",
            "ai_verdict", "user_set",
            "dl_note", "file", "summary", "manual_url")
    return {k: p.get(k) for k in keys}


def mark_downloaded(run):
    have = downloaded_files()
    for p in run.papers.values():
        f = have.get(paper_key(p))
        if f:
            p["file"], p["dl_status"] = f, "ok"
            md = f[:-4] + ".md"
            p["summary"] = md if os.path.exists(os.path.join(ROOT, md)) else None


# ---------------------------------------------------------------- fetching

def set_j(run, emit, abbr, **kw):
    """Update one journal tile's fetch state and push it to the UI."""
    st = run.jstate.setdefault(abbr, {"abbr": abbr})
    st.update(kw)
    emit("journal", dict(st))


def fetch_weekly(run, emit, stop):
    seen_path = os.path.join(STATE, "seen.json")
    seen = load_json(seen_path, {})
    js = journals()
    fresh_all = []
    for j in js:
        set_j(run, emit, j["abbr"], state="queued", label="Waiting", done=0, total=0)
    for n, j in enumerate(js, 1):
        if stop.is_set():
            return
        emit("progress", {"stage": "fetching", "label": "Fetching %s" % j["abbr"], "done": n - 1, "total": len(js)})
        set_j(run, emit, j["abbr"], state="fetching", label="Reading feed")
        try:
            items = fx.fetch_journal(j)
        except Exception as e:
            run.errors.append("%s: %s" % (j["abbr"], str(e)[:120]))
            emit("log", {"msg": "%s feed failed: %s" % (j["abbr"], str(e)[:120]), "level": "warn"})
            set_j(run, emit, j["abbr"], state="failed", label="Feed failed")
            continue
        new = [p for p in items if p["id"] not in seen]
        pre_ids = [p["id"] for p in new]  # pre-lookup keys; remembered too, so a
        # paper whose DOI lookup fails next week isn't mistaken for a new one
        emit("log", {"msg": "%s: %d items, %d new" % (j["abbr"], len(items), len(new))})
        set_j(run, emit, j["abbr"], state="enriching", label="Looking up new papers",
              done=0, total=len(new))
        # lookups are network-bound: run a few in parallel (Crossref allows it;
        # http_get backs off on 429)
        with ThreadPoolExecutor(6) as ex:
            futs = [ex.submit(fx.enrich, p) for p in new]
            for k, fut in enumerate(as_completed(futs), 1):
                if stop.is_set():
                    for f in futs:
                        f.cancel()
                    return
                p = fut.result()
                set_j(run, emit, j["abbr"], done=k)
                if p["id"] in seen:  # DOI resolved to one we already had
                    continue
                p["source_mode"] = "weekly"
                if run.add_paper(p):
                    fresh_all.append(p)
                    emit("paper", slim(p))
        set_j(run, emit, j["abbr"], state="fetched", label="Fetched",
              found=sum(1 for p in run.papers.values() if p.get("abbr") == j["abbr"]))
        fx.dedupe(new, seen)
        for k in pre_ids:
            seen.setdefault(k, {"journal": j["name"], "alias": True})
        with open(seen_path, "w") as f:
            json.dump(seen, f, indent=1)
        run.save()
    # keep agent.py's --resume path working
    with open(os.path.join(STATE, "new_papers.json"), "w") as f:
        json.dump(fresh_all, f, indent=1)
    emit("progress", {"stage": "fetching", "label": "Fetched", "done": len(js), "total": len(js)})


def fetch_search(run, emit, stop):
    p = run.params
    js = [j for j in journals() if j["abbr"] in p["journals"]]
    for j in js:
        set_j(run, emit, j["abbr"], state="queued", label="Waiting", done=0, total=0)
    for n, j in enumerate(js, 1):
        if stop.is_set():
            return
        label = "Searching %s %s–%s" % (j["abbr"], p["year_from"], p["year_to"])
        emit("progress", {"stage": "fetching", "label": label, "done": n - 1, "total": len(js)})
        set_j(run, emit, j["abbr"], state="fetching", label="Querying Crossref")
        try:
            items = fx.crossref_journal_range(
                j.get("issn"), int(p["year_from"]), int(p["year_to"]),
                progress=lambda a, b, ab=j["abbr"]: set_j(run, emit, ab, done=a, total=b),
                doi_prefix=j.get("doi_prefix"), work_type=j.get("crossref_type", "journal-article"))
        except Exception as e:
            run.errors.append("%s: %s" % (j["abbr"], str(e)[:120]))
            emit("log", {"msg": "%s search failed: %s" % (j["abbr"], str(e)[:120]), "level": "warn"})
            set_j(run, emit, j["abbr"], state="failed", label="Search failed")
            continue
        skip = re.compile(r"^(editorial board|issue information|erratum|correction to|"
                          r"corrigendum|front matter|back matter|retraction)", re.I)
        y0, y1 = int(p["year_from"]), int(p["year_to"])
        items = [it for it in items if not skip.match(it["title"])
                 and y0 <= int((it.get("pub_date") or "0")[:4] or 0) <= y1]
        set_j(run, emit, j["abbr"], state="enriching", label="Filling missing abstracts")
        fill_abstracts(items, emit, stop, j["abbr"])
        for it in items:
            it.update({"journal": j["name"], "abbr": j["abbr"], "publisher": j["publisher"],
                       "source": "crossref", "source_mode": "search"})
            it["id"] = fx.key_for(it.get("doi"), j["name"], it["title"])
            if run.add_paper(it):
                emit("paper", slim(it))
        set_j(run, emit, j["abbr"], state="fetched", label="Fetched", found=len(items))
        emit("log", {"msg": "%s: %d articles (%d with abstract)"
                     % (j["abbr"], len(items), sum(1 for x in items if x.get("abstract")))})
        run.save()
    emit("progress", {"stage": "fetching", "label": "Fetched", "done": len(js), "total": len(js)})


def fill_abstracts(items, emit, stop, abbr):
    """OpenAlex by DOI for articles Crossref has no abstract for (JMIS, DSS).
    Gives up after repeated failures (keyless OpenAlex is rate-limited)."""
    todo = [it for it in items if not it.get("abstract") and it.get("doi")]
    if not todo:
        return
    emit("log", {"msg": "%s: looking up %d missing abstracts (OpenAlex)" % (abbr, len(todo))})
    fails = 0
    with ThreadPoolExecutor(4) as ex:
        futs = {ex.submit(fx.openalex_abstract_doi, it["doi"]): it for it in todo}
        for fut in as_completed(futs):
            if stop.is_set():
                break
            ab = fut.result()
            if ab:
                futs[fut]["abstract"] = ab
                fails = 0
            else:
                fails += 1
                if fails >= 8 and not fx.openalex_key():
                    emit("log", {"msg": "OpenAlex is rate-limiting; add a free OPENALEX_API_KEY to .env "
                                        "for JMIS/DSS abstracts. Screening those on title.", "level": "warn"})
                    for f in futs:
                        f.cancel()
                    break


# ---------------------------------------------------------------- screening

SCREEN_SCHEMA = {
    "type": "object",
    "properties": {"results": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "n": {"type": "integer"},
            "verdict": {"type": "string", "enum": ["download", "borderline", "skip"]},
            "relevance": {"type": "integer"},
            "topics": {"type": "array", "items": {"type": "string"}},
            "method": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["n", "verdict", "relevance", "topics", "method", "reason"],
        "additionalProperties": False}}},
    "required": ["results"],
    "additionalProperties": False,
}

SCREEN_SYSTEM = """You screen journal articles for an Information Systems researcher.

{criteria}
{method_rule}{focus}
For every paper return:
- verdict: "download" = meets every criterion above; "borderline" = adjacent or partly meets them;
  "skip" = does not. Editorials, errata and "Issue information" items are skip.
- relevance: 0-100.
- topics: the matched topic labels, exactly as written above (empty if none or skip).
- method: the paper's main methodology in 2-5 words, e.g. "Econometrics (DiD)", "Lab experiment",
  "Survey / SEM", "Design science", "Qualitative case study", "Analytical model", "ML prediction".
- reason: at most 20 words, specific to the paper.
If a paper has no abstract, judge from the title and prefer "borderline" over "download" unless the
title is unambiguous. Return one result per paper, using its number n."""


def screening_system(topics, query, method_names=None):
    lines = []
    if topics:
        lines.append("Topics (judge by meaning, not keyword overlap — the term lists are only examples):")
        for t in interests()["topics"]:
            label = t.get("label", t["name"])
            if label in topics:
                lines.append("- %s (e.g. %s)" % (label, ", ".join(t["terms"][:18])))
    else:
        lines.append("Topic: any Information Systems / management topic qualifies.")
    rule = ""
    chosen = [m for m in methods() if m.get("label", m["name"]) in (method_names or [])]
    if chosen:
        rule = ("\nMethod requirement: the paper's MAIN methodology must be one of:\n" +
                "\n".join("- %s: %s (e.g. %s)" % (m.get("label", m["name"]), m.get("description", ""),
                                                  ", ".join(m.get("terms", [])[:16])) for m in chosen) +
                "\nIf that method is only secondary (robustness check, small part of a mixed-methods "
                "design) the paper is at most \"borderline\"; another main method means \"skip\".\n")
    focus = ("\nThe researcher's specific focus for this search: \"%s\". A paper must fit this "
             "focus to be \"download\".\n" % query) if query else ""
    return SCREEN_SYSTEM.format(criteria="\n".join(lines), method_rule=rule, focus=focus)


def _screen_batch(system, batch, usage):
    items = []
    for n, p in enumerate(batch, 1):
        items.append({"n": n, "journal": p.get("abbr"), "year": (p.get("pub_date") or "")[:4],
                      "title": p.get("title"), "keywords": p.get("keywords"),
                      "abstract": (p.get("abstract") or "")[:1800] or None})
    for attempt in range(3):
        try:
            msg = client().beta.messages.create(
                model=MODEL, max_tokens=8000, betas=BETAS, fallbacks="default",
                thinking={"type": "adaptive"},
                output_config={"effort": "low",
                               "format": {"type": "json_schema", "schema": SCREEN_SCHEMA}},
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": json.dumps(items, ensure_ascii=False)}],
            )
        except anthropic.RateLimitError:
            time.sleep(10 * (attempt + 1))
            continue
        usage.add(msg.usage)
        if msg.stop_reason == "refusal":
            return {}
        text = "".join(b.text for b in msg.content if b.type == "text")
        try:
            res = json.loads(text)["results"]
        except (ValueError, KeyError):
            continue
        return {r["n"]: r for r in res if isinstance(r.get("n"), int)}
    return {}


def screen(run, emit, stop, ids=None, batch_size=25, workers=4):
    system = screening_system(run.params.get("topics"), run.params.get("query"),
                              run.params.get("methods"))
    todo = [run.papers[i] for i in (ids or run.order) if not run.papers[i].get("verdict")]
    batches = [todo[i:i + batch_size] for i in range(0, len(todo), batch_size)]
    done = 0
    emit("progress", {"stage": "screening", "label": "Screening with Sonnet 5.5", "done": 0, "total": len(todo)})
    valid = set(topic_labels())
    with ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(_screen_batch, system, b, run.usage): b for b in batches}
        for fut in as_completed(futs):
            if stop.is_set():
                for f in futs:
                    f.cancel()
                break
            batch, res = futs[fut], fut.result()
            for n, p in enumerate(batch, 1):
                r = res.get(n)
                if not r:
                    p["verdict"], p["relevance"], p["reason"], p["topics"] = "error", 0, "screening failed", []
                    p["method"] = None
                else:
                    p["verdict"], p["relevance"] = r["verdict"], r["relevance"]
                    p["topics"] = [t for t in r["topics"] if t in valid]
                    searched = run.params.get("topics") or []
                    if not p["topics"] and p["verdict"] != "skip" and len(searched) == 1:
                        p["topics"] = list(searched)
                    p["reason"] = r["reason"]
                    p["method"] = r.get("method")
                emit("verdict", {k: p.get(k) for k in ("id", "verdict", "relevance", "topics", "method", "reason")})
            done += len(batch)
            emit("progress", {"stage": "screening", "label": "Screening with Sonnet 5.5",
                              "done": done, "total": len(todo)})
            emit("cost", {"cost": round(run.usage.cost(), 4), "usage": run.usage.d})
            run.save()
    # remember weekly verdicts for agent.py
    if run.kind == "weekly":
        jpath = os.path.join(STATE, "judged.json")
        judged = load_json(jpath, {})
        for p in run.papers.values():
            if p.get("verdict") in ("download", "borderline"):
                judged[p["id"]] = {"verdict": p["verdict"], "topics": p.get("topics", []),
                                   "reason": p.get("reason", ""), "date": run.created[:10]}
        with open(jpath, "w") as f:
            json.dump(judged, f, indent=1)


# ---------------------------------------------------------------- download + summarize

SUMMARY_PROMPT = """Read this paper in full and write a markdown summary for an IS researcher
whose interests are: {interests}.

Use exactly this structure:

# {title}
**Authors**: ... | **Journal**: {journal} | **Year**: ... | **DOI**: {doi}
**Matched interests**: ...
## TL;DR
2-3 sentences.
## Research question & contribution
## Method
Data, setting, identification/design, sample size.
## Key findings
- one bullet per finding, with effect sizes where reported
## Implications
For research and for practice.
## Limitations & open questions
## Relevance to your interests
1-2 lines tying it to the matched interests.

Output only the markdown."""


def summarize(p, pdf_path, usage, template=None):
    """Summary of the full PDF, written to <pdf>.md. Goes through the AI layer (ai.py), so the provider and model come from
    Settings → AI ("Paper summaries") and the wording from the summary template chosen in Settings → Reader (default: IS brief)."""
    md_path = pdf_path[:-4] + ".md"
    if os.path.exists(md_path):
        return open(md_path).read()
    try:
        import ai
        import prefs
        prompt = prefs.summary_prompt(template or prefs.get_settings()["summary_template"], interests=", ".join(topic_labels()),
                                      title=p.get("title"), journal=p.get("journal"), doi=p.get("doi") or "n/a")
    except ImportError:
        ai = None
    if ai and prompt:
        res = ai.chat("summarize", [{"role": "user", "content": [{"type": "pdf", "path": pdf_path}, {"type": "text", "text": prompt}]}], max_tokens=16000)
        usage.add_result(res)
        text = res.text.strip()
        with open(md_path, "w") as f:
            f.write(text + "\n")
        return text
    data = base64.standard_b64encode(open(pdf_path, "rb").read()).decode()
    with client().beta.messages.stream(
        model=MODEL, max_tokens=16000, betas=BETAS, fallbacks="default",
        thinking={"type": "adaptive"}, output_config={"effort": "medium"},
        messages=[{"role": "user", "content": [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                            "data": data}},
            {"type": "text", "text": SUMMARY_PROMPT.format(
                interests=", ".join(topic_labels()), title=p.get("title"),
                journal=p.get("journal"), doi=p.get("doi") or "n/a")},
        ]}],
    ) as stream:
        msg = stream.get_final_message()
    usage.add(msg.usage)
    if msg.stop_reason == "refusal":
        raise RuntimeError("summary declined by the model")
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    with open(md_path, "w") as f:
        f.write(text + "\n")
    return text


def download_and_summarize(run, ids, emit, stop):
    env = dl.load_env()
    papers = [run.papers[i] for i in ids if i in run.papers]
    # same publisher back to back: after one manual "verify you are human" the
    # rest of that site's papers go through the same window
    papers.sort(key=lambda p: p.get("publisher") or "")
    total = len(papers)
    got = []
    emit("progress", {"stage": "downloading", "label": "Downloading", "done": 0, "total": total})
    def on_challenge(host, state):
        msg = {"waiting": "%s wants to check you're human. A browser window just opened: tick "
                          "“Verify you are human” (you have 3 minutes); the download then continues "
                          "by itself, and the site's other papers in this batch reuse the window." % host,
               "passed": "%s check passed; continuing in the background." % host,
               "failed": "%s check wasn't completed; that paper was skipped." % host}[state]
        emit("challenge", {"host": host, "state": state, "msg": msg})
        emit("log", {"msg": msg, "level": "warn" if state != "passed" else "ok"})

    with dl.open_browser(on_challenge=on_challenge) as br:
        for n, p in enumerate(papers, 1):
            if stop.is_set():
                break
            if p.get("file") and os.path.exists(os.path.join(ROOT, p["file"])):
                got.append(p)
                continue
            p["dl_status"] = "downloading"
            emit("download", {"id": p["id"], "dl_status": "downloading"})
            ok, path, note = dl.download_paper(dict(p), env, browser=br)
            if ok:
                p.update(dl_status="ok", file=os.path.relpath(path, ROOT), dl_note=note)
                got.append(p)
            else:
                own = re.sub(r"wrong paper via [^|]*", "", note)           # what the routes said, without what a refused PDF said about itself (its headline is not a status)
                p.update(dl_status="needs_library" if "needs_library_login" in own else
                         "unavailable" if "not_in_library" in own else
                         "not_yet" if "not found in library EBSCO" in own else
                         "needs_login" if "needs_login" in own else
                         "needs_check" if "challenge:" in own else
                         "wrong_paper" if "wrong paper" in note else "failed",
                         dl_note=note, manual_url=dl.browser_pdf_url(p))
            emit("download", {k: p.get(k) for k in ("id", "dl_status", "dl_note", "file", "manual_url")})
            emit("progress", {"stage": "downloading", "label": "Downloading", "done": n, "total": total})
            run.save()
    todo = [p for p in got if not p.get("summary")]
    for n, p in enumerate(todo, 1):
        if stop.is_set():
            break
        emit("progress", {"stage": "summarizing", "label": "Summarizing with Sonnet 5.5",
                          "done": n - 1, "total": len(todo)})
        p["dl_status"] = "summarizing"
        emit("download", {"id": p["id"], "dl_status": "summarizing", "file": p["file"]})
        try:
            summarize(p, os.path.join(ROOT, p["file"]), run.usage)
            p["summary"] = p["file"][:-4] + ".md"
            p["dl_status"] = "ok"
        except Exception as e:
            p["dl_status"], p["dl_note"] = "ok", "summary failed: %s" % str(e)[:100]
        emit("download", {k: p.get(k) for k in ("id", "dl_status", "dl_note", "file", "summary")})
        emit("cost", {"cost": round(run.usage.cost(), 4), "usage": run.usage.d})
        run.save()
    emit("progress", {"stage": "summarizing", "label": "Done", "done": len(todo), "total": len(todo)})
