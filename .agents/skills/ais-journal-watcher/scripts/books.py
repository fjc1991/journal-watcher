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
import threading
import urllib.parse
import zipfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import download as dl  # noqa: E402
import fetch_new as fx  # noqa: E402
import identity  # noqa: E402
import blocks as bl  # noqa: E402
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
        return updater.user_agent()
    except Exception:  # noqa: BLE001
        return "JournalWatcher/dev (an open-source tool for reading academic papers; https://github.com/fjc1991/journal-watcher)"


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


def place(info, page):
    """Where a page is, without the chapter: 'p. 48', 'file page 57', 'front matter (file page 5)' or, for an EPUB (no printed pages), 'location 57'."""
    page = int(page)
    if info.get("source") == "epub":
        return "location %d" % page
    off = int(info.get("offset") or 0)
    return "file page %d" % page if not off else "p. %d" % (page - off) if page > off else "front matter (file page %d)" % page


def info_at(pdf):
    """A book's .book.json for the absolute path of its PDF ({} for anything that is not a book)."""
    return dl.load_json(pdf[:-4] + ".book.json", {}) if isinstance(pdf, str) and pdf.lower().endswith(".pdf") else {}


def label(info, page):
    """Where a page is, in words you could cite: 'Chapter 3 · p. 48' (a PDF book, with the printed-page offset you set), 'Chapter 3 · file page 57' (a PDF book with no offset yet) or
    'Chapter 3 · location 57' (an EPUB has no printed pages: the number is the place in the reading copy, which is all it can honestly be)."""
    page = int(page)
    inside = [c for c in chapters(info) if c["start"] <= page <= c["end"]]
    ch = max(inside, key=lambda c: (c["start"], c["level"], c["i"])) if inside else None            # the innermost entry that holds the page
    return (ch["title"] + " · " if ch else "") + place(info, page)


def info_of(rel):
    """A book's .book.json ({} when it has none), for a path under Papers/."""
    pdf = ns.pdf_path(rel)
    return dl.load_json(pdf[:-4] + ".book.json", {}) if pdf else {}


_BOOKLOCK = threading.Lock()


def _write_info(pdf, info):
    tmp = pdf[:-4] + ".book.json.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=1)
    os.replace(tmp, pdf[:-4] + ".book.json")


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
    with _BOOKLOCK:
        info = dl.load_json(pdf[:-4] + ".book.json", {})            # (read again under the lock: a summary may have been saved meanwhile)
        info["offset"] = off
        _write_info(pdf, info)
    return info



# ---------------------------------------------------------------- a summary of one chapter, on request, after you have seen what it costs
SUMMARY_SYSTEM = """You summarise ONE chapter of an academic book for a researcher who has not read it. You are given the chapter's text, with page markers like [57] where a page begins.
Write Markdown, concise, no preamble, about 350 words at most, in exactly this shape:
**Argument.** Two or three sentences: what the chapter argues and why it matters.
**Key points.** Four to seven bullets: claims, concepts, definitions. Each ends with the page it comes from, written (p. 57) with a number from the page markers.
**Evidence and method.** One to three sentences: what it rests on (cases, data, theory). Say so if the text gives none.
**Limits.** One sentence: what the chapter leaves open, or what to check in the book itself.
Use only the text you are given; never add facts from elsewhere. If a note says part of the chapter was left out, say so in one short line before **Argument.**"""
SUMMARY_MAX_CHARS = 150_000            # about 37,000 tokens: a longer chapter is read from its start and its end, and the estimate says so
SUMMARY_OUT_TOKENS = 600


def chapter_key(c):
    return "%d-%d" % (c["start"], c["end"])


def chapter_text(pdf, c, blocks=None):
    """(text, characters, left_out) of one chapter for a summary: its body paragraphs with [page] markers. Over SUMMARY_MAX_CHARS the first 60 % and the last 40 % of the allowance are kept
    and a note says what was left out. blocks: the book's blocks when the caller has them already (a whole book is read once)."""
    bs = [b for b in (blocks if blocks is not None else bl.extract(pdf)) if b.get("kind", "body") == "body" and c["start"] - 1 <= b["page"] <= c["end"] - 1]
    parts, last = [], None
    for b in bs:
        mark = "[%d] " % (b["page"] + 1) if b["page"] != last else ""
        last = b["page"]
        parts.append(mark + b["text"])
    total = sum(len(x) for x in parts)
    if total <= SUMMARY_MAX_CHARS:
        return "\n\n".join(parts), total, False
    head, tail, used = [], [], 0
    for x in parts:
        if used + len(x) > SUMMARY_MAX_CHARS * 0.6:
            break
        head.append(x); used += len(x)
    used2 = 0
    for x in reversed(parts[len(head):]):
        if used2 + len(x) > SUMMARY_MAX_CHARS * 0.4:
            break
        tail.insert(0, x); used2 += len(x)
    return "\n\n".join(head + ["[Note: the middle of this chapter, about %d%% of it, is left out here because it is long.]" % round(100 * (total - used - used2) / total)] + tail), used + used2, True


def _chapter(rel, i):
    pdf = ns.pdf_path(rel)
    info = info_at(pdf) if pdf else {}
    if not info:
        raise ValueError("That is not a book in your library.")
    chs = chapters(info)
    try:
        n = int(i)
        if isinstance(i, bool) or not 0 <= n < len(chs):          # (not -1: that would be the last chapter)
            raise IndexError(n)
        c = chs[n]
    except (TypeError, ValueError, IndexError):
        raise ValueError("That chapter does not exist.")
    return pdf, info, c


def summary_estimate(rel, i):
    """What a summary of chapter i costs, before anyone asks for it: {"chars", "pages", "cost" (None when the model has no price in ai.json), "provider", "model", "left_out", "saved"}."""
    import ai
    pdf, info, c = _chapter(rel, i)
    text, used, left_out = chapter_text(pdf, c)
    chars = used + len(SUMMARY_SYSTEM) + 300
    provider, model = ai.feature_model("summarize")
    return {"chars": chars, "pages": c["end"] - c["start"] + 1, "cost": ai.estimate("summarize", chars, SUMMARY_OUT_TOKENS), "provider": provider, "model": model, "left_out": left_out,
            "saved": bool((info.get("summaries") or {}).get(chapter_key(c)))}


def _relabel(text, info):
    """The model cites file pages, (p. 57): say them the way the rest of the app does ('p. 48' with your offset, 'location 57' for an EPUB)."""
    def one(m):
        a, b = int(m.group(1)), m.group(2)
        return "(" + place(info, a) + ((" – " + place(info, int(b))) if b else "") + ")"
    return re.sub(r"\(pp?\.\s*(\d+)(?:\s*[–-]\s*(\d+))?\)", one, text)


class NoText(ValueError):
    """A part of a book with no body text to summarise (a bibliography, an index, a scanned image)."""


def _summarise(pdf, info, c, title="", fresh=False, override=None, blocks=None):
    """Write the summary of chapter c and save it in the book's .book.json. A generator: yields ("status", msg) and ("text", delta) as the answer arrives, and
    returns the saved record ({"title", "start", "end", "text", "provider", "model", "cost", "left_out", "at"}). Raises ValueError (no text) or ai.AIError (Blocked,
    Cancelled, the provider's refusal with its HTTP status, or an empty answer: an empty summary is never saved, nor kept in the answer cache, so asking again
    really asks again)."""
    import ai
    key = chapter_key(c)
    yield "status", "Reading the chapter…"
    text, used, left_out = chapter_text(pdf, c, blocks)
    if not text.strip():
        raise NoText("No text could be read from this chapter (a scanned image?).")
    messages = [{"role": "user", "content": "Book: %s\nChapter: %s (pages %d–%d of the file)\n\nText of the chapter:\n\n%s" % (title or "a book", c["title"], c["start"], c["end"], text)}]
    yield "status", "Writing the summary…"
    res = None
    for kind, v in ai.stream("summarize", messages, system=SUMMARY_SYSTEM, max_tokens=1200, cache=True, fresh=fresh, override=override,
                             cache_if=lambda t: bool((t or "").strip())):
        if kind == "text":
            yield "text", v
        else:
            res = v
    if res is None or not (res.text or "").strip():
        raise ai.AIError("The model gave an empty summary, so nothing was saved. Ask again, or choose another model in Settings → AI.")
    rec = {"title": c["title"], "start": c["start"], "end": c["end"], "text": _relabel(res.text, info), "provider": res.provider, "model": res.model,
           "cost": res.cost, "left_out": left_out, "at": datetime.now().isoformat(timespec="seconds")}
    with _BOOKLOCK:
        cur = info_at(pdf)
        cur.setdefault("summaries", {})[key] = rec
        _write_info(pdf, cur)
    return rec


def summary_stream(rel, i, title="", fresh=False, override=None):
    """Write (or return the saved) summary of chapter i. Yields {"type": "status"|"text"|"final"|"done"|"error"}; "final" carries the text with its pages said the way the rest of the app says
    them. It goes through the budgeted AI layer like every other call, so the monthly limit, the ledger and the tray apply."""
    import ai
    try:
        pdf, info, c = _chapter(rel, i)
        saved = (info.get("summaries") or {}).get(chapter_key(c))
        if saved and saved.get("text") and not fresh:
            yield {"type": "text", "delta": saved["text"]}
            yield {"type": "final", "text": saved["text"]}
            yield {"type": "done", "provider": saved.get("provider", ""), "model": saved.get("model", ""), "cost": 0.0, "cached": True, "saved": True}
            return
        gen = _summarise(pdf, info, c, title, fresh, override)
        while True:
            try:
                kind, v = next(gen)
            except StopIteration as stop:
                rec = stop.value
                break
            yield {"type": "status", "msg": v} if kind == "status" else {"type": "text", "delta": v}
        yield {"type": "final", "text": rec["text"]}
        yield {"type": "done", "provider": rec["provider"], "model": rec["model"], "cost": rec["cost"], "cached": False, "saved": True, "left_out": rec["left_out"]}
    except ValueError as e:
        yield {"type": "error", "error": str(e)}
    except ai.AIError as e:
        yield {"type": "error", "error": str(e)}


# ---------------------------------------------------------------- the whole book: every chapter and section, then an overview written from those summaries
OVERVIEW_SYSTEM = """You write an OVERVIEW of a whole academic book for a researcher who has not read it. You are given summaries of its chapters and sections, in the book's order,
each under a heading with its title and pages. They are all you know about the book.
Write Markdown, no preamble, about 600 words at most, in exactly this shape:
**The book's argument.** Three to five sentences: what the book as a whole argues, and why it matters.
**How it unfolds.** One short paragraph per chapter or part (by its title, in order): what it contributes to the argument.
**Key concepts.** Four to eight bullets: the book's main concepts and claims. Each ends with the page reference that the summary gives for it, copied exactly as it is written there.
**Evidence and method.** Two or three sentences: what the book rests on (cases, data, theory).
**Limits and open questions.** One to three sentences.
Use only the summaries; never add facts from elsewhere, and never invent a page reference. If a note says some summaries were shortened, say so in one short line first."""
OVERVIEW_OUT_TOKENS = 1000
OVERVIEW_MAX_CHARS = 150_000           # the summaries sent for the overview; past this, sections are left out first (chapters kept), then every summary is shortened alike
EST_SUMMARY_CHARS = 2000               # how long a chapter summary comes out (for the overview's estimate, before the summaries exist)


def parts(info):
    """The parts of a book that the whole-book summary writes: every entry of its outline (chapters, and their sections, at any depth), in the book's order. An
    entry with the same page range as one before it (a part and its first chapter, say) is the same text: it is summarised once, under the first title."""
    out, seen = [], set()
    for c in chapters(info):
        k = chapter_key(c)
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out


def _book(rel):
    pdf = ns.pdf_path(rel)
    info = info_at(pdf) if pdf else {}
    if not info:
        raise ValueError("That is not a book in your library.")
    if not chapters(info):
        raise ValueError("This book has no outline (no table of contents in the file), so it cannot be summarised chapter by chapter.")
    return pdf, info


def _done(sums, c):
    """A part needs nothing more: its summary is saved, or it was found to have no text to summarise (a bibliography, an index; marked "empty")."""
    s = sums.get(chapter_key(c)) or {}
    return bool(s.get("text") or s.get("empty"))


def mark_empty(pdf, c):
    """Note that this part has no body text (nothing is asked of the AI for it, and the overview leaves it out)."""
    with _BOOKLOCK:
        cur = info_at(pdf)
        cur.setdefault("summaries", {})[chapter_key(c)] = {"title": c["title"], "start": c["start"], "end": c["end"], "text": "", "empty": True,
                                                            "at": datetime.now().isoformat(timespec="seconds")}
        _write_info(pdf, cur)


def _fingerprint(info, ps):
    """What the overview was written from: the parts and their summaries. A summary written again (or a part added) makes the overview out of date."""
    import hashlib
    sums = info.get("summaries") or {}
    s = "\n".join("%s|%s" % (chapter_key(c), (sums.get(chapter_key(c)) or {}).get("text", "")) for c in ps)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def overview_pending(info):
    """True when every part has a summary and the overview is missing, or was written from other summaries (out of date)."""
    ps = parts(info)
    sums = info.get("summaries") or {}
    if not ps or any(not _done(sums, c) for c in ps) or not any((sums.get(chapter_key(c)) or {}).get("text") for c in ps):
        return False
    return (info.get("overview") or {}).get("fingerprint") != _fingerprint(info, ps)


def overview_stale(info):
    """An overview exists but no longer matches the summaries (one was written again, or a part has none yet)."""
    ov = info.get("overview") or {}
    return bool(ov.get("text")) and ov.get("fingerprint") != _fingerprint(info, parts(info))


BOOK_NO_TEMPLATE = ("A book is not summarised with a paper template: the whole book would be sent at once. Use Library → Books → Chapters → “Summarise the whole "
                    "book…”, which shows what it costs first.")


def overview_view(info):
    """What a book's page and the reader's Summary tab show, read from its own .book.json each time (never a copy): {overview (or None), overview_stale, parts,
    parts_done (summarised or found to have no text), parts_summarised}. No AI call."""
    ps = parts(info)
    sums = info.get("summaries") or {}
    ov = info.get("overview") or {}
    return {"overview": ov if ov.get("text") else None, "overview_stale": overview_stale(info), "parts": len(ps),
            "parts_done": sum(1 for c in ps if _done(sums, c)), "parts_summarised": sum(1 for c in ps if (sums.get(chapter_key(c)) or {}).get("text"))}


def is_book(rel):
    """Is this file under Papers/ a book? Its .book.json, or (that file missing) its library row's journal "Books", or the EPUB it was made from."""
    pdf = ns.pdf_path(rel)
    if not pdf:
        return False
    if info_at(pdf) or os.path.exists(pdf[:-4] + ".epub"):
        return True
    try:
        import csv
        with open(os.path.join(dl.PAPERS, "_index.csv"), newline="", encoding="utf-8") as f:
            return any(r.get("file") == rel and r.get("journal") == JOURNAL for r in csv.DictReader(f))
    except OSError:
        return False


def whole_plan(rel):
    """(pdf, info, the parts still without a summary, all the parts)."""
    pdf, info = _book(rel)
    ps = parts(info)
    sums = info.get("summaries") or {}
    return pdf, info, [c for c in ps if not _done(sums, c)], ps


def _overview_input(info, ps):
    """(text sent for the overview, cut): the stored summaries in the book's order, each under its title and pages."""
    sums = info.get("summaries") or {}
    items = [(c, (sums.get(chapter_key(c)) or {}).get("text", "")) for c in ps]
    items = [(c, t) for c, t in items if t]                                   # (a part with no text to summarise is left out)
    size = lambda its: sum(len(t) + 80 for _, t in its)                      # noqa: E731
    head = lambda c: "%s %s (%s – %s)" % ("#" * min(4, c["level"] + 1), c["title"], place(info, c["start"]), place(info, c["end"]))  # noqa: E731
    cut, dropped = "", None
    while size(items) > OVERVIEW_MAX_CHARS and len({c["level"] for c, _ in items}) > 1:     # the deepest level goes first (sections before chapters, chapters before parts)
        dropped = max(c["level"] for c, _ in items)
        items = [(c, t) for c, t in items if c["level"] < dropped]
        cut = "sections"
    if size(items) > OVERVIEW_MAX_CHARS:
        each = max(300, OVERVIEW_MAX_CHARS // max(1, len(items)) - 80)
        items = [(c, t if len(t) <= each else t[:each].rsplit(" ", 1)[0] + " …") for c, t in items]
        cut = "shortened"
    notes = []
    if dropped is not None:
        notes.append("the summaries of the entries below outline level %d are left out here because the book is long" % (dropped - 1 if dropped > 1 else 1))
    if cut == "shortened":
        notes.append("each summary given is shortened")
    note = "[Note: %s.]\n\n" % "; ".join(notes) if notes else ""
    return note + "\n\n".join(head(c) + "\n" + t for c, t in items), cut


def whole_estimate(rel):
    """What summarising the whole book would cost, before anything is spent (no AI call): {"parts", "done", "todo", "nested", "overview" (it will be written),
    "cost" (summaries of the parts to do plus the overview; None when the model has no price), "parts_cost", "overview_cost", "provider", "model", "left_out"
    (parts that are long enough to be read from their start and end)}. Sections are summarised as well as the chapters that hold them, so their pages are sent twice."""
    import ai
    pdf, info, todo, ps = whole_plan(rel)
    blocks = bl.extract(pdf) if todo else None
    costs, left, empty = [], 0, 0
    for c in todo:
        text, used, lo = chapter_text(pdf, c, blocks)
        if not text.strip():                       # (a bibliography, an index: nothing is sent for it)
            empty += 1
            continue
        left += bool(lo)
        costs.append(ai.estimate("summarize", used + len(SUMMARY_SYSTEM) + 300, SUMMARY_OUT_TOKENS))
    sums = info.get("summaries") or {}
    ov_in = min(OVERVIEW_MAX_CHARS, sum(len((sums.get(chapter_key(c)) or {}).get("text", "")) + 80 for c in ps) + EST_SUMMARY_CHARS * (len(todo) - empty)) + len(OVERVIEW_SYSTEM)
    ov = (len(todo) > empty or (bool(todo) and any((sums.get(chapter_key(c)) or {}).get("text") for c in ps))) or overview_pending(info)
    ov_cost = ai.estimate("summarize", ov_in, OVERVIEW_OUT_TOKENS) if ov else 0.0
    parts_cost = sum(costs) if all(c is not None for c in costs) else None
    provider, model = ai.feature_model("summarize")
    return {"parts": len(ps), "done": len(ps) - len(todo), "todo": len(todo) - empty, "empty": empty, "nested": any(c["level"] > 1 for c in ps), "overview": ov,
            "parts_cost": parts_cost, "overview_cost": ov_cost, "cost": parts_cost + ov_cost if parts_cost is not None and ov_cost is not None else None,
            "provider": provider, "model": model, "left_out": left}


def write_overview(rel, title="", override=None):
    """Write the book's overview from its stored summaries only (never the book's text) and save it as info["overview"]. Raises ValueError when a part has no
    summary yet, ai.AIError otherwise (an empty answer included: nothing is saved or cached). The page references are the summaries' own, copied as they are."""
    import ai
    pdf, info = _book(rel)
    ps = parts(info)
    sums = info.get("summaries") or {}
    if any(not _done(sums, c) for c in ps) or not any((sums.get(chapter_key(c)) or {}).get("text") for c in ps):
        raise ValueError("Every chapter and section needs its summary before the overview is written.")
    body, cut = _overview_input(info, ps)
    messages = [{"role": "user", "content": "Book: %s\n\nSummaries of its chapters and sections, in the book's order:\n\n%s" % (title or "a book", body)}]
    res = None
    for kind, v in ai.stream("summarize", messages, system=OVERVIEW_SYSTEM, max_tokens=2000, cache=True, override=override, cache_if=lambda t: bool((t or "").strip())):
        if kind == "done":
            res = v
    if res is None or not (res.text or "").strip():
        raise ai.AIError("The model gave an empty overview, so nothing was saved.")
    rec = {"text": res.text.strip(), "provider": res.provider, "model": res.model, "cost": res.cost, "parts": len(ps), "fingerprint": _fingerprint(info, ps), "cut": cut,
           "at": datetime.now().isoformat(timespec="seconds")}
    with _BOOKLOCK:
        cur = info_at(pdf)
        cur["overview"] = rec
        _write_info(pdf, cur)
    return rec


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
