"""Updates for the Mac app: "a new version is ready — Update now".

    updater.check()          ask the feed (a small JSON file, see app/make_appcast.py) what the newest version is
    updater.public()         what the page shows: current, latest, notes, progress, whether it can install itself
    updater.install()        download the new app, check it, and swap it for the running one (the app quits and the new one opens)

The feed is the `appcast.json` that `app/release.sh` writes next to each new build:
    {"app": "Journal Watcher", "version": "1.2.0", "released": "2026-10-28", "notes": ["…"], "url": "https://…/Journal-Watcher-1.2.0.zip",
     "sha256": "…", "size": 123456789, "min_macos": "11.0", "history": [{"version": "1.1.0", "released": "…", "notes": ["…"]}]}
Where the feed is: JW_UPDATE_FEED (tests), else your choice in Settings → About & updates (<settings folder>/update.json), else the address built into the app
(defaults/update_feed.txt). Downloads are checked against the feed's sha256, the unpacked app must be a valid signature for the same bundle id and the advertised version,
and only https (or loopback http, for tests) is accepted. Run from the project folder (not the app) it checks and reports but never installs.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402

SETTINGS = os.path.join(paths.BASE, "update.json")
STATE_FILE = os.path.join(paths.STATE, "update_state.json")
SUPPORT = os.environ.get("JW_UPDATE_DIR") or os.path.expanduser("~/Library/Application Support/Journal Watcher/updates")        # downloads and the swap script (JW_UPDATE_DIR: tests)
CHECK_EVERY = 6 * 3600
MAX_FEED = 262144
_lock = threading.RLock()
_progress = {"phase": "idle", "pct": 0, "error": ""}      # idle | downloading | verifying | installing | restarting | revealed | error
EXIT = None                                                  # the app sets this: a function that quits it (so the new one can take its place)


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
        json.dump(d, f, indent=1)
    os.replace(tmp, path)


def vtuple(v):
    p = [int(x) for x in re.findall(r"\d+", str(v))[:4]]
    return tuple(p + [0] * (4 - len(p)))


def newer(a, b):
    return vtuple(a) > vtuple(b)


def version():
    """The version of this app: JW_VERSION (tests), the number built into the app, or app/VERSION of the project."""
    if os.environ.get("JW_VERSION"):
        return os.environ["JW_VERSION"]
    for p in (os.path.join(paths.DEFAULTS, "version.json"), ):
        d = _read(p, {})
        if d.get("version"):
            return str(d["version"])
    try:
        return open(os.path.join(os.path.dirname(paths.DASHBOARD), "app", "VERSION"), encoding="utf-8").read().strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def settings():
    d = _read(SETTINGS, {})
    return {"auto": d.get("auto") is not False, "feed": str(d.get("feed") or ""), "skipped": str(d.get("skipped") or ""), "seen": str(d.get("seen") or "")}


def save_settings(patch):
    s = settings()
    for k in ("auto", "feed", "skipped", "seen"):
        if k in patch:
            s[k] = bool(patch[k]) if k == "auto" else str(patch[k] or "").strip()[:500]
    if s["feed"] and not _ok_url(s["feed"]):
        raise ValueError("The update address must start with https://")
    _write(SETTINGS, s)
    return s


def built_in_feed():
    try:
        return open(os.path.join(paths.DEFAULTS, "update_feed.txt"), encoding="utf-8").read().strip()
    except OSError:
        return ""


def feed_url():
    return os.environ.get("JW_UPDATE_FEED") or settings()["feed"] or built_in_feed()


def _ok_url(u):
    p = urllib.parse.urlsplit(u or "")
    return p.scheme == "https" and bool(p.hostname) or (p.scheme == "http" and p.hostname in ("127.0.0.1", "localhost"))


def target_app():
    """The app that would be replaced: the running Journal Watcher.app (or JW_UPDATE_TARGET, for tests); None in a project run."""
    t = os.environ.get("JW_UPDATE_TARGET")
    if t:
        return t
    if paths.FROZEN:
        p = os.path.abspath(sys.executable)
        while p and p != "/" and not p.endswith(".app"):
            p = os.path.dirname(p)
        return p if p.endswith(".app") else None
    return None


def can_install():
    t = target_app()
    return bool(t and EXIT and os.path.isdir(t) and os.access(os.path.dirname(t), os.W_OK) and os.access(t, os.W_OK))


# ------------------------------------------------------------------------------------------------ asking the feed

def _fetch(url, limit=MAX_FEED, timeout=15):
    if not _ok_url(url):
        raise ValueError("The update address must start with https://")
    req = urllib.request.Request(url, headers={"User-Agent": "JournalWatcher/" + version(), "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise ValueError("The update feed is too large.")
    return data


def _clean_notes(x):
    return [re.sub(r"\s+", " ", str(n)).strip()[:300] for n in (x if isinstance(x, list) else []) if str(n).strip()][:30]


def parse_feed(data):
    d = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
    if not isinstance(d, dict):
        raise ValueError("The update feed is not what was expected.")
    v = str(d.get("version") or "")
    if not re.fullmatch(r"\d+(\.\d+){1,3}", v):
        raise ValueError("The update feed names no valid version.")
    url, sha = str(d.get("url") or ""), str(d.get("sha256") or "").lower()
    if not _ok_url(url) or not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise ValueError("The update feed has no safe download address or checksum.")
    hist = []
    for h in (d.get("history") or [])[:20]:
        if isinstance(h, dict) and re.fullmatch(r"\d+(\.\d+){1,3}", str(h.get("version") or "")):
            hist.append({"version": str(h["version"]), "released": str(h.get("released") or "")[:10], "notes": _clean_notes(h.get("notes"))})
    return {"version": v, "released": str(d.get("released") or "")[:10], "notes": _clean_notes(d.get("notes")), "url": url, "sha256": sha,
            "size": int(d.get("size") or 0), "min_macos": str(d.get("min_macos") or ""), "history": hist}


def load_state():
    return _read(STATE_FILE, {})


def check(quiet=False):
    """Ask the feed; remember what it said. Never raises: a failure is recorded as state['error']."""
    with _lock:
        st = load_state()
        url = feed_url()
        st["checked"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if not url:
            st["error"] = "no feed"
            _write(STATE_FILE, st)
            return public()
        try:
            f = parse_feed(_fetch(url))
            st.update({"latest": f["version"], "released": f["released"], "notes": f["notes"], "url": f["url"], "sha256": f["sha256"], "size": f["size"], "min_macos": f["min_macos"],
                       "history": [h for h in f["history"] if newer(h["version"], version())], "error": ""})
        except (OSError, ValueError, KeyError, TypeError) as e:
            st["error"] = ("Could not reach the update feed: %s" % str(e)[:120]) if not quiet else str(e)[:120]
        _write(STATE_FILE, st)
        return public()


def public():
    st, s, cur = load_state(), settings(), version()
    latest = st.get("latest") or ""
    avail = bool(latest) and newer(latest, cur)
    notes = list(st.get("notes") or [])
    seen = s["seen"]
    just = bool(seen) and seen != cur and not newer(seen, cur)      # first start after an update
    if not seen:
        try:
            save_settings({"seen": cur})
        except (OSError, ValueError):
            pass
    return {"current": cur, "latest": latest, "available": avail, "skipped": avail and latest == s["skipped"], "notes": notes if avail else [], "history": st.get("history") or [] if avail else [],
            "released": st.get("released") or "", "size": st.get("size") or 0, "checked": st.get("checked") or "", "error": "" if (st.get("error") or "") == "no feed" else (st.get("error") or ""),
            "feed": bool(feed_url()), "feed_url": feed_url(), "auto": s["auto"], "can_install": can_install(), "app": bool(target_app()), "progress": dict(_progress), "just_updated": just,
            "seen": seen}


def acknowledge():
    save_settings({"seen": version()})


# ------------------------------------------------------------------------------------------------ installing

def _set(phase, pct=0, error=""):
    _progress.update({"phase": phase, "pct": pct, "error": error})


def _download(url, dest, size, sha):
    h = hashlib.sha256()
    got = 0
    req = urllib.request.Request(url, headers={"User-Agent": "JournalWatcher/" + version()})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or size or 0)
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            got += len(chunk)
            _set("downloading", min(99, int(100 * got / total)) if total else 0)
    if h.hexdigest() != sha:
        raise ValueError("The download is damaged (its checksum does not match). Try again.")


def _plist(app, key):
    r = subprocess.run(["/usr/bin/defaults", "read", os.path.join(app, "Contents", "Info"), key], capture_output=True, text=True)
    return r.stdout.strip()


def _stage(zip_path, want_version, bundle_id):
    d = tempfile.mkdtemp(prefix="stage-", dir=SUPPORT)
    r = subprocess.run(["/usr/bin/ditto", "-x", "-k", zip_path, d], capture_output=True, text=True)
    apps = [n for n in os.listdir(d) if n.endswith(".app")]
    if r.returncode != 0 or len(apps) != 1:
        shutil.rmtree(d, ignore_errors=True)
        raise ValueError("The download is not a Journal Watcher app.")
    app = os.path.join(d, apps[0])
    v = subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", app], capture_output=True, text=True)
    if v.returncode != 0:
        shutil.rmtree(d, ignore_errors=True)
        raise ValueError("The new app's signature is not valid: %s" % v.stderr.strip()[-120:])
    if bundle_id and _plist(app, "CFBundleIdentifier") != bundle_id:
        shutil.rmtree(d, ignore_errors=True)
        raise ValueError("The download is a different app than this one.")
    if _plist(app, "CFBundleShortVersionString") != want_version:
        shutil.rmtree(d, ignore_errors=True)
        raise ValueError("The download is not the version the feed announced.")
    return d, app


HELPER = """#!/bin/bash
# Swaps the new Journal Watcher.app in once the running one has quit, then opens it. Written by updater.py.
PID="$1"; NEW="$2"; TARGET="$3"; STAGE="$4"; LOG="$5"; OPEN="${JW_OPEN:-open}"
exec >>"$LOG" 2>&1
echo "$(date): waiting for $PID to quit"
for i in $(seq 1 300); do kill -0 "$PID" 2>/dev/null || break; sleep 0.2; done
if kill -0 "$PID" 2>/dev/null; then echo "the old app is still running: giving up"; exit 1; fi
BACK="${TARGET}.old-$$"
if ! mv "$TARGET" "$BACK"; then echo "cannot move the old app aside"; $OPEN -n "$TARGET"; exit 1; fi
if ditto "$NEW" "$TARGET"; then
  xattr -cr "$TARGET" 2>/dev/null
  echo "$(date): installed, opening"
  $OPEN -n "$TARGET"
  rm -rf "$BACK" "$STAGE"
else
  echo "copy failed: putting the old app back"
  rm -rf "$TARGET"; mv "$BACK" "$TARGET"; $OPEN -n "$TARGET"; exit 1
fi
"""


def _apply(new_app, stage_dir, target):
    os.makedirs(SUPPORT, exist_ok=True)
    script = os.path.join(SUPPORT, "swap.sh")
    with open(script, "w", encoding="utf-8") as f:
        f.write(HELPER)
    os.chmod(script, 0o755)
    log = open(os.path.join(SUPPORT, "swap.log"), "a")
    subprocess.Popen(["/bin/bash", script, str(os.getpid()), new_app, target, stage_dir, os.path.join(SUPPORT, "swap.log")], stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)


def _run_install():
    try:
        st = load_state()
        if not (st.get("latest") and newer(st["latest"], version())):
            raise ValueError("You already have the newest version.")
        os.makedirs(SUPPORT, exist_ok=True)
        zip_path = os.path.join(SUPPORT, "Journal-Watcher-%s.zip" % st["latest"])
        _set("downloading", 0)
        _download(st["url"], zip_path, st.get("size"), st["sha256"])
        _set("verifying", 100)
        target = target_app()
        bundle_id = _plist(target, "CFBundleIdentifier") if target and os.path.isdir(target) else ""
        try:
            stage, new_app = _stage(zip_path, st["latest"], bundle_id)
        finally:
            try:
                os.remove(zip_path)
            except OSError:
                pass
        if not can_install():
            # not allowed to replace the running app (or a project run): leave the new one where it is easy to find
            dest = os.path.join(os.path.expanduser("~/Downloads"), os.path.basename(new_app))
            shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(new_app, dest, symlinks=True)
            shutil.rmtree(stage, ignore_errors=True)
            subprocess.run(["/usr/bin/open", "-R", dest])
            _set("revealed", 100)
            return
        _set("installing", 100)
        _apply(new_app, stage, target)
        _set("restarting", 100)
        time.sleep(1.2)                       # the page learns that it is restarting
        EXIT()
    except Exception as e:  # noqa: BLE001
        _set("error", 0, (str(e) or "The update failed.")[:200])


def install():
    """Start the update in the background (poll public()['progress']). Returns False if one is already running."""
    with _lock:
        if _progress["phase"] in ("downloading", "verifying", "installing", "restarting"):
            return False
        _set("downloading", 0)
    threading.Thread(target=_run_install, daemon=True, name="update").start()
    return True


# ------------------------------------------------------------------------------------------------ in the background

_started = False


def start_background(first_delay=25):
    """Look for an update a little after start-up and then every six hours (if there is a feed, you did not switch it off, and this is not a test)."""
    global _started
    if _started or os.environ.get("JW_NO_UPDATE_CHECK"):
        return
    _started = True

    def loop():
        time.sleep(first_delay)
        while True:
            try:
                if settings()["auto"] and feed_url():
                    check(quiet=True)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(CHECK_EVERY)
    threading.Thread(target=loop, daemon=True, name="update-check").start()
