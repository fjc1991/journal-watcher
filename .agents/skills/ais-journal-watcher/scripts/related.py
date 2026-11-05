"""Related and missing papers (Q6).

    related.paper(row, library)                   -> {"similar", "related", "cited", "stats"} for one paper (the reader's Related tab)
    for m in related.scope_stream(rows, library, min_n=2): ...   a folder, or the whole library:
        {"type": "status", "msg"}  {"type": "result", "missing": [...], "related": [...], "similar": [...], "stats": {...}}  | {"type": "error", "error"}
    related.ignore(ids) / related.unignore(ids) / related.ignored()            "not interested" (kept in Papers/_related.json)
    related.run_papers(items) -> ([paper dicts for a dashboard run], [already in the library])        for the one-click download

What the lists mean
  similar   papers you already have that are closest in meaning to the paper (or to the folder as a whole): local embeddings of title + card/summary
  missing   papers cited by several of your papers that are not in your library (OpenAlex's reference lists; for a paper OpenAlex has no
            references for yet — typical for this year's papers — the DOIs printed in the PDF's own bibliography are used instead)
  related   papers OpenAlex itself calls related to yours (not in your library), the more of your papers that point at one the higher it ranks
Library membership is decided by DOI, OpenAlex id, or an identical title. Everything from OpenAlex is cached for a week (state/openalex_cache.json).
"""
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import download as dl  # noqa: E402
import evidence as evd  # noqa: E402
import notes_store as ns  # noqa: E402

CACHE = os.path.join(dl.LOCAL, "openalex_cache.json")
IGNORED = os.path.join(dl.PAPERS, "_related.json")
TTL = 7 * 86400
REL_SIM = float(os.environ.get("JW_RELATED_SIM", 0.74))      # OpenAlex's own "related works" are noisy (a "Janus" paper for a "Janus effect" one): keep those whose title is this close to the paper that points at them
CHUNK = 40
SEL_FULL = "id,doi,title,publication_year,publication_date,cited_by_count,primary_location,authorships,referenced_works,related_works"
SEL_LIGHT = "id,doi,title,publication_year,publication_date,cited_by_count,primary_location,authorships"
_mem = {}


# ---------------------------------------------------------------- OpenAlex, cached

def _fetch(path):
    """GET an OpenAlex path (None when it can't be reached). The tests replace this."""
    import fetch_new as fx
    return fx._openalex(path)


def _cache():
    try:
        mt = os.stat(CACHE).st_mtime_ns
    except OSError:
        return {}
    if _mem.get("k") != (CACHE, mt):
        _mem["k"], _mem["v"] = (CACHE, mt), dl.load_json(CACHE, {})
    return _mem["v"]


def _save_cache(c):
    os.makedirs(dl.LOCAL, exist_ok=True)
    tmp = CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(c, f, ensure_ascii=False)
    os.replace(tmp, CACHE)
    _mem.clear()


def short(i):
    return str(i or "").rsplit("/", 1)[-1]


def _doi(d):
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", str(d or ""), flags=re.I).strip().lower()


def rec_of(w):
    loc = (w.get("primary_location") or {}).get("source") or {}
    return {"id": short(w.get("id")), "doi": _doi(w.get("doi")), "title": re.sub(r"\s+", " ", w.get("title") or "").strip(), "year": w.get("publication_year"),
            "date": w.get("publication_date") or "", "cited_by": w.get("cited_by_count") or 0, "venue": loc.get("display_name") or "",
            "authors": list(dict.fromkeys((a.get("author") or {}).get("display_name") for a in (w.get("authorships") or [])[:12] if (a.get("author") or {}).get("display_name")))[:8],
            "refs": [short(x) for x in w.get("referenced_works") or []], "related": [short(x) for x in w.get("related_works") or []]}


def lookup(kind, values, full=False):
    """{value: record} for DOIs (kind "doi") or OpenAlex ids (kind "id"); `full` also returns each work's reference and related-works lists."""
    c = dict(_cache())
    now, out, need = time.time(), {}, []
    key = lambda v: "%s:%s:%s" % ("f" if full else "l", kind, v)  # noqa: E731
    vals = [v for v in dict.fromkeys(str(x).lower() if kind == "doi" else short(x) for x in values) if v and "," not in v and "|" not in v]
    for v in vals:
        e = c.get(key(v))
        if e and now - e["ts"] < TTL:
            if e["rec"]:
                out[v] = e["rec"]
        else:
            need.append(v)
    changed = False
    for i in range(0, len(need), CHUNK):
        chunk = need[i:i + CHUNK]
        flt = "doi:" + "|".join(urllib.parse.quote(v, safe="/") for v in chunk) if kind == "doi" else "openalex:" + "|".join(chunk)
        d = _fetch("works?filter=%s&per-page=%d&select=%s" % (flt, CHUNK + 10, SEL_FULL if full else SEL_LIGHT))
        if not isinstance(d, dict):
            continue                                     # unreachable now: leave it uncached, so the next try asks again
        got = {}
        for w in d.get("results") or []:
            r = rec_of(w)
            got[r["doi"] if kind == "doi" else r["id"]] = r
        for v in chunk:
            c[key(v)] = {"ts": now, "rec": got.get(v)}
            if got.get(v):
                out[v] = got[v]
        changed = True
    if changed:
        _save_cache(c)
    return out


# ---------------------------------------------------------------- "not interested"

def ignored():
    d = dl.load_json(IGNORED, {}) if os.path.exists(IGNORED) else {}
    return d.get("ignored", {}) if isinstance(d, dict) else {}


def _write_ignored(m):
    os.makedirs(os.path.dirname(IGNORED), exist_ok=True)
    tmp = IGNORED + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "ignored": m}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, IGNORED)


def ignore(items):
    """items: [{id, title}] — hidden from every list from now on."""
    m = ignored()
    for x in items:
        if re.fullmatch(r"W\d+", str(x.get("id") or "")):
            m[x["id"]] = {"title": str(x.get("title") or "")[:200], "ts": time.strftime("%Y-%m-%d")}
    _write_ignored(m)
    return m


def unignore(ids):
    m = ignored()
    for i in ids:
        m.pop(i, None)
    if m:
        _write_ignored(m)
    elif os.path.exists(IGNORED):
        os.remove(IGNORED)
    return m


# ---------------------------------------------------------------- what is already in the library

def _norm(t):
    return re.sub(r"[^a-z0-9]", "", (t or "").lower())


class Lib:
    """Which OpenAlex works are the library's own: by DOI, id, or an identical (long enough) title."""

    def __init__(self, rows):
        self.rows = rows
        self.recs = lookup("doi", [r["doi"] for r in rows if r.get("doi")], full=True)
        self.ids = {r["id"] for r in self.recs.values()}
        self.dois = {r["doi"].lower() for r in rows if r.get("doi")}
        self.titles = {_norm(r.get("title")) for r in rows if len(r.get("title", "")) >= 25}

    def rec(self, row):
        return self.recs.get((row.get("doi") or "").lower())

    def has(self, rec):
        return rec["id"] in self.ids or (rec["doi"] and rec["doi"] in self.dois) or (len(rec["title"]) >= 25 and _norm(rec["title"]) in self.titles)


def _pdf_dois(row, cap=80):
    pdf = ns.pdf_path(row.get("file"))
    if not pdf:
        return []
    try:
        import refs
        return list(dict.fromkeys(e["doi"].lower().rstrip(".,;)") for e in refs.parse(pdf) if e.get("doi")))[:cap]
    except Exception:  # noqa: BLE001  (a PDF without a readable bibliography)
        return []


# ---------------------------------------------------------------- more like this, from your own library (local, free)

def _text_of(row):
    bits = [row.get("title", ""), row.get("matched_topics", "")]
    pdf = ns.pdf_path(row.get("file")) if row.get("card") else None
    if pdf:
        import papercard as pc
        card = pc.load(pdf)
        if card:
            d = card["data"]
            bits += [d["question"]["text"], ", ".join(d["topics"]), d["contribution"]["text"]]
    if row.get("summary"):
        try:
            with open(os.path.join(dl.ROOT, row["summary"]), encoding="utf-8") as f:
                bits.append(re.sub(r"[#*`>|]+", " ", f.read(2500)))
        except OSError:
            pass
    return re.sub(r"\s+", " ", " . ".join(b for b in bits if b))[:1800]


def _vectors(rows):
    import ai
    import numpy as np
    texts = [_text_of(r) for r in rows]
    keys = [hashlib.sha1((ai.config_data()["embeddings"]["model"] + t).encode()).hexdigest() for t in texts]
    need = [i for i, k in enumerate(keys) if k not in _mem.setdefault("vec", {})]
    for s in range(0, len(need), 48):
        part = need[s:s + 48]
        vs = ai.embed([texts[i] for i in part])
        for i, v in zip(part, vs):
            _mem["vec"][keys[i]] = v
    return np.array([_mem["vec"][k] for k in keys]) if keys else np.zeros((0, 1))


def similar(targets, library, k=8):
    """Library papers (not among `targets`) closest to the targets taken together: [{file, key, title, authors, year, journal, sim}]."""
    import numpy as np
    tfiles = {r["file"] for r in targets}
    pool = [r for r in library if r["file"] not in tfiles]
    if not targets or not pool:
        return []
    tv, pv = _vectors(targets), _vectors(pool)
    c = tv.mean(axis=0)
    c = c / max(float(np.linalg.norm(c)), 1e-9)
    sims = pv @ c
    order = np.argsort(-sims)[:k]
    return [{"file": pool[i]["file"], "key": pool[i].get("key"), "title": pool[i].get("title", ""), "authors": pool[i].get("authors", ""), "year": pool[i].get("year", ""),
             "journal": pool[i].get("journal", ""), "sim": round(float(sims[i]), 2), "cite": evd.short_cite(pool[i])} for i in order]


# ---------------------------------------------------------------- candidates

def _card(rec, n=0, via=None, sim=None):
    return {"id": rec["id"], "doi": rec["doi"], "title": rec["title"], "year": rec["year"], "authors": rec["authors"], "venue": rec["venue"], "cited_by": rec["cited_by"],
            "date": rec["date"], "n": n, "via": via or [], "sim": sim}


def _closeness(cands, rows, via_files):
    """For each candidate record: its highest closeness (cosine of local embeddings: its title vs the paper's title/card/summary) to the papers that point at it."""
    import ai
    if not cands:
        return []
    idx = {r["file"]: i for i, r in enumerate(rows)}
    V = _vectors(rows)
    E = ai.embed([c["title"] for c in cands])
    return [max([float(V[idx[f]] @ e) for f in fs if f in idx] or [0.0]) for e, fs in zip(E, via_files)]


def _rank(counter, recs, lib, hide, limit):
    out = []
    for i, n in sorted(counter.items(), key=lambda kv: (-kv[1], -(recs[kv[0]]["cited_by"] if kv[0] in recs else 0))):
        r = recs.get(i)
        if r and r["title"] and not lib.has(r) and i not in hide:
            out.append((r, n))
        if len(out) >= limit:
            break
    return out


def paper(row, library, k=8):
    """Everything for one paper: {similar, related, cited, stats}. `related` and `cited` come from OpenAlex and may be empty when it can't be reached."""
    lib = Lib(library)
    hide = set(ignored())
    rec = lib.rec(row)
    refs, via_pdf = (rec or {}).get("refs", []), False
    if not refs:
        dois = _pdf_dois(row)
        refs = [r["id"] for r in lookup("doi", dois).values()]
        via_pdf = bool(refs)
    ids = list(dict.fromkeys(refs + (rec or {}).get("related", [])))
    recs = lookup("id", ids)
    cited = _rank(Counter({i: 1 for i in refs}), recs, lib, hide, 12) if refs else []
    cited.sort(key=lambda x: -x[0]["cited_by"])
    related = _rank(Counter({i: 1 for i in (rec or {}).get("related", [])}), recs, lib, hide, 25)
    sims = _closeness([r for r, _ in related], [row], [[row["file"]]] * len(related))
    related = [(r, s_) for (r, _), s_ in zip(related, sims) if s_ >= REL_SIM]
    related.sort(key=lambda t: -t[1])
    return {"similar": similar([row], library, k), "cited": [_card(r, 1) for r, _ in cited[:10]], "related": [_card(r, 1, sim=round(s_, 2)) for r, s_ in related[:10]],
            "stats": {"openalex": bool(rec), "refs": len(refs), "via_pdf": via_pdf, "related": len((rec or {}).get("related", []))}}


def scope_stream(rows, library, min_n=2, limit=60):
    """A folder (or any set of papers): which papers do several of them cite that you don't have, and what else is related."""
    try:
        if not rows:
            yield {"type": "error", "error": "There are no papers in this scope."}
            return
        yield {"type": "status", "msg": "Looking up your %d papers in OpenAlex…" % len(library)}
        lib = Lib(library)
        hide = set(ignored())
        refs_of, related_of, via_pdf, nodata = {}, {}, 0, []
        for r in rows:
            rec = lib.rec(r)
            refs = list((rec or {}).get("refs", []))
            if not refs:
                dois = _pdf_dois(r)
                if dois:
                    yield {"type": "status", "msg": "Reading the reference list of “%s”…" % r.get("title", "")[:50]}
                    refs = [x["id"] for x in lookup("doi", dois).values()]
                    via_pdf += 1 if refs else 0
            if not refs:
                nodata.append(r.get("title", ""))
            refs_of[r["file"]], related_of[r["file"]] = set(refs), list((rec or {}).get("related", []))
        cites, rel = Counter(), Counter()
        for f in refs_of:
            cites.update(refs_of[f])
            rel.update(set(related_of[f]))
        top_ids = [i for i, _ in sorted(cites.items(), key=lambda kv: -kv[1]) if cites[i] >= min_n and i not in hide][:200]
        yield {"type": "status", "msg": "Fetching details of %d papers your papers cite…" % len(top_ids)}
        rel_ids = [i for i, _ in rel.most_common(80)]
        recs = lookup("id", list(dict.fromkeys(top_ids + rel_ids)))
        by_file = {r["file"]: r for r in rows}
        missing = []
        for r, n in _rank(Counter({i: cites[i] for i in top_ids}), recs, lib, hide, limit):
            citing = [{"file": f, "cite": evd.short_cite(by_file[f]), "title": by_file[f].get("title", "")} for f, ids in refs_of.items() if r["id"] in ids]
            missing.append(_card(r, n, citing))
        shown = {m["id"] for m in missing}
        related, pool = [], []
        for r, n in _rank(Counter({i: rel[i] for i in rel_ids}), recs, lib, hide | shown, 60):
            who = [{"file": f, "cite": evd.short_cite(by_file[f]), "title": by_file[f].get("title", "")} for f, ids in related_of.items() if r["id"] in ids]
            pool.append((r, n, who))
        sims = _closeness([r for r, _, _ in pool], rows, [[w["file"] for w in who] for _, _, who in pool])
        for (r, n, who), s_ in sorted(zip(pool, sims), key=lambda t: (-t[0][1], -t[1])):
            if s_ >= REL_SIM and len(related) < 20:
                related.append(_card(r, n, who, round(s_, 2)))
        yield {"type": "status", "msg": "Comparing with the rest of your library…"}
        yield {"type": "result", "min_n": min_n, "missing": missing, "related": related, "similar": similar(rows, library, 8),
               "stats": {"papers": len(rows), "with_refs": len(rows) - len(nodata), "via_pdf": via_pdf, "no_refs": nodata[:12], "unresolved": len([r for r in rows if lib.rec(r) is None])}}
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        yield {"type": "error", "error": "Something went wrong: %s" % re.sub(r"\s+", " ", str(e))[:200]}


# ---------------------------------------------------------------- one click into the download pipeline

from fetch_new import PUBLISHER_BY_PREFIX, venue_abbr as _abbr, venue_route as _journal  # noqa: E402,F401  (the routing of a paper from any journal: fetch_new.py)


def run_papers(items):
    """Paper dicts in the shape the dashboard's runs use, from chosen candidates [{id, doi, title, authors, year, venue, date, reason}].
    Returns (papers, already_in_library). Watched journals keep their publisher settings and every other paper gets the route of its DOI's publisher, so the
    usual download routes (open access first, then publisher sign-ins and library access) run exactly as for papers found by a weekly check."""
    import fetch_new as fx
    import importer
    journals = dl.load_json(os.path.join(dl.BASE, "journals.json"), {"journals": []})["journals"]
    papers, have = [], []
    for it in items:
        doi = importer.clean_doi(it.get("doi"))
        title = re.sub(r"\s+", " ", str(it.get("title") or "")).strip()
        if not title:
            continue
        meta = importer.crossref_meta(doi) if doi else None
        name, abbr, publisher = _journal(meta, str(it.get("venue") or ""), doi, journals)
        date = (meta or {}).get("pub_date") or str(it.get("date") or "")[:10] or ("%s-01" % it["year"] if it.get("year") else "")
        if re.fullmatch(r"\d{4}", date):
            date += "-01"
        p = {"title": (meta or {}).get("title") or title, "authors": (meta or {}).get("authors") or it.get("authors") or [], "doi": doi, "url": (meta or {}).get("url") or (("https://doi.org/" + doi) if doi else ""),
             "journal": name, "abbr": abbr, "pub_date": date, "topics": [], "publisher": publisher}
        ex = importer._existing(p)
        if ex:
            have.append({"title": title, "file": ex})
            continue
        p.update({"id": fx.key_for(p["doi"], p["journal"], p["title"]), "abstract": (meta or {}).get("abstract"), "keywords": None, "source": "related",
                  "verdict": "download", "reason": str(it.get("reason") or "")[:300], "openalex": it.get("id")})
        papers.append(p)
    return papers, have
