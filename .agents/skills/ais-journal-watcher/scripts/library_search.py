"""Search the library: title, authors, journal, topics, the summary the pipeline wrote, *and what you wrote yourself*
(highlights, comments, note cards, one-line takeaways). Plus the highlights hub (every highlight across all papers).

Works on the rows of pipeline.library(). In memory with a cache by each file's times and size — plenty fast for thousands of papers; if the library
ever outgrows that, swap the scan for SQLite FTS5 without changing the callers.

    library_search.search(rows, q="platform", status="reading", folder="gov", tag=<id>, notes=True, sort="opened")
        -> [{"file", "score", "snippets": [{"kind": "highlight"|"comment"|"summary"|"takeaway", "page", "text"}]}]
    library_search.highlights(rows, q="", tag=None, color=None, folder=None, journal=None, limit=300)
        -> [{file, title, abbr, journal, year, id, page, y, text, comment, color, tags, created}]
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import download as dl  # noqa: E402
import notes_store as ns  # noqa: E402

ROOT = dl.ROOT
_cache = {}     # path -> ((mtime_ns, size, ctime_ns), what the loader read, {name: (made from, made)}): what is worked out from a file (_kept) lives and dies with that reading of it
STATS = {"read": 0, "made": 0}      # files read, and things worked out from one: a search that changes nothing on disk adds to neither (the tests count on it)


def _cached(path, loader):
    try:
        st = os.stat(path)
    except OSError:
        return None
    sig = (st.st_mtime_ns, st.st_size, st.st_ctime_ns)       # (the size and the change time too: a file written again with its old modification time is still read again)
    hit = _cache.get(path)
    if hit and hit[0] == sig:
        return hit[1]
    val = loader(path)
    STATS["read"] += 1
    _cache[path] = (sig, val, {})
    return val


def _kept(path, name, src, fn):
    """fn(src), worked out once and kept beside the cached reading of `path` for as long as `src` is the very object it was made from. When the file changes, _cached puts a new
    entry in its place and this goes with it. It is never the answer for another text: a `src` that is not the one kept (the file was read again in between, a test's stand-in
    for summary_of) is worked out afresh, and nothing is kept for a path that was never read."""
    hit = _cache.get(path)
    if hit is None:
        return fn(src)
    kept = hit[2].get(name)
    if kept is not None and kept[0] is src:
        return kept[1]
    out = fn(src)
    STATS["made"] += 1
    hit[2][name] = (src, out)
    return out


def _json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _text(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def notes_of(row):
    return _cached(ns.notes_path(os.path.join(ROOT, row["file"])), _json) if row.get("notes") else None


def summary_of(row):
    return (_cached(os.path.join(ROOT, row["summary"]), _text) or "") if row.get("summary") else ""


def card_text(row):
    """What the paper's card says (topics, theories, method, limitations…), '' if it has none. Lets a search for "fixed effects" find the paper."""
    if not row.get("card"):
        return ""
    path = ns.card_path(os.path.join(ROOT, row["file"]))
    card = _cached(path, _json)
    if not isinstance(card, dict):
        return ""
    import papercard
    return _kept(path, "text", card, papercard.text_of)


def _tokens(q):
    toks = [m[0] or m[1] for m in re.findall(r'"([^"]+)"|(\S+)', q.lower())]
    return [t for t in toks if len(t) >= 2]


def _lower(text):
    return text.lower()


def _plain(text):
    plain = re.sub(r"(?m)^#+\s*", "", text.replace("**", "").replace("`", ""))     # snippets come from markdown summaries
    return text if plain == text else plain                                        # (the same object when nothing was taken out: a card's text is then kept once, not twice)


def _window(text, tok, n=70, plain=None):
    """The words around `tok`. plain: (_plain(text), its lower()) when the caller has them already (_snippet)."""
    text, low = plain or (_plain(text), None)
    i = (text.lower() if low is None else low).find(tok)
    if i < 0:
        return None
    a, b = max(0, i - n), min(len(text), i + len(tok) + n)
    return ("…" if a else "") + re.sub(r"\s+", " ", text[a:b]).strip() + ("…" if b < len(text) else "")


def _snippet(path, text, low, tok):
    """_window(text, tok) for a summary or a card's text (`low`: text.lower()), with the text without its markdown, and that in lower case, kept beside the file."""
    plain = _kept(path, "plain", text, _plain)
    return _window(text, tok, plain=(plain, low if plain is text else _kept(path, "plain_low", plain, _lower)))


NO_FOLDER = "~none"


def search(rows, q="", status="", star=False, folder="", journal="", year="", tag="", notes=False, sort="", kind=""):
    toks = _tokens(q or "")
    out = []
    for r in rows:
        st = r.get("state") or {}
        if status == "none" and st.get("status"):
            continue
        if status in ("toread", "reading", "read") and st.get("status") != status:
            continue
        if star and not st.get("star"):
            continue
        if folder == NO_FOLDER:                                    # papers in no folder yet (to file them: select them all, Add to folder)
            if r.get("folders"):
                continue
        elif folder and folder not in (r.get("folders") or []):
            continue
        if journal and r.get("abbr") != journal:
            continue
        if year and r.get("year") != year:
            continue
        if kind and (r.get("kind") or "journal") != kind:          # (kinds.py: journal | preprint | conference | other)
            continue
        if notes and not r.get("notes"):
            continue
        nt = notes_of(r) if (tag or toks) else None
        if tag and not nt:
            continue
        if tag and not any(tag in (x.get("tags") or []) for x in (nt["highlights"] + nt["cards"])):
            continue
        score, snips = 0, []
        if toks:
            # every text is put in lower case once per paper, not once per word; the two long ones (the summary, the card's text) once per reading of their file (_kept)
            fields = [(name, w, text, text.lower(), None) for name, w, text in (
                ("title", 6, r.get("title", "")), ("authors", 4, r.get("authors", "")),
                ("meta", 2, " ".join([r.get("journal", ""), r.get("abbr", ""), r.get("matched_topics", "")])), ("takeaway", 3, st.get("takeaway", "")))]
            for name, text, path in (("summary", summary_of(r), os.path.join(ROOT, r.get("summary") or "")), ("card", card_text(r), ns.card_path(os.path.join(ROOT, r["file"])))):
                fields.append((name, 1, text, _kept(path, "low", text, _lower) if text else text.lower(), path))
            hl = [("highlight", h.get("text", ""), h) for h in (nt or {}).get("highlights", [])]
            cm = [("comment", h.get("comment", ""), h) for h in (nt or {}).get("highlights", []) if h.get("comment")]
            cm += [("comment", (c.get("text", "") if c.get("type") == "note" else c.get("note", "")), c) for c in (nt or {}).get("cards", [])]
            mine = [(sk, text, text.lower(), obj) for sk, text, obj in hl + cm]    # (sk: not `kind`, the filter above)
            ok = True
            for t in toks:
                found = False
                for name, w, text, low, path in fields:
                    c = low.count(t)
                    if c:
                        found = True; score += w * min(c, 3)
                        if name in ("takeaway", "summary", "card") and len(snips) < 3:
                            snips.append({"kind": name, "page": None, "text": _snippet(path, text, low, t) if path else _window(text, t)})
                for sk, text, low, obj in mine:
                    if t in low:
                        found = True; score += 3
                        if len(snips) < 3:
                            pg = (obj.get("parts") or [{}])[0].get("page") if sk == "highlight" or obj.get("parts") else None
                            snips.append({"kind": sk, "page": pg, "text": _window(text, t)})
                ok = ok and found
            if not ok:
                continue
        out.append({"file": r["file"], "score": score, "snippets": snips, "_r": r})
    if toks and sort in ("", "relevance"):
        out.sort(key=lambda x: -x["score"])
    elif sort == "title":
        out.sort(key=lambda x: x["_r"].get("title", "").lower())
    elif sort == "journal":
        out.sort(key=lambda x: (x["_r"].get("journal", ""), x["_r"].get("year", "")))
    elif sort == "added":                                       # the date the paper came into the library, newest first (a run's papers together)
        out.sort(key=lambda x: (x["_r"].get("date_checked") or ""), reverse=True)
    elif sort == "opened":
        out.sort(key=lambda x: (x["_r"].get("state") or {}).get("opened", ""), reverse=True)
    elif sort == "notes":
        out.sort(key=lambda x: -((x["_r"].get("notes") or {}).get("highlights", 0)))
    for x in out:
        x.pop("_r")
    return out


def highlights(rows, q="", tag=None, color=None, folder=None, journal=None, limit=300):
    toks = _tokens(q or "")
    items = []
    for r in rows:
        if folder and folder not in (r.get("folders") or []):
            continue
        if journal and r.get("abbr") != journal:
            continue
        nt = notes_of(r)
        if not nt:
            continue
        for h in nt.get("highlights", []):
            if color and h.get("color") != color:
                continue
            if tag and tag not in (h.get("tags") or []):
                continue
            blob = (h.get("text", "") + " " + h.get("comment", "")).lower()
            if toks and not all(t in blob for t in toks):
                continue
            pt = (h.get("parts") or [{}])[0]
            rect = (pt.get("rects") or [[0, 0]])[0]
            items.append({"file": r["file"], "title": r.get("title", ""), "abbr": r.get("abbr", ""), "journal": r.get("journal", ""),
                          "year": r.get("year", ""), "id": h.get("id"), "kind": h.get("kind", "highlight"), "page": pt.get("page"), "y": rect[1], "rects": pt.get("rects") or [], "text": h.get("text", ""),
                          "comment": h.get("comment", ""), "color": h.get("color"), "tags": h.get("tags", []), "created": h.get("created", "")})
    items.sort(key=lambda x: x["created"], reverse=True)
    return {"n": len(items), "items": items[:limit]}


def tag_usage(rows):
    """{tag id: number of highlights + cards carrying it} across the whole library."""
    use = {}
    for r in rows:
        nt = notes_of(r)
        for x in ((nt or {}).get("highlights", []) + (nt or {}).get("cards", [])):
            for t in x.get("tags") or []:
                use[t] = use.get(t, 0) + 1
    return use
