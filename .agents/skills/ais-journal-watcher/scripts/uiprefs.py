"""The choices the dashboard page makes for you (theme, the reader's mode, list or grid, where you stopped reading each paper…), kept in the data folder so they follow you from one computer to
the next. The page keeps them in the browser's own storage (localStorage) for speed; static/prefsync.js copies the ones named here to and from this file.

    uiprefs.get()                      -> {"rules": {"exact": [...], "prefix": [...]}, "prefs": {key: value}}   (what the page loads before anything else)
    uiprefs.update(set={}, remove=[])  merge changes (a key at a time, so two computers changing different things do not undo each other)
    uiprefs.is_shared(key)             True for the keys that travel; anything else stays in the browser on this computer

What stays on one computer: how wide the reader's side panel is and whether the sidebar is folded (they depend on the screen). Everything is a short string, as localStorage holds it.
The file is ui_prefs.json next to the settings: {"v": 1, "prefs": {key: {"v": value, "t": seconds}}}. Reading positions are many (one per paper), so only the newest MAX_POS are kept.
"""
import json
import os
import re
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import download as dl  # noqa: E402

FILE = os.path.join(dl.BASE, "ui_prefs.json")
EXACT = ("jw-theme", "jw-reflow", "jw-rd-mode", "jw-rd-dark", "jw-rd-tab", "jw-ask-mode", "jw-askmany-mode", "jw-drop-last", "jw-skim-d", "jw-skim-off", "jw-fp-view", "jw-fp-sort", "jw-welcome")
PREFIX = ("jw-skim-on:", "jw-pos:", "jw-lsn-")
POS = "jw-pos:"
MAX_POS = 1500
MAX_KEYS = 4000
MAX_VALUE = 4000
KEY_RE = re.compile(r"^jw-[^\x00-\x1f\x7f]{1,400}$")
_lock = threading.RLock()


def is_shared(key):
    return isinstance(key, str) and bool(KEY_RE.match(key)) and (key in EXACT or key.startswith(PREFIX))


def _read():
    """(the stored {key: {"v", "t"}}, ok). A file that cannot be read is treated as empty, and ok is False so that writing keeps a copy of it first."""
    if not os.path.exists(FILE):
        return {}, True
    try:
        with open(FILE, encoding="utf-8") as f:
            d = json.load(f)
        p = d.get("prefs") if isinstance(d, dict) else None
        if not isinstance(p, dict):
            return {}, False
        return {k: v for k, v in p.items() if is_shared(k) and isinstance(v, dict) and isinstance(v.get("v"), str)}, True
    except (OSError, ValueError):
        return {}, False


def _write(prefs, ok):
    if not ok and os.path.exists(FILE):
        try:
            os.replace(FILE, FILE + ".unreadable")              # (kept, not overwritten: it may be half-synced and fine a minute later)
        except OSError:
            pass
    pos = sorted((k for k in prefs if k.startswith(POS)), key=lambda k: prefs[k].get("t", 0))
    for k in pos[:max(0, len(pos) - MAX_POS)]:
        del prefs[k]
    os.makedirs(os.path.dirname(FILE), exist_ok=True)
    tmp = dl.tmp_path(FILE)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"v": 1, "prefs": prefs}, f, ensure_ascii=False, indent=0)
    os.replace(tmp, FILE)


def get():
    with _lock:
        stored, _ = _read()
    return {"rules": {"exact": list(EXACT), "prefix": list(PREFIX)}, "prefs": {k: v["v"] for k, v in stored.items()}}


def update(set=None, remove=None):  # noqa: A002
    """Merge changes into the file. Keys that are not shared, or that are too long, are ignored; returns how many keys were changed."""
    set = set if isinstance(set, dict) else {}
    remove = remove if isinstance(remove, list) else []
    with _lock:
        stored, ok = _read()
        changed, now = 0, int(time.time())
        for k, v in set.items():
            if is_shared(k) and isinstance(v, str) and len(v) <= MAX_VALUE and (k in stored or len(stored) < MAX_KEYS):
                if stored.get(k, {}).get("v") != v:
                    stored[k] = {"v": v, "t": now}; changed += 1
        for k in remove:
            if k in stored:
                del stored[k]; changed += 1
        if changed:
            _write(stored, ok)
        return changed
