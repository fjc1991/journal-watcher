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
import atexit
import hashlib
import json
import os
import re
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import download as dl  # noqa: E402

PAPERS, ROOT = dl.PAPERS, dl.ROOT
TAGS_PATH = os.path.join(PAPERS, "_tags.json")
MAX_BYTES = 8 * 1024 * 1024
LISTS = ("highlights", "cards", "bookmarks")  # the per-paper collections
BOARD = ("frames", "links")                   # the notes canvas: labelled frames around cards, arrows between cards (saved when present)

_lock = threading.RLock()
# the content hash of every PDF this process has hashed or remembers from an earlier run: path -> (size, mtime_ns, inode, ctime_ns, sha1). An entry is used only while the file's
# size, modification time, inode and change time are exactly these; anything else is hashed again. The map is kept on THIS computer (SHA_CACHE, in paths.LOCAL), never in the shared folder.
_sha = {}
_sha_own = set()                     # the paths this process hashed itself (guarded by _sha_lock): only these override what is in the saved map now
_SHA_HEX = re.compile(r"[0-9a-f]{40}\Z")
_sha_lock = threading.Lock()         # guards _sha and _sha_state; never held while a PDF is read or the map is written (it may be taken while _lock is held, never the other way round)
_sha_io = threading.Lock()           # one writer of SHA_CACHE at a time in this process
_sha_state = {"loaded": False, "dirty": False, "timer": None, "last": None}       # last: (path, signature, sha1) of the PDF hashed last, for sha1_known only
SHA_CACHE = os.path.join(dl.LOCAL, "pdf_sha1.json")
SHA_SAVE_S = 5.0                     # the map is written this long after a new hash (one write for a whole pass over the library)
SHA_YOUNG_NS = 2_000_000_000         # a file changed less than 2 s ago is hashed but not remembered: a second change inside the file system's time step could not be told apart
                                     # ("changed": the later of its modification time and its change time)


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
    return [p for p in (pdf[:-4] + ".md", notes_path(pdf), chat_path(pdf), card_path(pdf), skim_path(pdf), pdf[:-4] + ".book.json", pdf[:-4] + ".epub", pdf[:-4] + ".paper.json") if os.path.exists(p)]       # (the last two: a book's own details and the EPUB it was made from: see books.py)


def _sha_sig(st):
    """What tells that a file is still the one that was hashed. The change time is in it because a file rewritten in place with the same length can be given its old
    modification time back (a sync client, a restore), but never its old change time."""
    return (st.st_size, st.st_mtime_ns, st.st_ino, st.st_ctime_ns)


def _sha_fits(pdf, entry):
    try:
        return _sha_sig(os.stat(pdf)) == entry[:4]
    except OSError:
        return False


def _sha_file(pdf):
    h = hashlib.sha1()
    with open(pdf, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha_read():
    """What SHA_CACHE holds, as {path: (size, mtime_ns, inode, ctime_ns, sha1)}; {} when it is missing, damaged in any way or from another version (version 1 had no
    change time: its files are hashed again once). Never raises."""
    try:
        with open(SHA_CACHE, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("v") != 2:
            return {}
        return {p: (int(e[0]), int(e[1]), int(e[2]), int(e[3]), e[4]) for p, e in d["files"].items()
                if isinstance(e, list) and len(e) == 5 and isinstance(e[4], str) and _SHA_HEX.match(e[4])}
    except Exception:  # noqa: BLE001        (a number too large, a file nested too deep, anything: a damaged map is an empty one)
        return {}


def _sha_hit(pdf, sig):
    """The remembered hash of this file when its signature is still `sig`, else None. The saved map is read the first time (a small file; no PDF is opened)."""
    with _sha_lock:
        if not _sha_state["loaded"]:
            _sha_state["loaded"] = True
            for p, e in _sha_read().items():
                _sha.setdefault(p, e)
        hit = _sha.get(pdf)
        if hit and hit[:4] == sig:
            _sha_own.add(pdf)            # (confirmed against the file as it is now)
            return hit[4]
        return None


def sha_flush():
    """Write the map to SHA_CACHE now (it is written by itself SHA_SAVE_S after a new hash, and not at all when nothing new was hashed). What another process wrote meanwhile
    is kept, also for a file this process only knows from the map it read at its start (what it hashed or confirmed itself wins); files that are gone are dropped."""
    with _sha_lock:
        t, _sha_state["timer"] = _sha_state["timer"], None
        if t:
            t.cancel()                   # (written now: the write that was waiting is not needed)
        if not _sha_state["dirty"]:
            return
        _sha_state["dirty"] = False
        mine = dict(_sha)
        own = set(_sha_own)
    with _sha_io:
        merged = dict(mine)
        for p, e in _sha_read().items():         # what is saved now wins, except over an entry this process hashed or confirmed itself that still fits the file
            if p not in mine or (mine[p] != e and not (p in own and _sha_fits(p, mine[p]))):
                merged[p] = e
        files = {p: list(e) for p, e in merged.items() if os.path.exists(p)}
        if not files and not os.path.exists(SHA_CACHE):
            return                       # (nothing to keep and nothing to correct: no folder is made for an empty map)
        try:
            os.makedirs(os.path.dirname(SHA_CACHE), exist_ok=True)
            tmp = dl.tmp_path(SHA_CACHE)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"v": 2, "files": files}, f, ensure_ascii=False)
            os.replace(tmp, SHA_CACHE)
        except OSError:                  # (a read-only or full disk: the hashes are simply worked out again next time)
            pass


def sha_forget():
    """Forget what is in memory, as after a restart (the saved map is read again at the next sha1_of). For tests."""
    with _sha_lock:
        _sha.clear()
        _sha_own.clear()
        _sha_state["loaded"], _sha_state["last"] = False, None


def sha1_of(pdf):
    """The SHA-1 of the file's content. Read from the file only when its size, modification time, change time or inode differ from the last time it was hashed (here, or in an earlier run)."""
    st = os.stat(pdf)
    sig = _sha_sig(st)
    hit = _sha_hit(pdf, sig)
    if hit:
        return hit
    sha = _sha_file(pdf)
    _sha_state["last"] = (pdf, sig, sha)                # (one store of one object; read only by sha1_known, never to answer sha1_of)
    try:
        same = _sha_sig(os.stat(pdf)) == sig            # (changed while it was read: the hash is of a mixture, so it is not remembered)
    except OSError:
        same = False
    if not same or time.time_ns() - max(st.st_mtime_ns, st.st_ctime_ns) < SHA_YOUNG_NS:
        return sha
    with _sha_lock:
        _sha[pdf] = sig + (sha,)
        _sha_own.add(pdf)
        _sha_state["dirty"] = True
        if _sha_state["timer"] is None:
            t = threading.Timer(SHA_SAVE_S, sha_flush)
            t.daemon = True
            _sha_state["timer"] = t
            t.start()
    return sha


atexit.register(sha_flush)           # (a script that ends normally keeps what it hashed; the Mac app leaves with os._exit, hence the timer)


def sha1_known(pdf):
    """The hash of this PDF when it is remembered (or is the one hashed last, however new the file) and the file has not changed since, else None. The file is never
    opened (ai.pdf_chars: measuring a call that has just been cancelled must not read the PDF again)."""
    try:
        sig = _sha_sig(os.stat(pdf))
    except (OSError, TypeError):
        return None
    last = _sha_state["last"]            # (the triple, read once)
    return _sha_hit(pdf, sig) or (last[2] if last and last[:2] == (pdf, sig) else None)


# ---------------------------------------------------------------- file io

def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


class Unreadable(ValueError):
    """A notes file is there but could not be read (still syncing, locked, damaged): nothing is written over it."""


UNREADABLE = ("%s could not be read (the file may still be syncing, or it is damaged), so nothing is saved over it. Close and open the paper again in a minute. "
              "A copy of the file as it is now was kept beside it (%s).")


def _state(path):
    """("missing" | "ok" | "bad", data): a file that is there but cannot be read or is not JSON is "bad", never "missing"."""
    if not os.path.lexists(path):
        return "missing", None
    try:
        with open(path, encoding="utf-8") as f:
            return "ok", json.load(f)
    except (OSError, ValueError):
        return "bad", None


def _set_aside(path):
    """Keep a copy of an unreadable file (once per state of the file); returns the copy's name."""
    import shutil
    try:
        st = os.stat(path)
        dest = "%s.unreadable-%d.json" % (path[:-5] if path.endswith(".json") else path, int(st.st_mtime))
        if not os.path.exists(dest):
            shutil.copy2(path, dest)
        return os.path.basename(dest)
    except OSError:
        return "no copy could be made"


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
        state, d = _state(notes_path(pdf))
        if not isinstance(d, dict):
            e = empty(pdf, doc)
            if state == "bad" or (state == "ok" and d is not None):          # (there, but not notes that can be read: said on the page, and never saved over)
                e["unreadable"] = True
            return e
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
        state, cur = _state(path)
        if state == "bad" or (state == "ok" and not isinstance(cur, dict)):
            raise Unreadable(UNREADABLE % ("This paper's notes file", _set_aside(path)))
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


def counts(pdf, refused=None):
    """{"highlights": n, "cards": n} for the Library chip, or None if the paper has no notes. refused: what to answer instead of None when the file is there but the
    system would not let it be read (the library list does not remember such an answer)."""
    if not os.path.exists(notes_path(pdf)):
        return None
    try:
        with open(notes_path(pdf), encoding="utf-8") as f:
            d = json.load(f)
    except OSError:
        return refused
    except ValueError:
        return None
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
        state, raw = _state(TAGS_PATH)
        if state == "bad" or (state == "ok" and not (isinstance(raw, dict) and isinstance(raw.get("tags"), list))):
            raise Unreadable(UNREADABLE % ("Your tags file", _set_aside(TAGS_PATH)))
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
        state, raw = _state(TAGS_PATH)
        if state == "bad" or (state == "ok" and not (isinstance(raw, dict) and isinstance(raw.get("tags"), list))):
            raise Unreadable(UNREADABLE % ("Your tags file", _set_aside(TAGS_PATH)))          # (before any paper's notes are changed)
        for root, _, files in os.walk(PAPERS):
            for fn in files:
                if not fn.endswith(".notes.json") or ".unreadable-" in fn:
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
