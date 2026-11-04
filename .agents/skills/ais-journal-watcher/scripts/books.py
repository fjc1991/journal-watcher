"""Books: PDF and EPUB files you own, kept in the library next to your papers.

A book is a library row like a paper, filed under the reserved journal "Books" (Papers/YYYY-MM/Books/…): so the reader, notes, highlights, folders, tags, search and Ask all work on it
with no special case. What is special lives in the files beside it:

    <stem>.pdf        what the reader shows. A PDF book is the file itself; an EPUB is converted to a PDF (its chapters become the PDF's outline)
    <stem>.epub       the EPUB you gave (kept as it was)
    <stem>.book.json  {"isbn", "publisher", "edition", "source": "pdf"|"epub", "pages", "offset", "toc": [[level, title, page], ...]}

    books.is_epub(data) / books.read_epub(data)       the file's own details (title, authors, ISBN, publisher, year) and whether it is copy-protected
    books.analyse(data, filename, hint_pdf=False)     look at a dropped book and stage it; files nothing (same idea as importer.analyse_pdf)
    books.confirm(token, fields)                      file a staged book with the details you confirmed (importer.confirm sends fields with kind "book" here)

A book protected by DRM (a Kindle or Adobe-locked file) cannot be read and is refused with a plain message. Lookups: Open Library (by ISBN, else by title) and Crossref (books with a DOI).
"""
import io
import json
import os
import re
import sys
import urllib.parse
import zipfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import download as dl  # noqa: E402
import fetch_new as fx  # noqa: E402
import identity  # noqa: E402
import importer  # noqa: E402
import notes_store as ns  # noqa: E402

JOURNAL, ABBR = "Books", "BOOK"
MAX_UNPACKED = 3 * 1024 ** 3                  # an EPUB that would unpack to more than this is refused (a zip bomb)
PAGE = (480, 720)                              # the page of the PDF made from an EPUB (points), and its type size: a comfortable book page on screen
EM = 12
FONT_OK = ("http://www.idpf.org/2008/embedding", "http://ns.adobe.com/pdf/enc#RC")        # EPUB font obfuscation: not DRM


def is_epub(data):
    """True for an EPUB: a zip whose first entry is the 'mimetype' file."""
    if not (isinstance(data, (bytes, bytearray)) and data[:4] == b"PK\x03\x04"):
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return "mimetype" in z.namelist() and z.read("mimetype").strip() == b"application/epub+zip"
    except (zipfile.BadZipFile, OSError, KeyError, RuntimeError):
        return False


def isbn_ok(s):
    """The digits of an ISBN-10 or ISBN-13 with a correct check digit, else ''."""
    d = re.sub(r"[\s-]", "", str(s or "")).upper()
    if re.fullmatch(r"\d{13}", d) and d[:3] in ("978", "979"):
        return d if (10 - sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(d[:12]))) % 10 == int(d[12]) else ""
    if re.fullmatch(r"\d{9}[\dX]", d):
        tot = sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(d))
        return d if tot % 11 == 0 else ""
    return ""


def isbn13(s):
    """ISBN-13 form of a valid ISBN (an ISBN-10 is converted), else ''."""
    d = isbn_ok(s)
    if len(d) == 10:
        core = "978" + d[:9]
        d = core + str((10 - sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(core)) % 10) % 10)
    return d


def find_isbn(text):
    """The first valid ISBN printed in a text (a PDF's first pages), written as 'ISBN 978-…' or 'ISBN-13: …'."""
    for m in re.finditer(r"ISBN(?:-1[03])?\s*[:#]?\s*((?:97[89][\s-]?)?\d[\d\s-]{8,15}[\dXx])", str(text or ""), re.I):
        d = isbn13(m.group(1))
        if d:
            return d
    return ""


def _unxml(s):
    s = re.sub(r"<[^>]+>", " ", str(s or ""))
    for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'"), ("&#39;", "'")):
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip()


def _flip(name):
    """'Lovelace, Ada' -> 'Ada Lovelace' (only the plain one-comma form; lists are left as they are)."""
    n = _unxml(name)
    if n.count(",") == 1 and ";" not in n and " and " not in n.lower() and "&" not in n:
        a, b = [x.strip() for x in n.split(",")]
        if a and b and not re.search(r"\b(jr|sr|ii|iii|phd)\b\.?$", b, re.I):
            return b + " " + a
    return n


def read_epub(data):
    """The EPUB's own details: {"title", "authors", "publisher", "date", "isbn", "language", "description"}. Raises ValueError (plain words) for a file that is not a readable EPUB or is copy-protected."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("That is not a readable EPUB file.")
    with z:
        if sum(i.file_size for i in z.infolist()) > MAX_UNPACKED:
            raise ValueError("That EPUB unpacks to far more than its size: it looks damaged, so it was not opened.")
        names = z.namelist()
        if "META-INF/encryption.xml" in names:
            algos = re.findall(r'Algorithm\s*=\s*"([^"]+)"', z.read("META-INF/encryption.xml").decode("utf-8", "replace"))
            if any(a not in FONT_OK for a in algos):
                raise ValueError("This EPUB is copy-protected (DRM), so it cannot be read here. Use a copy without protection.")
        opf = ""
        try:
            m = re.search(r'full-path\s*=\s*"([^"]+)"', z.read("META-INF/container.xml").decode("utf-8", "replace"))
            opf = m.group(1) if m else ""
        except KeyError:
            pass
        if opf not in names:
            opf = next((n for n in names if n.lower().endswith(".opf")), "")
        txt = z.read(opf).decode("utf-8", "replace") if opf else ""
    md = re.search(r"<(?:\w+:)?metadata\b.*?</(?:\w+:)?metadata>", txt, re.S | re.I)
    md = md.group(0) if md else txt

    def vals(tag):
        return [_unxml(v) for v in re.findall(r"<(?:\w+:)?%s\b[^>]*>(.*?)</(?:\w+:)?%s>" % (tag, tag), md, re.S | re.I) if _unxml(v)]
    ids = vals("identifier")
    return {"title": (vals("title") or [""])[0], "authors": [_flip(a) for a in vals("creator")][:30], "publisher": (vals("publisher") or [""])[0], "date": (vals("date") or [""])[0],
            "isbn": next((d for d in (isbn13(re.sub(r"^urn:isbn:", "", i, flags=re.I)) for i in ids) if d), ""), "language": (vals("language") or [""])[0], "description": (vals("description") or [""])[0][:600]}


def to_pdf(data):
    """(pdf bytes, toc, pages) for an EPUB: laid out on a book-sized page, chapters as the PDF's outline. Raises ValueError for a file that cannot be laid out."""
    import pymupdf
    try:
        doc = pymupdf.open(stream=data, filetype="epub")
        doc.layout(rect=pymupdf.Rect(0, 0, *PAGE), fontsize=EM)
        toc = doc.get_toc()
        raw = doc.convert_to_pdf()
        doc.close()
        out = pymupdf.open(stream=raw, filetype="pdf")
        if not len(out):
            raise ValueError("empty")
        if toc:
            out.set_toc(toc)
        pages = len(out)
        pdf = out.tobytes(garbage=3, deflate=True)
        out.close()
        return pdf, toc, pages
    except ValueError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ValueError("That EPUB could not be turned into pages (%s)." % str(e)[:80])


def _pdf_facts(data):
    """For a PDF book: {"title", "authors", "isbn", "pages", "toc", "scan"} from its metadata, outline and first pages."""
    import pymupdf
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        md = doc.metadata or {}
        text = "".join(doc[i].get_text() for i in range(min(8, len(doc))))
        toc = [[l, re.sub(r"\s+", " ", t).strip()[:200], max(1, p)] for l, t, p in doc.get_toc(simple=True) if p and p > 0][:2000]
        title = re.sub(r"\s+", " ", md.get("title") or "").strip()
        if len(title) < 4 or title.lower().endswith((".pdf", ".docx", ".tex", ".indd")):
            title = ""
        authors = [a.strip() for a in re.split(r"\s*(?:;| and |&)\s*", md.get("author") or "") if a.strip()][:30]
        return {"title": title, "authors": authors, "isbn": find_isbn(text), "pages": len(doc), "toc": toc, "scan": len(text.strip()) < 200 and len(doc) > 3}


def _year(s):
    m = re.search(r"\b(1[5-9]\d\d|20\d\d)\b", str(s or ""))
    return m.group(1) if m else ""


def _record(title, authors, publisher, year, isbn, doi, pages, source, url=""):
    return {"title": re.sub(r"\s+", " ", title or "").strip(), "authors": [a for a in (authors or []) if a], "publisher": publisher or "", "year": _year(year), "pub_date": _year(year),
            "isbn": isbn13(isbn) if isbn else "", "doi": doi or "", "pages": pages or 0, "source": source, "url": url or ("https://doi.org/" + doi if doi else "")}


def _ua():
    """Open Library asks the programs that use its API to say who they are: this one does (its name, version and project page)."""
    try:
        import updater
        v = updater.version()
    except Exception:  # noqa: BLE001
        v = ""
    return "JournalWatcher/%s (an open-source tool for reading academic papers; https://github.com/fjc1991/journal-watcher)" % (v or "dev")


def openlibrary_search(title="", authors=None, isbn="", rows=4):
    """Records Open Library has for an ISBN (if given) or a title (and first author). [] when nothing is found or it cannot be reached. Only the title/author/ISBN are sent."""
    if os.environ.get("JW_OFFLINE"):
        return []
    q = {"limit": rows, "fields": "key,title,subtitle,author_name,first_publish_year,publisher,isbn,number_of_pages_median"}
    if isbn:
        q["q"] = "isbn:" + isbn
    elif len(str(title or "").strip()) >= 4:
        q["title"] = str(title)[:200]
        if authors:
            q["author"] = str(authors[0])[:100]
    else:
        return []
    try:
        raw, _ = dl.http_get("https://openlibrary.org/search.json?" + urllib.parse.urlencode(q), timeout=25, headers={"Accept": "application/json", "User-Agent": _ua()})
        docs = json.loads(raw.decode("utf-8", "replace")).get("docs") or []
    except Exception:  # noqa: BLE001
        return []
    out = []
    for d in docs:
        t = d.get("title") or ""
        if d.get("subtitle"):
            t += ": " + d["subtitle"]
        isbns = [x for x in (isbn13(i) for i in d.get("isbn") or []) if x]
        out.append(_record(t, d.get("author_name"), (d.get("publisher") or [""])[0], d.get("first_publish_year"), (isbn if isbn else (isbns[0] if isbns else "")), "", d.get("number_of_pages_median"), "openlibrary",
                           "https://openlibrary.org" + d["key"] if d.get("key") else ""))
    return [r for r in out if r["title"]]


def crossref_books(title, rows=3):
    """Books (and edited volumes) Crossref has for a title: they carry a DOI. [] when nothing is found or it cannot be reached."""
    if os.environ.get("JW_OFFLINE") or len(str(title or "").strip()) < 8:
        return []
    try:
        raw, _ = dl.http_get("https://api.crossref.org/works?query.bibliographic=%s&filter=type:book,type:monograph,type:edited-book,type:reference-book&rows=%d" % (urllib.parse.quote(str(title)[:250]), rows),
                             timeout=30, headers={"Accept": "application/json"})
        items = json.loads(raw.decode("utf-8", "replace")).get("message", {}).get("items") or []
    except Exception:  # noqa: BLE001
        return []
    out = []
    for it in items:
        m = importer._meta_from_item(it)
        if m and m.get("doi"):
            eds = [" ".join(x for x in [e.get("given"), e.get("family")] if x) or e.get("name", "") for e in it.get("editor") or []]            # (an edited volume has editors, not authors)
            out.append(_record(m["title"], m["authors"] or [e for e in eds if e], m.get("publisher"), m.get("pub_date"), (it.get("ISBN") or [""])[0], m["doi"], 0, "crossref", m.get("url")))
    return out


def _norm(s):
    return fx.norm_title(s)


def _candidate(rec, data_pdf, own):
    """One record with what the PDF says about it (same evidence rules as papers: the title, and the authors, on the first pages; the file's own details count as backed by the file)."""
    verdict, reason, ev = identity.check({"title": rec["title"], "doi": "", "authors": rec["authors"]}, data_pdf)
    au = ev.get("authors")
    out = dict(rec, verdict="match" if own else verdict, reason="the file's own details" if own else reason, title_score=ev.get("title") or 0.0,
               authors_found=bool(own) or (au is not None and au >= identity.AUTHORS_MIN), doi_printed=False, journal=JOURNAL, year=rec["year"])
    return out


def _strong(c):
    return c["verdict"] == "match" and (c["authors_found"] or c.get("own"))


# ---------------------------------------------------------------- chapters, and where a page is
def chapters(info):
    """The outline of a book (its .book.json) as ranges of file pages: [{"i", "level", "title", "start", "end"}]. A chapter runs from its own page to the page where the next entry of the same or a higher
    level starts (that page is in both: a chapter often starts half-way down a page, and a search must not lose the words above it); the last one runs to the end. An entry with no later sibling runs to the end."""
    pages = int(info.get("pages") or 0)
    toc = []
    for e in info.get("toc") or []:
        try:
            lvl, title, start = int(e[0]), re.sub(r"\s+", " ", str(e[1])).strip(), int(e[2])
        except (TypeError, ValueError, IndexError):
            continue
        if title and 1 <= start <= max(pages, start):
            toc.append((max(1, lvl), title, min(start, pages) if pages else start))
    out = []
    for i, (lvl, title, start) in enumerate(toc):
        end = pages or start
        for l2, _, p2 in toc[i + 1:]:
            if l2 <= lvl:
                end = p2 if p2 > start else start
                break
        out.append({"i": i, "level": lvl, "title": title, "start": start, "end": max(start, min(end, pages) if pages else end)})
    return out


def label(info, page):
    """Where a page is, in words you could cite: 'Chapter 3 · p. 48' (a PDF book, with the printed-page offset you set), 'Chapter 3 · file page 57' (a PDF book with no offset yet) or
    'Chapter 3 · location 57' (an EPUB has no printed pages: the number is the place in the reading copy, which is all it can honestly be)."""
    page = int(page)
    inside = [c for c in chapters(info) if c["start"] <= page <= c["end"]]
    ch = max(inside, key=lambda c: (c["start"], c["level"], c["i"])) if inside else None            # the innermost entry that holds the page
    if info.get("source") == "epub":
        where = "location %d" % page
    else:
        off = int(info.get("offset") or 0)
        where = "file page %d" % page if not off else "p. %d" % (page - off) if page > off else "front matter (file page %d)" % page
    return (ch["title"] + " · " if ch else "") + where


def info_of(rel):
    """A book's .book.json ({} when it has none), for a path under Papers/."""
    pdf = ns.pdf_path(rel)
    return dl.load_json(pdf[:-4] + ".book.json", {}) if pdf else {}


def set_offset(rel, offset):
    """The printed-page offset of a PDF book: printed page 1 is file page offset + 1 (a book with 14 pages of front matter has offset 14). Raises ValueError (plain words)."""
    pdf = ns.pdf_path(rel)
    info = dl.load_json(pdf[:-4] + ".book.json", {}) if pdf else {}
    if not info:
        raise ValueError("That is not a book in your library.")
    if info.get("source") == "epub":
        raise ValueError("An EPUB has no printed pages: its hits are given as chapter and location.")
    try:
        off = int(str(offset).strip())
    except (TypeError, ValueError):
        raise ValueError("The offset is a whole number of pages (0 if the file starts at printed page 1).")
    if not 0 <= off < max(1, int(info.get("pages") or 1)):
        raise ValueError("The offset must be between 0 and %d." % max(0, int(info.get("pages") or 1) - 1))
    info["offset"] = off
    tmp = pdf[:-4] + ".book.json.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=1)
    os.replace(tmp, pdf[:-4] + ".book.json")
    return info


def _existing_book(isbn, title, doi=""):
    """Path of a book already in the library with this ISBN (read from the books' own files), DOI or title."""
    if isbn:
        for r in importer.library_rows():
            if r.get("journal") == JOURNAL and r.get("file"):
                try:
                    with open(os.path.join(dl.ROOT, r["file"][:-4] + ".book.json"), encoding="utf-8") as f:
                        if json.load(f).get("isbn") == isbn:
                            return r["file"]
                except (OSError, ValueError):
                    pass
    return importer._existing({"doi": doi, "title": title}) or ""


def analyse(data, filename="book", hint_pdf=False):
    """Look at a dropped EPUB (or a PDF you say is a book) and stage it; files nothing. Returns what importer.analyse_pdf returns, with "type": "book", "format": "epub" | "pdf" and the file's
    own details as the first record. Raises ValueError (plain words) for a file that is neither."""
    epub = is_epub(data)
    if not epub and not dl.is_pdf(data):
        raise ValueError("That is not a PDF or an EPUB (or it is cut off).")
    if epub:
        own = read_epub(data)
        pdf, toc, pages = to_pdf(data)
        facts = {"title": own["title"], "authors": own["authors"], "isbn": own["isbn"], "pages": pages, "toc": toc, "scan": False}
        rec0 = _record(own["title"], own["authors"], own["publisher"], own["date"], own["isbn"], "", pages, "file") if own["title"] else None
    else:
        pdf = data
        facts = _pdf_facts(data)
        own = {}
        rec0 = _record(facts["title"], facts["authors"], "", "", facts["isbn"], "", facts["pages"], "file") if facts["title"] else None
    guess = facts["title"] or re.sub(r"\s*\(\d+\)$", "", re.sub(r"\s+", " ", re.sub(r"[_-]+", " ", os.path.splitext(os.path.basename(filename))[0])).strip())
    cands, seen = [], set()

    def add(rec, is_own=False):
        for c in cands:                                               # the same ISBN is the same book: keep one record and fill in what the other one knows (a DOI, the publisher, the year)
            if c["isbn"] and rec["isbn"] and c["isbn"] == rec["isbn"]:
                for k in ("doi", "publisher", "year", "pub_date", "pages", "url"):
                    c[k] = c.get(k) or rec[k]
                return
        key = rec["isbn"] or _norm(rec["title"])
        if not rec["title"] or not key or key in seen:
            return
        seen.add(key)
        c = _candidate(rec, pdf, is_own)
        c["own"] = is_own
        cands.append(c)
    if rec0:
        add(rec0, True)
    found = openlibrary_search(isbn=facts["isbn"]) if facts["isbn"] else []
    if not found:
        found = openlibrary_search(title=guess, authors=facts["authors"])
    for r in found:
        add(r)
    for r in crossref_books(guess):
        add(r)
    if cands and not cands[0].get("own"):
        cands.sort(key=lambda c: (c["verdict"] == "match", c["authors_found"], c["title_score"]), reverse=True)
    elif len(cands) > 1:
        cands[1:] = sorted(cands[1:], key=lambda c: (c["verdict"] == "match", c["authors_found"], c["title_score"]), reverse=True)
    cands = cands[:4]
    pick = 0 if cands and cands[0]["verdict"] != "mismatch" else -1
    top = cands[pick] if pick >= 0 else {"title": guess, "doi": "", "isbn": facts["isbn"]}
    token = importer._stage(data)
    if epub:                                                          # the converted copy waits beside the original: confirming does not convert again
        with open(os.path.join(importer.STAGING, token + ".conv"), "wb") as f:
            f.write(pdf)
    return {"token": token, "name": filename, "type": "book", "format": "epub" if epub else "pdf", "candidates": cands, "pick": pick,
            "pdf": {"title": guess, "authors": facts["authors"], "isbn": facts["isbn"], "pages": facts["pages"], "scan": facts["scan"], "chapters": len(facts["toc"])},
            "confidence": "none" if not cands else "high" if pick == 0 and _strong(cands[0]) else "check",
            "existing": _existing_book(top.get("isbn") or "", top.get("title") or "", top.get("doi") or "")}


def confirm(token, fields):
    """File a staged book with the details you confirmed. fields: title (required), authors, publisher, pub_date (YYYY, YYYY-MM or YYYY-MM-DD), isbn, doi, edition, source ("lookup" = a record taken
    as it is, "edited" = a record you changed, "hand" = typed by you). Raises ValueError (plain words); a book refused for its details stays staged."""
    f = fields if isinstance(fields, dict) else {}
    path = importer._waiting(token)
    if not path:
        raise ValueError("That book is no longer waiting to be added: drop it again.")
    title = importer._clean_text(f.get("title"), 500)
    if not title:
        raise ValueError("The title is empty: give the book a title.")
    date = re.sub(r"\s+", " ", str(f.get("pub_date") or "")).strip()
    if date and not importer.date_ok(date):
        raise ValueError("The date must be a year (2024), a month (2024-05) or a day (2024-05-17), or empty.")
    isbn_in = str(f.get("isbn") or "").strip()
    isbn = isbn13(isbn_in) if isbn_in else ""
    if isbn_in and not isbn:
        raise ValueError("That is not a valid ISBN (the check digit does not match). Correct it or leave it empty.")
    authors = f.get("authors")
    authors = re.split(r"\s*;\s*", str(authors or "")) if not isinstance(authors, list) else authors
    authors = [a for a in (importer._clean_text(x, 120) for x in authors[:100]) if a]
    doi = importer.clean_doi(f.get("doi")) if str(f.get("doi") or "").strip() else ""
    pd = date + "-01" if re.fullmatch(r"\d{4}", date) else date
    p = {"title": title, "authors": authors, "doi": doi, "url": "https://doi.org/" + doi if doi else "", "journal": JOURNAL, "abbr": ABBR,
         "pub_date": pd or datetime.now().strftime("%Y-%m-%d"), "topics": []}
    note = {"lookup": "added by you (details from a book database, confirmed by you)", "edited": "added by you (details from a book database or the file, edited by you)",
            "file": "added by you (details from the file itself, confirmed by you)"}.get(f.get("source"), "added by you (details typed by you)")
    claimed = path + ".claimed"
    try:
        os.replace(path, claimed)
    except OSError:
        raise ValueError("That book is no longer waiting to be added: drop it again.")
    conv = os.path.join(importer.STAGING, token + ".conv")
    try:
        with open(claimed, "rb") as fh:
            original = fh.read()
        epub = is_epub(original)
        if epub:
            if not os.path.exists(conv):
                raise ValueError("That book is no longer waiting to be added: drop it again.")
            with open(conv, "rb") as fh:
                pdf = fh.read()
            import pymupdf
            with pymupdf.open(stream=pdf, filetype="pdf") as d:
                toc, pages = [[l, re.sub(r"\s+", " ", t).strip()[:200], max(1, pg)] for l, t, pg in d.get_toc(simple=True)], len(d)
        else:
            pdf = original
            facts = _pdf_facts(pdf)
            toc, pages = facts["toc"], facts["pages"]

        def sidecars(dest):
            info = {"isbn": isbn, "publisher": importer._clean_text(f.get("publisher"), 120), "edition": importer._clean_text(f.get("edition"), 60), "source": "epub" if epub else "pdf",
                    "pages": pages, "offset": 0, "toc": toc}
            with open(dest[:-4] + ".book.json", "w", encoding="utf-8") as fh:
                json.dump(info, fh, ensure_ascii=False, indent=1)
            if epub:
                with open(dest[:-4] + ".epub", "wb") as fh:
                    fh.write(original)
        with importer._LOCK:
            ex = _existing_book(isbn, title, doi)
            if ex:
                res = {"file": ex, "title": title, "existing": True, "note": "already in your library"}
            else:
                res = importer._file(p, pdf, note, after=sidecars)
    except Exception:
        try:
            os.replace(claimed, path)
        except OSError:
            pass
        raise
    for x in (claimed, conv):
        try:
            os.remove(x)
        except OSError:
            pass
    return res
