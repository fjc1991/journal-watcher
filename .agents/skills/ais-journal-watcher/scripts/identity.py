#!/usr/bin/env python3
"""Is this PDF the paper we asked for?

A download is only a paper if the file that came back is that paper. Every route that fetches a PDF (the publisher through your sign-in, Unpaywall, the library, a free copy
elsewhere) can return a *different* complete PDF: a paywall stub, a reference the page linked to, a preprint of another work. Before 1.2 the app accepted any complete PDF.
check() compares what the PDF says (its first pages, its embedded title) with what we know about the paper (DOI, title, authors) and answers one of

    match       the DOI is printed on the first pages, or the title is (and, when it is too short to be unique, the authors too, or it is the page's own headline)
    mismatch    the PDF is demonstrably something else (another work, a stub, an appendix or a correction): refuse it, and say what it is
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
import shutil
import sys
import time
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402

# --- the rules, in one place ---------------------------------------------------------------------------------------------------------------------------------------
BODY_PAGES = 3          # compare with the first pages that carry real text (cover sheets and legal pages are skipped) ...
MAX_PAGES = 6           # ... looking at no more than this many pages
THIN_WORDS = 150        # a page with fewer words than this is not a "body" page
SCAN_COVER = 0.5        # a page is a picture (a scan) when one image covers at least this share of it
TITLE_FULL = 0.90       # share of the title found (in order) in the text: enough on its own
TITLE_PARTIAL = 0.70    # ... or this much, with the authors
AUTHORS_MIN = 0.50      # share of the authors' family names found as whole words
MAIN_TITLE_MIN = 20     # letters in the part of the title before a colon or dash, for "the title without its subtitle" (with the authors)
SHORT_TITLE_LETTERS = 25
SHORT_TITLE_WORDS = 3   # a title of fewer content words than this, or fewer letters than above, is too generic to identify a paper by itself
HEADLINE_MIN_LETTERS = 12   # a short title with no author on record is accepted when it IS the page's headline, if it has at least this many letters ...
HEADLINE_MIN_WORDS = 2      # ... and this many content words
REF_AFTER_WORDS = 150   # a "References" heading counts only after this many words of the text (a sidebar listing of headings must not cut the title off)

STOP = set("the a an of and in on for to from with by at as or is are be".split())
REFHEAD = re.compile(r"^\s*(?:(?:\d+|[ivxlc]+)[.)]?\s*)?(?:r\s?eferences?(?:\s+and\s+notes)?|notes\s+and\s+references|bibliography|works\s+cited|literature\s+cited)\s*:?\s*$", re.I | re.M)
DOI_TOKEN = re.compile(r"10\.\d{4,9}/\S+")
LIGATURES = re.compile(r"ffi|ffl|fi|fl|ff")       # a PDF text layer often loses these glyphs: both sides drop them, so a title still matches
COMPANION = re.compile(r"\b(online\s+appendix|appendix\s+to|supplement(?:ary|al)?|e-?\s?companion|supporting\s+information|erratum|corrigendum|correction\s+to|comment\s+on|"
                       r"reply\s+to|rejoinder|retraction|retracted)\b", re.I)       # items that repeat a paper's title and DOI without being the paper
_SPECIAL = str.maketrans({"ø": "o", "đ": "d", "ł": "l", "ı": "i", "ð": "d", "þ": "th", "æ": "ae", "œ": "oe", "ß": "ss",
                          "'": "", "’": "", "`": "", "¨": "", "´": "", "ˆ": "", "˜": "", "¸": "", "ˇ": "", "˘": "", "˙": "",
                          "˚": "", "˝": "", "¯": ""})      # letters NFKD leaves alone, apostrophes, and the spacing accents old LaTeX PDFs print before a letter


# --- text helpers ---------------------------------------------------------------------------------------------------------------------------------------------------
def fold(s):
    """Lower-case, no accents, ligatures spelled out (ﬁ -> fi), soft hyphens and apostrophes removed (O'Brien -> obrien, Møller -> moller)."""
    s = unicodedata.normalize("NFKD", (s or "").lower().translate(_SPECIAL))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.replace("­", "").replace("‐", "-").replace("‑", "-")


def squash(s):
    """Letters and digits only, without the ligature letter pairs: hyphenation, line breaks, dashes, quotes, spacing and lost ligature glyphs cannot make a title fail to match."""
    return LIGATURES.sub("", re.sub(r"[^a-z0-9]", "", fold(s)))


def plain_title(title):
    """Crossref titles can carry markup and entities (<i>Bayesian</i>, <sub>2</sub>, &amp;, MathML): the PDF prints the text only."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html.unescape(title or ""))).strip()


def content_words(title):
    return [w for w in re.findall(r"[a-z]+", fold(title)) if w not in STOP and len(w) > 2]


def is_short(title):
    return len(squash(title)) < SHORT_TITLE_LETTERS or len(content_words(title)) < SHORT_TITLE_WORDS


# --- reading the PDF -------------------------------------------------------------------------------------------------------------------------------------------------
def read_pdf(data):
    """What the first pages say. Returns dict(text, meta, meta_title, headline, pages_read, scans, words); raises on a file PyMuPDF cannot open."""
    import pymupdf
    flags = pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES        # (the page pictures are not wanted: a 600 dpi scan would be copied into memory)
    with pymupdf.open(stream=data, filetype="pdf") as d:
        parts, got, scans, first_body, read = [], 0, 0, None, 0
        for i in range(min(MAX_PAGES, len(d))):
            pg = d[i]
            t = pg.get_text()
            parts.append(t)
            read += 1
            area = abs(pg.rect.width * pg.rect.height) or 1
            if max((abs(im["bbox"][2] - im["bbox"][0]) * abs(im["bbox"][3] - im["bbox"][1]) for im in pg.get_image_info()), default=0) / area >= SCAN_COVER:
                scans += 1
            if len(t.split()) >= THIN_WORDS:
                got += 1
                if first_body is None:
                    first_body = i
            if got >= BODY_PAGES:
                break
        text = "\n".join(parts)
        for m in REFHEAD.finditer(text):         # a paper that merely CITES the expected one must not match through its reference list
            if len(text[:m.start()].split()) >= REF_AFTER_WORDS:
                text = text[:m.start()]
                break
        md = d.metadata or {}
        meta = " ".join(str(v) for v in md.values() if v)
        headline = ""
        if len(d):
            spans = []
            for b in d[first_body or 0].get_text("dict", flags=flags)["blocks"]:         # the title page, not a cover sheet
                for ln in b.get("lines", []):
                    for sp in ln["spans"]:
                        if len(sp["text"].strip()) > 2:
                            spans.append((sp["size"], sp["text"].strip()))
            if spans:
                top = max(s for s, _ in spans)
                headline = " ".join(t for s, t in spans if s >= top - 0.5)[:140]
        return {"text": text, "meta": meta, "meta_title": (md.get("title") or "").strip(), "headline": headline, "pages_read": read, "scans": scans, "words": len(text.split())}


# --- the kinds of evidence ------------------------------------------------------------------------------------------------------------------------------------------
def doi_found(doi, text):
    """The expected DOI is printed as a whole DOI (not as the start of a longer one), also when a line break splits it."""
    if not doi:
        return False
    t = re.sub(r"\s+", " ", (text or "").replace("­", ""))
    t = re.sub(r"(10\.\d{4,9}/) (?=[A-Za-z0-9])", r"\1", t)           # "10.1287/\nmnsc...": broken right after the prefix
    for _ in range(3):                                               # "10.1287/mnsc.\n2024.0001": rejoin a DOI broken after . / _ -
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


def main_title(title):
    """The title without its subtitle (the part before a colon or a dash): journals often print the subtitle apart."""
    return re.split(r"\s*[:–—]\s+| - ", title or "", maxsplit=1)[0]


def main_title_found(title, text):
    main = squash(main_title(title))
    return len(main) >= MAIN_TITLE_MIN and main in squash(text)


def author_names(authors):
    """One list of acceptable family-name words per author: the last word ("Jing Tian"), the word before the comma ("Tian, Jing"), also the one before the last of three
    ("María García López"); hyphenated names either way ("Smith-Jones"). Words of fewer than 3 letters ("Li", "Wu") are not used: they would match anything."""
    if isinstance(authors, str):
        authors = [a for a in re.split(r"\s*;\s*", authors) if a.strip()]
    out = []
    for a in authors or []:
        a = (a or "").strip()
        if not a:
            continue
        if "," in a:                                                 # "Tian, Jing": the family name comes first
            fams = [a.split(",")[0]]
        else:                                                        # "Jing Tian", "Jing Tian Jr.": the last word, and for three words or more the one before it
            toks = re.sub(r"[,\s]+(jr|sr|ii|iii|iv)\.?$", "", a, flags=re.I).split()
            fams = ([toks[-1]] + ([toks[-2]] if len(toks) >= 3 else [])) if toks else []
        words = []
        for f in fams:
            words += [re.sub(r"[^a-z]", "", p) for p in re.split(r"[-\s]+", fold(f))]
        words = [w for w in words if len(w) >= 3]
        if words:
            out.append(words)
    return out


def authors_score(authors, text):
    """Share of the authors whose family name appears as a whole word; None when we know no usable author."""
    fams = author_names(authors)
    if not fams:
        return None
    words = set(re.findall(r"[a-z]+", fold(text)))
    return sum(any(p in words for p in parts) for parts in fams) / len(fams)


def headline_is_title(title, headline):
    """The page's largest text is the title (it may carry a byline in the same size)."""
    t, h = squash(title), squash(headline)
    return bool(t) and t in h and len(h) <= 2 * len(t) + 20


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
    if says and COMPANION.search(says) and not COMPANION.search(title):       # an appendix, a correction or a comment repeats the title and the DOI without being the paper
        return "mismatch", "this PDF is titled “%s”: it is an appendix, a correction or a comment, not the paper itself" % says[:90].replace("|", "/"), ev
    if ev["doi"]:
        return "match", "its DOI is printed on the first pages", ev
    if title:
        ev["title"] = round(title_score(title, r["text"]), 2)
        ev["authors"] = authors_score(authors, r["text"])
        if r["meta_title"] and title_score(title, r["meta_title"]) >= TITLE_FULL and len(squash(title)) >= HEADLINE_MIN_LETTERS:
            return "match", "the title embedded in the PDF is this title", ev
    a = ev["authors"]
    scanned = r["pages_read"] and r["scans"] * 2 >= r["pages_read"]
    if scanned and ev["title"] < TITLE_FULL:
        return "unverified", "the PDF is a scan with no text to compare", ev
    if not title:
        return "unverified", "the DOI is not printed on the first pages and there is no title to compare", ev
    if is_short(title):
        if ev["title"] >= TITLE_FULL and a is not None and a >= AUTHORS_MIN:
            return "match", "its title and authors are on the first pages", ev
        if ev["title"] >= TITLE_FULL and a is None and len(squash(title)) >= HEADLINE_MIN_LETTERS and len(content_words(title)) >= HEADLINE_MIN_WORDS \
                and headline_is_title(title, says):
            return "match", "its title is the headline of the first page", ev
        why = "the title is too short to identify it alone and neither the DOI nor the authors are on the first pages"
    elif ev["title"] >= TITLE_FULL:
        return "match", "its title is on the first pages", ev
    elif ev["title"] >= TITLE_PARTIAL and a is not None and a >= AUTHORS_MIN:
        return "match", "most of its title and its authors are on the first pages", ev
    elif main_title_found(title, r["text"]) and not is_short(main_title(title)):
        return "match", "its main title is on the first pages", ev
    elif main_title_found(title, r["text"]) and a is not None and a >= AUTHORS_MIN:
        return "match", "its main title and authors are on the first pages", ev
    else:
        why = "its title is not on the first pages (%d%% found)" % round(100 * ev["title"])
    if says:
        why = "this PDF is titled “%s”: %s" % (says[:90].replace("|", "/"), why)
    return "mismatch", why, ev


# --- the audit of a library ----------------------------------------------------------------------------------------------------------------------------------------
KEPT = os.path.join(paths.STATE, "identity_ok.json")        # {file: size}: PDFs you looked at and said are the right paper; they are not read again while the file is unchanged


def kept():
    try:
        with open(KEPT, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def keep(file):
    """Remember that you checked this PDF and it is the right paper (until the file changes)."""
    f = os.path.join(paths.ROOT, file)
    d = kept()
    d[file] = os.path.getsize(f)
    os.makedirs(os.path.dirname(KEPT), exist_ok=True)
    tmp = KEPT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, indent=1)
    os.replace(tmp, KEPT)


def audit(progress=None, use_kept=False):
    """Read-only. For every row of Papers/_index.csv with status ok and a file, what check() says. Returns a list of dicts, mismatches first, then unverified.
    use_kept: a PDF you said is the right paper (keep()) and that has not changed is not read again (verdict "kept")."""
    idx = os.path.join(paths.PAPERS, "_index.csv")
    rows = []
    if os.path.exists(idx):
        with open(idx, newline="", encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r.get("status") == "ok" and r.get("file")]
    out = []
    ok = kept() if use_kept else {}
    rows = [r for r in rows if r.get("journal") != "Books"]       # a book is not a paper with a DOI to look for on its first pages (books.py checks a book when it is added)
    for n, r in enumerate(rows, 1):
        f = os.path.join(paths.ROOT, r["file"])
        item = {"file": r["file"], "title": r.get("title", ""), "doi": r.get("doi", ""), "journal": r.get("journal", ""), "note": r.get("note", "")}
        if os.path.exists(f) and ok.get(r["file"]) == os.path.getsize(f):
            item.update(verdict="kept", reason="you checked it and said it is the right paper", pdf_says="")
        elif not os.path.exists(f):
            item.update(verdict="missing", reason="the file is not there", pdf_says="")
        else:
            with open(f, "rb") as fh:
                data = fh.read()
            v, why, ev = check({"title": r.get("title"), "doi": r.get("doi"), "authors": r.get("authors")}, data)
            item.update(verdict=v, reason=why, pdf_says=ev["pdf_says"])
        out.append(item)
        if progress:
            progress(n, len(rows))
    order = {"mismatch": 0, "unverified": 1, "missing": 2, "kept": 3, "match": 4}
    return sorted(out, key=lambda x: order.get(x["verdict"], 9))


def remove(file):
    """(One writer of Papers/_index.csv at a time: see _remove.)"""
    import download as dl
    with dl.INDEX_LOCK:
        return _remove(file)


def _remove(file):
    """Take a PDF out of the library without deleting anything: the PDF and its notes, summary, card and skim files move to Papers/_removed/<time>/ (same sub-folders),
    and its row in Papers/_index.csv becomes status "removed" (so the library no longer lists it and a download can try again). Returns the row's data for a new download
    ({title, doi, authors, journal, date}) and where the files went."""
    import download as dl                                  # (INDEX_COLS; imported here: download imports this module)
    import notes_store as ns                               # (the files that belong to a PDF)
    base, root = os.path.realpath(paths.PAPERS), os.path.realpath(paths.ROOT)       # (real paths all through: /var is /private/var on a Mac, and a Papers folder may be a link)
    pdf = os.path.realpath(os.path.join(paths.ROOT, file)) if isinstance(file, str) and "\0" not in file else ""
    if not pdf.startswith(base + os.sep) or not pdf.lower().endswith(".pdf") or not os.path.isfile(pdf) or os.sep + "_removed" + os.sep in pdf:
        raise ValueError("That file is not in your library.")
    idx = os.path.join(paths.PAPERS, "_index.csv")
    with open(idx, newline="", encoding="utf-8") as f:
        rd = csv.DictReader(f)
        rows = list(rd)
        extra = [c for c in (rd.fieldnames or []) if c and c not in dl.INDEX_COLS]       # (columns a later version may add: kept)
    mine = [r for r in rows if r.get("file") == file and r.get("status") == "ok" and os.path.realpath(os.path.join(paths.ROOT, r["file"])) == pdf]
    if not mine:
        raise ValueError("That file is not in your library index.")
    row = mine[0]
    import kinds
    side = kinds.read_sidecar(pdf, dl.index_key(row))             # (its kind goes with the new download)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest_root = os.path.join(base, "_removed", stamp)
    moves = []
    for src in [pdf] + ns.sidecars(pdf):
        rel = os.path.relpath(src, base)
        if rel.startswith(".."):                                  # never move a file to anywhere but under Papers/_removed
            raise ValueError("That file is not in your library.")
        moves.append((src, os.path.join(dest_root, rel)))
    moved = []
    for src, dst in moves:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
        moved.append(os.path.relpath(dst, root))
    row["file"], row["status"] = "", "removed"
    row["note"] = ("removed by Check my PDFs: it did not look like this paper (kept in %s)" % os.path.relpath(dest_root, root))[:300]
    tmp = idx + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, dl.INDEX_COLS + extra, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, idx)
    m = re.match(r"Papers/(\d{4}-\d{2})/", file)
    return {"title": row.get("title", ""), "doi": row.get("doi", ""), "authors": [a for a in (row.get("authors") or "").split("; ") if a], "journal": row.get("journal", ""),
            "date": m.group(1) if m else "", "moved_to": os.path.relpath(dest_root, root), "files": moved, "kind": side.get("kind") or "", "label": side.get("label") or ""}


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
