"""Using one data folder from more than one computer (it lives in Dropbox, say).

Dropbox copies files between computers; it does not lock them and it cannot merge them. So this module does three things the folder itself cannot:

    syncinfo.start()           every 30 s this computer writes a small "I am here" file into the data folder (devices/<id>.json) and removes it when the app closes
    syncinfo.others()          the other computers whose file says they were here in the last 3 minutes ("also open on Work Mac")
    syncinfo.conflicts()       files Dropbox had to duplicate because both computers changed them ("… (Joe's MacBook's conflicted copy 2026-11-03).csv")
    syncinfo.merge_index(rel)  the library index (Papers/_index.csv) of both copies joined: no paper is lost
    syncinfo.set_aside(rel)    any conflicted copy moved (never deleted) to Papers/_conflicts/<time>/

What is kept on one computer only (the sign-in browser, the search index, caches, the lock) is not in the data folder at all: see paths.LOCAL.
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


def conflicts(fresh=False):
    """(Cached for 20 s: the banner asks often and a scan walks Papers/.) Files whose name says Dropbox kept two versions: [{"path" (relative to the data folder), "original", "kind" (index | json | other), "original_exists", "size", "modified", "original_modified"}]."""
    if not fresh and time.time() - _cache["at"] < 20:
        return list(_cache["rows"])
    out = []
    root = os.path.realpath(paths.ROOT)
    skip = {os.path.realpath(os.path.join(paths.PAPERS, "_conflicts")), os.path.realpath(os.path.join(paths.PAPERS, "_removed")), os.path.realpath(paths.LOCAL)}
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
                            "kind": "index" if base == "_index.csv" else "json" if base.endswith(".json") else "other", "size": st.st_size,
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


def _better(a, b):
    """Of two rows for the same paper, the one to keep: a downloaded one over a failed one, then the one checked later."""
    def rank(r):
        return (r.get("status") == "ok" and bool(r.get("file")) and os.path.exists(os.path.join(paths.ROOT, r.get("file") or "")), r.get("date_checked") or "")
    return a if rank(a) >= rank(b) else b


def merge_index(rel):
    """Join the two copies of Papers/_index.csv: every paper of either stays (the same paper, by DOI or title, keeps its better row). The conflicted copy is set aside, not deleted.
    Returns {"papers", "added", "set_aside"}."""
    full, root = _inside(rel)
    orig = os.path.join(os.path.dirname(full), CONFLICT_RE.sub("", os.path.basename(full), count=1))
    if os.path.basename(orig) != "_index.csv" or os.path.dirname(os.path.realpath(orig)) != os.path.realpath(paths.PAPERS):
        raise ValueError("Only the library index (Papers/_index.csv) can be joined this way.")
    mine = _rows(orig) if os.path.exists(orig) else []
    theirs = _rows(full)
    merged, order = {}, []
    for r in mine + theirs:
        k = dl.index_key(r) or json.dumps(r, sort_keys=True)
        if k not in merged:
            merged[k] = r
            order.append(k)
        else:
            merged[k] = _better(merged[k], r)
    rows = [merged[k] for k in order]
    cols = list(dl.INDEX_COLS) + [c for r in rows for c in r if c not in dl.INDEX_COLS and c is not None]
    cols = list(dict.fromkeys(cols))
    tmp = orig + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, orig)
    return {"papers": len(rows), "added": len(rows) - len(mine), "set_aside": set_aside(rel)}


def info():
    """Everything the Settings page shows."""
    m = me()
    comps = computers()
    return {"me": m, "data_folder": paths.ROOT, "local_folder": paths.LOCAL, "separate": os.path.realpath(paths.LOCAL) != os.path.realpath(paths.STATE),
            "computers": comps, "open_elsewhere": [c for c in comps if c["open"]], "conflicts": conflicts(fresh=True)}
