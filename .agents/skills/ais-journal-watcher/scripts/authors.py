"""Author search: an author's works from OpenAlex, downloaded into a folder named after them.

    authors.search(name)            -> matching OpenAlex authors [{id, name, institutions, works, cited, topics, orcid}]
    authors.works(ids)              -> (works, complete, total): the works of the ticked author ids (one person can have two ids), all pages
    authors.select(works)           -> (kept, excluded): journal articles, conference papers, working papers; a working paper only when no published version is listed
    authors.to_papers(works, fid)   -> paper dicts in the shape of a run's papers (the routes of each paper's publisher, no Crossref call per paper)
    authors.split(papers)           -> (new, have, deleted): new ones to download; ones already in the library (their index keys); ones you deleted (never fetched again)
    authors.folder_for(ids, name, institution) -> the folder id for this author (made, or found again through Papers/_authors.json)

Following an author (the weekly check downloads their new works into the folder; pipeline.follow_list):
    authors.set_follow(fid, on)     switch it on or off for the author whose folder is fid
    authors.followed()              the records being followed (read from the file each time: another computer may have changed it)
    authors.follow_mark(fid, wids)  OpenAlex work ids dealt with (downloaded, already in the library, deleted, or not a kind that is downloaded): never asked about again
    authors.saved()                 the authors with a folder, for the Authors tab's "Your authors" list

A record in _authors.json: {ids, name, folder, filed} and, once followed, {follow, follow_on, follow_known: [W ids], follow_last, follow_warn}. "New" means a work
in the author's full OpenAlex list that is not in follow_known (no date filter: a work OpenAlex lists late, or backdated, is still new). Only works that were dealt
with join follow_known, so one that could not be downloaded is tried again the next week. The writes are unions, so a write lost to the other computer is made
again a week later. No e-mail address or key is kept there.

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


def _fetch(path, missing=None):
    return fx._openalex(path, missing) if missing is not None else fx._openalex(path)


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
    if re.fullmatch(r"[Aa]\d{4,12}", q):           # (the list endpoint: an id OpenAlex does not know is an empty list, not an error that reads as "no answer")
        r = _answer("authors?filter=openalex:%s&select=%s" % (q.upper(), SELECT_AUTHOR))
        return [_candidate(a) for a in r.get("results") or []]
    words = clean(q)
    if not words:
        return []
    r = _answer("authors?search=%s&per-page=15&select=%s" % (urllib.parse.quote(words), SELECT_AUTHOR))
    return [_candidate(a) for a in r.get("results") or []]


SELECT_WORK = "id,doi,display_name,publication_date,publication_year,type,primary_location,best_oa_location,authorships"


def works(ids, progress=None, stop=None):
    """All works of these OpenAlex author ids (of the types asked), page by page. Returns (works, complete, total): complete is False when a page did not come
    (the daily allowance, the network) or the list is longer than MAX_PAGES pages; what came is returned either way."""
    ids = [i for i in (_short(x).upper() for x in ids) if re.fullmatch(r"A\d{4,12}", i)]
    if not ids:
        return [], True, 0
    flt = "author.id:%s,type:%s" % ("|".join(ids), TYPES)
    out, cursor, total = [], "*", 0
    for _ in range(MAX_PAGES):
        if stop is not None and stop.is_set():
            return out, False, total
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


def tkey(t):
    """A title as the joins here compare it: letters and digits of any script (a title in Chinese or Greek is not the empty string), case and accents folded."""
    s = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", str(t or "")).casefold())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[\W_]+", " ", s).strip()


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
    published = {tkey(_title(w)) for w in kept if w["_kind"] != "working"} - {""}
    out = []
    for w in kept:
        if w["_kind"] == "working" and tkey(_title(w)) in published:
            excluded["working paper (published version listed)"] = excluded.get("working paper (published version listed)", 0) + 1
            continue
        out.append(w)
    return out, excluded


def to_papers(ws, fid, label):
    """Paper dicts for a run. The journal and its download route: a watched journal recognised by ISSN keeps its settings; any other gets the route of its
    DOI's publisher (fetch_new.venue_route). No Crossref call per paper (an author can have hundreds)."""
    journals = dl.load_json(os.path.join(dl.BASE, "journals.json"), {"journals": []})["journals"]
    out, ids = [], set()
    for w in ws:
        doi, title, src = _doi(w), _title(w), _source(w)
        meta = {"issn": src.get("issn") or ([src["issn_l"]] if src.get("issn_l") else []), "container": src.get("display_name") or ""}
        name, abbr, publisher = fx.venue_route(meta, src.get("display_name") or "", doi, journals)
        oa = (w.get("best_oa_location") or {}).get("pdf_url") or ""
        url = ("https://doi.org/" + doi) if doi else (oa or ((w.get("primary_location") or {}).get("landing_page_url") or ""))
        date = str(w.get("publication_date") or "")[:10] or ("%s-01-01" % w["publication_year"] if w.get("publication_year") else "")
        authors_ = [((a.get("author") or {}).get("display_name") or "") for a in (w.get("authorships") or [])]
        pid = fx.key_for(doi, name, title)
        if pid in ids:              # (two records without a DOI, same title, same venue: one paper for the run, which keys papers by this id)
            continue
        ids.add(pid)
        out.append({"id": pid, "title": title, "authors": [a for a in authors_ if a], "doi": doi, "url": url, "journal": name, "abbr": abbr,
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
    """Is this index row this paper? The same DOI; or the same title when one side has no DOI, or when the row is a published version and the paper is its preprint
    (SSRN, arXiv…): a published paper you have, or deleted, keeps its preprint away. Never the other way round: a preprint you have, or deleted, does not stop
    the published version (you may have been waiting for it)."""
    d1, d2 = (row.get("doi") or "").strip().lower(), (p.get("doi") or "").strip().lower()
    if d1 and d2 and d1 == d2:
        return True
    t1, t2 = tkey(row.get("title") or ""), tkey(p.get("title") or "")
    if not t1 or t1 != t2:
        return False
    return not (d1 and d2) or (is_preprint_doi(d2) and not is_preprint_doi(d1))


def split(papers):
    """(new, have, deleted). have: [(paper, index key of the library row)] for papers already in the library (their file exists); deleted: papers you deleted."""
    import deletion
    rows = _index_rows()
    by_doi, by_title = {}, {}
    for r in rows:
        if (r.get("doi") or "").strip():
            by_doi.setdefault(r["doi"].strip().lower(), []).append(r)
        by_title.setdefault(tkey(r.get("title") or ""), []).append(r)
    new, have, gone = [], [], []
    for p in papers:
        cands = by_doi.get((p.get("doi") or "").lower(), []) + (by_title.get(tkey(p["title"]), []) if tkey(p["title"]) else [])
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


def _fkey(s):
    """A folder name as folders.create compares it (spaces collapsed, case ignored)."""
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


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
        names = {_fkey(f["name"]) for f in fo.list_all()}
        base = name if _fkey(name) not in names else ("%s · %s" % (name, institution.strip()[:40]) if institution.strip() else name)
        f, n = None, 1
        while f is None:
            suffix = "" if n == 1 else " (%d)" % n
            cand = base[:60 - len(suffix)].rstrip() + suffix          # (as folders.create keeps it: at most 60 characters)
            n += 1
            if _fkey(cand) in names:
                continue
            try:
                f = fo.create(cand)
            except ValueError:            # (made meanwhile, or the same name once cut to 60 characters: the next number)
                names.add(_fkey(cand))
        if rec:
            rec.update(ids=sorted(set(rec.get("ids") or []) | set(ids)), folder=f["id"], name=name, filed=[])       # (a new folder: filled again)
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


def file_new(rec_folder, key):
    """A paper of an author run is in the library: into the author's folder, unless it was filed there before (then you may have taken it out). A folder with
    no author record (made by hand) just gets it."""
    import folders as fo
    with _lock:
        d = load()
        rec = next((r for r in d["authors"] if r.get("folder") == rec_folder), None)
        if rec is not None and key in (rec.get("filed") or []):
            return False
        fo.add(rec_folder, [key])
        if rec is not None:
            rec["filed"] = sorted(set(rec.get("filed") or []) | {key})
            _save(d)
        return True



# ---------------------------------------------------------------- following an author (the weekly check)

def _rec(d, fid):
    return next((r for r in d["authors"] if r.get("folder") == fid), None)


def set_follow(fid, on):
    """Follow (or stop following) the author whose folder is fid. Returns the record, or None when there is none."""
    from datetime import date
    with _lock:
        d = load()
        rec = _rec(d, fid)
        if rec is None:
            return None
        rec["follow"] = bool(on)
        if on:
            rec.setdefault("follow_on", date.today().isoformat())
            rec["follow_warn"] = ""
        _save(d)
        return dict(rec)


def followed():
    """Copies of the records being followed, read from the file now (no cache)."""
    with _lock:
        return [dict(r) for r in load()["authors"] if r.get("follow") and r.get("folder")]


def follow_mark(fid, wids, seed=False):
    """Add these OpenAlex work ids to what was dealt with for this author (a union: nothing is ever taken out), note today as the last good listing and clear any
    warning. seed=True writes follow_known even when wids is empty (the Follow button's listing: from now on only newer works are new)."""
    from datetime import date
    ws = {_short(w).upper() for w in wids if _short(w)}
    with _lock:
        d = load()
        rec = _rec(d, fid)
        if rec is None:
            return False
        if ws or seed or "follow_known" in rec:
            rec["follow_known"] = sorted(set(rec.get("follow_known") or []) | ws)
        rec["follow_last"] = date.today().isoformat()
        rec["follow_warn"] = ""
        _save(d)
        return True


def set_follow_warn(fid, msg):
    with _lock:
        d = load()
        rec = _rec(d, fid)
        if rec is not None:
            rec["follow_warn"] = str(msg or "")[:300]
            _save(d)


def add_ids(fid, ids):
    with _lock:
        d = load()
        rec = _rec(d, fid)
        if rec is not None:
            rec["ids"] = sorted(set(rec.get("ids") or []) | {_short(i).upper() for i in ids if _short(i)})
            _save(d)


def resolve(ids):
    """The author ids as OpenAlex has them now: an id that OpenAlex merged into another answers with the other (the lookup follows it). None when OpenAlex did
    not answer (the daily allowance, the network): nothing can be said then."""
    out, answered = set(), False
    for i in ids:
        i = _short(i).upper()
        if not re.fullmatch(r"A\d{4,12}", i):
            continue
        r = _fetch("authors/%s?select=id" % i, missing={})
        if r is None:
            continue
        answered = True
        if r.get("id"):                     # (OpenAlex knows it: as itself, or as the id it was merged into; {} = it knows no such author any more)
            out.update({i, _short(r["id"]).upper()})
    return sorted(out) if answered else None


def saved():
    """The authors with a folder, for the Authors tab: [{folder, folder_name ("" when it was deleted), name, ids, follow, follow_last, follow_warn, known}]. A deleted
    folder is listed only with the note that paused following."""
    import folders as fo
    with _lock:
        recs = [dict(r) for r in load()["authors"] if r.get("folder")]
    out = []
    for r in recs:
        f = fo.get(r["folder"])
        if not f and not r.get("follow_warn"):            # (a folder you deleted is listed only when following it was paused because of that: the note says so)
            continue
        out.append({"folder": r["folder"], "folder_name": (f or {}).get("name", ""), "name": r.get("name", ""), "ids": r.get("ids") or [], "follow": bool(r.get("follow")),
                    "follow_last": r.get("follow_last") or "", "follow_warn": r.get("follow_warn") or "", "known": len(r.get("follow_known") or [])})
    return sorted(out, key=lambda r: r["name"].lower())
