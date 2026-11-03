"""API keys in this Mac's Keychain, so they need not sit in a file inside a data folder that is synced to other computers (Dropbox).

    keychain.available()          True on a Mac (or when a test names a fake store)
    keychain.get("NAME")          the key, or ""
    keychain.set("NAME", value)   stores it (replaces an older one); raises ValueError for a name or value the Keychain helper cannot take safely
    keychain.delete("NAME")
    keychain.values(names)        {name: key} for the names that have one (cached for a minute: this runs a program per name)

It uses macOS's own `security` command, so nothing is installed. The key travels to `security -i` on its standard input, never on a command line (which any program on the computer can list).
Items are made by `security`, so `security` can read them back without asking you each time. JW_KEYCHAIN names another keychain file (the tests make a temporary one); JW_FAKE_KEYCHAIN
names a JSON file used instead of the real thing (tests on systems that have no Keychain); JW_NO_KEYCHAIN=1 switches the feature off.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

SERVICE = "Journal Watcher"
NAME_RE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
VALUE_RE = re.compile(r"[\x21-\x7e]{1,400}")
_lock = threading.Lock()
_cache = {}                 # name -> (time, value)
TTL = 60.0


def _fake():
    return os.environ.get("JW_FAKE_KEYCHAIN") or ""


def available():
    if os.environ.get("JW_NO_KEYCHAIN"):
        return False
    return bool(_fake()) or (sys.platform == "darwin" and bool(shutil.which("security")))


def _check(name, value=None):
    if not NAME_RE.fullmatch(str(name or "")):
        raise ValueError("That is not a key name the Keychain can hold.")
    if value is not None and (not VALUE_RE.fullmatch(str(value)) or re.search(r"[\"'\\`$;&|<>()#]", str(value))):
        raise ValueError("That key has characters the Keychain helper cannot take safely: keep it in the .env file instead.")


def _kc():
    kc = os.environ.get("JW_KEYCHAIN")
    return [kc] if kc else []


def _load_fake():
    try:
        with open(_fake(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_fake(d):
    tmp = _fake() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, _fake())


def get(name):
    _check(name)
    if _fake():
        return str(_load_fake().get(name) or "")
    if not available():
        return ""
    try:
        r = subprocess.run(["security", "find-generic-password", "-a", name, "-s", SERVICE, "-w"] + _kc(), capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def set(name, value):                                   # noqa: A001  (the module is used as keychain.set)
    _check(name, value)
    if not available():
        raise ValueError("The Keychain is not available here.")
    if _fake():
        d = _load_fake()
        d[name] = value
        _save_fake(d)
    else:
        cmd = 'add-generic-password -a "%s" -s "%s" -w "%s" -U%s\n' % (name, SERVICE, value, (' "%s"' % _kc()[0]) if _kc() else "")
        try:
            r = subprocess.run(["security", "-i"], input=cmd, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as e:
            raise ValueError("The Keychain could not be written: %s" % str(e)[:100])
        if r.returncode != 0 or "error" in (r.stderr or "").lower() or get(name) != value:
            raise ValueError("The Keychain did not accept the key (%s)." % ((r.stderr or r.stdout or "no answer").strip()[:120]))
    with _lock:
        _cache.pop(name, None)


def delete(name):
    _check(name)
    if _fake():
        d = _load_fake()
        d.pop(name, None)
        _save_fake(d)
    elif available():
        subprocess.run(["security", "delete-generic-password", "-a", name, "-s", SERVICE] + _kc(), capture_output=True, text=True, timeout=15)
    with _lock:
        _cache.pop(name, None)


def values(names):
    """{name: key} for the names that have one in the Keychain. Cached for a minute (each lookup starts a program); set() and delete() forget a name at once."""
    out = {}
    if not available():
        return out
    now = time.time()
    for n in names:
        if not NAME_RE.fullmatch(str(n or "")):
            continue
        with _lock:
            hit = _cache.get(n)
        if hit and now - hit[0] < TTL:
            v = hit[1]
        else:
            try:
                v = get(n)
            except ValueError:
                v = ""
            with _lock:
                _cache[n] = (now, v)
        if v:
            out[n] = v
    return out
