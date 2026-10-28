"""Library access and sign-in status.

Two things the dashboard needs to know that are not about papers:

  * which university library you use and how it lets you in (library.json: "name", "mode", …) — see profile()/save_profile(). Modes:
      openathens     the library uses OpenAthens (give its domain, e.g. myuni.ac.uk)
      ezproxy        the library gives you an EZproxy link (give its login address, e.g. https://ezproxy.myuni.edu/login?url=)
      institutional  nothing to set up: each publisher's "sign in through your institution" page asks for your university (works for most)
      vpn            you are on campus or on the university VPN (access is by network address); nothing to sign in
      none           no institutional access
  * who is signed in right now (state/signins.json): informs, ais, library, publishers, each with the time it last worked — see record()/status().
    A browser session cannot be asked "are you still signed in?" without opening a page, so a sign-in counts as current for as long as the sessions of that kind
    normally last (ttl) and the page says "Signed in 3 h ago" or "Sign in again".
"""
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402

PROFILE_FILE = os.path.join(paths.BASE, "library.json")
STATUS_FILE = os.path.join(paths.STATE, "signins.json")
MODES = ("openathens", "ezproxy", "institutional", "vpn", "none")
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
              "home": str(d.get("home") or d.get("ebsco_home") or "").strip(), "ill_email": str(d.get("ill_email") or "").strip(), "chooser_text": str(d.get("chooser_text") or "").strip()})
    p["configured"] = bool((mode == "openathens" and p["openathens"]) or (mode == "ezproxy" and p["ezproxy"]) or (mode in ("institutional", "vpn") and p["name"]))
    p["signs_in"] = mode in ("openathens", "ezproxy")          # the library has a sign-in of its own (otherwise the publishers' pages do it)
    return p


def save_profile(patch):
    """Validate and keep what the Settings page sends. Unknown keys of the file (EBSCO routes, notes) are left alone."""
    d = _read(PROFILE_FILE, {})
    new = {}
    for k in ("name", "mode", "openathens", "ezproxy", "home", "ill_email", "chooser_text"):
        if k in patch:
            new[k] = str(patch[k] or "").strip()[:300]
    if "mode" in new and new["mode"] not in MODES:
        raise ValueError("Choose how your library lets you in.")
    if new.get("openathens") and not re.fullmatch(r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}|[A-Za-z0-9.-]+", new["openathens"]):
        raise ValueError("The OpenAthens domain looks like myuni.ac.uk (no https:// and no slashes).")
    if new.get("ezproxy"):
        e = new["ezproxy"]
        if not e.startswith("https://") or "." not in urllib.parse.urlsplit(e).netloc:
            raise ValueError("The EZproxy address must start with https:// (for example https://ezproxy.myuni.edu/login?url=).")
        if "{url}" not in e and not e.endswith(("url=", "qurl=", "?url=", "&url=")):
            raise ValueError("The EZproxy address must end with url= (the paper's address is added after it).")
    if new.get("home") and not new["home"].startswith(("https://", "http://")):
        raise ValueError("The library page must be a web address (https://…).")
    if new.get("ill_email") and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", new["ill_email"]):
        raise ValueError("That doesn’t look like an email address.")
    d.update(new)
    for legacy in ("redirector",):
        if d.get("mode") in ("ezproxy", "institutional", "vpn", "none") and legacy in d:
            d.pop(legacy)                                      # an old OpenAthens redirector must not outlive a change of method
    _write(PROFILE_FILE, d)
    return profile()


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
    if ok:
        d[site] = {"ok": True, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    elif site in d:
        d[site]["failed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _write(STATUS_FILE, d)


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
        out[s] = {"signed_in": bool(e.get("ok") and age is not None and age < ttl), "at": at, "age_hours": None if age is None else round(age, 2), "ttl_hours": ttl, "expired": bool(e.get("ok") and age is not None and age >= ttl)}
    out["library"].update({"name": p["name"], "mode": p["mode"], "configured": p["configured"], "signs_in": p["signs_in"], "ill_email": p["ill_email"], "home": p["home"]})
    return out
