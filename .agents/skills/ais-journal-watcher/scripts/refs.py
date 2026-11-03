"""A paper's bibliography, parsed from its own text, so a citation in the text ([14], Rai et al., 2019) can show its entry.

    refs.parse(pdf_path)          -> [{i, n, text, doi, year, surname, page, rect}]  (page 1-based, rect = [x,y,w,h] fractions of the page)
    refs.match_library(refs, rows) adds "in_library": the Papers/… path of a paper you already have, else None
    refs.cite_info(doi)           -> {title, year, venue, citations, tldr} from Semantic Scholar (cached; {} when unavailable)

Reference blocks come from blocks.py (kind == "ref"). Numbered lists ("[1] …" or "1. …") are split into entries; otherwise each block is one entry.
"""
import json
import os
import re
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blocks as bl  # noqa: E402
import download as dl  # noqa: E402
import notes_store as ns  # noqa: E402

DOI = re.compile(r"(?:doi\.org/|doi:\s*)?(10\.\d{4,9}/[^\s\"<>,;]+)", re.I)
HEAD = re.compile(r"^(?:\d+[.)]?\s*)?(?:references|bibliography|works cited|literature cited)\s+", re.I)
L = r"[^\W\d_]"      # a letter


def fold(s):
    return re.sub(r"[^a-z]", "", unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower())


def _numbered(text):
    """[(n, entry text)] when the block is a numbered list ([1] … [2] … or 1. … 2. …), else None."""
    marks = [(m.start(), m.end(), int(m.group(1))) for m in re.finditer(r"(?:^|(?<=\s))\[(\d{1,3})\]\s*", text)]
    if len(marks) < 2:
        marks = [(m.start(), m.end(), int(m.group(1))) for m in re.finditer(r"(?:^|(?<=\s))(\d{1,3})\.\s+(?=[A-Z])", text)]
        ns_ = [m[2] for m in marks]
        if len(marks) < 3 or ns_ != list(range(ns_[0], ns_[0] + len(ns_))):
            return None
    out = []
    for k, (a, b, n) in enumerate(marks):
        out.append((n, text[b:marks[k + 1][0] if k + 1 < len(marks) else len(text)].strip()))
    return out


def _entry(i, n, text, b):
    text = re.sub(r"\s+", " ", text).strip()
    m = DOI.search(text)
    doi = m.group(1).rstrip(".)") if m else ""
    ym = re.search(r"\(((?:19|20)\d{2}[a-z]?)\)", text) or re.search(r"\b((?:19|20)\d{2}[a-z]?)\b", text)
    sm = re.match(r"^((?:(?:van|von|de|der|den|del|da|di|le|la|dos|ten|ter)\s+){0,2}[^\W\d_][^\W\d_'’\-]*(?:['’\-][^\W\d_]+)*),\s+[A-Z]", text) \
        or re.match(r"^(?:[A-Z]\.\s*)+((?:(?:van|von|de|der|den|del|da|di|le|la|dos)\s+)?[^\W\d_][^\W\d_'’\-]*)", text) \
        or re.match(r"^([^\W\d_]+)", text)
    W, H = b.get("page_w") or 1, b.get("page_h") or 1
    x0, y0, x1, y1 = b["bbox"]
    return {"i": i, "n": n, "text": text[:700], "doi": doi, "year": ym.group(1) if ym else "", "surname": sm.group(1) if sm else "",
            "page": b["page"] + 1, "rect": [round(x0 / W, 4), round(y0 / H, 4), round((x1 - x0) / W, 4), round((y1 - y0) / H, 4)]}


# where one reference ends and the next begins: after a full stop / closing bracket / DOI digit comes an author list —
# "Rai, A., …" (APA, Chicago) or "Bender EM, Gebru T (2021)" (INFORMS, Vancouver). Commas inside an author list never split.
NAME = r"(?:(?:van|von|de|der|den|del|da|di|le|la|dos)\s+){0,2}[^\W\d_][^\W\d_'’\-]*(?:['’\-][^\W\d_]+)*"
AUTHOR_START = re.compile(r"^" + NAME + r"(?:,\s+[A-Z]\.|\s+[A-Z]{1,3}(?:,|\s\(|\s+[A-Z]{1,3}[,\s(])|\s+[A-Z]\.)")
START = re.compile(r"(?<=[.?!”\")\d]\s)(?=" + AUTHOR_START.pattern[1:] + r")")


def parse(pdf_path):
    cache = os.path.join(bl.INDEX, ns.sha1_of(pdf_path) + ".refs.json")
    if os.path.exists(cache):
        d = dl.load_json(cache, None)
        if d and d.get("v") == bl.VERSION:
            return d["refs"]
    bs = [b for b in bl.extract(pdf_path) if b.get("kind") == "ref"]
    text, offs = "", []
    for b in bs:                                  # the whole reference list as one stream: entries rarely respect block boundaries
        offs.append(len(text)); text += (HEAD.sub("", b["text"], count=1) if not text else b["text"]) + " "
    import bisect
    block_at = lambda o: bs[max(0, bisect.bisect_right(offs, o) - 1)]  # noqa: E731
    out, pos = [], 0
    pieces = _numbered(text)
    if pieces:
        for n, seg in pieces:
            if len(seg) >= 25:
                out.append(_entry(len(out), n, seg, block_at(text.find(seg[:40], pos) if text.find(seg[:40], pos) >= 0 else pos)))
                pos = max(pos, text.find(seg[:40], pos))
    else:
        starts = [0] + [m.start() for m in START.finditer(text)]
        for k, a in enumerate(starts):
            seg = text[a:starts[k + 1] if k + 1 < len(starts) else len(text)].strip()
            if k == 0 and not AUTHOR_START.match(seg) and len(starts) > 1:
                seg = seg                           # a fragment before the first entry: keep (it is usually the first entry's start)
            if len(seg) >= 25:
                out.append(_entry(len(out), None, seg, block_at(a)))
    os.makedirs(bl.INDEX, exist_ok=True)
    tmp = cache + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"v": bl.VERSION, "refs": out}, f, ensure_ascii=False)
    os.replace(tmp, cache)
    return out


def match_library(refs, rows):
    """Mark which references you already have in the library (same DOI, or the library paper's title appears in the entry)."""
    by_doi = {r["doi"].lower(): r["file"] for r in rows if r.get("doi")}
    titles = [(re.sub(r"[^a-z0-9]", "", r["title"].lower()), r["file"]) for r in rows if len(r.get("title", "")) >= 20]
    res = []
    for e in refs:
        flat = re.sub(r"[^a-z0-9]", "", e["text"].lower())
        f = by_doi.get(e["doi"].lower()) if e["doi"] else None
        f = f or next((file for t, file in titles if t in flat), None)
        res.append({**e, "in_library": f})
    return res


def cite_info(doi):
    """Title, venue, citation count and TL;DR from Semantic Scholar's public API; cached; {} if it can't be reached."""
    path = os.path.join(dl.LOCAL, "cite_cache.json")
    cache = dl.load_json(path, {}) if os.path.exists(path) else {}
    key = doi.lower()
    if key in cache:
        return cache[key]
    import urllib.parse
    try:
        raw, _ = dl.http_get("https://api.semanticscholar.org/graph/v1/paper/DOI:%s?fields=title,year,venue,citationCount,tldr" % urllib.parse.quote(doi, safe="/"),
                             timeout=10, headers={"Accept": "application/json"})
        d = json.loads(raw.decode("utf-8", "replace"))
        info = {"title": d.get("title", ""), "year": d.get("year"), "venue": d.get("venue", ""), "citations": d.get("citationCount"),
                "tldr": ((d.get("tldr") or {}).get("text") or "")}
    except Exception:  # noqa: BLE001
        return {}
    cache[key] = info
    os.makedirs(dl.LOCAL, exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)
    os.replace(path + ".tmp", path)
    return info
