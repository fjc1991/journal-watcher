"""Add papers yourself: a PDF you drop onto the Library, or a DOI. They are filed exactly where the pipeline files its
downloads (Papers/YYYY-MM/<Journal>/<ym>_<ABBR>_<doi>_<slug>.pdf) and get a row in _index.csv, so the Library, the reader and
the summaries treat them like any other paper.

    importer.add_pdf(data, filename)  -> {"file", "title", "existing": bool, "note"}
    importer.add_doi("10.1287/isre.2024.0984")                       (needs a free copy: Unpaywall, arXiv, SSRN, ...)
    importer.analyse_pdf(data, filename) -> what the PDF is, by its own DOI and title: records from Crossref, each checked against the PDF (nothing is filed;
                                            the PDF waits in state/staging/ under a token)
    importer.confirm(token, fields)      -> files the staged PDF with the details you confirmed or edited ({"file", "title", "existing", "note"})
    importer.discard(tokens)             -> forgets staged PDFs
Raises ValueError with a message meant for the user.
"""
import json
import os
import re
import sys
import time
import urllib.parse
import uuid
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import download as dl  # noqa: E402
import fetch_new as fx  # noqa: E402
import identity  # noqa: E402

DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)")


def clean_doi(s):
    s = urllib.parse.unquote(str(s or "")).strip()
    s = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", s, flags=re.I)
    m = DOI_RE.search(s)
    return m.group(1).rstrip(".,;)]") if m else ""


def crossref_meta(doi):
    """Full bibliographic record from Crossref, or None."""
    if os.environ.get("JW_OFFLINE"):          # tests and offline work: no scholarly-database lookups
        return None
    try:
        raw, _ = dl.http_get("https://api.crossref.org/works/" + urllib.parse.quote(doi, safe="/"), timeout=30,
                             headers={"Accept": "application/json"})
        it = json.loads(raw.decode("utf-8", "replace")).get("message")
    except Exception:
        return None
    return _meta_from_item(it, doi)


def _meta_from_item(it, doi=""):
    """The fields we keep of a Crossref record, or None for one without a title."""
    if not isinstance(it, dict) or not it.get("title"):
        return None
    authors = [" ".join(x for x in [a.get("given"), a.get("family")] if x) or a.get("name", "") for a in it.get("author", [])]
    return {"title": re.sub(r"\s+", " ", fx.strip_html(it["title"][0])).strip(), "authors": [a for a in authors if a],
            "container": (it.get("container-title") or [""])[0], "issn": it.get("ISSN") or [], "doi": it.get("DOI", doi),
            "pub_date": fx.fmt_date_parts((it.get("published") or it.get("issued") or {}).get("date-parts")),
            "url": it.get("URL") or "https://doi.org/" + (it.get("DOI") or doi), "volume": it.get("volume", ""), "issue": it.get("issue", ""),
            "pages": it.get("page", ""), "publisher": it.get("publisher", ""), "type": it.get("type", ""),
            "abstract": fx.cr_strip_jats(it.get("abstract")), "keywords": it.get("subject") or []}


def crossref_search(title, rows=4):
    """The records Crossref finds for a title (best first), as crossref_meta gives them; [] when nothing is found or it cannot be reached.
    Only the title is sent."""
    if os.environ.get("JW_OFFLINE") or len(str(title or "").strip()) < 8:
        return []
    try:
        raw, _ = dl.http_get("https://api.crossref.org/works?query.bibliographic=%s&rows=%d" % (urllib.parse.quote(str(title)[:250]), rows), timeout=30,
                             headers={"Accept": "application/json"})
        items = json.loads(raw.decode("utf-8", "replace")).get("message", {}).get("items") or []
    except Exception:
        return []
    return [m for m in (_meta_from_item(it) for it in items) if m and m.get("doi")]


def journal_for(meta):
    """(name, abbr) — one of the watched journals when the ISSN or DOI prefix matches, else the journal's own name."""
    for j in dl.load_json(os.path.join(dl.BASE, "journals.json"), {"journals": []})["journals"]:
        if (j.get("issn") and j["issn"] in meta.get("issn", [])) or (j.get("doi_prefix") and meta["doi"].startswith(j["doi_prefix"] + "/")):
            return j["name"], j["abbr"]
    name = re.sub(r"[\\/:*?\"<>|]+", " ", meta.get("container") or "").strip() or "Other"
    abbr = "".join(w[0] for w in re.findall(r"[A-Za-z][A-Za-z]+", name) if w[0].isupper() and w.lower() not in ("of", "the", "and"))[:6].upper()
    return name, abbr or "OTH"


def _paper(meta):
    name, abbr = journal_for(meta)
    pd = meta.get("pub_date") or ""
    if re.fullmatch(r"\d{4}", pd):
        pd += "-01"
    return {"title": meta["title"], "authors": meta.get("authors", []), "doi": meta["doi"], "url": meta.get("url", ""),
            "journal": name, "abbr": abbr, "pub_date": pd or datetime.now().strftime("%Y-%m-%d"), "topics": []}


def _existing(p):
    """Relative file path if this paper is already in the library."""
    key = dl.index_key({"doi": p.get("doi"), "title": p.get("title")})
    idx = os.path.join(dl.PAPERS, "_index.csv")
    if os.path.exists(idx):
        import csv
        with open(idx, newline="") as f:
            for r in csv.DictReader(f):
                if r["status"] == "ok" and r["file"] and dl.index_key(r) == key and os.path.exists(os.path.join(dl.ROOT, r["file"])):
                    return r["file"]
    return None


def _file(p, data, note):
    ex = _existing(p)
    if ex:
        return {"file": ex, "title": p["title"], "existing": True, "note": "already in your library"}
    dest = dl.out_path(p)
    tmp = dest + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dest)
    rel = os.path.relpath(dest, dl.ROOT)
    dl.save_index(p, rel, "ok", note)
    return {"file": rel, "title": p["title"], "existing": False, "note": note}


def _sniff(data):
    """(doi, title) guessed from the PDF itself: first pages' text for a DOI, document info / largest type for the title."""
    import pymupdf
    doi = title = ""
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        text = "".join(doc[i].get_text() for i in range(min(2, len(doc))))
        m = DOI_RE.search(text)
        doi = m.group(1).rstrip(".,;)]") if m else ""
        title = (doc.metadata or {}).get("title") or ""
        if len(title.strip()) < 8 or title.lower().endswith((".pdf", ".docx", ".tex")):
            best = (0, "")
            for b in doc[0].get_text("dict")["blocks"]:
                for ln in b.get("lines", []):
                    for sp in ln["spans"]:
                        if sp["size"] > best[0] and len(sp["text"].strip()) > 6:
                            best = (sp["size"], sp["text"].strip())
            title = best[1]
    return doi, re.sub(r"\s+", " ", title).strip()


def add_pdf(data, filename="paper.pdf", doi=""):
    if not dl.is_pdf(data):
        raise ValueError("That doesn’t look like a complete PDF.")
    sniffed, title = _sniff(data)
    doi = clean_doi(doi) or sniffed
    meta = crossref_meta(doi) if doi else None
    if meta:
        p, note = _paper(meta), "added by you (metadata from Crossref)"
    else:
        title = title or re.sub(r"[_-]+", " ", os.path.splitext(os.path.basename(filename))[0]).strip() or "Untitled paper"
        p = {"title": title, "authors": [], "doi": doi, "url": "https://doi.org/" + doi if doi else "", "journal": "Other", "abbr": "OTH",
             "pub_date": datetime.now().strftime("%Y-%m-%d"), "topics": []}
        note = "added by you (no metadata found: check the title)"
    return _file(p, data, note)


def add_doi(doi):
    doi = clean_doi(doi)
    if not doi:
        raise ValueError("Paste a DOI such as 10.1287/isre.2024.0984 or a doi.org link.")
    meta = crossref_meta(doi)
    if not meta:
        raise ValueError("Crossref doesn’t know that DOI (or can’t be reached).")
    p = _paper(meta)
    ex = _existing(p)
    if ex:
        return {"file": ex, "title": p["title"], "existing": True, "note": "already in your library"}
    data, note, refused = None, "", ""
    url = dl.try_unpaywall(doi, dl.load_env().get("UNPAYWALL_EMAIL"))
    if url:
        try:
            raw, _ = dl.http_get(url, timeout=60)
            if dl.is_pdf(raw):
                verdict, why, _ = identity.check(p, raw)
                if verdict != "mismatch":
                    data, note = raw, "added by DOI (open access, Unpaywall)"
                else:
                    refused = "wrong paper via Unpaywall: %s" % why
        except Exception:
            pass
    if not data:
        import open_copies
        raw, label = open_copies.find_open_copy(doi, p["title"], dl.http_get, authors=p.get("authors"))
        if raw:
            data, note = raw, "added by DOI (%s)" % label
        elif label.startswith("wrong paper"):
            refused = refused or label
    if not data:
        raise ValueError("No free copy of “%s” was found%s. Download the PDF through your library and drop it onto the Library page — "
                         "the DOI will be recognised." % (p["title"][:80], " (a PDF came back, but it is not this paper: %s)" % refused.split(": ", 1)[-1] if refused else ""))
    return _file(p, data, note)


# ---------------------------------------------------------------- review before filing: analyse, then confirm
# A dropped PDF is first ANALYSED: what the PDF says about itself (the DOIs printed in its text and metadata, its title), the records Crossref has for those DOIs and for that
# title, and each record checked against the PDF (identity.check by title and authors, never by the DOI the record was found with: that DOI came from the PDF itself and
# would prove nothing). Nothing is filed. The PDF waits in state/staging/ under a random token; you check or correct the details, and CONFIRM files it.
STAGING = os.path.join(dl.STATE, "staging")
STAGE_HOURS = 24
TOKEN_RE = re.compile(r"[0-9a-f]{32}")


def _staged_path(token):
    return os.path.join(STAGING, token + ".pdf") if isinstance(token, str) and TOKEN_RE.fullmatch(token) else None


def _stage(data):
    os.makedirs(STAGING, exist_ok=True)
    cut = time.time() - STAGE_HOURS * 3600
    for n in os.listdir(STAGING):                      # PDFs nobody confirmed or cancelled are forgotten after a day
        p = os.path.join(STAGING, n)
        try:
            if os.path.getmtime(p) < cut:
                os.remove(p)
        except OSError:
            pass
    token = uuid.uuid4().hex
    tmp = os.path.join(STAGING, token + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, _staged_path(token))
    return token


def discard(tokens):
    """Forget staged PDFs (Cancel, or papers you unticked). Returns how many were removed."""
    n = 0
    for t in tokens or []:
        p = _staged_path(t)
        if p and os.path.exists(p):
            try:
                os.remove(p); n += 1
            except OSError:
                pass
    return n


def _read(data):
    """What a PDF says about itself: {"dois": [...], "title": str, "pages": n, "scan": bool}. The DOIs come from the text before its reference list (a paper cites other papers'
    DOIs) and from its metadata, in the order they appear."""
    r = identity.read_pdf(data)
    seen, dois = set(), []
    for src in (r["text"], r["meta"]):
        for m in DOI_RE.finditer(src):
            d = m.group(1).rstrip(".,;)]")
            if d.lower() not in seen:
                seen.add(d.lower()); dois.append(d)
    title = r["meta_title"] if len(r["meta_title"]) >= 8 and not r["meta_title"].lower().endswith((".pdf", ".docx", ".tex", ".doc")) else r["headline"]
    return {"dois": dois[:4], "title": re.sub(r"\s+", " ", title or "").strip(), "pages": r["pages_read"], "scan": bool(r["pages_read"] and r["scans"] * 2 >= r["pages_read"] and r["words"] < 60)}


def _candidate(meta, source, data, printed):
    """One record, with what the PDF says about it."""
    verdict, reason, ev = identity.check({"title": meta["title"], "doi": "", "authors": meta.get("authors") or []}, data)
    pd = meta.get("pub_date") or ""
    return {"title": meta["title"], "authors": meta.get("authors") or [], "journal": meta.get("container") or "", "pub_date": pd, "year": pd[:4], "doi": meta.get("doi") or "",
            "url": meta.get("url") or "", "source": source, "doi_printed": printed, "verdict": verdict, "reason": reason, "title_score": ev.get("title") or 0.0}


def analyse_pdf(data, filename="paper.pdf"):
    """Look at a PDF and stage it; files nothing. Returns {"token", "name", "pdf": {title, dois, pages, scan}, "candidates": [up to 3 records, best first], "pick": index of the
    best one or -1, "confidence": "high" (the title, and the authors if known, are on the PDF's first pages) | "check" | "none", "existing": path of the paper if you already have it}."""
    if not dl.is_pdf(data):
        raise ValueError("That doesn’t look like a complete PDF.")
    info = _read(data)
    guess = info["title"] or re.sub(r"\s*\(\d+\)$", "", re.sub(r"\s+", " ", re.sub(r"[_-]+", " ", os.path.splitext(os.path.basename(filename))[0])).strip())      # (the "(3)" a browser adds to a second download is not part of the title)
    cands, seen = [], set()

    def add(meta, source):
        key = (meta.get("doi") or "").lower() or fx.norm_title(meta["title"])
        if not key or key in seen:
            return
        seen.add(key)
        cands.append(_candidate(meta, source, data, (meta.get("doi") or "").lower() in {d.lower() for d in info["dois"]}))
    for d in info["dois"][:3]:
        m = crossref_meta(d)
        if m:
            add(m, "doi")
    if not any(c["verdict"] == "match" for c in cands):         # no printed DOI led to this paper: search by the title
        for m in crossref_search(guess):
            add(m, "title")
    cands.sort(key=lambda c: (c["verdict"] == "match", c["verdict"] != "mismatch", c["doi_printed"], c["title_score"]), reverse=True)
    cands = cands[:3]
    pick = 0 if cands and cands[0]["verdict"] != "mismatch" else -1
    top = cands[pick] if pick >= 0 else {"doi": "", "title": guess}
    ex = _existing({"doi": top.get("doi"), "title": top.get("title")})
    return {"token": _stage(data), "name": filename, "pdf": {"title": guess, "dois": info["dois"], "pages": info["pages"], "scan": info["scan"]}, "candidates": cands, "pick": pick,
            "confidence": "none" if not cands else "high" if pick == 0 and cands[0]["verdict"] == "match" else "check", "existing": ex or ""}


def _clean_text(v, limit):
    return re.sub(r"\s+", " ", str(v or "")).strip()[:limit]


def confirm(token, fields):
    """File a staged PDF with the details you confirmed or corrected. fields: title (required), authors (a list or "A; B"), journal, pub_date (YYYY, YYYY-MM or YYYY-MM-DD), doi,
    source ("crossref" = a record taken as it is, "edited" = a record you changed, "hand" = typed by you). Raises ValueError with a message for the user."""
    path = _staged_path(token)
    if not path or not os.path.exists(path):
        raise ValueError("That PDF is no longer waiting to be added: drop it again.")
    f = fields if isinstance(fields, dict) else {}
    title = _clean_text(f.get("title"), 500)
    if not title:
        raise ValueError("The title is empty: give the paper a title.")
    authors = f.get("authors")
    authors = re.split(r"\s*;\s*", str(authors or "")) if not isinstance(authors, list) else authors
    authors = [a for a in (_clean_text(x, 120) for x in authors[:100]) if a]
    doi = clean_doi(f.get("doi")) if str(f.get("doi") or "").strip() else ""
    date = _clean_text(f.get("pub_date"), 10)
    if not re.fullmatch(r"\d{4}(-\d{2}(-\d{2})?)?", date):
        date = ""
    meta = {"title": title, "authors": authors, "doi": doi, "url": "https://doi.org/" + doi if doi else "", "container": _clean_text(f.get("journal"), 120), "issn": [], "pub_date": date}
    note = {"crossref": "added by you (metadata from Crossref, confirmed by you)", "edited": "added by you (metadata from Crossref, edited by you)"}.get(f.get("source"), "added by you (details typed by you)")
    with open(path, "rb") as fh:
        data = fh.read()
    res = _file(_paper(meta), data, note)
    discard([token])
    return res
