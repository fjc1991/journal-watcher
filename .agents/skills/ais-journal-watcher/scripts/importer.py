"""Add papers yourself: a PDF you drop onto the Library, or a DOI. They are filed exactly where the pipeline files its
downloads (Papers/YYYY-MM/<Journal>/<ym>_<ABBR>_<doi>_<slug>.pdf) and get a row in _index.csv, so the Library, the reader and
the summaries treat them like any other paper.

    importer.add_pdf(data, filename)  -> {"file", "title", "existing": bool, "note"}
    importer.add_doi("10.1287/isre.2024.0984")                       (needs a free copy: Unpaywall, arXiv, SSRN, ...)
Raises ValueError with a message meant for the user.
"""
import json
import os
import re
import sys
import urllib.parse
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
    if not isinstance(it, dict) or not it.get("title"):
        return None
    authors = [" ".join(x for x in [a.get("given"), a.get("family")] if x) or a.get("name", "") for a in it.get("author", [])]
    return {"title": re.sub(r"\s+", " ", fx.strip_html(it["title"][0])).strip(), "authors": [a for a in authors if a],
            "container": (it.get("container-title") or [""])[0], "issn": it.get("ISSN") or [], "doi": it.get("DOI", doi),
            "pub_date": fx.fmt_date_parts((it.get("published") or it.get("issued") or {}).get("date-parts")),
            "url": it.get("URL") or "https://doi.org/" + doi, "volume": it.get("volume", ""), "issue": it.get("issue", ""),
            "pages": it.get("page", ""), "publisher": it.get("publisher", ""), "type": it.get("type", ""),
            "abstract": fx.cr_strip_jats(it.get("abstract")), "keywords": it.get("subject") or []}


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
    data, note = None, ""
    url = dl.try_unpaywall(doi, dl.load_env().get("UNPAYWALL_EMAIL"))
    if url:
        try:
            raw, _ = dl.http_get(url, timeout=60)
            if dl.is_pdf(raw) and identity.check(p, raw)[0] != "mismatch":
                data, note = raw, "added by DOI (open access, Unpaywall)"
        except Exception:
            pass
    if not data:
        import open_copies
        raw, label = open_copies.find_open_copy(doi, p["title"], dl.http_get, authors=p.get("authors"))
        if raw:
            data, note = raw, "added by DOI (%s)" % label
    if not data:
        raise ValueError("No free copy of “%s” was found. Download the PDF through your library and drop it onto the Library page — "
                         "the DOI will be recognised." % p["title"][:80])
    return _file(p, data, note)
