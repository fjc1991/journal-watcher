"""Saved searches (smart folders): a Library search plus its filters under a name, re-run each time you open it. Papers/_smart.json (yours; the papers are not copied or moved).

    smart.load() -> [{id, name, filters}]      filters: q, status, star, folder, journal, year, tag, notes
    smart.add(name, filters) / smart.rename(id, name) / smart.delete(id)
"""
import json
import os
import re
import sys
import threading
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import download as dl  # noqa: E402

PATH = os.path.join(dl.PAPERS, "_smart.json")
_lock = threading.RLock()
STRS = ("q", "status", "folder", "journal", "year", "tag")
BOOLS = ("star", "notes")


def clean_filters(f):
    f = f if isinstance(f, dict) else {}
    out = {k: str(f.get(k) or "").strip()[:200] for k in STRS if str(f.get(k) or "").strip()}
    out.update({k: True for k in BOOLS if f.get(k)})
    return out


def load():
    d = dl.load_json(PATH, {}) if os.path.exists(PATH) else {}
    return [s for s in (d.get("searches") if isinstance(d, dict) else []) or [] if isinstance(s, dict) and s.get("id") and s.get("name")]


def _save(items):
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    if not items:
        if os.path.exists(PATH):
            os.remove(PATH)
        return
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "searches": items}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PATH)


def add(name, filters):
    name = re.sub(r"\s+", " ", str(name or "")).strip()[:60]
    flt = clean_filters(filters)
    if not name:
        raise ValueError("Give the search a name.")
    if not flt:
        raise ValueError("There is nothing to save: set a search or a filter first.")
    with _lock:
        items = load()
        if any(s["name"].lower() == name.lower() for s in items):
            raise ValueError("A saved search with that name exists.")
        items.append({"id": uuid.uuid4().hex[:8], "name": name, "filters": flt})
        _save(items)
        return items


def rename(sid, name):
    name = re.sub(r"\s+", " ", str(name or "")).strip()[:60]
    if not name:
        raise ValueError("Give the search a name.")
    with _lock:
        items = load()
        s = next((x for x in items if x["id"] == sid), None)
        if not s:
            raise KeyError("no such saved search")
        s["name"] = name
        _save(items)
        return items


def delete(sid):
    with _lock:
        items = [s for s in load() if s["id"] != sid]
        _save(items)
        return items
