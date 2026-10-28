"""Authors the index doesn't have. Some feeds (Decision Support Systems, HBR) list a paper before its authors are known, so _index.csv has
an empty authors cell — and citations, BibTeX keys, the matrix and the Ideas Lab then say "Anon.". Crossref usually has them a few days later.

    authorcache.backfill(rows)    one-time lookups (Crossref) for rows with a DOI and no authors; the answers are kept in state/authors_cache.json
    authorcache.apply(rows)       fills empty "authors" of library rows from that cache (no network; _index.csv is never rewritten)

A lookup that finds nothing is remembered for three days, then tried again.
"""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import download as dl  # noqa: E402

CACHE = os.path.join(dl.STATE, "authors_cache.json")
RETRY_AFTER = 3 * 86400
_mem = {}


def cached():
    try:
        mt = os.stat(CACHE).st_mtime_ns
    except OSError:
        return {}
    if _mem.get("k") != (CACHE, mt):
        _mem["k"], _mem["v"] = (CACHE, mt), dl.load_json(CACHE, {})
    return _mem["v"]


def apply(rows):
    c = cached()
    if c:
        for r in rows:
            if not (r.get("authors") or "").strip() and r.get("doi"):
                names = (c.get(r["doi"].lower()) or {}).get("authors")
                if names:
                    r["authors"] = "; ".join(names)
    return rows


def backfill(rows, lookup=None):
    """Look up the DOIs that lack authors (6 at a time). Returns how many papers got authors."""
    if lookup is None:
        import importer
        lookup = importer.crossref_meta
    c = dict(cached())
    now = time.time()
    def known(doi):
        e = c.get(doi.lower())
        return bool(e) and (bool(e.get("authors")) or now - e.get("ts", 0) < RETRY_AFTER)
    todo = sorted({r["doi"] for r in rows if r.get("doi") and not (r.get("authors") or "").strip() and not known(r["doi"])})
    if not todo:
        return 0

    def one(doi):
        try:
            return (lookup(doi) or {}).get("authors") or []
        except Exception:  # noqa: BLE001  (offline: try again next time)
            return None
    with ThreadPoolExecutor(6) as ex:
        found = list(ex.map(one, todo))
    n = 0
    for doi, names in zip(todo, found):
        if names is None:
            continue
        c[doi.lower()] = {"authors": names, "ts": now}
        n += 1 if names else 0
    os.makedirs(dl.STATE, exist_ok=True)
    tmp = CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(c, f, ensure_ascii=False)
    os.replace(tmp, CACHE)
    return n
