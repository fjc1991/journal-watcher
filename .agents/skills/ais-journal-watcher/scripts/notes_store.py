"""Reader annotations: highlights, note cards, bookmarks and tags.

A paper's annotations live in a sidecar next to its PDF, exactly like the .md
summary, so the Papers/YYYY-MM/<Journal>/ layout never changes:

    Papers/2026-04/MIS Quarterly/<name>.pdf
    Papers/2026-04/MIS Quarterly/<name>.md          (summary, written by the pipeline)
    Papers/2026-04/MIS Quarterly/<name>.notes.json  (created on the first annotation)

Tag definitions are shared by every paper in Papers/_tags.json. Both files are
written atomically and carry a `rev`: a stale browser tab gets a Conflict (the
server answers 409 with the current copy) instead of overwriting newer notes.
"""
import hashlib
import json
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import download as dl  # noqa: E402

PAPERS, ROOT = dl.PAPERS, dl.ROOT
TAGS_PATH = os.path.join(PAPERS, "_tags.json")
MAX_BYTES = 8 * 1024 * 1024
LISTS = ("highlights", "cards", "bookmarks")  # the per-paper collections
BOARD = ("frames", "links")                   # the notes canvas: labelled frames around cards, arrows between cards (saved when present)

_lock = threading.RLock()
_sha = {}


class Conflict(Exception):
    """The caller's rev is older than what is on disk; .current holds the newer copy."""

    def __init__(self, current):
        super().__init__("stale rev")
        self.current = current


# ---------------------------------------------------------------- paths

def safe_papers_path(rel):
    """Absolute path of `rel` (relative to the project root) if it is inside Papers/, else None."""
    if not isinstance(rel, str) or "\0" in rel:
        return None
    full = os.path.realpath(os.path.join(ROOT, rel))
    return full if full.startswith(os.path.realpath(PAPERS) + os.sep) else None


def pdf_path(rel):
    """Like safe_papers_path, but only for PDFs that exist."""
    full = safe_papers_path(rel)
    return full if full and full.lower().endswith(".pdf") and os.path.isfile(full) else None


def notes_path(pdf):
    return pdf[:-4] + ".notes.json"


def chat_path(pdf):
    return pdf[:-4] + ".chat.json"


def card_path(pdf):
    return pdf[:-4] + ".card.json"


def skim_path(pdf):
    return pdf[:-4] + ".skim.json"


def sidecars(pdf):
    """Files that belong to a paper besides the PDF (move/delete them together)."""
    return [p for p in (pdf[:-4] + ".md", notes_path(pdf), chat_path(pdf), card_path(pdf), skim_path(pdf), pdf[:-4] + ".book.json", pdf[:-4] + ".epub") if os.path.exists(p)]       # (the last two: a book's own details and the EPUB it was made from: see books.py)


def sha1_of(pdf):
    st = os.stat(pdf)
    key = (pdf, st.st_mtime_ns, st.st_size)
    if _sha.get("k") != key:
        h = hashlib.sha1()
        with open(pdf, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        _sha["k"], _sha["v"] = key, h.hexdigest()
    return _sha["v"]


# ---------------------------------------------------------------- file io

def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write(path, obj):
    data = json.dumps(obj, ensure_ascii=False, indent=1)
    if len(data) > MAX_BYTES:
        raise ValueError("notes too large")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
    os.replace(tmp, path)


def empty(pdf, doc=None):
    d = {"version": 1, "rev": 0, "doc": dict(doc or {}), "highlights": [], "cards": [], "bookmarks": [], "frames": [], "links": [], "paper_note": ""}
    d["doc"]["sha1"] = sha1_of(pdf)
    return d


def load(pdf, doc=None):
    """The paper's notes, or an empty skeleton (nothing is written until the first save)."""
    with _lock:
        d = _read(notes_path(pdf))
        if not isinstance(d, dict):
            return empty(pdf, doc)
        for k in LISTS + BOARD:
            d.setdefault(k, [])
        d.setdefault("paper_note", "")
        d.setdefault("rev", 0)
        d.setdefault("doc", dict(doc or {}))
        return d


def save(pdf, notes, base_rev, doc=None):
    """Write `notes` if `base_rev` is the rev on disk; returns the new notes (rev + 1)."""
    if not isinstance(notes, dict) or any(not isinstance(notes.get(k, []), list) for k in LISTS):
        raise ValueError("bad notes")
    with _lock:
        path = notes_path(pdf)
        cur = _read(path)
        cur_rev = cur.get("rev", 0) if isinstance(cur, dict) else 0
        if cur_rev != base_rev:
            raise Conflict(load(pdf, doc))
        out = {"version": 1, "rev": cur_rev + 1, "doc": dict(notes.get("doc") or doc or {})}
        out["doc"]["sha1"] = sha1_of(pdf)
        for k in LISTS:
            out[k] = [x for x in notes.get(k, []) if isinstance(x, dict)]
        for k in BOARD:
            items = [x for x in notes.get(k) or [] if isinstance(x, dict)] if isinstance(notes.get(k), list) else []
            if items:
                out[k] = items
        if isinstance(notes.get("paper_note"), str) and notes["paper_note"].strip():
            out["paper_note"] = notes["paper_note"][:20000]
        _write(path, out)
        return out


def counts(pdf):
    """{"highlights": n, "cards": n} for the Library chip, or None if the paper has no notes."""
    d = _read(notes_path(pdf)) if os.path.exists(notes_path(pdf)) else None
    if not isinstance(d, dict):
        return None
    n = {k: len(d.get(k) or []) for k in ("highlights", "cards")}
    return n if any(n.values()) else None


# ---------------------------------------------------------------- the Ask tab's conversation (only exists once you have asked something)

def load_chat(pdf):
    d = _read(chat_path(pdf))
    return {"version": 1, "turns": d["turns"]} if isinstance(d, dict) and isinstance(d.get("turns"), list) else {"version": 1, "turns": []}


def save_chat(pdf, turns):
    if not isinstance(turns, list):
        raise ValueError("bad chat")
    turns = [t for t in turns if isinstance(t, dict)][-100:]
    with _lock:
        if not turns:
            if os.path.exists(chat_path(pdf)):
                os.remove(chat_path(pdf))
            return {"version": 1, "turns": []}
        _write(chat_path(pdf), {"version": 1, "turns": turns})
    return {"version": 1, "turns": turns}


# ---------------------------------------------------------------- tags

def load_tags():
    d = _read(TAGS_PATH)
    if not isinstance(d, dict) or not isinstance(d.get("tags"), list):
        return {"version": 1, "rev": 0, "tags": []}
    d.setdefault("rev", 0)
    return d


def save_tags(tags, base_rev):
    if not isinstance(tags, list):
        raise ValueError("bad tags")
    with _lock:
        cur = load_tags()
        if cur["rev"] != base_rev:
            raise Conflict(cur)
        out = {"version": 1, "rev": cur["rev"] + 1, "tags": [t for t in tags if isinstance(t, dict) and t.get("id")]}
        _write(TAGS_PATH, out)
        return out


def retag(old, new=None):
    """Library-wide: replace tag id `old` by `new` (merge) or drop it (new=None) in every paper's notes and in _tags.json.
    Returns how many highlights/cards changed. Close the reader first: an open tab would see its notes as changed (409)."""
    n = 0
    with _lock:
        for root, _, files in os.walk(PAPERS):
            for fn in files:
                if not fn.endswith(".notes.json"):
                    continue
                path = os.path.join(root, fn)
                d = _read(path)
                if not isinstance(d, dict):
                    continue
                hit = False
                for x in d.get("highlights", []) + d.get("cards", []):
                    t = x.get("tags") or []
                    if old in t:
                        x["tags"] = list(dict.fromkeys([new if i == old else i for i in t] if new else [i for i in t if i != old]))
                        hit = True; n += 1
                if hit:
                    d["rev"] = d.get("rev", 0) + 1
                    _write(path, d)
        cur = load_tags()
        _write(TAGS_PATH, {"version": 1, "rev": cur["rev"] + 1, "tags": [t for t in cur["tags"] if t["id"] != old]})
    return n
