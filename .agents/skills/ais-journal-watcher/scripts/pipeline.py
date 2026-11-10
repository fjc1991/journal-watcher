"""Shared engine for the dashboard (and agent.py): runs, fetching, Sonnet
screening, downloading and summarizing — all reporting progress through an
`emit(event, data)` callback so a UI can show it live.

Run kinds:
  weekly  — latest items from all 7 journals, minus everything in seen.json
  search  — every article of chosen journals in a year range (Crossref);
            never touches seen.json
"""
import base64
import copy
import csv
import hashlib
import unicodedata
import urllib.parse
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
import kinds  # noqa: E402
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


SCREEN_HISTORY_MIN = 50        # papers on record (sent to the same provider and model) before the estimate rests on what they cost: two full batches
SCREEN_HISTORY_CALLS = 200     # only the most recent screening calls count: a price or a prompt that changed long ago should not hold the estimate


def screen_history(provider, model):
    """What your earlier screening with this provider and model cost, from the usage ledger: (papers sent, mean cost per paper). Only the calls that say how many
    papers they carried (so not a Settings Test, nor a call made before the ledger recorded it) and that have a price; an answer that could not be used was paid for
    too, so it counts. A request that repeats papers already sent (the one resend, the halves of a batch that was split) carries 0: its cost counts, its papers are
    not counted a second time, so the mean is what was paid per different paper."""
    import ai
    rows = [r for r in ai._rows() if r.get("feature") == "screen" and r.get("provider") == provider and r.get("model") == model
            and type(r.get("papers")) is int and r["papers"] >= 0 and not r.get("cached") and not r.get("cancelled")
            and isinstance(r.get("cost"), (int, float))][-SCREEN_HISTORY_CALLS:]
    n = sum(r["papers"] for r in rows)
    return n, (sum(r["cost"] for r in rows) / n if n else None)


def screen_estimate(provider=None, model=None):
    """(what screening one paper costs, what that rests on) with the model chosen for screening (Settings → AI), or the one given: "history" = the mean of what your
    earlier screenings with it cost per paper, once SCREEN_HISTORY_MIN papers are on record; else "prices" = its price for EST_SCREEN_CHARS in and EST_SCREEN_OUT out;
    (None, None) when neither is known."""
    try:
        import ai
        if not model:
            provider, model = ai.feature_model("screen")
        if not model:
            return None, None
        n, mean = screen_history(provider, model)
        if n >= SCREEN_HISTORY_MIN:
            return mean, "history"
        per = ai.cost_of(model, {"in": int(EST_SCREEN_CHARS / 3.3), "out": EST_SCREEN_OUT}, 0, provider)
        return per, ("prices" if per is not None else None)
    except Exception:  # noqa: BLE001      (the AI layer is not installed: the direct Anthropic path screens with Claude Sonnet 5.5)
        return EST_SCREEN_COST_PER_PAPER, "prices"


def est_screen_cost_per_paper(provider=None, model=None):
    """What screening one paper costs with the model chosen for screening (or the one given), or None when it is not known: see screen_estimate."""
    return screen_estimate(provider, model)[0]

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


# ---------------------------------------------------------------- the library list
# Nothing here is trusted without asking the disk: every call stats the files it depends on, and only what changed is read again. Another process, or another computer
# through Dropbox, changes these files without telling this one.

_LIB_LOCK = threading.RLock()      # one thread at a time checks the disk and builds rows (no other lock is taken while it is held)
_LIB_FILES = {}                    # (path, tag) -> (signature, settled, value): what a file said when it had this mtime, size and inode
LIB_RACY_NS = 2_000_000_000        # a file changed less than this long before it was read is read again next time: a second write within the timestamp's resolution
                                   # (1 s on HFS+, 2 s on exFAT, a few ms on ext4) with the same size would otherwise look unchanged
LIB_STATS = {"reads": 0}           # how often a file was parsed (the tests look at it)


def _lib_sig(path):
    """(mtime in ns, size, inode) of a file or folder, None when it is not there."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def _lib_file(path, load, default=None, tag=None):
    """load(path), read again only when the file's signature changed (or it was too new to trust: LIB_RACY_NS); `default` when the file is not there.
    What load raises is raised (and nothing is kept), as before."""
    k = (path, tag)
    sig = _lib_sig(path)
    if sig is None:
        _LIB_FILES.pop(k, None)
        return default
    hit = _LIB_FILES.get(k)
    if hit and hit[0] == sig and hit[1]:
        return hit[2]
    now = time.time_ns()
    LIB_STATS["reads"] += 1
    val = load(path)
    _LIB_FILES[k] = (sig, sig[0] < now - LIB_RACY_NS, val)
    return val


def _lib_names(d):
    """The names in a folder (nothing is kept: a folder's own date is not trusted to say that a file was added or removed in it)."""
    try:
        return frozenset(os.listdir(d))
    except OSError:
        return frozenset()


def _lib_read_index(idx):
    """The library's rows of Papers/_index.csv as (rows, by_file): each row is (the row as read, with the plain journal and title; its key; its year; the folder
    the PDF is in; the PDF's name; the PDF's full path). by_file: Papers/… path -> the last such row (the one library() lists first)."""
    out, by_file = [], {}
    with open(idx, newline="") as f:
        for r in csv.DictReader(f):
            if r["status"] != "ok" or not r["file"]:
                continue
            key = dl.index_key(r)                # (from the title as stored: folders, reading status and runs know a paper without a DOI by it)
            r["journal"], r["title"] = dl.plain(r.get("journal")), dl.plain(r.get("title"))      # (shown with "&" for "&amp;"; the key above is unchanged)
            m = re.match(r"Papers/(\d{4})-\d{2}/", r["file"])          # Papers/YYYY-MM/<Journal>/... -> publication year for grouping
            pdf = os.path.join(ROOT, r["file"])
            b = (r, key, m.group(1) if m else "", os.path.dirname(pdf), os.path.basename(pdf), pdf)
            out.append(b)
            by_file[r["file"]] = b
    return out, by_file


def _lib_member():
    """folders.membership(), read again only when a folder file was added, removed or changed."""
    try:
        sig = tuple(sorted((fn,) + (_lib_sig(os.path.join(fo.DIR, fn)) or ()) for fn in os.listdir(fo.DIR) if fn.endswith(".json") and "." not in fn[:-5]))
    except OSError:
        sig = ()
    hit = _LIB_FILES.get((fo.DIR, "member"))
    if hit and hit[0] == sig and hit[1]:
        return hit[2]
    now = time.time_ns()
    LIB_STATS["reads"] += 1
    val = fo.membership()
    _LIB_FILES[(fo.DIR, "member")] = (sig, all(len(s) == 4 and s[1] < now - LIB_RACY_NS for s in sig), val)
    return val


def _lib_notes(path):
    return ns.counts(path[:-len(".notes.json")] + ".pdf")


def _lib_own(v):
    """A copy the caller may change: what is kept is never handed out."""
    return dict(v) if type(v) is dict and not any(type(x) in (dict, list) for x in v.values()) else copy.deepcopy(v)


def _lib_row(b, state, member, dirs):
    """One library row, new every time: the caller may change it, and whatever is in it (reading status, folders, notes counts, a book's details), without changing
    what the next caller gets. dirs: the folders already listed for this call ({} to start with)."""
    base, key, year, d, name, pdf = b
    names, stem = dirs.get(d), pdf[:-4]
    if names is None:
        names = dirs[d] = _lib_names(d)
    if name in names:                   # the folder's list answers "is there a summary, notes, a card…" for every paper in it at once
        has = lambda ext: name[:-4] + ext in names    # noqa: E731
    else:                                        # the PDF is not there under exactly this name (another case or accent form on a Mac, or gone): ask the disk, as before
        has = lambda ext: os.path.exists(stem + ext)    # noqa: E731
    r = dict(base)
    if None in r:                                # (a line of the index with more fields than columns: csv keeps the rest as a list)
        r[None] = list(r[None])
    md = r["file"][:-4] + ".md"
    r["summary"] = md if has(".md") else None
    n = _lib_file(stem + ".notes.json", _lib_notes) if has(".notes.json") else None     # reader highlights/cards, None if none
    r["notes"] = dict(n) if n else None
    r["card"] = has(".card.json")                # paper card (papercard.py)
    r["skim"] = has(".skim.json")                # skim layer (skim.py)
    st = state.get(key)
    r["key"], r["state"], r["folders"] = key, _lib_own(st) if st else {}, list(member.get(key, []))
    if r.get("journal") == "Books":              # a book: its ISBN, publisher, pages and chapters are in the file beside it (books.py)
        r["book"] = copy.deepcopy(_lib_file(stem + ".book.json", lambda p: dl.load_json(p, {}), {})) if has(".book.json") else {}
    side = _lib_file(stem + ".paper.json", lambda p: kinds.read_sidecar(pdf, key), {}, key) if has(".paper.json") else {}
    r["kind"], r["kind_label"] = kinds.of_row(r, side)     # journal article, preprint, conference paper, other (kinds.py)
    r["year"] = year
    return r


def _lib_parts():
    idx = os.path.join(PAPERS, "_index.csv")
    rows, by_file = _lib_file(idx, _lib_read_index, ([], {}), ROOT)
    if len(_LIB_FILES) > 4 * len(rows) + 2000:       # (papers deleted or moved leave entries behind: start again rather than grow)
        _LIB_FILES.clear()
        rows, by_file = _lib_file(idx, _lib_read_index, ([], {}), ROOT)
    return rows, by_file, _lib_file(rs.PATH, lambda p: rs.load(), {}), _lib_member()   # reading status (Papers/_status.json) and virtual folders (Papers/_folders/)


def library():
    with _LIB_LOCK:
        rows, _, state, member = _lib_parts()
        dirs = {}
        out = [_lib_row(b, state, member, dirs) for b in rows]
    return ac.apply(out)[::-1]         # authors the index lacks come from the Crossref cache (authorcache.py)


def library_row(file):
    """The library row of one paper by its Papers/… path, None when it is not in the library: what next(r for r in library() if r["file"] == file) gives,
    without building the other rows."""
    if not isinstance(file, str):
        return None
    with _LIB_LOCK:
        _, by_file, state, member = _lib_parts()
        b = by_file.get(file)
        if b is None:
            return None
        row = _lib_row(b, state, member, {})
    return ac.apply([row])[0]


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


_SAVE_LOCK = threading.RLock()


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
        self.screen_backup = {}    # the backup model that screened this run because the chosen one cannot answer in structured JSON (screen); {} when the chosen one did
        self._last_save = 0

    def slug(self):
        if self.kind == "weekly":
            return "weekly"
        if self.kind == "picks":
            return "picks"
        if self.kind == "author":         # the author's name, and their ids (two people of the same name get two files)
            p = self.params
            return "author_" + (re.sub(r"[^a-z0-9]+", "", unicodedata.normalize("NFKD", p.get("label") or "").encode("ascii", "ignore").decode().lower())[:20] or "x") \
                + "_" + hashlib.sha1("|".join(sorted(p.get("author_ids") or [])).encode()).hexdigest()[:6]
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
        if self.kind == "author":       # an author's works (authors.py), downloaded into their folder
            return "Author · " + (self.params.get("label") or "an author")
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
             "search_key": search_key(self.params) if self.kind == "search" else "", "reused": self.reused, "screen_error": self.screen_error, "screen_backup": self.screen_backup,
             "n_download": sum(1 for p in self.papers.values() if p.get("verdict") == "download"),
             "n_borderline": sum(1 for p in self.papers.values() if p.get("verdict") == "borderline")}
        if full:
            d["papers"] = [self.papers[i] for i in self.order]
        return d

    def save(self, force=False):
        """Write the run to RUNS/<id>.json. A job's thread and a request's thread (a verdict, a PDF you added) can save the same run at once: one write at a
        time, each through its own temporary file, and the papers are turned into text before anything is written."""
        if not force and time.time() - self._last_save < 2:
            return
        with _SAVE_LOCK:
            self._last_save = time.time()
            os.makedirs(RUNS, exist_ok=True)
            for n in range(5):
                try:
                    text = json.dumps(self.to_dict(), ensure_ascii=False)
                    break
                except RuntimeError:                         # (a paper got a new field while it was read: read it again)
                    if n == 4:
                        raise
                    time.sleep(0.01)
            dest = os.path.join(RUNS, self.id + ".json")
            tmp = dl.tmp_path(dest)
            with open(tmp, "w") as f:
                f.write(text)
            os.replace(tmp, dest)

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
        r.screen_backup = d.get("screen_backup") if isinstance(d.get("screen_backup"), dict) else {}
        for p in d.get("papers", []):
            if p.get("dl_status") in ("downloading", "summarizing"):
                # interrupted (dashboard closed mid-download): ready to retry
                p["dl_status"] = "ok" if p.get("file") and os.path.exists(os.path.join(ROOT, p["file"])) else None
                p["dl_note"] = "interrupted — press Download again"
            elif p.get("dl_status") == "needs_check" and str(p.get("dl_note") or "").startswith("waiting for you"):     # (this version's note, or an earlier one's)
                p["dl_note"] = NEEDS_CHECK_NOTE % SITE_NAMES.get(p.get("publisher"), p.get("publisher") or "Its site")     # (the app quit while it waited)
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
            "dl_note", "file", "summary", "manual_url", "term_hits", "summary_by_hand", "dl_queued")
    return {k: p.get(k) for k in keys}


DELETED_NOTE = "You deleted this paper from your library: it is not downloaded again (add it yourself if you want it back)."


def mark_downloaded(run):
    """Papers of the run that are in the library get their file; papers you deleted are marked "deleted" (never downloaded again by a run)."""
    import deletion
    have, gone = downloaded_files(), deletion.deleted_keys()
    for p in run.papers.values():
        k = paper_key(p)
        f = have.get(k)
        if f:
            came = p.get("dl_status") != "ok" or p.get("file") != f
            p["file"], p["dl_status"] = f, "ok"
            md = f[:-4] + ".md"
            p["summary"] = md if os.path.exists(os.path.join(ROOT, md)) else None
            if came and p.get("folder"):                    # (it waited for a folder and came another way: another run, the Library's Add, a PDF dropped elsewhere)
                _into_folder(p, k)
        elif k in gone:
            p["file"], p["summary"], p["dl_status"], p["dl_note"] = None, None, "deleted", DELETED_NOTE
        elif p.get("dl_status") == "deleted":               # (no longer deleted: the row was replaced when you added the paper again elsewhere)
            p["dl_status"], p["dl_note"] = None, None


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


def follow_list(run, emit, stop):
    """The weekly check, before the feeds: the new works of every author you follow (Authors tab → Follow) go into this week's run as papers to download, into
    the author's folder, without screening (you chose the author). Returns the run ids of the papers to download; nothing is downloaded here (follow_download,
    after the screening). A work the feeds will bring too is the same paper: one already in the run is marked for download in place, and the followed papers'
    keys go into state/seen.json so a feed read afterwards does not add them again.

    For each author: their full OpenAlex list minus the works already dealt with (authors.follow_known). Those that are not a kind that is downloaded (books,
    a preprint of a paper whose published version is listed), already in the library (filed into the folder) or deleted by you are dealt with at once; the
    rest are new. An author followed with no record of what was dealt with yet (followed from another computer, an older record) is set up silently: everything
    listed now counts as dealt with, and new works start from next week. A folder you deleted pauses following (with a note on the Authors tab)."""
    import authors
    import folders as fo
    recs = authors.followed()
    if not recs:
        return []
    seen_path = os.path.join(STATE, "seen.json")
    todo, n_new = [], 0
    for rec in recs:
        if stop.is_set():
            break
        fid, name = rec["folder"], rec.get("name") or "an author"
        if not fo.get(fid):
            authors.set_follow(fid, False)                    # (paused for real: tick Follow on the Authors tab to start a new folder)
            authors.set_follow_warn(fid, "Its folder was deleted, so following is paused. Tick Follow to start a new folder.")
            emit("log", {"msg": "Followed author %s: the folder was deleted, so following is paused (Authors tab)." % name, "level": "warn"})
            continue
        ids = rec.get("ids") or []
        try:
            ws, complete, total = authors.works(ids, stop=stop)
        except authors.AuthorError as e:
            emit("log", {"msg": "Followed author %s: %s Trying again next week." % (name, e), "level": "warn"})
            continue
        if stop.is_set():
            break
        if "follow_known" not in rec:
            authors.follow_mark(fid, [w.get("id") for w in ws], seed=True)
            emit("log", {"msg": "Following %s is set up: %d works listed now; their new works come from next week." % (name, len(ws))})
            continue
        known = {str(k).upper() for k in rec.get("follow_known") or []}
        if not ws and known:                                  # an id OpenAlex merged into another lists nothing any more: ask OpenAlex which id it is now
            now = authors.resolve(ids)
            if now is None or fx.openalex_spent():
                emit("log", {"msg": "Followed author %s: OpenAlex did not answer; trying again next week." % name, "level": "warn"})
                continue
            if set(now) - set(ids):
                authors.add_ids(fid, now)
                try:
                    ws, complete, total = authors.works(now, stop=stop)
                except authors.AuthorError as e:
                    emit("log", {"msg": "Followed author %s: %s Trying again next week." % (name, e), "level": "warn"})
                    continue
            if not ws:
                msg = "OpenAlex lists no works for %s any more (the author's record may have been merged or removed). Search for them again and press Follow." % name
                authors.set_follow_warn(fid, msg)
                emit("log", {"msg": msg, "level": "warn"})
                continue
        kept_all, _ = authors.select(ws)                      # (on the full list: a new preprint of a paper whose published version is listed is still left out)
        kept = [w for w in kept_all if authors._short(w.get("id")).upper() not in known]
        kept_ids = {authors._short(w.get("id")).upper() for w in kept}
        authors.follow_mark(fid, [w.get("id") for w in ws if authors._short(w.get("id")).upper() not in known | kept_ids])   # (not a kind that is downloaded: dealt with)
        papers = authors.to_papers(kept, fid, name)
        for p in papers:
            p["reason"] = "followed author · " + p["reason"]
        new, have, gone = authors.split(papers)
        authors.file_have(fid, have)
        authors.follow_mark(fid, [p["openalex"] for p, _ in have] + [p["openalex"] for p in gone])
        by_title = {}
        for q in run.papers.values():
            by_title.setdefault((authors.tkey(q.get("title")), q.get("abbr")), []).append(q)
        for p in new:
            q = run.papers.get(p["id"])
            if q is None and authors.tkey(p["title"]):
                q = next((x for x in by_title.get((authors.tkey(p["title"]), p.get("abbr")), []) if not (x.get("doi") and p.get("doi"))), None)
            if q is not None and q["id"] in todo:              # a work of two authors you follow: one download, filed into both folders (follow_settle)
                if q.get("folder") != fid:
                    q.setdefault("also_folders", []).append([fid, p["openalex"]])
                continue
            if q is not None:                                 # the feeds brought it already this week: the same paper, now one to download into the folder
                if q.get("user_set"):                         # (unless you set its verdict yourself)
                    authors.follow_mark(fid, [p["openalex"]])
                    continue
                q.update(verdict="download", folder=fid, openalex=p["openalex"], reason=p["reason"], source=q.get("source") or "author", summary_by_hand=True)
                q.pop("model_verdict", None)
                emit("verdict", {k: q.get(k) for k in ("id", "verdict", "relevance", "topics", "method", "reason", "model_verdict")})
                todo.append(q["id"])
            else:
                p["source_mode"] = "weekly"
                p["summary_by_hand"] = True                   # (downloaded only: its summary comes from the Library's "Summarise N papers…", with its estimate first)
                run.add_paper(p)
                emit("paper", slim(p))
                todo.append(p["id"])
            n_new += 1
        seen = load_json(seen_path, {})
        for p in new + [p for p, _ in have] + gone:
            for k in {p["id"], fx.key_for(None, p.get("journal") or "", p.get("title") or "")}:
                seen.setdefault(k, {"journal": p.get("journal"), "title": p.get("title"), "added": datetime.now(timezone.utc).date().isoformat(), "follow": True})
        os.makedirs(STATE, exist_ok=True)
        with open(seen_path, "w") as f:
            json.dump(seen, f, indent=1)
        emit("log", {"msg": "Followed author %s: %d new work%s%s; %d already in your library (in the folder too), %d you deleted." % (
            name, len(new), "" if len(new) == 1 else "s", "" if complete else " (OpenAlex stopped answering: the rest next week)", len(have), len(gone))})
    run.save(force=True)
    return todo


def follow_settle(run, ids, keys=None):
    """After the followed works were downloaded: those that came (or that you deleted meanwhile) are dealt with; the others (a human check, a sign-in, no access)
    are not, so next week's check tries them again. Returns (dealt with, to try again)."""
    import authors
    ok, again = 0, 0
    for i in ids:
        p = run.papers.get(i) or {}
        if not p.get("folder") or not p.get("openalex"):
            continue
        if p.get("dl_status") in ("ok", "deleted"):
            authors.follow_mark(p["folder"], [p["openalex"]])
            for fid2, oa2 in p.get("also_folders") or []:     # (a co-author you also follow)
                authors.follow_mark(fid2, [oa2])
                if p.get("dl_status") == "ok":
                    try:
                        authors.file_new(fid2, (keys or {}).get(i) or p.get("lib_key") or paper_key(p))
                    except Exception:  # noqa: BLE001
                        pass
            ok += 1
        else:
            again += 1
    return ok, again


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

    def __init__(self, reason, partial=None):
        super().__init__(reason)
        self.reason = reason
        self.partial = partial or {}       # {n: result} of the papers that were judged all the same (the halves of a batch that was split after a cut-off answer)


SPLIT_DEPTH = 2                # a batch whose answer was cut off is halved at most this many times (25 → 13 → 7 papers: 1 + 2 + 4 requests at most, never one per paper)


def _screen_batch(system, batch, usage, override=None, stop=None):
    """One batch of papers judged by the model of the "screen" feature (Settings → AI), through the AI layer: the monthly budget stops it, local-only mode applies, and
    every call is in the usage ledger. Returns {n: result}. Raises ai.Blocked when nothing more may be asked, or the ai.AIError of an answer that asking again cannot change
    (ai.retryable: a refused request, key, credit or model): the caller stops the whole screening on either (its `partial`, when a split batch was stopped that way between
    its halves, holds the verdicts of the halves that did answer). Raises BatchFailed (with the reason) when this batch could not be judged.
    An answer that is not the JSON asked for is not simply sent again: if `stop` (the researcher's Stop) is set, nothing more is sent and None is returned (the papers
    stay unscreened); an answer cut off at the length limit is asked again as two halves (SPLIT_DEPTH; BatchFailed.partial then holds the verdicts of the halves that
    did answer); anything else is asked once more, and no further."""
    items = []
    for n, p in enumerate(batch, 1):
        items.append({"n": n, "journal": p.get("abbr"), "year": (p.get("pub_date") or "")[:4],
                      "title": p.get("title"), "keywords": p.get("keywords"),
                      "abstract": (p.get("abstract") or "")[:1800] or None})
    return _ask_verdicts(system, items, usage, override, stop, SPLIT_DEPTH)


def _ask_verdicts(system, items, usage, override, stop, depth, again=False):
    """The verdicts {n: result} for these papers of one batch (each carries its number n in the batch, so the halves of a split batch merge back); see _screen_batch.
    again: these papers were already sent in a request of this batch (a half of a split batch)."""
    why, resent = "no answer", False
    for attempt in range(3):
        try:
            res = ai.chat("screen", [{"role": "user", "content": json.dumps(items, ensure_ascii=False)}], system=system, schema=SCREEN_SCHEMA, max_tokens=8000,
                          override=override, log={"papers": 0 if again or resent else len(items)})       # (the ledger row says how many papers it carried, 0 when it repeats papers a request of this batch already carried: the estimate rests on it, see screen_history)
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
            if stop is not None and stop.is_set():             # Stop was pressed meanwhile: nothing is sent again, and the papers stay unscreened
                return None
            began = re.sub(r"\s+", " ", res.text or "").strip()[:80] or "it was empty"
            if getattr(res, "cut_off", False):                 # the answer hit the length limit: the same request would be cut off the same way, so ask for half the papers at a time
                if len(items) < 2 or depth <= 0:
                    raise BatchFailed("the model's answer was cut off at its length limit before the verdicts were complete (%s)" % began)
                mid = (len(items) + 1) // 2
                got, whys = {}, []
                for half in (items[:mid], items[mid:]):
                    if stop is not None and stop.is_set():     # Stop was pressed while the other half was answered: this one is not sent
                        return None
                    try:
                        part = _ask_verdicts(system, half, usage, override, stop, depth - 1, again=True)
                    except BatchFailed as e:
                        got.update(e.partial)
                        whys.append(e.reason)
                        continue
                    except ai.AIError as e:                    # the budget, or a refused request: the screening stops, but the halves that did answer were paid for and keep their verdicts
                        got.update(getattr(e, "partial", None) or {})
                        e.partial = got
                        raise
                    if part is None:                           # stopped: nothing of this batch is kept (the caller leaves its papers unscreened)
                        return None
                    got.update(part)
                if whys:
                    raise BatchFailed(whys[0], got)
                return got
            why = "the model's answer was not the JSON that screening asks for (%s)" % began
            if resent:                                         # asked once more already: a third identical request would only be paid for again
                break
            resent = True
            continue
        sent = {it["n"] for it in items}
        got = {r["n"]: r for r in out if isinstance(r, dict) and type(r.get("n")) is int and r["n"] in sent}      # only the numbers this request carried: a half of a split batch that numbers its answer from 1 again must not overwrite the other half's verdicts
        if not got:                                            # an answer for none of its own papers cannot be used (and asking again would pay for the same)
            raise BatchFailed("the model's answer had no verdict for any of the papers it was sent")
        return got
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


def screen_model(override=None):
    """The model that screens, as Settings → AI sets it (or `override`, a backup for one run). Claude Sonnet 5.5 is named as before (so verdicts saved earlier still
    count); another provider or model by name."""
    prov, model = (override["provider"], override["model"]) if override else ai.feature_model("screen")
    return MODEL if (prov, model) == ("anthropic", MODEL) else "%s/%s" % (prov, model)


def screen_fingerprint(system, override=None):
    """A verdict answers one question: the model and the whole instruction it was given (topics, method, focus phrase). Another question gets its own verdicts."""
    return hashlib.sha256((screen_model(override) + "\n" + system).encode("utf-8")).hexdigest()[:16]


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
    """Judge the run's papers that have no verdict yet, in batches, with the model chosen for screening (Settings → AI). A verdict saved for the same question and
    text is reused. If that model cannot answer in the structured JSON screening needs (ai.cannot_structure: a 400/404/422 that says so), the papers still without a
    verdict are screened once more with a backup model of the same provider (prices.backup_for), for this run only: the setting is not changed, the run says which
    model it was (run.screen_backup), and there is never a third model. Anything else that stops the screening (the budget, a refused key) stops it as before."""
    system = screening_system(run.params.get("topics"), run.params.get("query"),
                              run.params.get("methods"))
    todo = [run.papers[i] for i in (ids or run.order) if not run.papers[i].get("verdict")]
    total = len(todo)
    prog = {"done": 0}
    valid = set(topic_labels())

    def reuse(papers, fp, est=None):
        """The papers of `papers` still to ask (a verdict saved for this question and text is taken instead: no second payment). est: what one paper would cost
        with the model of this pass (None: the screening model's)."""
        known = known_verdicts(fp, papers, reuse_days())
        if not known:
            return papers
        for p in papers:
            e = known.get(p["id"])
            if e:
                p["verdict"], p["relevance"], p["topics"], p["method"], p["reason"] = e["v"], e.get("rel") or 0, e.get("topics") or [], e.get("method"), e.get("reason") or ""
                p.pop("model_verdict", None)
                terms_rule(run, p)
                emit("verdict", {k: p.get(k) for k in ("id", "verdict", "relevance", "topics", "method", "reason", "model_verdict")})
        run.reused += len(known)
        prog["done"] += len(known)
        est = est if est is not None else est_screen_cost_per_paper()
        emit("log", {"msg": "%d papers already had a verdict from an earlier screening with the same question%s" % (len(known), " (about $%.2f not paid for again)" % (len(known) * est) if est is not None else "")})
        run.save(force=True)
        return [p for p in papers if p["id"] not in known]

    def run_pass(papers, override, fp):
        """Screen `papers` with the configured model (override None) or `override`. Returns (the AIError that stopped it or None, batches answered)."""
        batches = [papers[i:i + batch_size] for i in range(0, len(papers), batch_size)]
        blocked, answered = None, 0
        halt = threading.Event()         # set by the worker whose batch met Blocked or a refused request: a batch that has not started yet is then not sent (the batches already
                                         # on their way finish; cancelling the waiting futures from this thread alone would let a free worker start one more first)

        def one(b):
            if halt.is_set() or stop.is_set():
                return None              # never sent: the papers stay unscreened
            try:
                return _screen_batch(system, b, run.usage, override, stop)
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
                if fut.cancelled():                            # cancelled below once nothing more may be asked: never started, not paid for
                    continue
                why, cut_short = "", False
                try:
                    res = fut.result()
                except ai.AIError as e:                        # Blocked (budget, local-only, no key) or a request the provider refuses for every batch alike (model, key, credit):
                    for f in futs:                             # the papers stay unscreened, to pick up later ("Retry screening"), instead of each being marked as failed
                        f.cancel()
                    if blocked is None:
                        blocked = e
                    res, cut_short = getattr(e, "partial", None), True      # (partial: a split batch stopped between its halves; the halves that answered keep their verdicts)
                    if not res:
                        continue
                except BatchFailed as e:
                    res, why = e.partial, e.reason             # (partial: the halves of a split batch that did answer keep their verdicts)
                    emit("log", {"msg": "Screening failed for %d papers: %s" % (len([n for n in range(1, len(batch) + 1) if not res.get(n)]), why[:300]), "level": "warn"})
                if res is None:                                # not sent (see one), or stopped half-way (see _screen_batch): left unscreened, to pick up later
                    continue
                if res:
                    answered += 1                              # (a batch that failed gave no verdict: it does not count as screened by this model)
                for n, p in enumerate(batch, 1):
                    r = res.get(n)
                    if not r and cut_short:                    # never judged, because the screening was stopped: left unscreened
                        continue
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
                prog["done"] += sum(1 for n in range(1, len(batch) + 1) if res.get(n)) if cut_short else len(batch)
                emit("progress", {"stage": "screening", "label": "Screening", "done": prog["done"], "total": total})
                emit("cost", {"cost": round(run.usage.cost(), 4), "usage": run.usage.d, "detail": run.usage.detail()})
                remember_verdicts(fp, [p for p in batch if p.get("verdict") != "error"])
                run.save()
        return blocked, answered

    fp = screen_fingerprint(system)
    emit("progress", {"stage": "screening", "label": "Screening", "done": 0, "total": total})
    todo = reuse(todo, fp)
    blocked, answered = run_pass(todo, None, fp)
    if answered:
        run.screen_backup = {}                                 # the chosen model screens again: no backup is in use
    no_backup = ""
    if blocked is not None and ai.cannot_structure(blocked) and not stop.is_set():
        prov, model = ai.feature_model("screen")
        mine = ai.pricing.name(prov, model)
        try:
            b = ai.pricing.backup_for(prov, model)
        except Exception:  # noqa: BLE001      (no list could be read: as before, the screening stops)
            b = None
        rest = [p for p in todo if not p.get("verdict")]
        if b and rest:
            cost_note = ""
            if not b["within"]:
                cost_note = (" No %s model at no more than twice its price can, so the cheapest one that can was chosen ($%.2f against $%.2f per million tokens in and out)." % (
                    prov, b["price"], b["current_price"]) if b["current_price"] is not None else " The price of %s is not known, so the cheapest model that can was chosen." % mine)
            emit("log", {"msg": "%s can't answer in the structured JSON that screening needs (%s). Screening the remaining %d papers with %s, a backup from %s, for this run "
                                "only: your setting in Settings → AI is unchanged.%s" % (mine, str(blocked)[:200], len(rest), b["name"], prov, cost_note), "level": "warn"})
            ov = {"provider": prov, "model": b["model"]}
            fp2 = screen_fingerprint(system, ov)
            reused0 = run.reused
            est2 = est_screen_cost_per_paper(prov, b["model"])
            rest = reuse(rest, fp2, est2)
            blocked2, answered2 = run_pass(rest, ov, fp2)
            if answered2 or run.reused > reused0:
                run.screen_backup = {**b, "instead_of": mine, "why": str(blocked)[:300], "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
                run.save(force=True)
            if blocked2 is not None:
                blocked = ai.AIError("%s could not screen (%s). The backup %s was tried and also stopped: %s" % (mine, str(blocked)[:200], b["name"], str(blocked2)[:200]),
                                     status=getattr(blocked2, "status", None))
            else:
                blocked = None
        elif not b and ai.pricing.is_openrouter((ai.config_data().get("providers") or {}).get(prov)):
            no_backup = " No backup model could be chosen: OpenRouter's model list could not be read, or it has no other model that can."
    if blocked is not None:
        left = len([p for p in todo if not p.get("verdict")])
        hint = ""
        if not isinstance(blocked, ai.Blocked) and getattr(blocked, "status", None) in (400, 404, 422):
            hint = " The model chosen for screening (Settings → AI → Screening new papers) may not be able to answer in the structured JSON that screening asks for: its Test button there checks that." + no_backup
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
        if res.cut_off:                 # half a summary would count as the summary for ever too (nothing asks again once the .md exists)
            raise ai.AIError("The summary was cut off at the model’s length limit, so it was not saved. You can try again.")       # (short: the run page shows the first 100 characters of it)
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
    if msg.stop_reason in ("max_tokens", "model_context_window_exceeded"):
        raise RuntimeError("The summary was cut off at the model’s length limit, so it was not saved. You can try again.")
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


def site_key(p):
    """Which site a paper's download goes to, for spacing papers out and for setting a site's papers aside while its human check waits: its publisher; for a
    publisher the app has no route for (papers from any journal: related papers, author search), its DOI prefix (one prefix, one publisher's site), else the
    host of its address. Before, all of those shared one key, so one site's check held up every other unknown site."""
    if p.get("publisher"):
        return p["publisher"]
    doi = str(p.get("doi") or "")
    if "/" in doi:
        return "prefix:" + doi.split("/", 1)[0]
    host = urllib.parse.urlsplit(str(p.get("url") or "")).netloc
    return ("host:" + host) if host else "other"


def _write_kind(p, path):
    """A paper that knows its kind (an author's working or conference paper, a re-download from Check my PDFs) gets its sidecar next to the PDF (kinds.py)."""
    if p.get("kind") in kinds.PAPER_KINDS:
        try:
            kinds.write_sidecar(path, p["kind"], p.get("journal") or "", p.get("kind_label") or "", dl.index_key(p))
        except OSError:
            pass


def _into_folder(p, key=None, force=False):
    """A paper of an author run is filed into the author's folder once it is in the library. key: its key in the library (a copy you already had may be keyed by
    its title); force: put it back even if it was filed there before (you added, yourself, a paper you had deleted). Returns the folder's name if the paper is in it."""
    fid = p.get("folder")
    if not fid:
        return ""
    k = key or paper_key(p)
    try:
        import authors
        authors.file_new(fid, k)          # (once: a paper you took out of the folder is not put back by a later retry)
        if force:
            fo.add(fid, [k])
        f = fo.get(fid) or {}
        return f.get("name", "") if k in (f.get("papers") or []) else ""
    except Exception:  # noqa: BLE001      (the folder was deleted meanwhile: the paper stays in the library)
        return ""


# ---------------------------------------------------------------- papers of a run into a folder you choose (after the run: "Add to a folder…" on its page)

GENERIC_PICKS = ("this folder", "the whole library", "related papers", "library")     # (what a Discover run was called before it knew its folder)


def run_to_folder(run, fid, ids=None, pending=True):
    """Put papers of a run into the folder fid: those in your library now, at once (whatever was filed before: you chose it); with pending, the papers still to
    download (marked for download, not deleted) go in when they arrive (download, retry, or a PDF you add), unless they already wait for another folder (an
    author's). ids: only these papers (default: all of the run). A Discover run that did not know its folder takes this one, and its name. Never an AI call.
    Returns {added, already, pending, kept, folder}."""
    f = fo.get(fid)
    if not f:
        raise ValueError("That folder is not there any more: reload the page.")
    have = downloaded_files()
    keys, later, kept = [], 0, 0
    for i in (ids if ids is not None else list(run.order)):
        p = run.papers.get(str(i))
        if not p:
            continue
        k = next((x for x in (p.get("lib_key"), paper_key(p)) if x and x in have), None)
        if not k and p.get("dl_status") == "ok" and has_file(p):
            k = p.get("lib_key") or paper_key(p)           # (it came while this was being read: the download has its row by now)
        if k:
            keys.append(k)
            continue
        if not pending or p.get("verdict") != "download" or p.get("dl_status") == "deleted":
            continue
        if p.get("folder") and p["folder"] != fid and fo.get(p["folder"]):
            kept += 1                                     # (one folder waits per paper: an author's work keeps its author's folder, a Discover pick its run's)
            continue
        if p.get("folder") != fid:
            p["folder"] = fid
            later += 1
    keys = list(dict.fromkeys(keys))
    before = set(f.get("papers") or [])
    if keys:
        fo.add(fid, keys)
    if pending and run.kind == "picks" and (not run.params.get("folder") or not fo.get(run.params["folder"])) and (run.params.get("label") or "related papers") in GENERIC_PICKS:
        run.params.update(folder=fid, folder_name=f["name"], label=f["name"])        # ("Picks · this folder" becomes "Picks · Labor Market Papers")
    run.save(force=True)
    return {"added": sum(1 for k in keys if k not in before), "already": sum(1 for k in keys if k in before), "pending": later, "kept": kept, "folder": f["name"]}


# ---------------------------------------------------------------- a PDF you downloaded yourself, for a paper of a run

ATTACH_NOTE = "added by you (a PDF you downloaded yourself)"
ATTACH_LOCK = threading.RLock()     # a paper's download status: a download and a PDF you add never change it at the same moment (download_and_summarize takes it too)


def has_file(p):
    return bool(p.get("file")) and os.path.exists(os.path.join(ROOT, p["file"]))


def _adopt(p):
    """The paper is in the library already (another run, or a PDF you added to it on another run's card, while this download was running): the run's paper points to that
    copy, and nothing is downloaded. A copy you added yourself is not summarised by itself. True if it was there."""
    import importer
    k = paper_key(p)                                 # (the same key exactly, as mark_downloaded: an "Editorial" without a DOI is not another journal's "Editorial")
    row = next((r for r in importer.library_rows() if k and dl.index_key(r) == k and os.path.exists(os.path.join(ROOT, r["file"]))), None)
    if not row:
        return False
    ex, md = row["file"], row["file"][:-4] + ".md"
    p.update(file=ex, summary=md if os.path.exists(os.path.join(ROOT, md)) else None, dl_note=row.get("note") or p.get("dl_note"),
             summary_by_hand=bool(p.get("summary_by_hand") or str(row.get("note") or "").startswith("added by you")), lib_key=k)      # (have_it marks it "ok" and tells the page)
    return True


def _attachable(run, pid):
    p = run.papers.get(str(pid or ""))
    if not p:
        raise ValueError("That paper is not in this run any more: reload the page.")
    if p.get("dl_status") == "downloading":
        raise ValueError("This paper is being downloaded right now. Wait until it is done; if it does not come, add your PDF then.")
    if p.get("dl_status") in ("ok", "summarizing") and has_file(p):
        raise ValueError("This paper is already in your library.")
    return p


def _attach_file(run, p, data, note, save):
    import importer
    with ATTACH_LOCK:
        _attachable(run, p["id"])                                # (again, now that nothing else can change it: its download may have started meanwhile)
        was_deleted = p.get("dl_status") == "deleted"
        res = importer._file(dict(p), data, note, after=lambda dest: _write_kind(p, dest))   # the download's file name, an index row 'ok', never a copy twice, never over another file
        row = next((r for r in importer.library_rows() if r["file"] == res["file"]), None)
        key = dl.index_key(row) if row else paper_key(p)
        md = res["file"][:-4] + ".md"
        upd = dict(dl_status="ok", file=res["file"], dl_note=note if not res["existing"] else "already in your library",
                   summary=md if os.path.exists(os.path.join(ROOT, md)) else None, summary_by_hand=True, lib_key=key)      # (its summary is asked for in the Library, with the cost first)
        if p.get("verdict") != "download":                       # (you wanted it: it is no longer shown as skipped)
            upd.update(ai_verdict=p.get("ai_verdict") or p.get("verdict"), verdict="download", user_set=True)
        p.update(upd)                                            # (one update: a running job may be saving this run)
    folder = _into_folder(p, key, force=was_deleted)
    if run.kind == "weekly" and p.get("folder"):
        follow_settle(run, [p["id"]], keys={p["id"]: key})      # a followed author's work: dealt with, not fetched again next week
    if save:
        run.save(force=True)
    return {"filed": True, **res, "was_deleted": was_deleted, "folder": folder, "paper": slim(p)}


def attach_pdf(run, pid, data, save=True):
    """A PDF you downloaded yourself, for paper `pid` of `run`. It gets the same check as a download (identity.check): a PDF that is this paper, or that cannot be checked
    (a scan), is filed at once like a download; one that does not look like it is kept aside (a token) until you say it is the right paper (attach_keep) or cancel. Never an
    AI call. Returns {"filed": True, file, existing, was_deleted, folder, paper, …} or {"filed": False, token, title, seems, why}. Raises ValueError with words for you."""
    import identity
    import importer
    p = _attachable(run, pid)
    if not dl.looks_pdf(data):
        raise ValueError("That doesn’t look like a complete PDF. Drop the PDF file itself, not a web page, a link to it, or a download that has not finished.")
    if importer._existing(p):                                    # you have it already: the card points to your copy (nothing to check, nothing written)
        return _attach_file(run, p, data, ATTACH_NOTE, save)
    verdict, why, ev = identity.check(p, data)
    if verdict == "mismatch":
        says = (ev or {}).get("pdf_says") or ""
        pre = "this PDF is titled “%s”: " % says[:90].replace("|", "/")
        return {"filed": False, "token": importer._stage(data, bind={"run": run.id, "id": p["id"]}), "title": p.get("title") or "",
                "seems": says or importer._read(data)["title"] or "", "why": why[len(pre):] if why.startswith(pre) else why}
    res = _attach_file(run, p, data, ATTACH_NOTE + ("; identity not checked (%s)" % why if verdict == "unverified" else ""), save)
    return dict(res, unverified=why if verdict == "unverified" else "")


def attach_keep(run, pid, token, save=True):
    """You said the PDF that did not look like this paper is the right one: file it, and 'Check my PDFs' no longer flags it."""
    import identity
    import importer
    p = _attachable(run, pid)
    res = importer.take_staged(token, lambda data: _attach_file(run, p, data, ATTACH_NOTE + "; you said it is the right paper", save), expect={"run": run.id, "id": p["id"]})
    if not res["existing"]:
        identity.keep(res["file"])
    return dict(res, kept=True)


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


NOTIFY = None       # the Mac app sets a function (title, text) that shows a notification: a human check is waiting for the user (app/jw_app.py)
WAITING_NOTE = "waiting for you: its site wants a “verify you are human” check (the app asks you to open it)"
NEEDS_CHECK_NOTE = "%s wants a human check: select the paper and press Download again (or open it in your browser)"


def _notify_user(title, text):
    if NOTIFY:
        try:
            NOTIFY(title, text)
        except Exception:  # noqa: BLE001
            pass


def download_and_summarize(run, ids, emit, stop, sleep=None, clock=None, checks=None, auto_open=True, summarize=True):
    """Download the papers `ids` (one browser, a site's papers together), then summarise them. Papers from the same publisher are spaced out (pace_gap); the
    waiting time is used to summarise papers already downloaded, so a careful pace costs little time overall. `sleep`/`clock` are for the tests.

    `checks` (the app): a queue.Queue of the hosts whose "verify you are human" check the user asked to open. With it, a check opens nothing by itself: the
    paper and the rest of that site's papers are set aside, the user is told (the page's banner, and NOTIFY), and the other sites go on in the background.
    A press is served between two papers or during a pace wait: the check opens in a window on that one page, and when the user has ticked the box the
    site's papers come back to the front of the queue, in the background. A site that asks again after a pass gets one more nudge; that time its window
    stays for its remaining papers. Papers still set aside when the rest is done end as needs_check (no waiting), except when nothing else was being
    downloaded: then the check opens at once, as the command line does (and the user is not first told to press "Open the check": until some paper gets a
    final answer, a check met is told only once one does). Without `checks` the check window opens at once (browser.Browser's default).

    auto_open=False: a check never opens by itself, even when nothing else was downloaded (the user did not start this for these papers: the weekly check's
    followed authors); the user is told at once. summarize=False: the papers are downloaded only (no AI call; the Library offers their summaries, with an
    estimate first)."""
    import collections
    import queue as queue_mod
    env = dl.load_env()
    sleep = sleep or (lambda t: stop.wait(t))
    clock = clock or time.monotonic
    pace = (signins.profile().get("pace") or "careful")
    last_at, challenged = {}, set()
    papers = [run.papers[i] for i in ids if i in run.papers]
    for p in papers:                         # (what waits its turn is written down: if the app is quit mid-batch, the run offers to go on with the rest)
        p["dl_queued"] = True
    run.save(force=True)
    # same publisher back to back: after one manual "verify you are human" the
    # rest of that site's papers go through the same window
    papers.sort(key=lambda p: p.get("publisher") or "")
    total = len(papers)
    got = []
    ask = checks is not None
    todo_q = collections.deque(papers)
    deferred = {}           # host of a check page -> the papers set aside until the user opens it
    waiting_pub = {}        # publisher -> the host whose check its papers wait for (they are not fetched until then: they would only meet the check page)
    nudged = {}             # host -> times the user was asked in this batch (at most twice)
    given_up = set()        # hosts whose check was not passed, or that asked a third time: their papers end as needs_check
    count = {"done": 0, "final": 0}         # papers finished; papers that got a final answer from a site in this batch (not set aside)
    quiet = []              # (host, state, pub) of checks met before any paper got a final answer: told once one does (a batch of nothing else opens them at once instead)
    emit("progress", {"stage": "downloading", "label": "Downloading", "done": 0, "total": total})
    current = {}

    def on_challenge(host, state):
        if state in ("pending", "again"):                   # (ask mode) the paper's routes go on; the user is told once the paper is actually set aside
            current.setdefault("chall", (host, state))      # the first check this paper met (its publisher's own page comes first)
            if current.get("pub"):
                challenged.add(current["pub"])
            return
        msg = {"waiting": "%s wants to check you're human. A browser window just opened: tick "
                          "“Verify you are human” (you have 3 minutes); the download then continues "
                          "by itself, and the site's other papers in this batch reuse the window." % host,
               "open": "A Chromium window is open on %s: tick “Verify you are human” there (you have 3 minutes). Downloading goes back to the background "
                       "afterwards." % host,
               "passed": "%s check passed; continuing in the background." % host,
               "failed": "%s check wasn't completed; its papers need a check (select them and press Download again)." % host}.get(state)
        if not msg:
            return
        emit("challenge", {"host": host, "state": state, "msg": msg})
        emit("log", {"msg": msg, "level": "warn" if state != "passed" else "ok"})
        if state == "waiting" and current.get("pub"):
            challenged.add(current["pub"])                  # that site is watching: leave more time between its papers for the rest of the batch

    def emit_dl(p):
        emit("download", {k: p.get(k) for k in ("id", "dl_status", "dl_note", "file", "manual_url")})

    def finished(p, final=True):
        p.pop("dl_queued", None)
        count["done"] += 1
        count["final"] += final
        emit("progress", {"stage": "downloading", "label": "Downloading", "done": count["done"], "total": total})

    def set_aside(p, host, pub):
        with ATTACH_LOCK:
            if has_file(p):                                  # (you added its PDF yourself meanwhile)
                return False
            deferred.setdefault(host, []).append(p)
            waiting_pub[pub] = host
            p.update(dl_status="needs_check", dl_note=WAITING_NOTE, manual_url=dl.browser_pdf_url(p))
        emit_dl(p)
        return True

    def have_it(p):                                          # the paper has its PDF (from before, or you added it yourself): nothing to download
        if not p.get("summary_by_hand"):                     # (a PDF you added is summarised only when you ask, in the Library, with the cost first)
            got.append(p)
        if p.get("dl_status") != "ok":
            p["dl_status"] = "ok"
            emit_dl(p)
        _into_folder(p, p.get("lib_key"))                   # (the key of the copy in the library: one you had may be keyed by its title)
        finished(p, final=False)

    def nudge(host, state, pub):
        nudged[host] = nudged.get(host, 0) + 1
        site = SITE_NAMES.get(pub, host)
        msg = ("%s wants to check you're human. Its papers wait; the other sites' papers go on downloading in the background. Press “Open the check” when "
               "you are ready: that one page opens in a window, you tick the box, and downloading goes back to the background." % site if state == "pending" else
               "%s asked again after your check. Press “Open the check” once more: this time the window stays open for that site's remaining papers in this "
               "batch." % site)
        emit("challenge", {"host": host, "state": state, "msg": msg, "site": site})
        emit("log", {"msg": msg, "level": "warn"})
        _notify_user("A human check is waiting", "%s wants to check you're human. Open Journal Watcher and press “Open the check”." % site)

    def release_quiet():
        for q in quiet:
            if q[0] in deferred and q[0] not in given_up:
                nudge(*q)
        quiet.clear()

    def open_one(br, host, stay):
        if host not in deferred or host in given_up or not hasattr(br, "open_check"):
            return
        with ATTACH_LOCK:
            nothing = all(has_file(q) or _adopt(q) for q in deferred[host])
        if nothing:                                         # you added the PDFs of every paper waiting for this check yourself (here or on another run's card): nothing to open
            for q in deferred.pop(host):
                have_it(q)
            for k in [k for k, v in waiting_pub.items() if v == host]:
                waiting_pub.pop(k)
            emit("challenge", {"host": host, "state": "cleared", "msg": ""})
            emit("log", {"msg": "No check to open for %s: you added the PDFs of the papers that waited for it." % host, "level": "ok"})
            return
        try:
            ok = br.open_check(host, stay=stay)
        except Exception as e:  # noqa: BLE001        (a check that could not be shown is a check not passed: the batch goes on)
            emit("log", {"msg": "The check for %s could not be shown: %s" % (host, str(e)[:120]), "level": "warn"})
            ok = False
        if ok:
            back = deferred.pop(host)
            for k in [k for k, v in waiting_pub.items() if v == host]:
                waiting_pub.pop(k)
            again = []
            for q in back:                                  # no longer waiting for the user (a Stop before they come up leaves them as never tried)
                with ATTACH_LOCK:
                    mine = has_file(q) or _adopt(q)         # (you added its PDF while it waited, here or on another run's card)
                    if not mine:
                        q.update(dl_status=None, dl_note=None)
                if mine:
                    have_it(q)
                    continue
                emit_dl(q)
                again.append(q)
            todo_q.extendleft(reversed(again))              # the site's papers, back to the front of the queue (still paced)
        else:
            given_up.add(host)

    def serve(br):
        """Open the checks the user asked for since the last look (only this thread touches the browser)."""
        while ask:
            try:
                host = checks.get_nowait()
            except queue_mod.Empty:
                return
            open_one(br, host, stay=nudged.get(host, 0) >= 2)

    import deletion
    gone = deletion.deleted_keys()
    try:
        getattr(dl, "BROWSER_ERROR", {}).update(why="")
        with dl.open_browser(on_challenge=on_challenge, ask=ask) as br:
            try:
                br.stop = stop                                  # Stop also ends a wait for the user's tick
            except AttributeError:
                pass
            if br is None:                                      # the browser the sign-ins live in did not start: say so once, in plain words (each paper would only say "blocked")
                why = getattr(dl, "BROWSER_ERROR", {}).get("why") or "it did not start"
                emit("log", {"msg": "The sign-in browser could not start (%s). Papers that are free to read are still downloaded; papers behind a publisher or library sign-in cannot be, "
                                    "until this is fixed. Quit other copies of Journal Watcher, then try again; if it stays, restart the computer." % why, "level": "error"})
            while True:
                while todo_q:
                    if stop.is_set():
                        break
                    serve(br)
                    if stop.is_set():
                        break
                    p = todo_q.popleft()
                    if has_file(p):
                        have_it(p)
                        continue
                    if paper_key(p) in gone:                         # you deleted it: never fetched again by a run
                        p.update(dl_status="deleted", dl_note=DELETED_NOTE)
                        emit_dl(p)
                        finished(p, final=False)
                        continue
                    pub = site_key(p)
                    if pub in waiting_pub:                           # its site's check waits for the user
                        if not set_aside(p, waiting_pub[pub], pub):
                            have_it(p)
                        continue
                    if pub in last_at:
                        due = last_at[pub] + pace_gap(pub, pace, pub in challenged)
                        if clock() < due:
                            emit("log", {"msg": "Waiting %d s before the next %s paper (%s download pace, so the site does not take Journal Watcher for a robot)." % (
                                round(due - clock()), SITE_NAMES.get(pub, p.get("journal") or pub), pace)})
                        while clock() < due and not stop.is_set():
                            serve(br)                                # a press is served while waiting
                            ready = summarize and next((q for q in got if not q.get("summary") and q.get("dl_status") == "ok"), None)
                            if ready and due - clock() > 5:
                                _summarize_one(run, ready, emit)         # use the wait: this paper becomes "Ready to read" sooner
                            else:
                                sleep(min(1.0, max(0.0, due - clock())))
                        if stop.is_set():
                            break
                    if quiet and not auto_open and not stop.is_set():       # another site's paper starts: the check can be opened while it downloads, so say so now
                        release_quiet()
                    with ATTACH_LOCK:                                # (you may have added its PDF yourself during the wait, here or on another run's card)
                        late = has_file(p) or _adopt(p)
                        if not late:
                            p["dl_status"] = "downloading"
                    if late:
                        have_it(p)
                        continue
                    current.clear()
                    current["pub"] = pub
                    emit("download", {"id": p["id"], "dl_status": "downloading"})
                    try:
                        ok, path, note = dl.download_paper(dict(p), env, browser=br)
                    except Exception as e:  # noqa: BLE001      (one paper's error is that paper's: the batch goes on, except when the disk is full)
                        full = isinstance(e, OSError) and getattr(e, "errno", None) == 28
                        ok, path, note = False, None, ("the disk is full: nothing more can be saved" if full else "could not be saved: %s" % (str(e)[:120] or e.__class__.__name__))
                        emit("log", {"msg": ("The disk is full: the download stops here. Free some space, then press Download again." if full
                                             else "“%s” could not be saved (%s). The other papers go on." % ((p.get("title") or "")[:60], str(e)[:120])), "level": "error"})
                        if full:
                            run.errors.append("The disk was full: the download stopped.")
                            stop.set()
                    chall = current.get("chall")
                    last_at[pub] = clock()
                    if ok:
                        p.update(dl_status="ok", file=os.path.relpath(path, ROOT), dl_note=note)
                        _write_kind(p, path)
                        if not p.get("summary_by_hand"):             # (a followed author's work: its summary is asked for in the Library, with the cost first)
                            got.append(p)
                        _into_folder(p)
                    elif ask and chall and hasattr(br, "open_check"):
                        host, state = chall
                        if host not in given_up and state == "again" and nudged.get(host, 0) >= 2 and host not in deferred:
                            given_up.add(host)                       # asked a third time: no more nudges; its papers end as needs_check
                            emit("challenge", {"host": host, "state": "failed", "msg": "%s asked for a check a third time; its papers need a check later "
                                               "(select them and press Download again)." % SITE_NAMES.get(pub, host)})
                        first = host not in deferred and host not in given_up
                        set_aside(p, host, pub)
                        if first and not stop.is_set():
                            if count["final"]:
                                nudge(host, state, pub)
                            else:
                                quiet.append((host, state, pub))     # (told when another paper gets its answer; if none does, the check opens by itself, or with
                                                                     # auto_open=False the papers just end as needing a check, with nobody told to press a button)
                        run.save()
                        continue
                    else:
                        with ATTACH_LOCK:                            # (its PDF came meanwhile, on another run's card or by another run: the library keeps it, and so does this card)
                            came = _adopt(p)
                        if came:
                            have_it(p)
                            run.save()
                            continue
                        own = re.sub(r"wrong paper via [^|]*", "", note)           # what the routes said, without what a refused PDF said about itself (its headline is not a status)
                        p.update(dl_status="needs_library" if ("needs_library_login" in own or "library_setup:" in own) else
                                 "unavailable" if "not_in_library" in own else
                                 "needs_login" if "needs_login" in own else           # (before "not yet in EBSCO": EBSCO is now asked for any paper, so its "not found" must not hide a sign-in the paper needs)
                                 "not_yet" if "not found in library EBSCO" in own else
                                 "needs_check" if "challenge:" in own else
                                 "wrong_paper" if "wrong paper" in note else "failed",
                                 dl_note=note, manual_url=dl.browser_pdf_url(p))
                    emit_dl(p)
                    finished(p)
                    run.save()
                    if quiet and not stop.is_set():                  # a paper got its answer: the end of the batch will not open the checks by itself, so say so now
                        release_quiet()
                if ask and not stop.is_set():
                    serve(br)                                        # a press made during the last paper is still served
                    if todo_q:
                        continue
                # the rest is done. Papers set aside: when nothing else was being downloaded (say, a retry of papers that need a check), the user started this
                # for them, so their check opens at once (the window then stays for that site's papers); otherwise they end as needs_check, without waiting.
                waiting = [h for h in deferred if h not in given_up]
                if ask and auto_open and waiting and not stop.is_set() and count["final"] == 0 and br is not None:
                    quiet.clear()                                    # (the window's own message tells the user: no "press Open the check" first)
                    for h in waiting:
                        nudged[h] = max(nudged.get(h, 0), 2)
                        open_one(br, h, stay=True)
                    if todo_q:
                        continue
                break
    finally:                                                # (also after an error: no paper is left 'waiting for you')
        for host, ps in deferred.items():
            for p in ps:
                with ATTACH_LOCK:
                    mine = has_file(p) or _adopt(p)         # (you added its PDF while it waited, here or on another run's card)
                    if not mine:
                        p.update(dl_status="needs_check", dl_note=NEEDS_CHECK_NOTE % SITE_NAMES.get(p.get("publisher"), host))
                if mine:
                    have_it(p)
                else:
                    emit_dl(p)
                    finished(p)
        left = sum(1 for v in deferred.values() for p in v if p.get("dl_status") == "needs_check")
        if left:
            emit("log", {"msg": "%d paper(s) still need a human check: select them and press Download again." % left, "level": "warn"})
        if any(deferred.values()):
            emit("challenge", {"host": "", "state": "done", "msg": ""})
        for p in papers:                                    # (the batch ended here, by itself or by Stop: nothing is "still waiting" any more; only a quit leaves the marks)
            p.pop("dl_queued", None)
        run.save(force=True)
    todo = [p for p in got if not p.get("summary")] if summarize else []
    for n, p in enumerate(todo, 1):
        if stop.is_set():
            break
        emit("progress", {"stage": "summarizing", "label": "Summarizing with Sonnet 5.5",
                          "done": n - 1, "total": len(todo)})
        _summarize_one(run, p, emit)
    emit("progress", {"stage": "summarizing", "label": "Done", "done": len(todo), "total": len(todo)})
