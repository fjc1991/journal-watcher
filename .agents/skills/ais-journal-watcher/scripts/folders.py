"""Virtual folders: named groups of papers. A folder is a small list file, Papers/_folders/<id>.json; the PDFs never move and a
paper can be in several folders. Papers are identified by download.index_key (DOI, else title), so a re-download keeps them.

    folders.list_all()                  # [{id, name, count, created}]
    folders.create("Platform governance") -> folder
    folders.add(id, [keys]) / remove(id, [keys]) / rename(id, name) / delete(id)
    folders.membership()                # {paper key: [folder id, ...]} for the Library
Later features write next to these files: <id>.ideas.json (Research Ideas Lab), <id>.digest.md.
"""
import json
import os
import re
import sys
import threading
import unicodedata
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import download as dl  # noqa: E402

DIR = os.path.join(dl.PAPERS, "_folders")
_lock = threading.RLock()


def _path(fid):
    return os.path.join(DIR, fid + ".json")


def _slug(name):
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:40] or "folder"


def _write(f):
    os.makedirs(DIR, exist_ok=True)
    tmp = _path(f["id"]) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(f, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, _path(f["id"]))


def get(fid):
    if not re.fullmatch(r"[a-z0-9-]{1,48}", fid or ""):
        return None
    return dl.load_json(_path(fid), None) if os.path.exists(_path(fid)) else None


def list_all():
    out = []
    if os.path.isdir(DIR):
        for fn in sorted(os.listdir(DIR)):
            if fn.endswith(".json") and "." not in fn[:-5]:       # <id>.ideas.json and other side files are not folders
                f = get(fn[:-5])
                if f:
                    p = _path(f["id"])
                    out.append({"id": f["id"], "name": f["name"], "count": len(f["papers"]), "created": f.get("created", ""),
                                "updated": datetime.fromtimestamp(os.path.getmtime(p), timezone.utc).isoformat(timespec="seconds"),
                                "has": {k: os.path.exists(os.path.join(DIR, "%s.%s.json" % (f["id"], k))) for k in ("ideas", "matrix", "ask")}})   # what else lives beside the folder
    return sorted(out, key=lambda f: f["name"].lower())


def create(name):
    name = re.sub(r"\s+", " ", str(name or "")).strip()[:60]
    if not name:
        raise ValueError("a folder needs a name")
    with _lock:
        if any(f["name"].lower() == name.lower() for f in list_all()):
            raise ValueError("a folder with that name exists")
        base = fid = _slug(name)
        n = 2
        while get(fid):
            fid = "%s-%d" % (base, n); n += 1
        f = {"version": 1, "id": fid, "name": name, "created": datetime.now(timezone.utc).isoformat(timespec="seconds"), "papers": []}
        _write(f)
        return f


def _edit(fid, fn):
    with _lock:
        f = get(fid)
        if not f:
            raise KeyError("no such folder")
        fn(f)
        _write(f)
        return f


def rename(fid, name):
    name = re.sub(r"\s+", " ", str(name or "")).strip()[:60]
    if not name:
        raise ValueError("a folder needs a name")
    return _edit(fid, lambda f: f.update(name=name))


def add(fid, keys):
    return _edit(fid, lambda f: f["papers"].extend(k for k in dict.fromkeys(keys) if k and k not in f["papers"]))


def remove(fid, keys):
    return _edit(fid, lambda f: f.update(papers=[k for k in f["papers"] if k not in set(keys)]))


def delete(fid):
    with _lock:
        if not get(fid):
            raise KeyError("no such folder")
        for suffix in (".json", ".ideas.json", ".digest.md", ".ask.json", ".matrix.json", ".draft.json", ".claims.json", ".synthesis.json", ".workspace.json", ".review.json"):
            p = os.path.join(DIR, fid + suffix)
            if os.path.exists(p):
                os.remove(p)


def membership():
    m = {}
    for f in list_all():
        for k in (get(f["id"]) or {}).get("papers", []):
            m.setdefault(k, []).append(f["id"])
    return m
