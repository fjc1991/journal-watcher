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
import signins  # noqa: E402

BASE, ROOT, STATE, PAPERS, LOCAL = paths.BASE, paths.ROOT, paths.STATE, paths.PAPERS, paths.LOCAL       # see paths.py: the project folder, or the app's data home
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def read_dotenv():
    """Only the .env files: project-root .env, then the skill-folder .env (older layout)."""
    env = {}
    for p in (os.path.join(BASE, ".env"), os.path.join(ROOT, ".env")):
        if os.path.exists(p):
            for line in open(p):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def key_names():
    """The names of the API keys the AI providers use (from ai.json), for looking them up in the Keychain."""
    names = {"ANTHROPIC_API_KEY"}
    for ai_json in (os.path.join(BASE, "ai.json"),):
        for p in ((load_json(ai_json, {}) or {}).get("providers") or {}).values():
            if isinstance(p, dict) and p.get("key_env"):
                names.add(str(p["key_env"]))
    return sorted(names)


def load_env():
    """The .env files, with the API keys that are in this Mac's Keychain on top (a key you moved there wins over an old line left in a file)."""
    env = read_dotenv()
    try:
        import keychain
        env.update(keychain.values(key_names()))
    except Exception:  # noqa: BLE001      (no Keychain, or it did not answer: the files are what there is)
        pass
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


def _site_word(url):
    """The part of a publisher's host that survives an EZproxy rewrite: onlinelibrary.wiley.com → wiley, www.sciencedirect.com → sciencedirect."""
    parts = urllib.parse.urlsplit(url).netloc.lower().split(":")[0].split(".")
    return parts[-3] if len(parts) >= 3 and ".".join(parts[-2:]) in ("co.uk", "ac.uk", "org.uk", "com.au", "co.jp") else parts[-2] if len(parts) >= 2 else ".".join(parts)


# The host words (as _site_word gives them) of each publisher's own sites. A paper's PDF route can start at doi.org (MISQ and JAIS; a DSS paper found by an archive
# search, which has no ScienceDirect address yet): that address names no publisher, so the page an attempt ends on is compared with these instead.
PUBLISHER_SITES = {"aisel": ("aisnet",), "informs": ("informs",), "wiley": ("wiley",), "tandfonline": ("tandfonline",), "elsevier": ("sciencedirect",),
                   "acm": ("acm",), "aaas": ("science",), "pnas": ("pnas",), "nber": ("nber",), "hbr": ("hbr",), "ssrn": ("ssrn",)}
_RESOLVERS = ("doi.org", "handle.net")
# a sign-in service's host (OpenAthens, Microsoft, Okta, a university's sso./idp./login. host): never "the publisher's site"
_IDP_HOST = re.compile(r"(^|[.-])(login|signin|sso|idp|shibboleth|adfs|cas|wayf|auth)[.-]|openathens\.net$|microsoftonline\.com$|okta\.com$")


def _ended_on(note):
    """The host a browser attempt ended on, from its note ("needs_login:<host> …" or "browser: no PDF at <address>"); "" when the note does not say."""
    m = re.match(r"needs_login:([^\s;()]+)", note or "")
    if m:
        return m.group(1).lower()
    m = re.match(r"browser: no PDF at (\S+)", note or "")
    return urllib.parse.urlsplit(m.group(1)).netloc.lower().split(":")[0] if m else ""


def _publisher_words(p, start, first_note):
    """The host words that mean "the publisher's own site" for this paper: the publisher's known sites, the address the attempt started from (unless it is a DOI
    resolver), and the site the plain attempt (without the library) ended on, unless that was already a sign-in service. Through the library, ending on one of these
    is the publisher's paywall. Ending anywhere else means the library asked for a password."""
    words = set(PUBLISHER_SITES.get(p.get("publisher"), ()))
    host = urllib.parse.urlsplit(start or "").netloc.lower().split(":")[0]
    if host and not host.endswith(_RESOLVERS):
        words.add(_site_word(start))
    first = _ended_on(first_note)
    if first and not _IDP_HOST.search(first):
        words.add(_site_word("https://" + first + "/"))
    return {w for w in words if w}


def browser_pdf_url(p):
    """PDF URL for the real-browser route (browser.py)."""
    pub, doi = p.get("publisher"), p.get("doi")
    m = re.fullmatch(r"10\.2139/ssrn\.(\d+)", doi or "", re.I)
    if m:                                       # an SSRN paper: its page there (the PDF link on it is followed: browser._pdf_link)
        return "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=%s" % m.group(1)
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
                    lib_ = signins.profile()
                    paywalled = "needs_login" in note or note.startswith("browser: no PDF at")
                    if not data and paywalled and lib_["signs_in"] and lib_["configured"]:
                        # The publisher did not give the PDF to this computer as it is (on campus the network address usually does; off campus it does not). Your
                        # university library may (OpenAthens or EZproxy, set up in Settings → Library access): the same page address is asked for through the library's
                        # address. This is tried whatever the sign-in tick says: the tick is a guess from the time of the last sign-in, and the university's single
                        # sign-on often lets a saved session through without a password long after it. Only when the library has asked for a password in this batch
                        # is it not asked again (each try would only wait for a password); the card then offers the library sign-in.
                        if getattr(browser, "library_signed_out", False):
                            note += "; needs_library_login"
                        else:
                            seen, plain = len(refused), note
                            d4, n4 = browser.fetch_pdf(signins.via(u), doi=doi)
                            d4, _ = vet(d4, "browser (via your library)")
                            if d4:
                                data, note = d4, "browser (via your library)"
                                signins.record("library", True)                       # it worked: the sign-in is current, whatever the clock said
                            elif len(refused) == seen and n4 and n4.startswith("library_setup:"):
                                try:                                                  # a wrong OpenAthens domain: no paper can come this way; asking again would not help
                                    browser.library_signed_out = True
                                except AttributeError:
                                    pass
                                note += "; " + n4
                            elif len(refused) == seen and n4:
                                at = n4.split(":", 1)[1].split()[0].lower() if n4.startswith("needs_login:") else ""
                                # it ended on somebody else's sign-in page: the library session has ended. The publisher's own host, also as an EZproxy rewrites it
                                # (onlinelibrary-wiley-com.ezproxy.uni.edu still contains "wiley"), is the publisher's paywall, not the library asking for a password.
                                # "The publisher's own host" is not only the start address's: from doi.org (MISQ, JAIS) it is where the plain attempt ended.
                                if at and not any(w in at for w in _publisher_words(p, u, plain)):
                                    signins.forget("library")
                                    try:
                                        browser.library_signed_out = True
                                    except AttributeError:
                                        pass
                                    note += "; needs_library_login (your library asked for a password at %s)" % at
                                else:
                                    note += "; via your library: " + n4
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
    #    Set up in Settings → Library access ("Find my library's settings" finds the start page). With a list in library.json via_ebsco.journals only those journals
    #    are looked for there; without one, every paper the publisher did not give is (a search takes some seconds). Not tried once the library has asked for a
    #    password in this batch (the EBSCO search goes through the same sign-in).
    lib = load_json(os.path.join(BASE, "library.json"), {})
    prof = signins.profile()                                # (the same library.json, read with its defaults filled in)
    only = (prof.get("via_ebsco") or {}).get("journals") or []
    free_kind = p.get("publisher") in ("nber",) or re.fullmatch(r"10\.2139/ssrn\.\d+", doi or "", re.I)      # working papers: not in a subscription database
    if (browser is not None and not dry and prof["use_ebsco"] and (doi or p.get("title")) and (not only or p.get("abbr") in only)
            and not free_kind and not getattr(browser, "library_signed_out", False)):
        try:
            data, note = browser.fetch_via_ebsco(doi, p.get("title"))
        except Exception as e:
            data, note = None, "library EBSCO: %s" % str(e)[:60]
        if data:
            got = store(data, note)
            if got:
                return got
        else:
            if note == "needs_library_login":
                try:
                    browser.library_signed_out = True
                except AttributeError:
                    pass
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
