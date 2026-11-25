"""What a PDF's own first page says about the paper: its title, its authors (each with the affiliation, place and e-mail printed under the name), the conference and
year in its running header or footer, the kind of submission ("Regular Paper"), its abstract and its keywords.

    pdfmeta.read(data)            the details, read by rules about the page's layout (no network, no AI)
    pdfmeta.with_ai(data, got)    the same, after the AI was asked to read the first page; only what is literally printed on the page is kept
    pdfmeta.clean(details)        details as they are stored and shown (lengths cut, wrong types dropped)

Why the layout and not the PDF's own "author" field: that field is whatever the word processor wrote there, often the name of the person who made the template
(a workshop paper by four authors carried the name of a fifth person). What is printed on the page is what a reader sees, so that is what is read.

A paper that Crossref knows is described by Crossref (importer.py); this module is for the ones it does not know (workshop and conference submissions, working
papers, drafts), and adds what Crossref never has (affiliations, e-mails, the kind of submission).
"""
import re
import unicodedata

import kinds

MAX_AUTHORS, MAX_ABSTRACT, MAX_KEYWORDS = 60, 6000, 30
EMAIL = re.compile(r"[A-Za-z0-9][\w.+\-']*@[\w\-]+(?:\.[\w\-]+)+")
EMAIL_GROUP = re.compile(r"[{\[(]\s*([^}\])@]{3,200})\s*[}\])]\s*@\s*([\w\-]+(?:\.[\w\-]+)+)")          # {ann, bob}@uni.edu
STOP = re.compile(r"^\s*(?:(?:\d+|[IVX]+)\.?\s+)?(abstract|introduction|keywords?|key\s?words|index terms|summary|executive summary|background|motivation|jel\b)\b", re.I)
ABSTRACT = re.compile(r"^\s*abstract\b\s*[:.\-—–]?\s*", re.I)
KEYWORDS = re.compile(r"^\s*(keywords?|key\s?words|index terms)\b\s*[:.\-—–]?\s*", re.I)
SECTION = re.compile(r"^\s*(?:(?:\d+|[IVX]+)\.?\s+)?(introduction|background|motivation|related (work|literature)|literature review|jel\b)", re.I)
AFFIL = re.compile(r"universit|institut|\bschool\b|\bcollege\b|department|\bdept\b|laborator|\blabs?\b|\bcent(?:er|re)\b|faculty|academy|polytechnic|politecnico|hospital|\bresearch\b|"
                   r"\binc\b|\bllc\b|\bltd\b|\bgmbh\b|\bcorp(?:oration)?\b|\bcompany\b|\bbank\b|ministry|foundation|hochschule|[ée]cole|\bcampus\b|\bbusiness\b|\bgroup\b|"
                   r"\b(?:MIT|ETH|EPFL|INSEAD|HEC|ESSEC|ESADE|IESE|KAIST|NUS|NTU|HKUST|HKU|CUHK|UCLA|UCSD|USC|NYU|CMU|LSE|IIM|IIT|IBM|UNSW|KU Leuven|TU \w+)\b", re.I)
PAPER_TYPE = re.compile(r"^(regular|short|full|long|complete[d]?|completed research|research[- ]in[- ]progress|emergent research forum|extended|poster|position|vision|"
                        r"teaching case|practitioner|work[- ]in[- ]progress|student)\b[\w \-()]{0,40}?\b(papers?|abstract|research|submission|track|case|forum)\b\W*$|^(research[- ]in[- ]progress|"
                        r"extended abstract|work[- ]in[- ]progress|teaching case)\W*$", re.I)
PLACES = set("""usa us uk china india germany france canada australia singapore japan korea netherlands switzerland italy spain israel taiwan brazil mexico sweden norway denmark
finland austria belgium ireland portugal greece turkey poland russia egypt nigeria kenya ghana chile argentina colombia peru qatar emirates arabia pakistan bangladesh
indonesia malaysia thailand vietnam philippines zealand states kingdom united hong kong new york san francisco los angeles saint st city republic province state county
north south east west central upon the of and""".split())
PARTICLES = set("van von de der den del della di da dos das du la le el al bin ibn ben ter ten zu y e".split())
NOT_NAMES = set("""the a an of for and in on at to by with from paper papers research working draft abstract version preliminary submitted submission proceedings conference workshop
journal review article study evidence introduction keywords university department school college institute this is are do not please cite quote circulate
comments welcome forthcoming accepted revised received published copyright rights reserved all email e-mail corresponding author authors affiliation
january february march april may june july august september october november december""".split())
MARK = re.compile(r"^[\s,;]*(?:\d{1,2}|[*†‡§¶#∗˚]+|[a-f])(?:\s*[,;]?\s*(?:\d{1,2}|[*†‡§¶#∗˚]+|[a-f]))*[\s,;]*$")
LEADING_MARK = re.compile(r"^\s*(\d{1,2}|[*†‡§¶#∗]|[a-f](?=\s))\s*")
YEAR = re.compile(r"\b(19[5-9]\d|20\d{2})\b")
TOLD = re.compile(r"presented|earlier version|previous version|based on|accepted|forthcoming|appeared", re.I)
MONTHS = "january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
DATED = re.compile(r"\b(?:%s)\.?\s+(?:\d{1,2},?\s+)?(19[5-9]\d|20\d{2})\b|\b(19[5-9]\d|20\d{2})[-/](0[1-9]|1[0-2])[-/]\d{2}\b" % MONTHS, re.I)


def _fold(s):
    """Letters only, without accents and case: for asking "is this printed on the page?"."""
    s = unicodedata.normalize("NFKD", str(s or ""))
    return re.sub(r"[^a-z0-9]+", " ", "".join(c for c in s if not unicodedata.combining(c)).lower()).strip()


def _sp(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()


class _Line:
    __slots__ = ("x0", "y0", "x1", "y1", "size", "bold", "text", "sups", "page", "n")

    def __init__(self, x0, y0, x1, y1, size, bold, text, sups, page):
        self.x0, self.y0, self.x1, self.y1, self.size, self.bold, self.text, self.sups, self.page = x0, y0, x1, y1, size, bold, text, sups, page
        self.n = 0                                  # its place in the PDF's own order of reading (down one column, then the next)

    @property
    def cx(self):
        return (self.x0 + self.x1) / 2

    @property
    def style(self):
        return (self.bold, round(self.size))


def _lines(page, n):
    """The page's lines of text, top to bottom. A line whose words stand far apart (two names side by side) is cut into its parts. A small raised mark after a name
    (1, *, †) is kept apart in `sups`, as [(position in the text, mark)]."""
    import pymupdf
    flags = pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES
    out = []
    for b in page.get_text("dict", flags=flags).get("blocks", []):
        for ln in b.get("lines", []):
            if abs((ln.get("dir") or (1, 0))[1]) > 0.2:                 # (text set sideways: a watermark in the margin)
                continue
            spans = [s for s in ln.get("spans", []) if s.get("text", "").strip()]
            if not spans:
                continue
            main = max(s["size"] for s in spans)
            parts, cur = [], []
            for s in spans:
                if cur and s["bbox"][0] - cur[-1]["bbox"][2] > 2.5 * main:
                    parts.append(cur)
                    cur = []
                cur.append(s)
            parts.append(cur)
            for part in parts:
                text, sups, prev = "", [], None
                for s in part:
                    t = s["text"]
                    if prev is not None and text and not text.endswith(" ") and not t.startswith(" ") and s["bbox"][0] - prev["bbox"][2] > 0.18 * main:
                        text += " "                      # (each word is a piece of its own in some PDFs, with the space left out)
                    prev = s
                    raised = (s.get("flags", 0) & 1 or s["size"] < 0.8 * main) and MARK.match(t)
                    if raised and text.strip():
                        sups.append((len(text.rstrip()), re.sub(r"\s+", "", t).strip(",;")))
                    else:
                        text += t
                text = text.replace(" ", " ")
                if not text.strip():
                    continue
                big = max(part, key=lambda s: len(s["text"].strip()))
                out.append(_Line(min(s["bbox"][0] for s in part), min(s["bbox"][1] for s in part), max(s["bbox"][2] for s in part), max(s["bbox"][3] for s in part),
                                 big["size"], bool(big.get("flags", 0) & 16) or "bold" in big.get("font", "").lower(), _sp(text), sups, n))
    for i, l in enumerate(out):
        l.n = i
    out.sort(key=lambda l: (round(l.y0 / 3), l.x0))
    return out


# ---------------------------------------------------------------- the conference and year (running header or footer)

def _venue(pages):
    """(kind, venue, year, full name) from the lines in the top and bottom margins of the first pages: "Workshop on Information Technology and Systems, 2026" under
    every page is the paper's own venue, while a conference named in a footnote ("presented at…") is not. (None, "", "", "") when no margin says it."""
    for lines, height in pages:
        edge = [l for l in lines if l.y1 <= 0.11 * height or l.y0 >= 0.88 * height]
        for l in edge:
            if TOLD.search(l.text) or not kinds.CONF_WORDS.search(l.text) and not any(rx.search(l.text) for rx, _ in kinds._CONF_NAMES):
                continue
            y = YEAR.search(l.text)
            full = _sp(re.sub(r"^\W*(?:in:?\s+)?(?:the\s+)?(?:proceedings of\s+)?(?:the\s+)?", "", re.split(r"[|•·]", l.text)[0], flags=re.I))
            full = _sp(re.sub(r"[\s,;:(\-–—]*\b(19[5-9]\d|20\d{2})\b.*$", "", full)) or _sp(full)
            full = _sp(re.sub(r"\s*\([^)]*$|\s*\([A-Za-z0-9 ’'\-]{2,20}\)\s*$", "", full)).rstrip(" ,;:-–—")       # ("… Systems (NIPS 2017)": the short name in brackets goes)
            full = re.sub(r"^\d+\s+|\s+\d+$", "", full)                 # (the page number set beside it)
            full = re.sub(r"^(?:\d+(?:st|nd|rd|th)|[A-Za-z\-]+(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tieth|teenth|eleventh|twelfth))\s+(?=\S+\s)", "", full, flags=re.I)
            if len(full) < 4:
                continue
            short = next((name for rx, name in kinds._CONF_NAMES if rx.search(l.text)), "")
            if not y and not short:
                continue
            year = y.group(1) if y else ""
            return "conference", _sp("%s %s" % (short or full, year)), year, full
    return None, "", "", ""


# ---------------------------------------------------------------- names

def _name_tokens(s):
    return [t for t in re.split(r"\s+", s.strip()) if t]


def is_name(s):
    """Whether these words read as one person's name: two to five words, each beginning with a capital (or a particle such as "van", or an initial), none of them a
    word that belongs to a place, an institution or the furniture of a title page."""
    s = _sp(s).strip(" ,;")
    if not 3 <= len(s) <= 60 or re.search(r"[\d@:/\\|()\[\]{}!?]|\.{2}", s):
        return False
    toks = _name_tokens(s)
    if not 2 <= len(toks) <= 5 or AFFIL.search(s):
        return False
    caps = 0
    for t in toks:
        low = t.lower().strip(".,")
        if low in PARTICLES:
            continue
        if re.fullmatch(r"(?:[^\W\d_]\.-?){1,3}", t):          # an initial: "A.", "J.-P."
            continue
        if low in NOT_NAMES or low in PLACES:
            return False
        core = t.strip(".,'’-")
        if not core or not core[0].isalpha() or not core[0].isupper():
            return False
        if not re.fullmatch(r"[^\W\d_](?:[^\W\d_]|[.'’\-])*", t.rstrip(",")):
            return False
        caps += 1
    return caps >= 2


_ONE_MARK = r"(?:\d{1,2}|[*†‡§¶#∗˚]|\x01[^\x02]*\x02)"
INLINE_MARKS = re.compile(r"(?<=[^\W\d_.])\s*(%s(?:\s*,?\s*%s)*)(?=\s*(?:[,;]|$|\s+(?:and|&)\s|\s+[^\W\d_]))|(?<=[\s,;])(%s(?:\s*,?\s*%s)*)(?=[^\W\d_]{2})" % ((_ONE_MARK,) * 4))


def _split_names(line):
    """The names on one line, each with the marks set after (or before) it: "Ann Lee¹, Bob Ray² and Cy Wu¹*" -> [("Ann Lee", ["1"]), ("Bob Ray", ["2"]),
    ("Cy Wu", ["1", "*"])]; also "Ann Lee 1 * Bob Ray 2", where the marks are all that parts the names. [] when any part of the line is not a name."""
    text = line.text
    def back(at):                                        # ("Ann Lee,¹ Bob Ray": the mark belongs before the comma)
        while at > 0 and line.text[at - 1] in ",; ":
            at -= 1
        return at
    for at, _, mk in sorted(((back(at), i, mk) for i, (at, mk) in enumerate(line.sups)), reverse=True):      # the raised marks go back to where they stood, fenced
        text = text[:at] + "\x01" + re.sub(r"[,;]", "\x07", mk) + "\x02" + text[at:]

    def fence(m):
        if m.group(1):                                   # (a comma between two marks is not one between two names)
            return " \x03" + re.sub(r"[,;]", "\x07", m.group(1)) + "\x04 , "
        return " \x05" + re.sub(r"[,;]", "\x07", m.group(2)) + "\x06"
    text = INLINE_MARKS.sub(fence, text)
    pieces, pos = [], 0
    for m in re.finditer(r"\s*(?:,\s*(?:and|&)\s+|\s+(?:and|&)\s+|[,;·•])\s*", text + ","):
        seg = text[pos:m.start()]
        if seg.strip():
            pieces.append(seg)
        pos = m.end()
    out = []
    for seg in pieces:
        marks = [k for grp in re.findall(r"[\x03\x05]([^\x04\x06]*)[\x04\x06]", seg) for k in re.findall(r"\d+|[*†‡§¶#∗˚]", grp)]
        seg = _sp(re.sub(r"[\x03\x05][^\x04\x06]*[\x04\x06]", " ", seg)).strip(" ,;")
        if not seg and marks and out:                        # (marks that stood alone after a comma belong to the name before)
            out[-1][1].extend(marks)
            continue
        if not is_name(seg):
            return []
        out.append((seg, marks))
    return out


def _emails(text):
    out = []
    for m in EMAIL_GROUP.finditer(text):
        out += ["%s@%s" % (u.strip(), m.group(2)) for u in re.split(r"[,;|\s]+", m.group(1)) if u.strip()]
    rest = EMAIL_GROUP.sub(" ", text)
    out += [m.group(0).strip(".,;") for m in EMAIL.finditer(rest)]
    return list(dict.fromkeys(out))


def email_fits(email, name):
    """Whether an address looks like this person's: its part before the @ holds the family name or the given name (their first four letters), or is the initials, or
    begins with an initial and the start of the other name ("jchancf" for Jason Chan)."""
    local = _fold(email.split("@")[0]).replace(" ", "")
    toks = [t for t in _fold(name).split() if t not in PARTICLES]
    if not local or len(toks) < 2:
        return False
    first, last = toks[0], toks[-1]
    if (len(last) >= 3 and last[:4] in local) or (len(first) >= 3 and first[:4] in local):
        return True
    base = re.sub(r"\d+$", "", local)
    ini = "".join(t[0] for t in toks)
    return base in (ini, ini[::-1]) or (len(base) >= 3 and (base.startswith(first[0] + last[:3]) or base.startswith(last[:3] + first[0]) or base.startswith(first[:3] + last[0])))


def _is_place(s):
    toks = [t for t in re.split(r"[,\s]+", _fold(s)) if t]
    return bool(toks) and len(s) <= 60 and not DATED.search(s) and not AFFIL.search(s) and ("," in s or any(t in PLACES for t in toks)) and not EMAIL.search(s) and not re.search(r"\d{3,}", s.replace(" ", "")[:2])


def _front(lines, height, width):
    """(title, the lines between the title and where the paper begins). The title is the largest text in the upper half of the first page."""
    top = [l for l in lines if l.y0 < 0.55 * height and 0.07 * height < l.y1 and len(l.text) >= 3 and not EMAIL.search(l.text)]
    if not top:
        return "", []
    big = max(l.size for l in top if len(_fold(l.text)) >= 6) if any(len(_fold(l.text)) >= 6 for l in top) else max(l.size for l in top)
    tl = [l for l in top if l.size >= big - 0.6]
    if len(tl) > 6:                                     # (the whole page is one size: no title stands out)
        return "", []
    first = tl[0]
    tl = [l for l in tl if l.y0 - first.y0 < 6 * big * max(1, len(tl))]
    title = _sp(" ".join(l.text for l in tl))
    bottom = max(l.y1 for l in tl)
    zone = []
    for l in lines:
        if l.y0 < bottom - 1 or l in tl:
            continue
        if l.y0 >= 0.88 * height:
            break
        if STOP.match(l.text) and len(l.text) < 60:
            break
        if l.x1 - l.x0 > 0.62 * width and len(l.text) >= 60 and not EMAIL.search(l.text) and not AFFIL.search(l.text[:40]) and not _split_names(l):      # (the text has begun)
            break
        zone.append(l)
    return title, zone[:80]


def _authors(zone, width):
    """The authors of the title page, in reading order, each {name, affiliation, place, email}, and how sure the reading is ("high" | "check" | "")."""
    names, details, ptype = [], [], ""
    styles = {}
    for l in zone:
        if PAPER_TYPE.match(l.text) and not ptype:
            ptype = l.text
            continue
        got = _split_names(l) if not EMAIL.search(l.text) and not AFFIL.search(l.text) else []
        if got:
            names.append((l, got))
            styles.setdefault(l.style, 0)
            styles[l.style] += 1
        else:
            details.append(l)
    if not names:
        return [], "", ptype
    # where the names are set in a face of their own (bold, or larger), a line in the face of the affiliations is not a name, whatever its words ("New York")
    detail_styles = {l.style for l in details if AFFIL.search(l.text) or EMAIL.search(l.text)}
    lead = names[0][0].style
    if lead not in detail_styles and detail_styles:
        moved = [(l, g) for l, g in names if l.style in detail_styles]
        names = [(l, g) for l, g in names if l.style not in detail_styles]
        details += [l for l, _ in moved]
        details.sort(key=lambda l: (l.y0, l.x0))
    distinct = lead not in detail_styles and bool(detail_styles)
    authors, anchors = [], []
    for l, got in sorted(names, key=lambda x: (round(x[0].y0 / 4), x[0].x0)):
        group = []
        for nm, marks in got:
            a = {"name": nm, "affiliation": "", "place": "", "email": "", "_marks": marks}
            authors.append(a)
            group.append(a)
        anchors.append((l, group))
    tol = 0.17 * width
    loose_mail, by_mark, shared = [], {}, []
    for l in details:
        text = l.text
        lead_mark = None
        if l.sups and l.sups[0][0] == 0:
            lead_mark = l.sups[0][1]
        else:
            m = LEADING_MARK.match(text) or re.match(r"^\s*(\d{1,2}|[*†‡§¶#∗˚])(?=[^\W\d_])", text)
            if m and any(m.group(1) in a["_marks"] for a in authors) and len(text) <= 160:
                lead_mark, text = m.group(1), text[m.end():]
        mails = _emails(text)
        above = [(al, g) for al, g in anchors if al.y0 < l.y0 + 1 and (abs(al.cx - l.cx) <= tol or (al.x0 - 6 <= l.cx <= al.x1 + 6))]
        owner = max(above, key=lambda x: (x[0].y0, -abs(x[0].cx - l.cx)))[1] if above else None
        if lead_mark is not None:
            by_mark.setdefault(lead_mark, []).append((text, mails))
            continue
        if mails:
            if owner and len(owner) == 1 and len(mails) == 1 and not owner[0]["email"]:
                owner[0]["email"] = mails[0]
            else:
                loose_mail += mails
            continue
        if owner is None:
            continue
        kind = "affiliation" if AFFIL.search(text) else "place" if _is_place(text) else ""
        if not kind:                                     # the first short line under a name, with no word that says what it is ("Stanford & NBER", "OpenAI")
            plain = not DATED.search(text) and not YEAR.search(text) and not re.search(r"[.!?:]$", text) and len(text) <= 60 and len(text.split()) <= 7 and not PAPER_TYPE.match(text)
            kind = "affiliation" if plain and not owner[0]["affiliation"] and not owner[0]["place"] and not any(t in NOT_NAMES - {"of", "and", "for", "the", "at", "in"} for t in _fold(text).split()) else ""
        if not kind or len(text) > 160:
            continue
        if len(owner) > 1:
            shared.append((owner, kind, text))
            continue
        a = owner[0]
        a[kind] = _sp((a[kind] + ", " if a[kind] else "") + text.strip(" ,;"))
    for a in authors:                                    # "15" set after a name is the marks 1 and 5 when no line is marked 15
        a["_marks"] = [k for mk in a["_marks"] for k in ([mk] if mk in by_mark or not (mk.isdigit() and all(c in by_mark for c in mk)) else list(mk))]
    for mark, items in by_mark.items():
        for text, mails in items:
            who = [a for a in authors if mark in a["_marks"]]
            body = _sp(EMAIL.sub("", EMAIL_GROUP.sub("", text))).strip(" ,;")
            for a in who:
                if body and not re.search(r"^(corresponding|equal|these authors|contact|e-?mail|work done)", body, re.I) and (AFFIL.search(body) or len(body) <= 90 and not re.search(r"[.!?]$", body)):
                    a["affiliation"] = _sp((a["affiliation"] + "; " if a["affiliation"] else "") + body)
            loose_mail += mails
    marked = any(a["_marks"] for a in authors)
    for owner, kind, text in shared:                    # a line under several names set on one line: theirs together, unless marks say who belongs where
        for a in owner:
            if not (marked and by_mark) and not a[kind]:
                a[kind] = text.strip(" ,;")
    loose_mail = [m for m in dict.fromkeys(loose_mail) if m not in {a["email"] for a in authors}]
    for m in list(loose_mail):                           # an address belongs to the one author it fits
        fit = [a for a in authors if not a["email"] and email_fits(m, a["name"])]
        if len(fit) == 1:
            fit[0]["email"] = m
            loose_mail.remove(m)
    free = [a for a in authors if not a["email"]]
    if loose_mail and len(loose_mail) == len(free) == len(authors):       # as many addresses as authors, none placed by its words: in the order printed
        for a, m in zip(free, loose_mail):
            a["email"] = m
    # a line in the face of the names that is no person: when every other author has an address that fits and this one, set under the names, has none, it is their
    # shared affiliation ("Google AI Language")
    with_mail = [a for a in authors if a["email"] and email_fits(a["email"], a["name"])]
    without = [a for a in authors if not a["email"]]
    if len(with_mail) >= 2 and len(without) == 1 and authors[-1] is without[0] and not without[0]["affiliation"] and len(anchors[-1][1]) == 1 and anchors[-1][0].y0 > anchors[0][0].y0 + 2:
        shared_name = authors.pop()["name"]
        for a in authors:
            a["affiliation"] = a["affiliation"] or shared_name
    for a in authors:
        a.pop("_marks", None)
    seen, uniq = set(), []
    for a in authors:
        k = _fold(a["name"])
        if k and k not in seen:
            seen.add(k)
            uniq.append(a)
    uniq = uniq[:MAX_AUTHORS]
    backed = [a for a in uniq if (a["email"] and email_fits(a["email"], a["name"])) or a["affiliation"] or (distinct and a["email"])]
    sure = "high" if uniq and len(backed) == len(uniq) else "check"
    return uniq, sure, ptype


# ---------------------------------------------------------------- abstract and keywords

def _join(parts):
    text = ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if text.endswith("-") and p[:1].islower() and not text.endswith(" -"):
            text = text[:-1] + p                     # (a word cut at the end of a line)
        else:
            text += (" " if text else "") + p
    return _sp(text)


def _abstract(pages):
    """(abstract, keywords) from the first two pages: the text under the heading "Abstract" up to the keywords or the first section, and the list after "Keywords:"."""
    lines = [l for ls, h in pages[:2] for l in sorted(ls, key=lambda x: x.n) if 0.06 * h < l.y1 and l.y0 < 0.93 * h]      # (in reading order: a page in two columns)
    abstract, keywords = "", []
    for i, l in enumerate(lines):
        m = ABSTRACT.match(l.text)
        if m and not abstract and (len(l.text) < 12 or l.text[m.end():m.end() + 1].isupper() or m.group(0).strip().lower() != "abstract"):
            parts = [l.text[m.end():]]
            for nx in lines[i + 1:]:
                if KEYWORDS.match(nx.text) or (SECTION.match(nx.text) and len(nx.text) < 60) or nx.page != l.page and len(_join(parts)) > 300:
                    break
                if EMAIL.search(nx.text) and len(nx.text) < 80:
                    continue
                parts.append(nx.text)
                if sum(len(p) for p in parts) > MAX_ABSTRACT:
                    break
            abstract = _join(parts)[:MAX_ABSTRACT]
        k = KEYWORDS.match(l.text)
        if k and not keywords:
            parts = [l.text[k.end():]]
            for nx in lines[i + 1:i + 4]:
                if (SECTION.match(nx.text) or ABSTRACT.match(nx.text) or STOP.match(nx.text)) and len(nx.text) < 60 or nx.y0 - l.y0 > 4.5 * max(l.size, 9) or len(nx.text) > 140 and nx.text.rstrip().endswith("."):
                    break
                if parts[-1].rstrip().endswith((",", ";", "-")) or not re.search(r"[.]\s*$", parts[-1]) and len(nx.text) < 110 and nx.text[:1].islower():
                    parts.append(nx.text)
                else:
                    break
            keywords = [w for w in (_sp(x).strip(" .") for x in re.split(r"\s*[;,·•|]\s*", _join(parts))) if 2 <= len(w) <= 80][:MAX_KEYWORDS]
    if len(abstract) < 80:                              # (a heading with nothing under it, or a word in a sentence)
        abstract = ""
    return abstract, keywords


# ---------------------------------------------------------------- the whole reading

def empty():
    return {"title": "", "authors": [], "abstract": "", "keywords": [], "kind": None, "venue": "", "conference": "", "year": "", "paper_type": "", "sure": "", "by": "",
            "page_text": ""}


def read(data):
    """The details the PDF prints, read by rules: {title, authors: [{name, affiliation, place, email}], abstract, keywords, kind ("conference" or None), venue (as the paper is
    filed: "WITS 2026"), conference (its name in full), year, paper_type, sure ("high": every author is backed by an e-mail that fits the name or by an affiliation set under it or
    marked for it | "check" | "" no author read), by ("layout" | ""), page_text (the first page, for the AI and for checking its answer)}. Never raises for a PDF it
    cannot read: it answers with nothing found."""
    out = empty()
    try:
        import pymupdf
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            if not len(doc):
                return out
            pages = []
            for i in range(min(3, len(doc))):
                pg = doc[i]
                pages.append((_lines(pg, i), pg.rect.height or 1))
            width = doc[0].rect.width or 1
            out["page_text"] = doc[0].get_text()[:8000]
    except Exception:  # noqa: BLE001
        return out
    lines, height = pages[0]
    kind, venue, year, full = _venue(pages)
    running = set()
    if len(pages) > 1:                                  # a line that stands in the margin of two pages is the running head, not the title
        norm = lambda l: re.sub(r"\d+", "", _fold(l.text))    # noqa: E731
        later = {norm(l) for ls, h in pages[1:] for l in ls if l.y1 <= 0.11 * h or l.y0 >= 0.88 * h}
        running = {id(l) for l in lines if (l.y1 <= 0.11 * height or l.y0 >= 0.88 * height) and norm(l) in later and norm(l)}
    body = [l for l in lines if id(l) not in running]
    title, zone = _front(body, height, width)
    authors, sure, ptype = _authors(zone, width)
    abstract, keywords = _abstract(pages)
    if not year:
        head = " ".join(l.text for l in zone)
        m = DATED.search(head)
        year = (m.group(1) or m.group(2)) if m else ""
    out.update(title=title, authors=authors, abstract=abstract, keywords=keywords, kind=kind, venue=venue, conference=full, year=year, paper_type=_sp(ptype),
               sure=sure if authors else "", by="layout" if authors else "")
    return out


# ---------------------------------------------------------------- the AI as a second reader

AI_MAX_CHARS, AI_OUT_TOKENS = 6000, 1200
AI_SYSTEM = ("You read the first page of a research paper, given as plain text, and report what it prints about the paper. Copy every value exactly as it is printed; never "
             "complete, translate or guess. authors: every author in the order printed, each with the affiliation (institution), the place (city, region, country) and "
             "the e-mail address printed for that author; use an empty string for what is not printed. conference: the name of the conference or workshop the paper "
             "was submitted to, only when the page itself states it (for example in its header or footer), else an empty string. year: the four-digit year printed with "
             "that conference or as the paper's date, else an empty string. paper_type: a line such as “Regular Paper” or “Research-in-Progress”, else an empty string. "
             "keywords: the paper's own keywords when printed, else an empty list.")
AI_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["title", "authors", "conference", "year", "paper_type", "keywords"], "properties": {
    "title": {"type": "string"}, "conference": {"type": "string"}, "year": {"type": "string"}, "paper_type": {"type": "string"}, "keywords": {"type": "array", "items": {"type": "string"}},
    "authors": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["name", "affiliation", "place", "email"],
                                           "properties": {"name": {"type": "string"}, "affiliation": {"type": "string"}, "place": {"type": "string"}, "email": {"type": "string"}}}}}}


def printed(value, page_fold):
    """Whether these words stand on the page, in this order (accents, case and punctuation aside)."""
    v = _fold(value)
    return bool(v) and v in page_fold


def ai_estimate(got):
    import ai
    provider, model = ai.feature_model("details")
    return {"cost": ai.estimate("details", min(len(got.get("page_text") or ""), AI_MAX_CHARS) + len(AI_SYSTEM) + 200, 400), "provider": provider, "model": model}


def needs_ai(got):
    """The rules did not do the job: no author was read, or the reading is not backed by the page's own signs (an e-mail that fits, names in a face of their own)."""
    return bool((got.get("page_text") or "").strip()) and got.get("sure") != "high"


def with_ai(data, got=None):
    """Ask the AI to read the first page (one call through the budgeted layer: the month's budget, local-only mode and the cost ledger apply), and keep only what is
    literally printed on the page: a name, an affiliation, a place or an address the page does not hold is dropped, so the model cannot add an author. Returns
    (details, note): the details are `got` when the AI could not be asked or added nothing, with the reason in `note`."""
    import json
    import ai
    got = dict(got or read(data))
    page = got.get("page_text") or ""
    if not page.strip():
        return got, "The first page has no text to read (a scan)."
    try:
        res = ai.chat("details", [{"role": "user", "content": "First page:\n\n" + page[:AI_MAX_CHARS]}], system=AI_SYSTEM, schema=AI_SCHEMA, max_tokens=AI_OUT_TOKENS)
        d = json.loads(res.text if res else "")
    except ai.AIError as e:
        return got, str(e)
    except ValueError:
        return got, "The AI’s answer could not be read."
    if not isinstance(d, dict):
        return got, "The AI’s answer could not be read."
    fold = _fold(page)
    fold_tight = fold.replace(" ", "")
    authors = []
    for a in d.get("authors") or []:
        if not isinstance(a, dict) or not printed(a.get("name"), fold) or not 3 <= len(_sp(a.get("name"))) <= 80:
            continue
        mail = _sp(a.get("email"))
        mail = mail if EMAIL.fullmatch(mail) and _fold(mail).replace(" ", "") in fold_tight else ""
        authors.append({"name": _sp(a.get("name")), "affiliation": _sp(a.get("affiliation")) if printed(a.get("affiliation"), fold) else "",
                        "place": _sp(a.get("place")) if printed(a.get("place"), fold) else "", "email": mail})
    seen, uniq = set(), []
    for a in authors:
        if _fold(a["name"]) not in seen:
            seen.add(_fold(a["name"]))
            uniq.append(a)
    out = dict(got)
    mine = [_fold(a["name"]) for a in got.get("authors") or []]
    if uniq:
        agree = mine == [_fold(a["name"]) for a in uniq]
        if agree:                                         # both readers found the same people: keep the rules' reading, filled in where it had nothing
            merged = []
            for a, b in zip(got["authors"], uniq):
                merged.append({k: a.get(k) or b.get(k) or "" for k in ("name", "affiliation", "place", "email")})
            out["authors"], out["sure"], out["by"] = merged, "high", "layout+ai"
        else:
            out["authors"], out["sure"], out["by"] = uniq[:MAX_AUTHORS], "check", "ai"
    if not out.get("title") and printed(d.get("title"), fold) and len(_sp(d.get("title"))) >= 8:
        out["title"] = _sp(d.get("title"))
    conf, yr = _sp(d.get("conference")), _sp(d.get("year"))
    if not out.get("venue") and printed(conf, fold) and kinds.CONF_WORDS.search(conf) and not TOLD.search(conf):
        yr = yr if re.fullmatch(r"(19[5-9]\d|20\d{2})", yr) and yr in page else ""
        short = next((name for rx, name in kinds._CONF_NAMES if rx.search(conf)), "")
        out.update(kind="conference", conference=conf, venue=_sp("%s %s" % (short or conf, yr)), year=out.get("year") or yr)
    if not out.get("paper_type") and printed(d.get("paper_type"), fold) and len(_sp(d.get("paper_type"))) <= 60:
        out["paper_type"] = _sp(d.get("paper_type"))
    if not out.get("keywords"):
        out["keywords"] = [_sp(k) for k in (d.get("keywords") or []) if isinstance(k, str) and printed(k, fold) and 2 <= len(_sp(k)) <= 80][:MAX_KEYWORDS]
    out["ai"] = {"cost": getattr(res, "cost", None), "provider": getattr(res, "provider", ""), "model": getattr(res, "model", "")}
    return out, ("" if uniq or mine else "The AI found no author printed on the first page.")


# ---------------------------------------------------------------- details as they are stored

FIELDS = {"abstract": MAX_ABSTRACT, "conference": 200, "paper_type": 60, "session": 200, "volume": 20, "issue": 20, "pages": 30, "note": 2000}


def clean(d, keep_names=False):
    """Details as they are kept beside the PDF: {authors: [{name, affiliation, place, email}], keywords: [...], abstract, conference, paper_type, session, volume, issue,
    pages, note}; anything else, and anything of the wrong shape, is left out. Empty values are not stored. The authors are kept when one of them has an affiliation,
    a place or an address (the names alone are in the index already), or always with keep_names (a correction of the names)."""
    d = d if isinstance(d, dict) else {}
    out = {}
    people = []
    for a in (d.get("authors") if isinstance(d.get("authors"), list) else [])[:MAX_AUTHORS + 40]:
        if isinstance(a, str):
            a = {"name": a}
        if not isinstance(a, dict) or not _sp(a.get("name")):
            continue
        mail = _sp(a.get("email"))[:120]
        row = {"name": _sp(a.get("name"))[:120], "affiliation": _sp(a.get("affiliation"))[:200], "place": _sp(a.get("place"))[:120], "email": mail if EMAIL.fullmatch(mail) else ""}
        people.append(row)
    people = people[:MAX_AUTHORS + 40]
    if people and (keep_names or any(a["affiliation"] or a["place"] or a["email"] for a in people)):
        out["authors"] = people
    kw = d.get("keywords")
    kw = re.split(r"\s*[;\n]\s*", kw) if isinstance(kw, str) else kw if isinstance(kw, list) else []
    kw = list(dict.fromkeys(w for w in (_sp(x)[:80] for x in kw if isinstance(x, str)) if w))[:MAX_KEYWORDS]
    if kw:
        out["keywords"] = kw
    for k, limit in FIELDS.items():
        v = d.get(k)
        v = (str(v).strip() if k in ("abstract", "note") else _sp(v))[:limit] if isinstance(v, (str, int)) else ""
        if v:
            out[k] = v
    return out
