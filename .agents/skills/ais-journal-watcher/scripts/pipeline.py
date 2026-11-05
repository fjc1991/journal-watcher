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
import hashlib
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
import signins  # noqa: E402
import terms as tm  # noqa: E402
import fetch_new as fx  # noqa: E402
import notes_store as ns  # noqa: E402
import folders as fo  # noqa: E402
import authorcache as ac  # noqa: E402
import readstate as rs  # noqa: E402
import ai  # noqa: E402

BASE, ROOT, STATE, PAPERS = dl.BASE, dl.ROOT, dl.STATE, dl.PAPERS
RUNS = os.path.join(STATE, "runs")

MODEL = "claude-sonnet-5-5"
BETAS = ["server-side-fallback-2026-07-01"]
# $ per million tokens (Claude Sonnet 5.5): only for the tokens of runs made before each model was counted on its own (Usage.d's top-level buckets), and the direct
# Anthropic path used when the AI layer is not installed
PRICE = {"in": 2.00, "out": 10.00, "cache_write": 2.50, "cache_read": 0.20}
EST_SCREEN_COST_PER_PAPER = 0.0015  # Claude Sonnet 5.5's price for what one paper sends and gets back (EST_SCREEN_CHARS, EST_SCREEN_OUT); see est_screen_cost_per_paper()
EST_SCREEN_CHARS, EST_SCREEN_OUT = 1500, 60      # one paper in a screening batch: its share of the prompt and the abstract (characters), and its verdict (tokens)


def est_screen_cost_per_paper():
    """What screening one paper costs with the model chosen for screening (Settings → AI), or None when its price is not known."""
    try:
        import ai
        return ai.estimate("screen", EST_SCREEN_CHARS, EST_SCREEN_OUT)
    except Exception:  # noqa: BLE001      (the AI layer is not installed: the direct Anthropic path screens with Claude Sonnet 5.5)
        return EST_SCREEN_COST_PER_PAPER

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


SSRN = {"name": "SSRN", "abbr": "SSRN", "publisher": "ssrn", "source": "ssrn", "issn": None, "doi_prefix": fx.SSRN_PREFIX, "crossref_type": "posted-content", "per_year": None,
        "archive": True, "weekly": False, "needs_query": True, "default_off": True, "limit": fx.SSRN_LIMIT}


# HBR's print issues can be searched by year (fetch_new.hbr_archive). A journals.json copied before that has HBR as "weekly checks only": it is read with these.
HBR_ARCHIVE = {"archive": True, "archive_source": "hbr_issues", "per_year": 120, "default_off": True,
               "archive_note": "Year searches read HBR's print issues (title, authors and a one-line summary). Web-only articles come only in the weekly check."}


def journals():
    """The journals of your journals.json, and SSRN: a place to search by words (an archive search with a focus phrase), never read in the weekly check and never selected
    by itself. It is added here so that a journals.json copied before SSRN existed has it too. HBR gets its year search (HBR_ARCHIVE) the same way."""
    js = load_json(os.path.join(BASE, "journals.json"), {"journals": []})["journals"]
    js = [dict(j, **HBR_ARCHIVE) if j.get("publisher") == "hbr" and not j.get("archive_source") else j for j in js]
    return js + [dict(SSRN)] if not any(j.get("abbr") == "SSRN" for j in js) else js


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
        """Add an ai.Result (any provider): counted per model ("models": {"provider · model": {calls, in, out, cost, unpriced}}), at that model's price; a call whose
        price is not known is counted in "unpriced", never as $0. An answer from the saved answers cost nothing and is not counted."""
        if getattr(r, "cached", False):
            return
        with self.lock:
            m = self.d.setdefault("models", {}).setdefault("%s · %s" % (r.provider, r.model), {"provider": r.provider, "model": r.model, "calls": 0, "in": 0, "out": 0, "cost": 0.0, "unpriced": 0})
            m["calls"] += 1
            m["in"] += int(r.usage.get("in", 0)) + int(r.usage.get("cache_read", 0)) + int(r.usage.get("cache_write", 0))
            m["out"] += int(r.usage.get("out", 0))
            if r.cost is None:
                m["unpriced"] += 1
            else:
                m["cost"] += r.cost

    def _legacy(self):
        """USD of the top-level buckets: tokens of Claude Sonnet 5.5 (runs from before models were counted apart, and the direct Anthropic path)."""
        return sum(self.d.get(k, 0) * PRICE[k] for k in PRICE) / 1e6

    def cost(self):
        """USD of every call whose price is known (see detail() for the calls without one)."""
        return self._legacy() + self.d.get("extra", 0) + sum(m.get("cost", 0) for m in (self.d.get("models") or {}).values())

    def detail(self):
        """What the top bar says about the run: {"cost", "unpriced" (calls without a known price), "models": [{name, provider, model, calls, in, out, cost, unpriced}]},
        the costliest first. A run from before models were counted apart shows its tokens as Claude Sonnet 5.5's, which they were."""
        import prices as pricing
        rows = []
        if any(self.d.get(k) for k in PRICE):
            rows.append({"name": pricing.name("anthropic", MODEL), "provider": "anthropic", "model": MODEL, "calls": None, "in": self.d.get("in", 0) + self.d.get("cache_read", 0) + self.d.get("cache_write", 0),
                         "out": self.d.get("out", 0), "cost": round(self._legacy(), 6), "unpriced": 0})
        if self.d.get("extra"):
            rows.append({"name": "other models", "provider": "", "model": "", "calls": None, "in": 0, "out": 0, "cost": round(self.d["extra"], 6), "unpriced": 0})
        for m in (self.d.get("models") or {}).values():
            rows.append({"name": pricing.name(m.get("provider"), m.get("model")), "provider": m.get("provider"), "model": m.get("model"), "calls": m.get("calls", 0),
                         "in": m.get("in", 0), "out": m.get("out", 0), "cost": round(m.get("cost", 0), 6), "unpriced": m.get("unpriced", 0)})
        rows.sort(key=lambda x: -x["cost"])
        return {"cost": round(self.cost(), 4), "unpriced": sum(x["unpriced"] for x in rows), "models": rows}


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
                if r.get("journal") == "Books":      # a book: its ISBN, publisher, pages and chapters are in the file beside it (books.py)
                    r["book"] = dl.load_json(os.path.join(ROOT, r["file"][:-4] + ".book.json"), {})
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
        self.checks = []   # weekly: timestamps of every check merged into this week; a search: every time it was run again
        self.reused = 0    # verdicts taken from earlier screening instead of asked for again (see screen)
        self.screen_error = ""     # why the last screening stopped early (budget, key, a model that cannot screen); "" once one finishes
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
        return "search_" + "_".join(bits) + "_" + hashlib.sha1(search_key(p).encode("utf-8")).hexdigest()[:6]     # (the journals and the focus phrase too: two searches started in the same second must not share a file)

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
        if p.get("terms"):
            return " · ".join([yrs, "terms: " + ", ".join(p["terms"])])
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
             "usage": self.usage.d, "cost": round(self.usage.cost(), 4), "cost_detail": self.usage.detail(),
             "week": self.week, "checks": self.checks,
             "updated": max(self.checks) if self.checks else self.created,
             "n_papers": len(self.order), "errors": self.errors[-20:], "jstate": self.jstate,
             "search_key": search_key(self.params) if self.kind == "search" else "", "reused": self.reused, "screen_error": self.screen_error,
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
        r.reused = int(d.get("reused") or 0)
        r.screen_error = str(d.get("screen_error") or "")
        for p in d.get("papers", []):
            if p.get("dl_status") in ("downloading", "summarizing"):
                # interrupted (dashboard closed mid-download): ready to retry
                p["dl_status"] = "ok" if p.get("file") and os.path.exists(os.path.join(ROOT, p["file"])) else None
                p["dl_note"] = "interrupted — press Download again"
            terms_rule(r, p)                    # a search by terms screened before 1.2.1: its skipped and borderline matches become downloads (the model's word is kept)
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


def search_key(p):
    """What makes two archive searches the same search: the years, the journals, the topics, the methods and the focus phrase (their order and the case of the phrase do not matter).
    The page works the same string out (index.html searchKey) to say that a search was already run."""
    if p.get("terms"):                      # a search by terms: the years, the journals and the terms (not your interests: editing them must not make it another search)
        return "%s-%s|%s|terms:%s" % (p.get("year_from"), p.get("year_to"), ",".join(sorted(p.get("journals") or [])),
                                      ",".join(sorted(t if tm.is_acronym(t) else t.lower() for t in p["terms"])))
    return "%s-%s|%s|%s|%s|%s" % (p.get("year_from"), p.get("year_to"), ",".join(sorted(p.get("journals") or [])), ",".join(sorted(p.get("topics") or [])),
                                   ",".join(sorted(p.get("methods") or [])), (p.get("query") or "").strip().lower())


def find_search(params):
    """The newest saved archive search that is the same search as `params`, or None. Pressing Search for a search you already ran continues that one (its papers, verdicts and
    downloads stay; the journals are asked again, and only papers without a verdict are screened) instead of starting a second copy and paying for it all again."""
    key = search_key(params)
    for d in list_runs():
        if d["kind"] == "search" and d.get("search_key") == key:
            return d
    return None


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
                    if d["kind"] == "search" and not d.get("search_key"):
                        d["search_key"] = search_key(d.get("params") or {})
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
            "ai_verdict", "user_set", "model_verdict",
            "dl_note", "file", "summary", "manual_url", "term_hits")
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
    js = [j for j in journals() if j.get("weekly", True)]
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


FETCHED = os.path.join(dl.LOCAL, "fetched")       # what a journal's year range returned, abstracts included (a saved copy: asking Crossref again takes minutes)
FETCH_KEEP_DAYS = 7        # a past year's articles hardly change ...
FETCH_KEEP_DAYS_NOW = 1    # ... but the current year's list grows every week


def _fetched_path(abbr, y0, y1, words=""):
    tag = "_" + hashlib.sha1(words.strip().lower().encode("utf-8")).hexdigest()[:8] if words else ""         # (a search by words: the words are part of what was asked)
    return os.path.join(FETCHED, "%s_%s_%s%s.json" % (re.sub(r"[^A-Za-z0-9]+", "", abbr or "x"), y0, y1, tag))


def saved_fetch(abbr, y0, y1, now=None, words=""):
    """(items, fetched_at) from the saved copy of this journal and year range, or (None, None) when there is none or it is older than FETCH_KEEP_DAYS days (a day when the
    range reaches the current year)."""
    d = load_json(_fetched_path(abbr, y0, y1, words), None)
    if not d or not d.get("items") or not d.get("at"):
        return None, None
    try:
        age = ((now or datetime.now(timezone.utc)) - datetime.fromisoformat(d["at"])).total_seconds() / 86400
    except ValueError:
        return None, None
    keep = FETCH_KEEP_DAYS_NOW if int(y1) >= (now or datetime.now(timezone.utc)).year else FETCH_KEEP_DAYS
    return (d["items"], d["at"]) if 0 <= age < keep else (None, None)


def save_fetch(abbr, y0, y1, items, words=""):
    os.makedirs(FETCHED, exist_ok=True)
    path = _fetched_path(abbr, y0, y1, words)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "items": items}, f, ensure_ascii=False)
    os.replace(tmp, path)


def fetch_search(run, emit, stop):
    """Every article of the chosen journals in the year range (a saved copy when there is a recent one), with abstracts filled in. A search by terms (params "terms")
    keeps only the articles whose title, abstract or keywords contain one of the terms (terms.py), each with what matched; the rest are not added to the run."""
    p = run.params
    js = [j for j in journals() if j["abbr"] in p["journals"]]
    match = tm.matcher(p["terms"]) if p.get("terms") else None

    def keep(it):
        if not match:
            return True
        h = match(it)
        if h:
            it["term_hits"] = h
            it["reason"] = tm.describe(h)
        return bool(h)
    for j in js:
        set_j(run, emit, j["abbr"], state="queued", label="Waiting", done=0, total=0)
    for n, j in enumerate(js, 1):
        if stop.is_set():
            return
        label = "Searching %s %s–%s" % (j["abbr"], p["year_from"], p["year_to"])
        emit("progress", {"stage": "fetching", "label": label, "done": n - 1, "total": len(js)})
        words = (p.get("query") or " ".join(p.get("terms") or [])) if j.get("source") == "ssrn" else ""
        saved, when = saved_fetch(j["abbr"], int(p["year_from"]), int(p["year_to"]), words=words)
        if saved:                                                 # asked before, not long ago: use the saved copy (abstracts included)
            if j.get("source") != "ssrn" and any(not it.get("abstract") and it.get("doi") for it in saved):
                fill_abstracts(saved, emit, stop, j["abbr"])      # a copy saved while OpenAlex could not be asked lacks abstracts: try again (a term would otherwise be missed)
                if not stop.is_set():
                    save_fetch(j["abbr"], int(p["year_from"]), int(p["year_to"]), saved, words=words)
            kept = 0
            for it in saved:
                it = dict(it)
                if keep(it):
                    kept += 1
                    if run.add_paper(it):
                        emit("paper", slim(it))
            set_j(run, emit, j["abbr"], state="fetched", label="Saved copy", found=kept, done=kept, total=kept)
            emit("log", {"msg": "%s: %d articles from your saved copy of %s (fetched %s)%s" % (j["abbr"], len(saved), "%s–%s" % (p["year_from"], p["year_to"]), when[:10],
                                                                                        "; %d contain your terms" % kept if match else "")})
            run.save()
            continue
        set_j(run, emit, j["abbr"], state="fetching", label="Reading HBR's issues" if j.get("archive_source") == "hbr_issues" else "Querying Crossref")
        try:
            if j.get("archive_source") == "hbr_issues":           # HBR: its print issues' contents pages (no DOIs, not in Crossref)
                items = fx.hbr_archive(int(p["year_from"]), int(p["year_to"]), progress=lambda a, b, ab=j["abbr"]: set_j(run, emit, ab, done=a, total=b))
            elif j.get("source") == "ssrn" and p.get("terms"):    # by terms: each term on its own (one query of all of them would favour papers with all), then together
                items, seen_ = [], set()
                for t in p["terms"]:
                    for it in fx.crossref_ssrn(t, int(p["year_from"]), int(p["year_to"]), limit=j.get("limit") or fx.SSRN_LIMIT):
                        if it["doi"] not in seen_:
                            seen_.add(it["doi"]); items.append(it)
            elif j.get("source") == "ssrn":                     # SSRN is only searched by words: the best matches of the focus phrase
                items = fx.crossref_ssrn(words, int(p["year_from"]), int(p["year_to"]), limit=j.get("limit") or fx.SSRN_LIMIT)
            else:
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
        kept = 0
        for it in items:
            it.update({"journal": j["name"], "abbr": j["abbr"], "publisher": j["publisher"],
                       "source": "hbr-issues" if j.get("archive_source") == "hbr_issues" else "crossref", "source_mode": "search"})
            it["id"] = fx.key_for(it.get("doi"), j["name"], it["title"])
            row = dict(it)                                     # (the saved copy keeps every article, without the marks of this search)
            if keep(row):
                kept += 1
                if run.add_paper(row):
                    emit("paper", slim(row))
        set_j(run, emit, j["abbr"], state="fetched", label="Fetched", found=kept if match else len(items))
        emit("log", {"msg": "%s: %d articles (%d with abstract)%s"
                     % (j["abbr"], len(items), sum(1 for x in items if x.get("abstract")), "; %d contain your terms" % kept if match else "")})
        if items and not stop.is_set():
            save_fetch(j["abbr"], y0, y1, items, words=words)
        run.save()
    emit("progress", {"stage": "fetching", "label": "Fetched", "done": len(js), "total": len(js)})


def fill_abstracts(items, emit, stop, abbr):
    """OpenAlex by DOI for articles Crossref has no abstract for (JMIS, DSS).
    Gives up after repeated failures (keyless OpenAlex is rate-limited)."""
    todo = [it for it in items if not it.get("abstract") and it.get("doi")]
    if not todo:
        return
    emit("log", {"msg": "%s: looking up %d missing abstracts (OpenAlex)" % (abbr, len(todo))})
    # many at a time first (one OpenAlex call per 50 DOIs: an NBER year of ~1,300 papers is 26 calls instead of 1,300, seconds instead of minutes), then one by one
    # for what those calls did not return
    by_doi = {it["doi"].lower(): it for it in todo}
    chunks = [list(by_doi)[i:i + 50] for i in range(0, len(by_doi), 50)]
    with ThreadPoolExecutor(4) as ex:
        futs = [ex.submit(fx.openalex_abstracts_dois, c) for c in chunks]
        for fut in as_completed(futs):
            if stop.is_set():
                for f in futs:
                    f.cancel()                                  # (Stop: the calls not yet started are dropped, not waited for)
                return
            for d, ab in (fut.result() or {}).items():
                if d in by_doi and ab:
                    by_doi[d]["abstract"] = ab
    todo = [it for it in todo if not it.get("abstract")]
    if not todo:
        return
    if fx.openalex_spent():
        emit("log", {"msg": "OpenAlex's daily allowance %s is used up, so the quick lookups were not possible; looking abstracts up one by one (slower). %s" % (
            "for your key" if fx.openalex_key() else "for keyless use from your network",
            "" if fx.openalex_key() else "A free OpenAlex key (Settings → Paper sources) gives you an allowance of your own."), "level": "warn"})
    fails = 0
    with ThreadPoolExecutor(8) as ex:
        futs = {ex.submit(fx.openalex_abstract_doi, it["doi"]): it for it in todo}
        for fut in as_completed(futs):
            if stop.is_set():
                for f in futs:
                    f.cancel()
                break
            ab = fut.result()
            if ab:
                futs[fut]["abstract"] = ab
                fails = 0
            else:
                fails += 1
                if fails >= 8 and not fx.openalex_key():
                    emit("log", {"msg": "OpenAlex is rate-limiting; add a free OpenAlex key in Settings → Paper sources "
                                        "for JMIS/DSS/NBER abstracts. Screening those on title.", "level": "warn"})
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


SCREEN_TEST_PAPER = {"n": 1, "journal": "TEST", "year": "2026", "title": "Digital nudges and contributions on an online platform: a field experiment",
                     "keywords": ["digital nudges", "platforms"], "abstract": "A randomised field experiment on a mobile feedback platform tests whether reminders increase user contributions."}


def screen_test(provider, model):
    """Settings → AI → "Screening new papers" → Test: one small screening request to this provider and model, with the same JSON schema as a real screening. A model that
    answers ordinary questions but cannot return verdicts in that schema (a summariser, say) is refused or answers in prose; either way it fails here instead of in a weekly
    check. {ok, ms, text, cost} or {ok: False, error}."""
    t = time.time()
    system = SCREEN_SYSTEM.format(criteria="Topic: any Information Systems / management topic qualifies.", method_rule="", focus="")
    try:
        res = ai.chat("screen", [{"role": "user", "content": json.dumps([SCREEN_TEST_PAPER], ensure_ascii=False)}], system=system, schema=SCREEN_SCHEMA, max_tokens=1500,
                      override={"provider": provider, "model": model})
    except ai.AIError as e:
        return {"ok": False, "error": "Can’t screen papers: the provider refused the request (%s)." % str(e).rstrip(".")}
    try:
        out = json.loads(res.text)["results"]
        r = next(x for x in out if isinstance(x, dict) and x.get("n") == 1)
        if r.get("verdict") not in ("download", "borderline", "skip"):
            raise ValueError("verdict %r" % r.get("verdict"))
    except (ValueError, KeyError, TypeError, StopIteration):
        return {"ok": False, "error": "Can’t screen papers: it answered, but not with the structured verdicts screening needs (%s)." % (
            re.sub(r"\s+", " ", res.text or "").strip()[:80] or "an empty answer"), "cost": res.cost}
    return {"ok": True, "ms": int((time.time() - t) * 1000), "text": "screens: %s, relevance %s" % (r["verdict"], r.get("relevance")), "cost": res.cost}


class BatchFailed(Exception):
    """One batch of papers could not be judged. `reason` is why, in the provider's own words where there are some: it is shown on each paper's card and in the log."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _screen_batch(system, batch, usage):
    """One batch of papers judged by the model of the "screen" feature (Settings → AI), through the AI layer: the monthly budget stops it, local-only mode applies, and
    every call is in the usage ledger. Returns {n: result}. Raises ai.Blocked when nothing more may be asked, or the ai.AIError of an answer that asking again cannot change
    (ai.retryable: a refused request, key, credit or model): the caller stops the whole screening on either. Raises BatchFailed (with the reason) when this batch could not be judged."""
    items = []
    for n, p in enumerate(batch, 1):
        items.append({"n": n, "journal": p.get("abbr"), "year": (p.get("pub_date") or "")[:4],
                      "title": p.get("title"), "keywords": p.get("keywords"),
                      "abstract": (p.get("abstract") or "")[:1800] or None})
    why = "no answer"
    for attempt in range(3):
        try:
            res = ai.chat("screen", [{"role": "user", "content": json.dumps(items, ensure_ascii=False)}], system=system, schema=SCREEN_SCHEMA, max_tokens=8000)
        except ai.Declined as e:
            if e.result is not None:
                usage.add_result(e.result)                     # a refusal is billed too
            raise BatchFailed(str(e) or "the model declined to judge these papers")      # a refusal is final: asking again would pay again for the same refusal
        except ai.Blocked:
            raise
        except ai.AIError as e:
            if not ai.retryable(e):                            # the same request would be refused the same way for every batch: stop the screening, say why
                raise
            why = str(e) or "the provider did not answer"      # the provider was busy or unreachable: wait and ask again
            if attempt < 2:
                time.sleep(10 * (attempt + 1))
            continue
        usage.add_result(res)
        try:
            out = json.loads(res.text)["results"]
        except (ValueError, KeyError, TypeError):
            why = "the model's answer was not the JSON that screening asks for (%s)" % (re.sub(r"\s+", " ", res.text or "").strip()[:80] or "it was empty")
            continue
        return {r["n"]: r for r in out if isinstance(r, dict) and isinstance(r.get("n"), int)}
    raise BatchFailed(why)


VERDICTS = os.path.join(STATE, "verdicts.json")      # {fingerprint: {paper id: {h, v, rel, topics, method, reason, at}}}: what the screening said, so a paper is not paid for twice
_verdict_lock = threading.Lock()
KEEP_VERDICTS_DAYS = 400


def reuse_days():
    """How many days an earlier screening verdict is believed (Settings → AI & privacy → Privacy and spending); 0 = always ask again."""
    try:
        return max(0, min(3650, int(((ai.config_data().get("screening") or {}).get("reuse_days", 90)))))
    except (TypeError, ValueError, OSError):
        return 90


def screen_model():
    """The model that screens, as Settings → AI sets it. Claude Sonnet 5.5 is named as before (so verdicts saved earlier still count); another provider or model by name."""
    prov, model = ai.feature_model("screen")
    return MODEL if (prov, model) == ("anthropic", MODEL) else "%s/%s" % (prov, model)


def screen_fingerprint(system):
    """A verdict answers one question: the model and the whole instruction it was given (topics, method, focus phrase). Another question gets its own verdicts."""
    return hashlib.sha256((screen_model() + "\n" + system).encode("utf-8")).hexdigest()[:16]


def paper_digest(p):
    """What the model was shown of the paper. A verdict is reused only for the same text (an abstract that arrived later is a different question)."""
    s = json.dumps([p.get("abbr"), (p.get("pub_date") or "")[:4], p.get("title"), p.get("keywords"), (p.get("abstract") or "")[:1800] or None], ensure_ascii=False)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def known_verdicts(fp, papers, days, now=None):
    """{paper id: cached verdict} for the papers whose verdict for this question is saved, still fresh and about the same text."""
    if days <= 0:
        return {}
    now = now or datetime.now(timezone.utc)
    with _verdict_lock:
        mine = (load_json(VERDICTS, {}) or {}).get(fp) or {}
    out = {}
    for p in papers:
        e = mine.get(p["id"])
        if not e or e.get("h") != paper_digest(p) or e.get("v") not in ("download", "borderline", "skip"):
            continue
        try:
            if (now - datetime.fromisoformat(e["at"])).total_seconds() < days * 86400:
                out[p["id"]] = e
        except (KeyError, ValueError):
            continue
    return out


def terms_rule(run, p):
    """A search by terms: every paper in it contains one of your terms (in its title, abstract or keywords: terms.py), and that alone makes it a paper to download.
    "Screen with AI" still gives each one topics, the method, a relevance (which orders the list) and a reason. Its own download / borderline / skip is kept as
    model_verdict (the card shows it when it is not "download") and never takes a match out of the download list. Applied once per verdict of the model: a paper
    that already has model_verdict, or whose verdict you set yourself, is left as it is."""
    if not (run.params.get("terms") and p.get("term_hits")) or p.get("user_set") or "model_verdict" in p:
        return
    if p.get("verdict") in ("download", "borderline", "skip"):
        p["model_verdict"], p["verdict"] = p["verdict"], "download"


def remember_verdicts(fp, papers, now=None):
    """Keep the verdicts the model just gave (never an error; in a search by terms the model's own word, not the download the terms rule made of it). Old ones, and
    old questions that nothing has asked lately, are dropped."""
    now = now or datetime.now(timezone.utc)
    at = now.isoformat(timespec="seconds")
    with _verdict_lock:
        d = load_json(VERDICTS, {}) or {}
        mine = d.setdefault(fp, {})
        for p in papers:
            v = p.get("model_verdict") or p.get("verdict")
            if v in ("download", "borderline", "skip"):
                mine[p["id"]] = {"h": paper_digest(p), "v": v, "rel": p.get("relevance"), "topics": p.get("topics") or [], "method": p.get("method"),
                                 "reason": p.get("reason") or "", "at": at}
        cut = (now - timedelta(days=KEEP_VERDICTS_DAYS)).isoformat(timespec="seconds")
        for q in list(d):
            d[q] = {i: e for i, e in d[q].items() if (e.get("at") or "") >= cut}
            if not d[q]:
                del d[q]
        os.makedirs(os.path.dirname(VERDICTS), exist_ok=True)
        tmp = VERDICTS + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, VERDICTS)


def screen(run, emit, stop, ids=None, batch_size=25, workers=4):
    system = screening_system(run.params.get("topics"), run.params.get("query"),
                              run.params.get("methods"))
    todo = [run.papers[i] for i in (ids or run.order) if not run.papers[i].get("verdict")]
    fp = screen_fingerprint(system)
    known = known_verdicts(fp, todo, reuse_days())
    if known:                                                  # asked before, with the same question and the same text, not long ago: no second payment
        for p in todo:
            e = known.get(p["id"])
            if e:
                p["verdict"], p["relevance"], p["topics"], p["method"], p["reason"] = e["v"], e.get("rel") or 0, e.get("topics") or [], e.get("method"), e.get("reason") or ""
                p.pop("model_verdict", None)
                terms_rule(run, p)
                emit("verdict", {k: p.get(k) for k in ("id", "verdict", "relevance", "topics", "method", "reason", "model_verdict")})
        todo = [p for p in todo if p["id"] not in known]
        run.reused += len(known)
        emit("log", {"msg": "%d papers already had a verdict from an earlier screening with the same question%s" % (len(known), " (about $%.2f not paid for again)" % (len(known) * est) if (est := est_screen_cost_per_paper()) is not None else "")})
        run.save(force=True)
    batches = [todo[i:i + batch_size] for i in range(0, len(todo), batch_size)]
    done, blocked = 0, None
    emit("progress", {"stage": "screening", "label": "Screening", "done": 0, "total": len(todo)})
    valid = set(topic_labels())
    halt = threading.Event()             # set by the worker whose batch met Blocked or a refused request: a batch that has not started yet is then not sent (the batches already
                                         # on their way finish; cancelling the waiting futures from this thread alone would let a free worker start one more first)

    def one(b):
        if halt.is_set() or stop.is_set():
            return None                  # never sent: the papers stay unscreened
        try:
            return _screen_batch(system, b, run.usage)
        except ai.AIError:
            halt.set()
            raise

    with ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(one, b): b for b in batches}
        for fut in as_completed(futs):
            if stop.is_set():
                for f in futs:
                    f.cancel()
                break
            batch = futs[fut]
            if fut.cancelled():                                # cancelled below once nothing more may be asked: never started, not paid for
                continue
            why = ""
            try:
                res = fut.result()
            except ai.AIError as e:                            # Blocked (budget, local-only, no key) or a request the provider refuses for every batch alike (model, key, credit):
                for f in futs:                                 # the papers stay unscreened, to pick up later ("Retry screening"), instead of each being marked as failed
                    f.cancel()
                if blocked is None:
                    blocked = e
                continue
            except BatchFailed as e:
                res, why = {}, e.reason
                emit("log", {"msg": "Screening failed for %d papers: %s" % (len(batch), why[:300]), "level": "warn"})
            if res is None:                                    # not sent (see one): left unscreened, to pick up later
                continue
            for n, p in enumerate(batch, 1):
                r = res.get(n)
                if not r:
                    reason = "screening failed: " + (why or ("the model's answer had no verdict for this paper" if res else "no answer"))
                    p["verdict"], p["relevance"], p["reason"], p["topics"] = "error", 0, reason[:400], []
                    p["method"] = None
                else:
                    p["verdict"], p["relevance"] = r["verdict"], r["relevance"]
                    p["topics"] = [t for t in r["topics"] if t in valid]
                    searched = run.params.get("topics") or []
                    if not p["topics"] and p["verdict"] != "skip" and len(searched) == 1:
                        p["topics"] = list(searched)
                    p["reason"] = r["reason"]
                    p["method"] = r.get("method")
                    p.pop("model_verdict", None)
                    terms_rule(run, p)
                emit("verdict", {k: p.get(k) for k in ("id", "verdict", "relevance", "topics", "method", "reason", "model_verdict")})
            done += len(batch)
            emit("progress", {"stage": "screening", "label": "Screening",
                              "done": done, "total": len(todo)})
            emit("cost", {"cost": round(run.usage.cost(), 4), "usage": run.usage.d, "detail": run.usage.detail()})
            remember_verdicts(fp, [p for p in batch if p.get("verdict") != "error"])
            run.save()
    if blocked is not None:
        left = len([p for p in todo if not p.get("verdict")])
        hint = ""
        if not isinstance(blocked, ai.Blocked) and getattr(blocked, "status", None) in (400, 404, 422):
            hint = " The model chosen for screening (Settings → AI → Screening new papers) may not be able to answer in the structured JSON that screening asks for: its Test button there checks that."
        run.screen_error = ("%s%s" % (blocked, hint))[:600]          # shown on the run's page until a screening finishes without stopping
        emit("log", {"msg": "Screening stopped with %d papers not screened: %s%s Once that is fixed, press “Retry screening” (or search again): the papers already judged are kept, and only the others are screened." % (left, blocked, hint), "level": "warn"})
        run.save(force=True)
    elif not stop.is_set():
        run.screen_error = ""
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
        if not text:                    # an empty file would count as a summary for ever (the "Ready to read" bar, and nothing would ask again)
            raise ai.AIError("%s · %s returned an empty summary" % (res.provider, res.model))
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


# How long to leave between two papers from the same publisher, in seconds: (sites known for "verify you are human" checks, other sites). Fetching a site's PDFs
# back to back is what makes it suspect a robot; spacing them out (with some randomness) is what a person reading would do. Settings → Library access → Download
# pace (library.json "pace"). These are cautious guesses, not published limits: no publisher says how many downloads trigger its check.
PACES = {"careful": (45, 20), "normal": (20, 8), "fast": (2, 0)}
CHECKY = {"elsevier", "wiley", "tandfonline", "informs", "aaas", "pnas", "ssrn"}       # behind Cloudflare-style checks (ScienceDirect above all)
SITE_NAMES = {"elsevier": "ScienceDirect", "wiley": "Wiley", "tandfonline": "Taylor & Francis", "informs": "INFORMS", "aaas": "Science", "pnas": "PNAS",
              "aisel": "AIS eLibrary", "acm": "ACM", "nber": "NBER", "hbr": "HBR", "ssrn": "SSRN"}


def pace_gap(publisher, pace, challenged=False, rnd=None):
    """Seconds to wait before the next paper from `publisher`: the pace's base gap, times 0.75–1.35 at random (a fixed rhythm is itself robot-like), doubled for a
    site that has already asked for a human check in this batch."""
    import random
    base = PACES.get(pace, PACES["careful"])[0 if publisher in CHECKY else 1]
    return base * (rnd or random.uniform)(0.75, 1.35) * (2 if challenged else 1)


def _summarize_one(run, p, emit):
    """Write one paper's summary (the reader's Summary tab, and the "Ready to read" bar in the Library); a failure is noted on the paper, never raised."""
    p["dl_status"] = "summarizing"
    emit("download", {"id": p["id"], "dl_status": "summarizing", "file": p["file"]})
    try:
        summarize(p, os.path.join(ROOT, p["file"]), run.usage)
        p["summary"] = p["file"][:-4] + ".md"
        p["dl_status"] = "ok"
    except Exception as e:
        p["dl_status"], p["dl_note"] = "ok", "summary failed: %s" % str(e)[:100]
    emit("download", {k: p.get(k) for k in ("id", "dl_status", "dl_note", "file", "summary")})
    emit("cost", {"cost": round(run.usage.cost(), 4), "usage": run.usage.d, "detail": run.usage.detail()})
    run.save()


def download_and_summarize(run, ids, emit, stop, sleep=None, clock=None):
    """Download the papers `ids` (one browser, a site's papers together), then summarise them. Papers from the same publisher are spaced out (pace_gap); the
    waiting time is used to summarise papers already downloaded, so a careful pace costs little time overall. `sleep`/`clock` are for the tests."""
    env = dl.load_env()
    sleep = sleep or (lambda t: stop.wait(t))
    clock = clock or time.monotonic
    pace = (signins.profile().get("pace") or "careful")
    last_at, challenged = {}, set()
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
        if state == "waiting" and current.get("pub"):
            challenged.add(current["pub"])                  # that site is watching: leave more time between its papers for the rest of the batch
    current = {}

    with dl.open_browser(on_challenge=on_challenge) as br:
        for n, p in enumerate(papers, 1):
            if stop.is_set():
                break
            if p.get("file") and os.path.exists(os.path.join(ROOT, p["file"])):
                got.append(p)
                continue
            pub = p.get("publisher") or "other"
            if pub in last_at:
                due = last_at[pub] + pace_gap(pub, pace, pub in challenged)
                if clock() < due:
                    emit("log", {"msg": "Waiting %d s before the next %s paper (%s download pace, so the site does not take Journal Watcher for a robot)." % (
                        round(due - clock()), SITE_NAMES.get(pub, pub), pace)})
                while clock() < due and not stop.is_set():
                    ready = next((q for q in got if not q.get("summary") and q.get("dl_status") == "ok"), None)
                    if ready and due - clock() > 5:
                        _summarize_one(run, ready, emit)         # use the wait: this paper becomes "Ready to read" sooner
                    else:
                        sleep(min(1.0, max(0.0, due - clock())))
                if stop.is_set():
                    break
            current["pub"] = pub
            p["dl_status"] = "downloading"
            emit("download", {"id": p["id"], "dl_status": "downloading"})
            ok, path, note = dl.download_paper(dict(p), env, browser=br)
            if ok:
                p.update(dl_status="ok", file=os.path.relpath(path, ROOT), dl_note=note)
                got.append(p)
            else:
                own = re.sub(r"wrong paper via [^|]*", "", note)           # what the routes said, without what a refused PDF said about itself (its headline is not a status)
                p.update(dl_status="needs_library" if ("needs_library_login" in own or "library_setup:" in own) else
                         "unavailable" if "not_in_library" in own else
                         "needs_login" if "needs_login" in own else           # (before "not yet in EBSCO": EBSCO is now asked for any paper, so its "not found" must not hide a sign-in the paper needs)
                         "not_yet" if "not found in library EBSCO" in own else
                         "needs_check" if "challenge:" in own else
                         "wrong_paper" if "wrong paper" in note else "failed",
                         dl_note=note, manual_url=dl.browser_pdf_url(p))
            emit("download", {k: p.get(k) for k in ("id", "dl_status", "dl_note", "file", "manual_url")})
            emit("progress", {"stage": "downloading", "label": "Downloading", "done": n, "total": total})
            last_at[pub] = clock()
            run.save()
    todo = [p for p in got if not p.get("summary")]
    for n, p in enumerate(todo, 1):
        if stop.is_set():
            break
        emit("progress", {"stage": "summarizing", "label": "Summarizing with Sonnet 5.5",
                          "done": n - 1, "total": len(todo)})
        _summarize_one(run, p, emit)
    emit("progress", {"stage": "summarizing", "label": "Done", "done": len(todo), "total": len(todo)})
