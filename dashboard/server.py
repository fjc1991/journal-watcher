#!/usr/bin/env python3
"""Journal Watcher dashboard server (stdlib only, plus the pipeline's deps).

    python dashboard/server.py [--port 8765] [--no-open]

Serves dashboard/index.html, dashboard/static/ (the PDF reader) and a small
JSON API; live progress is pushed to the page with Server-Sent Events
(/api/events). One job runs at a time because downloads share one browser
profile. Reader requests (notes, AI) run in their own request threads, outside
that lock, so reading never waits for a download.
"""
import json
import mimetypes
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import traceback
import urllib.error
import urllib.parse
import zipfile
import urllib.request
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEV_SCRIPTS = os.path.join(os.path.dirname(_HERE), ".agents", "skills", "ais-journal-watcher", "scripts")
if os.path.isdir(_DEV_SCRIPTS):          # run from the project folder; as the Mac app the modules are in the bundle
    sys.path.insert(0, _DEV_SCRIPTS)
import paths  # noqa: E402     where the code (HERE: the dashboard, SCRIPTS) and your data (ROOT) are: see paths.py
HERE, ROOT, SCRIPTS = paths.DASHBOARD, paths.ROOT, paths.SCRIPTS
sys.path.insert(0, SCRIPTS)
import activity  # noqa: E402
import aifiles  # noqa: E402
import authorcache as ac  # noqa: E402
import bibimport  # noqa: E402
import refextract  # noqa: E402
import books  # noqa: E402
import exports  # noqa: E402
import folders as fo  # noqa: E402
import importer  # noqa: E402
import library_search as ls  # noqa: E402
import notes_store as ns  # noqa: E402
import pipeline as pl  # noqa: E402
import authors  # noqa: E402
import prefs  # noqa: E402
import signins  # noqa: E402
import identity  # noqa: E402
import deletion  # noqa: E402
import support  # noqa: E402
import syncinfo  # noqa: E402
import uiprefs  # noqa: E402
import updater  # noqa: E402
import keychain  # noqa: E402
import smart  # noqa: E402
import readstate as rs  # noqa: E402
import refs as rf  # noqa: E402
try:   # the reader's AI features; the dashboard still runs without them
    import ai  # noqa: E402
    import ask  # noqa: E402
    import audio  # noqa: E402
    import citeuse  # noqa: E402
    import claims  # noqa: E402
    import explain  # noqa: E402
    import evidence  # noqa: E402
    import flashcards  # noqa: E402
    import fulltext  # noqa: E402
    import ideas  # noqa: E402
    import indexer  # noqa: E402
    import matrix  # noqa: E402
    import multiask  # noqa: E402
    import papercard  # noqa: E402
    import personal  # noqa: E402
    import related  # noqa: E402
    import relatedwork  # noqa: E402
    import reviewer  # noqa: E402
    import skim  # noqa: E402
    import summaries  # noqa: E402
    import bookjob  # noqa: E402
    import synthesis  # noqa: E402
    import workspace  # noqa: E402
except ImportError as _e:
    ai = ask = audio = citeuse = claims = explain = evidence = flashcards = fulltext = ideas = indexer = papercard = matrix = multiask = related = relatedwork = reviewer = skim = summaries = synthesis = workspace = personal = bookjob = None
    print("AI features unavailable (%s) — run: pip install -r requirements.txt" % _e)

STATIC = os.path.join(HERE, "static")
mimetypes.add_type("text/javascript", ".mjs")


# ---------------------------------------------------------------- live events

class Hub:
    def __init__(self):
        self.clients = set()
        self.lock = threading.Lock()

    def subscribe(self):
        q = queue.Queue(maxsize=5000)
        with self.lock:
            self.clients.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.clients.discard(q)

    def emit(self, event, data):
        msg = "event: %s\ndata: %s\n\n" % (event, json.dumps(data, ensure_ascii=False))
        with self.lock:
            for q in list(self.clients):
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    pass


HUB = Hub()
activity.set_emitter(HUB.emit)        # the activity tray follows AI jobs through the same event stream as everything else


# ---------------------------------------------------------------- jobs

class Jobs:
    def __init__(self):
        self.lock = threading.Lock()
        self.thread = None
        self.stop = threading.Event()
        self.run = None        # the run currently shown / being worked on
        self.kind = None       # what the job is doing: fetch | download | login
        self.checks = queue.Queue()     # a download: the hosts whose "verify you are human" check the user asked to open (pipeline.download_and_summarize)
        self.waiting = {}      # a download: host -> its last 'challenge' event while that check waits for the user (a page opened or reloaded shows it again)
        self.summarizing = True    # a download: whether it writes its papers' summaries itself (the weekly check's followed authors do not)

    def busy(self):
        return self.thread is not None and self.thread.is_alive()

    def set_kind(self, kind):
        """What the running job does now (the weekly check downloads its followed authors' works as a download: "Open the check" works then)."""
        self.kind = kind
        HUB.emit("job", {"busy": True, "kind": kind, "run": self.run.to_dict(full=False) if self.run else None})

    def start(self, kind, fn, run=None):
        with self.lock:
            if self.busy():
                return False
            self.stop = threading.Event()
            self.checks = queue.Queue()
            self.waiting = {}
            self.kind = kind
            if run is not None:
                self.run = run
            self.thread = threading.Thread(target=self._wrap, args=(fn,), daemon=True)
            self.thread.start()
            return True

    def _wrap(self, fn):
        run = self.run
        try:
            if run is not None:
                run.status = "running"
                HUB.emit("job", {"busy": True, "kind": self.kind, "run": run.to_dict(full=False)})
            fn()
            if run is not None:
                run.status = "stopped" if self.stop.is_set() else "done"
        except Exception as e:
            traceback.print_exc()
            if run is not None:
                run.status = "error"
                run.errors.append(str(e)[:300])
            HUB.emit("log", {"msg": "Error: %s" % str(e)[:300], "level": "error"})
        finally:
            if run is not None:
                run.stage = "ready"
                pl.mark_downloaded(run)
                run.save(force=True)
            HUB.emit("job", {"busy": False, "kind": self.kind,
                             "run": run.to_dict(full=False) if run else None})
            if self.kind == "download" and indexer:
                indexer.kick(3)                  # new papers: read them into the index for Ask (the local model only; see indexer.py)
            self.kind = None
            self.waiting = {}


JOBS = Jobs()


def emit_for(run):
    def emit(event, data):
        if event == "progress":
            run.stage = data.get("stage", run.stage)
        HUB.emit(event, data)
    return emit


def start_fetch(run):
    def job():
        emit = emit_for(run)
        if run.kind == "weekly":
            run.checks.append(datetime.now(timezone.utc).isoformat())
            emit("log", {"msg": "Check %d for %s (new papers are added to this week)"
                         % (len(run.checks), run.title())})
        else:
            if not run.checks:
                run.checks.append(run.created)
            if run.order:                                                      # it holds papers already: this is a search run again (it keeps its papers and verdicts; "updated" shows when)
                run.checks.append(datetime.now(timezone.utc).isoformat())
            emit("log", {"msg": ("Started: %s" % run.title()) if len(run.checks) < 2 else
                         "Continuing your saved search: %s (%d papers are already in it; only papers without a verdict are screened)" % (run.title(), len(run.order))})
        follow_ids = []
        if run.kind == "weekly":
            try:
                follow_ids = pl.follow_list(run, emit, JOBS.stop)         # the authors you follow: their new works (before the feeds, which then do not add them again)
            except Exception as e:  # noqa: BLE001      (a problem with one of them must not stop the week's check)
                traceback.print_exc()
                run.errors.append("followed authors: %s" % str(e)[:200])
                emit("log", {"msg": "The followed authors could not be checked: %s" % str(e)[:200], "level": "warn"})
            if JOBS.stop.is_set():
                return
            pl.fetch_weekly(run, emit, JOBS.stop)
        else:
            pl.fetch_search(run, emit, JOBS.stop)
        pl.mark_downloaded(run)
        if run.params.get("terms"):             # a search by terms: the matches are shown as they are (free); "Screen with AI" asks the model only if you want
            if JOBS.stop.is_set():
                emit("log", {"msg": "Stopped: %d papers found so far contain your terms. Search again to continue." % len(run.order), "level": "warn"})
                return
            emit("log", {"msg": "Finished: %d papers contain your terms. Tick the ones you want and press Download, or press “Screen with AI” to rank them against your interests." % len(run.order), "level": "ok"})
            return
        failed = None
        if not JOBS.stop.is_set() and run.order:
            try:
                pl.screen(run, emit, JOBS.stop)
            except Exception as e:  # noqa: BLE001      (the followed authors' works are downloaded all the same; the error is raised again after them)
                failed = e
        if follow_ids and not JOBS.stop.is_set():
            follow_download(run, follow_ids, emit)
        if failed is not None:
            raise failed
        n_dl = sum(1 for p in run.papers.values() if p.get("verdict") == "download")
        n_bl = sum(1 for p in run.papers.values() if p.get("verdict") == "borderline")
        emit("log", {"msg": "Finished: %d papers, %d to download, %d borderline · $%.2f"
                     % (len(run.order), n_dl, n_bl, run.usage.cost()), "level": "ok"})
    return JOBS.start("fetch", job, run)


def _download_emit(base):
    """emit for a download: it also keeps which checks wait for the user, so that a page opened (or reloaded) later still offers "Open the check"."""
    def emit(event, data):
        if event == "challenge":
            if data.get("state") in ("pending", "again"):
                JOBS.waiting[data["host"]] = data
            elif data.get("state") == "done":
                JOBS.waiting.clear()
            elif data.get("state") in ("open", "passed", "failed", "cleared"):
                JOBS.waiting.pop(data.get("host"), None)
        base(event, data)
    return emit


def follow_download(run, ids, emit):
    """The weekly check, after the screening: download the followed authors' new works into their folders. A human check never opens by itself here (you started
    the check for the feeds, not for these papers): the page offers "Open the check", as for any download. No summary is written (no AI call): the Library's
    "Summarise N papers…" does that, with its estimate first. A work that does not come is tried again next week (pipeline.follow_settle)."""
    JOBS.summarizing = False
    JOBS.set_kind("download")
    try:
        pl.download_and_summarize(run, ids, _download_emit(emit), JOBS.stop, checks=JOBS.checks, auto_open=False, summarize=False)
    finally:
        JOBS.summarizing = True
        ok, again = pl.follow_settle(run, ids)
        JOBS.waiting.clear()
        JOBS.set_kind("fetch")
        emit("log", {"msg": "Followed authors: %d new work%s downloaded into their folders%s. Their summaries are not written by themselves: Library → “Summarise N papers…”." % (
            ok, "" if ok == 1 else "s", ", %d to try again next week" % again if again else ""), "level": "ok"})
        if ok and indexer:
            indexer.kick(3)


ATTACH_LOCK = threading.Lock()      # one PDF you add at a time: a run loaded from its file is changed and saved whole (two at once would each save their own copy)


def attach_to_run(rid, fn):
    """fn(run) for a paper's PDF you add: the open run itself, or the run read from its file. None if there is no such run."""
    rid = str(rid or "")
    with ATTACH_LOCK:
        run = JOBS.run if JOBS.run and JOBS.run.id == rid else (pl.Run.load(rid) if rid else None)
        return fn(run) if run else None


def start_download(ids, prepare=None):
    """Download these papers of the open run in the background. `prepare(emit)`, when given, runs first inside the same job and returns the ids (an author's works
    are listed there: it can take a while, and a "verify you are human" check during the downloads works as for any download)."""
    run = JOBS.run
    emit = _download_emit(emit_for(run))

    def job():
        todo = prepare(emit) if prepare else ids
        if not todo:
            return
        pl.download_and_summarize(run, todo, emit, JOBS.stop, checks=JOBS.checks)
        ok = sum(1 for i in todo if run.papers.get(i, {}).get("dl_status") == "ok")
        HUB.emit("log", {"msg": "Downloads finished: %d of %d" % (ok, len(todo)), "level": "ok"})
    return JOBS.start("download", job)


def _why(r):
    """The reason a helper process gave for failing: the last line of what it printed to stderr (a Python traceback ends with it)."""
    lines = [l.strip() for l in (r.stderr or "").splitlines() if l.strip() and not l.strip().startswith(("║", "╔", "╚", "File ", "^", "~"))]
    return lines[-1][:200] if r.returncode != 0 and lines else ""


# "Check my PDFs": is every PDF the paper it is filed under (identity.py)? It reads the first pages of each PDF in the background and keeps its answers here until the app closes.
PDFCHECK = {"state": "idle", "done": 0, "total": 0, "results": [], "counts": {}, "at": "", "error": ""}
PDFCHECK_LOCK = threading.Lock()


def start_pdfcheck():
    with PDFCHECK_LOCK:
        if PDFCHECK["state"] == "running":
            return False
        PDFCHECK.update(state="running", done=0, total=0, results=[], counts={}, error="")

    def progress(n, total):
        PDFCHECK.update(done=n, total=total)

    def job():
        try:
            res = identity.audit(progress, use_kept=True)
            counts = {}
            for r in res:
                counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
            PDFCHECK.update(results=[r for r in res if r["verdict"] in ("mismatch", "unverified", "missing")], counts=counts, state="done",
                            at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            PDFCHECK.update(state="error", error="The check stopped: %s" % str(e)[:200])
    threading.Thread(target=job, daemon=True).start()
    return True


# ---------------------------------------------------------------- where papers come from: OpenAlex, Unpaywall, Elsevier (Settings → Paper sources)

SOURCES = {"openalex_key": "OPENALEX_API_KEY", "unpaywall_email": "UNPAYWALL_EMAIL", "elsevier_key": "ELSEVIER_API_KEY", "elsevier_insttoken": "ELSEVIER_INSTTOKEN"}
SECRET_SOURCES = set(pl.dl.SOURCE_KEYS)          # the keys among them: kept in this Mac's Keychain where there is one (the data folder is synced), as the AI keys are


def _env_get(name):
    return os.environ.get(name) or pl.dl.load_env().get(name, "")


def sources_state():
    """What is set (keys are never sent back: only whether there is one; the Unpaywall email is shown, it is yours and not a secret)."""
    return {"openalex_key": bool(_env_get("OPENALEX_API_KEY")), "unpaywall_email": _env_get("UNPAYWALL_EMAIL"),
            "elsevier_key": bool(_env_get("ELSEVIER_API_KEY")), "elsevier_insttoken": bool(_env_get("ELSEVIER_INSTTOKEN")),
            "openalex_spent": pl.fx.openalex_spent(), "keychain": keychain.available(),
            "where": {f: _key_where(n) for f, n in SOURCES.items() if n in SECRET_SOURCES}}


def _key_where(name):
    """".env file", "Keychain", "environment" or "" (as ai.key_where, without needing the AI modules)."""
    if pl.dl.read_dotenv().get(name):
        return ".env file"
    if keychain.values([name]).get(name):
        return "Keychain"
    return "environment" if os.environ.get(name) else ""


def _check_openalex(key):
    """Ask OpenAlex whether the key is good: one free lookup (by ID) with the key. 401/403 = a key OpenAlex does not know. None when OpenAlex cannot be reached."""
    req = urllib.request.Request("https://api.openalex.org/works/W2741809807", headers={"Authorization": "Bearer " + key, "User-Agent": updater.user_agent()})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        return False if e.code in (401, 403) else None
    except Exception:  # noqa: BLE001
        return None


def sources_save(b):
    """Save the fields sent ({openalex_key, unpaywall_email, elsevier_key, elsevier_insttoken}; "" removes one). A key goes to this Mac's Keychain where there is
    one (and its line leaves the synced .env file); without a Keychain, or for a key it cannot hold, it goes into the data home's .env (mode 600), as before.
    The Unpaywall email, which is not a secret, always goes into .env."""
    new = {}
    for field, name in SOURCES.items():
        if field not in b:
            continue
        v = str(b.get(field) or "").strip()
        if v and field == "unpaywall_email" and not re.fullmatch(r"[^@\s\"'\\]+@[^@\s\"'\\]+\.[^@\s\"'\\]+", v):
            raise ValueError("Unpaywall needs an email address: it is sent only to Unpaywall, with each lookup, so that it knows who is asking.")
        if v and (len(v) > 400 or re.search(r"[\s\"'\\]", v)):
            raise ValueError("That doesn’t look right: paste just the key, with no spaces or quotes.")
        if v and field == "openalex_key" and _check_openalex(v) is False:
            raise ValueError("OpenAlex does not recognise that key. Copy it again from openalex.org/settings/api.")
        new[name] = v
    in_kc = set()
    for name in sorted(SECRET_SOURCES & set(new)):
        if not keychain.available():
            break
        v = new[name]
        try:
            if v:
                keychain.set(name, v)
                if keychain.get(name) == v:            # (read back before its line leaves the file)
                    in_kc.add(name)
            elif keychain.get(name):
                keychain.delete(name)
        except ValueError:                     # a key the Keychain helper cannot take safely: the file, as before
            pass
    gone = [n for n, v in new.items() if not v] + sorted(in_kc)          # (a key now in the Keychain leaves the older layout's file too)
    old_env = os.path.join(paths.BASE, ".env")                  # (the older layout kept keys in the settings folder: a removed key must go from there too)
    if gone and os.path.exists(old_env) and os.path.abspath(old_env) != os.path.abspath(os.path.join(ROOT, ".env")):
        keep_ = [l for l in open(old_env, encoding="utf-8").read().splitlines() if not any(re.match(r"\s*%s\s*=" % re.escape(n), l) for n in gone)]
        with open(old_env, "w", encoding="utf-8") as f:
            f.write("\n".join(keep_) + ("\n" if keep_ else ""))
    path = os.path.join(ROOT, ".env")
    lines = open(path, encoding="utf-8").read().splitlines() if os.path.exists(path) else []
    lines = [l for l in lines if not any(re.match(r"\s*(export\s+)?%s\s*=" % re.escape(n), l) for n in new)] + ["%s=%s" % (n, v) for n, v in new.items() if v and n not in in_kc]
    tmp = path + ".sources.tmp"                                    # (not ai.set_key's .env.tmp: two saves at once must not write the same scratch file)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    os.replace(tmp, path)
    for n, v in new.items():
        if v:
            os.environ[n] = v
        else:
            os.environ.pop(n, None)
    if new.get("OPENALEX_API_KEY") is not None:
        pl.fx.OPENALEX_SPENT.update(until=0.0)         # a new key: its own allowance
    return sources_state()


LOGIN_PENDING = {"site": ""}        # which sign-in window is open right now ("" = none)
LIBTEST_LOCK = threading.Lock()     # one "Test my access" at a time


def start_login(which="publishers"):
    """Open a browser window for one kind of sign-in (informs | ais | library | publishers) and remember whether it worked: signins.record, and a "signin" event
    ({site, state: opened|done, signed_in, error, status}) so the page can tick the right row."""
    which = "library" if which == "nova" else which
    if which not in signins.SITES:
        which = "publishers"

    def job():
        LOGIN_PENDING["site"] = which
        lib = signins.profile()
        label = {"informs": "INFORMS", "ais": "AIS", "library": lib["name"] or "Your library", "publishers": "The publisher sign-in pages"}[which]
        HUB.emit("signin", {"site": which, "state": "opened", "name": label})
        try:
            HUB.emit("log", {"msg": ("%s sign-in opened: enter your password; the window closes by itself." % label) if which != "publishers" else "Login window opened — sign in on each tab you have access to, then close the browser window."})
            cmd = {"informs": ("member-login", "informs"), "ais": ("member-login", "ais"), "library": ("library-login",), "publishers": ("login",)}[which]
            r = subprocess.run(paths.cmd("browser.py", *cmd), cwd=ROOT, capture_output=True, text=True)
            ok = "Signed in to" in r.stdout if which != "publishers" else r.returncode == 0
            why = "" if ok else _why(r)
            signins.record(which, ok)
            msg = {"informs": "Signed in to INFORMS.", "ais": "Signed in to AIS.", "library": "Signed in to %s." % label, "publishers": "Sign-in window closed; the sessions are saved."}[which]
            HUB.emit("log", {"msg": msg if ok else "%s sign-in not completed.%s" % (label, " " + why if why else ""), "level": "ok" if ok else "warn"})
            HUB.emit("signin", {"site": which, "state": "done", "signed_in": ok, "error": why, "name": label, "status": signins.status()})
        finally:
            LOGIN_PENDING["site"] = ""
    return JOBS.start("login", job)


# ---------------------------------------------------------------- http

def config():
    import datetime
    y = datetime.date.today().year
    return {
        "journals": [{"abbr": j["abbr"], "name": j["name"], "publisher": j["publisher"],
                      "per_year": j.get("per_year"), "archive": j.get("archive", True), "needs_query": bool(j.get("needs_query")), "default_off": bool(j.get("default_off")), "limit": j.get("limit"), "weekly": bool(j.get("weekly", True)), "archive_note": j.get("archive_note") or ""}
                     for j in pl.journals()],
        "topics": pl.topic_labels(),
        "methods": [{"label": m.get("label", m["name"]), "description": m.get("description", "")}
                    for m in pl.methods()],
        "years": list(range(y, 1999, -1)),
        "openalex_key": bool(pl.fx.openalex_key()),
        **dict(zip(("est_cost_per_paper", "est_cost_basis"), pl.screen_estimate())),      # with the model chosen for screening: per paper, and "history" (what your earlier screenings cost) or "prices"; None when not known (the page says so)
        "screen_model": ai.pricing.name(*ai.feature_model("screen")) if ai else "Claude Sonnet 5.5",
        "screen_feature": dict(zip(("provider", "model"), ai.feature_model("screen"))) if ai else {},     # (which provider and model: a backup on a run's page is compared with it)
        "model": pl.MODEL,
        "this_week": pl.week_of(),
        "this_week_label": pl.week_label(pl.week_of()),
        "home": ROOT,                                            # where your data lives (the welcome guide and Settings say so)
        "app": {"frozen": paths.FROZEN, "version": updater.version()},
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if "/api/events" not in str(args[0] if args else ""):
            sys.stderr.write("  %s\n" % (fmt % args))

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, ctype=None, cache="no-store"):
        """Whole file or a byte range (PDF.js reads PDFs in ranges). cache="no-cache" adds an ETag so the
        browser revalidates instead of re-downloading a 20 MB PDF every time."""
        if not os.path.isfile(path):
            return self.send_json({"error": "not found"}, 404)
        st = os.stat(path)
        size = st.st_size
        etag = '"%x-%x"' % (st.st_mtime_ns, size)
        if cache == "no-cache" and self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
            return
        start, end, code = 0, size - 1, 200
        m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range") or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start, end = int(m.group(1)), min(int(m.group(2) or end), size - 1)
            else:  # "-N": the last N bytes
                start = max(0, size - int(m.group(2)))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            code = 206
        self.send_response(code)
        self.send_header("Content-Type", ctype or mimetypes.guess_type(path)[0] or "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", cache)
        if cache == "no-cache":
            self.send_header("ETag", etag)
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the viewer cancelled a range it no longer needs

    def body(self):
        """The request's JSON object ({} when there is none). Anything else (not JSON, or JSON that is not an object) is a ValueError: the caller answers 400."""
        n = int(self.headers.get("Content-Length") or 0)
        b = json.loads(self.rfile.read(n) or b"{}") if n else {}
        if not isinstance(b, dict):
            raise ValueError("the request is not a JSON object")
        return b

    LOCAL = ("localhost", "127.0.0.1", "::1")

    def host_ok(self):
        """The request names this computer as its host. A page on another site whose name was pointed at 127.0.0.1 (DNS rebinding) names its own host, and
        would otherwise read the library through this server, which has no login."""
        return (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower() in self.LOCAL

    def local_only(self):
        """Host/Origin guard for POSTs: they spend API money and write notes, and the server has no login, so refuse
        anything that isn't the dashboard page itself (cross-site forms, DNS rebinding)."""
        if not self.host_ok():
            return False
        origin = self.headers.get("Origin")
        return not origin or (urllib.parse.urlsplit(origin).hostname or "") in self.LOCAL

    def get_ok(self, path):
        """Guard for GETs: the host must be this computer, and data (/api/, /files/) is not handed to a request another site made (an image or script tag, a
        fetch: the browser marks those "cross-site"). Opening the page itself from a link elsewhere stays allowed."""
        if not self.host_ok():
            return False
        return not ((path.startswith("/api/") or path.startswith("/files/")) and (self.headers.get("Sec-Fetch-Site") or "").lower() == "cross-site")

    def send_response(self, code, message=None):
        self._answered = True                       # (an answer has begun: a later error cannot be sent as JSON any more)
        super().send_response(code, message)

    def guarded(self, fn):
        """Run a request. An error nobody caught is answered as JSON with status 500 (and printed), so the page can say what happened instead of
        "can't reach the dashboard"; the connection used to be dropped without an answer."""
        self._answered = False
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            if self._answered:                      # (a stream or a file was already on its way: end it)
                if not isinstance(e, (ConnectionResetError, BrokenPipeError, TimeoutError)):          # (those: the browser left, which is no news)
                    traceback.print_exc()
                self.close_connection = True
                return None
            traceback.print_exc()                   # (also a time-out or a reset from a site the app itself was asking: the page is told, not left waiting)
            try:
                self.send_json({"error": "Something went wrong inside the app (%s: %s). Try again; if it happens again, the details are in the app's log."
                                         % (e.__class__.__name__, str(e)[:160])}, 500)
            except OSError:
                pass
            self.close_connection = True            # (the request's body may not have been read to its end)
            return None

    def send_bytes(self, data, ctype, filename=None):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if filename:
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(filename))
        self.end_headers()
        self.wfile.write(data)

    def send_temp_file(self, path, ctype, filename):
        """Stream a temporary file as a download, then delete it."""
        try:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(os.path.getsize(path)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(filename))
            self.end_headers()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def rows_for(self, q):
        """Library rows picked by ?folder=<id> or one-or-more ?file=<Papers/…pdf>; no selector = the whole library."""
        rows = pl.library()
        if q.get("folder"):
            f = fo.get(q["folder"][0])
            if not f:
                return None
            keys = set(f["papers"])
            return [r for r in rows if r["key"] in keys]
        if q.get("file"):
            want = set(q["file"])
            return [r for r in rows if r["file"] in want]
        return rows

    def author_download(self, b, follow=False):
        """Every work of the author ids in b (journal, conference, working papers) into the author's folder, as one download job. follow=True also follows the
        author: what this listing found counts as dealt with, and the weekly check brings the works OpenAlex lists after it."""
        ids = [authors._short(x).upper() for x in (b.get("ids") or []) if re.fullmatch(r"[Aa]\d{4,12}", authors._short(x))][:10]
        name = re.sub(r"\s+", " ", str(b.get("name") or "")).strip()[:60]
        if not ids or not name:
            return self.send_json({"error": "Pick the author first."}, 400)
        fid, _ = authors.folder_for(ids, name, str(b.get("institution") or ""))
        run = pl.Run("author", {"label": name, "author_ids": ids, "folder": fid, "folder_name": (fo.get(fid) or {}).get("name", name)})
        run.stage, run.status = "ready", "stopped"
        run.save(force=True)
        JOBS.run = run

        def prepare(emit):
            lab = "Listing %s's works on OpenAlex" % name
            emit("progress", {"stage": "listing", "label": lab, "done": 0, "total": 0})

            def said(listing, msg, level):        # the run says what came of the listing (its page shows it at once, and after a reload)
                run.params["listing"] = listing
                run.save(force=True)
                emit("run", {"id": run.id, "params": run.params})
                emit("log", {"msg": msg, "level": level})
            try:
                ws, complete, total = authors.works(ids, progress=lambda n, t: emit("progress", {"stage": "listing", "label": lab, "done": n, "total": t}), stop=JOBS.stop)
            except authors.AuthorError as e:
                run.errors.append(str(e))
                said({"error": str(e)}, "Listing %s's works failed: %s" % (name, e), "error")
                return []
            if JOBS.stop.is_set():
                said({"stopped": True, "listed": len(ws)}, "Stopped while listing %s's works: nothing was downloaded or filed." % name, "warn")
                return []
            if follow:                            # followed once the listing worked; what it listed (complete or not) is not new next week (it would all be downloaded again)
                authors.set_follow(fid, True)
                authors.follow_mark(fid, [w.get("id") for w in ws], seed=True)
                run.params["follow"] = True                    # (said on the run's page only now: a listing that failed follows no one)
            kept, excluded = authors.select(ws)
            new, have, gone = authors.split(authors.to_papers(kept, fid, name))
            authors.file_have(fid, have)
            added = [p for p in new if run.add_paper(p)]
            for p in added:
                emit("paper", pl.slim(p))
            said({"complete": complete, "listed": len(ws), "total": total, "kept": len(kept), "excluded": excluded, "have": len(have), "deleted": len(gone), "new": len(added)},
                 "%s: %d works listed%s; %d already in your library (now in the folder too), %d you deleted (left out), %d to download.%s" % (
                     name, len(ws), "" if complete else " (OpenAlex stopped answering: the list is incomplete; search again later for the rest)", len(have), len(gone), len(added),
                     " Followed: the weekly check brings their new works into the folder." if follow else ""),
                 "ok" if complete else "warn")
            return [p["id"] for p in added]
        if not start_download([], prepare=prepare):
            return self.send_json({"error": "Another task is running — wait or press Stop."}, 409)
        return self.send_json({"ok": True, "run": run.to_dict(full=False), "folder": fid})

    def folder_rows(self, fid):
        f = fo.get(fid)
        if not f:
            return None, None
        keys = set(f["papers"])
        return f, [r for r in pl.library() if r["key"] in keys]

    def scope_rows(self, s):
        """Library rows picked by a scope: {folder?, journal?, year?, tag?, status?, star?} (all optional; none = the whole library)."""
        rows = pl.library()
        if s.get("folder") == ls.NO_FOLDER:                     # ("Not in any folder" in the Library's filters)
            rows = [r for r in rows if not r.get("folders")]
        elif s.get("folder"):
            f = fo.get(str(s["folder"]))
            if not f:
                return None
            keys = set(f["papers"])
            rows = [r for r in rows if r["key"] in keys]
        if any(s.get(k) for k in ("journal", "year", "tag", "status", "star")):
            keep = {h["file"] for h in ls.search(rows, "", str(s.get("status") or ""), bool(s.get("star")), "", str(s.get("journal") or ""), str(s.get("year") or ""), str(s.get("tag") or ""))}
            rows = [r for r in rows if r["file"] in keep]
        return rows

    def export(self, what, q):
        if what == "matrix":
            f, rows = self.folder_rows((q.get("folder") or [""])[0]) if matrix else (None, None)
            if not f:
                return self.send_json({"error": "open a folder first"}, 404)
            fmt = (q.get("fmt") or ["csv"])[0]
            makers = {"csv": (lambda: matrix.csv_bytes(f["id"], rows), "text/csv; charset=utf-8"), "md": (lambda: matrix.markdown(f["id"], rows).encode(), "text/markdown; charset=utf-8"),
                      "tex": (lambda: matrix.latex(f["id"], rows).encode(), "application/x-tex; charset=utf-8"),
                      "docx": (lambda: matrix.docx_bytes(f["id"], rows), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}
            if fmt not in makers:
                return self.send_json({"error": "unknown format"}, 400)
            return self.send_bytes(makers[fmt][0](), makers[fmt][1], "%s-comparison.%s" % (f["name"], fmt))
        if what == "draft":
            f = fo.get((q.get("folder") or [""])[0]) if relatedwork else None
            doc = relatedwork.load(f["id"]) if f else None
            if not doc:
                return self.send_json({"error": "write the related-work draft first"}, 404)
            if (q.get("fmt") or ["md"])[0] == "docx":
                return self.send_bytes(relatedwork.docx(doc, f["name"]), "application/vnd.openxmlformats-officedocument.wordprocessingml.document", f["name"] + "-related-work.docx")
            if (q.get("fmt") or ["md"])[0] == "tex":
                return self.send_bytes(relatedwork.latex(doc, f["name"]).encode(), "application/x-tex; charset=utf-8", f["name"] + "-related-work.tex")
            if (q.get("fmt") or ["md"])[0] == "bib":      # the references of the papers the text cites, with the keys \\citep uses
                cited = [r for r in self.folder_rows(f["id"])[1] if r["file"] in {p["file"] for pid, p in doc["papers"].items() if pid in relatedwork.render(doc)["cited"]}]
                return self.send_bytes(exports.bibtex(cited).encode(), "application/x-bibtex; charset=utf-8", f["name"] + "-related-work.bib")
            return self.send_bytes(relatedwork.markdown(doc, f["name"]).encode(), "text/markdown; charset=utf-8", f["name"] + "-related-work.md")
        if what == "workspace":
            f, rows = self.folder_rows((q.get("folder") or [""])[0]) if workspace else (None, None)
            doc = workspace.load(f["id"]) if f else None
            if not doc or not (doc["cards"] or doc["frames"]):
                return self.send_json({"error": "the workspace is empty"}, 404)
            if (q.get("fmt") or ["md"])[0] == "docx":
                return self.send_bytes(workspace.docx(doc, f["name"], rows), "application/vnd.openxmlformats-officedocument.wordprocessingml.document", f["name"] + "-workspace.docx")
            return self.send_bytes(workspace.markdown(doc, f["name"], rows).encode(), "text/markdown; charset=utf-8", f["name"] + "-workspace.md")
        if what == "review":
            f = fo.get((q.get("folder") or [""])[0]) if reviewer else None
            doc = reviewer.load(f["id"]) if f else None
            if not doc:
                return self.send_json({"error": "ask Reviewer 2 first"}, 404)
            return self.send_bytes(reviewer.markdown(doc, f["name"]).encode(), "text/markdown; charset=utf-8", f["name"] + "-reviewer2.md")
        if what == "synthesis":
            f = fo.get((q.get("folder") or [""])[0]) if synthesis else None
            doc = synthesis.load(f["id"]) if f else None
            if not doc:
                return self.send_json({"error": "make the synthesis first"}, 404)
            if (q.get("fmt") or ["md"])[0] == "docx":
                return self.send_bytes(synthesis.docx(doc, f["name"]), "application/vnd.openxmlformats-officedocument.wordprocessingml.document", f["name"] + "-synthesis.docx")
            return self.send_bytes(synthesis.markdown(doc, f["name"]).encode(), "text/markdown; charset=utf-8", f["name"] + "-synthesis.md")
        if what == "claims":
            f = fo.get((q.get("folder") or [""])[0]) if claims else None
            doc = claims.load(f["id"]) if f else None
            if not doc:
                return self.send_json({"error": "check some claims first"}, 404)
            if (q.get("fmt") or ["md"])[0] == "docx":
                return self.send_bytes(claims.docx(doc, f["name"]), "application/vnd.openxmlformats-officedocument.wordprocessingml.document", f["name"] + "-claim-check.docx")
            return self.send_bytes(claims.markdown(doc, f["name"]).encode(), "text/markdown; charset=utf-8", f["name"] + "-claim-check.md")
        if what == "ideas":
            f = fo.get((q.get("folder") or [""])[0]) if ideas else None
            if not f:
                return self.send_json({"error": "open a folder first"}, 404)
            if (q.get("fmt") or ["md"])[0] == "docx":
                return self.send_bytes(ideas.docx(f["id"]), "application/vnd.openxmlformats-officedocument.wordprocessingml.document", f["name"] + "-ideas.docx")
            return self.send_bytes(ideas.markdown(f["id"]).encode(), "text/markdown; charset=utf-8", f["name"] + "-ideas.md")
        if what == "cards":
            rows = self.rows_for(q) or []
            parts = []
            for r in rows:
                pdf = ns.pdf_path(r["file"]); card = papercard.load(pdf) if pdf and papercard else None
                if card:
                    parts.append(papercard.markdown(card, r.get("title", "")))
            if not parts:
                return self.send_json({"error": "none of these papers has a card yet"}, 404)
            return self.send_bytes("\n\n---\n\n".join(parts).encode(), "text/markdown; charset=utf-8", ((fo.get(q["folder"][0]) or {}).get("name") if q.get("folder") else "paper") + "-cards.md")
        rows = self.rows_for(q)
        if rows is None or not rows:
            return self.send_json({"error": "nothing to export"}, 404)
        name = "library"
        if q.get("folder"):
            name = fo.get(q["folder"][0])["name"]
        elif len(rows) == 1:
            name = os.path.basename(rows[0]["file"])[:-4]
        if what == "bib":
            return self.send_bytes(exports.bibtex(rows).encode(), "application/x-bibtex; charset=utf-8", name + ".bib")
        if what == "ris":
            return self.send_bytes(exports.ris(rows).encode(), "application/x-research-info-systems; charset=utf-8", name + ".ris")
        if what == "cites":     # the keys of the .bib file as one LaTeX command: \citep{rai2026fair,lee2020…}
            keys = [e["key"] for e in exports.entries(rows)]
            return self.send_bytes(("\\citep{%s}\n" % ",".join(keys)).encode(), "text/plain; charset=utf-8", name + "-cites.tex")
        if what == "notes":
            if (q.get("fmt") or ["md"])[0] == "docx":
                return self.send_bytes(exports.notes_docx(rows, name if len(rows) > 1 else None),
                                       "application/vnd.openxmlformats-officedocument.wordprocessingml.document", name + "-notes.docx")
            return self.send_bytes(exports.notes_markdown(rows, name if len(rows) > 1 else None).encode(), "text/markdown; charset=utf-8", name + "-notes.md")
        if what == "pdf" and len(rows) == 1:
            pdf = ns.pdf_path(rows[0]["file"])
            return self.send_bytes(exports.annotated_pdf(pdf, ns.load(pdf), ns.load_tags()["tags"]), "application/pdf", name + "-annotated.pdf")
        self.send_json({"error": "unknown export"}, 400)

    def stream_ndjson(self, messages, job=None):
        """Send dicts from a generator as they are produced, one JSON object per line (the reader reads them live).
        job = (kind, label, scope): shown in the activity tray with its progress and cost, and cancellable there."""
        jb = activity.start(job[0], job[1], scope=job[2]) if job else None
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for m in messages:
                if jb:
                    jb.observe(m)
                self.wfile.write((json.dumps(m, ensure_ascii=False) + "\n").encode())
                self.wfile.flush()
                if jb and jb.cancelled and not jb.completed:     # Cancel in the tray: stop paying for the answer, tell the page
                    messages.close()
                    self.wfile.write((json.dumps({"type": "error", "error": "Cancelled."}) + "\n").encode())
                    self.wfile.flush()
                    break
        except (BrokenPipeError, ConnectionResetError):
            messages.close()          # the reader navigated away: stop paying for the answer
            if jb:
                jb.cancel_ev.set()
        finally:
            if jb:
                jb.finish()
        self.close_connection = True

    def job_of(self, kind, label, **scope):
        return (kind, label, scope)

    def title_of(self, rel):
        return str(self.doc_info(rel)[0].get("title") or "this paper")[:64]

    def attached(self, res):
        """Answer a PDF you added to a paper of a run (pipeline.attach_pdf / attach_keep): the open pages show it at once. No summary is written."""
        if res.get("filed"):
            p = res["paper"]
            HUB.emit("download", {k: p.get(k) for k in ("id", "dl_status", "dl_note", "file", "summary", "manual_url", "summary_by_hand")})
            HUB.emit("verdict", {k: p.get(k) for k in ("id", "verdict", "relevance", "topics", "method", "reason", "ai_verdict", "user_set")})
            HUB.emit("log", {"msg": "Your PDF of “%s” was added to the library." % (p.get("title") or "")[:60], "level": "ok"})
            if indexer and not res.get("existing"):
                indexer.kick(2)
        return self.send_json({"ok": True, **res})

    def finish_import(self, res, summarize):
        """Answer an import; if asked (and the paper is new), write its summary in the background like the pipeline does."""
        row = next((r for r in pl.library() if r["file"] == res["file"]), {})
        if indexer and not res["existing"]:
            indexer.kick(2)
        if summarize and not res["existing"] and not row.get("summary"):
            def job():
                try:
                    pl.summarize({"title": row.get("title"), "journal": row.get("journal"), "doi": row.get("doi")},
                                 os.path.join(ROOT, res["file"]), pl.Usage())
                    HUB.emit("log", {"msg": "Summary written for “%s”." % res["title"][:60], "level": "ok"})
                except Exception as e:  # noqa: BLE001
                    HUB.emit("log", {"msg": "Couldn’t summarize “%s”: %s" % (res["title"][:50], str(e)[:100]), "level": "warn"})
            threading.Thread(target=job, daemon=True).start()
        return self.send_json({"ok": True, **res, "row": row})

    def doc_info(self, rel):
        """Library row of a paper (by its Papers/… path) and the small identity record stored in its notes."""
        row = next((r for r in pl.library() if r["file"] == rel), None) or {}
        key = (row.get("doi") or row.get("title") or "").lower()
        return row, {"id": key, "title": row.get("title", ""), "doi": row.get("doi", "")}

    # -- GET
    def do_GET(self):
        if not self.get_ok(urllib.parse.urlsplit(self.path).path):
            return self.send_json({"error": "forbidden"}, 403)
        return self.guarded(self.get)

    def get(self):
        u = urllib.parse.urlsplit(self.path)
        path = u.path
        if path in ("/", "/index.html"):
            return self.send_file(os.path.join(HERE, "index.html"), "text/html; charset=utf-8")
        if path == "/api/events":
            return self.events()
        if path == "/api/config":
            return self.send_json(config())
        if path == "/api/signins":      # who is signed in (informs, ais, library, publishers), and which sign-in window is open now
            return self.send_json({**signins.status(), "pending": LOGIN_PENDING["site"]})
        if path == "/api/sources":
            return self.send_json(sources_state())
        if path == "/api/library/access":   # which university library you use and how it lets you in
            return self.send_json(signins.profile())
        if path == "/api/update":       # the version, whether a newer one is announced, its notes, the progress of an update that is running
            return self.send_json(updater.public())
        if path == "/api/support":      # the author's page, and whether the reminder about supporting the app is due (nothing leaves this computer)
            return self.send_json(support.public())
        if path == "/api/state":
            run = JOBS.run
            return self.send_json({"busy": JOBS.busy(), "kind": JOBS.kind, "checks": list(JOBS.waiting.values()),
                                   "run": run.to_dict() if run else None})
        if path == "/api/runs":
            return self.send_json(pl.list_runs())
        if path.startswith("/api/runs/"):
            rid = path.rsplit("/", 1)[-1]
            if JOBS.run and JOBS.run.id == rid:
                return self.send_json(JOBS.run.to_dict())
            run = pl.Run.load(rid)
            if not run:
                return self.send_json({"error": "no such run"}, 404)
            if not JOBS.busy():
                pl.mark_downloaded(run)
                JOBS.run = run
            return self.send_json(run.to_dict())
        if path == "/api/library":
            return self.send_json(pl.library())
        if path == "/api/summaries":    # Library → "Summarise N papers…": the papers without a summary, the model, what it may cost (and on what that rests), and the job's state
            if not summaries:          # ?status=1: only the job's state (cheap: the Library asks for it when it opens, so it must not read the whole library again)
                return self.send_json({"error": "AI features aren't installed"}, 501)
            if urllib.parse.parse_qs(u.query).get("status"):
                return self.send_json({"status": summaries.status()})
            return self.send_json({**summaries.estimate(), "status": summaries.status()})
        if path == "/api/library/check":
            return self.send_json(PDFCHECK)
        if path.startswith("/files/"):
            full = ns.safe_papers_path(urllib.parse.unquote(path[len("/files/"):]))
            if not full:
                return self.send_json({"error": "forbidden"}, 403)
            ctype = "text/markdown; charset=utf-8" if full.endswith(".md") else None
            return self.send_file(full, ctype, "no-cache")
        if path.startswith("/static/"):  # the reader's own JS/CSS and the vendored PDF.js
            try:
                full = os.path.realpath(os.path.join(STATIC, urllib.parse.unquote(path[len("/static/"):])))
            except ValueError:
                full = ""
            if not full.startswith(os.path.realpath(STATIC) + os.sep):
                return self.send_json({"error": "forbidden"}, 403)
            return self.send_file(full, cache="no-cache")
        if path == "/api/doc":  # one paper's notes + the shared tags, for the reader
            rel = (urllib.parse.parse_qs(u.query).get("file") or [""])[0]
            pdf = ns.pdf_path(rel)
            if not pdf:
                return self.send_json({"error": "no such paper"}, 404)
            row, doc = self.doc_info(rel)
            return self.send_json({"file": rel, "meta": row, "notes": ns.load(pdf, doc), "tags": ns.load_tags()})
        if path == "/api/tags":
            return self.send_json(ns.load_tags())
        q = urllib.parse.parse_qs(u.query)
        one = lambda k, d="": (q.get(k) or [d])[0]  # noqa: E731
        if path == "/api/doc/chat":     # the Ask tab's saved conversation
            pdf = ns.pdf_path(one("file"))
            return self.send_json(ns.load_chat(pdf) if pdf else {"error": "no such paper"}, 200 if pdf else 404)
        if path in ("/api/doc/ask/estimate", "/api/doc/summary/estimate"):     # what the next question (?file=&from=&to=&hist=) or "Generate" in the Summary tab (?file=&template=) costs; nothing is sent
            pdf = ns.pdf_path(one("file"))
            if not pdf:
                return self.send_json({"error": "no such paper"}, 404)
            if not ask:
                return self.send_json({"error": "AI features aren't installed"}, 501)
            if path == "/api/doc/summary/estimate":
                if books.is_book(one("file")):                 # (as POST /api/ai/summary: a book is summarised chapter by chapter)
                    return self.send_json({"error": books.BOOK_NO_TEMPLATE}, 400)
                return self.send_json(ask.summary_estimate(pdf, self.doc_info(one("file"))[0], one("template") or None))
            pg = None
            try:
                if one("from") or one("to"):                   # one chapter of a book, as {from, to} in POST /api/ai/ask
                    pg = (int(one("from")), int(one("to")))
                hist = int(one("hist", "0") or 0)              # the length of the conversation that would go along
            except ValueError:
                return self.send_json({"error": "bad chapter range"}, 400)
            if pg and (pg[0] < 1 or pg[1] < pg[0]):
                return self.send_json({"error": "bad chapter range"}, 400)
            return self.send_json(ask.estimate(pdf, pg, hist))
        if path == "/api/doc/blocks":    # the paper as paragraphs in reading order, for the text view
            pdf = ns.pdf_path(one("file"))
            if not pdf:
                return self.send_json({"error": "no such paper"}, 404)
            try:
                import blocks as _bl
                return self.send_json({"blocks": [{"page": b["page"], "text": b["text"], "kind": b.get("kind", "body"), "y": round(b["bbox"][1] / (b.get("page_h") or 1), 4)} for b in _bl.extract(pdf)]})
            except ImportError as e:
                return self.send_json({"error": "missing package: %s" % e.name}, 501)
        if path == "/api/doc/refs":     # the paper's own reference list, for citation cards
            pdf = ns.pdf_path(one("file"))
            if not pdf:
                return self.send_json({"error": "no such paper"}, 404)
            try:
                return self.send_json({"refs": rf.match_library(rf.parse(pdf), pl.library())})
            except ImportError as e:
                return self.send_json({"error": "missing package: %s" % e.name}, 501)
        if path == "/api/cite/info":    # Semantic Scholar's TL;DR and citation count for a DOI (cached)
            doi = importer.clean_doi(one("doi"))
            return self.send_json(rf.cite_info(doi) if doi else {})
        if path == "/api/folders":
            return self.send_json(fo.list_all())
        if path == "/api/related/paper":    # similar papers you have + related / cited papers you don't (OpenAlex)
            if not related:
                return self.send_json({"error": "AI features aren't installed"}, 501)
            lib = pl.library()
            row = next((r for r in lib if r["file"] == one("file")), None)
            if not row:
                return self.send_json({"error": "no such paper"}, 404)
            return self.send_json(related.paper(row, lib))
        if path == "/api/related/ignored":
            return self.send_json({"ignored": related.ignored() if related else {}})
        if path == "/api/ask/scope":    # how many papers a question over this scope reads, and what it costs
            if not multiask:
                return self.send_json({"error": "AI features aren't installed"}, 501)
            rows = self.scope_rows({k: one(k) for k in ("folder", "journal", "year", "tag", "status")} | {"star": one("star") == "1"})
            if rows is None:
                return self.send_json({"error": "no such folder"}, 404)
            return self.send_json({**multiask.scope_info(rows), "estimate": {"passages": multiask.estimate(rows), "cards": multiask.estimate(rows, "cards")}})
        if path == "/api/ask/chat":     # the saved conversation of a folder (or of the whole library when no folder is given)
            if not multiask:
                return self.send_json({"error": "AI features aren't installed"}, 501)
            if one("folder") and not fo.get(one("folder")):
                return self.send_json({"error": "no such folder"}, 404)
            return self.send_json(multiask.load_chat(one("folder") or None))
        if path == "/api/folder/matrix":
            if not matrix:
                return self.send_json({"error": "AI features aren't installed"}, 501)
            f, rows = self.folder_rows(one("id"))
            if not f:
                return self.send_json({"error": "no such folder"}, 404)
            return self.send_json({**matrix.table(f["id"], rows), "fill": matrix.estimate_fill(f["id"], rows), "folder": {"id": f["id"], "name": f["name"]}})
        if path == "/api/doc/skim":     # the paper's skim layer (or what making one would cost)
            pdf = ns.pdf_path(one("file"))
            if not pdf:
                return self.send_json({"error": "no such paper"}, 404)
            if not skim:
                return self.send_json({"error": "AI features aren't installed"}, 501)
            d = skim.load(pdf)
            return self.send_json({"skim": d, "lenses": skim.LENSES, **({} if d else {"estimate": skim.estimate(pdf)})})
        if path == "/api/doc/card":     # the paper's card (or what making one would cost)
            pdf = ns.pdf_path(one("file"))
            if not pdf:
                return self.send_json({"error": "no such paper"}, 404)
            if not papercard:
                return self.send_json({"error": "AI features aren't installed"}, 501)
            card = papercard.load(pdf)
            return self.send_json({"card": card} if card else {"card": None, "estimate": papercard.estimate(pdf)})
        if path in ("/api/folder/evidence", "/api/folder/ideas"):
            if not ideas:
                return self.send_json({"error": "AI features aren't installed"}, 501)
            f, rows = self.folder_rows(one("id"))
            if not f:
                return self.send_json({"error": "no such folder"}, 404)
            have = [r for r in rows if r.get("card")]
            cards = {"have": len(have), "missing": len(rows) - len(have), "total": len(rows)}
            if path == "/api/folder/ideas":
                return self.send_json({"doc": ideas.load(f["id"]), "cards": cards, "folder": {"id": f["id"], "name": f["name"]}})
            miss = [r for r in rows if not r.get("card")]
            cost = 0.0
            for r in miss[:60]:
                pdf = ns.pdf_path(r["file"]); e = papercard.estimate(pdf, quick=True) if pdf else None
                cost = None if cost is None or not e or e["cost"] is None else cost + e["cost"]
            return self.send_json({"evidence": evidence.build(rows), "cards": cards, "estimate": {"cards": cost}})
        if path == "/api/library/search":     # books=only: just the books (the Books tab); books=all: both; otherwise papers only (a book has its own tab)
            bk_ = one("books")
            rows_ = [r for r in pl.library() if bk_ == "all" or (r.get("journal") == "Books") == (bk_ == "only")]
            return self.send_json(ls.search(rows_, one("q"), one("status"), one("star") == "1", one("folder"), one("journal"),
                                            one("year"), one("tag"), one("notes") == "1", one("sort"), one("kind")))
        if path == "/api/library/highlights":
            return self.send_json(ls.highlights(pl.library(), one("q"), one("tag") or None, one("color") or None, one("folder") or None, one("journal") or None))
        if path == "/api/tags/usage":
            return self.send_json(ls.tag_usage(pl.library()))
        if path.startswith("/api/export/"):
            try:
                return self.export(path.rsplit("/", 1)[-1], q)
            except ImportError as e:
                return self.send_json({"error": "missing package: %s — run pip install -r requirements.txt" % e.name}, 501)
        if path == "/api/settings":     # everything the Settings page shows
            return self.send_json({"prefs": prefs.get_settings(), **prefs.get_prompts(), "ai": ai.config() if ai else None,
                                   "usage": ai.usage_summary() if ai else None, "cache": ai.cache_stats() if ai else None})
        if path == "/api/folder/draft":  # ?id=: the saved related-work draft (rendered), the folder's papers (with or without a card) and an estimate for writing one
            f, rows = self.folder_rows(one("id"))
            if not f or not relatedwork:
                return self.send_json({"error": "no such folder"}, 404)
            doc = relatedwork.load(f["id"])
            if doc:
                doc["render"] = relatedwork.render(doc)
            ps = [{"file": r["file"], "title": r.get("title", ""), "cite": relatedwork.evd.short_cite(r), "card": bool(papercard.exists(ns.pdf_path(r["file"]))) if ns.pdf_path(r["file"]) else False} for r in rows]
            st = (doc or {}).get("settings") or {}
            return self.send_json({"doc": doc, "papers": ps, "estimate": relatedwork.estimate(rows, {"files": st.get("files") or [p["file"] for p in ps if p["card"]], "words": st.get("words", 600)})})
        if path == "/api/folder/workspace" and workspace:       # ?id=: the folder's board
            f, rows = self.folder_rows(one("id"))
            if not f:
                return self.send_json({"error": "no such folder"}, 404)
            return self.send_json({"doc": workspace.load(f["id"])})
        if path == "/api/folder/workspace/sources" and workspace:   # ?id=: what can be pulled onto the board: highlights, card lines, takeaways, per paper
            f, rows = self.folder_rows(one("id"))
            if not f:
                return self.send_json({"error": "no such folder"}, 404)
            return self.send_json({"papers": workspace.sources(rows)})
        if path == "/api/queue" and personal:     # ?n=: what to read next (your "to read" list first), each with why, how close it is to what you read, and the folder it belongs in
            return self.send_json(personal.queue(pl.library(), max(1, min(30, int(one("n", "8") or 8)))))
        if path == "/api/smart":        # your saved searches
            return self.send_json({"searches": smart.load()})
        if path == "/api/flashcards" and flashcards:   # ?folder=&all=1: today's queue (with what each answer would mean), the counts, and with all=1 every card
            files = None
            if one("folder"):
                f = fo.get(one("folder"))
                if not f:
                    return self.send_json({"error": "no such folder"}, 404)
                keys = set(f["papers"])
                files = {r["file"] for r in pl.library() if r["key"] in keys}
            doc = flashcards.load()
            qu = [{**c, "preview": flashcards.preview(c)} for c in flashcards.queue(doc, files)]
            return self.send_json({"counts": flashcards.counts(doc, files), "queue": qu, "cards": [c for c in doc["cards"] if not files or c["file"] in files] if one("all") == "1" else [], "settings": doc["settings"], "all_counts": flashcards.counts(doc)})
        if path == "/api/folder/synthesis":  # ?id=: the saved synthesis, the folder's papers (with or without a card) and an estimate
            f, rows = self.folder_rows(one("id"))
            if not f or not synthesis:
                return self.send_json({"error": "no such folder"}, 404)
            doc = synthesis.load(f["id"])
            ps = [{"file": r["file"], "title": r.get("title", ""), "cite": relatedwork.evd.short_cite(r), "card": bool(papercard.exists(ns.pdf_path(r["file"]))) if ns.pdf_path(r["file"]) else False} for r in rows]
            st = (doc or {}).get("settings") or {}
            return self.send_json({"doc": doc, "papers": ps, "estimate": synthesis.estimate(rows, {"files": st.get("files") or [p["file"] for p in ps if p["card"]]})})
        if path == "/api/folder/claims":  # ?id=: the saved claim check, and what you could check (the related-work draft's text if there is one)
            f, rows = self.folder_rows(one("id"))
            if not f or not claims:
                return self.send_json({"error": "no such folder"}, 404)
            dr = relatedwork.load(f["id"]) if relatedwork else None
            return self.send_json({"doc": claims.load(f["id"]), "papers": len(rows), "library": len(pl.library()), "draft": relatedwork.render(dr)["text"] if dr else "", "review": reviewer.load(f["id"]) if reviewer else None})
        if path == "/api/book":         # ?file=: a book's details, its chapters as page ranges, and the printed-page offset
            info = books.info_of(one("file"))
            if not info:
                return self.send_json({"error": "that is not a book in your library"}, 404)
            return self.send_json({"file": one("file"), "source": info.get("source"), "pages": info.get("pages"), "isbn": info.get("isbn"), "publisher": info.get("publisher"), "offset": info.get("offset") or 0,
                                   "chapters": [dict(c, summary=s_ if s_ and s_.get("text") else None, empty=bool(s_ and s_.get("empty")))
                                                for c in books.chapters(info) for s_ in [(info.get("summaries") or {}).get(books.chapter_key(c))]],
                                   **books.overview_view(info)})
        if path == "/api/authors":      # the Authors tab's "Your authors": each author with a folder, whether you follow them, when the weekly check last listed them
            return self.send_json({"authors": authors.saved()})
        if path == "/api/book/whole":   # ?file=: what summarising the whole book would cost (no AI call) and the job's state; &status=1: only the job's state
            if not bookjob:
                return self.send_json({"error": "AI features aren't installed"}, 501)
            if one("status"):
                return self.send_json({"status": bookjob.status()})
            try:
                return self.send_json({**books.whole_estimate(one("file")), "status": bookjob.status()})
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)
        if path == "/api/uiprefs":      # the page's own choices (theme, view modes, where you stopped reading…) kept in the data folder; static/prefsync.js asks for them before anything else
            return self.send_json(uiprefs.get())
        if path == "/api/sync":         # Settings → Computers & sync: which computers use this data folder, which are open now, what Dropbox had to duplicate
            return self.send_json(syncinfo.info())
        if path == "/api/sync/status":  # the small version for the banner: names of the other computers that have the folder open, and how many conflicted copies there are
            return self.send_json({"open": [c["name"] for c in syncinfo.others()], "conflicts": len(syncinfo.conflicts())})
        if path == "/api/aifiles":      # Settings → AI files: what the AI wrote or cached, how big it is, what was cleared lately, how big a backup is
            return self.send_json({"kinds": aifiles.scan(), "trash": aifiles.trash_list(), "backup": aifiles.backup_size(), "export_dir": prefs.get_settings().get("export_dir") or "",
                                   "mcp": {"python": paths.cmd("mcp_server.py")[0], "args": paths.cmd("mcp_server.py")[1:], "env": {"JW_HOME": ROOT} if paths.FROZEN else {}}})
        if path == "/api/library/deleted":   # what you deleted in the last 30 days and can put back
            return self.send_json({"kept": deletion.recently_deleted(), "days": deletion.KEEP_DAYS})
        if path == "/api/snapshots":    # the daily snapshots of your own files on this computer
            return self.send_json({"snapshots": aifiles.snapshots(), "keep": aifiles.KEEP_SNAPSHOTS})
        if path == "/api/backup":       # ?pdfs=1: a zip of your own data (notes, cards, folders, tags, settings…), optionally with the PDFs
            name = "journal-watcher-backup-%s.zip" % datetime.now().strftime("%Y%m%d-%H%M")
            return self.send_temp_file(aifiles.backup(one("pdfs", "") == "1"), "application/zip", name)
        if path == "/api/activity":     # what the AI is doing now and just did, plus the background indexer's state; ?pending=1 also counts unindexed papers
            out = {"jobs": activity.snapshot(), "indexing": indexer.status() if indexer else {"enabled": False, "local": False, "running": False}}
            if one("pending", "") and indexer:
                out["indexing"]["pending"] = len(indexer.pending())
            return self.send_json(out)
        if path in ("/api/ai/config", "/api/ai/usage", "/api/ai/log", "/api/ai/budget"):
            if not ai:
                return self.send_json({"error": "AI features aren't installed"}, 501)
            if path == "/api/ai/budget":        # ?cost=: a large job's estimate against what is left of the monthly budget, asked by the page before the job starts (no cost: the estimate is not known)
                try:
                    cost = float(one("cost")) if one("cost") else None
                except ValueError:
                    return self.send_json({"error": "cost must be a number"}, 400)
                return self.send_json(ai.budget_check(cost))
            return self.send_json({"/api/ai/config": ai.config, "/api/ai/usage": ai.usage_summary,
                                   "/api/ai/log": lambda: ai.usage_log(int(one("limit", "40")))}[path]())
        self.send_json({"error": "not found"}, 404)

    def events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = HUB.subscribe()
        try:
            self.wfile.write(b"event: hello\ndata: {}\n\n")
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=15)
                except queue.Empty:
                    msg = ": ping\n\n"
                self.wfile.write(msg.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            HUB.unsubscribe(q)

    # -- POST
    def do_POST(self):
        if not self.local_only():
            return self.send_json({"error": "forbidden"}, 403)
        return self.guarded(self.post)

    def post(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/refs/extract":  # a document with references (Word, PowerPoint, PDF, text; the raw file is the body, X-Filename its name): its references, each
            # looked up on Crossref, streamed with progress. X-Mode "ai": the AI lists the references first (the page showed its cost); otherwise no AI call.
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 < n <= refextract.MAX_BYTES:
                return self.send_json({"error": "The file is empty or larger than 150 MB."}, 400)
            data = self.rfile.read(n)
            name = urllib.parse.unquote(self.headers.get("X-Filename") or "pasted text")[:200]
            use_ai = self.headers.get("X-Mode") == "ai"

            def gen():
                for m in (refextract.ai_stream if use_ai else refextract.extract_stream)(data, name):
                    if m.get("type") == "done":                    # (each reference marked with the paper in your library, if you have it)
                        m = dict(m, entries=bibimport.match(m["entries"], pl.library()))
                    yield m
            return self.stream_ndjson(gen(), job=self.job_of("refs", "References · " + name[:60]) if use_ai else None)
        if path == "/api/run/attach":   # a PDF you downloaded yourself, for one paper of a run: the raw PDF is the body; X-Run, X-Paper (URL-encoded)
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 < n <= 150 * 1024 * 1024:
                return self.send_json({"error": "The file is empty or larger than 150 MB, so it cannot be added."}, 400)
            data = self.rfile.read(n)
            pid = urllib.parse.unquote(self.headers.get("X-Paper") or "")
            try:
                res = attach_to_run(urllib.parse.unquote(self.headers.get("X-Run") or ""), lambda run: pl.attach_pdf(run, pid, data))
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)
            if res is None:
                return self.send_json({"error": "That run is not there any more: reload the page."}, 404)
            return self.attached(res)
        if path == "/api/import/pdf":   # the raw PDF is the request body (not JSON); the name travels in a header
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 < n <= 150 * 1024 * 1024:
                return self.send_json({"error": "the file is empty or larger than 150 MB"}, 400)
            data = self.rfile.read(n)
            try:
                res = importer.add_pdf(data, urllib.parse.unquote(self.headers.get("X-Filename") or "paper.pdf"), self.headers.get("X-Doi") or "")
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)
            return self.finish_import(res, self.headers.get("X-Summarize") == "1")
        if path == "/api/import/analyse":   # a dropped PDF to look at first: nothing is filed until /api/import/confirm
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 < n <= 150 * 1024 * 1024:
                return self.send_json({"error": "the file is empty or larger than 150 MB"}, 400)
            data = self.rfile.read(n)
            name = urllib.parse.unquote(self.headers.get("X-Filename") or "paper.pdf")
            try:
                if books.is_epub(data) or self.headers.get("X-Kind") == "book":       # an EPUB is always a book; a PDF is one when the dialog says so (X-Kind: book)
                    return self.send_json({"ok": True, **books.analyse(data, name, hint_pdf=True)})
                return self.send_json({"ok": True, **importer.analyse_pdf(data, name)})
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)
        try:
            b = self.body()
        except ValueError:
            return self.send_json({"error": "bad json"}, 400)
        if path == "/api/import/confirm":   # {token, fields: {title, authors, journal, pub_date, doi, source}, summarize}
            try:
                res = importer.confirm(b.get("token"), b.get("fields"))
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)
            return self.finish_import(res, bool(b.get("summarize")) and (b.get("fields") or {}).get("kind") != "book")      # (a book is never summarised by itself: it is long, and that costs)
        if path == "/api/book/summary/estimate":      # {file, chapter}: what a summary of that chapter costs, before it is asked for
            try:
                return self.send_json({"ok": True, **books.summary_estimate(str(b.get("file") or ""), b.get("chapter"))})
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)
            except ImportError:
                return self.send_json({"error": "The AI backend isn’t installed on this server yet."}, 501)
        if path == "/api/ai/chapter":       # {file, chapter, fresh?, override?}: write (or return the saved) summary of one chapter; streams status | text | final | done | error
            row_ = next((r for r in pl.library() if r["file"] == b.get("file")), {})
            ov = b.get("override") if isinstance(b.get("override"), dict) else None
            return self.stream_ndjson(books.summary_stream(str(b.get("file") or ""), b.get("chapter"), row_.get("title") or "", bool(b.get("fresh")), ov),
                                      job=self.job_of("summary", "Chapter summary · " + (row_.get("title") or "a book")[:50], file=b.get("file"), tab="sum"))
        if path == "/api/book":             # {file, offset}: where printed page 1 is in a PDF book (the file page minus the offset is the printed page)
            try:
                info = books.set_offset(str(b.get("file") or ""), b.get("offset"))
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)
            return self.send_json({"ok": True, "offset": info.get("offset") or 0})
        if path == "/api/uiprefs":          # {set: {key: value}, remove: [key]}: only the keys uiprefs shares are kept; the rest are ignored
            return self.send_json({"ok": True, "changed": uiprefs.update(b.get("set"), b.get("remove"))})
        if path == "/api/sync":             # {action: "rename", name} | {action: "merge", path} (join the library index copies) | {action: "set_aside", path}
            try:
                act = b.get("action")
                if act == "rename":
                    syncinfo.rename(b.get("name"))
                    return self.send_json({"ok": True, **syncinfo.info()})
                if act == "merge":
                    res = syncinfo.merge(b.get("path"))
                elif act == "set_aside":
                    res = {"set_aside": syncinfo.set_aside(b.get("path"))}
                else:
                    return self.send_json({"error": "unknown action"}, 400)
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)
            return self.send_json({"ok": True, **res, **syncinfo.info()})
        if path == "/api/import/check":     # {doi, title}: is this paper already in the library? (the review dialog asks again when you pick another record or edit the boxes)
            if b.get("kind") == "book":
                return self.send_json({"ok": True, "existing": books._existing_book(books.isbn13(str(b.get("isbn") or "")), str(b.get("title") or ""), importer.clean_doi(b.get("doi")) if str(b.get("doi") or "").strip() else "")})
            return self.send_json({"ok": True, "existing": importer.check_existing(b.get("doi"), str(b.get("title") or ""))})
        if path == "/api/import/discard":   # {tokens: [...]}: Cancel, or papers you unticked
            return self.send_json({"ok": True, "removed": importer.discard(b.get("tokens") if isinstance(b.get("tokens"), list) else [])})
        if path == "/api/import/doi":
            try:
                res = importer.add_doi(b.get("doi"))
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)
            return self.finish_import(res, bool(b.get("summarize")))
        if path == "/api/settings":     # {prefs?: {legend, summary_template, export_dir, wpm}, prompts?: [...], summaries?: [...]}
            try:
                if "prefs" in b:
                    prefs.save_settings(b["prefs"])
                if "prompts" in b or "summaries" in b:
                    prefs.save_prompts({k: b[k] for k in ("prompts", "summaries") if k in b})
            except (ValueError, TypeError, KeyError) as e:
                return self.send_json({"error": str(e)[:160]}, 400)
            return self.send_json({"ok": True, "prefs": prefs.get_settings(), **prefs.get_prompts()})
        if path == "/api/tags/retag":   # {from, into?}: merge a tag into another (or delete it) across every paper
            if not b.get("from"):
                return self.send_json({"error": "which tag?"}, 400)
            try:
                return self.send_json({"ok": True, "changed": ns.retag(b["from"], b.get("into") or None)})
            except ns.Unreadable as e:
                return self.send_json({"error": str(e)}, 423)
        if path == "/api/folders":      # {action: create|rename|delete|add|remove, id, name, files:[Papers/…pdf]}
            act, fid = b.get("action"), b.get("id")
            keys = [r["key"] for r in pl.library() if r["file"] in set(b.get("files") or [])]
            try:
                if act == "create":
                    f = fo.create(b.get("name"))
                    if keys:
                        fo.add(f["id"], keys)
                elif act == "rename":
                    fo.rename(fid, b.get("name"))
                elif act == "delete":
                    fo.delete(fid)
                elif act in ("add", "remove"):
                    (fo.add if act == "add" else fo.remove)(fid, keys)
                else:
                    return self.send_json({"error": "unknown action"}, 400)
            except (ValueError, KeyError) as e:
                return self.send_json({"error": str(e.args[0] if e.args else e)}, 400)
            return self.send_json({"ok": True, "folders": fo.list_all()})
        if path == "/api/paper/state":  # {file, status?, star?, takeaway?, page?, total?, opened?}
            row = next((r for r in pl.library() if r["file"] == b.get("file")), None)
            if not row:
                return self.send_json({"error": "no such paper"}, 404)
            try:
                if b.get("opened"):
                    st = rs.touch_open(row["key"], b.get("page"), b.get("total"))
                else:
                    patch = {k: b[k] for k in ("status", "star", "skip", "takeaway", "page", "progress") if k in b}
                    if b.get("page") and b.get("total"):
                        patch["progress"] = b["page"] / b["total"]
                    st = rs.update(row["key"], patch)
            except (ValueError, TypeError) as e:
                return self.send_json({"error": str(e)[:100]}, 400)
            return self.send_json({"ok": True, "state": st})
        if path == "/api/doc/skim":   # {file, dismissed: [ids]}: the proposals you said no to
            pdf = ns.pdf_path(b.get("file"))
            if not pdf or not skim:
                return self.send_json({"error": "no such paper"}, 404)
            try:
                return self.send_json({"ok": True, "dismissed": skim.save_dismissed(pdf, [str(i) for i in (b.get("dismissed") or [])][:500])})
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)
        if path == "/api/doc/card":   # {file, card, rev}: your edits to a paper card
            pdf = ns.pdf_path(b.get("file"))
            if not pdf or not papercard:
                return self.send_json({"error": "no such paper"}, 404)
            try:
                return self.send_json({"ok": True, "card": papercard.save_edit(pdf, b.get("card"), int(b.get("rev") or 0))})
            except ns.Conflict as c:
                return self.send_json({"error": "This card changed in another window.", "current": c.current}, 409)
            except (ValueError, TypeError) as e:
                return self.send_json({"error": str(e)[:120]}, 400)
        if path == "/api/folder/ideas":   # {id, doc, rev}: the board's own changes (brief, events ticked, stars, status, notes, deletes)
            try:
                return self.send_json({"ok": True, "doc": ideas.save(str(b.get("id") or ""), b.get("doc"), int(b.get("rev") or 0))})
            except ns.Conflict as c:
                return self.send_json({"error": "The board changed in another window.", "current": c.current}, 409)
            except KeyError:
                return self.send_json({"error": "no such folder"}, 404)
            except (ValueError, TypeError, AttributeError) as e:
                return self.send_json({"error": str(e)[:120] or "bad board"}, 400)
        if path == "/api/related/ignore":   # {items: [{id, title}]} hides them; {undo: [ids]} brings them back
            if not related:
                return self.send_json({"error": "AI features aren't installed"}, 501)
            if b.get("undo"):
                related.unignore([str(i) for i in b["undo"]])
            if b.get("items"):
                related.ignore([x for x in b["items"] if isinstance(x, dict)])
            return self.send_json({"ok": True, "ignored": related.ignored()})
        if path == "/api/related/scope":    # {folder?, journal?, year?, tag?, status?, min}: papers cited by several of these papers, not in your library
            if not related:
                return self.send_json({"error": "AI features aren't installed"}, 501)
            lib = pl.library()
            rows = self.scope_rows(b.get("scope") if isinstance(b.get("scope"), dict) else {})
            if rows is None:
                return self.send_json({"error": "no such folder"}, 404)
            return self.stream_ndjson(related.scope_stream(rows, lib, max(1, min(10, int(b.get("min") or 2)))))
        if path == "/api/authors/search":    # {name}: OpenAlex authors of that name (or ORCID iD, or OpenAlex id), for you to pick the right person
            try:
                return self.send_json({"authors": authors.search(b.get("name"))})
            except authors.AuthorError as e:
                return self.send_json({"error": str(e)}, 502)
        if path == "/api/authors/download":  # {ids: [A…], name, institution}: every work of those ids (journal, conference, working papers) into the author's folder
            if JOBS.busy():
                return self.send_json({"error": "Another task is running — wait or press Stop."}, 409)
            return self.author_download(b, follow=False)
        if path == "/api/authors/follow":    # {folder, on: false} stops following; {ids, name, institution, on: true} (or {folder, on: true}): download all now, then new works each week
            on = bool(b.get("on"))
            fid0 = str(b.get("folder") or "")
            if not on:
                if not authors.set_follow(fid0, False):
                    return self.send_json({"error": "No author has that folder."}, 404)
                return self.send_json({"ok": True, "authors": authors.saved()})
            if JOBS.busy():
                return self.send_json({"error": "Another task is running — wait or press Stop."}, 409)
            if fid0 and not b.get("ids"):
                rec = next((r for r in authors.load()["authors"] if r.get("folder") == fid0), None)
                if rec is None:
                    return self.send_json({"error": "No author has that folder."}, 404)
                if fo.get(fid0) and "follow_known" in rec:      # followed before, folder still there: just on again (the weekly check picks up what is new since)
                    authors.set_follow(fid0, True)
                    return self.send_json({"ok": True, "authors": authors.saved()})
                b = {"ids": rec.get("ids") or [], "name": rec.get("name") or "", "institution": ""}
            return self.author_download(b, follow=True)
        if path == "/api/related/download":   # {items: [{id, doi, title, authors, year, venue, date, reason}], label}: straight into the download pipeline
            if not related:
                return self.send_json({"error": "AI features aren't installed"}, 501)
            if JOBS.busy():
                return self.send_json({"error": "Another task is running — wait or press Stop."}, 409)
            items = [x for x in (b.get("items") or []) if isinstance(x, dict)][:40]
            fid = str(b.get("folder") or "")                      # (Discover inside a folder: what you pick goes into that folder)
            folder = fo.get(fid) if fid else None
            if fid and not folder:
                return self.send_json({"error": "That folder is not there any more: reload the page."}, 404)
            papers, have = related.run_papers(items)
            if folder and have:                                    # the ones you had already: into the folder now (nothing to download)
                hf = {h["file"] for h in have}
                fo.add(fid, [r["key"] for r in pl.library() if r["file"] in hf])
            if not papers:
                if folder and have:
                    return self.send_json({"ok": True, "run": None, "have": have, "n": 0, "folder": folder["name"]})
                return self.send_json({"error": "Nothing to download: %s" % ("they are all in your library already." if have else "no usable papers were chosen."), "have": have}, 400)
            params = {"label": re.sub(r"\s+", " ", str((folder or {}).get("name") or b.get("label") or "related papers")).strip()[:60]}
            if folder:
                params.update(folder=fid, folder_name=folder["name"])
            run = pl.Run("picks", params)
            for p in papers:
                if folder:
                    p["folder"] = fid                               # (filed into it once it is in the library: by this download, a retry, or a PDF you add yourself)
                run.add_paper(p)
            run.stage, run.status = "ready", "stopped"
            run.save(force=True)
            JOBS.run = run
            if not start_download([p["id"] for p in papers]):
                return self.send_json({"error": "Another task is running — wait or press Stop."}, 409)
            return self.send_json({"ok": True, "run": run.to_dict(full=False), "have": have, "n": len(papers), "folder": (folder or {}).get("name", "")})
        if path == "/api/ask/chat":      # {folder?, turns}: replaces the saved conversation (an empty list deletes it)
            try:
                return self.send_json(multiask.save_chat(str(b.get("folder") or "") or None, b.get("turns")))
            except KeyError:
                return self.send_json({"error": "no such folder"}, 404)
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)
        if path == "/api/folder/matrix":   # {id, rev, action?: "add" (name, prompt) | default save {columns?, custom?, edits?}}
            fid = str(b.get("id") or "")
            try:
                if b.get("action") == "add":
                    doc = matrix.add_custom(fid, b.get("name"), b.get("prompt"), int(b.get("rev") or 0))
                else:
                    doc = matrix.save(fid, b.get("doc"), int(b.get("rev") or 0))
                return self.send_json({"ok": True, "doc": doc})
            except ns.Conflict as c:
                return self.send_json({"error": "The matrix changed in another window.", "current": c.current}, 409)
            except KeyError:
                return self.send_json({"error": "no such folder"}, 404)
            except (ValueError, TypeError, AttributeError) as e:
                return self.send_json({"error": str(e)[:120] or "bad matrix"}, 400)
        if path == "/api/personal/fit" and personal:     # {items: [{id, title, abstract?, topics?}]}: how close candidates are to what you read, starred or marked up ({} until you have enough to learn from)
            items = [x for x in (b.get("items") or []) if isinstance(x, dict)][:400]
            return self.send_json({"fits": personal.fits(items, pl.library()), "profile": personal.profile_info(pl.library())})
        if path == "/api/library/fulltext" and fulltext:     # {q, scope?: {folder, journal, year, tag, status}}: the words inside the PDFs themselves (stream: status | results | error)
            rows = self.scope_rows(b.get("scope") if isinstance(b.get("scope"), dict) else {})
            if rows is None:
                return self.send_json({"error": "no such folder"}, 404)
            pg = b.get("pages") if isinstance(b.get("pages"), dict) else None            # {file, from, to}: only inside one chapter of one book
            if pg:
                try:
                    pg = {"file": str(pg["file"]), "from": int(pg["from"]), "to": int(pg["to"])}
                except (KeyError, TypeError, ValueError):
                    return self.send_json({"error": "bad chapter range"}, 400)
                if pg["from"] < 1 or pg["to"] < pg["from"]:
                    return self.send_json({"error": "bad chapter range"}, 400)
            return self.stream_ndjson(fulltext.stream(rows, str(b.get("q") or "")[:300], pg))
        if path == "/api/smart":         # {action: add (name, filters) | rename (id, name) | delete (id)}
            try:
                a = b.get("action")
                items = smart.add(b.get("name"), b.get("filters")) if a == "add" else smart.rename(str(b.get("id") or ""), b.get("name")) if a == "rename" else smart.delete(str(b.get("id") or "")) if a == "delete" else None
                if items is None:
                    return self.send_json({"error": "unknown action"}, 400)
                return self.send_json({"ok": True, "searches": items})
            except KeyError as e:
                return self.send_json({"error": str(e.args[0])}, 404)
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)
        if path == "/api/bib/parse":     # {text, name}: the entries of a .ris / .bib file, each marked with the paper in your library if you have it
            entries = bibimport.parse(str(b.get("text") or "")[:8_000_000], str(b.get("name") or ""))
            if not entries:
                return self.send_json({"error": "No references were found. This reads RIS (EndNote, Mendeley, Zotero) and BibTeX (.bib) files."}, 400)
            ents = bibimport.match(entries, pl.library())
            return self.send_json({"entries": ents, "have": sum(1 for e in ents if e["file"]), "no_doi": sum(1 for e in ents if not e["file"] and not e["has_doi"])})
        if path == "/api/export/folderdir":   # {folder}: the folder as a real directory in your export folder: the PDFs, a .bib and your notes as Markdown
            d = prefs.export_dir()
            f, rows = self.folder_rows(str(b.get("folder") or ""))
            if not d or not f:
                return self.send_json({"error": "Choose an export folder in Settings → Reader first." if not d else "no such folder"}, 400)
            safe = re.sub(r"[\\/:*?\"<>|]+", "-", f["name"]).strip(" .") or "folder"
            dest = os.path.join(d, safe)
            n = skipped = 0
            try:
                os.makedirs(dest, exist_ok=True)
                for r in rows:
                    pdf = ns.pdf_path(r["file"])
                    if not pdf:
                        continue
                    target = os.path.join(dest, os.path.basename(pdf))
                    if os.path.exists(target) and os.path.getsize(target) == os.path.getsize(pdf):
                        skipped += 1
                        continue
                    shutil.copy2(pdf, target)
                    n += 1
                if rows:
                    with open(os.path.join(dest, "references.bib"), "w", encoding="utf-8") as fh:
                        fh.write(exports.bibtex(rows))
                    with open(os.path.join(dest, "notes.md"), "w", encoding="utf-8") as fh:
                        fh.write(exports.notes_markdown(rows, f["name"]))
            except OSError as e:
                return self.send_json({"error": "Couldn’t write to %s: %s (%d of the PDFs were copied)" % (dest, e.strerror or e, n)}, 400)
            return self.send_json({"ok": True, "path": dest, "pdfs": n, "unchanged": skipped, "papers": len(rows)})
        if path == "/api/export/save":   # {what: ideas|cards, folder, fmt}: write the file into your export folder (Settings -> Reader)
            d = prefs.export_dir()
            f = fo.get(str(b.get("folder") or ""))
            if not d or not f:
                return self.send_json({"error": "Choose an export folder in Settings → Reader first." if not d else "no such folder"}, 400)
            what, fmt = b.get("what"), b.get("fmt") or "md"
            try:
                if what == "workspace":
                    wd, wrows = workspace.load(f["id"]), self.folder_rows(f["id"])[1]
                    if not (wd["cards"] or wd["frames"]):
                        return self.send_json({"error": "the workspace is empty"}, 400)
                    data, name = (workspace.docx(wd, f["name"], wrows), f["name"] + "-workspace.docx") if fmt == "docx" else (workspace.markdown(wd, f["name"], wrows).encode(), f["name"] + "-workspace.md")
                elif what == "synthesis":
                    sd = synthesis.load(f["id"])
                    if not sd:
                        return self.send_json({"error": "make the synthesis first"}, 400)
                    data, name = (synthesis.docx(sd, f["name"]), f["name"] + "-synthesis.docx") if fmt == "docx" else (synthesis.markdown(sd, f["name"]).encode(), f["name"] + "-synthesis.md")
                elif what == "claims":
                    cd = claims.load(f["id"])
                    if not cd:
                        return self.send_json({"error": "check some claims first"}, 400)
                    data, name = (claims.docx(cd, f["name"]), f["name"] + "-claim-check.docx") if fmt == "docx" else (claims.markdown(cd, f["name"]).encode(), f["name"] + "-claim-check.md")
                elif what == "draft":
                    dr = relatedwork.load(f["id"])
                    if not dr:
                        return self.send_json({"error": "write the related-work draft first"}, 400)
                    data, name = (relatedwork.docx(dr, f["name"]), f["name"] + "-related-work.docx") if fmt == "docx" else (relatedwork.markdown(dr, f["name"]).encode(), f["name"] + "-related-work.md")
                elif what == "ideas":
                    data, name = (ideas.docx(f["id"]), f["name"] + "-ideas.docx") if fmt == "docx" else (ideas.markdown(f["id"]).encode(), f["name"] + "-ideas.md")
                elif what == "matrix" and fmt in ("csv", "md", "tex", "docx"):
                    rows = self.folder_rows(f["id"])[1]
                    data = {"csv": lambda: matrix.csv_bytes(f["id"], rows), "md": lambda: matrix.markdown(f["id"], rows).encode(), "tex": lambda: matrix.latex(f["id"], rows).encode(),
                            "docx": lambda: matrix.docx_bytes(f["id"], rows)}[fmt]()
                    name = "%s-comparison.%s" % (f["name"], fmt)
                else:
                    return self.send_json({"error": "unknown export"}, 400)
                os.makedirs(d, exist_ok=True)
                out = os.path.join(d, re.sub(r"[^\w .()-]+", "_", name))
                with open(out, "wb") as fh:
                    fh.write(data)
            except OSError as e:
                return self.send_json({"error": "Couldn’t write there: %s" % e.strerror}, 400)
            return self.send_json({"ok": True, "path": out})
        if path == "/api/doc/chat":   # {file, turns:[...]}: replaces the saved conversation (empty list deletes it)
            pdf = ns.pdf_path(b.get("file"))
            if not pdf:
                return self.send_json({"error": "no such paper"}, 404)
            try:
                return self.send_json(ns.save_chat(pdf, b.get("turns")))
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)
        if path == "/api/doc/summary":   # {file, text}: replace the paper's saved summary (the old one is kept in state/summary_backups/)
            pdf = ns.pdf_path(b.get("file"))
            text = str(b.get("text") or "").strip()
            if not pdf or len(text) < 20:
                return self.send_json({"error": "nothing to save"}, 400)
            if books.is_book(b.get("file")):
                return self.send_json({"error": "A book keeps its summaries with its chapters (Library → Books → Chapters); nothing was saved."}, 400)
            md = pdf[:-4] + ".md"
            if os.path.exists(md):
                bk = os.path.join(pl.STATE, "summary_backups")
                os.makedirs(bk, exist_ok=True)
                shutil.copy(md, os.path.join(bk, "%s-%s.md" % (ns.sha1_of(pdf)[:12], datetime.now().strftime("%Y%m%d-%H%M%S"))))
            with open(md, "w", encoding="utf-8") as f:
                f.write(text + "\n")
            return self.send_json({"ok": True, "path": os.path.relpath(md, ROOT)})
        if path == "/api/doc/notes":  # autosave from the reader; rev guards against a stale tab
            pdf = ns.pdf_path(b.get("file"))
            if not pdf:
                return self.send_json({"error": "no such paper"}, 404)
            try:
                out = ns.save(pdf, b.get("notes"), int(b.get("rev") or 0), self.doc_info(b["file"])[1])
            except ns.Conflict as c:
                return self.send_json({"error": "These notes changed in another window.", "current": c.current}, 409)
            except ns.Unreadable as e:
                return self.send_json({"error": str(e)}, 423)
            except (ValueError, TypeError) as e:
                return self.send_json({"error": str(e)[:120]}, 400)
            return self.send_json({"ok": True, "rev": out["rev"]})
        if path == "/api/folder/draft" and relatedwork:     # {id, raw?, style?, pages?, rev?}: your edits, or only how the citations are written; no model call
            try:
                return self.send_json({"ok": True, "doc": relatedwork.update(str(b.get("id") or ""), b.get("raw"), b.get("style"), b.get("pages"), b.get("rev"))})
            except KeyError as e:
                return self.send_json({"error": str(e.args[0])}, 404)
            except ValueError as e:
                return self.send_json({"error": str(e)}, 409)
        if path == "/api/folder/workspace" and workspace:        # {id, doc, rev}: save the board (409 with the newer copy when another window saved first)
            try:
                return self.send_json({"ok": True, "doc": workspace.save(str(b.get("id") or ""), b.get("doc"), b.get("rev"))})
            except workspace.Conflict as c:
                return self.send_json({"error": "This workspace changed in another window.", "current": c.current}, 409)
            except KeyError as e:
                return self.send_json({"error": str(e.args[0])}, 404)
            except (ValueError, TypeError) as e:
                return self.send_json({"error": str(e)[:120]}, 400)
        if path.startswith("/api/flashcards/") and flashcards:     # rate {id, rating} · add {q, a, file?, page?, quote?} · update {id, q?, a?, suspended?} · delete {ids} · settings {new_per_day}
            act_ = path.rsplit("/", 1)[-1]
            try:
                if act_ == "rate":
                    c, cn = flashcards.rate(str(b.get("id") or ""), int(b.get("rating") or 0))
                    return self.send_json({"ok": True, "card": c, "counts": cn})
                if act_ == "add":
                    return self.send_json({"ok": True, "card": flashcards.add(b.get("q"), b.get("a"), b.get("file") or "", b.get("page"), b.get("quote") or "")})
                if act_ == "update":
                    return self.send_json({"ok": True, "card": flashcards.update(str(b.get("id") or ""), b.get("q"), b.get("a"), b.get("suspended"))})
                if act_ == "delete":
                    return self.send_json({"ok": True, "removed": flashcards.delete([str(i) for i in b.get("ids") or []])})
                if act_ == "settings":
                    return self.send_json({"ok": True, "settings": flashcards.set_new_per_day(b.get("new_per_day"))})
            except KeyError as e:
                return self.send_json({"error": str(e.args[0])}, 404)
            except (ValueError, TypeError) as e:
                return self.send_json({"error": str(e)[:160]}, 400)
        if path == "/api/folder/claims" and claims:       # {id, n, handled, rev?}: tick a claim off; no model call
            try:
                return self.send_json({"ok": True, "doc": claims.mark(str(b.get("id") or ""), int(b.get("n") or 0), bool(b.get("handled")), b.get("rev"))})
            except KeyError as e:
                return self.send_json({"error": str(e.args[0])}, 404)
            except ValueError as e:
                return self.send_json({"error": str(e)}, 409)
        if path == "/api/aifiles/clear":         # {kind}: delete one kind of AI file (a zip of it goes to state/trash first unless it is freely regenerable)
            try:
                r = aifiles.clear(str(b.get("kind") or ""))
            except KeyError as e:
                return self.send_json({"error": str(e.args[0] if e.args else e)}, 400)
            return self.send_json({"ok": True, **r, "kinds": aifiles.scan(), "trash": aifiles.trash_list()})
        if path == "/api/aifiles/restore":       # {name}: put a cleared kind back (never over a file that exists again)
            try:
                r = aifiles.restore(str(b.get("name") or ""))
            except (KeyError, OSError) as e:
                return self.send_json({"error": str(e.args[0] if e.args else e)[:120]}, 400)
            return self.send_json({"ok": True, **r, "kinds": aifiles.scan(), "trash": aifiles.trash_list()})
        if path == "/api/snapshots/restore":     # {name}: your notes, folders, index and settings as they were then (what is there now is kept in a snapshot first)
            if JOBS.busy():
                return self.send_json({"error": "Another task is running — wait or press Stop."}, 409)
            try:
                return self.send_json({"ok": True, **aifiles.restore_snapshot(b.get("name")), "snapshots": aifiles.snapshots()})
            except KeyError:
                return self.send_json({"error": "That snapshot is not there any more."}, 404)
            except ValueError as e:                  # (a damaged snapshot: found before anything was changed)
                return self.send_json({"error": str(e)[:200]}, 400)
            except OSError as e:
                return self.send_json({"error": "The snapshot could not be restored in full (%s). What you had is in the newest snapshot marked “before a restore”." % (str(e)[:160] or e.__class__.__name__)}, 400)
        if path == "/api/snapshots/now":         # take one now
            return self.send_json({"ok": True, "name": aifiles.snapshot(force=True), "snapshots": aifiles.snapshots()})
        if path == "/api/backup/save":           # save the zip into "my export folder" (Settings → Reader) instead of downloading it
            d = prefs.export_dir()
            if not d:
                return self.send_json({"error": "Choose an export folder first (Settings → Reader & summaries)."}, 400)
            tmp = aifiles.backup(bool(b.get("pdfs")))
            dest = os.path.join(d, "journal-watcher-backup-%s.zip" % datetime.now().strftime("%Y%m%d-%H%M"))
            try:
                os.makedirs(d, exist_ok=True)            # (as the other exports do: a folder that is set but not made yet is made)
                shutil.move(tmp, dest)
            except OSError as e:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                return self.send_json({"error": "Couldn’t write to %s: %s" % (d, e.strerror or e)}, 400)
            return self.send_json({"ok": True, "path": dest, "bytes": os.path.getsize(dest)})
        if path == "/api/activity/cancel":       # {id}
            return self.send_json({"ok": activity.cancel(str(b.get("id") or ""))})
        if path == "/api/activity/indexing":     # {enabled?: bool, now?: true}: the background indexing switch / "Index now"
            if not indexer:
                return self.send_json({"error": "AI features aren't installed"}, 501)
            if "enabled" in b:
                prefs.save_settings({"background_index": bool(b["enabled"])})
            started = indexer.kick(0, manual=True) if b.get("now") else (indexer.kick(0) if b.get("enabled") else False)
            return self.send_json({"ok": True, "started": started, "indexing": indexer.status()})
        if path.startswith("/api/ai/"):
            if not ai:
                return self.send_json({"error": "AI features aren't installed"}, 501)
            if path == "/api/ai/config":
                try:
                    return self.send_json(ai.save_config(b))
                except (ValueError, KeyError, TypeError, AttributeError) as e:
                    return self.send_json({"error": str(e)[:120] or "bad settings"}, 400)
            if path == "/api/ai/key":       # {provider, key}: saved in the data home's .env (mode 600), never echoed back; {provider, remove: true}: the saved key is removed
                try:
                    if b.get("remove"):
                        return self.send_json(ai.remove_key(str(b.get("provider") or "")))
                    return self.send_json(ai.set_key(str(b.get("provider") or ""), b.get("key")))
                except ValueError as e:
                    return self.send_json({"error": str(e)[:160]}, 400)
            if path == "/api/ai/keys/move":     # the API keys in the .env file go to this Mac's Keychain (each is read back before its line is removed)
                try:
                    res = ai.move_keys_to_keychain()
                except ValueError as e:
                    return self.send_json({"error": str(e)[:160]}, 400)
                with ai._lock:
                    ai._clients.clear()
                return self.send_json({"ok": True, **res, "config": ai.config()})
            if path == "/api/ai/test":      # one tiny real call: does this provider + key + model work? {feature: "screen"}: can it also give the structured verdicts screening needs?
                if b.get("feature") == "screen":
                    return self.send_json(pl.screen_test(str(b.get("provider") or ""), str(b.get("model") or "")))
                return self.send_json(ai.test(str(b.get("provider") or ""), str(b.get("model") or "")))
            if path == "/api/ai/cache/clear":
                return self.send_json({"ok": True, "removed": ai.cache_clear()})
            if path == "/api/ai/explain":   # {file, text?, page, prompt_id | custom, image? (base64 PNG), fresh?}
                pdf = ns.pdf_path(b.get("file"))
                if not pdf or not (b.get("text") or b.get("image")):
                    return self.send_json({"error": "select some text or box a figure first"}, 400)
                return self.stream_ndjson(explain.stream(pdf, self.doc_info(b["file"])[0], str(b.get("text") or "")[:6000], int(b.get("page") or 1), b.get("prompt_id"),
                                                         str(b.get("custom") or "")[:2000] or None, b.get("image"), bool(b.get("fresh"))),
                                       job=self.job_of("explain", "Explain · " + self.title_of(b["file"]), file=b["file"]))
            if path == "/api/ai/ask":   # runs in this request's thread, outside JOBS: reading never waits for a download
                pdf, q = ns.pdf_path(b.get("file")), str(b.get("question") or "").strip()
                if not pdf or not q:
                    return self.send_json({"error": "a paper and a question are required"}, 400)
                ov = b.get("override") if isinstance(b.get("override"), dict) else None
                hist = [{"q": str(h.get("q", ""))[:3000], "a": str(h.get("a", ""))[:6000]} for h in (b.get("history") or [])[-4:] if isinstance(h, dict)]
                pg = b.get("pages") if isinstance(b.get("pages"), dict) else None          # {from, to}: the answer comes from one chapter of a book only
                if pg:
                    try:
                        pg = (int(pg["from"]), int(pg["to"]))
                    except (KeyError, TypeError, ValueError):
                        return self.send_json({"error": "bad chapter range"}, 400)
                    if pg[0] < 1 or pg[1] < pg[0]:
                        return self.send_json({"error": "bad chapter range"}, 400)
                return self.stream_ndjson(ask.stream(pdf, q[:6000], "full" if b.get("mode") == "full" else "rag", hist, ov, bool(b.get("fresh")), pg),
                                       job=self.job_of("ask", "Ask · " + q[:60], file=b["file"], tab="ask"))
            if path == "/api/ai/card":      # {file, fresh?}: write (or return) the paper's card; streams status / progress / card / done
                pdf = ns.pdf_path(b.get("file"))
                if not pdf:
                    return self.send_json({"error": "no such paper"}, 404)
                return self.stream_ndjson(papercard.stream(pdf, self.doc_info(b["file"])[0], bool(b.get("fresh"))), job=self.job_of("card", "Card · " + self.title_of(b["file"]), file=b["file"], tab="card"))
            if path == "/api/ai/skim":      # {file, fresh?}: find (or return) the paper's key sentences by lens; streams status / progress / skim / done
                pdf = ns.pdf_path(b.get("file"))
                if not pdf:
                    return self.send_json({"error": "no such paper"}, 404)
                return self.stream_ndjson(skim.stream(pdf, self.doc_info(b["file"])[0], bool(b.get("fresh"))), job=self.job_of("skim", "Skim · " + self.title_of(b["file"]), file=b["file"], tab="skim"))
            if path == "/api/ai/cards":     # {folder | files, fresh?}: cards for many papers, three at a time
                if b.get("folder"):
                    f, rows = self.folder_rows(b["folder"])
                    if not f:
                        return self.send_json({"error": "no such folder"}, 404)
                else:
                    want = set(b.get("files") or [])
                    rows = [r for r in pl.library() if r["file"] in want]
                return self.stream_ndjson(papercard.batch(rows, bool(b.get("fresh"))), job=self.job_of("cards", "Cards for %d paper%s" % (len(rows), "" if len(rows) == 1 else "s"), folder=b.get("folder") or "", ftab="evidence"))
            if path == "/api/ai/askmany":   # {scope:{folder?,journal?,year?,tag?,status?,star?}, question, mode: passages|cards, history, override, fresh}
                sc = b.get("scope") if isinstance(b.get("scope"), dict) else {}
                rows, q = self.scope_rows(sc), str(b.get("question") or "").strip()
                if rows is None or not q:
                    return self.send_json({"error": "a scope and a question are required"}, 400)
                ov = b.get("override") if isinstance(b.get("override"), dict) else None
                hist = [{"q": str(h.get("q", ""))[:3000], "a": str(h.get("a", ""))[:6000]} for h in (b.get("history") or [])[-4:] if isinstance(h, dict)]
                return self.stream_ndjson(multiask.stream(rows, q[:6000], "cards" if b.get("mode") == "cards" else "passages", hist, ov, bool(b.get("fresh"))),
                                       job=self.job_of("askmany", "Ask %s · %s" % ("a folder" if sc.get("folder") else "the library", q[:50]), folder=sc.get("folder") or "", ftab="ask", library=not sc.get("folder")))
            if path in ("/api/ai/matrix/fill", "/api/ai/matrix/estimate"):   # {id, columns?: [custom column ids], fresh?}
                f, rows = self.folder_rows(str(b.get("id") or ""))
                if not f:
                    return self.send_json({"error": "no such folder"}, 404)
                cols = [c for c in (b.get("columns") or []) if isinstance(c, str)] or None
                if path.endswith("/estimate"):
                    return self.send_json(matrix.estimate_fill(f["id"], rows, cols, bool(b.get("fresh"))))
                return self.stream_ndjson(matrix.fill(f["id"], rows, cols, bool(b.get("fresh"))), job=self.job_of("matrix", "Matrix · " + f["name"][:50], folder=f["id"], ftab="matrix"))
            if path in ("/api/ai/ideas/events", "/api/ai/ideas/generate", "/api/ai/ideas/develop", "/api/ai/ideas/estimate"):
                f, rows = self.folder_rows(str(b.get("id") or ""))
                if not f:
                    return self.send_json({"error": "no such folder"}, 404)
                if path.endswith("/estimate"):
                    ev = evidence.build(rows)
                    board = ideas.load(f["id"])
                    return self.send_json({"events": ideas.estimate_events(), "generate": ideas.estimate_generate(ev, [e for e in board["events"]["items"] if e.get("keep", True)], ideas._brief(b.get("brief"))), "papers": len(ev["papers"])})
                if path.endswith("/events"):
                    return self.stream_ndjson(ideas.refresh_events(f["id"], rows, str(b.get("focus") or "")[:400]), job=self.job_of("ideas", "Ideas · current events", folder=f["id"], ftab="lab"))
                if path.endswith("/develop"):
                    return self.stream_ndjson(ideas.develop(f["id"], str(b.get("idea") or "")), job=self.job_of("ideas", "Ideas · developing an outline", folder=f["id"], ftab="lab"))
                return self.stream_ndjson(ideas.generate(f["id"], rows, b.get("brief"), "replace" if b.get("mode") == "replace" else "add"), job=self.job_of("ideas", "Ideas · generating for " + f["name"][:40], folder=f["id"], ftab="lab"))
            if path in ("/api/ai/draft", "/api/ai/draft/estimate"):    # {id, settings:{topic, themes, words, style, pages, files}, fresh?}
                f, rows = self.folder_rows(str(b.get("id") or ""))
                if not f:
                    return self.send_json({"error": "no such folder"}, 404)
                if path.endswith("/estimate"):
                    return self.send_json(relatedwork.estimate(rows, b.get("settings")))
                return self.stream_ndjson(relatedwork.stream(f["id"], rows, b.get("settings"), bool(b.get("fresh"))), job=self.job_of("draft", "Related work · " + f["name"][:50], folder=f["id"], ftab="draft"))
            if path in ("/api/ai/flashcards", "/api/ai/flashcards/estimate"):    # {files: [paper files with a card], per: 4|8|12, fresh?}
                want = {str(x) for x in (b.get("files") or [])}
                rows = [r for r in pl.library() if r["file"] in want]
                per = int(b.get("per") or 8)
                if path.endswith("/estimate"):
                    return self.send_json(flashcards.estimate(rows, want, per))
                return self.stream_ndjson(flashcards.stream(rows, want, per, bool(b.get("fresh"))), job=self.job_of("flashcards", "Flashcards for %d paper%s" % (len(rows), "" if len(rows) == 1 else "s"), library=True, ltab="review"))
            if path in ("/api/ai/synthesis", "/api/ai/synthesis/estimate"):    # {id, settings:{files, focus}, fresh?}
                f, rows = self.folder_rows(str(b.get("id") or ""))
                if not f:
                    return self.send_json({"error": "no such folder"}, 404)
                if path.endswith("/estimate"):
                    return self.send_json(synthesis.estimate(rows, b.get("settings")))
                return self.stream_ndjson(synthesis.stream(f["id"], rows, b.get("settings"), bool(b.get("fresh"))), job=self.job_of("synthesis", "Synthesis · " + f["name"][:50], folder=f["id"], ftab="synthesis"))
            if path in ("/api/ai/review", "/api/ai/review/estimate"):    # {id, text, fresh?}: Reviewer 2 reads your text with the folder's papers in mind
                f, rows = self.folder_rows(str(b.get("id") or ""))
                if not f:
                    return self.send_json({"error": "no such folder"}, 404)
                if path.endswith("/estimate"):
                    return self.send_json(reviewer.estimate(rows, str(b.get("text") or "")))
                return self.stream_ndjson(reviewer.stream(f["id"], rows, str(b.get("text") or ""), bool(b.get("fresh"))), job=self.job_of("review", "Reviewer 2 · " + f["name"][:50], folder=f["id"], ftab="claims"))
            if path in ("/api/ai/claims", "/api/ai/claims/estimate"):    # {id, scope: folder|library, text, texts?: [sentences to check], fresh?}
                f, frows = self.folder_rows(str(b.get("id") or ""))
                if not f:
                    return self.send_json({"error": "no such folder"}, 404)
                lib_scope = b.get("scope") == "library"
                rows = self.scope_rows({}) if lib_scope else frows
                if path.endswith("/estimate"):
                    sp = claims.split(str(b.get("text") or "")[:60000])
                    texts = b.get("texts") if isinstance(b.get("texts"), list) else [c["text"] for c in sp if not c["own"]]
                    return self.send_json({"claims": [{**c, "cited": [relatedwork.evd.short_cite(r) for r in rows if r["file"] in claims.cited_papers(c["text"], rows)]} for c in sp], **claims.estimate(rows, texts)})
                texts = [str(t) for t in (b.get("texts") or [])][:claims.MAX_CLAIMS]
                return self.stream_ndjson(claims.stream(f["id"], rows, texts, "library" if lib_scope else "folder", bool(b.get("fresh")), str(b.get("text") or "")),
                                          job=self.job_of("claims", "Claim check · " + f["name"][:50], folder=f["id"], ftab="claims"))
            if path == "/api/ai/citeuse":    # {ref, contexts: [sentences that cite it], fresh?} -> {text, cost, cached}: how the paper uses that reference
                try:
                    return self.send_json(citeuse.answer(str(b.get("ref") or ""), [str(c) for c in (b.get("contexts") or [])][:6], bool(b.get("fresh"))))
                except ai.AIError as e:
                    return self.send_json({"error": str(e)}, 400)
            if path == "/api/ai/audio":      # {file | folder, fresh?}: the script of a short spoken briefing (the browser reads it aloud)
                if b.get("folder"):
                    f, frows = self.folder_rows(str(b["folder"]))
                    if not f:
                        return self.send_json({"error": "no such folder"}, 404)
                    return self.stream_ndjson(audio.stream_folder(f, frows, bool(b.get("fresh"))), job=self.job_of("audio", "Briefing · " + f["name"][:50], folder=f["id"], ftab="synthesis"))
                pdf = ns.pdf_path(b.get("file"))
                if not pdf:
                    return self.send_json({"error": "no such paper"}, 404)
                return self.stream_ndjson(audio.stream_paper(pdf, self.doc_info(b["file"])[0], bool(b.get("fresh"))), job=self.job_of("audio", "Briefing · " + self.title_of(b["file"]), file=b["file"], tab="card"))
            if path == "/api/ai/summary":   # {file, template?, fresh?}: stream a summary with one of your templates (not saved)
                pdf = ns.pdf_path(b.get("file"))
                if not pdf:
                    return self.send_json({"error": "no such paper"}, 404)
                if books.is_book(b.get("file")):                     # (a book would be sent whole, with a paper template and no estimate: its chapters are summarised instead)
                    return self.send_json({"error": books.BOOK_NO_TEMPLATE}, 400)
                return self.stream_ndjson(ask.summary_stream(pdf, self.doc_info(b["file"])[0], b.get("template"), bool(b.get("fresh"))), job=self.job_of("summary", "Summary · " + self.title_of(b["file"]), file=b["file"], tab="sum"))
            if path == "/api/ai/suggest":   # {file, q?, a?} -> {questions: [...]}
                pdf = ns.pdf_path(b.get("file"))
                if not pdf:
                    return self.send_json({"error": "no such paper"}, 404)
                try:
                    return self.send_json({"questions": ask.suggest(pdf, str(b.get("q") or "")[:2000] or None, str(b.get("a") or "")[:4000] or None)})
                except ai.AIError as e:
                    return self.send_json({"error": str(e)}, 400)
        if path == "/api/tags":
            try:
                out = ns.save_tags(b.get("tags"), int(b.get("rev") or 0))
            except ns.Conflict as c:
                return self.send_json({"error": "Tags changed in another window.", "current": c.current}, 409)
            except ns.Unreadable as e:
                return self.send_json({"error": str(e)}, 423)
            except (ValueError, TypeError) as e:
                return self.send_json({"error": str(e)[:120]}, 400)
            return self.send_json({"ok": True, "rev": out["rev"]})
        if path == "/api/run/weekly":
            if JOBS.busy():
                return self.started(False, None)
            run = pl.weekly_run(JOBS.run)  # one run per week: reuse this week's
            return self.started(start_fetch(run), run)
        if path == "/api/run/search":
            try:
                y0, y1 = sorted([int(b["year_from"]), int(b["year_to"])])
            except (KeyError, ValueError):
                return self.send_json({"error": "year_from and year_to required"}, 400)
            js = [j for j in b.get("journals") or []
                  if j in {x["abbr"] for x in pl.journals() if x.get("archive", True)}]
            if not js:
                return self.send_json({"error": "pick at least one journal"}, 400)
            meths = [m for m in b.get("methods") or [] if m in pl.method_labels()]
            topics = [t for t in b.get("topics") or [] if t in pl.topic_labels()]
            if not topics and not meths:
                topics = pl.topic_labels()  # nothing picked = all interests
            params = {"year_from": y0, "year_to": y1, "journals": js, "topics": topics, "methods": meths, "query": (b.get("query") or "").strip()[:200]}
            if not params["query"] and any(j.get("needs_query") for j in pl.journals() if j["abbr"] in js):
                return self.send_json({"error": "SSRN is searched by words: type a focus phrase (for example “platform governance”), or leave SSRN out."}, 400)
            old = pl.find_search(params)           # the same search as one you already ran: continue that one (its papers, verdicts and downloads are kept)
            run = (JOBS.run if JOBS.run and old and JOBS.run.id == old["id"] else pl.Run.load(old["id"]) if old else None) or pl.Run("search", params)
            return self.started(start_fetch(run), run)
        if path == "/api/run/terms":             # search the chosen journals and years for papers that contain any of the terms (terms.py); no AI unless asked
            import terms as tm
            try:
                y0, y1 = sorted([int(b["year_from"]), int(b["year_to"])])
            except (KeyError, ValueError):
                return self.send_json({"error": "year_from and year_to required"}, 400)
            words = tm.parse(b.get("terms") or "")
            if not words:
                return self.send_json({"error": "Type at least one term (for example difference-in-differences, DiD)."}, 400)
            js = [j for j in b.get("journals") or [] if j in {x["abbr"] for x in pl.journals() if x.get("archive", True)}]
            if not js:
                return self.send_json({"error": "pick at least one journal"}, 400)
            params = {"year_from": y0, "year_to": y1, "journals": js, "topics": pl.topic_labels(), "methods": [], "query": "", "terms": words}   # (topics: what "Screen with AI" ranks against)
            old = pl.find_search(params)
            run = (JOBS.run if JOBS.run and old and JOBS.run.id == old["id"] else pl.Run.load(old["id"]) if old else None) or pl.Run("search", params)
            return self.started(start_fetch(run), run)
        if path == "/api/sources":               # keys and addresses for where papers come from (OpenAlex, Unpaywall, Elsevier): saved in .env, never sent back
            try:
                return self.send_json(sources_save(b))
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)
        if path == "/api/download":
            ids = [i for i in b.get("ids") or [] if JOBS.run and i in JOBS.run.papers]
            ids = [i for i in ids if not (JOBS.run.papers[i].get("summary_by_hand") and JOBS.run.papers[i].get("dl_status") == "ok")]    # (a followed author's work is downloaded: its summary comes from the Library, with its estimate)
            if not ids:
                return self.send_json({"error": "nothing selected"}, 400)
            return self.started(start_download(ids), JOBS.run)
        if path == "/api/run/folder":           # {run_id, folder | new_name, ids?, pending}: papers of a run into a folder you choose (those in the library now; the rest when they arrive)
            try:
                ids = [str(x) for x in b["ids"]][:5000] if isinstance(b.get("ids"), list) else None

                def put(run):
                    fid = str(b.get("folder") or "") or fo.create(b.get("new_name"))["id"]      # (a new folder only once the run is found)
                    return dict(pl.run_to_folder(run, fid, ids, bool(b.get("pending", True))), run=run.to_dict(full=False))
                res = attach_to_run(b.get("run_id"), put)
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)
            if res is None:
                return self.send_json({"error": "That run is not there any more: reload the page."}, 404)
            return self.send_json({"ok": True, **res, "folders": fo.list_all()})
        if path == "/api/run/attach/keep":      # {run_id, id, token}: the PDF that did not look like the paper is the right one, you said
            try:
                res = attach_to_run(b.get("run_id"), lambda run: pl.attach_keep(run, str(b.get("id") or ""), b.get("token")))
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)
            if res is None:
                return self.send_json({"error": "That run is not there any more: reload the page."}, 404)
            return self.attached(res)
        if path == "/api/verdict":
            # the user's own call on a paper (e.g. borderline -> download queue)
            v = b.get("verdict")
            if v not in ("download", "borderline", "skip"):
                return self.send_json({"error": "bad verdict"}, 400)
            rid = b.get("run_id") or (JOBS.run.id if JOBS.run else None)
            run = JOBS.run if JOBS.run and JOBS.run.id == rid else pl.Run.load(rid or "")
            if not run:
                return self.send_json({"error": "no such run"}, 404)
            changed = []
            for i in b.get("ids") or []:
                p = run.papers.get(i)
                if not p:
                    continue
                p.setdefault("ai_verdict", p.get("verdict"))
                p["verdict"] = v
                p["user_set"] = v != p.get("ai_verdict")
                changed.append(i)
                HUB.emit("verdict", {k: p.get(k) for k in ("id", "verdict", "relevance", "topics", "method",
                                                           "reason", "ai_verdict", "user_set")})
            run.save(force=True)
            return self.send_json({"ok": True, "changed": changed})
        if path == "/api/rescreen":
            rid = str(b.get("run_id") or "")                # the run on the page (another may have been the last one to work)
            if JOBS.busy():
                return self.started(False, None)
            run = JOBS.run if JOBS.run and (not rid or JOBS.run.id == rid) else (pl.Run.load(rid) if rid else None)
            if not run:
                return self.send_json({"error": "no run"}, 400)
            JOBS.run = run
            failed = 0
            for p in run.papers.values():
                if p.get("verdict") == "error":                 # a failed screening is asked again ("Retry screening"); a weekly check never would: its papers are already seen
                    p.pop("verdict"); failed += 1
            todo = [i for i in run.order if not run.papers[i].get("verdict")]

            def job():
                emit = emit_for(run)
                emit("log", {"msg": "Screening %d papers again (%d had failed, %d had not been screened)" % (len(todo), failed, len(todo) - failed)})
                pl.screen(run, emit, JOBS.stop)
                left = sum(1 for i in todo if run.papers[i].get("verdict") in (None, "error"))
                emit("log", {"msg": "Screening finished: %d of %d papers judged%s" % (len(todo) - left, len(todo), (", %d still without a verdict" % left) if left else ""),
                             "level": "warn" if left else "ok"})
            ok = JOBS.start("fetch", job, run)
            return self.started(ok, run)
        if path == "/api/stop":
            JOBS.stop.set()
            return self.send_json({"ok": True})
        if path == "/api/check":                 # "Open the check": the running download opens that site's check page in a window between two papers
            host = str(b.get("host") or "")
            if not (JOBS.busy() and JOBS.kind == "download"):
                return self.send_json({"error": "No download is running."}, 409)
            if host not in JOBS.waiting:
                return self.send_json({"error": "No check is waiting for that site."}, 400)
            JOBS.waiting.pop(host, None)         # (a second press while it opens is not queued twice)
            JOBS.checks.put(host)
            return self.send_json({"ok": True})
        if path == "/api/login":
            which = b.get("which") or "publishers"
            if which in ("library", "nova") and not signins.profile()["signs_in"]:
                return self.send_json({"error": "Set up your library first (Settings → Library access): it has no sign-in of its own yet.", "setup": True}, 400)
            return self.started(start_login(which), None)
        if path == "/api/book/whole":            # {file, title}: summarise every chapter and section of a book that has no summary yet, then write its overview (in the background)
            if not bookjob:
                return self.send_json({"error": "AI features aren't installed"}, 501)
            if bookjob.status()["running"]:
                return self.send_json({"error": "A book is already being summarised: see the AI activity tray."}, 409)
            try:
                if not bookjob.start(str(b.get("file") or ""), str(b.get("title") or "")[:200]):
                    return self.send_json({"error": "Every chapter and section has its summary, and the overview is up to date."}, 400)
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)
            return self.send_json({"ok": True, "status": bookjob.status()})
        if path == "/api/summaries":             # {files}: summarise these papers (the list the estimate showed; one already summarised meanwhile is skipped) in the background
            if not summaries:
                return self.send_json({"error": "AI features aren't installed"}, 501)
            files = b.get("files")
            if files is not None and not isinstance(files, list):
                return self.send_json({"error": "files must be a list"}, 400)
            if summaries.status()["running"]:
                return self.send_json({"error": "Summaries are already being written: see the AI activity tray."}, 409)
            if JOBS.busy() and JOBS.kind == "download" and JOBS.summarizing:         # that job summarises what it downloads: two jobs on the same paper would pay for it twice
                return self.send_json({"error": "A download is running and writes its papers’ summaries itself. Press this again when it has finished."}, 409)
            if not summaries.start([str(f) for f in files] if files is not None else None):
                return self.send_json({"error": "Every one of these papers has a summary already."}, 400)
            return self.send_json({"ok": True, "status": summaries.status()})
        if path == "/api/library/kind":          # {files: [...], kind, label?}: what these papers are (journal article, preprint, conference paper, other): only <stem>.paper.json is written
            import kinds
            k = str(b.get("kind") or "")
            if k not in kinds.PAPER_KINDS:
                return self.send_json({"error": "Choose a type: journal article, preprint or working paper, conference paper, or report, thesis or other."}, 400)
            files = [str(f) for f in (b.get("files") or []) if isinstance(f, str)][:5000]
            by = {r["file"]: r for r in pl.library()}
            done = 0
            for f in files:
                r = by.get(f)
                if not r or r.get("kind") == "book":
                    continue
                pdf = ns.pdf_path(f)
                if not pdf:
                    continue
                kinds.write_sidecar(pdf, k, r.get("journal") or "", str(b.get("label") or "") if k == "other" else "", r.get("key") or pl.dl.index_key(r))
                done += 1
            return self.send_json({"ok": True, "changed": done})
        if path == "/api/library/delete":        # {files: [...]}: delete these papers or books for good (files, notes, summary, card, search index); a run never downloads them again
            files = b.get("files")
            if not isinstance(files, list) or not files or len(files) > 500:
                return self.send_json({"error": "files must be a list of library files"}, 400)
            if JOBS.busy() and JOBS.kind == "download":
                return self.send_json({"error": "A download is running. Delete when it has finished."}, 409)
            done, errors = [], []
            for f in files:
                try:
                    done.append(deletion.delete_item(str(f)))
                except (ValueError, OSError) as e:
                    errors.append({"file": str(f)[:300], "error": str(e)[:200]})
            return self.send_json({"ok": not errors, "deleted": done, "errors": errors})
        if path == "/api/library/restore":       # {tokens: [...]}: put back what you deleted (kept for 30 days: deletion.restore)
            done, errors = [], []
            for t in (b.get("tokens") or [])[:500]:
                try:
                    done.append(deletion.restore(str(t)))
                except (ValueError, OSError) as e:
                    errors.append({"token": str(t)[:60], "error": str(e)[:200]})
            if done and indexer:
                indexer.kick(2)
            return self.send_json({"ok": not errors, "restored": done, "errors": errors, "kept": deletion.recently_deleted()})
        if path == "/api/runs/delete":           # {id}: delete the record of a past run (its papers stay in the library)
            rid = str(b.get("id") or "")
            if JOBS.busy() and JOBS.run is not None and JOBS.run.id == os.path.basename(rid):
                return self.send_json({"error": "That run is still working. Stop it first."}, 409)
            try:
                title = deletion.delete_run(rid)
            except ValueError as e:
                return self.send_json({"error": str(e)}, 404)
            if JOBS.run is not None and JOBS.run.id == os.path.basename(rid):
                JOBS.run = None
            return self.send_json({"ok": True, "title": title})
        if path == "/api/library/check":         # start "Check my PDFs"
            return self.send_json({"ok": True, "started": start_pdfcheck(), **PDFCHECK})
        if path in ("/api/library/check/keep", "/api/library/check/remove"):
            f = str(b.get("file") or "")
            hit = next((r for r in PDFCHECK["results"] if r["file"] == f and r["verdict"] in ("mismatch", "unverified")), None)
            if not hit:
                return self.send_json({"error": "That PDF is not in the list of the last check. Check again."}, 404)
            if path.endswith("/keep"):
                identity.keep(f)
                PDFCHECK["results"] = [r for r in PDFCHECK["results"] if r["file"] != f]
                PDFCHECK["counts"]["kept"] = PDFCHECK["counts"].get("kept", 0) + 1
                PDFCHECK["counts"][hit["verdict"]] = max(0, PDFCHECK["counts"].get(hit["verdict"], 1) - 1)
                return self.send_json({"ok": True, **PDFCHECK})
            again = bool(b.get("download", True)) and related is not None
            if again and JOBS.busy():                      # the download is started by this very request: do not take the PDF away if it cannot start
                return self.send_json({"error": "Another task is running — wait or press Stop, then try again."}, 409)
            try:
                gone = identity.remove(f)
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)
            PDFCHECK["results"] = [r for r in PDFCHECK["results"] if r["file"] != f]
            PDFCHECK["counts"][hit["verdict"]] = max(0, PDFCHECK["counts"].get(hit["verdict"], 1) - 1)
            out = {"ok": True, "moved_to": gone["moved_to"], "title": gone["title"], "download": False}
            if again:
                papers, have = related.run_papers([{"doi": gone["doi"], "title": gone["title"], "authors": gone["authors"], "venue": gone["journal"], "date": gone["date"], "reason": "re-download: the PDF was another paper"}])
                if papers:
                    run = pl.Run("picks", {"label": "Download again: " + re.sub(r"\s+", " ", gone["title"])[:40]})
                    for p in papers:
                        if gone.get("kind"):
                            p["kind"], p["kind_label"] = gone["kind"], gone.get("label") or ""        # (a conference paper stays one)
                        run.add_paper(p)
                    run.stage, run.status = "ready", "stopped"
                    run.save(force=True)
                    JOBS.run = run
                    out["download"] = start_download([p["id"] for p in papers])
                    out["run"] = run.to_dict(full=False)
            return self.send_json({**out, **{k: PDFCHECK[k] for k in ("state", "counts", "results", "at")}})
        if path == "/api/library/test":          # is the library access working now? (a few seconds to half a minute: it opens the library page in the background)
            if JOBS.busy():
                return self.send_json({"error": "Wait for the task that is running to finish, then test again."}, 409)
            if not LIBTEST_LOCK.acquire(blocking=False):
                return self.send_json({"error": "A test is already running."}, 409)
            try:
                try:
                    r = subprocess.run(paths.cmd("browser.py", "library-test"), cwd=ROOT, capture_output=True, text=True, timeout=120)
                    lines = [l for l in (r.stdout or "").splitlines() if l.strip().startswith("{")]
                    res = json.loads(lines[-1]) if lines else {"state": "error", "message": "The test could not run. %s" % (_why(r) or "No answer came back.")}
                except subprocess.TimeoutExpired:
                    res = {"state": "error", "message": "The test took too long and was stopped. Check your internet connection and the addresses in Settings."}
                except ValueError:
                    res = {"state": "error", "message": "The test gave an answer the app could not read."}
                res = {**signins.record_test(res), "status": signins.status()}
                return self.send_json(res)
            finally:
                LIBTEST_LOCK.release()
        if path == "/api/library/access":
            try:
                return self.send_json(signins.save_profile(b))
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)
        if path == "/api/library/detect":        # work out OpenAthens / EZproxy / EBSCO from the library's web site or a link copied from it (reads a few of its pages)
            given = str(b.get("given") or "").strip()
            if not given:
                return self.send_json({"error": "Paste your library's web address, or a link to a database copied from its site."}, 400)
            try:
                return self.send_json(signins.detect(given))
            except Exception as e:  # noqa: BLE001
                return self.send_json({"error": "Could not read that site: %s" % str(e)[:160]}, 502)
        if path == "/api/update/check":
            return self.send_json(updater.check())
        if path == "/api/update/install":
            st = updater.public()
            if not st["available"]:
                return self.send_json({"error": "You already have the newest version."}, 409)
            return self.send_json({"ok": updater.install(), **updater.public()})
        if path == "/api/update/settings":      # {auto, feed, skipped, ack}
            try:
                if b.get("ack"):
                    updater.acknowledge()
                updater.save_settings({k: b[k] for k in ("auto", "feed", "skipped") if k in b})
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)
            return self.send_json(updater.public())
        if path == "/api/support/visit":        # the page was opened or came back after a while: counts one visit
            support.visit()
            return self.send_json(support.public())
        if path == "/api/support/action":       # {action: shown | never | supported | reminders, value}
            try:
                return self.send_json(support.act(str(b.get("action") or ""), b.get("value")))
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)
        self.send_json({"error": "not found"}, 404)

    def started(self, ok, run):
        if not ok:
            return self.send_json({"error": "Another task is running — wait or press Stop."}, 409)
        self.send_json({"ok": True, "run": run.to_dict(full=False) if run else None})


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # the browser closing an idle/keep-alive connection is normal: don't print a traceback
        if isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def free_port(start):
    for port in range(start, start + 50):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise SystemExit("no free port")


_LOCK = None     # the open lock file lives as long as the process (the lock goes with it, even after a crash)


def take_lock(port):
    """One dashboard per project: two of them write the same files (notes, folders, indexes) and would overwrite each other.
    Returns None when this process now holds the lock, or the details ({pid, port, url}) of the dashboard that already runs."""
    global _LOCK
    path = os.path.join(paths.LOCAL, "server.lock")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    f = open(path, "a+")
    try:
        import fcntl
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except ImportError:          # no fcntl (Windows): trust the file if the dashboard named in it still answers
        f.seek(0)
        try:
            old = json.load(f)
            urllib.request.urlopen(old["url"] + "/api/config", timeout=1)
            f.close()
            return old
        except Exception:  # noqa: BLE001
            pass
    except OSError:
        f.seek(0)
        try:
            old = json.load(f)
        except ValueError:
            old = {}
        f.close()
        return old or {"pid": None, "port": None, "url": None}
    f.seek(0); f.truncate()
    json.dump({"pid": os.getpid(), "port": port, "url": "http://localhost:%d" % port, "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "by": "app" if paths.FROZEN else "source", "code": os.path.dirname(HERE) if not paths.FROZEN else "", "version": updater.version()}, f)   # who holds the folder (the Mac app explains a clash with it)
    f.flush()
    _LOCK = f
    return None


def _daily_snapshot():
    try:
        deletion.purge_old()            # (what you deleted more than 30 days ago leaves the disk now)
    except Exception:  # noqa: BLE001
        pass
    try:
        aifiles.snapshot()
    except Exception as e:  # noqa: BLE001      (a full disk, a locked file: the app starts all the same)
        print("(no snapshot today: %s)" % str(e)[:120])


def start(port=8765, quiet=False):
    """Everything main() does except waiting for requests. The Mac app runs the server in a thread of its own process with this.
    Returns (server, url), or (None, url) when a dashboard for this folder already runs (url is where)."""
    paths.seed_home()            # a new data folder (or a fresh checkout): the folders and the settings files it starts from
    port = free_port(port)
    other = take_lock(port)
    if other is not None:
        return None, other.get("url")
    if not os.environ.get("ANTHROPIC_API_KEY") and not quiet:
        print("WARNING: ANTHROPIC_API_KEY missing from .env — screening and summaries will fail.")
    for canon, olds in pl.migrate_weekly():
        print("Merged %d weekly runs into %s" % (len(olds), canon))
    srv = Server(("127.0.0.1", port), Handler)
    syncinfo.start()           # tell the other computers that use this data folder that this one has it open (devices/<id>.json, refreshed every 30 s)
    try:
        syncinfo.tidy_caches() # copies Dropbox made of search-index or saved-answer files when two computers wrote the same one at once: they are regenerable, so they are simply removed
    except Exception:  # noqa: BLE001
        pass
    if ai:
        ai.pricing.refresh_soon()  # OpenRouter's price list, read again when it is a day old (only when an OpenRouter model is in use)
    importer.sweep()           # dropped PDFs nobody confirmed or cancelled within a day are forgotten (a confirmation of an older one is refused anyway)
    updater.start_background()
    threading.Thread(target=lambda: ac.backfill(pl.library()), daemon=True).start()   # authors the feeds left out (a few Crossref lookups, once)
    threading.Thread(target=_daily_snapshot, daemon=True).start()                     # your notes, folders, index and settings, kept once a day on this computer
    if indexer:
        indexer.kick(25)       # read papers into the index for Ask a little after start-up (local model only, switchable in the activity tray)
    return srv, "http://localhost:%d" % port


def main():
    a = sys.argv[1:]
    srv, url = start(int(a[a.index("--port") + 1]) if "--port" in a else 8765)
    if srv is None:
        print("Journal Watcher is already running%s." % (" at " + url if url else ""))
        print("Use that window: two copies would overwrite each other's notes and folders. Close it first if you want to start again.")
        if url and "--no-open" not in a:
            webbrowser.open(url)
        raise SystemExit(0)
    print("Journal Watcher dashboard running at %s  (close this window to stop)" % url)
    if "--no-open" not in a:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
