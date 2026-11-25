"""Virtual folders: named groups of papers. A folder is a small list file, Papers/_folders/<id>.json; the PDFs never move and a
paper can be in several folders. A folder can be inside another ("parent"); it then shows its own papers and those of the folders under it. Papers are identified by download.index_key (DOI, else title), so a re-download keeps them.

    folders.list_all()                  # [{id, name, count, created}]
    folders.create("Platform governance") -> folder
    folders.add(id, [keys]) / remove(id, [keys]) / rename(id, name) / delete(id)
    folders.membership()                # {paper key: [folder id, ...]} for the Library
Later features write next to these files: <id>.ideas.json (Research Ideas Lab), <id>.digest.md.
"""
import hashlib
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


# ---------------------------------------------------------------- folders inside folders
# A folder may name a parent ("parent": the id of another folder). The files stay side by side in Papers/_folders (nothing else has to learn a new layout); a folder's
# OWN papers are the keys in its file, and what a folder SHOWS is its own papers and those of every folder under it, each paper once.

def _read_all():
    """Every folder file, read once: {id: folder}."""
    out = {}
    if os.path.isdir(DIR):
        for fn in sorted(os.listdir(DIR)):
            if fn.endswith(".json") and "." not in fn[:-5]:       # <id>.ideas.json and other side files are not folders
                f = get(fn[:-5])
                if isinstance(f, dict) and isinstance(f.get("papers"), list) and f.get("id") == fn[:-5]:
                    out[f["id"]] = f
    return out


def _parents(every):
    """{id: parent id or ""}. A parent that is not there (deleted, or not yet synced from the other computer), is the folder itself, or lies on a chain that comes back
    to the folder (two computers each moved one folder into the other) counts as none: such a folder is shown at the top, never hidden."""
    raw = {i: f.get("parent") if isinstance(f.get("parent"), str) and f.get("parent") in every and f.get("parent") != i else "" for i, f in every.items()}
    out = {}
    for i in raw:
        seen, p = {i}, raw[i]
        while p and p not in seen:
            seen.add(p)
            p = raw[p]
        out[i] = "" if p else raw[i]                      # (p is non-empty only when the chain looped)
    return out


def _children(parents):
    kids = {}
    for i, p in parents.items():
        kids.setdefault(p, []).append(i)
    return kids


def _below(fid, kids):
    """The ids of every folder under fid, in the order of a tree: a folder, then what is inside it, then the next."""
    out, todo = [], list(kids.get(fid, []))
    while todo:
        i = todo.pop(0)
        if i not in out and i != fid:
            out.append(i)
            todo = list(kids.get(i, [])) + todo
    return out


def family(fid):
    """[fid] and every folder under it ([] when there is no such folder): what a filter by this folder covers."""
    every = _read_all()
    if fid not in every:
        return []
    return [fid] + _below(fid, _children(_parents(every)))


def all_papers(fid):
    """The keys of the papers a folder shows: its own and those of every folder under it, each once, in that order. None when there is no such folder."""
    every = _read_all()
    if fid not in every:
        return None
    return list(dict.fromkeys(k for i in [fid] + _below(fid, _children(_parents(every))) for k in every[i]["papers"]))


def path(fid):
    """The names from the top folder down to this one: ["WITS26", "Session 1B"]."""
    every = _read_all()
    parents, out = _parents(every), []
    while fid in every and len(out) < 50:
        out.insert(0, every[fid]["name"])
        fid = parents[fid]
    return out


def list_all():
    """Every folder, by name: {id, name, parent ("" at the top), path (the names from the top down), depth, children (how many folders directly inside), own (papers in
    the folder itself), count (papers it shows: its own and those of the folders under it, each once), created, updated, has}."""
    every = _read_all()
    parents = _parents(every)
    kids = _children(parents)
    out = []
    for i, f in every.items():
        p = _path(i)
        names, at = [], i
        while at and len(names) < 50:
            names.insert(0, every[at]["name"])
            at = parents[at]
        below = _below(i, kids)
        out.append({"id": i, "name": f["name"], "parent": parents[i], "path": names, "depth": len(names) - 1, "children": len(kids.get(i, [])), "own": len(f["papers"]),
                    "count": len(set(f["papers"]).union(*(every[b]["papers"] for b in below))) if below else len(f["papers"]), "created": f.get("created", ""),
                    "updated": datetime.fromtimestamp(os.path.getmtime(p), timezone.utc).isoformat(timespec="seconds"),
                    "has": {k: os.path.exists(os.path.join(DIR, "%s.%s.json" % (i, k))) for k in ("ideas", "matrix", "ask")}})   # what else lives beside the folder
    return sorted(out, key=lambda f: f["name"].lower())


def _clean_name(name):
    return re.sub(r"\s+", " ", str(name or "")).strip()[:60]


def _siblings(parent, every=None):
    every = _read_all() if every is None else every
    parents = _parents(every)
    return [every[i] for i in every if parents[i] == (parent or "")]


def create(name, parent=None):
    """A new folder, at the top or inside `parent`. A name is taken once among the folders of the same place (two masters may each have a "Session 1"). The id of a
    folder inside another is made from its name AND its parent, the same on every computer: two computers that each make "Session 1" under different masters get two
    files, which a sync cannot mistake for one folder."""
    name = _clean_name(name)
    if not name:
        raise ValueError("a folder needs a name")
    parent = str(parent or "")
    with _lock:
        every = _read_all()
        if parent and parent not in every:
            raise ValueError("the folder to put it in is not there")
        if any(f["name"].lower() == name.lower() for f in _siblings(parent, every)):
            raise ValueError("a folder with that name exists")
        base = fid = _slug(name) if not parent else "%s-%s" % (_slug(name)[:30].strip("-") or "folder", hashlib.sha1(parent.encode()).hexdigest()[:6])
        n = 2
        while get(fid):
            fid = "%s-%d" % (base, n); n += 1
        f = {"version": 1, "id": fid, "name": name, "created": datetime.now(timezone.utc).isoformat(timespec="seconds"), "papers": []}
        if parent:
            f["parent"] = parent
        _write(f)
        return f


def ensure(parent, names):
    """The folder at the end of a chain of names under `parent` ("" = the top), made where it is not there yet: ensure("wits26", ["Day 1", "Session 1B"]). Returns
    (its id, the ids made now, outermost first). A name is compared as it is kept (cut at 60 characters, case aside)."""
    made, at = [], str(parent or "")
    with _lock:
        for name in names:
            name = _clean_name(name)
            if not name:
                continue
            have = next((f for f in _siblings(at) if f["name"].lower() == name.lower()), None)
            if not have:
                have = create(name, at)
                made.append(have["id"])
            at = have["id"]
    return at, made


def move(fid, parent):
    """Put a folder inside another ("" = at the top). Refused into itself or into a folder under it, and where a folder of that name is already there."""
    parent = str(parent or "")
    with _lock:
        every = _read_all()
        if fid not in every:
            raise KeyError("no such folder")
        if parent and parent not in every:
            raise ValueError("the folder to put it in is not there")
        if parent == fid or parent in _below(fid, _children(_parents(every))):
            raise ValueError("a folder cannot be put inside itself")
        if any(f["id"] != fid and f["name"].lower() == every[fid]["name"].lower() for f in _siblings(parent, every)):
            raise ValueError("a folder with that name exists")
        return _edit(fid, lambda f: f.update(parent=parent) if parent else f.pop("parent", None))


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


def delete(fid, children="lift"):
    """Delete a folder and what was made in it (its board, matrix, conversation…); the papers stay. The folders inside it move up one level ("lift": nothing of theirs
    is lost), or are deleted with it ("delete": each with its own board, matrix and conversation)."""
    with _lock:
        every = _read_all()
        if fid not in every:
            raise KeyError("no such folder")
        parents = _parents(every)
        kids = _children(parents)
        if children == "delete":
            for sub in reversed(_below(fid, kids)):
                _remove_files(sub)
        else:
            for sub in kids.get(fid, []):
                up = parents[fid]
                _edit(sub, lambda f: f.update(parent=up) if up else f.pop("parent", None))
        _remove_files(fid)


def _remove_files(fid):
    if True:
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
