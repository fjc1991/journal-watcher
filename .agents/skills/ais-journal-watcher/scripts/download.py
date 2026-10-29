#!/usr/bin/env python3
"""Download PDFs for shortlisted papers via a cascade of avenues:

  1. Unpaywall open-access PDF (scripted)
  2. Elsevier Article Retrieval API (if ELSEVIER_API_KEY in .env)
  3. Best-effort direct fetch (aisel content link / publisher PDF over library
     IP — works only when the publisher isn't bot-blocking this client)
  4. Real browser (browser.py, Playwright persistent profile): passes the
     Cloudflare challenge and reuses the sign-ins made once via
     `browser.py login` (INFORMS member account / your university library).
  5. Manual fallback: user saves the PDF into Papers/inbox/; the agent files it.

Saves to  <root>/Papers/YYYY-MM/<Journal Name>/<YYYY-MM>_<ABBR>_<doi>_<slug>.pdf
and appends to <root>/Papers/_index.csv.

Usage:
    python3 download.py                    # everything in state/shortlist.json
    python3 download.py --pick 10.1287/...,10.1111/...
    python3 download.py --limit 3 --dry-run
    python3 download.py --no-browser       # skip the real-browser step
"""
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import urllib.parse
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402
import identity  # noqa: E402

BASE, ROOT, STATE, PAPERS = paths.BASE, paths.ROOT, paths.STATE, paths.PAPERS       # see paths.py: the project folder, or the app's data home
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def load_env():
    """Project-root .env, then the skill-folder .env (older layout)."""
    env = {}
    for p in (os.path.join(BASE, ".env"), os.path.join(ROOT, ".env")):
        if os.path.exists(p):
            for line in open(p):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def http_get(url, timeout=25, headers=None):
    h = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), dict(r.headers)


def is_pdf(data):
    """A complete PDF: header at the start, %%EOF near the end (streamed/partial
    responses from in-browser readers fail the second test)."""
    return bool(data) and len(data) > 40_000 and \
        data[:1024].lstrip(b"\xef\xbb\xbf \r\n").startswith(b"%PDF") and b"%%EOF" in data[-2048:]


def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


INDEX_COLS = ["date_checked", "journal", "abbr", "title", "authors",
              "doi", "url", "matched_topics", "score", "file", "status", "note"]


def index_key(row):
    return (row.get("doi") or row.get("title") or "").lower()


def save_index(p, file, status, note=""):
    """Upsert one row per paper (keyed by DOI, else title)."""
    idx = os.path.join(PAPERS, "_index.csv")
    rows = []
    if os.path.exists(idx):
        with open(idx, newline="") as f:
            rows = list(csv.DictReader(f))
    topics = p.get("topics") or list((p.get("topics_hit") or {}).keys())
    row = dict(zip(INDEX_COLS, [
        datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        p.get("journal", ""), p.get("abbr", ""), p.get("title", ""),
        "; ".join(p.get("authors") or []), p.get("doi") or "",
        p.get("url") or "", "; ".join(topics),
        p.get("score", ""), file, status, note,
    ]))
    rows = [r for r in rows if index_key(r) != index_key(row)] + [row]
    with open(idx, "w", newline="") as f:
        w = csv.DictWriter(f, INDEX_COLS)
        w.writeheader()
        w.writerows(rows)


def out_path(p):
    ym = (p.get("pub_date") or datetime.now().strftime("%Y-%m"))[:7]
    if not re.match(r"^\d{4}-\d{2}$", ym):
        ym = datetime.now().strftime("%Y-%m")
    doi = (p.get("doi") or "nodei").replace("/", "-")
    slug = re.sub(r"[^a-z0-9]+", "-", (p.get("title") or "").lower()).strip("-")
    slug = "-".join(slug.split("-")[:6])
    d = os.path.join(PAPERS, ym, p.get("journal", "Unknown"))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "%s_%s_%s_%s.pdf" % (ym, p.get("abbr", "xx"), doi, slug))


MEMBER_FOR = {"informs": "informs", "aisel": "ais"}  # publisher -> member sign-in (browser.MEMBER_SITES)
READER_HOSTS = {"pnas": "www.pnas.org", "aaas": "www.science.org", "informs": "pubsonline.informs.org",
                "wiley": "onlinelibrary.wiley.com", "tandfonline": "www.tandfonline.com"}


def browser_pdf_url(p):
    """PDF URL for the real-browser route (browser.py)."""
    pub, doi = p.get("publisher"), p.get("doi")
    if pub == "informs" and doi:
        return "https://pubsonline.informs.org/doi/pdf/%s" % doi
    if pub == "wiley" and doi:
        return "https://onlinelibrary.wiley.com/doi/pdfdirect/%s?download=true" % doi
    if pub == "tandfonline" and doi:
        return "https://www.tandfonline.com/doi/pdf/%s" % doi
    if pub == "acm" and doi:
        return "https://dl.acm.org/doi/pdf/%s" % doi
    if pub == "aaas" and doi:
        return "https://www.science.org/doi/pdf/%s?download=true" % doi
    if pub == "pnas" and doi:
        return "https://www.pnas.org/doi/pdf/%s?download=true" % doi
    if pub == "elsevier":
        # straight to ScienceDirect's PDF endpoint (the article page costs ~minute)
        m = re.search(r"/pii/([A-Z0-9]+)", p.get("url") or "")
        if m:
            return "https://www.sciencedirect.com/science/article/pii/%s/pdfft" % m.group(1)
    if pub == "nber" and doi and "/w" in doi:
        w = doi.split("/", 1)[1]
        return "https://www.nber.org/system/files/working_papers/%s/%s.pdf" % (w, w)
    return p.get("url") or ("https://doi.org/" + doi if doi else None)


def try_unpaywall(doi, email):
    if not doi or not email:
        return None
    url = "https://api.unpaywall.org/v2/%s?email=%s" % (
        urllib.parse.quote(doi, safe=""), urllib.parse.quote(email))
    try:
        raw, _ = http_get(url, headers={"Accept": "application/json", "User-Agent": UA})
        d = json.loads(raw.decode())
        loc = d.get("best_oa_location") or {}
        return loc.get("url_for_pdf") or loc.get("url")
    except Exception:
        return None


def try_aisel(p):
    """AIS eLibrary (MISQ, JAIS): fetch landing page, find the content link."""
    url = (p.get("url") or "").strip()
    if not url:
        return None
    if not url.endswith("/"):
        url = url + "/"
    try:
        raw, _ = http_get(url)
        html = raw.decode("utf-8", "replace")
        m = (re.search(r'href="([^"]*viewcontent\.cgi[^"]*)"', html, re.I)
             or re.search(r'href="([^"]*\.pdf(?:\?[^"]*)?)"', html, re.I))
        if m:
            cand = m.group(1).replace("&amp;", "&")
            if cand.startswith("/"):
                host = urllib.parse.urlsplit(url).netloc
                cand = "https://" + host + cand
            return cand
    except Exception:
        pass
    return None


def try_elsevier_api(doi, key, insttoken=None):
    """Elsevier Article Retrieval API (official, no captcha). Full text needs an
    entitled request: from your university's network/VPN, or with an institutional token."""
    if not doi or not key:
        return None
    url = "https://api.elsevier.com/content/article/doi/%s" % urllib.parse.quote(doi, safe="/")
    h = {"X-ELS-APIKey": key, "Accept": "application/pdf"}
    if insttoken:
        h["X-ELS-Insttoken"] = insttoken
    try:
        raw, _ = http_get(url, headers=h)
        return raw
    except urllib.error.HTTPError as e:
        return "ERR:elsevier-api HTTP %d%s" % (e.code, " (not entitled: use your university network/VPN or an "
                                               "institutional token)" if e.code in (401, 403) else "")
    except Exception as e:
        return "ERR:elsevier-api " + str(e)[:80]


def download_paper(p, env, dry=False, browser=None):
    """Returns (ok, file, note). `browser` is an open browser.Browser, or None
    to skip the real-browser step."""
    doi = p.get("doi")
    path = out_path(p)
    attempts = []

    refused = []                # what each route gave that turned out not to be this paper

    def vet(data, via):
        """(data, via) when `data` is this paper (via then says "identity not checked" if it could not be told), (None, "") when it is another work: the refusal is recorded and
        the route goes on to its next try. A PDF is only a paper when it IS the paper (identity.py): before 1.2 any complete PDF was accepted, and a link on a landing page
        once led to a different work's PDF filed under this title."""
        if not data:
            return None, via
        verdict, why, _ = identity.check(p, data)
        if verdict == "mismatch":
            refused.append("wrong paper via %s: %s" % (via, why))
            attempts.append(refused[-1])
            return None, ""
        if verdict == "unverified":                              # nothing to compare (a scan, no title on record): kept, and said so
            via = "%s; identity not checked (%s)" % (via, why)
        return data, via

    def write(data, via):
        if not dry:
            with open(path, "wb") as f:
                f.write(data)
            save_index(p, os.path.relpath(path, ROOT), "ok", via)
        return True, path, via

    def store(data, via):
        data, via = vet(data, via)
        return write(data, via) if data else None

    # 1) Unpaywall OA
    oa = try_unpaywall(doi, env.get("UNPAYWALL_EMAIL", ""))
    if oa:
        try:
            data, _ = http_get(oa)
            if is_pdf(data):
                got = store(data, "OA (Unpaywall)")
                if got:
                    return got
            else:
                attempts.append("unpaywall: not a PDF response")
        except Exception as e:
            attempts.append("unpaywall: %s" % str(e)[:60])

    # 2) Elsevier API (DSS)
    if p.get("publisher") == "elsevier":
        r = try_elsevier_api(doi, env.get("ELSEVIER_API_KEY", ""), env.get("ELSEVIER_INSTTOKEN"))
        if isinstance(r, bytes):
            if is_pdf(r):
                got = store(r, "elsevier-api")
                if got:
                    return got
            else:
                attempts.append("elsevier-api: not a PDF")
        elif isinstance(r, str):
            attempts.append(r)

    # 3) best-effort direct (aisel content link / publisher over library IP)
    cands = []
    if p.get("publisher") == "aisel":
        a = try_aisel(p)
        if a:
            cands.append(("aisel-content", a))
    if p.get("publisher") == "nber" and doi:
        cands.append(("direct", browser_pdf_url(p)))
    if p.get("publisher") in ("informs", "wiley", "tandfonline") and doi and browser is None:
        # Cloudflare answers plain HTTP with 403 (or leaves it hanging), so only
        # worth trying when there is no real browser to fall back on
        cands.append(("direct-ip", browser_pdf_url(p)))
    for name, u in cands:
        try:
            data, hdrs = http_get(u)
            if is_pdf(data):
                got = store(data, name)
                if got:
                    return got
            else:
                attempts.append("%s: blocked/non-PDF" % name)
        except Exception as e:
            attempts.append("%s: %s" % (name, str(e)[:40]))

    # 4) real browser with the user's saved sign-ins (HBR: article page saved as PDF). Every attempt is vetted on its own (a refused PDF does not end the route:
    #    the member renewal, the publisher's reader and the full-text page still get their turn)
    if browser is not None and not dry:
        u = browser_pdf_url(p)
        if u:
            try:
                if p.get("publisher") == "hbr":
                    data, note = vet(*browser.page_to_pdf(u))
                else:
                    data, note = vet(*browser.fetch_pdf(u, doi=doi))
                    member = MEMBER_FOR.get(p.get("publisher"))
                    if not data and "needs_login" in note and member and browser.renew_member(member):
                        data, note = vet(*browser.fetch_pdf(u, doi=doi))  # member session renewed silently: retry
                    reader_host = READER_HOSTS.get(p.get("publisher"))
                    if not data and reader_host and doi:
                        # the publisher's online reader often works where the PDF link doesn't (PNAS)
                        d2, n2 = vet(*browser.fetch_from_reader("https://%s/doi/epdf/%s" % (reader_host, doi)))
                        if d2:
                            data, note = d2, n2
                    if not data and p.get("publisher") == "acm" and doi:
                        # some CACM pieces are web-only (ACM sends an empty PDF): keep the
                        # full-text page; page_to_pdf refuses abstract-only pages
                        d3, n3 = vet(*browser.page_to_pdf("https://dl.acm.org/doi/full/%s" % doi, min_chars=12000))
                        if d3:
                            data, note = d3, "full-text page saved as PDF"
                        elif n3:
                            note = (note + "; " if note else "") + n3
            except Exception as e:
                data, note = None, "browser: %s" % str(e)[:60]
            if data:
                return write(data, note)
            if note:
                attempts.append(note)

    # 5) the library's own copy (e.g. EBSCO Business Source Ultimate, if library.json has an "ebsco_home") for journals
    #    the publisher site won't give us
    lib = load_json(os.path.join(BASE, "library.json"), {})
    if browser is not None and not dry and p.get("abbr") in (lib.get("via_ebsco") or {}).get("journals", []):
        try:
            data, note = browser.fetch_via_ebsco(doi, p.get("title"))
        except Exception as e:
            data, note = None, "library EBSCO: %s" % str(e)[:60]
        if data:
            got = store(data, note)
            if got:
                return got
        else:
            attempts.append(note)
    # 6) a legal free copy elsewhere (arXiv, PubMed Central, preprint servers)
    if not dry and p.get("publisher") != "hbr":
        import open_copies
        data, note = open_copies.find_open_copy(doi, p.get("title"), http_get, browser, authors=p.get("authors"))
        if data:
            got = store(data, note)
            if got:
                return got
        else:
            attempts.append(note)
            if note.startswith("wrong paper"):
                refused.append(note)
    held = (lib.get("not_held_recent") or {}).get(p.get("abbr"))
    if held:
        attempts.append("not_in_library: %s only has %s via %s" % (lib.get("name") or "your library", p.get("abbr"), held))

    if not dry:                 # a dry run must leave the index alone
        save_index(p, "", "wrong_paper" if refused else "failed", "; ".join(refused + [a for a in attempts if a not in refused])[:300])
    return False, None, " | ".join(attempts) or "no route available"


def main():
    args = sys.argv[1:]
    dry = "--dry-run" in args
    limit = None
    if "--limit" in args:
        limit = int(args[args.index("--limit") + 1])
    pick = None
    if "--pick" in args:
        pick = [x.strip() for x in args[args.index("--pick") + 1].split(",") if x.strip()]

    short = load_json(os.path.join(STATE, "shortlist.json"), [])
    if pick:
        # --pick searches the full scored set, not just the top-20 shortlist
        pool = load_json(os.path.join(STATE, "scored.json"), []) or short
        short = [p for p in pool
                 if any(k in (p.get("doi") or "") or k in (p.get("title") or "")
                        for k in pick)]
    if not short:
        print("state/shortlist.json is empty — run fetch_new.py + score.py first.")
        return
    if limit:
        short = short[:limit]
    if not short:
        print("Nothing selected.")
        return

    env = load_env()
    print("Downloading %d papers%s\n" % (len(short), " [DRY RUN]" if dry else ""))
    failed = []
    with open_browser(no_browser="--no-browser" in args or dry) as br:
        for i, p in enumerate(short, 1):
            ok, path, note = download_paper(p, env, dry, browser=br)
            status = "OK  " if ok else "FAIL"
            print("%s %2d. [%s] %s" % (status, i, p.get("abbr"), (p.get("title") or "")[:70]))
            print("     %s" % (note if not ok else path))
            if not ok:
                failed.append(p)
            time.sleep(1.0)  # be polite to publishers

    if failed:
        print("\n%s FAILED. If a note says needs_login, run `browser.py login` and sign in." % len(failed))
        print("  Otherwise open the URL below, save the PDF into Papers/inbox/, and the")
        print("  agent will file it into Papers/YYYY-MM/<Journal>/.")
        for p in failed:
            u = browser_pdf_url(p)
            print("  - [%s] %s" % (p.get("abbr"), u or "no URL"))


class open_browser:
    """Opens browser.Browser if Playwright is installed; yields None otherwise."""

    def __init__(self, no_browser=False, on_challenge=None):
        self.no_browser = no_browser
        self.on_challenge = on_challenge
        self.b = None

    def __enter__(self):
        if self.no_browser:
            return None
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from browser import Browser
            self.b = Browser(on_challenge=self.on_challenge)
            return self.b.__enter__()
        except Exception as e:
            print("(browser route unavailable: %s)" % str(e)[:80])
            self.b = None
            return None

    def __exit__(self, *exc):
        if self.b is not None:
            self.b.__exit__(*exc)


if __name__ == "__main__":
    main()
