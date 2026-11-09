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
MAX_PART = 20 * 1024 * 1024          # one file inside a .docx / .pptx (a zip can claim any size)
MAX_XML = 60 * 1024 * 1024           # all the parts of one file together
MAX_ENTRY = 3000                     # a reference is never longer
MAX_CANDIDATES = 400
RETRY_WAIT = 1.5
AI_MAX_CHARS = 120_000
AI_OUT_TOKENS = 16000                # about 150 references; a longer answer is cut, and the page says so
DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>{}|\\^`\]]+)", re.I)
YEAR_RE = re.compile(r"(?<!\d)(1[89]\d\d|20\d\d)[a-z]?(?!\d)")
PARTICLE = r"(?:de|van|von|vom|zu|zur|der|den|la|le|di|da|do|das|dos|des|del|du|el|al|af|av|bin|ter|ten|st\.)"
UP = r"[^\W\d_a-z]"                 # a capital letter of any alphabet (Ø, Å, Ç…): a letter that is not a-z
SURNAME = UP + r"[\w'’\-]+"
INIT = UP + r"\.(?:-" + UP + r"\.)?"    # "J." or "J.-P."
# how a reference begins: "Surname, I." / "Godinho de Matos, M." / "Surname, First" / "I. Surname" (IEEE) / "[12] …" / "12. …"
AUTHOR_START = re.compile(r"^(?:(?:%s|%s)\s+){0,4}%s,\s*(?:%s|%s(?=[\s,])|%s[\w\-]+(?:\s+%s\.)?(?:\s*[,.&(]|\s+(?:and|&)\s|\s+(?:1[89]|20)\d\d|\s+[\"“]))"
                          % (SURNAME, PARTICLE, SURNAME, INIT, UP, UP, UP))
INITIAL_START = re.compile(r"^(?:%s\s?){1,3}(?:%s\s+)?%s\b" % (INIT, PARTICLE, SURNAME))
VANCOUVER = re.compile(r"^(?:%s\s+)?%s\s+[A-Z]{1,3}[,.]\s" % (PARTICLE, SURNAME))            # "Smith JA, Jones KB. Title. Lancet. 2020;395:497."
NUMBERED = re.compile(r"^\s*(?:\[\d{1,3}\]|\(\d{1,3}\)|\d{1,3}[.)])\s+")
BULLET = re.compile(r"^\s*(?:[•·▪◦‣*\-–—]|o)\s+")
HEADING = re.compile(r"^\s*(?:references?|bibliography|works cited|literature cited|reference list|further reading|readings?)\s*:?\s*$", re.I)
NO_YEAR = re.compile(r"\b(forthcoming|in press|working paper|published online|online first|articles? in advance|accepted)\b", re.I)      # a reference with no year yet
AUTHOR_LIST = re.compile(r"^(?P<a>(?:(?:(?:%s|%s)\s+){0,4}%s,\s*(?:%s\s?){1,4}(?:,\s*(?:and|&)\s+|\s+(?:and|&)\s+|,\s*)?)+)" % (SURNAME, PARTICLE, SURNAME, INIT))
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
    start = re.compile(r"<%s[ >]" % p_tag)
    for piece in xml.split("</%s>" % p_tag):            # (each piece is read once: split at the closing tag, the paragraph begins at the last opening tag in it)
        opens = [m.start() for m in start.finditer(piece)]
        if not opens:
            continue
        p = piece[opens[-1]:]
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
    out = {}
    for piece in xml.split("<Relationship")[1:]:          # (one tag at a time, each read once)
        tag = piece.split(">", 1)[0][:4000]
        i, t = re.search(r'\bId="([^"]+)"', tag), re.search(r'\bTarget="([^"]+)"', tag)
        if i and t:
            out[i.group(1)] = html.unescape(t.group(1))
    return out


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
        out, total = [], 0
        for n in names[:2000]:
            folder, base = n.rsplit("/", 1)
            xml = _zip_part(z, n)
            total += len(xml)
            if total > MAX_XML:
                break
            out += _xml_paras(xml, "a:p", "a:t", _rels(_zip_part(z, "%s/_rels/%s.rels" % (folder, base))))
        return out


def _lines_to_entries(lines):
    """Lines of text (a PDF's, a pasted list's) joined into entries: a blank line ends one, and so does a line that begins like a reference once the entry so far
    has its year (a reference wrapped over three lines stays one entry; a word cut at a line's end is put back together)."""
    out, cur, year, head = [], "", False, False
    for ln in lines:
        ln = ln.strip()
        if not ln:
            if cur:
                out.append(cur)
            cur, year, head = "", False, False
            continue
        starts = bool(NUMBERED.match(ln) or BULLET.match(ln) or AUTHOR_START.match(ln) or INITIAL_START.match(ln) or VANCOUVER.match(ln) or HEADING.match(ln))
        if cur and (head or (starts and year) or len(cur) > MAX_ENTRY):
            out.append(cur)
            cur, year = "", False
        head = not cur and bool(HEADING.match(ln))
        cur = (cur[:-1] + ln) if cur.endswith("-") and ln[:1].islower() else (cur + " " + ln if cur else ln)
        year = year or bool(YEAR_RE.search(ln))
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
    if not 25 <= len(s) <= MAX_ENTRY:
        return False
    if DOI_RE.search(s):
        return True
    t = _lead(s)
    if not YEAR_RE.search(s):                               # ("…, forthcoming": no year yet, but a list of authors with initials and then a title)
        return bool(NO_YEAR.search(s) and AUTHOR_LIST.match(t))
    return bool(AUTHOR_START.match(t) or INITIAL_START.match(t) or VANCOUVER.match(t))


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
        v = re.match(r"^(?:In:?\s+)?(?!https?:)([^,.;()\d][^,.;()]{2,120})", rest)
        out["venue"] = v.group(1).strip() if v else ""
        out["cut"] = min(len(s), m.end() + 140)
        return out
    y = YEAR_RE.search(s)
    out["year"] = y.group(1) if y else ""
    if VANCOUVER.match(s):                                  # Smith JA, Jones KB. Title. Journal. 2020;395:497.
        m = re.match(r"^(?P<a>[^.]{3,400})\.\s+" + sent, s)
        if m:
            v = re.match(r"^([^,.;()\d][^,.;()]{2,120})", s[m.end():].strip())
            out.update(authors=m.group("a").strip(), title=m.group("t").strip().rstrip(".").strip(), venue=v.group(1).strip() if v else "", cut=min(len(s), m.end() + 140))
            return out
    a = AUTHOR_LIST.match(s)                                # Authors with initials, then the title, no year in its usual place ("… Management Science, forthcoming.")
    m = a and re.match(sent, s[a.end():])
    if m:
        rest = s[a.end() + m.end():].strip()
        v = re.match(r"^(?:In:?\s+)?(?!https?:)([^,.;()\d][^,.;()]{2,120})", rest)
        out.update(authors=a.group("a").strip(" .,"), title=m.group("t").strip().rstrip(".").strip(), venue=v.group(1).strip() if v else "", cut=min(len(s), a.end() + m.end() + 140))
    return out


def candidates(paras):
    """The references among the paragraphs, in the document's order, each once (at most MAX_CANDIDATES: count_candidates says how many there are)."""
    out, seen = [], set()
    for p in paras:
        if not looks_like_ref(p):
            continue
        k = re.sub(r"[\W_]+", "", _lead(p).casefold())[:160]
        if k and k not in seen:
            seen.add(k)
            out.append(p)
    return out


def _cap(refs):
    return refs[:MAX_CANDIDATES], max(0, len(refs) - MAX_CANDIDATES)


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


def _same_title(a, b):
    """Two titles as word lists: the same words (a hyphen at a line's end may have joined two of them)."""
    return bool(a) and (a == b or "".join(a) == "".join(b))


def check(meta, ref, parsed=None):
    """Is this Crossref record the reference the document prints? ("found" | "unsure" | "", why).

    When the reference's own title could be read (parsed["title"]): found = the two titles are the same (or one is the other without its subtitle, the part after a
    colon), AND the record's first author is among the reference's authors or its year is the reference's (one either way). A record whose title is only a PART of the
    reference's title ("Trust in online markets" for "Trust in online markets for used goods"), or the journal's name, or the same title with "Reply" or "Erratum"
    added, is another paper: unsure at most. A title of one or two words needs the author AND the year (two either way: online first, then print).
    When no title could be read from the reference: the record's whole title must be in its words, with the author AND the year."""
    p = parsed or parse_ref(ref)
    tw = _words(meta.get("title"))
    if not tw:
        return "", ""
    main = _words(re.split(r"[:?!]", str(meta.get("title") or ""))[0])          # (the title without its subtitle: the part before a colon or a question mark)
    fam = _words((meta.get("authors") or [""])[0])[-1:] if meta.get("authors") else []
    aw = _words(p["authors"]) if p.get("authors") else _words(ref)
    has_author = bool(fam) and fam[0] in aw
    yrs = {int(y) for y in ([p["year"]] if p.get("year") else YEAR_RE.findall(ref))}
    my = int(str(meta.get("pub_date"))[:4]) if re.match(r"\d{4}", str(meta.get("pub_date") or "")) else 0
    near = lambda d: bool(my) and any(abs(my - y) <= d for y in yrs)            # noqa: E731
    both, either = has_author and near(2), has_author or near(1)
    said = "the title and the %s match" % ("author and year" if has_author and near(1) else "author" if has_author else "year")
    pw = _words(p.get("title"))
    if pw:
        pmain = _words(re.split(r"[:?!]", str(p["title"]))[0])
        other = re.search(r"\b(reply|rejoinder|erratum|errata|corrigendum|correction|comment|commentary|retract\w*|response)\b", " ".join(tw[len(main):]))     # ("…: Reply" is another paper)
        same = _same_title(tw, pw) or (len(main) >= 3 and not other and _same_title(main, pw)) or (len(tw) >= 4 and _same_title(tw, pmain))
        if same and len(tw) < 3:
            return ("found", "the title, the author and the year match") if both else ("unsure", "a very short title, and the author or the year is not the same")
        if same and either:
            return "found", said
        fwd, back = _in_order(pw, tw), _in_order(tw, pw)                          # the record's words in the reference's title, and the other way round
        if same or (min(fwd, back) >= 0.6 and len(tw) >= 3) or (len(tw) >= 3 and _contains(pw, tw)):
            return "unsure", "Crossref's nearest record has %s" % ("the same title, but not the same author or year" if same else "a similar title, not the same one")
        return "", ""
    rw = _words(ref)
    if _contains(rw, tw) and len(tw) >= 3:
        return ("found", "the title, the author and the year match") if both else ("unsure", "Crossref's nearest record has this title, but not the same author and year")
    if _in_order(rw, tw) >= 0.6 and len(tw) >= 3:
        return "unsure", "Crossref's nearest record has a similar title, not the same one"
    return "", ""


TYPE_RANK = {"journal-article": 0, "proceedings-article": 1, "book-chapter": 2, "posted-content": 4}


def _entry(ref, meta=None, status="none", why="", parsed=None):
    p = parsed or parse_ref(ref)
    if meta:
        return {"ref": ref[:500], "title": meta.get("title") or "", "authors": list(meta.get("authors") or [])[:30], "year": str(meta.get("pub_date") or "")[:4], "journal": meta.get("container") or "",
                "doi": importer.clean_doi(meta.get("doi")), "type": meta.get("type") or "", "status": status, "why": why}
    return {"ref": ref[:500], "title": p["title"], "authors": [], "year": p["year"], "journal": p["venue"], "doi": importer.clean_doi(p.get("doi")) if p.get("doi") else "", "type": "", "status": "none",
            "why": why or "Crossref does not list it (a book, a working paper or a report, perhaps)"}


def question(p, text):
    """What Crossref is asked: the reference's first authors, year, title and venue when they could be read (a list of fourteen authors must not push the title out
    of the question); otherwise the reference's own beginning. Never the notes that follow it."""
    if p.get("title"):
        return re.sub(r"\s+", " ", "%s %s %s %s" % (str(p.get("authors") or "")[:90], p.get("year") or "", p["title"], str(p.get("venue") or "")[:80])).strip()[:400]
    return text[:p.get("cut") or 400]


def resolve(ref, parsed=None):
    """One reference -> its entry. A DOI the document prints is asked for directly; otherwise Crossref is asked with the reference's own words and the answers are
    checked. parsed: the reference's fields when they are already known (the AI's list)."""
    p = parsed or parse_ref(ref)
    p.setdefault("cut", 400)
    text = _lead(ref)
    if p["doi"]:
        meta = importer.crossref_meta(p["doi"])
        if meta:
            return _entry(ref, meta, "book" if meta.get("type") in BOOK_TYPES else "found", "the DOI is printed in the document")
    hits = None
    for attempt in (0, 1):                                  # (Crossref now and then gives no answer: asked once more before saying so)
        try:
            hits = importer.crossref_search(question(p, text), rows=4, strict=True) or []
            break
        except OSError:
            if attempt == 0:
                time.sleep(RETRY_WAIT)
    if hits is None:
        return dict(_entry(ref, parsed=p, why="Crossref did not answer for this one: read the document again in a moment"), unasked=True)
    best = None
    for n, meta in enumerate(hits):
        st, why = check(meta, text[:MAX_ENTRY], p)
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


def _resolve_or_skip(ref, done, parsed=None):
    """resolve(), unless Crossref gave no answer for the last three references: then it is not asked again for this document (each would wait for its time-out)."""
    if len(done) >= 3 and all(e.get("unasked") for e in done[-3:]):
        return dict(_entry(ref, parsed=parsed, why="Crossref did not answer for this one: read the document again in a moment"), unasked=True)
    return resolve(ref, parsed)


def _dedupe(entries):
    out, seen = [], set()
    for e in entries:
        k = ("d", e["doi"].lower()) if e["doi"] else ("t", (re.sub(r"[\W_]+", "", (e["title"] or "").casefold()) or re.sub(r"[\W_]+", "", e["ref"].casefold()))[:120])
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
    return {"cost": ai.estimate("refs", sent + len(AI_SYSTEM) + 200, min(AI_OUT_TOKENS, 400 + 90 * max(n_found, 20))), "provider": provider, "model": model, "cut": chars > AI_MAX_CHARS}


def extract_stream(data, name=""):
    """Read the document, find the references by their shape, look each up on Crossref. Yields {"type": "status", msg, done?, total?, step, steps} and ends with
    {"type": "done", entries, paragraphs, candidates, chars, ai} (or {"type": "error", error}). No AI call."""
    try:
        yield {"type": "status", "msg": "Reading the document", "step": 1, "steps": 2}
        paras = paragraphs(data, name)
        cands, more = _cap(candidates(paras))
        out = []
        for i, c in enumerate(cands):
            yield {"type": "status", "msg": "Looking up the references on Crossref", "done": i, "total": len(cands), "step": 2, "steps": 2}
            out.append(_resolve_or_skip(c, out))
        chars = sum(len(p) + 1 for p in paras)
        kept = [e for e in out if e["status"] != "none" or e["title"] or e.get("unasked")]          # (a sentence that only began like a reference: nothing on Crossref, no title to read)
        yield {"type": "done", "entries": _dedupe(kept), "paragraphs": len(paras), "candidates": len(cands), "more": more, "chars": chars, "ai": ai_estimate(chars, len(cands))}
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
    """(the references of the AI's answer, whether the answer was cut off). A cut-off answer keeps every reference that is complete."""
    text = str(text or "")
    ok = lambda rs: [r for r in rs if isinstance(r, dict) and str(r.get("title") or "").strip()]            # noqa: E731
    try:
        d = json.loads(text)
        return (ok(d.get("references") or []) if isinstance(d, dict) else []), False
    except ValueError:
        pass
    i = text.find("[", max(0, text.find('"references"')))
    out, dec = [], json.JSONDecoder()
    while i >= 0:
        i = text.find("{", i)
        if i < 0:
            break
        try:
            obj, end = dec.raw_decode(text, i)
        except ValueError:
            break                                         # (the object the answer stopped in)
        out.append(obj)
        i = end
    return ok(out), True


def ai_stream(data, name=""):
    """The AI reads the document's text and lists its references (one call, through the budgeted AI layer); each is then looked up on Crossref and checked like the
    others. Yields status messages, then {"type": "done", entries, cost, provider, model, cut}. An AI error ends it with {"type": "error"}; nothing is saved anywhere."""
    import ai
    try:
        yield {"type": "status", "msg": "Reading the document", "step": 1, "steps": 3}
        text = "\n".join(paragraphs(data, name))
        cut = len(text) > AI_MAX_CHARS
        yield {"type": "status", "msg": "The AI is listing the references", "step": 2, "steps": 3}
        res = ai.chat("refs", [{"role": "user", "content": "Document: %s\n\n%s" % (name or "pasted text", text[:AI_MAX_CHARS])}], system=AI_SYSTEM, schema=AI_SCHEMA, max_tokens=AI_OUT_TOKENS)
        refs, short = _ai_refs(res.text if res else "")
        short = short or getattr(res, "stop", "") in ("max_tokens", "length")
        refs, more = _cap(refs)
        low, out = text.lower(), []
        for i, r in enumerate(refs):
            yield {"type": "status", "msg": "Looking up the references on Crossref", "done": i, "total": len(refs), "step": 3, "steps": 3}
            ym = YEAR_RE.search(str(r.get("year") or ""))
            dm = DOI_RE.search(str(r.get("doi") or ""))
            doi = importer.clean_doi(dm.group(1)) if dm else ""
            if doi and doi.lower() not in low:               # (a DOI the document does not print is the model's guess: not used)
                doi = ""
            parsed = {"authors": str(r.get("authors") or "").strip(" ."), "year": ym.group(1) if ym else "", "title": re.sub(r"\s+", " ", str(r["title"])).strip().rstrip("."),
                      "venue": str(r.get("venue") or "").strip(" ."), "doi": doi}
            ref = re.sub(r"\s+", " ", "%s (%s). %s. %s.%s" % (parsed["authors"], parsed["year"] or "n.d.", parsed["title"], parsed["venue"], " https://doi.org/" + doi if doi else ""))
            out.append(_resolve_or_skip(ref, out, parsed))
        yield {"type": "done", "entries": _dedupe(out), "cost": getattr(res, "cost", None), "provider": getattr(res, "provider", ""), "model": getattr(res, "model", ""), "cut": cut,
               "short": short, "more": more}
    except ValueError as e:
        yield {"type": "error", "error": str(e)}
    except ai.AIError as e:
        yield {"type": "error", "error": str(e)}
