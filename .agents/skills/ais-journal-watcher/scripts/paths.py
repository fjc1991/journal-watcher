"""Where things live: the code, and your data.

Run from a checkout of the repository without JW_HOME, the data folder is the checkout itself (Papers/, .env and the settings files there are git-ignored); with JW_HOME it is
that folder instead. Either way a missing settings file is copied from defaults/ on start.
Run as the Mac app (frozen with PyInstaller) the code is inside the app and read-only, and the data is in a folder you chose:
the environment variable JW_HOME names it (the app sets it), and it has the same layout as the project folder:

    <home>/Papers/…                                        the papers and everything you wrote about them
    <home>/.env                                            API keys
    <home>/.agents/skills/ais-journal-watcher/*.json       settings (ai, journals, interests, library, prompts)
    <home>/.agents/skills/ais-journal-watcher/state/       what can be regenerated or is shared (the search index, saved AI answers, runs, backups; the browser profile and model are per computer: see LOCAL below)

    BASE   the folder with the settings and state      ROOT    the data home      STATE, PAPERS  inside them
    STATIC the dashboard's html/css/js (always code)    DEFAULTS  the settings a new home starts from
    cmd(script, *args)  the command line that runs one of the scripts in its own process (the app re-runs itself for it)
"""
import hashlib
import os
import sys

FROZEN = bool(getattr(sys, "frozen", False))
if FROZEN and os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "0") in ("", "0"):
    # Playwright's own default inside a frozen app is "0" = look for Chromium inside the app bundle, which would make the signed app grow and lose it at every update:
    # use the shared cache instead (where `playwright install chromium` puts it, for the project's Python as well)
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.expanduser("~/Library/Caches/ms-playwright")
HERE = os.path.dirname(os.path.abspath(__file__))                       # the scripts (or, frozen, where the bundle keeps its modules)
RES = getattr(sys, "_MEIPASS", None) or os.path.dirname(HERE)           # the bundle's resource folder when frozen
_SKILL = os.path.dirname(HERE)                                          # …/skills/ais-journal-watcher (a project run)

_home = os.environ.get("JW_HOME")
if _home:
    ROOT = os.path.abspath(os.path.expanduser(_home))
    BASE = os.path.join(ROOT, ".agents", "skills", "ais-journal-watcher")
else:
    BASE = _SKILL
    ROOT = os.path.abspath(os.path.join(BASE, "..", "..", ".."))
STATE = os.path.join(BASE, "state")
PAPERS = os.path.join(ROOT, "Papers")


def local_dir_for(root):
    """The per-computer folder (see _local_dir) of the data folder `root`. Pure: it does not depend on the data folder this process uses, so that a change of data folder
    can find the old and the new one."""
    env = os.environ.get("JW_LOCAL")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    if FROZEN:
        h = hashlib.sha1(os.path.realpath(root).encode("utf-8")).hexdigest()[:10]
        return os.path.join(os.path.expanduser("~/Library/Application Support/Journal Watcher/local"), h)
    return os.path.join(root, ".agents", "skills", "ais-journal-watcher", "state") if _home else STATE


def _local_dir():
    """Where the files that belong to THIS computer live. The data folder may be synced between computers (Dropbox): what must not travel (the sign-in browser's profile and
    cookies, the embedding model, the lookup caches, the lock that says a dashboard runs here) lives outside it. JW_LOCAL names the folder (tests, scripts); the Mac app
    uses ~/Library/Application Support/Journal Watcher/local/<one folder per data folder>; a run from a checkout keeps everything in state/ as before."""
    return local_dir_for(ROOT)


LOCAL = _local_dir()
# the entries of state/ that belong to one computer (everything else in state/ is shared with the data folder: run history, what was screened and seen, the AI spending ledger, backups,
# and the two regenerable folders below)
LOCAL_ITEMS = ("browser-profile", "session-cookies.json", "signins.json", "update_state.json", "models", "staging", "fetched",
               "crossref_cache.json", "authors_cache.json", "openalex_cache.json", "cite_cache.json", "personal_vecs.json", "personal_vecs.bin", "pdf_sha1.json")
# regenerable, but worth sharing between the computers that use the data folder: the search index (a paper is read into it once, on whichever computer sees it first) and the saved AI
# answers (an answer paid for once is free everywhere). Their files are named by the content of the paper or of the request, so they mean the same on every computer.
SHARED_CACHES = ("index", "ai_cache")


def migrate_local(say=None):
    """Move per-computer entries that an earlier version left in state/ (inside the data folder) to LOCAL, once; and bring the shared caches back from LOCAL (where a development version
    kept them) into state/. Returns the names moved. An entry that is already in LOCAL stays; one that cannot be moved is left where it is (it works from there)."""
    import shutil
    moved = []
    if os.path.realpath(LOCAL) == os.path.realpath(STATE):
        return moved
    if os.path.isdir(STATE):
        os.makedirs(LOCAL, exist_ok=True)
        for name in LOCAL_ITEMS:
            src, dst = os.path.join(STATE, name), os.path.join(LOCAL, name)
            if os.path.lexists(src) and not os.path.lexists(dst):
                try:
                    shutil.move(src, dst)
                    moved.append(name)
                except OSError as e:
                    if say:
                        say("Could not move %s out of the data folder: %s" % (name, e))
    for name in SHARED_CACHES:
        src, dst = os.path.join(LOCAL, name), os.path.join(STATE, name)
        if not os.path.isdir(src) or os.path.islink(src):
            continue
        try:
            os.makedirs(dst, exist_ok=True)
            n = 0
            for fn in os.listdir(src):
                a, b = os.path.join(src, fn), os.path.join(dst, fn)
                if not os.path.isfile(a):
                    continue
                if os.path.exists(b):
                    os.remove(a)                                   # (the shared one stays; this copy is only a duplicate of something regenerable)
                else:
                    shutil.move(a, b); n += 1
            if n:
                moved.append(name)
            try:
                os.rmdir(src)
            except OSError:
                pass
        except OSError as e:
            if say:
                say("Could not move %s into the data folder: %s" % (name, e))
    return moved

if FROZEN:
    DASHBOARD = os.path.join(RES, "dashboard")
    DEFAULTS = os.path.join(RES, "defaults")
    SCRIPTS = HERE
else:
    DASHBOARD = os.path.join(os.path.abspath(os.path.join(_SKILL, "..", "..", "..")), "dashboard")
    DEFAULTS = os.path.join(_SKILL, "defaults")                         # the generic starting settings in the repository; yours are copied from them
    SCRIPTS = HERE
STATIC = os.path.join(DASHBOARD, "static")

SETTINGS_FILES = ("ai.json", "journals.json", "interests.json", "library.json", "prompts.json")


def cmd(script, *args):
    """The command line for running scripts/<script>.py (or `-m <module>` with script="-m:module") in a separate process."""
    mod = script[3:] if script.startswith("-m:") else os.path.splitext(script)[0]
    if FROZEN:                                                          # the app is not a Python: it runs the module itself (see app/jw_app.py)
        return [sys.executable, "--jw-module", mod, *args]
    return [sys.executable, "-m", mod, *args] if script.startswith("-m:") else [sys.executable, os.path.join(SCRIPTS, script), *args]


def seed_home():
    """Make a new data home usable: the folders, and a copy of each settings file the app ships with (never over a file that is there)."""
    import shutil
    os.makedirs(PAPERS, exist_ok=True)
    os.makedirs(STATE, exist_ok=True)
    os.makedirs(LOCAL, exist_ok=True)
    migrate_local()
    if os.path.abspath(DEFAULTS) == os.path.abspath(BASE):
        return
    os.makedirs(BASE, exist_ok=True)
    for name in SETTINGS_FILES:
        src, dst = os.path.join(DEFAULTS, name), os.path.join(BASE, name)
        if os.path.exists(src) and not os.path.exists(dst):
            shutil.copy2(src, dst)


_BROWSER_OK = False


def ensure_browser(say=print):
    """Playwright's Chromium (sign-ins and PDF downloads) is fetched the first time it is needed, once per run of the app:
    `playwright install` does nothing when the right build is already in the shared cache (~/Library/Caches/ms-playwright)."""
    global _BROWSER_OK
    if _BROWSER_OK:
        return True
    import subprocess
    say("Checking the browser used for sign-ins and downloads (the first time it is fetched: about 150 MB)…")
    try:
        r = subprocess.run(cmd("-m:playwright", "install", "chromium"), capture_output=True, text=True, timeout=1800)
    except (OSError, subprocess.SubprocessError) as e:
        say("Could not install the browser: %s" % e)
        return False
    if r.returncode != 0:
        say("Could not install the browser: %s" % (r.stderr or r.stdout)[-300:])
        return False
    _BROWSER_OK = True
    return True
