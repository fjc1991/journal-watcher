#!/usr/bin/env python3
"""Fetch new papers from journal RSS feeds + Crossref.

- Dedupes against state/seen.json
- Writes state/new_papers.json
- Usage:
    python3 fetch_new.py                 # fetch all journals
    python3 fetch_new.py --doi 10.1287/...   # fetch a specific paper's metadata
"""
import json
import os
import re
import sys
import time
import hashlib
import urllib.error
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from html import unescape
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402

BASE, ROOT = paths.BASE, paths.ROOT       # the settings folder and the data home (paths.py)
STATE_DIR = paths.STATE
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# ---------- http ----------

def _api_ua(url, h):
    """OpenAlex and Crossref (and a JW_OPENALEX_URL mirror, see _openalex) are told plainly who is asking; feeds keep UA."""
    try:
        import updater
        return updater.api_headers(url, h)
    except Exception:  # noqa: BLE001
        return h


def http_get(url, timeout=40, headers=None):
    h = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    _api_ua(url, h)
    for attempt in range(4):
        req = urllib.request.Request(url, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read(), r.geturl(), dict(r.headers)
        except urllib.error.HTTPError as e:
            # Crossref/OpenAlex throttle bursts with 429: wait and retry. A wait of more than a minute is not a burst but a budget used up for the day (OpenAlex
            # without a key answers "retry in 10200 s"): waiting 30 s three times on every call only made searches hang, so that is raised at once.
            if e.code != 429 or attempt == 3:
                raise
            wait = e.headers.get("Retry-After") if e.headers else None
            if wait and wait.isdigit() and int(wait) > 60:
                raise
            time.sleep(min(int(wait), 30) if wait and wait.isdigit() else 3 * (attempt + 1))


# ---------- text helpers ----------

def strip_html(text):
    if not text:
        return ""
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def norm_title(t):
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def doi_from_text(text):
    if not text:
        return None
    m = re.search(r"(?:^|[/=\s?])(10\.[0-9]{4,9}/[^\s\"'<>&?]+)", text)
    if m:
        return m.group(1).rstrip(".,;:")
    m2 = re.search(r"doi:((?:10\.[0-9]{4,9}/\S+))", text, re.IGNORECASE)
    if m2:
        return m2.group(1).rstrip(".,;:")
    return None


def key_for(doi, journal, title):
    if doi:
        return "doi:" + doi.lower()
    return "t:" + hashlib.sha1((journal + "|" + norm_title(title)).encode()).hexdigest()[:20]


# ---------- feed parsing ----------

def localname(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def parse_feed(xml_text):
    root = ET.fromstring(xml_text)
    items = []
    for el in root.iter():
        if localname(el.tag) not in ("item", "entry"):
            continue
        d = {}
        for child in el:
            ln = localname(child.tag)
            if ln == "link":
                href = child.get("href") or (child.text or "").strip()
                if href and not d.get("link"):
                    d["link"] = href
            elif ln in ("author", "creator"):
                # Atom <author><name>…</name></author>, RSS <dc:creator>
                name = child.findtext("{*}name") or "".join(child.itertext())
                if name.strip():
                    d.setdefault("authors", []).append(name.strip())
            elif ln in ("title", "description", "summary", "content",
                        "pubDate", "published", "updated", "identifier",
                        "dc:identifier", "dc:date", "category", "keywords"):
                if ln not in d:
                    d[ln] = (child.text or "").strip()
        if d.get("title"):
            items.append(d)
    return items


def item_to_paper(d, j):
    title = strip_html(d.get("title", ""))
    link = (d.get("link") or "").strip()
    if link and not link.startswith("http"):  # HBR's Atom feed uses site-relative links
        link = urllib.parse.urljoin(j.get("base_url") or j.get("feed") or "", link)
    desc = strip_html(d.get("description") or d.get("summary") or d.get("content") or "")
    date = d.get("published") or d.get("pubDate") or d.get("updated") or d.get("dc:date") or ""
    date_m = re.search(r"(\d{4})-(\d{2})(-(\d{2}))?", date)
    pub = ""
    if date_m:
        pub = "-".join(x for x in (date_m.group(1), date_m.group(2), date_m.group(4)) if x)
    doi = doi_from_text(link) or doi_from_text(d.get("identifier", "")) or doi_from_text(d.get("dc:identifier", "")) or doi_from_text(desc[:500])
    # abstract: strip common feed prefixes
    abstract = desc
    for prefix in ("Abstract:", "Abstract:"):
        abstract = re.sub(r"^" + re.escape(prefix) + r"\s*", "", abstract)
    if len(abstract) > 4500:
        abstract = abstract[:4500].rsplit(" ", 1)[0]
    authors = d.get("authors") or None
    if j.get("publisher") == "nber":
        # "Title -- by A, B, C"; DOI follows from the working-paper number
        if " -- by " in title:
            title, by = title.rsplit(" -- by ", 1)
            authors = [a.strip() for a in re.split(r",\s*|\s+and\s+", by) if a.strip()]
        m = re.search(r"/papers/(w\d+)", link)
        if m:
            doi = "10.3386/" + m.group(1)
            link = "https://www.nber.org/papers/" + m.group(1)
        pub = pub or datetime.now(timezone.utc).strftime("%Y-%m")
    return {
        "journal": j["name"], "abbr": j["abbr"], "publisher": j["publisher"],
        "title": title, "url": link, "doi": doi, "abstract": abstract or None,
        "keywords": None, "pub_date": pub, "source": "rss", "authors": authors,
    }


# ---------- HBR magazine archive ----------
# HBR has no DOIs and is not in Crossref. Its website lists every print issue on a contents page, hbr.org/archive-toc/BR<YY><NN>. Each article there is a
# <stream-item> tag that carries its address, title, authors, topic and one-line summary as attributes (the same article can appear twice, short and long:
# the copies are merged). Monthly up to 2016, with some months combined, so not every number is an issue. Six issues a year since 2017. A number that is
# not an issue answers with a page that has no articles. HBR's web-only articles are not in any issue: only the weekly feed has them.
HBR_TOC = "https://hbr.org/archive-toc/BR%02d%02d"
HBR_SIX_A_YEAR_FROM = 2017


def hbr_issue_codes(year):
    """The issue numbers to try for a year: 1–6 from 2017, 1–12 before (some of those are not issues)."""
    return list(range(1, 7 if year >= HBR_SIX_A_YEAR_FROM else 13))


def hbr_issue_items(html, year):
    """The articles of one issue page: [{url, title, authors, summary, topic, pub_date}], in page order, one per address. Only articles of that year count (a page
    that is not an issue lists none, and nothing from another year is taken for this one)."""
    from html.parser import HTMLParser

    class _Items(HTMLParser):
        def __init__(self):
            super().__init__()
            self.found = []

        def handle_starttag(self, tag, attrs):
            if tag == "stream-item":
                self.found.append(dict(attrs))

    p = _Items()
    p.feed(html)
    out, by_url = [], {}
    for a in p.found:
        m = re.fullmatch(r"/(\d{4})/(\d{2})/[A-Za-z0-9-]+", (a.get("data-url") or "").strip())
        if not m or int(m.group(1)) != year:
            continue
        it = {"url": "https://hbr.org" + m.group(0), "title": strip_html(a.get("data-title") or ""),
              "authors": [x.strip() for x in (a.get("data-authors") or "").split(";") if x.strip()],
              "summary": strip_html(a.get("data-summary") or ""), "topic": (a.get("data-topic") or "").strip(),
              "pub_date": "%s-%s" % (m.group(1), m.group(2))}
        old = by_url.get(it["url"])
        if old is None:
            by_url[it["url"]] = it
            out.append(it)
        else:                                       # the second copy fills what the first left empty
            for k in ("title", "authors", "summary", "topic"):
                if not old[k] and it[k]:
                    old[k] = it[k]
    return [it for it in out if it["title"]]


def hbr_archive(y0, y1, progress=None, pause=0.5, get=None, retry_wait=3):
    """Every article of HBR's print issues from y0 to y1, as archive-search items (no DOI; the one-line summary as the abstract; the topic as keywords). An
    issue page that cannot be read stops the search with an error naming it, so that a year is never saved with an issue silently missing."""
    get = get or http_get
    codes = [(y, n) for y in range(int(y0), int(y1) + 1) for n in hbr_issue_codes(y)]
    out, seen = [], set()
    for k, (y, n) in enumerate(codes, 1):
        url = HBR_TOC % (y % 100, n)
        for attempt in range(2):
            try:
                raw, _, _ = get(url, timeout=40)
                break
            except Exception as e:  # noqa: BLE001
                if attempt:
                    raise RuntimeError("HBR issue BR%02d%02d could not be read (%s)" % (y % 100, n, str(e)[:80]))
                time.sleep(retry_wait)
        for it in hbr_issue_items(raw.decode("utf-8", "replace"), y):
            if it["url"] in seen:
                continue
            seen.add(it["url"])
            out.append({"doi": None, "title": it["title"], "url": it["url"], "abstract": it["summary"] or None, "authors": it["authors"],
                        "keywords": [it["topic"]] if it["topic"] else None, "pub_date": it["pub_date"]})
        if progress:
            progress(k, len(codes))
        if pause and k < len(codes):
            time.sleep(pause)                      # a light pace: these are HBR's own web pages
    return out


# ---------- crossref ----------

def cr_strip_jats(x):
    return strip_html(x) if x else None


def fmt_date_parts(dp):
    try:
        d = (dp or [[None]])[0]
        if d and d[0] and len(d) > 1 and d[1]:
            return "%04d-%02d" % (d[0], d[1])
        if d and d[0]:
            return "%04d" % d[0]
    except Exception:
        pass
    return ""


def crossref_journal(issn, rows=25):
    url = ("https://api.crossref.org/journals/%s/works?order=desc&sort=published"
           "&rows=%d&select=DOI,title,author,abstract,published,subject,container-title"
           % (issn, rows))
    raw, _, _ = http_get(url, headers={"Accept": "application/json"})
    data = json.loads(raw.decode("utf-8", "replace"))
    out = []
    for it in data.get("message", {}).get("items", []):
        title = (it.get("title") or [""])[0]
        authors = []
        for a in it.get("author", []):
            nm = " ".join(x for x in [a.get("given"), a.get("family")] if x)
            if nm:
                authors.append(nm)
        pub = fmt_date_parts((it.get("published") or {}).get("date-parts"))
        out.append({
            "doi": it.get("DOI"), "title": title,
            "url": "https://doi.org/" + it.get("DOI", ""),
            "abstract": cr_strip_jats(it.get("abstract")),
            "authors": authors,
            "keywords": it.get("subject") or None,
            "pub_date": pub,
        })
    return out


def crossref_doi(doi):
    # NOTE: no 'select' param here — it makes /works/{doi} fail validation.
    url = "https://api.crossref.org/works/%s" % urllib.parse.quote(doi, safe="/")
    try:
        raw, _, _ = http_get(url, timeout=30, headers={"Accept": "application/json"})
        msg = json.loads(raw.decode("utf-8", "replace")).get("message")
        if not isinstance(msg, dict):
            return None
        it = msg
    except Exception:
        return None
    authors = []
    for a in it.get("author", []):
        nm = " ".join(x for x in [a.get("given"), a.get("family")] if x)
        if nm:
            authors.append(nm)
    return {
        "abstract": cr_strip_jats(it.get("abstract")),
        "authors": authors,
        "keywords": it.get("subject") or None,
        "pub_date": fmt_date_parts((it.get("published") or {}).get("date-parts")),
    }


def crossref_title_search(title, issn=None, doi_prefix=None):
    """Best Crossref match for a title. With `issn` / `doi_prefix`, only that
    journal / publisher is searched (ISSN first; very fresh records sometimes
    lack it) — without them, preprints (SSRN etc.) with the same title can win."""
    if issn or doi_prefix:
        for flt in (["issn:" + issn] if issn else []) + (["prefix:" + doi_prefix] if doi_prefix else []):
            r = _crossref_title_query(title, flt)
            if r:
                return r
        return None
    return _crossref_title_query(title, None)


def _crossref_title_query(title, flt):
    import difflib
    q = urllib.parse.quote(title[:200])
    url = ("https://api.crossref.org/works?query.bibliographic=%s&rows=2"
           "&select=DOI,title,abstract,subject,published" % q)
    if flt:
        url += "&filter=" + flt
    raw = None
    for attempt in range(3):  # Crossref throttles bursts; retry before giving up
        try:
            raw, _, _ = http_get(url, timeout=30, headers={"Accept": "application/json"})
            break
        except Exception:
            time.sleep(2 * (attempt + 1))
    if raw is None:
        return None
    data = json.loads(raw.decode("utf-8", "replace"))
    target = norm_title(title)
    for it in data.get("message", {}).get("items", []):
        t = (it.get("title") or [""])[0]
        nt = norm_title(t)
        if not (target and (nt == target or
                            difflib.SequenceMatcher(None, target, nt).ratio() >= 0.9)):
            continue
        return {
                "doi": it.get("DOI"), "abstract": cr_strip_jats(it.get("abstract")),
                "keywords": it.get("subject") or None,
                "pub_date": fmt_date_parts((it.get("published") or {}).get("date-parts")),
            }
    return None


# ---------- which download route a paper from any journal gets (related papers, author search)

PUBLISHER_BY_PREFIX = {"10.1287": "informs", "10.1111": "wiley", "10.1002": "wiley", "10.1016": "elsevier", "10.1080": "tandfonline", "10.1145": "acm", "10.1126": "aaas",
                       "10.1073": "pnas", "10.25300": "aisel", "10.17705": "aisel", "10.3386": "nber", "10.2139": "ssrn"}      # which download route a DOI's publisher needs (download.py)
SMALL = {"of", "the", "and", "in", "for", "on", "to", "a", "an", "at", "by", "&"}


def venue_abbr(name):
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z]+", name) if w.lower() not in SMALL]
    return ("".join(w[0].upper() for w in words)[:6] if len(words) >= 2 else (words[0][:5].upper() if words else "OTH")) or "OTH"


def venue_route(meta, venue, doi, journals):
    """(name, abbr, publisher) for a candidate. A watched journal is recognised by its ISSN only (a DOI prefix such as Wiley's 10.1111 covers hundreds of journals);
    any other journal keeps its own name and gets the download route of its DOI prefix's publisher."""
    issn = set((meta or {}).get("issn") or [])
    j = next((x for x in journals if x.get("issn") and x["issn"] in issn), None)
    if j:
        return j["name"], j["abbr"], j["publisher"]
    name = re.sub(r"[\\/:*?\"<>|]+", " ", ((meta or {}).get("container") or venue or "")).strip()[:80] or "Other"
    return name, venue_abbr(name) if name != "Other" else "OTH", PUBLISHER_BY_PREFIX.get(doi.split("/")[0] if doi else "", "")


def openalex_key():
    key = os.environ.get("OPENALEX_API_KEY")
    if key:
        return key
    try:
        import keychain                    # (this Mac's Keychain: a key saved or moved there; cached for a minute, nothing under JW_NO_KEYCHAIN)
        key = keychain.values(["OPENALEX_API_KEY"]).get("OPENALEX_API_KEY")
        if key:
            return key
    except Exception:  # noqa: BLE001      (no Keychain: the file, as before)
        pass
    p = os.path.join(ROOT, ".env")
    if os.path.exists(p):
        for line in open(p):
            if line.startswith("OPENALEX_API_KEY="):
                return line.split("=", 1)[1].strip() or None
    return None


OPENALEX_SPENT = {"until": 0.0, "keyed": None}     # OpenAlex said the day's budget is used up: when it resets, and whether a key was used


def openalex_spent():
    """True while OpenAlex's daily budget is used up (for the key in use, or for keyless calls from this network). Lookups of one work by its DOI or ID are free and
    still work; lists, filters and searches are not asked until the budget resets."""
    return time.time() < OPENALEX_SPENT["until"] and OPENALEX_SPENT["keyed"] == bool(openalex_key())


def _openalex(path, missing=None):
    """GET an OpenAlex path. Without a key OpenAlex gives everyone on your network a small shared daily budget ($0.10: about 1,000 list calls or 100 searches); with a
    free key (Settings → Paper sources, or OPENALEX_API_KEY in .env) it is $1 a day of your own. Lookups of one work by DOI or ID are free either way. The key is sent
    in the Authorization header (not in the address, which ends up in logs). When the budget is used up the call returns None at once instead of waiting.
    missing: what to return when OpenAlex answers 404 (it does not know the thing asked for), instead of None (no answer)."""
    key = openalex_key()
    url = (os.environ.get("JW_OPENALEX_URL") or "https://api.openalex.org/") + path     # (the override is for tests and mirrors)
    single = bool(re.match(r"works/(doi:|W\d)", path)) and "?" not in path
    if not single and openalex_spent():
        return None
    h = {"Accept": "application/json"}
    _api_ua("https://api.openalex.org/", h)        # (also when the address is a mirror or a test server)
    if key:
        h["Authorization"] = "Bearer " + key
    try:
        raw, _, _ = http_get(url, timeout=30, headers=h)
        return json.loads(raw.decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        wait = (e.headers.get("Retry-After") if e.headers else None) or ""
        if e.code == 429 and wait.isdigit() and int(wait) > 60:
            OPENALEX_SPENT.update(until=time.time() + int(wait), keyed=bool(key))
        if e.code == 404 and missing is not None:
            return missing
        return None
    except Exception:
        return None


def _inverted_to_text(inv):
    if not inv:
        return None
    pos = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[i] for i in sorted(pos))


def openalex_abstract(title):
    data = _openalex("works?search=%s&per-page=1" % urllib.parse.quote(title[:200]))
    w = ((data or {}).get("results") or [None])[0]
    return _inverted_to_text(w.get("abstract_inverted_index")) if w else None


def openalex_abstracts_dois(dois):
    """{doi (lower case): abstract} for up to 50 DOIs in one OpenAlex call (a filter on doi, "|" between them). DOIs OpenAlex does not have, or has without an abstract,
    are left out; {} when the call fails (the caller then asks one by one)."""
    dois = [d for d in dois if d][:50]
    if not dois:
        return {}
    data = _openalex("works?filter=doi:%s&select=doi,abstract_inverted_index&per-page=50" % "|".join(urllib.parse.quote(d, safe="/") for d in dois))
    out = {}
    for w in (data or {}).get("results") or []:
        d = re.sub(r"^https?://(dx\.)?doi\.org/", "", (w.get("doi") or ""), flags=re.I).lower()
        ab = _inverted_to_text(w.get("abstract_inverted_index"))
        if d and ab:
            out[d] = ab
    return out


def openalex_abstract_doi(doi):
    w = _openalex("works/doi:%s" % urllib.parse.quote(doi, safe="/"))
    return _inverted_to_text((w or {}).get("abstract_inverted_index"))


def crossref_journal_range(issn, year_from, year_to, progress=None, doi_prefix=None,
                           work_type="journal-article"):
    """All articles of one journal published in [year_from, year_to]. Series
    without an ISSN (NBER working papers) are selected by DOI prefix."""
    out, cursor = [], "*"
    dates = "from-pub-date:%d-01-01,until-pub-date:%d-12-31,type:%s" % (year_from, year_to, work_type)
    while cursor:
        if issn:
            base = "https://api.crossref.org/journals/%s/works?filter=%s" % (issn, dates)
        else:
            base = "https://api.crossref.org/works?filter=prefix:%s,%s" % (doi_prefix, dates)
        url = ("%s&rows=500&cursor=%s&select=DOI,title,author,abstract,published,subject"
               % (base, urllib.parse.quote(cursor)))
        raw, _, _ = http_get(url, timeout=60, headers={"Accept": "application/json"})
        msg = json.loads(raw.decode("utf-8", "replace")).get("message", {})
        items = msg.get("items", [])
        for it in items:
            title = strip_html((it.get("title") or [""])[0])
            if not title:
                continue
            out.append({
                "doi": it.get("DOI"), "title": title,
                "url": "https://doi.org/" + it.get("DOI", ""),
                "abstract": cr_strip_jats(it.get("abstract")),
                "authors": [" ".join(x for x in [a.get("given"), a.get("family")] if x)
                            for a in it.get("author", [])],
                "keywords": it.get("subject") or None,
                "pub_date": fmt_date_parts((it.get("published") or {}).get("date-parts")),
            })
        if progress:
            progress(len(out), msg.get("total-results", len(out)))
        cursor = msg.get("next-cursor") if items else None
    return out


SSRN_PREFIX = "10.2139"
SSRN_LIMIT = 200


def crossref_ssrn(query, year_from, year_to, limit=SSRN_LIMIT, progress=None):
    """The papers on SSRN (DOI prefix 10.2139, registered with Crossref as posted content) that best match `query`, posted in [year_from, year_to], in Crossref's order of
    relevance, at most `limit`. SSRN holds hundreds of thousands of papers a year in every field, so it can only be searched by words, and only the best matches are taken."""
    url = ("https://api.crossref.org/works?query.bibliographic=%s&filter=prefix:%s,type:posted-content,from-pub-date:%d-01-01,until-pub-date:%d-12-31"
           "&rows=%d&select=DOI,title,author,abstract,published,posted,issued,subject"
           % (urllib.parse.quote(str(query)[:200]), SSRN_PREFIX, year_from, year_to, max(1, min(int(limit), 1000))))
    raw, _, _ = http_get(url, timeout=60, headers={"Accept": "application/json"})
    msg = json.loads(raw.decode("utf-8", "replace")).get("message", {})
    out = []
    for it in msg.get("items", []):
        title = strip_html((it.get("title") or [""])[0])
        if not title or not (it.get("DOI") or "").lower().startswith(SSRN_PREFIX + "/"):
            continue
        dp = next((it[k]["date-parts"] for k in ("published", "posted", "issued") if (it.get(k) or {}).get("date-parts")), None)
        date = fmt_date_parts(dp)
        out.append({
            "doi": it["DOI"], "title": title, "url": "https://doi.org/" + it["DOI"], "abstract": cr_strip_jats(it.get("abstract")),
            "authors": [" ".join(x for x in [a.get("given"), a.get("family")] if x) for a in it.get("author", [])],
            "keywords": it.get("subject") or None, "pub_date": date + "-01" if re.fullmatch(r"\d{4}", date or "") else date,
        })
    if progress:
        progress(len(out), len(out))
    return out


# ---------- main ----------

def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def fetch_journal(j):
    """Latest items for one journals.json entry (RSS or Crossref)."""
    out = []
    if j["source"] == "rss":
        raw, final_url, _ = http_get(j["feed"])
        for d in parse_feed(raw.decode("utf-8", "replace")):
            p = item_to_paper(d, j)
            p["url"] = p["url"] or final_url
            out.append(p)
    else:
        for it in crossref_journal(j["issn"]):
            it.update({"journal": j["name"], "abbr": j["abbr"],
                       "publisher": j["publisher"], "source": "crossref"})
            out.append(it)
    for p in out:
        p["issn"], p["doi_prefix"] = j.get("issn"), j.get("doi_prefix")
        if j.get("lookup") is False:
            p["no_lookup"] = True
        p["id"] = key_for(p.get("doi"), p["journal"], p.get("title", ""))
    return out


def enrich(p):
    """Resolve a missing DOI / abstract in place (Crossref, then OpenAlex)."""
    if p.get("no_lookup"):  # e.g. HBR: not in Crossref, feed summary is all there is
        p["id"] = key_for(p.get("doi"), p["journal"], p.get("title", ""))
        return p
    ab_short = not p.get("abstract") or len(p.get("abstract")) < 200
    if not p.get("doi") and p.get("title"):
        time.sleep(0.4)
        # DOI and date only from the paper's own journal; elsewhere (e.g. the SSRN
        # preprint) at most the abstract is borrowed, below
        r = crossref_title_search(p["title"], p.get("issn"), p.get("doi_prefix"))
        if r:
            p["doi"] = r.get("doi")
            p["pub_date"] = p.get("pub_date") or r.get("pub_date", "")
            if ab_short:
                p["abstract"] = r.get("abstract")
            if not p.get("keywords"):
                p["keywords"] = r.get("keywords")
    elif p.get("doi") and (ab_short or not p.get("pub_date") or not p.get("authors")):
        # DOI in hand but no real abstract (e.g. INFORMS 'Ahead of Print' feeds)
        time.sleep(0.3)
        r = crossref_doi(p["doi"])
        if r:
            if r.get("abstract") and len(r["abstract"]) > 200:
                p["abstract"] = r["abstract"]
            if not p.get("authors"):
                p["authors"] = r.get("authors")
            if not p.get("keywords"):
                p["keywords"] = r.get("keywords")
            if not p.get("pub_date"):
                p["pub_date"] = r.get("pub_date") or ""
        if ab_short and (not p.get("abstract") or len(p.get("abstract") or "") < 200):
            # publisher DOI lacks an abstract -> try the title (finds SSRN etc.)
            time.sleep(0.4)
            r2 = crossref_title_search(p.get("title", ""))
            if r2 and r2.get("abstract") and len(r2["abstract"]) > 200:
                p["abstract"] = r2["abstract"]
                if not p.get("keywords"):
                    p["keywords"] = r2.get("keywords")
    if not p.get("abstract") and p.get("title"):
        time.sleep(0.3)
        p["abstract"] = (openalex_abstract_doi(p["doi"]) if p.get("doi") else None) \
            or openalex_abstract(p["title"])
    p["id"] = key_for(p.get("doi"), p["journal"], p.get("title", ""))
    return p


def dedupe(papers, seen):
    """Papers not in `seen`; marks them seen (caller saves seen.json)."""
    fresh = []
    for p in papers:
        k = key_for(p.get("doi"), p["journal"], p.get("title", ""))
        p["id"] = k
        if k in seen:
            continue
        seen[k] = {"journal": p["journal"], "title": p.get("title", "")[:120],
                   "added": datetime.now(timezone.utc).strftime("%Y-%m-%d")}
        fresh.append(p)
    return fresh


def main():
    os.makedirs(STATE_DIR, exist_ok=True)
    journals = load_json(os.path.join(BASE, "journals.json"), {"journals": []})["journals"]
    seen_path = os.path.join(STATE_DIR, "seen.json")
    seen = load_json(seen_path, {})

    # --doi mode: fetch one paper's metadata
    if "--doi" in sys.argv:
        doi = sys.argv[sys.argv.index("--doi") + 1]
        # no `select` here: /works/{doi} rejects it
        url = "https://api.crossref.org/works/%s" % urllib.parse.quote(doi, safe="/")
        raw, _, _ = http_get(url)
        it = json.loads(raw.decode("utf-8", "replace"))["message"]
        paper = {
            "journal": it.get("container-title", ["?"])[0] if it.get("container-title") else "?",
            "abbr": "?", "publisher": "unknown",
            "title": (it.get("title") or [""])[0], "url": "https://doi.org/" + doi,
            "doi": doi, "abstract": cr_strip_jats(it.get("abstract")),
            "keywords": it.get("subject") or None,
            "authors": [" ".join(x for x in [a.get("given"), a.get("family")] if x)
                        for a in it.get("author", [])],
            "pub_date": "", "source": "crossref-doi",
        }
        paper["pub_date"] = fmt_date_parts((it.get("published") or {}).get("date-parts"))
        out = os.path.join(STATE_DIR, "new_papers.json")
        with open(out, "w") as f:
            json.dump([paper], f, indent=1)
        print("Fetched %s -> %s" % (paper["title"], out))
        return

    new_papers = []
    for j in journals:
        try:
            items = fetch_journal(j)
            new_papers.extend(items)
            print("  [%s] %d items from %s" % (j["abbr"], len(items), j["source"]))
        except Exception as e:
            print("  [%s] FAILED: %s" % (j["abbr"], e))
            time.sleep(1)

    for p in new_papers:
        enrich(p)

    fresh = dedupe(new_papers, seen)

    with open(seen_path, "w") as f:
        json.dump(seen, f, indent=1)
    with open(os.path.join(STATE_DIR, "new_papers.json"), "w") as f:
        json.dump(fresh, f, indent=1)
    with open(os.path.join(STATE_DIR, "last_run.txt"), "w") as f:
        f.write(datetime.now(timezone.utc).isoformat())

    print("\nNEW: %d papers (of %d seen this run)" % (len(fresh), len(new_papers)))
    for p in sorted(fresh, key=lambda x: x.get("pub_date", ""), reverse=True):
        ab = "Y" if p.get("abstract") else "-"
        print("  %s | %s | abs:%s | %s" % (
            p.get("pub_date", "?")[:7], p["abbr"], ab,
            (p.get("title") or "")[:70]))


if __name__ == "__main__":
    main()
