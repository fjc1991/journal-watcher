#!/usr/bin/env python3
"""Is this PDF the paper we asked for?

A download is only a paper if the file that came back is that paper. Every route that fetches a PDF (the publisher through your sign-in, Unpaywall, the library, a free copy
elsewhere) can return a *different* complete PDF: a paywall stub, a reference the page linked to, a preprint of another work. Before 1.2 the app accepted any complete PDF.
check() compares what the PDF says (its first pages, its metadata) with what we know about the paper (DOI, title, authors) and answers one of

    match       the DOI is printed on the first pages, or the title (and, when it is not long enough to be unique, the authors) is
    mismatch    the PDF is demonstrably something else: refuse it, and say what it is
    unverified  there is nothing to compare (a scan with no text, no title or DOI on record, an unreadable file): accepted, but noted as "identity not checked"

Only PyMuPDF is used (it is a hard dependency of the app), so the check is never silently skipped because an optional package is missing.

    python identity.py audit [--json]      read-only: list the PDFs in your library (Papers/_index.csv) that do not look like the paper they are filed under
"""
import csv
import difflib
import html
import json
import os
import re
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402

# --- the rules, in one place ---------------------------------------------------------------------------------------------------------------------------------------
BODY_PAGES = 3          # compare with the first pages that carry real text (cover sheets and legal pages are skipped) ...
MAX_PAGES = 6           # ... looking at no more than this many pages
THIN_WORDS = 150        # a page with fewer words than this is not a "body" page
NO_TEXT_WORDS = 80      # fewer words than this in all the pages read, and some images: a scan
TITLE_FULL = 0.90       # share of the title found (in order) in the text: enough on its own
TITLE_PARTIAL = 0.70    # ... or this much, with the authors
AUTHORS_MIN = 0.50      # share of the authors' family names found as whole words
MAIN_TITLE_MIN = 20     # letters in the part of the title before a colon or dash, for "the title without its subtitle"
SHORT_TITLE_LETTERS = 25
SHORT_TITLE_WORDS = 3   # a title of fewer content words than this, or fewer letters than above, is too generic to identify a paper by itself

STOP = set("the a an of and in on for to from with by at as or is are be".split())
REFHEAD = re.compile(r"^\s*(?:\d+[.)]?\s*)?(references|bibliography|works cited|literature cited)\s*$", re.I | re.M)
DOI_TOKEN = re.compile(r"10\.\d{4,9}/\S+")


# --- text helpers ---------------------------------------------------------------------------------------------------------------------------------------------------
def fold(s):
    """Lower-case, no accents, ligatures spelled out (ﬁ -> fi), soft hyphens removed."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.replace("­", "").replace("‐", "-").replace("‑", "-").lower()


def squash(s):
    """Letters and digits only: hyphenation, line breaks, dashes, quotes and spacing cannot make a title fail to match."""
    return re.sub(r"[^a-z0-9]", "", fold(s))


def plain_title(title):
    """Crossref titles can carry markup and entities (<i>Bayesian</i>, <sub>2</sub>, &amp;, MathML): the PDF prints the text only."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html.unescape(title or ""))).strip()


def content_words(title):
    return [w for w in re.findall(r"[a-z]+", fold(title)) if w not in STOP and len(w) > 2]


# --- reading the PDF -------------------------------------------------------------------------------------------------------------------------------------------------
def read_pdf(data):
    """What the first pages say. Returns dict(text, meta, images, headline, pages, words); raises on a file PyMuPDF cannot open."""
    import pymupdf
    with pymupdf.open(stream=data, filetype="pdf") as d:
        parts, got, images, first_body = [], 0, 0, None
        for i in range(min(MAX_PAGES, len(d))):
            t = d[i].get_text()
            parts.append(t)
            images += len(d[i].get_images())
            if len(t.split()) >= THIN_WORDS:
                got += 1
                if first_body is None:
                    first_body = i
            if got >= BODY_PAGES:
                break
        text = "\n".join(parts)
        m = REFHEAD.search(text)
        if m:                                    # a paper that merely CITES the expected one must not match through its reference list
            text = text[:m.start()]
        meta = " ".join(str(v) for v in (d.metadata or {}).values() if v)
        headline = ""
        if len(d):
            spans = []
            for b in d[first_body or 0].get_text("dict")["blocks"]:         # the title page, not a cover sheet
                for ln in b.get("lines", []):
                    for sp in ln["spans"]:
                        if len(sp["text"].strip()) > 2:
                            spans.append((sp["size"], sp["text"].strip()))
            if spans:
                top = max(s for s, _ in spans)
                headline = " ".join(t for s, t in spans if s >= top - 0.5)[:140]
        return {"text": text, "meta": meta, "images": images, "headline": headline, "pages": len(d), "words": len(text.split())}


# --- the three kinds of evidence ------------------------------------------------------------------------------------------------------------------------------------
def doi_found(doi, text):
    """The expected DOI is printed as a whole DOI (not as the start of a longer one), also when a line break splits it."""
    if not doi:
        return False
    t = re.sub(r"\s+", " ", (text or "").replace("­", ""))
    for _ in range(3):                           # "10.1287/mnsc.\n2024.0001": rejoin a DOI broken after . / _ -
        t = re.sub(r"(10\.\d{4,9}/\S*[./_-]) (?=\S)", r"\1", t)
    want = doi.strip().lower().rstrip(".,;:]}>\"'")
    for tok in DOI_TOKEN.findall(t.lower()):
        for cand in {tok, tok.rstrip(".,;:)]}>\"'")}:
            if cand == want or (cand.startswith(want) and not cand[len(want)].isalnum()):
                return True
    return False


def title_score(title, text):
    """0..1: how much of the title is found, in order, in the text (both squashed)."""
    t, s = squash(title), squash(text)
    if not t:
        return 0.0
    if t in s:
        return 1.0
    sm = difflib.SequenceMatcher(None, t, s, autojunk=False)
    got = sum(b.size for b in sm.get_matching_blocks() if b.size >= 5)       # runs of at least 5 characters, in order
    return min(1.0, got / len(t))


def main_title_found(title, text):
    """The title without its subtitle (the part before a colon or a dash) is in the text: journals often print the subtitle apart."""
    main = squash(re.split(r"\s*[:–—]\s+| - ", title or "", maxsplit=1)[0])
    return len(main) >= MAIN_TITLE_MIN and main in squash(text)


def author_names(authors):
    if isinstance(authors, str):
        authors = [a for a in re.split(r"\s*;\s*", authors) if a.strip()]
    fams = []
    for a in authors or []:
        a = (a or "").strip()
        if not a:
            continue
        if "," in a:                                                 # "Tian, Jing": the family name comes first
            fam = a.split(",")[0]
        else:                                                        # "Jing Tian", "Jing Tian Jr.": the last word
            toks = re.sub(r"[,\s]+(jr|sr|ii|iii|iv)\.?$", "", a, flags=re.I).split()
            fam = toks[-1] if toks else ""
        parts = [re.sub(r"[^a-z]", "", p) for p in re.split(r"[-\s]+", fold(fam))]       # "Smith-Jones": either part
        parts = [p for p in parts if len(p) >= 3]
        if parts:
            fams.append(parts)
    return fams


def authors_score(authors, text):
    """Share of the authors whose family name appears as a whole word; None when we know no author."""
    fams = author_names(authors)
    if not fams:
        return None
    words = set(re.findall(r"[a-z]+", fold(text)))
    return sum(any(p in words for p in parts) for parts in fams) / len(fams)


# --- the verdict -----------------------------------------------------------------------------------------------------------------------------------------------------
def check(entry, data):
    """entry: dict with title, doi, authors (a list, or "A; B"). data: the PDF's bytes. Returns (verdict, reason, evidence)."""
    title, doi, authors = plain_title(entry.get("title")), (entry.get("doi") or "").strip(), entry.get("authors") or []
    ev = {"doi": False, "title": 0.0, "authors": None, "words": 0, "pdf_says": ""}
    if not title and not doi:
        return "unverified", "there is no title or DOI on record to compare with", ev
    try:
        r = read_pdf(data)
    except Exception as e:  # noqa: BLE001  (a PDF that PyMuPDF cannot open is not proof of anything)
        return "unverified", "the PDF could not be read (%s)" % str(e)[:60], ev
    says = r["headline"]
    ev.update(doi=doi_found(doi, r["text"] + " " + r["meta"]), words=r["words"], pdf_says=says)
    if title:
        ev["title"] = round(title_score(title, r["text"]), 2)
        ev["authors"] = authors_score(authors, r["text"])
    a = ev["authors"]
    if ev["doi"]:
        return "match", "its DOI is printed on the first pages", ev
    if r["words"] < NO_TEXT_WORDS and r["images"]:
        return "unverified", "the PDF has no text to compare (a scan?)", ev
    if not title:
        return "unverified", "the DOI is not printed on the first pages and there is no title to compare", ev
    short = len(squash(title)) < SHORT_TITLE_LETTERS or len(content_words(title)) < SHORT_TITLE_WORDS
    if short:
        if ev["title"] >= TITLE_FULL and a is not None and a >= AUTHORS_MIN:
            return "match", "its title and authors are on the first pages", ev
        why = "the title is too short to identify it alone and neither the DOI nor the authors are on the first pages"
    elif ev["title"] >= TITLE_FULL:
        return "match", "its title is on the first pages", ev
    elif ev["title"] >= TITLE_PARTIAL and a is not None and a >= AUTHORS_MIN:
        return "match", "most of its title and its authors are on the first pages", ev
    elif main_title_found(title, r["text"]) and a is not None and a >= AUTHORS_MIN:
        return "match", "its main title and authors are on the first pages", ev
    else:
        why = "its title is not on the first pages (%d%% found)" % round(100 * ev["title"])
    if says:
        why = "this PDF is titled “%s”: %s" % (says[:90], why)
    return "mismatch", why, ev


def explain(verdict, reason):
    """One line for the download note and the card."""
    return "wrong paper: %s" % reason if verdict == "mismatch" else reason


# --- the audit of a library ----------------------------------------------------------------------------------------------------------------------------------------
def audit(progress=None):
    """Read-only. For every row of Papers/_index.csv with status ok and a file, what check() says. Returns a list of dicts, mismatches first, then unverified."""
    idx = os.path.join(paths.PAPERS, "_index.csv")
    rows = []
    if os.path.exists(idx):
        with open(idx, newline="", encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r.get("status") == "ok" and r.get("file")]
    out = []
    for n, r in enumerate(rows, 1):
        f = os.path.join(paths.ROOT, r["file"])
        item = {"file": r["file"], "title": r.get("title", ""), "doi": r.get("doi", ""), "journal": r.get("journal", ""), "note": r.get("note", "")}
        if not os.path.exists(f):
            item.update(verdict="missing", reason="the file is not there", pdf_says="")
        else:
            with open(f, "rb") as fh:
                data = fh.read()
            v, why, ev = check({"title": r.get("title"), "doi": r.get("doi"), "authors": r.get("authors")}, data)
            item.update(verdict=v, reason=why, pdf_says=ev["pdf_says"])
        out.append(item)
        if progress:
            progress(n, len(rows))
    order = {"mismatch": 0, "unverified": 1, "missing": 2, "match": 3}
    return sorted(out, key=lambda x: order.get(x["verdict"], 9))


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    if not a or a[0] != "audit":
        print(__doc__)
        return 2
    res = audit()
    if "--json" in a:
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return 0
    bad = [r for r in res if r["verdict"] == "mismatch"]
    print("%d PDFs checked (read-only): %d do not look like the paper they are filed under, %d could not be checked, %d match."
          % (len(res), len(bad), sum(r["verdict"] in ("unverified", "missing") for r in res), sum(r["verdict"] == "match" for r in res)))
    for r in res:
        if r["verdict"] != "match":
            print("\n[%s] %s\n   expected: %s\n   %s" % (r["verdict"].upper(), r["file"], r["title"], r["reason"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
