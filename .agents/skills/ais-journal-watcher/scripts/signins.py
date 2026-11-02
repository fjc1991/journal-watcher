"""Library access and sign-in status.

Two things the dashboard needs to know that are not about papers:

  * which university library you use and how it lets you in (library.json: "name", "mode", …) — see profile()/save_profile(). Modes:
      openathens     the library uses OpenAthens (give its domain, e.g. myuni.ac.uk)
      ezproxy        the library gives you an EZproxy link (give its login address, e.g. https://ezproxy.myuni.edu/login?url=)
      institutional  nothing to set up: each publisher's "sign in through your institution" page asks for your university (works for most)
      vpn            only on campus or on the university VPN (access is by network address); nothing to sign in
      none           no institutional access
    OpenAthens and EZproxy also cover the campus: a download is first tried as it is (on campus the network address gives access) and only then through the library.
    detect() works the settings out from the library's own web site (the links on its database pages); verify_openathens() asks OpenAthens whether a domain is real.
  * who is signed in right now (state/signins.json): informs, ais, library, publishers, each with the time it last worked — see record()/status().
    A browser session cannot be asked "are you still signed in?" without opening a page, so a sign-in counts as current for as long as the sessions of that kind
    normally last (ttl) and the page says "Signed in 3 h ago" or "Sign in again".
"""
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402

PROFILE_FILE = os.path.join(paths.BASE, "library.json")
STATUS_FILE = os.path.join(paths.STATE, "signins.json")
MODES = ("openathens", "ezproxy", "institutional", "vpn", "none")
PACES = ("careful", "normal", "fast")          # how far apart a publisher's papers are fetched (pipeline.PACES has the seconds)
OA_REDIRECTOR = re.compile(r"go\.openathens\.net/redirector/([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)", re.I)
EBSCO_HOME = re.compile(r"https://research\.ebsco\.com/c/[A-Za-z0-9]+")
# a publisher page that every library's OpenAthens or EZproxy lets you into, used to sign in and to test the access (the library's own web site is NOT a protected
# resource: OpenAthens just passes you through to it, which once made the app report "signed in" without any sign-in having happened). library.json "probe" overrides it.
PROBE = "https://www.sciencedirect.com/"
# how long a sign-in of each kind normally lasts, in hours (informs / ais: "remember me" cookies; the library: OpenAthens sessions are 8 h)
TTL = {"informs": 14 * 24, "ais": 14 * 24, "library": 8, "publishers": 12}
SITES = ("informs", "ais", "library", "publishers")


def _read(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else default
    except (OSError, ValueError):
        return default


def _write(path, d):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


# ------------------------------------------------------------------------------------------------ the library you use

def profile():
    """library.json with the defaults filled in. Files from before this existed (Example University: an OpenAthens redirector) are understood as mode openathens."""
    d = _read(PROFILE_FILE, {})
    mode = d.get("mode")
    if mode not in MODES:
        mode = "openathens" if (d.get("openathens") or d.get("redirector")) else "institutional"
    p = dict(d)
    p.update({"name": str(d.get("name") or "").strip(), "mode": mode, "openathens": str(d.get("openathens") or "").strip(), "ezproxy": str(d.get("ezproxy") or "").strip(),
              "home": str(d.get("home") or "").strip(), "ill_email": str(d.get("ill_email") or "").strip(), "chooser_text": str(d.get("chooser_text") or "").strip()})
    p["pace"] = d.get("pace") if d.get("pace") in PACES else "careful"
    p["ebsco_home"] = str(d.get("ebsco_home") or "").strip()
    p["use_ebsco"] = bool(p["ebsco_home"]) and d.get("use_ebsco", True) is not False
    # "vpn" (network access only) needs nothing typed: on campus or on the VPN the publishers recognise the address. The name is optional for every mode.
    p["configured"] = bool((mode == "openathens" and p["openathens"]) or (mode == "ezproxy" and p["ezproxy"]) or mode == "vpn" or (mode == "institutional" and p["name"]))
    p["signs_in"] = mode in ("openathens", "ezproxy")          # the library has a sign-in of its own (otherwise the publishers' pages do it)
    return p


def clean_openathens(v):
    """What people paste into the OpenAthens box, reduced to the organisation's domain: a redirector link (go.openathens.net/redirector/<domain>?url=…),
    a web address (https://www.myuni.ac.uk/…) or the bare domain. Returns "" for nothing; never guesses beyond taking the host name."""
    v = (v or "").strip()
    if not v:
        return ""
    m = OA_REDIRECTOR.search(v)
    if m:
        return m.group(1).lower().rstrip(".")
    if "://" in v or "/" in v:
        return v            # another web address: refused by the check (taking its host would be a guess: a library's site is not its OpenAthens domain)
    return v.lower().strip(".")


def clean_ezproxy(v):
    """An EZproxy prefix from what people paste: the prefix itself (https://ezproxy.myuni.edu/login?url=) or a whole proxied link (…/login?url=https://publisher/…),
    which is cut after url=. http:// becomes https:// only if nothing else is wrong (the check below still refuses a plain http prefix that was typed as such)."""
    v = (v or "").strip()
    m = re.match(r"(https?://[^\s?#]+/login\?q?url=)", v, re.I)
    return m.group(1) if m else v


def clean_home(v):
    """A library web site typed without https:// (library.myuni.edu) is given it; anything else is left for the check."""
    v = (v or "").strip()
    if v and "://" not in v and re.fullmatch(r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}(/\S*)?", v):
        v = "https://" + v
    return v


def save_profile(patch):
    """Validate and keep what the Settings page sends. Unknown keys of the file (notes, the EBSCO journal list) are left alone.
    What people commonly paste is accepted and tidied first (a whole redirector link in the OpenAthens box, a web site without https://): in 1.2.0 these were refused
    with an error, which is how a working set-up ended up saved as something else."""
    d = _read(PROFILE_FILE, {})
    new = {}
    for k in ("name", "mode", "openathens", "ezproxy", "home", "ill_email", "chooser_text", "ebsco_home"):
        if k in patch:
            new[k] = str(patch[k] or "").strip()[:300]
    if "use_ebsco" in patch:
        new["use_ebsco"] = bool(patch["use_ebsco"])
    if "pace" in patch:
        if patch["pace"] not in PACES:
            raise ValueError("Choose a download pace: careful, normal or fast.")
        new["pace"] = patch["pace"]
    if "openathens" in new:
        new["openathens"] = clean_openathens(new["openathens"])
    if "ezproxy" in new:
        new["ezproxy"] = clean_ezproxy(new["ezproxy"])
    if "home" in new:
        new["home"] = clean_home(new["home"])
    if "mode" in new and new["mode"] not in MODES:
        raise ValueError("Choose how your library lets you in.")
    if new.get("openathens") and (not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)*", new["openathens"]) or new["openathens"].endswith("openathens.net")):
        raise ValueError("The OpenAthens domain looks like myuni.ac.uk. Paste a go.openathens.net/redirector/… link from your library's site, or use “Find my library's settings”.")
    if new.get("ezproxy"):
        e = new["ezproxy"]
        if not e.startswith("https://") or "." not in urllib.parse.urlsplit(e).netloc:
            raise ValueError("The EZproxy address must start with https:// (for example https://ezproxy.myuni.edu/login?url=).")
        if "{url}" not in e and not e.endswith(("url=", "qurl=", "?url=", "&url=")):
            raise ValueError("The EZproxy address must end with url= (the paper's address is added after it).")
    if new.get("home") and not new["home"].startswith(("https://", "http://")):
        raise ValueError("The library page must be a web address (https://…).")
    if new.get("ebsco_home") and not EBSCO_HOME.fullmatch(new["ebsco_home"].rstrip("/")):
        raise ValueError("The EBSCO address looks like https://research.ebsco.com/c/abc123 (your library's EBSCO start page).")
    if new.get("ebsco_home"):
        new["ebsco_home"] = new["ebsco_home"].rstrip("/")
    if new.get("ill_email") and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", new["ill_email"]):
        raise ValueError("That doesn’t look like an email address.")
    d.update(new)
    for legacy in ("redirector",):
        if d.get("mode") in ("ezproxy", "institutional", "vpn", "none") and legacy in d:
            d.pop(legacy)                                      # an old OpenAthens redirector must not outlive a change of method
    _write(PROFILE_FILE, d)
    return profile()


def probe_url():
    """The publisher page used to sign in to the library and to test it (see PROBE)."""
    p = _read(PROFILE_FILE, {}).get("probe")
    return p if isinstance(p, str) and p.startswith("https://") else PROBE


# ------------------------------------------------------------------------------------------------ working the settings out from the library's web site

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
_EZ_PREFIX = re.compile(r"(https://(?:[A-Za-z0-9.-]*(?:ezproxy|ezp|proxy)[A-Za-z0-9.-]*|[A-Za-z0-9.-]+\.idm\.oclc\.org)(?::\d+)?/login\?q?url=)", re.I)
# a publisher's host rewritten by the proxy: www-sciencedirect-com.ezproxy.myuni.edu, www-jstor-org.myuni.idm.oclc.org (the first label carries dashes)
_EZ_HOSTNAME = re.compile(r"https?://[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+\.((?:(?:ezproxy|ezp|proxy)[A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+)+)|(?:[A-Za-z0-9-]+\.idm\.oclc\.org))(?=[/:?#\s\"'<]|$)", re.I)
_OA_IDP = re.compile(r"entityID=(https?(?::|%3A)(?://|%2F%2F)[^&\"'\s<>]+)", re.I)
_EBSCO_ANY = re.compile(r"https://research\.ebsco\.com/c/([A-Za-z0-9]+)")
_GOOD_LINK = re.compile(r"database|a-?to-?z|a-z|e-?resource|electronic|off-?campus|remote|journals|acesso|bases?-de-dados|recursos|ressources|datenbank|bibliotec|access", re.I)


_BIG_PUBLISHER = re.compile(r"sciencedirect|elsevier|jstor|wiley|springer|sage|emerald|taylor|tandf|scopus|web-?of-?science|ebsco|business-source|proquest|oxford|cambridge", re.I)
_NOT_PAGE = re.compile(r"\.(pdf|docx?|xlsx?|pptx?|zip|jpe?g|png|gif|svg|mp4)(\?|$)|/wp-content/|/feed/?$|mailto:", re.I)


_TWO_LEVEL = {"co.uk", "ac.uk", "org.uk", "gov.uk", "com.au", "edu.au", "co.jp", "ac.jp", "co.nz", "com.br", "co.za", "com.cn", "edu.cn", "edu.pt", "com.pt", "edu.es", "ac.kr", "co.kr"}


def _reg_domain(url):
    """The registered domain of an address: library.myuni.example.edu → example.edu, lib.uni.ac.uk → uni.ac.uk (pages of the whole university count as the library's site)."""
    try:
        host = urllib.parse.urlsplit(url).netloc.lower().split(":")[0]
    except ValueError:
        return ""
    parts = host.split(".")
    n = 3 if len(parts) >= 3 and ".".join(parts[-2:]) in _TWO_LEVEL else 2
    return ".".join(parts[-n:])


def _priority(u):
    """Which page to read next: a page about one big publisher's database (that is where libraries put the access link), then any page about one database,
    then the lists and the off-campus page, then the rest."""
    one_db = bool(re.search(r"/(databases?|resources?|e-?resources?|bases-de-dados|recursos)/[^/?#]+/?(\?|$)", u, re.I))
    return (0 if one_db and _BIG_PUBLISHER.search(u) else 1 if one_db else 2 if re.search(r"a-?to-?z|off-?campus|remote|database", u, re.I) else 3, len(u))


def _fetch(url, timeout=15, limit=2_000_000):
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "text/html,*/*;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.geturl(), r.read(limit).decode("utf-8", "replace")


def scan(text):
    """What one page (or one pasted link) shows about how the library lets people in: {openathens: {domain: n}, ezproxy: {prefix: n}, ebsco: {address: n}, idp: {entity: n}}."""
    t = html.unescape(urllib.parse.unquote(text or ""))
    out = {"openathens": {}, "ezproxy": {}, "ebsco": {}, "idp": {}}
    add = lambda k, v: out[k].__setitem__(v, out[k].get(v, 0) + 1)
    for m in OA_REDIRECTOR.finditer(t):
        add("openathens", m.group(1).lower())
    prefixes = set()
    for m in _EZ_PREFIX.finditer(t):
        add("ezproxy", m.group(1))
        prefixes.add(urllib.parse.urlsplit(m.group(1)).netloc.lower())
    for m in _EZ_HOSTNAME.finditer(t):
        host = m.group(1).lower()
        if any(pre == host or pre.endswith("." + host) for pre in prefixes):
            continue                                                # the same proxy, already counted by its prefix
        add("ezproxy", "https://%s/login?url=" % host)
    for m in _EBSCO_ANY.finditer(t):
        add("ebsco", "https://research.ebsco.com/c/%s" % m.group(1))
    for m in _OA_IDP.finditer(t):
        e = m.group(1)
        if "openathens" in e.lower():
            add("idp", e)
    return out


def verify_openathens(domain, fetch_status=None):
    """Ask OpenAthens itself whether `domain` is a customer whose redirector is switched on: a real one answers the probe address with a redirect into the
    publisher's institution login (302), a wrong one with 403 "The redirector is not enabled for the specified customer domain". Returns (True|False|None, detail);
    None when OpenAthens could not be asked (no network): nothing is claimed then."""
    url = "https://go.openathens.net/redirector/%s?url=%s" % (domain, urllib.parse.quote(PROBE, safe=""))
    if fetch_status is None:
        def fetch_status(u):
            class _NoFollow(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *a, **k):
                    return None
            try:
                r = urllib.request.build_opener(_NoFollow).open(urllib.request.Request(u, headers={"User-Agent": _UA}), timeout=15)
                return r.status, r.headers.get("Location") or ""
            except urllib.error.HTTPError as e:
                return e.code, e.headers.get("Location") or ""
    try:
        code, loc = fetch_status(url)
    except Exception as e:  # noqa: BLE001
        return None, "OpenAthens could not be reached (%s)" % str(e)[:80]
    if code in (301, 302, 303, 307, 308) and loc:
        return True, urllib.parse.urlsplit(loc).netloc
    if code == 403:
        return False, "OpenAthens says its redirector is not enabled for “%s”: that is not your library's OpenAthens domain" % domain
    return None, "OpenAthens answered %s" % code


def detect(given, fetch=None, verify=None, max_pages=16, budget_s=45):
    """Work out how a library lets people in from its own web site, the way a person would: look at the links on its pages (the home page, then the pages about
    databases, A–Z lists and off-campus access) for an OpenAthens redirector (go.openathens.net/redirector/<domain>), an EZproxy prefix (…/login?url=) and an EBSCO
    start page. `given` is the library's web address or any link copied from it (a link that already shows the answer is read without opening anything).
    Returns {mode, openathens, ezproxy, ebsco_home, idp, verified, pages: [{url, found}], message}; mode "" when nothing was found. It reports what it saw (how many
    links, on which pages) so the person can check it, and an OpenAthens domain is confirmed with OpenAthens itself (verify_openathens)."""
    fetch = fetch or _fetch
    verify = verify or verify_openathens
    given = (given or "").strip()
    tally = {"openathens": {}, "ezproxy": {}, "ebsco": {}, "idp": {}}
    pages = []

    def merge(found):
        for k, d in found.items():
            for v, n in d.items():
                tally[k][v] = tally[k].get(v, 0) + n

    first = scan(given)
    merge(first)
    start = clean_home(given) if not (first["openathens"] or first["ezproxy"]) else ""
    if start.startswith(("http://", "https://")):
        t0, seen, queue, extra_ebsco = time.time(), set(), [start], 2
        site = _reg_domain(start)
        while queue and len(pages) < max_pages and time.time() - t0 < budget_s:
            queue.sort(key=_priority)                                       # the most telling pages first, wherever they were found
            u = queue.pop(0)
            if u in seen:
                continue
            seen.add(u)
            try:
                final, text = fetch(u)
            except Exception as e:  # noqa: BLE001
                pages.append({"url": u, "found": "", "error": str(e)[:100]})
                continue
            if not pages:
                site = _reg_domain(final)                               # where the start page really is (library.uni.edu may redirect to www.uni.edu/library/)
            found = scan(text)
            merge(found)
            pages.append({"url": final, "found": ", ".join("%s ×%d" % (k, sum(d.values())) for k, d in found.items() if d and k != "idp")})
            if sum(tally["openathens"].values()) >= 3 or sum(tally["ezproxy"].values()) >= 3:
                # a clear answer: enough pages read. Only an EBSCO start page may still be worth one or two more pages (Business Source holds many business journals)
                if tally["ebsco"] or extra_ebsco <= 0:
                    break
                extra_ebsco -= 1
                queue = [x for x in queue if re.search(r"ebsco|business-source", x, re.I)]
                if not queue:
                    break
                continue
            links = []
            for href, label in re.findall(r"<a\b[^>]*href=[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>", text, re.I | re.S):
                try:
                    full = urllib.parse.urljoin(final, html.unescape(href))
                    host = urllib.parse.urlsplit(full).netloc.lower().split(":")[0]
                except ValueError:                                      # a malformed link (http://[broken) must not end the whole search
                    continue
                ours = host == site or host.endswith("." + site) or host.endswith(".libguides.com")     # LibGuides A–Z lists are where many libraries keep their links
                if full.startswith("http") and ours and full not in seen and not _NOT_PAGE.search(full):
                    words = href + " " + re.sub(r"<[^>]+>", " ", label)
                    if _GOOD_LINK.search(words) or _BIG_PUBLISHER.search(words):
                        links.append(full)
            queue += [x for x in dict.fromkeys(links) if x not in queue][:60]
    best = lambda k: max(tally[k].items(), key=lambda kv: kv[1])[0] if tally[k] else ""
    res = {"openathens": best("openathens"), "ezproxy": best("ezproxy"), "ebsco_home": best("ebsco"), "idp": best("idp"), "pages": pages,
           "counts": {k: dict(sorted(d.items(), key=lambda kv: -kv[1])[:5]) for k, d in tally.items()}, "verified": None, "mode": ""}
    oa_n, ez_n = sum(tally["openathens"].values()), sum(tally["ezproxy"].values())
    if oa_n and oa_n >= ez_n:
        res["mode"] = "openathens"
        ok, detail = verify(res["openathens"])
        res["verified"] = ok
        res["message"] = ("OpenAthens, organisation domain %s (seen in %d link%s%s). %s" % (
            res["openathens"], oa_n, "" if oa_n == 1 else "s", (" on %d page%s" % (len(pages), "" if len(pages) == 1 else "s")) if pages else "",
            "OpenAthens confirms it (it sends that domain's users to %s)." % detail if ok else detail + "." if ok is False else "It could not be confirmed with OpenAthens just now (%s)." % detail))
        if len(tally["openathens"]) > 1:
            res["message"] += " Other domains seen: %s." % ", ".join(k for k in tally["openathens"] if k != res["openathens"])
    elif ez_n:
        res["mode"] = "ezproxy"
        res["message"] = "EZproxy, address %s (seen in %d link%s)." % (res["ezproxy"], ez_n, "" if ez_n == 1 else "s")
    else:
        res["message"] = ("No OpenAthens or EZproxy link was found on %d page%s of %s. Your library may give access by network address only (on campus or the VPN), "
                          "or through each publisher's “sign in through your institution” page." % (len(pages), "" if len(pages) == 1 else "s", start or "what you pasted"))
    if res["ebsco_home"]:
        res["message"] += " Your library also has an EBSCO start page (%s)." % res["ebsco_home"]
    return res


def via(url):
    """The address that gets you to `url` through your library (or `url` itself when the library has no address of its own)."""
    p = profile()
    if p["mode"] == "openathens" and p["openathens"]:
        red = p.get("redirector") or "https://go.openathens.net/redirector/%s?url={url}" % p["openathens"]
        if "{url}" not in red:
            red += "{url}"
        return red.replace("{url}", urllib.parse.quote(url, safe=""))
    if p["mode"] == "ezproxy" and p["ezproxy"]:
        e = p["ezproxy"]
        return e.replace("{url}", url) if "{url}" in e else e + url
    return url


# ------------------------------------------------------------------------------------------------ who is signed in

def record(site, ok):
    """Remember that a sign-in of this kind just worked (or did not)."""
    if site not in SITES:
        return
    d = _read(STATUS_FILE, {})
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if ok:
        d[site] = {"ok": True, "at": now}
    else:
        e = d.get(site) or {}
        e["failed_at"] = now                                   # an earlier good sign-in stays; the page learns that an attempt ended
        d[site] = e
    _write(STATUS_FILE, d)


def record_test(res):
    """Remember what the last "Test my access" found (browser.library_test). A library with a sign-in of its own is ticked when the test got through and un-ticked
    when it was asked for a password (that is clear); any other result (a human check, a page that did not open, a page that says nothing) changes no tick."""
    d = _read(STATUS_FILE, {})
    d["library_test"] = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **{k: str(res.get(k) or "")[:300] for k in ("state", "message", "host", "saw", "mode")}}
    if profile()["signs_in"]:
        if res.get("state") == "ok":
            d["library"] = {"ok": True, "at": d["library_test"]["at"]}
        elif res.get("state") == "login":
            d.pop("library", None)
    _write(STATUS_FILE, d)
    return d["library_test"]


def forget(site):
    d = _read(STATUS_FILE, {})
    if d.pop(site, None) is not None:
        _write(STATUS_FILE, d)


def status():
    """{site: {signed_in, at, age_hours, ttl_hours}} for every kind of sign-in, plus the library's own name and whether it is set up."""
    d, now, out = _read(STATUS_FILE, {}), time.time(), {}
    p = profile()
    for s in SITES:
        e = d.get(s) or {}
        at = e.get("at") or ""
        age = None
        if at:
            try:
                age = max(0.0, (now - datetime.fromisoformat(at).timestamp()) / 3600)
            except ValueError:
                age = None
        ttl = TTL[s]
        out[s] = {"signed_in": bool(e.get("ok") and age is not None and age < ttl), "at": at, "failed_at": e.get("failed_at") or "", "age_hours": None if age is None else round(age, 2), "ttl_hours": ttl, "expired": bool(e.get("ok") and age is not None and age >= ttl)}
    lt = d.get("library_test") or {}
    age = None
    try:
        age = round(max(0.0, (now - datetime.fromisoformat(lt["at"]).timestamp()) / 3600), 2) if lt.get("at") else None
    except (KeyError, ValueError):
        age = None
    out["library"]["test"] = {**{k: lt.get(k, "") for k in ("state", "message", "host", "saw", "mode", "at")}, "age_hours": age} if lt.get("state") else None
    out["library"].update({"name": p["name"], "mode": p["mode"], "configured": p["configured"], "signs_in": p["signs_in"], "ill_email": p["ill_email"], "home": p["home"]})
    return out
