"""Using one data folder from more than one computer (it lives in Dropbox, say).

Dropbox copies files between computers; it does not lock them and it cannot merge them. So this module does three things the folder itself cannot:

    syncinfo.start()           every 30 s this computer writes a small "I am here" file into the data folder (devices/<id>.json) and removes it when the app closes
    syncinfo.others()          the other computers whose file says they were here in the last 3 minutes ("also open on Work Mac")
    syncinfo.conflicts()       files Dropbox had to duplicate because both computers changed them ("… (Joe's MacBook's conflicted copy 2026-11-03).csv")
    syncinfo.merge_index(rel)  the library index (Papers/_index.csv) of both copies joined: no paper is lost
    syncinfo.set_aside(rel)    any conflicted copy moved (never deleted) to Papers/_conflicts/<time>/

What is kept on one computer only (the sign-in browser, the embedding model, lookup caches, the lock) is not in the data folder at all: see paths.LOCAL. The search index and the saved AI
answers are shared through the folder (paths.SHARED_CACHES); they can be made again, so a copy Dropbox duplicates there is simply removed (tidy_caches), never shown as a conflict.
"""
import atexit
import csv
import json
import os
import re
import shutil
import socket
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import download as dl  # noqa: E402
import paths  # noqa: E402

BEAT_SECONDS = 30
HERE_WINDOW = 180          # a computer counts as open if its file was refreshed in the last 3 minutes (clocks of two computers differ by seconds, and Dropbox needs a little while)
KEEP_DAYS = 60             # files of computers not seen for this long are forgotten
CONFLICT_RE = re.compile(r"\s*\([^()]*conflicted copy[^()]*\)", re.I)
_beat_thread = None
_stop = threading.Event()


def _dir():
    return os.path.join(paths.BASE, "devices")


def _me_file():
    return os.path.join(paths.LOCAL, "device.json")


def _load(p, default):
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, type(default)) else default
    except (OSError, ValueError):
        return default


def _save(p, obj):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".%d.tmp" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, p)


def default_name():
    n = socket.gethostname().split(".")[0].strip()
    return re.sub(r"[-_]+", " ", n) or "This computer"


def me():
    """{"id", "name"} of this computer. The id is made once and kept in paths.LOCAL (it must not travel with the data folder); the name defaults to the computer's name and you can change it."""
    d = _load(_me_file(), {})
    if not re.fullmatch(r"[0-9a-f]{32}", str(d.get("id") or "")):
        d["id"] = uuid.uuid4().hex
        d["name"] = default_name()
        _save(_me_file(), d)
    return {"id": d["id"], "name": str(d.get("name") or default_name())}


def rename(name):
    name = re.sub(r"\s+", " ", str(name or "")).strip()[:40]
    if not name:
        raise ValueError("Give this computer a name.")
    d = me()
    d["name"] = name
    _save(_me_file(), d)
    beat()
    return d


def beat():
    """Write this computer's 'I am here' file now."""
    m = me()
    try:
        import updater
        version = updater.version()
    except Exception:  # noqa: BLE001
        version = ""
    _save(os.path.join(_dir(), m["id"] + ".json"), {"id": m["id"], "name": m["name"], "seen": datetime.now(timezone.utc).isoformat(timespec="seconds"), "pid": os.getpid(),
                                                    "version": version, "app": bool(paths.FROZEN)})


def _loop():
    while not _stop.wait(BEAT_SECONDS):
        try:
            beat()
        except Exception:  # noqa: BLE001      (a folder that is briefly unavailable: the next beat tries again)
            pass


def leave():
    try:
        os.remove(os.path.join(_dir(), me()["id"] + ".json"))
    except OSError:
        pass


def start():
    """Begin the heartbeat (once). Safe to call twice."""
    global _beat_thread
    if _beat_thread and _beat_thread.is_alive():
        return
    try:
        beat()
    except Exception:  # noqa: BLE001
        pass
    _stop.clear()
    _beat_thread = threading.Thread(target=_loop, daemon=True, name="device-heartbeat")
    _beat_thread.start()
    atexit.register(leave)


def _age(iso, now):
    try:
        return max(0.0, (now - datetime.fromisoformat(iso)).total_seconds())
    except (TypeError, ValueError):
        return None


def computers(now=None):
    """Every computer that has used this folder (not this one): [{"id", "name", "seen", "age_s", "open", "version"}], the open ones first."""
    now = now or datetime.now(timezone.utc)
    mine = me()["id"]
    out = []
    d = _dir()
    for n in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if not n.endswith(".json"):
            continue
        r = _load(os.path.join(d, n), {})
        age = _age(r.get("seen"), now)
        if r.get("id") == mine or age is None or not re.fullmatch(r"[0-9a-f]{32}", str(r.get("id") or "")):
            continue
        if age > KEEP_DAYS * 86400:
            try:
                os.remove(os.path.join(d, n))
            except OSError:
                pass
            continue
        out.append({"id": r["id"], "name": str(r.get("name") or "Another computer")[:40], "seen": r.get("seen"), "age_s": int(age), "open": age <= HERE_WINDOW, "version": str(r.get("version") or "")})
    return sorted(out, key=lambda x: (not x["open"], x["age_s"]))


def others(now=None):
    return [c for c in computers(now) if c["open"]]


# ---------------------------------------------------------------- Dropbox conflicts
def _scan_dirs():
    yield paths.ROOT, False                    # (the files directly in the data folder: .env, …)
    for top in (paths.PAPERS, paths.BASE):
        yield top, True


_cache = {"at": 0.0, "rows": []}
STALE_TMP_S = 86400


def _cache_dirs():
    """The shared, regenerable folders (real paths): the search index and the saved AI answers."""
    return [os.path.realpath(os.path.join(paths.STATE, n)) for n in paths.SHARED_CACHES]


def tidy_caches():
    """Remove what the shared caches cannot use: copies Dropbox made when two computers wrote the same file at once ('… conflicted copy …': the original is still there, and both held the same
    thing, since the names come from the paper's or the request's content), and temporary files an interrupted write left more than a day ago. Returns how many files were removed."""
    n, now = 0, time.time()
    for d in _cache_dirs():
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for fn in names:
            full = os.path.join(d, fn)
            try:
                if CONFLICT_RE.search(fn) or (fn.endswith(".tmp") and now - os.path.getmtime(full) > STALE_TMP_S):
                    os.remove(full); n += 1
            except OSError:
                pass
    return n


def cache_size():
    """{"files", "bytes"} of the shared search index and saved answers (Settings shows it: it travels with the data folder)."""
    files = size = 0
    for d in _cache_dirs():
        try:
            for e in os.scandir(d):
                if e.is_file():
                    files += 1; size += e.stat().st_size
        except OSError:
            pass
    return {"files": files, "bytes": size}


def conflicts(fresh=False):
    """(Cached for 20 s: the banner asks often and a scan walks Papers/.) Files whose name says Dropbox kept two versions: [{"path" (relative to the data folder), "original", "kind" (index | json | other), "original_exists", "size", "modified", "original_modified"}]."""
    if not fresh and time.time() - _cache["at"] < 20:
        return list(_cache["rows"])
    out = []
    root = os.path.realpath(paths.ROOT)
    skip = {os.path.realpath(os.path.join(paths.PAPERS, "_conflicts")), os.path.realpath(os.path.join(paths.PAPERS, "_removed")), os.path.realpath(paths.LOCAL), *_cache_dirs()}
    for top, deep in _scan_dirs():
        top = os.path.realpath(top)            # (all paths below are real ones: a data folder reached through a link, such as ~/Dropbox or /var on a Mac, still gives paths relative to itself)
        if not os.path.isdir(top):
            continue
        for dirpath, dirnames, filenames in os.walk(top):
            if not deep:
                dirnames[:] = []
            else:
                dirnames[:] = [x for x in dirnames if os.path.realpath(os.path.join(dirpath, x)) not in skip and x not in (".git", "node_modules", "browser-profile")]
            for n in filenames:
                if not CONFLICT_RE.search(n):
                    continue
                full = os.path.join(dirpath, n)
                orig = os.path.join(dirpath, CONFLICT_RE.sub("", n, count=1))
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                base = os.path.basename(orig)
                ost = os.stat(orig) if os.path.exists(orig) else None
                out.append({"path": os.path.relpath(full, root), "original": os.path.relpath(orig, root), "original_exists": bool(ost),
                            "kind": "index" if base == "_index.csv" else "json" if base.endswith(".json") else "other", "join": join_kind(orig) if ost else "", "size": st.st_size,
                            "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(timespec="seconds"),
                            "original_modified": datetime.fromtimestamp(ost.st_mtime, timezone.utc).isoformat(timespec="seconds") if ost else ""})
    out.sort(key=lambda c: c["path"])
    _cache.update(at=time.time(), rows=out)
    return list(out)


def _inside(rel):
    """The absolute path of a conflicted copy named by `rel`, or a ValueError: it must be a file in the data folder (not a link out of it) and be named like a conflicted copy."""
    root = os.path.realpath(paths.ROOT)
    full = os.path.realpath(os.path.join(root, str(rel or ""))) if isinstance(rel, str) and "\0" not in rel else ""
    if not full.startswith(root + os.sep) or not os.path.isfile(full) or not CONFLICT_RE.search(os.path.basename(full)):
        raise ValueError("That is not a conflicted copy in your data folder.")
    return full, root


def set_aside(rel):
    """Move a conflicted copy to Papers/_conflicts/<time>/<same place> (never delete it). Returns where it went, relative to the data folder."""
    full, root = _inside(rel)
    dest = os.path.join(os.path.realpath(paths.PAPERS), "_conflicts", time.strftime("%Y%m%d-%H%M%S"), os.path.relpath(full, root))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.move(full, dest)
    _cache["at"] = 0.0
    return os.path.relpath(dest, root)


def _rows(p):
    with open(p, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _better(a, b, root=None):
    """Of two rows for the same paper, the one to keep: a downloaded one (its PDF present under `root`, the data folder) over a failed one, then the one checked later."""
    root = root or paths.ROOT
    def rank(r):
        return (r.get("status") == "ok" and bool(r.get("file")) and os.path.exists(os.path.join(root, r.get("file") or "")), r.get("date_checked") or "")
    return a if rank(a) >= rank(b) else b


def join_rows(mine, theirs, root=None):
    """The rows of two copies of Papers/_index.csv as one list: every paper of either copy stays once (by DOI, else title; _better picks the row), in the order first seen.
    Also used when a data folder is copied into another one (movehome.py), with `root` the folder the joined list is written to."""
    merged, order = {}, []
    for r in list(mine) + list(theirs):
        k = dl.index_key(r) or json.dumps(r, sort_keys=True)
        if k not in merged:
            merged[k] = r
            order.append(k)
        else:
            merged[k] = _better(merged[k], r, root)
    return [merged[k] for k in order]


def write_index(path, rows):
    """Write rows as Papers/_index.csv (the standard columns first, then any others), through a temporary file."""
    cols = list(dl.INDEX_COLS) + [c for r in rows for c in r if c not in dl.INDEX_COLS and c is not None]
    cols = list(dict.fromkeys(cols))
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def merge_index(rel):
    """Join the two copies of Papers/_index.csv: every paper of either stays (the same paper, by DOI or title, keeps its better row). The conflicted copy is set aside, not deleted.
    Returns {"papers", "added", "set_aside"}."""
    full, root = _inside(rel)
    orig = os.path.join(os.path.dirname(full), CONFLICT_RE.sub("", os.path.basename(full), count=1))
    if os.path.basename(orig) != "_index.csv" or os.path.dirname(os.path.realpath(orig)) != os.path.realpath(paths.PAPERS):
        raise ValueError("Only the library index (Papers/_index.csv) can be joined this way.")
    mine = _rows(orig) if os.path.exists(orig) else []
    theirs = _rows(full)
    rows = join_rows(mine, theirs)
    write_index(orig, rows)
    return {"papers": len(rows), "added": len(rows) - len(mine), "set_aside": set_aside(rel)}


def join_kind(orig):
    """Which of your own files can be joined from two copies (both sides kept): "index", "notes", "status", "folder", "tags", "usage"; "" for any other file."""
    base, d = os.path.basename(orig), os.path.realpath(os.path.dirname(orig))
    papers = os.path.realpath(paths.PAPERS)
    if base == "_index.csv" and d == papers:
        return "index"
    if base.endswith(".notes.json") and (d + os.sep).startswith(papers + os.sep):
        return "notes"
    if base == "_status.json" and d == papers:
        return "status"
    if base == "_tags.json" and d == papers:
        return "tags"
    if d == os.path.join(papers, "_folders") and base.endswith(".json") and "." not in base[:-5]:
        return "folder"
    if base == "ai_usage.jsonl":
        return "usage"
    return ""


def _json(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _write_json(p, obj):
    tmp = p + ".join.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def _by_id(mine, theirs):
    """Two lists of items with an "id": every item of either (the same id once, yours kept). Returns (the list, how many came from the other copy)."""
    mine = [x for x in (mine or []) if isinstance(x, dict)]
    have = {x.get("id") for x in mine if x.get("id")}
    new = [x for x in (theirs or []) if isinstance(x, dict) and x.get("id") and x["id"] not in have]
    return mine + new, len(new)


def join_notes(a, b):
    """A paper's notes from two computers: every highlight, card, bookmark, frame and arrow of either (by id; one you changed on both keeps this computer's
    wording); the note on the paper keeps both texts when they differ. Returns (notes, items that came from the other copy)."""
    out, added = dict(a), 0
    for k in ("highlights", "cards", "bookmarks", "frames", "links"):
        out[k], n = _by_id(a.get(k), b.get(k))
        added += n
        if not out[k] and k in ("frames", "links"):
            out.pop(k)
    na, nb = str(a.get("paper_note") or "").strip(), str(b.get("paper_note") or "").strip()
    if nb and nb != na:
        out["paper_note"] = (na + "\n\n(from the other computer)\n" + nb) if na else nb
        added += 1
    out["rev"] = max(int(a.get("rev") or 0), int(b.get("rev") or 0)) + 1
    return out, added


def join_status(a, b):
    """Reading status from two computers: every paper of either; for a paper in both, the entry opened last wins field by field, a star or a takeaway on either stays."""
    pa, pb = dict((a or {}).get("papers") or {}), (b or {}).get("papers") or {}
    added = 0
    for k, eb in pb.items():
        ea = pa.get(k)
        if not isinstance(eb, dict):
            continue
        if not isinstance(ea, dict):
            pa[k] = eb
            added += 1
            continue
        newer, older = (eb, ea) if str(eb.get("opened") or "") > str(ea.get("opened") or "") else (ea, eb)
        m = {**older, **{x: y for x, y in newer.items() if y not in ("", None)}}
        m["star"] = bool(ea.get("star") or eb.get("star"))
        if not m.get("star"):
            m.pop("star", None)
        if m != ea:
            added += 1
        pa[k] = m
    return {"version": 1, "papers": pa}, added


def merge(rel):
    """Join the two copies of one of your own files so that nothing of either is lost (join_kind says which files); the conflicted copy is then set aside, never
    deleted. Returns {"kind", "added" (what came from the other copy), "said" (a sentence for the page), "set_aside", and for the index "papers"}."""
    full, root = _inside(rel)
    orig = os.path.join(os.path.dirname(full), CONFLICT_RE.sub("", os.path.basename(full), count=1))
    kind = join_kind(orig)
    if kind == "index":
        r = merge_index(rel)
        return dict(r, kind=kind, said="Joined: %d papers in the list (%d from the other copy)." % (r["papers"], r["added"]))
    if not kind:
        raise ValueError("This file cannot be joined by the app: keep one of the two and set the other aside.")
    if not os.path.exists(orig):
        raise ValueError("The file this is a copy of is not there: set the copy aside, or rename it by hand.")
    try:
        if kind == "usage":
            with open(orig, encoding="utf-8") as f:
                mine = f.read().splitlines()
            with open(full, encoding="utf-8") as f:
                theirs = f.read().splitlines()
            have = set(mine)
            new = [x for x in theirs if x.strip() and x not in have]
            if new:
                tmp = orig + ".join.tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write("\n".join(sorted([x for x in mine if x.strip()] + new, key=lambda x: (json.loads(x).get("ts") or "") if x.startswith("{") else "")) + "\n")
                os.replace(tmp, orig)
            added, said = len(new), "Joined: %d AI spending records from the other copy." % len(new)
        else:
            a, b = _json(orig), _json(full)
            if not isinstance(a, dict) or not isinstance(b, dict):
                raise ValueError("not the expected kind of file")
            if kind == "notes":
                out, added = join_notes(a, b)
                said = "Joined: %d highlights and notes from the other copy." % added
            elif kind == "status":
                out, added = join_status(a, b)
                said = "Joined: reading status of %d papers from the other copy." % added
            elif kind == "tags":
                tags, added = _by_id(a.get("tags"), b.get("tags"))
                out = dict(a, tags=tags, rev=max(int(a.get("rev") or 0), int(b.get("rev") or 0)) + 1)
                said = "Joined: %d tags from the other copy." % added
            else:                                           # a folder: the papers of both lists
                mine = [k for k in (a.get("papers") or []) if isinstance(k, str)]
                new = [k for k in (b.get("papers") or []) if isinstance(k, str) and k not in set(mine)]
                out, added = dict(a, papers=mine + new), len(new)
                said = "Joined: %d papers from the other copy are in the folder too." % added
            _write_json(orig, out)
    except (OSError, ValueError) as e:
        raise ValueError("The two copies could not be joined (%s). Nothing was changed." % (str(e)[:100] or "one of them cannot be read"))
    return {"kind": kind, "added": added, "said": said, "set_aside": set_aside(rel)}


def info():
    """Everything the Settings page shows."""
    m = me()
    comps = computers()
    return {"me": m, "data_folder": paths.ROOT, "local_folder": paths.LOCAL, "separate": os.path.realpath(paths.LOCAL) != os.path.realpath(paths.STATE),
            "computers": comps, "open_elsewhere": [c for c in comps if c["open"]], "conflicts": conflicts(fresh=True), "shared_cache": cache_size()}
