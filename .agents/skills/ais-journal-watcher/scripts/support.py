"""The "About the author" page and the now-and-then reminder that the app is free and its author takes tips.

What the author wants to say lives in dashboard/static/author.json (name, role, university, a short text, links, the tip page); `author()` reads it and keeps
only what is safe to show (https links, a photo that exists). Nothing is shown for a field that is empty, and with no tip page there is no reminder at all.

When to remind: only the app on this computer counts, and nothing leaves it. <settings folder>/support.json keeps the day of the first start, how many separate
visits there have been (a visit is the page being opened or brought back after 30 minutes away) and how often the reminder was shown. It is due once there have
been 14 days and 5 visits since the first start, then at most twice more, 60 days apart; "Don't ask again" and a click on the tip link end it for good, and the
switch in Settings → About does the same. The page decides whether now is a good moment (not while you read or while the AI works) and says "shown" when it appears.
"""
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402

FILE = os.path.join(paths.BASE, "support.json")
AUTHOR = os.path.join(paths.STATIC, "author.json")
MIN_DAYS, MIN_VISITS = 14, 5        # since the first start
GAP = 30 * 60                       # away this long = the next look is a new visit
MAX_SHOWN, REPEAT_DAYS = 3, 60      # the reminder appears at most three times, 60 days apart
DAY = 86400
_lock = threading.RLock()


def _read(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else default
    except (OSError, ValueError):
        return default


def _write(path, d):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, path)


def _https(u):
    u = str(u or "").strip()
    return u if u.startswith("https://") and " " not in u and len(u) <= 300 else ""


def _text(v, n):
    return " ".join(str(v or "").split())[:n]


def author():
    """The author's page as the dashboard may show it: text trimmed, links only when they are https (or a mailto address), a photo only when its file is there."""
    d = _read(AUTHOR, {})
    links = []
    for x in (d.get("links") if isinstance(d.get("links"), list) else [])[:8]:
        if not isinstance(x, dict):
            continue
        url, label = str(x.get("url") or "").strip(), _text(x.get("label"), 40)
        if label and (_https(url) or (url.startswith("mailto:") and "@" in url and " " not in url and len(url) <= 120)):
            links.append({"label": label, "url": url})
    photo = os.path.basename(str(d.get("photo") or ""))
    if not (photo and os.path.isfile(os.path.join(paths.STATIC, photo))):
        photo = ""
    sup = d.get("support") if isinstance(d.get("support"), dict) else {}
    bio = d.get("bio") if isinstance(d.get("bio"), list) else []
    return {"name": _text(d.get("name"), 80), "role": _text(d.get("role"), 120), "university": _text(d.get("university"), 120), "university_url": _https(d.get("university_url")),
            "photo": photo, "bio": [_text(p, 900) for p in bio[:6] if _text(p, 900)], "links": links,
            "support": {"service": _text(sup.get("service"), 30) or "Ko-fi", "url": _https(sup.get("url"))}, "repo": _https(d.get("repo"))}


def _state():
    d = _read(FILE, {})
    return {"first": int(d.get("first") or 0), "last": int(d.get("last") or 0), "visits": int(d.get("visits") or 0), "shown": int(d.get("shown") or 0),
            "last_shown": int(d.get("last_shown") or 0), "optout": bool(d.get("optout")), "supported": bool(d.get("supported"))}


def visit(now=None):
    """The page was opened or came back to the front: counts as a new visit when the last look is 30 minutes or more ago (the first look also sets the start)."""
    now = int(now if now is not None else time.time())
    with _lock:
        s = _state()
        if not s["first"]:
            s["first"] = now
        if not s["last"] or now - s["last"] >= GAP:
            s["visits"] += 1
        s["last"] = now
        _write(FILE, s)
    return s


def due(s, a, now):
    """Whether the reminder may be shown now, from the saved counts and the author's page."""
    if not a["support"]["url"] or s["optout"] or s["supported"] or not s["first"] or s["shown"] >= MAX_SHOWN:
        return False
    if now - s["first"] < MIN_DAYS * DAY or s["visits"] < MIN_VISITS:
        return False
    return not s["shown"] or now - s["last_shown"] >= REPEAT_DAYS * DAY


def public(now=None):
    now = int(now if now is not None else time.time())
    a, s = author(), _state()
    return {"author": a, "due": due(s, a, now), "optout": s["optout"], "supported": s["supported"], "visits": s["visits"], "since": s["first"], "shown": s["shown"],
            "delay": int(os.environ.get("JW_SUPPORT_DELAY") or 45)}


def act(action, value=None, now=None):
    """shown (the reminder appeared) | never | supported (the tip link was pressed) | reminders (value: True to be reminded again, False to stop)."""
    now = int(now if now is not None else time.time())
    with _lock:
        s = _state()
        if action == "shown":
            s["shown"] += 1
            s["last_shown"] = now
        elif action == "never":
            s["optout"] = True
        elif action == "supported":
            s["supported"] = True
        elif action == "reminders":
            s["optout"] = not bool(value)
            if value:
                s["supported"] = False          # "remind me again" also lifts the end that a click on the tip link made
        else:
            raise ValueError("unknown action")
        if not s["first"]:
            s["first"] = now
        _write(FILE, s)
    return public(now)
