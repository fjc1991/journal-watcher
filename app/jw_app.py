"""Journal Watcher as a Mac app: a window (the system's WebKit, through pywebview) around the dashboard, whose server runs in this same process.

    python app/jw_app.py                      from the project folder (needs pywebview), or the built Journal Watcher.app

Where your data lives: a folder you choose the first time (remembered in ~/Library/Application Support/Journal Watcher/app.json);
it has the same layout as the project folder (see scripts/paths.py). JW_HOME in the environment overrides the remembered choice.

The app also runs itself as a helper: `Journal Watcher --jw-module fetch_new` is how the dashboard starts the weekly check,
the publisher sign-in windows and the browser download in their own processes (a frozen app has no separate Python to call).
JW_APP_TEST=<file> runs a short self-test of the finished window and writes what it found to <file> (used by build_app.sh).
"""
import os
import sys

if len(sys.argv) > 2 and sys.argv[1] == "--jw-module":          # helper process: run one module the way `python -m` would, then leave
    import runpy
    _mod = sys.argv[2]
    sys.argv = [_mod] + sys.argv[3:]
    runpy.run_module(_mod, run_name="__main__", alter_sys=True)
    raise SystemExit(0)

import base64
import json
import re
import subprocess
import threading
import time
import traceback
import urllib.parse
import urllib.request
import webbrowser

import webview
from webview.menu import Menu, MenuAction, MenuSeparator

NAME = "Journal Watcher"
PORT = 8790                                    # the same address every time: the page's own settings (theme, panel sizes, reading places) are kept per address
SUPPORT = os.path.expanduser("~/Library/Application Support/" + NAME)
CONFIG = os.path.join(SUPPORT, "app.json")
FROZEN = bool(getattr(sys, "frozen", False))
RES = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))     # the bundle, or the project folder
EXTERNAL_HOME = os.environ.get("JW_HOME")       # a folder named by whoever started the app (a test, a script) overrides the saved choice. launch() also puts the chosen folder in
#                                                 JW_HOME for the dashboard modules, so this is read once, here, and boot() uses it rather than the environment.
DOCS_HOME = os.path.join(os.path.expanduser("~/Documents"), NAME)       # where earlier versions started fresh, and the folder a first start looks for


def code_on_path():
    """The dashboard and the scripts importable (idempotent): inside the bundle they are the app's own modules; from a checkout they are two folders."""
    for d in (os.path.join(RES, "dashboard"), os.path.join(RES, ".agents", "skills", "ais-journal-watcher", "scripts")):
        if os.path.isdir(d) and d not in sys.path:
            sys.path.insert(0, d)


def bundle_path():
    """…/Journal Watcher.app when frozen, else None."""
    p = os.path.abspath(sys.executable)
    while p and p != "/" and not p.endswith(".app"):
        p = os.path.dirname(p)
    return p if p.endswith(".app") else None


def load_config():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(d):
    os.makedirs(SUPPORT, exist_ok=True)
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1)


def suggested_home():
    """The project folder this app was built from, if it is still there: the natural place for the data you already have."""
    try:
        p = open(os.path.join(RES, "defaults", "suggested_home.txt"), encoding="utf-8").read().strip()
    except OSError:
        return None
    return p if p and os.path.isdir(os.path.join(p, "Papers")) else None


def is_home(p):
    return bool(p) and os.path.isdir(p)


def has_data(p):
    """Does `p` already hold Journal Watcher data (papers or settings)?"""
    code_on_path()
    import movehome
    return movehome.looks_like_home(p)


def detected_home():
    """On a first start (nothing saved yet): the folder an earlier installation used, if there is one: the folder this build was made for, else ~/Documents/Journal Watcher."""
    s = suggested_home()
    if s:
        return s
    return DOCS_HOME if has_data(DOCS_HOME) else None


def esc(x):
    return str(x or "").replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")


# ------------------------------------------------------------------------------------------------ pages shown by the app itself

STYLE = """<style>
:root { color-scheme: light dark; --bg: #fbfaf7; --ink: #201e1b; --ink-3: #6a655d; --line: #e6e2d9; --panel: #fff; --btn: #201e1b; --btn-ink: #fff; }
@media (prefers-color-scheme: dark) { :root { --bg: #26272b; --ink: #edece8; --ink-3: #b0ada6; --line: #404148; --panel: #2f3035; --btn: #efede8; --btn-ink: #201e1b; } }
* { box-sizing: border-box; } html, body { margin: 0; height: 100%; background: var(--bg); color: var(--ink); font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; -webkit-font-smoothing: antialiased; }
main { max-width: 560px; margin: 0 auto; padding: 12vh 24px 40px; }
.mark { width: 44px; height: 44px; border-radius: 12px; background: var(--btn); color: var(--btn-ink); display: grid; place-items: center; margin-bottom: 22px; }
h1 { font-size: 30px; line-height: 1.2; letter-spacing: -.015em; margin: 0 0 8px; } p { color: var(--ink-3); margin: 0 0 22px; }
.opt { display: block; width: 100%; text-align: left; background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 14px 16px; margin: 0 0 10px; cursor: pointer; font: inherit; color: var(--ink); }
.opt:hover { border-color: var(--ink-3); } .opt b { display: block; } .opt span { color: var(--ink-3); font-size: 13px; word-break: break-all; }
.spin { width: 22px; height: 22px; border: 3px solid var(--line); border-top-color: var(--ink); border-radius: 50%; animation: s 1s linear infinite; } @keyframes s { to { transform: rotate(360deg); } }
.err { color: #b3261e; white-space: pre-wrap; font-size: 13px; }
</style>"""
MARK = '<div class="mark"><svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linejoin="round"><path d="M5 4h3.5v16H5zM10.5 4H14v16h-3.5zM15.6 5.4l3.3-.9 3 14.2-3.3.9z"/></svg></div>'
SPLASH = "<!doctype html><meta charset=utf-8><title>%s</title>%s<main>%s<h1>Opening %s…</h1><p>Starting up. This takes a few seconds.</p><div class=spin></div></main>" % (NAME, STYLE, MARK, NAME)


def chooser_page(found, note=""):
    """The first start. `found`: a folder an earlier installation used (detected_home), offered as it is or moved somewhere else; None: start fresh or choose."""
    opts = []
    if found:
        opts.append('<button class="opt" data-k="existing"><b>Keep using my existing folder</b><span>%s: your papers, notes and folders are already there</span></button>' % esc(found))
        opts.append('<button class="opt" data-k="move"><b>Move it to another folder…</b><span>Copy everything from %s to a folder you choose (a Dropbox or iCloud folder, say) and use that from now on. The old folder is left as it is.</span></button>' % esc(found))
    if not found or os.path.realpath(found) != os.path.realpath(DOCS_HOME):
        opts.append('<button class="opt" data-k="new"><b>Start fresh in my Documents folder</b><span>%s</span></button>' % esc(DOCS_HOME))
    opts.append('<button class="opt" data-k="pick"><b>Choose another folder…</b><span>Anywhere you like: a Dropbox or iCloud folder works too</span></button>')
    head = "Journal Watcher found the folder an earlier version used" if found else "Where should Journal Watcher keep your papers?"
    return ("<!doctype html><meta charset=utf-8><title>%s</title>%s<main>%s<h1>%s</h1>"
            "<p>Your PDFs, highlights, notes, folders and settings live in one ordinary folder that you own. You can back it up or move it like any other folder.</p>%s<div class=err id=e>%s</div></main>"
            "<script>document.querySelectorAll('.opt').forEach(b => b.onclick = async () => { document.getElementById('e').textContent = ''; const r = await pywebview.api.use_home(b.dataset.k); if (r && r.error) document.getElementById('e').textContent = r.error; });</script>"
            % (NAME, STYLE, MARK, head, "".join(opts), esc(note)))


def move_page(src, dst, dst_has_data):
    """Change data folder: take the data along, or just switch. Nothing is ever removed from `src` (the author's rule: copy only, and say what was copied)."""
    what = ("<p>%s already holds Journal Watcher data (from another computer, perhaps). Your papers, notes, folders and settings from %s can be added to it: "
            "what is already there is kept as it is, and the two library lists are joined so that no paper is lost.</p>" % (esc(dst), esc(src))) if dst_has_data else \
           ("<p>Your papers, notes, folders, settings and API keys can be copied from %s to %s first, so that you find everything where you left it.</p>" % (esc(src), esc(dst)))
    copy_b = "Add my data to it" if dst_has_data else "Copy my data there, then switch"
    switch_s = "Journal Watcher opens on what is already in %s" % esc(dst) if dst_has_data else "Journal Watcher starts empty in %s" % esc(dst)
    return ("<!doctype html><meta charset=utf-8><title>%s</title>%s<main>%s<h1>Use %s from now on?</h1>%s"
            '<button class="opt" data-k="copy"><b>%s</b><span>Nothing is removed from %s: it stays as a copy until you delete it yourself</span></button>'
            '<button class="opt" data-k="switch"><b>Switch without copying</b><span>%s; %s is left as it is</span></button>'
            '<button class="opt" data-k="cancel"><b>Cancel</b><span>Keep using %s</span></button><div class=err id=e></div></main>'
            "<script>document.querySelectorAll('.opt').forEach(b => b.onclick = async () => { document.getElementById('e').textContent = ''; document.querySelectorAll('.opt').forEach(x => x.disabled = true); "
            "const r = await pywebview.api.move_choice(b.dataset.k); if (r && r.error) { document.getElementById('e').textContent = r.error; document.querySelectorAll('.opt').forEach(x => x.disabled = false); } });</script>"
            % (NAME, STYLE, MARK, esc(dst), what, copy_b, esc(src), switch_s, esc(src), esc(src)))


def progress_page(src, dst):
    return ("<!doctype html><meta charset=utf-8><title>%s</title>%s<main>%s<h1>Copying your data to %s…</h1><p>From %s. Leave this window open; large PDF collections take a while.</p>"
            "<div class=spin></div><p id=p style='margin-top:16px'></p></main><script>window.JWMove = { progress(d, t, rel) { document.getElementById('p').textContent = d + ' of ' + t + ' files · ' + rel; } };</script>"
            % (NAME, STYLE, MARK, esc(dst), esc(src)))


def done_page(dst, text, errors):
    errs = ("<details><summary class=err>Could not copy %d file%s</summary><div class=err>%s</div></details>" % (len(errors), "" if len(errors) == 1 else "s",
            "\n".join("%s: %s" % (esc(a), esc(b)) for a, b in errors[:40]))) if errors else ""
    return ("<!doctype html><meta charset=utf-8><title>%s</title>%s<main>%s<h1>Copied</h1><p>%s</p>%s"
            '<button class="opt" data-k="open"><b>Open Journal Watcher in %s</b><span>From now on this is your data folder</span></button>'
            '<button class="opt" data-k="cancel"><b>Not yet: keep using the old folder</b><span>The copy stays in %s; you can switch to it later</span></button><div class=err id=e></div></main>'
            "<script>document.querySelectorAll('.opt').forEach(b => b.onclick = async () => { const r = await pywebview.api.move_choice(b.dataset.k); if (r && r.error) document.getElementById('e').textContent = r.error; });</script>"
            % (NAME, STYLE, MARK, esc(text), errs, esc(dst), esc(dst)))


def busy_page(other, healthy):
    """Another Journal Watcher already holds this data folder (server.take_lock): say which, and what to do. Until 1.2.0 the app silently showed that other copy,
    which (an old copy started from Terminal whose files had moved) answered with a bare {"error": "not found"}."""
    other = other or {}
    esc = lambda x: str(x or "").replace("&", "&amp;").replace("<", "&lt;")
    by = {"source": "a copy started from the source code (the “Journal Watcher.command” Terminal window)", "app": "another Journal Watcher app"}.get(other.get("by"), "another copy of Journal Watcher")
    where = (" in %s" % esc(other.get("code"))) if other.get("code") else ""
    when = (", started %s UTC" % esc(str(other.get("started", "")).replace("T", " ")[:16])) if other.get("started") else ""
    ver = (" (version %s)" % esc(other.get("version"))) if other.get("version") else ""
    if healthy:
        what = "<p>It is running%s%s%s at %s. Two copies at once would overwrite each other's notes and folders, so this window will not start a second one.</p>" % (ver, where, when, esc(other.get("url")))
        use = '<button class="opt" data-k="use"><b>Show that copy in this window</b><span>Nothing is lost: it is the same folder</span></button>'
    else:
        what = ("<p>It is not answering properly%s%s%s (it may be an old copy whose files were moved or deleted). Close it, then press Try again. "
                "If it was started from Terminal, close that Terminal window.</p>" % (ver, where, when))
        use = ""
    return ("<!doctype html><meta charset=utf-8><title>%s</title>%s<main>%s<h1>Journal Watcher is already open for this folder</h1><p>It is held by %s.</p>%s%s"
            '<button class="opt" data-k="retry"><b>Try again</b><span>After closing the other copy</span></button>'
            '<button class="opt" data-k="pick"><b>Use another folder…</b><span>Start with a different data folder</span></button><div class=err id=e></div></main>'
            "<script>document.querySelectorAll('.opt').forEach(b => b.onclick = async () => { const r = await pywebview.api.busy_choice(b.dataset.k); if (r && r.error) document.getElementById('e').textContent = r.error; });</script>"
            % (NAME, STYLE, MARK, by, what, use))


def answers(url):
    """Does a dashboard at `url` serve its page (not just its API)? The copy that held the folder in the case above answered /api/config but not /."""
    try:
        with urllib.request.urlopen(url + "/", timeout=3) as r:
            return r.status == 200 and "html" in (r.headers.get("Content-Type") or "")
    except Exception:  # noqa: BLE001
        return False


def error_page(msg):
    return "<!doctype html><meta charset=utf-8><title>%s</title>%s<main>%s<h1>Journal Watcher could not start</h1><p>Something went wrong while starting up. If it keeps happening, tell whoever maintains the app what is written below.</p><div class=err>%s</div></main>" % (
        NAME, STYLE, MARK, (msg or "").replace("<", "&lt;"))


# ------------------------------------------------------------------------------------------------ what the page can ask of the app

class Api:
    """Public methods are callable from the page as window.pywebview.api.<name>(…) (see static/appbridge.js)."""

    def __init__(self):
        self.home = None
        self.url = None
        self.move = None                                # a change of data folder under way: {"src", "dst", "busy"}

    # -- choosing the data folder
    def _pick_folder(self, start=None):
        r = webview.windows[0].create_file_dialog(webview.FileDialog.FOLDER, directory=start or os.path.expanduser("~/Documents"))
        return r[0] if r else None

    def use_home(self, kind):
        """The first start (chooser_page): keep the folder that was found, move it, start fresh, or choose."""
        try:
            found = detected_home()
            if kind == "existing":
                p = found
            elif kind == "new":
                p = DOCS_HOME
            elif kind in ("move", "pick"):
                p = self._pick_folder()
                if not p:
                    return {"ok": False}
                if found:                                   # there is data to take along: ask, as Change data folder… does
                    return self._offer(found, p)
            else:
                return {"error": "Unknown choice."}
            if not p:
                return {"error": "That folder is not available."}
            os.makedirs(p, exist_ok=True)
            save_config({"home": p})
            threading.Thread(target=launch, args=(webview.windows[0], p), daemon=True).start()
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)[:200]}

    def _offer(self, src, dst):
        """`dst` was chosen while `src` holds data: show move_page (copy, switch, cancel). With no source to copy from, just switch."""
        if not src or not os.path.isdir(src) or os.path.realpath(src) == os.path.realpath(dst):
            if src and os.path.realpath(src) == os.path.realpath(dst):
                return {"error": "That is the folder you already use."}
            return self._finish(dst)
        code_on_path()
        import movehome
        why = movehome.check(src, dst)
        if why:
            return {"error": why}
        self.move = {"src": src, "dst": dst, "busy": False}
        webview.windows[0].load_html(move_page(src, dst, movehome.looks_like_home(dst)))
        return {"ok": True}

    def move_choice(self, kind):
        """The buttons of move_page and done_page."""
        m = self.move
        w = webview.windows[0]
        if not m:
            return {"error": "Nothing to do: choose a folder first."}
        if m["busy"]:
            return {"error": "Still copying."}
        if kind == "cancel":
            self.move = None
            if self.url:
                w.load_url(self.url)
            else:
                w.load_html(chooser_page(detected_home()))
            return {"ok": True}
        if kind in ("switch", "open"):
            return self._finish(m["dst"], m["src"])
        if kind == "copy":
            m["busy"] = True
            w.load_html(progress_page(m["src"], m["dst"]))
            threading.Thread(target=self._copy, args=(w, m), daemon=True).start()
            return {"ok": True}
        return {"error": "Unknown choice."}

    def _copy(self, w, m):
        code_on_path()
        import movehome
        try:
            res = movehome.copy_into(m["src"], m["dst"], say=lambda d, t, rel: w.evaluate_js("window.JWMove && JWMove.progress(%d, %d, %s)" % (d, t, json.dumps(rel))))
            text, errors = movehome.describe(res, m["src"]), res["errors"]
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            text, errors = "The copy did not finish: %s. Nothing was changed in %s; the destination may hold a partial copy." % (str(e)[:300], m["src"]), []
        m["busy"] = False
        w.load_html(done_page(m["dst"], text, errors))

    def _finish(self, dst, src=None):
        """Use `dst` from now on: remember it and start again as a fresh process (the running dashboard, its lock and its imported modules all belong to the old folder).
        src: the folder this computer used until now; its sign-in browser and language model come along (movehome.carry_local), so there is no new sign-in or download."""
        os.makedirs(dst, exist_ok=True)
        if src:
            try:
                code_on_path()
                import movehome
                r = movehome.carry_local(src, dst)
                if r["errors"]:
                    print("Could not bring along: %s" % r["errors"])
            except Exception:  # noqa: BLE001      (a sign-in again is the worst that happens)
                traceback.print_exc()
        save_config({"home": dst})
        self.move = None
        relaunch()
        return {"ok": True}

    def busy_choice(self, kind):
        """The buttons of busy_page: show the other copy, try again, or choose another folder."""
        w = webview.windows[0]
        if kind == "use" and BUSY.get("url") and answers(BUSY["url"]):
            API.url = BUSY["url"]
            w.load_url(BUSY["url"])
            return {"ok": True}
        if kind == "retry":
            if not BUSY.get("home"):
                return {"error": "Nothing to try again."}
            relaunch(keep_home=True)                               # a fresh process: the lock and the imported server start clean (on the same folder, however it was named)
            return {"ok": True}
        if kind == "pick":
            return self.change_home()
        return {"error": "That copy is not answering any more. Press Try again."}

    def change_home(self):
        """Data → Change data folder…, and the button in Settings → About & updates: choose a folder, then (when there is data here) copy it along, or just switch."""
        try:
            p = self._pick_folder(self.home)
            if not p:
                return {"ok": False}
            return self._offer(self.home, p)                 # from busy_page (no home yet) there is nothing to copy from: just switch
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            return {"error": str(e)[:200]}

    # -- saving what the page makes (downloads)
    def _ask_save(self, name):
        forced = os.environ.get("JW_APP_TEST_SAVE")          # the self-test cannot click a save panel
        if forced:
            return forced
        w = webview.active_window() or webview.windows[0]
        r = w.create_file_dialog(webview.FileDialog.SAVE, directory=os.path.expanduser("~/Downloads"), save_filename=name)
        if not r:
            return None
        return r if isinstance(r, str) else r[0]

    def save_url(self, url, name=""):
        u = urllib.parse.urlsplit(url)
        if u.hostname not in ("127.0.0.1", "localhost") or u.scheme != "http":
            return {"error": "Only files from this app can be saved."}
        try:
            with urllib.request.urlopen(url, timeout=600) as resp:
                disp = resp.headers.get("Content-Disposition") or ""
                m = re.search(r"filename\*=UTF-8''([^;]+)|filename=\"?([^\";]+)\"?", disp)
                fname = urllib.parse.unquote(m.group(1) or m.group(2)) if m else ""
                fname = re.sub(r"[/\\:\x00-\x1f]", "_", fname or name or os.path.basename(u.path) or "download")
                dest = self._ask_save(fname)
                if not dest:
                    return {"ok": False}
                with open(dest, "wb") as f:
                    while True:
                        chunk = resp.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
            return {"ok": True, "name": os.path.basename(dest), "folder": os.path.dirname(dest)}
        except Exception as e:  # noqa: BLE001
            return {"error": "Could not save the file: %s" % str(e)[:160]}

    def save_data(self, name, b64):
        try:
            dest = self._ask_save(re.sub(r"[/\\:\x00-\x1f]", "_", name or "download"))
            if not dest:
                return {"ok": False}
            with open(dest, "wb") as f:
                f.write(base64.b64decode(b64 or ""))
            return {"ok": True, "name": os.path.basename(dest), "folder": os.path.dirname(dest)}
        except Exception as e:  # noqa: BLE001
            return {"error": "Could not save the file: %s" % str(e)[:160]}

    # -- windows and links
    def open_external(self, url):
        if urllib.parse.urlsplit(url).scheme in ("http", "https", "mailto"):
            webbrowser.open(url)
        return True

    def open_beside(self, url, features=""):
        """A second window on this app (the paper “beside” the one you are reading); size and place come from the window.open features."""
        f = {k: int(float(v)) for k, v in re.findall(r"(width|height|left|top)=(-?[\d.]+)", features or "")}
        webview.create_window(NAME, url, js_api=self, text_select=True, width=f.get("width", 700), height=f.get("height", 860), x=f.get("left"), y=f.get("top"), min_size=(480, 400))
        return True

    def reveal_home(self):
        if self.home:
            subprocess.run(["/usr/bin/open", self.home])
        return True

    def open_in_browser(self):
        if self.url:
            webbrowser.open(self.url)
        return True

    def show_welcome(self):
        webview.windows[0].evaluate_js("window.Welcome && Welcome.open(0)")
        return True

    def check_updates(self):
        webview.windows[0].evaluate_js("window.JWUpdate && JWUpdate.show(true)")
        return True

    def version(self):
        return {"app": NAME, "frozen": FROZEN, "home": self.home, "python": sys.version.split()[0]}


API = Api()
BUSY = {}                                      # set when another copy holds the data folder (busy_page): {home, url}


def relaunch(keep_home=False):
    """Start this app again as a fresh process, then leave. The new copy must read the saved folder (app.json), so JW_HOME, which launch() put in this process's
    environment for the dashboard modules, is taken out of what the new process inherits: `open` and execv both hand the environment on, and until 1.2.4 the new copy
    therefore went straight back to the old folder whatever had just been saved (Change data folder… seemed to do nothing, or ran into the old copy's lock).
    keep_home: the folder is to stay as it is, including one named from outside (busy_page's Try again)."""
    env = {k: v for k, v in os.environ.items() if k != "JW_HOME" or (keep_home and EXTERNAL_HOME)}
    b = bundle_path()
    if b:
        subprocess.Popen(["/usr/bin/open", "-n", b], env=env)
        os._exit(0)
    os.execve(sys.executable, [sys.executable] + sys.argv, env)       # run from the project folder: start this same script again


# ------------------------------------------------------------------------------------------------ start-up

def wait_for(url, seconds=60):
    end = time.time() + seconds
    while time.time() < end:
        try:
            urllib.request.urlopen(url + "/api/config", timeout=2).read()
            return True
        except Exception:  # noqa: BLE001
            time.sleep(0.25)
    return False


def launch(window, home):
    """Point the app at a data folder, start the dashboard server in this process and show it."""
    try:
        os.environ["JW_HOME"] = home
        os.makedirs(home, exist_ok=True)
        os.chdir(home)
        if FROZEN:                                    # certificates for the web calls (Crossref, OpenAlex, the AI providers) from the bundle
            try:
                import certifi
                os.environ.setdefault("SSL_CERT_FILE", certifi.where())
            except ImportError:
                pass
        code_on_path()
        for m in ("paths", "download", "syncinfo", "movehome"):
            sys.modules.pop(m, None)                   # a copy made on the first start imported these before the folder was chosen: paths.py must read JW_HOME afresh
        import server                                  # noqa: E402  (after JW_HOME is set: paths.py reads it when it is first imported)
        import updater                                 # noqa: E402
        updater.EXIT = lambda: os._exit(0)             # "Update now": the new app is swapped in once this process has gone
        srv, url = server.start(PORT, quiet=True)
        if srv is None:                                # another copy holds this folder: say so (never show it silently: it may be another version, or broken)
            other = {}
            try:
                with open(os.path.join(server.paths.LOCAL, "server.lock"), encoding="utf-8") as f:
                    other = json.load(f)
            except (OSError, ValueError):
                pass
            BUSY.update(home=home, url=url or other.get("url"))
            window.load_html(busy_page({**other, "url": url or other.get("url")}, bool(url) and answers(url)))
            return
        threading.Thread(target=srv.serve_forever, daemon=True, name="dashboard-server").start()
        if not url or not wait_for(url):
            raise RuntimeError("The dashboard did not answer at %s." % url)
        API.home, API.url = home, url
        window.load_url(url)
        if os.environ.get("JW_APP_TEST"):
            threading.Thread(target=selftest, args=(window, url), daemon=True).start()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        window.load_html(error_page(traceback.format_exc()[-1500:]))


def boot(window):
    home = EXTERNAL_HOME                             # named by whoever started the app (a test, a script): used as it is, made if it is not there yet
    if not home:
        home = load_config().get("home")
        if not is_home(home):
            window.load_html(chooser_page(detected_home()))     # nothing saved (a first start, or the folder is gone): keep or move the folder an earlier version used, or choose
            return
    launch(window, home)


def snap(window, path):
    """A PNG of this app's own window (Quartz); a failure is only noted, never fatal."""
    try:
        import AppKit
        import Quartz
        img = Quartz.CGWindowListCreateImage(Quartz.CGRectNull, Quartz.kCGWindowListOptionIncludingWindow, window.native.windowNumber(), Quartz.kCGWindowImageBoundsIgnoreFraming)
        rep = AppKit.NSBitmapImageRep.alloc().initWithCGImage_(img)
        rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, None).writeToFile_atomically_(path, True)
    except Exception:  # noqa: BLE001
        traceback.print_exc()


def selftest(window, url):
    """What the finished window really does; written to the file named by JW_APP_TEST as JSON."""
    out = {"checks": {}, "errors": []}
    try:
        for _ in range(120):
            if window.evaluate_js("document.readyState === 'complete' && !!document.querySelector('#libitem') && !document.querySelector('#palettebtn').disabled"):
                break
            time.sleep(0.5)
        js = window.evaluate_js
        c = out["checks"]
        c["title"] = js("document.title")
        c["version"] = js("(() => { const x = new XMLHttpRequest(); x.open('GET', '/api/update', false); x.send(); const d = JSON.parse(x.responseText); return [d.current, d.can_install, d.feed]; })()")
        c["welcome_dialog"] = js("typeof window.Welcome + '/' + typeof window.JWUpdate")
        c["theme"] = js("document.documentElement.dataset.theme")
        c["nav"] = js("[...document.querySelectorAll('.mainnav .navbtn .nt')].map(e => e.textContent)")
        c["folders_in_sidebar"] = js("document.querySelectorAll('#sbfolders .sbf').length")
        c["sidebar_visible"] = js("getComputedStyle(document.querySelector('#sidebar')).visibility")
        c["secure_context"] = js("window.isSecureContext")
        c["speech"] = js("typeof window.speechSynthesis")
        c["marked"] = js("typeof window.marked")
        c["api"] = js("typeof (window.pywebview && window.pywebview.api && window.pywebview.api.save_url)")
        c["storage"] = js("(() => { try { const k = localStorage.getItem('jw-selftest'); localStorage.setItem('jw-selftest', String(Date.now())); return k ? 'kept:' + k : 'new'; } catch (e) { return 'blocked'; } })()")
        c["text_select"] = js("getComputedStyle(document.body).webkitUserSelect")
        c["library_rows"] = js("(() => { const x = new XMLHttpRequest(); x.open('GET', '/api/library', false); x.send(); return JSON.parse(x.responseText).length; })()")
        # a download made by the page: the app should write it where the test says (no panel to click)
        dest = os.environ.get("JW_APP_TEST_SAVE")
        if dest:
            if os.path.exists(dest):
                os.remove(dest)
            js("(() => { const a = document.createElement('a'); a.href = '/api/config'; a.download = 'config.json'; document.body.append(a); a.click(); a.remove(); })()")
            for _ in range(40):
                if os.path.exists(dest) and os.path.getsize(dest) > 10:
                    break
                time.sleep(0.25)
            c["download_saved"] = os.path.exists(dest) and os.path.getsize(dest)
            js("(() => { const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob(['blob text'], {type: 'text/plain'})); a.download = 'blob.txt'; document.body.append(a); a.click(); a.remove(); })()")
            time.sleep(1.5)
            c["blob_saved"] = open(dest, "rb").read() if os.path.exists(dest) else None
            c["blob_saved"] = c["blob_saved"].decode("utf-8", "replace") if c["blob_saved"] else None
        js("document.querySelectorAll('dialog[open]').forEach(d => d.close())")
        js("Welcome.open(1); 1")                           # the welcome guide, the way Help → Welcome guide opens it
        time.sleep(1.2)
        c["welcome_open"] = js("!!document.querySelector('dialog.wl[open]') && document.querySelector('dialog.wl h2').textContent")
        c["welcome_has_key_field"] = js("!!document.querySelector('#wl-key')")
        if os.environ.get("JW_APP_SHOT"):
            for i in range(5):
                js("Welcome.open(%d); 1" % i)
                time.sleep(0.9)
                snap(window, os.path.join(os.environ["JW_APP_SHOT"], "welcome-%d.png" % i)) if os.makedirs(os.environ["JW_APP_SHOT"], exist_ok=True) is None else None
        js("document.querySelectorAll('dialog[open]').forEach(d => d.close())")
        shots = os.environ.get("JW_APP_SHOT")                # a folder: pictures of the real window (no screen-recording permission needed: it is our own window)
        if shots:
            os.makedirs(shots, exist_ok=True)
            time.sleep(1.0)
            snap(window, os.path.join(shots, "library.png") if js("location.hash") == "#/library" else os.path.join(shots, "start.png"))
            js("location.hash = '#/library'")
            time.sleep(1.5)
            snap(window, os.path.join(shots, "library.png"))
            js("document.querySelector('#themebtn').click()")
            time.sleep(0.8)
            snap(window, os.path.join(shots, "library-dark.png"))
            js("document.querySelector('#themebtn').click()")
        if os.environ.get("JW_APP_TEST_READ"):              # open the first paper in the reader: pdf.js (ES modules, a worker) in this web view
            first = js("(() => { const x = new XMLHttpRequest(); x.open('GET', '/api/library', false); x.send(); const r = JSON.parse(x.responseText).find(r => r.file); return r ? r.file : ''; })()")
            if first:
                js("location.hash = '#read=' + encodeURIComponent(%s)" % json.dumps(first))
                for _ in range(80):
                    if js("document.querySelectorAll('.rd-band .textLayer span').length") > 5:
                        break
                    time.sleep(0.5)
                c["reader_text_spans"] = js("document.querySelectorAll('.rd-band .textLayer span').length")
                c["reader_left"] = js("Math.round(document.querySelector('#reader').getBoundingClientRect().left)")
                c["sidebar_in_reader"] = js("getComputedStyle(document.querySelector('#sidebar')).visibility")
                if shots:
                    time.sleep(1.0)
                    snap(window, os.path.join(shots, "reader.png"))
    except Exception:  # noqa: BLE001
        out["errors"].append(traceback.format_exc()[-800:])
    with open(os.environ["JW_APP_TEST"], "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, default=str)
    time.sleep(0.5)
    os._exit(0)


def main():
    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    os.makedirs(SUPPORT, exist_ok=True)
    win = webview.create_window(NAME, html=SPLASH, js_api=API, width=1360, height=880, min_size=(900, 600), text_select=True, background_color="#fbfaf7")
    menu = [Menu("Data", [MenuAction("Show my data folder in Finder", API.reveal_home), MenuAction("Change data folder…", API.change_home), MenuSeparator(), MenuAction("Open in my web browser", API.open_in_browser)]),
            Menu("Help", [MenuAction("Welcome guide…", API.show_welcome), MenuAction("Check for Updates…", API.check_updates)])]
    webview.start(boot, win, private_mode=bool(os.environ.get("JW_APP_TEST")), storage_path=os.path.join(SUPPORT, "webkit"), menu=menu)     # a self-test must not leave its page storage (theme, "welcome seen") in your real app


if __name__ == "__main__":
    main()
