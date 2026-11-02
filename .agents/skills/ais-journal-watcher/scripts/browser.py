#!/usr/bin/env python3
"""Real-browser PDF route (Playwright Chromium, persistent profile).

Publishers sit behind Cloudflare-style bot protection, so plain HTTP gets 403.
Downloads run in the background (Chromium's new headless mode with a normal
user agent passes most checks) and reuse the sign-ins saved by `login`.
When a site insists on an interactive "Verify you are human" check (e.g.
ScienceDirect), a visible window opens on that page and the user ticks the box
themselves (it is never clicked automatically). The rest of that site's papers
in the same batch reuse the window, so it is one tick per batch.
AIS_BROWSER_INTERACTIVE=0 skips such papers instead (status needs_check).

Usage:
    python3 browser.py login          # open sign-in pages (publishers, and your library); log in, then close the window
    python3 browser.py library-login  # sign in through your university library (OpenAthens or EZproxy; set up in Settings → Library access)
    python3 browser.py library-test   # is your library access working now? (opens your library page in the background; prints one line of JSON)
    python3 browser.py member-login informs|ais   # sign in with a membership account
    python3 browser.py get <url> <out.pdf>

Set AIS_BROWSER_VISIBLE=1 to watch every download in a visible window.
"""
import json
import os
import re
import sys
import time
import urllib.parse

from playwright.sync_api import sync_playwright, Error as PWError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402
import signins as si  # noqa: E402

BASE = paths.BASE
PROFILE = os.path.join(BASE, "state", "browser-profile")
# Chromium drops session cookies (OpenAthens, Microsoft sign-in) when it closes;
# keep them here between launches so one library sign-in lasts its full session
SESSION_COOKIES = os.path.join(BASE, "state", "session-cookies.json")
SESSION_MAX_AGE = 12 * 3600

PUBLISHER_LOGINS = [
    ("INFORMS (ISR, MS, MSOM, Org Science) — member sign-in",
     "https://pubsonline.informs.org/action/showLogin"),
    ("Wiley (ISJ) — Institutional login: choose your university",
     "https://onlinelibrary.wiley.com/action/ssostart"),
    ("ScienceDirect (DSS) — Sign in via your institution",
     "https://www.sciencedirect.com/user/institution/login"),
    ("Taylor & Francis (JMIS) — Institutional access",
     "https://www.tandfonline.com/action/ssostart"),
    ("AIS eLibrary (MISQ, JAIS) — AIS member login",
     "https://aisel.aisnet.org/cgi/login.cgi"),
    ("Science — Institutional access",
     "https://www.science.org/action/ssostart"),
    ("PNAS — Institutional access",
     "https://www.pnas.org/action/ssostart"),
    ("Harvard Business Review — sign in (if you subscribe)",
     "https://hbr.org/sign-in"),
    ("SSRN — sign in (if you have an account: it can make downloads easier)",
     "https://papers.ssrn.com/"),
]


def login_pages():
    """One tab per publisher, and first your own library's page if you have set one up."""
    lib = si.profile()
    pages = []
    if lib["signs_in"]:                       # the library's own sign-in, through a publisher page (the library's web site is not protected: going there signs nobody in)
        pages.append(("%s — your library's sign-in (OpenAthens/EZproxy)" % (lib["name"] or "Library"), si.via(si.probe_url())))
    if lib["home"]:
        pages.append(("%s — your library's web site" % (lib["name"] or "Library"), lib["home"]))
    return pages + PUBLISHER_LOGINS


# links on a landing page that lead to the PDF
PDF_LINK_SEL = ", ".join(s + ':not([href*="upplement"]):not([href*="/suppl"]):not([href*="type=additional"])' for s in [
    'a[href*="/doi/pdf/"]', 'a[href*="/doi/pdfdirect/"]', 'a[href*="pdfft"]',
    'a[href*="viewcontent.cgi"]', 'a[href$=".pdf" i]', 'a[href*=".pdf?" i]',
])  # never a "supplementary materials" PDF
# where on a page a PDF link is NOT this article's: its reference list, "cited by", related and recommended articles, banners, footers and menus
# (a reference to another paper that happens to link its .pdf must never be mistaken for the article itself)
REFERENCE_LISTS = ("[class*='reference' i]:not([class*='preference' i]), [id*='reference' i]:not([id*='preference' i]), [class*='ref-list' i], [id*='ref-list' i], "
                   "[class*='bibliograph' i], [id*='bibliograph' i], [class*='citations' i], [id*='citations' i]")
NOT_THIS_ARTICLE = (REFERENCE_LISTS + ", [class*='cited' i]:not([class*='excited' i]), [id*='cited' i], [class*='related' i], [id*='related' i], [class*='recommend' i], [id*='recommend' i], "
                    "[class*='footer' i], [id*='footer' i], [class*='banner' i], [class*='cookie' i], [id*='onetrust' i], footer, nav, aside, [role='contentinfo'], [role='navigation']")
# the links of the page, and for each whether it sits in one of those places (a wrapper that is the whole page, or contains its headline, is not a footer or a list)
PAGE_LINKS_JS = """([sel, skip, refs]) => {
  const inside = (a, q) => { let el = a.closest(q); while (el && (el === document.documentElement || el === document.body || el.querySelector('h1'))) el = el.parentElement ? el.parentElement.closest(q) : null; return !!el; };
  return {meta: [...document.querySelectorAll('meta[name="citation_pdf_url"], meta[name="bepress_citation_pdf_url"]')].map(m => m.content).filter(Boolean),
          links: [...document.querySelectorAll(sel)].filter(a => typeof a.href === 'string').map(a => ({href: a.href, other: inside(a, skip), ref: inside(a, refs)}))};
}"""
TWO_LEVEL = {"co.uk", "ac.uk", "org.uk", "gov.uk", "com.au", "edu.au", "co.jp", "ac.jp", "co.nz", "com.br", "co.za", "com.cn", "edu.cn"}


def site_of(u):
    """The site a web address belongs to (its registered domain: onlinelibrary.wiley.com and cdn.wiley.com are one site; localhost and 127.0.0.1 are two)."""
    host = (urllib.parse.urlsplit(u).hostname or "").lower().removeprefix("www.")
    if re.fullmatch(r"[\d.]+|[0-9a-f:]+", host):
        return host
    labels = host.split(".")
    return ".".join(labels[-3:] if len(labels) >= 3 and ".".join(labels[-2:]) in TWO_LEVEL else labels[-2:])


def doi_in_url(u):
    """The DOI a publisher address names (/doi/pdf/<doi>, /doi/abs/<doi> ...), lower-case and unquoted, or None."""
    m = re.search(r"/doi/(?:abs/|full/|pdf/|pdfdirect/|epdf/)?(10\.\d{4,9}/[^?#]+)", u or "")
    return urllib.parse.unquote(m.group(1)).rstrip("/").lower() if m else None


# paywall wording (not plain "sign in"/"subscribe", which every site header has)
LOGIN_HINTS = re.compile(r"(request access|purchase|access through your institution|check access|"
                         r"log ?in to (view|access|read)|get access|buy (this )?article|rent this article|"
                         r"pay per view|institutional (login|access))", re.I)
CHALLENGE = re.compile(r"(just a moment|security verification|are you a robot|"
                       r"verify you are human|checking your browser)", re.I)


def is_pdf(data):
    """A complete PDF: header at the start, %%EOF near the end (streamed/partial
    responses from in-browser readers fail the second test)."""
    return bool(data) and len(data) > 40_000 and \
        data[:1024].lstrip(b"\xef\xbb\xbf \r\n").startswith(b"%PDF") and b"%%EOF" in data[-2048:]


def is_challenge(page):
    if "crasolve" in page.url or "/cdn-cgi/" in page.url:
        return True  # ScienceDirect's silent intermediate check
    try:
        return bool(CHALLENGE.search(page.title() or "") or
                    CHALLENGE.search(page.inner_text("body")[:1500]))
    except PWError:
        return False


def prefer_pdf_downloads():
    """Make Chromium download PDFs instead of opening its viewer, so every PDF
    arrives as a download event (the viewer swallows the response body)."""
    path = os.path.join(PROFILE, "Default", "Preferences")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        prefs = json.load(open(path))
    except (OSError, ValueError):
        prefs = {}
    if (prefs.get("plugins") or {}).get("always_open_pdf_externally") is True:
        return
    prefs.setdefault("plugins", {})["always_open_pdf_externally"] = True
    prefs.setdefault("download", {})["prompt_for_download"] = False
    with open(path, "w") as f:
        json.dump(prefs, f)


class Browser:
    """Context manager around one persistent Chromium context.

    visible=False (default): background downloads; an interactive challenge
    reopens just that page in a visible window and calls
    on_challenge(host, state) with state "waiting" / "passed" / "failed".
    """

    def __init__(self, visible=None, on_challenge=None, interactive=None):
        if visible is None:
            visible = os.environ.get("AIS_BROWSER_VISIBLE") == "1"
        if interactive is None:  # default: let the user verify in a visible window
            interactive = os.environ.get("AIS_BROWSER_INTERACTIVE", "1") != "0"
        self.checked_hosts = set()  # hosts verified by the user in this batch
        self.visible = self.start_visible = visible
        self.on_challenge = on_challenge
        self.interactive = interactive
        self.ctx = None
        self._ua = None

    def __enter__(self):
        os.makedirs(PROFILE, exist_ok=True)
        if paths.FROZEN:        # the Mac app does not carry Chromium: it is fetched the first time (see paths.ensure_browser)
            paths.ensure_browser()
        self._pw = sync_playwright().start()
        self._launch(self.start_visible)
        return self

    def __exit__(self, *exc):
        try:
            if self.ctx:
                self._save_session()
                self.ctx.close()
        finally:
            self._pw.stop()

    def _save_session(self):
        try:
            keep = [c for c in self.ctx.cookies() if c.get("expires", -1) == -1]
        except PWError:
            return
        tmp = SESSION_COOKIES + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"saved": time.time(), "cookies": keep}, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, SESSION_COOKIES)

    def _restore_session(self):
        try:
            d = json.load(open(SESSION_COOKIES))
        except (OSError, ValueError):
            return
        if time.time() - d.get("saved", 0) > SESSION_MAX_AGE or not d.get("cookies"):
            return
        try:
            self.ctx.add_cookies(d["cookies"])
        except PWError:
            pass

    def _launch(self, visible):
        if self.ctx:
            self._save_session()
            self.ctx.close()
        prefer_pdf_downloads()
        kw = dict(channel="chromium", headless=not visible, accept_downloads=True,
                  args=["--disable-blink-features=AutomationControlled"])
        if not visible:
            # headless Chromium announces itself as "HeadlessChrome"; use the
            # browser's normal user agent instead so sites treat it like a browser
            kw["user_agent"] = self._user_agent()
        self.ctx = self._pw.chromium.launch_persistent_context(PROFILE, **kw)
        self._restore_session()
        self.visible = visible

    def _user_agent(self):
        if not self._ua:
            b = self._pw.chromium.launch(channel="chromium", headless=True)
            try:
                ua = b.new_page().evaluate("navigator.userAgent")
            finally:
                b.close()
            self._ua = ua.replace("HeadlessChrome", "Chrome")
        return self._ua

    def _try(self, page, url, wait_s, settle_s=5):
        """Navigate to url; return PDF bytes if the browser ends up with one.
        Gives up `settle_s` after the page stops being a challenge page."""
        resps, dls = [], []
        on_resp = lambda r: resps.append(r)
        on_dl = lambda d: dls.append(d)
        page.on("response", on_resp)
        page.on("download", on_dl)
        try:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            except PWError:
                pass  # "Download is starting" or a slow challenge page
            deadline, calm = time.time() + wait_s, None
            while time.time() < deadline:
                if dls:
                    try:
                        data = open(dls[0].path(), "rb").read()  # waits for completion
                    except (PWError, OSError):
                        data = None
                    if is_pdf(data):
                        return data
                    self._empty_dl = True  # e.g. PNAS sends an empty "PDF" without access
                    dls.clear()
                for r in resps:
                    if "pdf" in (r.headers.get("content-type") or "").lower():
                        try:
                            data = r.body()
                        except PWError:
                            continue
                        if is_pdf(data):
                            return data
                resps.clear()
                if is_challenge(page):
                    calm = None
                else:
                    calm = calm or time.time()
                    if time.time() - calm > settle_s:
                        break
                page.wait_for_timeout(1000)
        finally:
            page.remove_listener("response", on_resp)
            page.remove_listener("download", on_dl)
        return None

    @staticmethod
    def _pdf_link(page, url, doi=None):
        """The PDF link on a landing page that belongs to THIS article, or None. In this order: the page's own declaration of its PDF (the citation_pdf_url tag, if on this
        site or naming the DOI); a link that names the DOI we asked for (wherever it sits, except in a reference list); otherwise, a link of the page itself: on the same site,
        not in the references, related articles, banners, footer or a menu, and, when the DOI is known, only the kinds of link that carry no DOI (viewcontent.cgi, pdfft:
        another article's /doi/pdf/ link would have named its own DOI). Taking the first PDF link on the page, whatever it pointed to, once stored another paper's PDF under
        this paper's title."""
        try:
            found = page.evaluate(PAGE_LINKS_JS, [PDF_LINK_SEL, NOT_THIS_ARTICLE, REFERENCE_LISTS])
        except PWError:
            return None
        base = page.url
        doi = (doi or doi_in_url(url) or doi_in_url(base) or "").lower().rstrip("/") or None
        names = lambda u: bool(doi) and bool(re.search(re.escape(doi) + r"(?=$|[?#&/])", urllib.parse.unquote(u).lower()))
        for m in found["meta"]:
            u = urllib.parse.urljoin(base, m)
            if names(u) or site_of(u) == site_of(base):
                return u
        links = [(urllib.parse.urljoin(base, l["href"]), l) for l in found["links"]]
        for u, l in links:
            if names(u) and not l["ref"]:
                return u
        ssrn = re.fullmatch(r"10\.2139/ssrn\.(\d+)", doi or "")
        for u, l in links:
            if l["other"] or site_of(u) != site_of(base):
                continue
            if ssrn and re.search(r"Delivery\.cfm", u, re.I):      # SSRN's own download link carries the paper's number (and no DOI), as SSRN_ID<n> and/or abstractid=<n>
                ids = set(re.findall(r"SSRN_ID(\d+)|[?&;]abstract_?id=(\d+)", u, re.I))
                ids = {a or b for a, b in ids}
                if ids == {ssrn.group(1)}:          # explicit identifiers only, and none that names another paper (a stray "code" number that happens to match does not count)
                    return u
            if doi and ("/doi/" in u or not re.search(r"viewcontent\.cgi|pdfft", u)):
                continue
            return u
        return None

    def _fetch(self, url, wait_s, doi=None):
        page = self.ctx.new_page()
        self._empty_dl = False
        try:
            data = self._try(page, url, wait_s)
            if data:
                return data, "browser", None
            # landing page instead of a PDF: follow a PDF link if there is one
            cand = None
            if "sciencedirect.com/science/article" in page.url:
                m = re.search(r"/pii/([A-Z0-9]+)", page.url)
                if m:
                    cand = "https://www.sciencedirect.com/science/article/pii/%s/pdfft" % m.group(1)
            if not cand and not is_challenge(page):
                cand = self._pdf_link(page, url, doi)
            if cand and cand != url:
                data = self._try(page, cand, wait_s)
                if data:
                    return data, "browser", None
            host = urllib.parse.urlsplit(page.url).netloc
            if is_challenge(page):
                return None, "challenge:%s" % host, host
            try:
                text = page.inner_text("body")[:5000]
            except PWError:
                text = ""
            if re.search(r"/doi/(pdf|pdfdirect|epdf)/", url) and re.search(r"/doi/(abs/|full/|10\.)", page.url) \
                    and "acm.org" not in page.url:
                # asked for the PDF, got bounced to the abstract page: that's the paywall
                return None, "needs_login:%s" % urllib.parse.urlsplit(page.url).netloc, None
            if self._empty_dl:
                return None, "needs_login:%s (publisher sent an empty file)" % urllib.parse.urlsplit(url).netloc, None
            if OA_NOT_ENABLED.search(text):
                return None, "library_setup: OpenAthens says the library's domain is not enabled (check Settings → Library access)", None
            try:
                pwd = page.locator("input[type=password]:visible").count()
            except PWError:
                pwd = 0
            if looks_like_login(page.url) or pwd:
                # it ended on a sign-in page (an identity provider's, such as the university's single sign-on through OpenAthens, or the publisher's own): the host
                # says whose, which tells a library session that has ended (another host than the publisher's) from a publisher that wants its own login
                return None, "needs_login:%s" % host, None
            if LOGIN_HINTS.search(text) or "login.cgi" in page.url:
                return None, "needs_login:%s" % host, None
            return None, "browser: no PDF at %s" % page.url[:120], None
        finally:
            page.close()

    def fetch_pdf(self, url, wait_s=25, doi=None):
        """Returns (pdf_bytes | None, note). `doi`: the paper's DOI, when the page we land on should name it (the publishers' own routes); a free copy on some repository does not."""
        host = urllib.parse.urlsplit(url).netloc
        if host in self.checked_hosts:
            # the user already verified this site in the visible window: stay there
            if not self.visible:
                self._launch(visible=True)
            data, note, again = self._fetch(url, wait_s=180, doi=doi)
            if again:
                self._notify(again, "waiting")
            return data, note
        if self.visible and not self.start_visible:
            self._launch(visible=False)  # back to the background for other sites
        data, note, chall = self._fetch(url, wait_s, doi=doi)
        if chall and self.interactive:
            # "Verify you are human": open this page in a window for the user
            self._notify(chall, "waiting")
            self._launch(visible=True)
            data, note, still = self._fetch(url, wait_s=180, doi=doi)
            self._notify(chall, "failed" if still else "passed")
            if not still:
                self.checked_hosts.update({host, chall})
        return data, note

    def page_to_pdf(self, url, min_chars=5000):
        """Save an article web page (HBR) as a PDF. Needs the headless browser."""
        if self.visible:
            self._launch(visible=False)
        page = self.ctx.new_page()
        try:
            try:
                page.goto(url, wait_until="networkidle", timeout=60_000)
            except PWError:
                pass
            page.wait_for_timeout(2000)
            if is_challenge(page):
                return None, "challenge:%s" % urllib.parse.urlsplit(url).netloc
            # keep cookie/consent overlays out of the saved copy
            page.evaluate("""() => {
                document.querySelectorAll("#onetrust-consent-sdk, .fc-consent-root, .fc-dialog-overlay,"
                  + "#CybotCookiebotDialog, [id^='Cybot'], [id*='consent' i], [class*='consent' i],"
                  + "[class*='cookie' i], [id*='cookie' i], [aria-modal='true']")
                  .forEach(e => e.remove());
                // keep just the article when the page marks one up (drops menus, ads, comments)
                const a = document.querySelector("article");
                if (a && a.innerText.length > 3000) {
                    document.body.replaceChildren(a);
                    a.style.cssText = "max-width: 46em; margin: 0 auto; font-size: 11pt;";
                }
                document.body.style.setProperty("overflow", "visible", "important");
            }""")
            if len(page.inner_text("body")) < min_chars:
                return None, "page too short to be the full article (paywall?)"
            data = page.pdf(format="A4", print_background=False,
                            margin={"top": "15mm", "bottom": "15mm", "left": "12mm", "right": "12mm"})
            return (data, "page saved as PDF") if is_pdf(data) else (None, "page too short (paywall?)")
        finally:
            page.close()

    def renew_member(self, site):
        """Silently renew a member session (the account site still remembers
        the user). Tried once per site per batch. True if signed in again."""
        if not hasattr(self, "_renewed"):
            self._renewed = {}
        if site in self._renewed:
            return self._renewed[site]
        cfg = MEMBER_SITES[site]
        page = self.ctx.new_page()
        ok = False
        try:
            try:
                page.goto(cfg["start"], wait_until="domcontentloaded", timeout=45_000)
            except PWError:
                pass
            for _ in range(15):
                page.wait_for_timeout(1000)
                if page.locator("input[type=password]").count():
                    break  # the account site wants the password: needs the user
                if cfg["home"] in page.url and "showLogin" not in page.url \
                        and not any(h in page.url for h in cfg["login_hosts"]):
                    ok = True
                    break
        finally:
            page.close()
        self._renewed[site] = ok
        return ok

    def fetch_from_reader(self, reader_url, wait_s=20):
        """Publisher online readers (Atypon "epdf": PNAS, Science, INFORMS, Wiley,
        T&F) stream the PDF in byte ranges; find the file they load and fetch it
        whole from inside the page (same cookies, same browser)."""
        page = self.ctx.new_page()
        urls = []
        page.on("response", lambda r: urls.append(r.url)
                if "pdf" in (r.headers.get("content-type") or "").lower() and r.url != reader_url else None)
        try:
            try:
                page.goto(reader_url, wait_until="domcontentloaded", timeout=45_000)
            except PWError:
                pass
            for _ in range(wait_s):
                if urls:
                    break
                page.wait_for_timeout(1000)
            if not urls:
                return None, "reader: no PDF (no access?)"
            b64 = page.evaluate("""async (u) => {
                const r = await fetch(u, {credentials: "include"});
                if (!r.ok) return "ERR" + r.status;
                const buf = new Uint8Array(await r.arrayBuffer()); let s = "";
                for (let i = 0; i < buf.length; i += 0x8000) s += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
                return btoa(s); }""", urls[0])
            if b64.startswith("ERR"):
                return None, "reader: HTTP %s" % b64[3:]
            import base64
            data = base64.b64decode(b64)
            return (data, "browser (publisher reader)") if is_pdf(data) else (None, "reader: incomplete PDF")
        except PWError as e:
            return None, "reader: %s" % str(e)[:60]
        finally:
            page.close()

    # ---------------------------------------------------------- library (EBSCO)

    def _library_signin(self, page, target):
        """Reach `target` (an EBSCO URL) with the library session; renew it through
        OpenAthens + the remembered Microsoft account when possible.
        Returns True when signed in, "login" when a password is needed, False when it did not get there (and nothing asked for a password)."""
        try:
            page.goto(target, wait_until="domcontentloaded", timeout=60_000)
        except PWError:
            pass
        page.wait_for_timeout(3000)
        if "research.ebsco.com" in page.url and "login" not in page.url and not on_library_login(page):
            return True
        try:
            page.goto(via_library(target), wait_until="domcontentloaded", timeout=60_000)
        except PWError:
            pass
        for _ in range(30):
            page.wait_for_timeout(1000)
            if "research.ebsco.com" in page.url and not on_library_login(page):
                return True
            try:
                if page.locator("input[type=password]:visible").count():
                    return "login"  # any identity provider asking for the password: the user has to sign in again
            except PWError:
                pass
            if "login.openathens.net" in page.url and library().get("chooser_text"):
                opt = page.get_by_text(library()["chooser_text"], exact=False).first
                if opt.count():
                    opt.click()
            elif "login.microsoftonline.com" in page.url:
                if page.locator("input[type=password]").count():
                    return "login"  # password needed: the user has to sign in again
                tiles = page.locator("[data-test-id] >> text=/@/")
                if tiles.count() == 1:
                    tiles.first.click()
        return False

    def fetch_via_ebsco(self, doi, title=None):
        """Find a paper in the library's EBSCO by DOI (or exact title) and
        download EBSCO's PDF. Returns (pdf | None, note)."""
        lib = library()
        base = lib["ebsco_home"].rstrip("/")
        page = self.ctx.new_page()
        try:
            queries = (["DI " + doi] if doi else []) + (['TI "%s"' % title.replace('"', "")] if title else [])
            first = base + "/search/results?q=" + urllib.parse.quote(queries[0])
            got = self._library_signin(page, first)
            if got == "login":
                return None, "needs_library_login"
            if got is not True:
                return None, "library EBSCO: the library's sign-in did not finish (no password was asked for)"
            for q in queries:
                url = base + "/search/results?q=" + urllib.parse.quote(q)
                if page.url != url:
                    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                links = page.locator("a[data-auto='result-item-title__link']")
                for _ in range(25):  # results render client-side: wait for them (or "No results")
                    if links.count() or page.get_by_text("No results for", exact=False).count():
                        break
                    page.wait_for_timeout(1000)
                self._dismiss_banner(page)
                for i in range(min(links.count(), 5)):
                    t = links.nth(i).inner_text()
                    if q.startswith("DI ") or _same_title(t, title):
                        href = links.nth(i).get_attribute("href")
                        return self._ebsco_download(page, urllib.parse.urljoin(page.url, href))
            return None, "not found in library EBSCO (very new papers appear once in an issue)"
        except PWError as e:
            return None, "library EBSCO: %s" % str(e)[:80]
        finally:
            page.close()

    def _ebsco_download(self, page, details_url):
        page.goto(details_url, wait_until="domcontentloaded", timeout=60_000)
        btn = page.locator("[data-auto='card-call-to-action-download-button']")
        for _ in range(20):
            if btn.count():
                break
            page.wait_for_timeout(1000)
        self._dismiss_banner(page)
        if not btn.count():
            return None, "library EBSCO has no PDF for this record"
        btn.first.click()
        page.wait_for_timeout(1500)
        pdf = page.locator("input[data-auto='bulk-download-formats-group-input'][value='pdf']")
        if pdf.count():
            pdf.first.check()
        with page.expect_download(timeout=90_000) as dl:
            page.click("[data-auto='bulk-download-modal-download-button']")
        data = open(dl.value.path(), "rb").read()
        return (data, "library (EBSCO)") if is_pdf(data) else (None, "library EBSCO sent no PDF")

    @staticmethod
    def _dismiss_banner(page):
        b = page.locator("button:has-text('Reject Non-Essential')")
        if b.count():
            try:
                b.first.click(timeout=3000)
            except PWError:
                pass

    def _notify(self, host, state):
        if self.on_challenge:
            try:
                self.on_challenge(host, state)
            except Exception:
                pass


def _norm(t):
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def _same_title(a, b):
    import difflib
    a, b = _norm(a), _norm(b)
    return bool(a and b) and (a == b or difflib.SequenceMatcher(None, a, b).ratio() >= 0.92)


def library():
    return si.profile()


def via_library(url):
    """The address that reaches `url` through your library (its OpenAthens redirector or EZproxy prefix; see signins.via)."""
    return si.via(url)


_LOGINISH = re.compile(r"(^|[/.?=&_-])(login|signin|sign-in|sso|saml2?|shibboleth|idp|openathens|microsoftonline|okta|adfs|cas|wayf|seamlessaccess|authenticate)([/.?=&_-]|$)")


def looks_like_login(url):
    """A sign-in page of some identity provider (university single sign-on, OpenAthens, Microsoft, Okta, Shibboleth …): judged by its address."""
    return bool(_LOGINISH.search((url or "").lower()))


def on_library_login(page):
    return looks_like_login(page.url)


def celebrate(page, text, sub="Journal Watcher has what it needs. This window closes by itself."):
    """Tell the person, in the browser window itself, that it worked — then give them a moment to read it before the window closes."""
    try:
        page.evaluate("""([t, u]) => {
          const d = document.createElement('div');
          d.style.cssText = 'position:fixed;inset:0;z-index:2147483647;display:grid;place-items:center;background:rgba(251,250,247,.94);font:16px -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;color:#201e1b';
          d.innerHTML = '<div style="text-align:center;max-width:460px;padding:24px"><div style="width:76px;height:76px;margin:0 auto 18px;border-radius:50%;background:#1b6b45;display:grid;place-items:center"><svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.2 4.2L19 7"/></svg></div><div style="font-size:26px;font-weight:700;letter-spacing:-.01em;margin-bottom:8px"></div><div style="color:#6a655d"></div></div>';
          d.children[0].children[1].textContent = t; d.children[0].children[2].textContent = u; document.body.appendChild(d);
        }""", [text, sub])
        page.wait_for_timeout(3200)
    except Exception:  # noqa: BLE001  (the page may be navigating: the sign-in still worked)
        pass


def settle(page, seconds=12):
    """Let the redirects of a sign-in redirector finish: the address is the same on three looks in a row (half a second apart), or `seconds` have passed."""
    last, same, t0 = None, 0, time.time()
    while time.time() - t0 < seconds:
        u = page.url
        same = same + 1 if u == last else 0
        last = u
        if same >= 3:
            return
        page.wait_for_timeout(500)


# what a publisher's page says when your university's access is in force (ScienceDirect: "Access provided by …")
ACCESS_SAYS = re.compile(r"(access provided by[^\n]{0,80}|provided by your (?:institution|library)[^\n]{0,60}|your institution has access[^\n]{0,60}|institutional access[^\n]{0,60})", re.I)
# an address on the way through a library's sign-in: an identity provider, OpenAthens, or a publisher's "institution login" step (Elsevier: auth.elsevier.com/ShibAuth/
# institutionLogin). Seeing one of these between the start and the publisher's page is what shows that the library's sign-in was really used (silently, with a saved
# session, or with a password).
_SIGNIN_STEP = re.compile(r"openathens\.net/(?!redirector)|shibauth|institutionlogin|/saml2?/|/idp/|/sso|wayf|seamlessaccess|microsoftonline|/adfs/|okta\.com|/cas/login|login\?q?url=", re.I)
OA_NOT_ENABLED = re.compile(r"redirector is not enabled for the specified customer domain", re.I)


def signin_step(url):
    """An address that is a step of a sign-in (see _SIGNIN_STEP). The OpenAthens redirector itself is not one: it is where every attempt starts, signed in or not."""
    u = url or ""
    if "go.openathens.net/redirector/" in u.lower():
        return False
    return bool(_SIGNIN_STEP.search(u)) or looks_like_login(u)


def _site_key(url):
    """The part of a publisher's host that survives an EZproxy rewrite: www.sciencedirect.com → sciencedirect (www-sciencedirect-com.ezproxy.uni.edu contains it too)."""
    host = urllib.parse.urlsplit(url).netloc.lower().removeprefix("www.")
    parts = host.split(".")
    return parts[-3] if len(parts) >= 3 and ".".join(parts[-2:]) in TWO_LEVEL else parts[-2] if len(parts) >= 2 else host


def _steps(chain, start, final):
    """The sign-in steps among the addresses a page went through: not the address it started from (the OpenAthens redirector or the EZproxy link, which every
    attempt starts from, signed in or not), not the page it ended on, only addresses that are steps of a sign-in (signin_step)."""
    return [u for u in chain if u != final and u != start and not u.startswith(start.split("?")[0] + "?") and signin_step(u)]


def _track(page):
    """The addresses the page's main frame goes through from now on (redirects included), as a list that keeps filling."""
    seen = []
    page.on("framenavigated", lambda f: seen.append(f.url) if f == page.main_frame else None)
    page.on("request", lambda r: seen.append(r.url) if r.is_navigation_request() and r.frame == page.main_frame else None)
    return seen


def library_test():
    """Is your library access working right now? Opens a publisher page (ScienceDirect, or library.json "probe") in the background through your library's sign-in, with
    the sessions saved by the sign-in windows, follows where it goes and classifies where it ends. Returns {state, message, host, saw, mode}; state: ok | login | setup
    | challenge | unknown | error.
    ok      OpenAthens / EZproxy took it through your library's sign-in (an identity-provider or institution-login step is in the chain of addresses, or the address is
            the EZproxy's own) back to the publisher without asking for a password; or the publisher's page says that your institution gives access.
    login   the library asked for a password (never signed in, or the session has expired).
    setup   the library's address itself is wrong (OpenAthens: "the redirector is not enabled for the specified customer domain").
    unknown the publisher's page opened but nothing shows that your library's sign-in was used, and the page does not say so either (that does not prove you have none:
            on campus the access is by network address, which a page often does not show).
    Until 1.2.0 the library's own web site was opened instead. It is not a protected resource, so OpenAthens just passes through to it and the test said "Signed in"
    without any sign-in having happened. It says what it saw and never more than that."""
    lib = library()
    proxy = lib["signs_in"]
    target = si.probe_url()
    url = via_library(target)
    out = {"mode": lib["mode"], "state": "error", "message": "", "host": "", "saw": ""}
    try:
        with Browser(interactive=False) as b:
            page = b.ctx.new_page()
            chain = _track(page)
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            settle(page)
            final = page.url
            out["host"] = urllib.parse.urlsplit(final).netloc.lower()
            host = out["host"] or "the page"
            try:
                text = page.inner_text("body")[:20000]
            except PWError:
                text = ""
            on_publisher = _site_key(target) in out["host"]
            steps = _steps(chain, url, final)
            if lib["mode"] == "ezproxy":            # through EZproxy the publisher is served under the proxy's own host name: that, and only that, shows the proxy session
                via_signin = bool(_ezproxy_host(lib)) and out["host"].endswith(_ezproxy_host(lib))
            else:
                via_signin = bool(steps)
            if OA_NOT_ENABLED.search(text):
                out.update(state="setup", message="OpenAthens says its redirector is not enabled for “%s”: that is not your library's OpenAthens domain. Use “Find my library's settings” above, or ask your library." % lib.get("openathens", ""))
            elif is_challenge(page):
                out.update(state="challenge", message="%s asked for a human check, which Journal Watcher cannot do in the background. Press “Sign in now” (or open the page yourself) and tick the box." % host)
            elif looks_like_login(final) or page.locator("input[type=password]:visible").count():
                out.update(state="login", message="Not signed in, or the sign-in has expired: %s asked for a password. %s" % (host, "Press “Sign in now” and sign in." if proxy else "Sign in on the publisher’s page (Sign-ins → Other publisher sites)."))
            else:
                m = ACCESS_SAYS.search(text)
                saw = re.sub(r"\s+", " ", m.group(0)).strip()[:100] if m else ""
                out["saw"] = saw
                step = urllib.parse.urlsplit(steps[0]).netloc if steps else ""
                if proxy and on_publisher and via_signin:
                    out.update(state="ok", message="Signed in: your library's sign-in%s let Journal Watcher through to %s without asking for a password.%s" % (" (%s)" % step if step else "", host, " The page says: “%s”." % saw if saw else ""))
                elif saw:
                    out.update(state="ok", message="%s says: “%s”. That looks like your university gives you access." % (host, saw))
                elif proxy:
                    out.update(state="unknown", message="%s opened, but your library's sign-in was not part of the way there and the page does not say whose access it is. On campus that can be normal (access by network address); off campus, press “Sign in now”." % host)
                else:
                    out.update(state="unknown", message="%s opened, but it does not say that your university gives you access. That does not prove you have none: some pages only show it on a paper." % host)
    except PWError as e:
        out.update(state="error", message="The page did not open (%s). Check your internet connection, your VPN and the addresses in Settings." % re.sub(r"\s+", " ", str(e).split("\n")[0])[:120])
    print(json.dumps(out, ensure_ascii=False))
    return out


def _ezproxy_host(lib):
    """ezproxy.myuni.edu for an EZproxy prefix https://ezproxy.myuni.edu/login?url= (a proxied publisher's host ends with it: www-sciencedirect-com.ezproxy.myuni.edu)."""
    return urllib.parse.urlsplit(lib.get("ezproxy") or "").netloc.lower().split(":")[0]


def library_login():
    """Sign in through your university library (set up in Settings → Library access: OpenAthens or EZproxy) and close by itself once a publisher page opens.
    The window starts on a publisher page through the library's address (ScienceDirect, see signins.PROBE): that is what makes OpenAthens or EZproxy ask for your
    university sign-in. A saved session counts as "already signed in" only when the way there went through the library's sign-in and back to the publisher."""
    lib = library()
    if not lib["signs_in"]:
        print("Your library has no sign-in of its own (%s): use “Publisher sites” instead, or set it up in Settings → Library access." % lib["mode"])
        return False
    target = si.probe_url()
    key = _site_key(target)        # "sciencedirect": also found in the proxied address of an EZproxy
    with Browser(visible=True) as b:
        page = b.ctx.pages[0] if b.ctx.pages else b.ctx.new_page()
        chain = _track(page)
        start = via_library(target)
        page.goto(start, wait_until="domcontentloaded", timeout=60_000)
        settle(page, 8)
        try:
            text = page.inner_text("body")[:4000]
        except PWError:
            text = ""
        if OA_NOT_ENABLED.search(text):
            print("Sign-in not possible: OpenAthens says its redirector is not enabled for “%s”. That is not your library's OpenAthens domain: check Settings → Library access." % lib.get("openathens", ""))
            page.wait_for_timeout(4000)
            return False
        here = urllib.parse.urlsplit(page.url).netloc.lower()
        through = (bool(_ezproxy_host(lib)) and here.endswith(_ezproxy_host(lib))) if lib["mode"] == "ezproxy" else bool(_steps(chain, start, page.url))
        if through and not looks_like_login(page.url) and key in urllib.parse.urlsplit(page.url).netloc.lower() and not page.locator("input[type=password]:visible").count():
            page.bring_to_front()           # the saved session still works: the way went through the library's sign-in and there is no password to type
            celebrate(page, "Already signed in to %s" % (lib["name"] or "your library"), "Nothing to do. This window closes by itself.")
            print("Signed in to %s." % (lib["name"] or "the library"))
            return True
        page.bring_to_front()
        print("Sign in with your %s account in the browser window." % (lib["name"] or "library"))
        print("It closes by itself once you are in.")
        deadline, seen_login, picked = time.time() + 1800, False, set()
        while time.time() < deadline:
            try:
                if not b.ctx.pages:
                    break
                cur = b.ctx.pages[-1]
                u = cur.url
                # the routine clicks are done for you; you only type your password
                if "login.openathens.net" in u and lib["chooser_text"] and "chooser" not in picked:
                    opt = cur.get_by_text(lib["chooser_text"], exact=False).first
                    if opt.count():
                        cur.bring_to_front()
                        opt.click()
                        picked.add("chooser")
                elif "login.microsoftonline.com" in u and "account" not in picked:
                    tiles = cur.locator("[data-test-id] >> text=/@/")
                    if tiles.count() == 1:  # exactly one remembered account: pick it
                        tiles.first.click()
                        picked.add("account")
                    elif cur.locator("input[type=password], input[type=email]").count():
                        cur.bring_to_front()
                        picked.add("account")
                if signin_step(u) or u.startswith("about:"):
                    seen_login = seen_login or signin_step(u)
                elif seen_login and key in urllib.parse.urlsplit(u).netloc.lower():
                    cur.wait_for_timeout(2500)           # let the site finish setting its session
                    celebrate(cur, "Signed in to %s" % (lib["name"] or "your library"))
                    print("Signed in to %s." % (lib["name"] or "the library"))
                    return True
                cur.wait_for_timeout(1000)
            except PWError:
                break
    print("Sign-in not completed (window closed, or 30 minutes passed).")
    return False


# member accounts on publisher sites (sign-in pages and how to tell it worked)
MEMBER_SITES = {
    "informs": {"name": "INFORMS (ISR, MS, MSOM, Organization Science)",
                "start": "https://pubsonline.informs.org/action/showLogin?redirectUri=%2F",
                "login_hosts": ("myaccount.informs.org",), "home": "pubsonline.informs.org",
                "remember": None},
    "ais": {"name": "AIS eLibrary (MISQ, JAIS)",
            "start": "https://aisel.aisnet.org/cgi/login.cgi",
            "login_hosts": ("login.cgi",), "home": "aisel.aisnet.org",
            "remember": "#remember"},
}


def member_login(site):
    """Open a member sign-in page; close by itself once the user is back on the site."""
    cfg = MEMBER_SITES[site]
    with Browser(visible=True) as b:
        page = b.ctx.pages[0] if b.ctx.pages else b.ctx.new_page()
        page.goto(cfg["start"], wait_until="domcontentloaded", timeout=60_000)
        page.bring_to_front()
        print("Sign in to %s in the browser window. It closes by itself when you're done." % cfg["name"])
        seen_login, deadline = False, time.time() + 1800
        while time.time() < deadline:
            try:
                if not b.ctx.pages:
                    break
                cur = b.ctx.pages[-1]
                on_login = any(h in cur.url for h in cfg["login_hosts"]) or "showLogin" in cur.url
                if on_login:
                    if not seen_login and cfg["remember"] and cur.locator(cfg["remember"]).count():
                        cur.locator(cfg["remember"]).first.check()  # keep the session
                    seen_login = True
                elif seen_login and cfg["home"] in cur.url:
                    cur.wait_for_timeout(2500)
                    celebrate(cur, "Signed in to %s" % cfg["name"].split(" (")[0])
                    print("Signed in to %s." % cfg["name"])
                    return True
                cur.wait_for_timeout(1000)
            except PWError:
                break
    print("Sign-in not completed (window closed, or 30 minutes passed).")
    return False


def login():
    with Browser(visible=True) as b:
        print("A browser window is open with one tab per publisher:")
        for name, url in login_pages():
            try:
                b.ctx.new_page().goto(url, wait_until="domcontentloaded", timeout=60_000)
            except PWError:
                pass
            print("  -", name)
        for p in b.ctx.pages:
            if p.url == "about:blank" and len(b.ctx.pages) > 1:
                p.close()
        print("\nSign in on each tab you have access to. Sessions are saved in")
        print("state/browser-profile/. Close the browser window when you're done.")
        while b.ctx.pages:
            try:
                b.ctx.pages[0].wait_for_event("close", timeout=0)
            except PWError:
                break
    print("Saved.")


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["login"]:
        login()
    elif a[:1] in (["library-login"], ["nova-login"]):
        library_login()
    elif a[:1] == ["library-test"]:
        library_test()
    elif a[:1] == ["member-login"] and len(a) == 2 and a[1] in MEMBER_SITES:
        member_login(a[1])
    elif a[:1] == ["get"] and len(a) == 3:
        with Browser() as b:
            data, note = b.fetch_pdf(a[1])
        if data:
            open(a[2], "wb").write(data)
            print("OK", a[2], len(data), "bytes")
        else:
            print("FAIL", note)
    else:
        print(__doc__)
