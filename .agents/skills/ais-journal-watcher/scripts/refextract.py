"""The references in a document you have: a syllabus, a reading list, a paper's bibliography (Word, PowerPoint, PDF, plain text or pasted text).

    refextract.paragraphs(data, name) -> [str]        the document's paragraphs (docx, pptx, pdf, txt/md), links to doi.org kept with their paragraph
    refextract.candidates(paras)      -> [str]        the paragraphs (or parts of them) that look like a reference, by their shape: no AI, nothing sent anywhere
    refextract.resolve(ref)           -> entry        the reference looked up on Crossref and CHECKED against what the document prints (title, and author or year)
    refextract.extract_stream(data, name)             all of it, with progress messages for the page; ends with {"type": "done", "entries": [...], "ai": {cost…}}
    refextract.ai_estimate(text, n) / ai_stream(data, name)   the backup: the AI lists the references (its cost is shown first), then each is checked on Crossref

An entry is {ref, title, authors, year, journal, doi, type, status, why}: status "found" (Crossref has it and it matches the text), "unsure" (the nearest record does not
match well: unticked on the page), "book" (found, but a whole book: not downloaded), "none" (not on Crossref: shown, fetched by its title only if you tick it).
Only the reference's own words go to Crossref. Nothing is downloaded here: the page sends what you tick through the usual download routes.
"""
import html
import time
import io
import json
import os
import re
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import importer  # noqa: E402

MAX_BYTES = 150 * 1024 * 1024
MAX_PART = 40 * 1024 * 1024          # one file inside a .docx / .pptx (a zip can claim any size)
MAX_CANDIDATES = 400
RETRY_WAIT = 1.5
AI_MAX_CHARS = 120_000
DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>{}|\\^`\]]+)", re.I)
YEAR_RE = re.compile(r"(?<!\d)(1[89]\d\d|20\d\d)[a-z]?(?!\d)")
PARTICLE = r"(?:de|van|von|der|den|la|le|di|da|dos|del|du|el|al|bin|ter|ten)"
SURNAME = r"[A-Z][\w'’\-]+"
# how a reference begins: "Surname, I." / "Godinho de Matos, M." / "Surname, First" / "I. Surname" (IEEE) / "[12] …" / "12. …"
AUTHOR_START = re.compile(r"^(?:(?:%s|%s)\s+){0,4}%s,\s*(?:[A-Z]\b\.?|[A-Z][a-z]+(?:\s+[A-Z]\.)?\s*[,.&(]|[A-Z][a-z]+\s+(?:and|&)\s)" % (SURNAME, PARTICLE, SURNAME))
INITIAL_START = re.compile(r"^(?:[A-Z]\.\s?){1,3}(?:%s\s+)?%s\b" % (PARTICLE, SURNAME))
NUMBERED = re.compile(r"^\s*(?:\[\d{1,3}\]|\(\d{1,3}\)|\d{1,3}[.)])\s+")
BULLET = re.compile(r"^\s*(?:[•·▪◦‣*\-–—]|o)\s+")
HEADING = re.compile(r"^\s*(?:references?|bibliography|works cited|literature cited|reference list|further reading|readings?)\s*:?\s*$", re.I)
NO_YEAR = re.compile(r"\b(forthcoming|in press|working paper|published online|online first|articles? in advance|accepted)\b", re.I)      # a reference with no year yet
AUTHOR_LIST = re.compile(r"^(?P<a>(?:(?:(?:%s|%s)\s+){0,4}%s,\s*(?:[A-Z]\.\s?){1,4}(?:,\s*(?:and|&)\s+|\s+(?:and|&)\s+|,\s*)?)+)" % (SURNAME, PARTICLE, SURNAME))
BOOK_TYPES = ("book", "monograph", "edited-book", "reference-book", "book-set", "book-series")


# ---------------------------------------------------------------- the document's text

def _zip_part(z, name):
    try:
        info = z.getinfo(name)
    except KeyError:
        return ""
    if info.file_size > MAX_PART:
        return ""
    return z.read(name).decode("utf-8", "replace")


def _xml_paras(xml, p_tag, t_tag, links=None):
    """The text of each <p_tag> paragraph of an Office XML part: its <t_tag> runs, tabs and line breaks as spaces; a link to doi.org inside it is added after the text."""
    out = []
    for m in re.finditer(r"<%s[ >].*?</%s>" % (p_tag, p_tag), xml, re.S):
        p = m.group(0)
        p = re.sub(r"<(?:w:tab|w:br|a:br)\b[^>]*/>", "<%s> </%s>" % (t_tag, t_tag), p)
        text = html.unescape("".join(re.findall(r"<%s(?:\s[^>]*)?>([^<]*)</%s>" % (t_tag, t_tag), p)))
        if links:
            for rid in re.findall(r'r:id="([^"]+)"', p):
                url = links.get(rid, "")
                if "doi.org/" in url.lower() and url not in text:
                    text += " " + url
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            out.append(text)
    return out


def _rels(xml):
    return {m.group(1): html.unescape(m.group(2)) for m in re.finditer(r'<Relationship\b[^>]*\bId="([^"]+)"[^>]*\bTarget="([^"]+)"', xml)} | \
           {m.group(2): html.unescape(m.group(1)) for m in re.finditer(r'<Relationship\b[^>]*\bTarget="([^"]+)"[^>]*\bId="([^"]+)"', xml)}


def _docx(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        out = []
        for part in ("word/document.xml", "word/footnotes.xml", "word/endnotes.xml"):
            xml = _zip_part(z, part)
            if xml:
                out += _xml_paras(xml, "w:p", "w:t", _rels(_zip_part(z, "word/_rels/%s.rels" % part.split("/")[1])))
        return out


def _pptx(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        num = lambda n: int(re.search(r"(\d+)\.xml$", n).group(1))           # noqa: E731
        names = sorted((n for n in z.namelist() if re.fullmatch(r"ppt/(?:slides|notesSlides)/(?:slide|notesSlide)\d+\.xml", n)), key=lambda n: (num(n), n))
        out = []
        for n in names[:2000]:
            folder, base = n.rsplit("/", 1)
            out += _xml_paras(_zip_part(z, n), "a:p", "a:t", _rels(_zip_part(z, "%s/_rels/%s.rels" % (folder, base))))
        return out


def _lines_to_entries(lines):
    """Lines of text (a PDF's, a pasted list's) joined into entries: a blank line ends one, and so does a line that begins like a reference once the entry so far
    has its year (a reference wrapped over three lines stays one entry; a word cut at a line's end is put back together)."""
    out, cur = [], ""
    for ln in lines:
        ln = ln.strip()
        if not ln:
            if cur:
                out.append(cur)
            cur = ""
            continue
        starts = bool(NUMBERED.match(ln) or BULLET.match(ln) or AUTHOR_START.match(ln) or INITIAL_START.match(ln) or HEADING.match(ln))
        if cur and (HEADING.match(cur) or (starts and YEAR_RE.search(cur))):
            out.append(cur)
            cur = ""
        cur = (cur[:-1] + ln) if cur.endswith("-") and ln[:1].islower() else (cur + " " + ln if cur else ln)
    if cur:
        out.append(cur)
    return [re.sub(r"\s+", " ", x).strip() for x in out]


def _pdf(data):
    import pymupdf
    lines = []
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        for page in doc:
            if len(lines) > 200_000:
                break
            lines += page.get_text().splitlines()
            lines.append("")
    return _lines_to_entries(lines)


def _plain(data):
    text = data.decode("utf-8-sig", "replace") if isinstance(data, (bytes, bytearray)) else str(data)
    return _lines_to_entries(text.replace("\r\n", "\n").replace("\r", "\n").split("\n"))


def kind_of(data, name):
    n = str(name or "").lower()
    if data[:4] == b"%PDF" or n.endswith(".pdf"):
        return "pdf"
    if data[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = set(z.namelist())
        except zipfile.BadZipFile:
            return ""
        return "docx" if "word/document.xml" in names else "pptx" if any(x.startswith("ppt/slides/") for x in names) else ""
    if n.endswith((".doc", ".ppt", ".rtf", ".odt", ".pages", ".key", ".xlsx", ".xls")):
        return ""
    return "text"


def paragraphs(data, name=""):
    """The document's paragraphs. Raises ValueError with words for you when the file is not one this reads."""
    if not data:
        raise ValueError("The file is empty.")
    if len(data) > MAX_BYTES:
        raise ValueError("The file is larger than 150 MB.")
    k = kind_of(data, name)
    try:
        if k == "docx":
            return _docx(data)
        if k == "pptx":
            return _pptx(data)
        if k == "pdf":
            return _pdf(data)
        if k == "text":
            if b"\x00" in data[:4000]:
                raise ValueError("not text")
            return _plain(data)
    except ValueError:
        raise ValueError(UNREADABLE % (name or "the file"))
    except Exception:  # noqa: BLE001      (a damaged file: say so in plain words)
        raise ValueError("“%s” could not be read: it seems to be damaged or protected." % (name or "the file"))
    raise ValueError(UNREADABLE % (name or "the file"))


UNREADABLE = ("“%s” is not a document this can read. It reads Word (.docx), PowerPoint (.pptx), PDF and plain text (.txt, .md), and bibliographies (.ris, .bib). "
              "An older .doc or .ppt file: save it as .docx or .pptx (or as PDF) first.")


# ---------------------------------------------------------------- which paragraphs are references

def _lead(s):
    return BULLET.sub("", NUMBERED.sub("", s, 1), 1).strip()


def looks_like_ref(s):
    """Does this paragraph begin like a reference (authors first) and carry a year? Or does it print a DOI?"""
    s = str(s or "")
    if not 25 <= len(s) <= 3000:
        return False
    if DOI_RE.search(s):
        return True
    t = _lead(s)
    if not YEAR_RE.search(s):                               # ("…, forthcoming": no year yet, but a list of authors with initials and then a title)
        return bool(NO_YEAR.search(s) and AUTHOR_LIST.match(t))
    return bool(AUTHOR_START.match(t) or INITIAL_START.match(t))


def parse_ref(s):
    """What can be read from the reference's own shape: {authors (as written), year, title, venue, doi, cut (where the reference seems to end in s)}. Any of them may be
    empty: Crossref does the real reading, this is for its question and for references it does not know."""
    s = _lead(str(s or ""))
    doi = (DOI_RE.search(s) or [None, ""])[1].rstrip(".,;)") if DOI_RE.search(s) else ""
    out = {"authors": "", "year": "", "title": "", "venue": "", "doi": doi, "cut": min(len(s), 400)}
    sent = r"(?P<t>.{8,400}?[.?!])(?=\s|$)"                 # (a title ends at its first full stop, question or exclamation mark)
    m = re.match(r"^(?P<a>.{3,500}?)\s*\((?P<y>(?:1[89]|20)\d\d)[a-z]?(?:,\s*[A-Z][a-z]+\.?(?:\s+\d{1,2})?)?\)[.,:]?\s*" + sent, s)          # APA: Authors (2020). Title. Venue…
    q = None if m else re.search(r"[“\"]([^”\"]{12,400})[,.]?[”\"]", s)                # IEEE and others: A. Author, “Title,” Venue, 2020.
    if q:
        y = YEAR_RE.search(s[q.end():]) or YEAR_RE.search(s)
        out.update(authors=s[:q.start()].strip(" .,"), title=q.group(1).strip().rstrip(".,"), year=y.group(1) if y else "", cut=min(len(s), q.end() + 160))
        return out
    if not m:
        m = re.match(r"^(?P<a>.{3,500}?)[.,]\s+(?P<y>(?:1[89]|20)\d\d)[a-z]?[.,]\s+" + sent, s)                                            # Chicago / MISQ: Authors. 2020. Title. Venue…
    if m:
        out.update(authors=m.group("a").strip(" .,"), year=m.group("y"), title=m.group("t").strip().rstrip(".").strip())
        rest = s[m.end():].strip()
        v = re.match(r"^(?:In:?\s+)?([^,.;()\d][^,.;()]{2,120})", rest)
        out["venue"] = v.group(1).strip() if v else ""
        out["cut"] = min(len(s), m.end() + 140)
        return out
    y = YEAR_RE.search(s)
    out["year"] = y.group(1) if y else ""
    a = AUTHOR_LIST.match(s)                                # Authors with initials, then the title, no year in its usual place ("… Management Science, forthcoming.")
    m = a and re.match(sent, s[a.end():])
    if m:
        rest = s[a.end() + m.end():].strip()
        v = re.match(r"^(?:In:?\s+)?([^,.;()\d][^,.;()]{2,120})", rest)
        out.update(authors=a.group("a").strip(" .,"), title=m.group("t").strip().rstrip(".").strip(), venue=v.group(1).strip() if v else "", cut=min(len(s), a.end() + m.end() + 140))
    return out


def candidates(paras):
    """The references among the paragraphs, in the document's order, each once."""
    out, seen = [], set()
    for p in paras:
        if not looks_like_ref(p):
            continue
        k = re.sub(r"[^a-z0-9]+", "", _lead(p).lower())[:160]
        if k and k not in seen:
            seen.add(k)
            out.append(p)
            if len(out) >= MAX_CANDIDATES:
                break
    return out


# ---------------------------------------------------------------- Crossref, checked against the text

def _words(s):
    return re.sub(r"[^a-z0-9]+", " ", _fold(s).lower()).split()


def _fold(s):
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", str(s or "")) if not unicodedata.combining(c)).replace("’", "'")


def _contains(hay, needle):
    return bool(needle) and (" " + " ".join(needle) + " ") in (" " + " ".join(hay) + " ")


def _in_order(hay, needle):
    """The share of needle's words found in hay in the same order (1.0 = all of them)."""
    i = n = 0
    for w in needle:
        try:
            i = hay.index(w, i) + 1
            n += 1
        except ValueError:
            pass
    return n / len(needle) if needle else 0.0


def check(meta, ref):
    """Is this Crossref record the reference the document prints? ("found" | "unsure" | "", why). Found: its title is in the reference's words (the whole title, or the
    part before a colon when the document leaves the subtitle out) AND its first author's family name or its year is there too. Unsure: most of the title's words are
    there. A record with a title of one or two words is found only when the author AND the year (two years either way: online first, then print) are there too."""
    rw, tw = _words(ref), _words(meta.get("title"))
    main = _words(str(meta.get("title") or "").split(":")[0])
    whole = _contains(rw, tw) and len(tw) >= 3
    part = not whole and len(main) >= 4 and _contains(rw, main)
    share = _in_order(rw, tw)
    fam = _words((meta.get("authors") or [""])[0])[-1:] if meta.get("authors") else []
    has_author = bool(fam) and fam[0] in rw
    yrs = {int(y) for y in YEAR_RE.findall(ref)}
    my = int(str(meta.get("pub_date") or "0")[:4] or 0) if re.match(r"\d{4}", str(meta.get("pub_date") or "")) else 0
    has_year = bool(my) and any(abs(my - y) <= 1 for y in yrs)
    if 1 <= len(tw) < 3 and _contains(rw, tw) and has_author and bool(my) and any(abs(my - y) <= 2 for y in yrs):       # ("Wikipedia matters": two words prove little alone)
        return "found", "the title, the author and the year match"
    if (whole or part or (share >= 0.9 and len(tw) >= 5)) and (has_author or has_year):
        return "found", "the title and the %s match" % ("author and year" if has_author and has_year else "author" if has_author else "year")
    if (whole or part or share >= 0.6) and len(tw) >= 3:
        return "unsure", "Crossref's nearest record has a similar title, but not the same %s" % ("author or year" if whole or part else "words")
    return "", ""


TYPE_RANK = {"journal-article": 0, "proceedings-article": 1, "book-chapter": 2, "posted-content": 4}


def _entry(ref, meta=None, status="none", why="", parsed=None):
    p = parsed or parse_ref(ref)
    if meta:
        return {"ref": ref[:500], "title": meta.get("title") or "", "authors": list(meta.get("authors") or [])[:30], "year": str(meta.get("pub_date") or "")[:4], "journal": meta.get("container") or "",
                "doi": importer.clean_doi(meta.get("doi")), "type": meta.get("type") or "", "status": status, "why": why}
    return {"ref": ref[:500], "title": p["title"], "authors": [], "year": p["year"], "journal": p["venue"], "doi": "", "type": "", "status": "none",
            "why": why or "Crossref does not list it (a book, a working paper or a report, perhaps)"}


def resolve(ref):
    """One reference -> its entry. A DOI the document prints is asked for directly; otherwise Crossref is asked with the reference's own words and the answers are checked."""
    p = parse_ref(ref)
    text = _lead(ref)
    if p["doi"]:
        meta = importer.crossref_meta(p["doi"])
        if meta:
            return _entry(ref, meta, "book" if meta.get("type") in BOOK_TYPES else "found", "the DOI is printed in the document")
    hits = None
    for attempt in (0, 1):                                  # (Crossref now and then gives no answer: asked once more before saying so)
        try:
            hits = importer.crossref_search(text[:p["cut"]], rows=4, strict=True) or []
            break
        except OSError:
            if attempt == 0:
                time.sleep(RETRY_WAIT)
    if hits is None:
        return dict(_entry(ref, parsed=p, why="Crossref did not answer for this one: read the document again in a moment"), unasked=True)
    best = None
    for n, meta in enumerate(hits):
        st, why = check(meta, text[:max(p["cut"], 400)])
        if not st:
            continue
        y = str(meta.get("pub_date") or "")[:4]
        key = (st != "found", not (y and y == p["year"]), TYPE_RANK.get(meta.get("type") or "", 3), n)
        if best is None or key < best[0]:
            best = (key, meta, st, why)
    if not best:
        return _entry(ref, parsed=p)
    _, meta, st, why = best
    if st == "found" and meta.get("type") in BOOK_TYPES:
        st, why = "book", "a whole book: the app does not download books (add the file yourself in Library → Books)"
    return _entry(ref, meta, st, why)


def _dedupe(entries):
    out, seen = [], set()
    for e in entries:
        k = ("d", e["doi"].lower()) if e["doi"] else ("t", re.sub(r"[^a-z0-9]+", "", (e["title"] or e["ref"]).lower())[:120])
        if k not in seen:
            seen.add(k)
            out.append(e)
    return out


def ai_estimate(chars, n_found):
    """What asking the AI to list the references would cost (no call): {cost (None when the model has no price), provider, model, cut (the text is too long: only its
    first part is sent)}."""
    import ai
    provider, model = ai.feature_model("refs")
    sent = min(chars, AI_MAX_CHARS)
    return {"cost": ai.estimate("refs", sent + len(AI_SYSTEM) + 200, 400 + 90 * max(n_found, 20)), "provider": provider, "model": model, "cut": chars > AI_MAX_CHARS}


def extract_stream(data, name=""):
    """Read the document, find the references by their shape, look each up on Crossref. Yields {"type": "status", msg, done?, total?, step, steps} and ends with
    {"type": "done", entries, paragraphs, candidates, chars, ai} (or {"type": "error", error}). No AI call."""
    try:
        yield {"type": "status", "msg": "Reading the document", "step": 1, "steps": 2}
        paras = paragraphs(data, name)
        cands = candidates(paras)
        out = []
        for i, c in enumerate(cands):
            yield {"type": "status", "msg": "Looking up the references on Crossref", "done": i, "total": len(cands), "step": 2, "steps": 2}
            out.append(resolve(c))
        chars = sum(len(p) + 1 for p in paras)
        kept = [e for e in out if e["status"] != "none" or e["title"]]          # (a sentence that only began like a reference: nothing on Crossref, no title to read)
        yield {"type": "done", "entries": _dedupe(kept), "paragraphs": len(paras), "candidates": len(cands), "chars": chars, "ai": ai_estimate(chars, len(cands))}
    except ValueError as e:
        yield {"type": "error", "error": str(e)}


# ---------------------------------------------------------------- the backup: the AI lists the references, Crossref still checks each

AI_SYSTEM = ("You list the bibliographic references that appear in a document. Return every work the document cites or lists with enough detail to identify it: journal "
             "articles, conference papers, working papers, book chapters and books. Copy the authors, year, title and venue exactly as the document gives them; never add "
             "a work the document does not mention, never guess a DOI. A bare mention such as “Smith (2020)” with no title is not a reference: leave it out.")
AI_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["references"], "properties": {"references": {"type": "array", "items": {
    "type": "object", "additionalProperties": False, "required": ["authors", "year", "title", "venue", "doi"],
    "properties": {"authors": {"type": "string"}, "year": {"type": "string"}, "title": {"type": "string"}, "venue": {"type": "string"}, "doi": {"type": "string"}}}}}}


def _ai_refs(text):
    try:
        d = json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text or "", re.S)
        try:
            d = json.loads(m.group(0)) if m else {}
        except ValueError:
            d = {}
    return [r for r in (d.get("references") or []) if isinstance(r, dict) and str(r.get("title") or "").strip()] if isinstance(d, dict) else []


def ai_stream(data, name=""):
    """The AI reads the document's text and lists its references (one call, through the budgeted AI layer); each is then looked up on Crossref and checked like the
    others. Yields status messages, then {"type": "done", entries, cost, provider, model, cut}. An AI error ends it with {"type": "error"}; nothing is saved anywhere."""
    import ai
    try:
        yield {"type": "status", "msg": "Reading the document", "step": 1, "steps": 3}
        text = "\n".join(paragraphs(data, name))
        cut = len(text) > AI_MAX_CHARS
        yield {"type": "status", "msg": "The AI is listing the references", "step": 2, "steps": 3}
        res = ai.chat("refs", [{"role": "user", "content": "Document: %s\n\n%s" % (name or "pasted text", text[:AI_MAX_CHARS])}], system=AI_SYSTEM, schema=AI_SCHEMA, max_tokens=8000)
        refs = _ai_refs(res.text if res else "")[:MAX_CANDIDATES]
        out = []
        for i, r in enumerate(refs):
            yield {"type": "status", "msg": "Looking up the references on Crossref", "done": i, "total": len(refs), "step": 3, "steps": 3}
            y = (YEAR_RE.search(str(r.get("year") or "")) or [None, ""])[1] if YEAR_RE.search(str(r.get("year") or "")) else ""
            doi = importer.clean_doi(r.get("doi")) if DOI_RE.search(str(r.get("doi") or "")) else ""
            ref = "%s (%s). %s. %s.%s" % (str(r.get("authors") or "").strip(" ."), y or "n.d.", str(r["title"]).strip().rstrip("."), str(r.get("venue") or "").strip(" ."), " https://doi.org/" + doi if doi else "")
            out.append(resolve(re.sub(r"\s+", " ", ref)))
        yield {"type": "done", "entries": _dedupe(out), "cost": getattr(res, "cost", None), "provider": getattr(res, "provider", ""), "model": getattr(res, "model", ""), "cut": cut}
    except ValueError as e:
        yield {"type": "error", "error": str(e)}
    except ai.AIError as e:
        yield {"type": "error", "error": str(e)}
