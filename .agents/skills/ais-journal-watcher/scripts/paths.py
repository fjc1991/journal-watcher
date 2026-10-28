"""Where things live: the code, and your data.

Run from a checkout of the repository without JW_HOME, the data folder is the checkout itself (Papers/, .env and the settings files there are git-ignored); with JW_HOME it is
that folder instead. Either way a missing settings file is copied from defaults/ on start.
Run as the Mac app (frozen with PyInstaller) the code is inside the app and read-only, and the data is in a folder you chose:
the environment variable JW_HOME names it (the app sets it), and it has the same layout as the project folder:

    <home>/Papers/…                                        the papers and everything you wrote about them
    <home>/.env                                            API keys
    <home>/.agents/skills/ais-journal-watcher/*.json       settings (ai, journals, interests, library, prompts)
    <home>/.agents/skills/ais-journal-watcher/state/       what can be regenerated (caches, indexes, runs, the browser profile)

    BASE   the folder with the settings and state      ROOT    the data home      STATE, PAPERS  inside them
    STATIC the dashboard's html/css/js (always code)    DEFAULTS  the settings a new home starts from
    cmd(script, *args)  the command line that runs one of the scripts in its own process (the app re-runs itself for it)
"""
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
