"""Find a legal free copy of a paper when the publisher and the library can't
supply it: arXiv, Semantic Scholar's open-access link, Europe PMC, and preprint
servers registered in Crossref (bioRxiv, medRxiv, SSRN, Research Square, …).

Each candidate is checked: a complete PDF that identity.py recognises as this paper (its DOI, or its title and authors, are on the first pages).
A candidate that is a different work is skipped and the next one tried.
Returns (pdf_bytes | None, note) — the note says which version it is.
"""
import difflib
import json
import os
import re
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import identity  # noqa: E402

UA = {"User-Agent": "JournalWatcher/1.0 (academic literature tool)"}


def _norm(t):
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def _similar(a, b, cut=0.9):
    a, b = _norm(a), _norm(b)
    return bool(a and b) and difflib.SequenceMatcher(None, a, b).ratio() >= cut


def candidates(doi, title, http_get):
    """Yield (url, label) for possible open copies, best first."""
    def get_json(url):
        try:
            raw, _ = http_get(url, headers=dict(UA, Accept="application/json"))
            return json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            return None

    # 1) Semantic Scholar: open-access PDF link + arXiv id
    if doi:
        d = get_json("https://api.semanticscholar.org/graph/v1/paper/DOI:%s?fields=title,openAccessPdf,externalIds"
                     % urllib.parse.quote(doi, safe="/"))
        if d:
            u = (d.get("openAccessPdf") or {}).get("url")
            if u and "doi.org/" + doi.lower() not in u.lower():
                yield u, "open copy (Semantic Scholar)"
            ax = (d.get("externalIds") or {}).get("ArXiv")
            if ax:
                yield "https://arxiv.org/pdf/%s" % ax, "preprint (arXiv)"
    # 2) arXiv title search
    if title:
        q = urllib.parse.quote('ti:"%s"' % re.sub(r'["\\:]', " ", title)[:200])
        try:
            raw, _ = http_get("http://export.arxiv.org/api/query?search_query=%s&max_results=3" % q, headers=UA)
            x = raw.decode("utf-8", "replace")
            for t, i in zip(re.findall(r"<entry>.*?<title>(.*?)</title>", x, re.S),
                            re.findall(r"<entry>.*?<id>(.*?)</id>", x, re.S)):
                if _similar(re.sub(r"\s+", " ", t), title):
                    yield i.replace("/abs/", "/pdf/"), "preprint (arXiv)"
        except Exception:
            pass
    # 3) Europe PMC (PubMed Central copies, e.g. PNAS after deposit)
    if doi:
        d = get_json("https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=DOI:%s&format=json&resultType=lite"
                     % urllib.parse.quote(doi, safe="/"))
        for r in ((d or {}).get("resultList") or {}).get("result", []):
            if r.get("pmcid") and r.get("isOpenAccess") == "Y":
                yield "https://europepmc.org/articles/%s?pdf=render" % r["pmcid"], "open copy (PubMed Central)"
    # 4) preprint servers registered in Crossref (bioRxiv, medRxiv, SSRN, …)
    if title:
        d = get_json("https://api.crossref.org/works?query.bibliographic=%s&filter=type:posted-content&rows=3"
                     "&select=DOI,title,URL,publisher" % urllib.parse.quote(title[:200]))
        for it in ((d or {}).get("message") or {}).get("items", []):
            if _similar((it.get("title") or [""])[0], title):
                url = it.get("URL") or "https://doi.org/" + it["DOI"]
                if "biorxiv" in it["DOI"] or "medrxiv" in it["DOI"] or it["DOI"].startswith("10.1101/"):
                    yield "https://www.biorxiv.org/content/%s.full.pdf" % it["DOI"], "preprint (bioRxiv)"
                yield url, "preprint (%s)" % (it.get("publisher") or "preprint server")


def find_open_copy(doi, title, http_get, browser=None, authors=None):
    entry = {"doi": doi, "title": title, "authors": authors or []}
    tried, refused = set(), None
    for url, label in candidates(doi, title, http_get):
        if url in tried:
            continue
        tried.add(url)
        data = None
        try:
            data, _ = http_get(url, headers=UA)
        except Exception:
            data = None
        if not _complete(data) and browser is not None:
            try:
                data, _ = browser.fetch_pdf(url)
            except Exception:
                data = None
        if _complete(data):
            verdict, why, _ = identity.check(entry, data)
            if verdict != "mismatch":
                return data, label
            refused = refused or "wrong paper via %s: %s" % (label, why)
        time.sleep(0.5)
    return None, refused or "no free copy found"


def _complete(data):
    return bool(data) and len(data) > 40_000 and data[:1024].lstrip().startswith(b"%PDF") \
        and b"%%EOF" in data[-2048:]
