"""What kind of work a paper is: a journal article, a preprint or working paper, a conference paper, or something else (a report, a thesis, a chapter); books are books.py.

    kinds.from_crossref(meta)        (kind, venue, label) from a Crossref record (its "type", container, group title, institution, event)
    kinds.from_text(text)            (kind, venue, label) from what a PDF prints on its first page, or (None, "", "") when it says nothing clear
    kinds.clean_venue(name, kind)    a venue name that is safe as one folder name (Papers/YYYY-MM/<venue>/), or the kind's own folder when there is none
    kinds.venue_abbr(name, kind, taken)   a short abbreviation for a venue that is not a watched journal (ICIS, HICSS, SSRN…), never one already used by another venue
    kinds.read_sidecar(pdf, key) / kinds.write_sidecar(pdf, …)   <stem>.paper.json next to the PDF: {kind, venue, label, crossref_type, for}
    kinds.of_row(row, sidecar)       the kind of a library row: its sidecar, else what its DOI or journal says (a book; a preprint series), else a journal article

Why a file next to the PDF and not a column of Papers/_index.csv: versions of the app before this one write the index with a fixed list of columns and stop with an
error on any other, and two computers that share the data folder may run different versions for a while. The sidecar is ignored by older versions. It names the paper
it was written for ("for": the index key), so a file left behind never gives its kind to another paper filed at the same place later.
"""
import json
import os
import re
import unicodedata

KINDS = ("journal", "preprint", "conference", "other", "book")
PAPER_KINDS = KINDS[:4]
WORDS = {"journal": "Journal article", "preprint": "Preprint or working paper", "conference": "Conference paper", "other": "Report, thesis or other", "book": "Book"}
TAGS = {"preprint": "Preprint", "conference": "Conference", "other": "Other"}          # the small tag on a library row (none for a journal article); a label (Report…) wins
LABELS = ("Report", "Thesis", "Chapter", "Dataset", "Other")
FALLBACK_VENUE = {"journal": "Other", "other": "Other", "preprint": "Preprints", "conference": "Conference papers"}
FALLBACK_ABBR = {"journal": "OTH", "other": "OTH", "preprint": "PRE", "conference": "CONF"}
FROM_AUTHORS = {"journal": "journal", "conference": "conference", "working": "preprint"}    # authors.kind's words

# DOI prefixes that are preprint or working-paper series (one table: authors.py and fetch_new use it too)
PREPRINT_SERIES = {"10.2139": "SSRN", "10.48550": "arXiv", "10.3386": "NBER Working Papers", "10.31219": "OSF Preprints", "10.31234": "PsyArXiv", "10.31235": "SocArXiv",
                   "10.21203": "Research Square", "10.20944": "Preprints.org", "10.36227": "TechRxiv", "10.22541": "Authorea"}
SERIES_ABBR = {"SSRN": "SSRN", "arXiv": "ARXIV", "NBER Working Papers": "NBER", "OSF Preprints": "OSF", "PsyArXiv": "PSYARX", "SocArXiv": "SOCARX",
               "Research Square": "RSQ", "Preprints.org": "PREPR", "TechRxiv": "TECHRX", "Authorea": "AUTHOR"}

CONF_WORDS = re.compile(r"proceedings|conference|lecture notes|symposium|workshop|congress", re.I)
SMALL = {"of", "the", "and", "in", "for", "on", "to", "a", "an", "at", "by", "&", "annual", "proceedings", "international"}


def prefix(doi):
    doi = str(doi or "").strip().lower()
    return doi.split("/", 1)[0] if "/" in doi else ""


def series_of(doi):
    """The preprint series of a DOI ("SSRN", "arXiv", …), or ""."""
    return PREPRINT_SERIES.get(prefix(doi), "")


def from_crossref(meta):
    """(kind, venue, label) for a Crossref record (importer._meta_from_item). A journal article of a preprint series' DOI (SSRN's old records say "SSRN Electronic
    Journal") is a preprint of that series."""
    m = meta or {}
    t = str(m.get("type") or "").lower()
    cont = str(m.get("container") or "").strip()
    series = series_of(m.get("doi"))
    if t in ("", "journal-article", "journal-issue", "journal"):
        return ("preprint", series, "") if series else ("journal", cont, "")
    if t in ("posted-content",):
        gt = str(m.get("group_title") or "")                    # (bioRxiv's group-title is a subject, "Neuroscience": only a server's or a series' name is a venue)
        gt = gt if re.search(r"rxiv|preprint|working paper|discussion paper|ssrn|research square|repec", gt, re.I) else ""
        return "preprint", series or gt or str(m.get("institution") or "") or str(m.get("publisher") or ""), ""
    if t in ("proceedings-article", "proceedings", "proceedings-series"):
        return "conference", cont or str(m.get("event") or ""), ""
    if t in ("book-chapter", "book-section", "book-part", "reference-entry") and CONF_WORDS.search(cont):
        return "conference", cont, ""
    if t in ("report", "report-component", "report-series"):
        return ("preprint", series, "") if series else ("other", cont or str(m.get("institution") or "") or str(m.get("publisher") or ""), "Report")
    if t in ("dissertation",):
        return "other", str(m.get("institution") or m.get("publisher") or ""), "Thesis"
    if t in ("book-chapter", "book-section", "book-part", "reference-entry"):
        return "other", cont, "Chapter"
    if t in ("dataset",):
        return "other", cont or str(m.get("publisher") or ""), "Dataset"
    return "other", cont, ""


# conference footers that name the conference (AIS conferences have no Crossref DOI): "Forty-Fourth International Conference on Information Systems, Hyderabad 2023"
_CONF_NAMES = [(re.compile(r"international conference on information systems", re.I), "ICIS"), (re.compile(r"hawaii international conference on system sciences", re.I), "HICSS"),
               (re.compile(r"european conference on information systems", re.I), "ECIS"), (re.compile(r"pacific asia conference on information systems", re.I), "PACIS"),
               (re.compile(r"americas conference on information systems", re.I), "AMCIS"), (re.compile(r"\bCHI Conference on Human Factors in Computing Systems", re.I), "CHI")]


def from_text(text):
    """(kind, venue, label) from the first page of a PDF, or (None, "", "") when it says nothing clear. Only the page's first and last lines are read (its header and
    footer): a journal article may mention a conference or a thesis in its footnotes or its text."""
    lines = [l.strip() for l in str(text or "").splitlines() if l.strip()]
    edge = "\n".join(lines[:12] + lines[-8:])
    if re.search(r"\barXiv:\s?\d{4}\.\d{4,5}(v\d+)?", edge):
        return "preprint", "arXiv", ""
    if re.search(r"NBER\s+WORKING\s+PAPER\s+SERIES", edge, re.I) and re.search(r"Working\s+Paper\s+\d+", edge, re.I):
        return "preprint", "NBER Working Papers", ""
    if re.search(r"ssrn\.com/abstract\s*=\s*\d+", edge, re.I) and not re.search(r"journal|vol\.|volume", edge[:400], re.I):
        return "preprint", "SSRN", ""
    told = re.compile(r"presented|earlier version|previous version|based on|accepted|forthcoming", re.I)      # a footnote about a talk is not the paper's own venue
    edge_lines = lines[:12] + lines[-8:]
    seam = min(12, len(lines))                     # (the head's last line and the foot's first are not neighbours)
    begun = re.compile(r"\b(presented|appeared)\s+(at|in)\b[^.]{0,40}$|\b(earlier|previous) version\b[^.]{0,60}$", re.I)
    said = lambda i: told.search(edge_lines[i]) or (0 < i != seam and len(lines) > 20 and begun.search(edge_lines[i - 1]))      # (a footnote wrapped over two lines: "…presented at the Fortieth" / "International Conference…")
    for i, ln in enumerate(edge_lines):
        y = re.search(r"\b(19|20)\d{2}\b", ln)
        if not y or said(i):
            continue
        for rx, name in _CONF_NAMES:
            if rx.search(ln):
                return "conference", name + " " + y.group(0), ""
    for i, ln in enumerate(edge_lines):
        m = re.search(r"Proceedings of the [^|]{3,90}?(Conference|Symposium|Workshop)[^,|]{0,40}", ln)
        if m and not said(i):
            return "conference", m.group(0).strip(), ""
    if thesis_in(text):
        return "other", "", "Thesis"
    if re.search(r"^\s*technical report\b", edge, re.I | re.M):
        return "other", "", "Report"
    return None, "", ""


_THESIS = re.compile(r"\b(a (doctoral |master'?s )?(thesis|dissertation) submitted|in partial fulfil{1,2}ment of the requirements|submitted (in partial fulfil{1,2}ment|for the degree))", re.I)
_ABOUT = re.compile(r"based on|draws on|this (article|paper|study|work)|chapter of|from (my|the author'?s?|the first author'?s?) (doctoral|phd|master)", re.I)


def thesis_in(text):
    """Does the cover (the first 25 lines) say this is a thesis? A line that says the article is based on a dissertation (a footnote) does not count."""
    lines = [l.strip() for l in str(text or "").splitlines() if l.strip()][:25]
    return any(_THESIS.search(l) and not _ABOUT.search(l) for l in lines) or any(re.fullmatch(r"doctoral dissertations?( \d+/\d{4})?", l, re.I) for l in lines)


_DEVICE = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])$", re.I)


def clean_venue(name, kind="journal"):
    """A venue name that is safe as ONE folder name: no path separators or characters a disk refuses, no control characters, at most 80 characters (cut at a word),
    no dots or spaces at the ends, not "." or "..", not a Windows device name, not "Books" (the books' folder) for a paper. Empty: the kind's own folder."""
    s = unicodedata.normalize("NFC", str(name or ""))
    s = "".join(ch for ch in s if unicodedata.category(ch)[0] != "C")
    s = re.sub(r"[\\/:*?\"<>|]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > 80:
        s = s[:80].rsplit(" ", 1)[0] if " " in s[:80] else s[:80]
    s = s.strip(" .")
    if not s:
        return FALLBACK_VENUE.get(kind, "Other")
    if _DEVICE.match(s):
        s += " (venue)"
    if s.lower() == "books" and kind != "book":
        s = "Books (series)"
    return s


def _ascii(s):
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")


def venue_abbr(name, kind="journal", taken=()):
    """A short abbreviation for a venue that is not a watched journal: a known series (SSRN, ARXIV, NBER…); else an acronym the name carries (ICIS, HICSS, CHI; not ACM,
    IEEE or a Roman numeral); else the initials of its words (without "Proceedings of the", ordinals and years); else the kind's own (OTH, PRE, CONF). Never one of
    `taken` (abbreviations other venues already use: a digit is added) and never BOOK (the books' mark)."""
    name = str(name or "").strip()
    taken = {str(t).upper() for t in taken if t}
    a = SERIES_ABBR.get(name, "") or {v: FALLBACK_ABBR[k] for k, v in FALLBACK_VENUE.items()}.get(name, "")      # (a kind's own folder keeps its own: CONF, PRE, OTH)
    if not a and kind == "journal":                # a journal: the initials of its capitalised words, as the app has always made them ("Test Journal of IS" → TJI)
        a = "".join(w[0] for w in re.findall(r"[A-Za-z][A-Za-z]+", _ascii(name)) if w[0].isupper() and w.lower() not in ("of", "the", "and"))[:6].upper()
    if not a:
        for rx, short in _CONF_NAMES:
            if rx.search(name) or re.fullmatch(r"%s(\s+\d{4})?(\s+proceedings)?" % short, name, re.I):
                a = short
                break
    if not a:
        acr = [w for w in re.findall(r"\b[A-Z]{2,6}\b", name) if w not in ("ACM", "IEEE", "AIS") and not re.fullmatch(r"[IVXLC]+", w)]
        a = acr[0] if acr else ""
    if not a:
        plain = re.sub(r"(?i)\bproceedings of the\b|\b\d+(st|nd|rd|th)\b|\b(19|20)\d{2}\b", " ", _ascii(name))
        words = [w for w in re.findall(r"[A-Za-z][A-Za-z]+", plain) if w.lower() not in SMALL and not re.fullmatch(r"(?i)(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|[a-z]+-?(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth))", w)]
        a = ("".join(w[0].upper() for w in words)[:6] if len(words) >= 2 else (words[0][:5].upper() if words else "")) or ""
    a = a or FALLBACK_ABBR.get(kind, "OTH")
    if a == "BOOK":
        a = "BOOKS"
    base, n = a, 2
    while a in taken:
        a = "%s%d" % (base[:5], n)
        n += 1
    return a


def sidecar_path(pdf):
    return pdf[:-4] + ".paper.json"


def read_sidecar(pdf, key=None, refused=None):
    """The sidecar of the PDF at this absolute path ({} when there is none, it cannot be read, or it was written for another paper than `key`).
    refused: what to answer instead of {} when the file is there but the system would not let it be read (the library list does not remember such an answer)."""
    p = sidecar_path(pdf) if isinstance(pdf, str) and pdf.lower().endswith(".pdf") else ""
    if not p or not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except OSError:
        return {} if refused is None else refused
    except ValueError:
        return {}
    if not isinstance(d, dict) or d.get("kind") not in PAPER_KINDS or (key and d.get("for") and str(d.get("for")).lower() != str(key).lower()):
        return {}
    return d


def write_sidecar(pdf, kind, venue="", label="", key="", crossref_type=""):
    """Write (or replace) the sidecar of the PDF: {kind, venue, label, crossref_type, for}. Written for journal articles too, so an old one at the same place never stays."""
    kind = kind if kind in PAPER_KINDS else "journal"
    d = {"kind": kind, "venue": str(venue or "")[:200], "label": label if label in LABELS else "", "crossref_type": str(crossref_type or "")[:40], "for": str(key or "")}
    tmp = sidecar_path(pdf) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, sidecar_path(pdf))
    return d


def of_row(row, side=None):
    """(kind, label) of a library row: its sidecar; else a book (journal "Books"), a preprint series' DOI, a watched preprint series (abbr NBER, SSRN); else a journal article."""
    side = side or {}
    if side.get("kind") in PAPER_KINDS:
        return side["kind"], side.get("label") or ""
    if (row.get("journal") or "") == "Books":
        return "book", ""
    if series_of(row.get("doi")) or (row.get("abbr") or "").upper() in ("SSRN", "NBER", "ARXIV"):
        return "preprint", ""
    return "journal", ""


def tag(kind, label=""):
    """The word on a library row's tag ("" for a journal article or a book)."""
    if kind in ("journal", "book"):
        return ""
    return label if label and kind == "other" else TAGS.get(kind, "")
