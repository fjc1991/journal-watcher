"""A paper's details, after it is in the library: what Edit details shows and saves, and reading them again from the PDF.

    details.get(file)                  everything known of one paper: the index row's title, authors, DOI, venue and year, and the details kept beside the PDF
    details.save(file, fields)         your corrections
    details.read_again(file, use_ai)   what the PDF's first page says now (pdfmeta): a proposal; nothing is written

Where a correction is kept. Papers/_index.csv names each paper by its DOI, or by its title when it has none: folders, reading status, comparison tables and runs all
know a paper by that. So a corrected title, author list or DOI is NOT written into the index (that would cut the paper off from its folders and its reading status):
it is kept in <stem>.paper.json beside the PDF with the other details, and the library shows it in place of the index's (pipeline._lib_row). The index keeps the title
the paper was filed under.
"""
import os
import re
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import download as dl  # noqa: E402
import kinds  # noqa: E402
import pdfmeta  # noqa: E402

_lock = threading.RLock()
SHOWN = ("title", "doi")                 # corrections shown in place of the index's


def _row(file):
    import pipeline as pl
    r = pl.library_row(str(file or ""))
    if not r or r.get("kind") == "book":
        raise ValueError("That paper is not in your library.")
    return r


def _pdf(file):
    import notes_store as ns
    pdf = ns.pdf_path(file)
    if not pdf or not os.path.exists(pdf):
        raise ValueError("That paper’s PDF is not in your library.")
    return pdf


def _names(s):
    return [a for a in (re.sub(r"\s+", " ", x).strip() for x in re.split(r"\s*;\s*", str(s or ""))) if a]


def shown(row, det):
    """A library row with the corrections kept beside its PDF in place: the title, the authors (when the details list any) and a DOI the index does not have."""
    det = det if isinstance(det, dict) else {}
    if det.get("title"):
        row["title"] = det["title"]
    names = [a.get("name") for a in det.get("authors") or [] if isinstance(a, dict) and a.get("name")]
    if names:
        row["authors"] = "; ".join(names)
    if det.get("doi") and not row.get("doi"):
        row["doi"] = det["doi"]
        row["url"] = row.get("url") or "https://doi.org/" + det["doi"]
    if det.get("session"):
        row["session"] = det["session"]
    return row


def clean(fields, row=None):
    """The details to keep, from what a dialog sent: pdfmeta.clean's, with the author list kept whenever it is given (also without affiliations: it is the correction
    of the names), and a corrected title and DOI."""
    f = fields if isinstance(fields, dict) else {}
    if isinstance(f.get("authors"), str):
        f = dict(f, authors=[{"name": n} for n in _names(f["authors"])])
    out = pdfmeta.clean(f, keep_names=True)
    title = re.sub(r"\s+", " ", str(f.get("title") or "")).strip()[:500]
    if title and (row is None or title != (row.get("_index_title") or "")):
        out["title"] = title
    doi = str(f.get("doi") or "").strip()
    if doi:
        import importer
        doi = importer.clean_doi(doi)
        if not re.fullmatch(r"10\.\d{4,9}/\S+", doi):
            raise ValueError("That does not look like a DOI (it begins 10.…).")
        out["doi"] = doi
    return out


def get(file):
    """{file, title, authors: [names], doi, journal, kind, kind_label, year, index: {title, authors, doi} (as the paper was filed), details: {...}}"""
    r = _row(file)
    pdf = _pdf(file)
    side = kinds.read_sidecar(pdf, r.get("key"))
    det = dict(side.get("details") or {})
    idx = _index_row(file)
    people = det.get("authors") or [{"name": n, "affiliation": "", "place": "", "email": ""} for n in _names(r.get("authors"))]
    return {"file": r["file"], "title": r.get("title") or "", "authors": [a["name"] for a in people], "people": people, "doi": r.get("doi") or "", "journal": r.get("journal") or "",
            "kind": r.get("kind") or "journal", "kind_label": r.get("kind_label") or "", "year": r.get("year") or "",
            "index": {"title": idx.get("title") or "", "authors": idx.get("authors") or "", "doi": idx.get("doi") or ""},
            "details": {k: v for k, v in det.items() if k not in ("authors", "title", "doi")}}


def _index_row(file):
    import importer
    return next((r for r in importer.library_rows() if r.get("file") == file and r.get("status") == "ok"), {})


def save(file, fields):
    """Keep your corrections of one paper: title, authors (a list of {name, affiliation, place, email}, or "A; B"), doi (only for a paper filed without one), abstract,
    keywords, conference, paper_type, session, volume, issue, pages, note. Raises ValueError with words for the user. Returns details.get(file)."""
    f = fields if isinstance(fields, dict) else {}
    with _lock:
        r = _row(file)
        pdf = _pdf(file)
        idx = _index_row(file)
        if not re.sub(r"\s+", " ", str(f.get("title") if "title" in f else r.get("title") or "")).strip():
            raise ValueError("The title is empty: give the paper a title.")
        if str(f.get("doi") or "").strip() and idx.get("doi") and str(f.get("doi")).strip().lower() != idx["doi"].lower():
            raise ValueError("This paper was filed under the DOI %s, which names it in your folders and reading status: it cannot be changed here." % idx["doi"])
        side = kinds.read_sidecar(pdf, r.get("key"))
        was = dict(side.get("details") or {})
        new = clean(dict(f, doi="" if idx.get("doi") else f.get("doi")), {"_index_title": idx.get("title") or ""})
        if "title" not in f and was.get("title"):
            new["title"] = was["title"]
        for k in ("authors", "keywords", *pdfmeta.FIELDS):                 # a field the dialog did not send stays as it was
            if k not in f and k in was:
                new[k] = was[k]
        if "doi" not in f and was.get("doi"):
            new["doi"] = was["doi"]
        if new.get("doi") and not idx.get("doi"):
            import importer
            other = importer.check_existing(new["doi"], "")
            if other and other != file:
                raise ValueError("Another paper in your library already has that DOI.")
        kind, label = kinds.of_row(r, side)
        kinds.write_sidecar(pdf, kind if kind in kinds.PAPER_KINDS else "journal", side.get("venue") or r.get("journal") or "", label, r.get("key") or dl.index_key(idx),
                            side.get("crossref_type") or "", new or {})
    return get(file)


def read_again(file, use_ai=False):
    """What the PDF's first page says, beside what the library has: {file, title, have: get(file), found: {title, authors, people, abstract, keywords, conference, year,
    paper_type, venue, kind, sure, by, ai, ai_note}, changes: [the fields where the page says something the library lacks or says otherwise]}. Nothing is written."""
    import importer
    have = get(file)
    with open(_pdf(file), "rb") as fh:
        data = fh.read()
    got, note = pdfmeta.read(data), ""
    if use_ai and pdfmeta.needs_ai(got):
        try:
            got, note = pdfmeta.with_ai(data, got)
        except ImportError:
            note = "The AI backend isn’t installed."
    found = dict(importer.page_details(got, note), title=got.get("title") or "", venue=got.get("venue") or "", kind=got.get("kind"))
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()        # noqa: E731
    changes = []
    if found["title"] and norm(found["title"]) != norm(have["title"]):
        changes.append("title")
    had = {norm(a["name"]): a for a in have["people"]}
    if found["people"] and ([norm(a["name"]) for a in found["people"]] != [norm(a["name"]) for a in have["people"]]
                            or any(a.get(k) and not (had.get(norm(a["name"])) or {}).get(k) for a in found["people"] for k in ("affiliation", "place", "email"))):
        changes.append("authors")
    for k in ("abstract", "keywords", "conference", "paper_type"):
        if found.get(k) and not have["details"].get(k):
            changes.append(k)
    return {"file": file, "title": have["title"], "have": have, "found": found, "changes": changes}
