"""Author search: an author's works from OpenAlex, downloaded into a folder named after them.

    authors.search(name)            -> matching OpenAlex authors [{id, name, institutions, works, cited, topics, orcid}]
    authors.works(ids)              -> (works, complete, total): the works of the ticked author ids (one person can have two ids), all pages
    authors.select(works)           -> (kept, excluded): journal articles, conference papers, working papers; a working paper only when no published version is listed
    authors.to_papers(works, fid)   -> paper dicts in the shape of a run's papers (the routes of each paper's publisher, no Crossref call per paper)
    authors.split(papers)           -> (new, have, deleted): new ones to download; ones already in the library (their index keys); ones you deleted (never fetched again)
    authors.folder_for(ids, name, institution) -> the folder id for this author (made, or found again through Papers/_authors.json)

OpenAlex is reached through fetch_new._openalex: your OpenAlex key, if you set one, goes in the Authorization header; no e-mail address is sent.
The record of which folder belongs to which author ids is Papers/_authors.json (in the data folder, so both computers share it).
"""
import json
import os
import re
import sys
import threading
import unicodedata
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import download as dl  # noqa: E402
import fetch_new as fx  # noqa: E402

TYPES = "article|preprint|report|review|book-chapter"      # asked of OpenAlex; book chapters are kept only when they are conference papers (select)
PER_PAGE = 200
MAX_PAGES = 50                                               # 10,000 works: beyond that the list is marked incomplete, never cut silently
PREPRINT_PREFIXES = ("10.2139", "10.48550", "10.31219", "10.31234", "10.31235", "10.21203", "10.20944", "10.3386", "10.36227", "10.22541")   # SSRN, arXiv, OSF, Research Square, Preprints.org, NBER, TechRxiv, Authorea
_lock = threading.Lock()


class AuthorError(Exception):
    pass


def _fetch(path):
    return fx._openalex(path)


def _answer(path):
    r = _fetch(path)
    if r is None:
        if fx.openalex_spent():
            raise AuthorError("OpenAlex's daily allowance is used up. It resets within a day; with a free OpenAlex key (Settings → Paper sources) you get your own.")
        raise AuthorError("OpenAlex didn't answer. Check the connection and try again.")
    return r


def clean(name):
    """The words of a name as OpenAlex's search takes them (its query syntax reads ':' and '?'; quotes and commas only get in the way)."""
    s = re.sub(r"[:?\"'(),;|*]+", " ", str(name or ""))
    return re.sub(r"\s+", " ", s).strip()[:120]


def _short(x):
    return str(x or "").rsplit("/", 1)[-1]


def _candidate(a):
    insts = [i.get("display_name") for i in (a.get("last_known_institutions") or []) if i and i.get("display_name")]
    topics = [t.get("display_name") for t in (a.get("topics") or []) if t and t.get("display_name")][:3]
    orcid = _short(a.get("orcid")) if a.get("orcid") else ""
    return {"id": _short(a.get("id")), "name": a.get("display_name") or "", "institutions": insts, "works": int(a.get("works_count") or 0),
            "cited": int(a.get("cited_by_count") or 0), "topics": topics, "orcid": orcid}


SELECT_AUTHOR = "id,display_name,orcid,works_count,cited_by_count,last_known_institutions,topics"


def search(name):
    """OpenAlex authors matching a name (or an ORCID iD, or an OpenAlex author id A…), the most cited first as OpenAlex ranks them."""
    q = str(name or "").strip()
    m = re.search(r"(\d{4}-\d{4}-\d{4}-\d{3}[\dXx])", q)
    if m:
        r = _answer("authors?filter=orcid:%s&select=%s" % (m.group(1).upper(), SELECT_AUTHOR))
        return [_candidate(a) for a in r.get("results") or []]
    if re.fullmatch(r"[Aa]\d{4,12}", q):
        r = _answer("authors/%s?select=%s" % (q.upper(), SELECT_AUTHOR))
        return [_candidate(r)] if r.get("id") else []
    words = clean(q)
    if not words:
        return []
    r = _answer("authors?search=%s&per-page=15&select=%s" % (urllib.parse.quote(words), SELECT_AUTHOR))
    return [_candidate(a) for a in r.get("results") or []]


SELECT_WORK = "id,doi,display_name,publication_date,publication_year,type,primary_location,best_oa_location,authorships"


def works(ids, progress=None):
    """All works of these OpenAlex author ids (of the types asked), page by page. Returns (works, complete, total): complete is False when a page did not come
    (the daily allowance, the network) or the list is longer than MAX_PAGES pages; what came is returned either way."""
    ids = [i for i in (_short(x).upper() for x in ids) if re.fullmatch(r"A\d{4,12}", i)]
    if not ids:
        return [], True, 0
    flt = "author.id:%s,type:%s" % ("|".join(ids), TYPES)
    out, cursor, total = [], "*", 0
    for _ in range(MAX_PAGES):
        r = _fetch("works?filter=%s&per-page=%d&cursor=%s&select=%s" % (flt, PER_PAGE, urllib.parse.quote(cursor), SELECT_WORK))
        if r is None:
            if not out:
                _answer("works?filter=%s&per-page=1" % flt)        # (says why: the allowance, or no answer)
            return out, False, total
        total = int((r.get("meta") or {}).get("count") or total)
        out += r.get("results") or []
        if progress:
            progress(len(out), total)
        cursor = (r.get("meta") or {}).get("next_cursor")
        if not cursor or not r.get("results"):
            return out, True, total
    return out, False, total


def _source(w):
    loc = w.get("primary_location") or {}
    return loc.get("source") or {}


def kind(w):
    """'journal' | 'conference' | 'working' | None (left out: books, chapters that are not conference papers, and anything else)."""
    t, src = w.get("type") or "", _source(w)
    st, venue = (src.get("type") or "").lower(), (src.get("display_name") or "")
    if st == "conference" or re.search(r"proceedings|conference|lecture notes|symposium|workshop", venue, re.I) and t in ("article", "book-chapter"):
        return "conference"
    if t in ("preprint", "report") or st == "repository":
        return "working"
    if t in ("article", "review"):
        return "journal"
    return None


def _doi(w):
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", str(w.get("doi") or ""), flags=re.I).strip().lower()


def _title(w):
    return re.sub(r"\s+", " ", str(w.get("display_name") or w.get("title") or "")).strip()


def is_preprint_doi(doi):
    return str(doi or "").lower().startswith(PREPRINT_PREFIXES)


def select(ws):
    """The works to download. Kept: journal articles, conference papers, working papers. The same work listed twice (same OpenAlex id or DOI) once; a working paper is
    dropped when a published version (journal or conference) of the same title is listed. Different works that merely share a title ('Introduction',
    'Editorial') are kept apart: titles only join a working paper to a published version. Returns (kept, excluded {reason: count})."""
    excluded, seen_id, seen_doi, kept = {}, set(), set(), []
    for w in ws:
        k = kind(w)
        if not k:
            excluded[w.get("type") or "other"] = excluded.get(w.get("type") or "other", 0) + 1
            continue
        wid, doi = _short(w.get("id")), _doi(w)
        if wid in seen_id or (doi and doi in seen_doi) or not _title(w):
            continue
        seen_id.add(wid)
        if doi:
            seen_doi.add(doi)
        kept.append(dict(w, _kind=k))
    published = {fx.norm_title(_title(w)) for w in kept if w["_kind"] != "working"}
    out = []
    for w in kept:
        if w["_kind"] == "working" and fx.norm_title(_title(w)) in published:
            excluded["working paper (published version listed)"] = excluded.get("working paper (published version listed)", 0) + 1
            continue
        out.append(w)
    return out, excluded


def to_papers(ws, fid, label):
    """Paper dicts for a run. The journal and its download route: a watched journal recognised by ISSN keeps its settings; any other gets the route of its
    DOI's publisher (fetch_new.venue_route). No Crossref call per paper (an author can have hundreds)."""
    journals = dl.load_json(os.path.join(dl.BASE, "journals.json"), {"journals": []})["journals"]
    out = []
    for w in ws:
        doi, title, src = _doi(w), _title(w), _source(w)
        meta = {"issn": src.get("issn") or ([src["issn_l"]] if src.get("issn_l") else []), "container": src.get("display_name") or ""}
        name, abbr, publisher = fx.venue_route(meta, src.get("display_name") or "", doi, journals)
        oa = (w.get("best_oa_location") or {}).get("pdf_url") or ""
        url = ("https://doi.org/" + doi) if doi else (oa or ((w.get("primary_location") or {}).get("landing_page_url") or ""))
        date = str(w.get("publication_date") or "")[:10] or ("%s-01-01" % w["publication_year"] if w.get("publication_year") else "")
        authors_ = [((a.get("author") or {}).get("display_name") or "") for a in (w.get("authorships") or [])]
        out.append({"id": fx.key_for(doi, name, title), "title": title, "authors": [a for a in authors_ if a], "doi": doi, "url": url, "journal": name, "abbr": abbr,
                    "pub_date": date, "topics": [], "publisher": publisher, "abstract": None, "keywords": None, "source": "author", "verdict": "download",
                    "reason": {"journal": "journal article", "conference": "conference paper", "working": "working paper"}[w["_kind"]] + " by " + str(label)[:60],
                    "openalex": _short(w.get("id")), "folder": fid})
    return out


def _index_rows():
    import csv
    p = os.path.join(dl.PAPERS, "_index.csv")
    if not os.path.exists(p):
        return []
    with open(p, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _same(row, p):
    """Is this index row this paper? The same DOI; or the same title when one side has no DOI, or when the two DOIs differ because one is a preprint's (SSRN, arXiv…)
    and the other the published version's (a deleted published paper must keep its preprint away, and a downloaded one must not bring it in again)."""
    d1, d2 = (row.get("doi") or "").strip().lower(), (p.get("doi") or "").strip().lower()
    if d1 and d2 and d1 == d2:
        return True
    t1, t2 = fx.norm_title(row.get("title") or ""), fx.norm_title(p.get("title") or "")
    if not t1 or t1 != t2:
        return False
    return not (d1 and d2) or is_preprint_doi(d1) or is_preprint_doi(d2)


def split(papers):
    """(new, have, deleted). have: [(paper, index key of the library row)] for papers already in the library (their file exists); deleted: papers you deleted."""
    import deletion
    rows = _index_rows()
    by_doi, by_title = {}, {}
    for r in rows:
        if (r.get("doi") or "").strip():
            by_doi.setdefault(r["doi"].strip().lower(), []).append(r)
        by_title.setdefault(fx.norm_title(r.get("title") or ""), []).append(r)
    new, have, gone = [], [], []
    for p in papers:
        cands = by_doi.get((p.get("doi") or "").lower(), []) + by_title.get(fx.norm_title(p["title"]), [])
        cands = [r for r in cands if _same(r, p)]
        ok = [r for r in cands if r.get("status") == "ok" and r.get("file") and os.path.exists(os.path.join(dl.ROOT, r["file"]))]
        if ok:
            have.append((p, dl.index_key(ok[0])))
        elif any(r.get("status") == deletion.DELETED for r in cands):
            gone.append(p)
        else:
            new.append(p)
    return new, have, gone


# ---------------------------------------------------------------- which folder is this author's (Papers/_authors.json)

def _store_path():
    return os.path.join(dl.PAPERS, "_authors.json")


def load():
    d = dl.load_json(_store_path(), {"authors": []}) if os.path.exists(_store_path()) else {"authors": []}
    return d if isinstance(d.get("authors"), list) else {"authors": []}


def _save(d):
    os.makedirs(dl.PAPERS, exist_ok=True)
    tmp = dl.tmp_path(_store_path())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _store_path())


def _plain(s):
    return unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower().strip()


def folder_for(ids, name, institution=""):
    """The folder for the author with these OpenAlex ids. The record that shares the most ids with them (their folder, if it still exists) is used again, so a second
    search of the same person fills the same folder; otherwise a folder is made: the author's name, or, when a folder of that name exists and is not theirs,
    'name · institution' (then a number). Returns (folder id, record)."""
    import folders as fo
    ids = sorted({_short(i).upper() for i in ids if _short(i)})
    name = re.sub(r"\s+", " ", str(name or "")).strip()[:50] or "Author"
    with _lock:
        d = load()
        best = max(d["authors"], key=lambda r: len(set(r.get("ids") or []) & set(ids)), default=None)
        rec = best if best and set(best.get("ids") or []) & set(ids) else None
        if rec and rec.get("folder") and fo.get(rec["folder"]):
            rec["ids"] = sorted(set(rec.get("ids") or []) | set(ids))
            _save(d)
            return rec["folder"], rec
        names = {_plain(f["name"]) for f in fo.list_all()}
        base = name if _plain(name) not in names else ("%s · %s" % (name, institution.strip()[:40]) if institution.strip() else name)
        cand, n = base, 2
        while _plain(cand) in names:
            cand = "%s (%d)" % (base, n); n += 1
        f = fo.create(cand)
        if rec:
            rec.update(ids=sorted(set(rec.get("ids") or []) | set(ids)), folder=f["id"], name=name)
        else:
            rec = {"ids": ids, "name": name, "folder": f["id"], "filed": []}
            d["authors"].append(rec)
        _save(d)
        return f["id"], rec


def file_have(rec_folder, have):
    """Put papers already in the library into the author's folder, once: a paper you took out of the folder later is not put back by the next search."""
    import folders as fo
    with _lock:
        d = load()
        rec = next((r for r in d["authors"] if r.get("folder") == rec_folder), None)
        filed = set((rec or {}).get("filed") or [])
        keys = [k for _, k in have if k and k not in filed]
        if keys:
            fo.add(rec_folder, keys)
        if rec is not None:
            rec["filed"] = sorted(filed | {k for _, k in have if k})
            _save(d)
        return keys


def note_filed(rec_folder, keys):
    """Remember keys filed into the author's folder by a download (see file_have)."""
    with _lock:
        d = load()
        rec = next((r for r in d["authors"] if r.get("folder") == rec_folder), None)
        if rec is not None:
            rec["filed"] = sorted(set(rec.get("filed") or []) | {k for k in keys if k})
            _save(d)
