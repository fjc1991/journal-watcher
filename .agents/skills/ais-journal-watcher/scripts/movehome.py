"""Copy one data folder into another: what "Change data folder…" does when you ask it to take your data along (app/jw_app.py), and what a first start offers
when it finds the folder an earlier version used.

Rules (decided with the author, Nov 2026):
  * copy only: the old folder is never changed or deleted; the app says what was copied and leaves the rest to you
  * the destination wins: a file that exists in both stays as it is in the destination (so a folder already synced from another computer is not overwritten),
    settings files (ai.json, journals.json, …) and .env included
  * the library list is joined, not overwritten: Papers/_index.csv of both folders becomes one list with every paper of either (syncinfo.join_rows: the same paper,
    by DOI or title, keeps its better row), otherwise papers whose PDFs were just copied would be missing from the list
  * per-computer items never travel: paths.LOCAL_ITEMS (the sign-in browser profile and cookies, signins.json, update state, the models, staging, fetched, the lookup
    caches) and the server.lock, wherever an older version left them inside the data folder; they are made afresh where they belong (paths.LOCAL)
  * the computers' "I am here" files (devices/, syncinfo.py) never travel either: after a change of folder this computer has a new id (its id is kept per data
    folder, in paths.LOCAL), so its old file would show up in the new folder as a second computer with its own name, "open now" for three minutes

What belongs to one computer does come along on THIS computer (carry_local): the sign-in browser (so you need not sign in to your library again), its cookies and sign-in
state, and the embedding model (so it is not downloaded again). They are copied from the old folder's per-computer folder to the new one's, never into a data folder.

This module imports paths only inside functions: paths.py fixes ROOT when it is first imported (from JW_HOME), and the app may copy before it has chosen a folder.
The app always starts a fresh process after a copy, so nothing here depends on paths.ROOT.
"""
import os
import shutil
import stat

SKILL_REL = os.path.join(".agents", "skills", "ais-journal-watcher")
STATE_REL = os.path.join(SKILL_REL, "state")
INDEX_REL = os.path.join("Papers", "_index.csv")
_NOISE = {".DS_Store", "Icon\r", "desktop.ini", "Thumbs.db"}


def looks_like_home(p):
    """Does `p` hold Journal Watcher data (a Papers folder or the settings folder)? A plain empty folder does not."""
    return bool(p) and os.path.isdir(p) and (os.path.isdir(os.path.join(p, "Papers")) or os.path.isdir(os.path.join(p, SKILL_REL)))


def _excluded():
    import paths
    ex = {os.path.join(STATE_REL, n) for n in paths.LOCAL_ITEMS}
    ex.add(os.path.join(STATE_REL, "server.lock"))
    ex.add(os.path.join(SKILL_REL, "devices"))
    return ex


def _same(a, b):
    return os.path.realpath(a) == os.path.realpath(b)


def _nested(inner, outer):
    i, o = os.path.realpath(inner), os.path.realpath(outer)
    return i == o or i.startswith(o.rstrip(os.sep) + os.sep)


def check(src, dst):
    """Why `src` cannot be copied into `dst`, or None."""
    if not src or not os.path.isdir(src):
        return "The folder to copy from is not there any more."
    if not dst:
        return "No destination was chosen."
    if _same(src, dst):
        return "That is the folder you already use."
    if _nested(dst, src):
        return "The new folder is inside the current one: choose a folder outside %s." % src
    if _nested(src, dst):
        return "The current folder is inside the new one: choose a folder that does not contain %s." % src
    return None


def plan(src):
    """What a copy of `src` would carry: {"files", "bytes", "pdfs", "skipped": [relative paths left out]}. Symbolic links are left out (a data folder has none of its own)."""
    ex = _excluded()
    out = {"files": 0, "bytes": 0, "pdfs": 0, "skipped": []}
    for rel, full in _walk(src, ex, out["skipped"]):
        try:
            st = os.lstat(full)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        out["files"] += 1
        out["bytes"] += st.st_size
        if rel.lower().endswith(".pdf"):
            out["pdfs"] += 1
    return out


def _walk(src, excluded, skipped):
    """(relative path, full path) of every entry under src that may be copied, directories first; the excluded ones are listed in `skipped` and not entered."""
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        rel_dir = os.path.relpath(dirpath, src)
        rel_dir = "" if rel_dir == "." else rel_dir
        keep = []
        for d in sorted(dirnames):
            rel = os.path.join(rel_dir, d)
            full = os.path.join(dirpath, d)
            if rel in excluded or os.path.islink(full):
                skipped.append(rel)
                continue
            keep.append(d)
            yield rel, full
        dirnames[:] = keep
        for f in sorted(filenames):
            if f in _NOISE or f.endswith(".tmp"):
                continue
            rel = os.path.join(rel_dir, f)
            full = os.path.join(dirpath, f)
            if rel in excluded or os.path.islink(full):
                skipped.append(rel)
                continue
            yield rel, full


def copy_into(src, dst, say=None):
    """Copy the data folder `src` into `dst` by the rules above. `say(done, total, rel)` is called as files are copied (for a progress page).
    Returns {"copied", "bytes", "pdfs", "kept" (existed in dst, left alone), "index": {"papers", "added"} or None, "skipped": [...], "errors": [(rel, why)]}.
    Raises ValueError when check() objects."""
    why = check(src, dst)
    if why:
        raise ValueError(why)
    ex = _excluded()
    res = {"copied": 0, "bytes": 0, "pdfs": 0, "kept": 0, "index": None, "skipped": [], "errors": []}
    entries = list(_walk(src, ex, res["skipped"]))
    total = sum(1 for _, full in entries if os.path.isfile(full))
    done = 0
    os.makedirs(dst, exist_ok=True)
    for rel, full in entries:
        to = os.path.join(dst, rel)
        if os.path.isdir(full):
            try:
                os.makedirs(to, exist_ok=True)
            except OSError as e:
                res["errors"].append((rel, str(e)))
            continue
        done += 1
        if rel == INDEX_REL:
            continue                                            # joined at the end, once the PDFs it names are in place
        if os.path.lexists(to):
            res["kept"] += 1
        else:
            try:
                shutil.copy2(full, to)
                if os.path.getsize(to) != os.path.getsize(full):
                    raise OSError("the copy is %d bytes, the original %d" % (os.path.getsize(to), os.path.getsize(full)))
                res["copied"] += 1
                res["bytes"] += os.path.getsize(to)
                if rel.lower().endswith(".pdf"):
                    res["pdfs"] += 1
            except OSError as e:
                res["errors"].append((rel, str(e)))
        if say:
            say(done, total, rel)
    src_idx, dst_idx = os.path.join(src, INDEX_REL), os.path.join(dst, INDEX_REL)
    if os.path.isfile(src_idx):
        try:
            res["index"] = join_index(src_idx, dst_idx, dst)
        except (OSError, ValueError) as e:
            res["errors"].append((INDEX_REL, str(e)))
        if say:
            say(total, total, INDEX_REL)
    return res


def join_index(src_idx, dst_idx, dst_root):
    """Papers/_index.csv of the destination becomes the join of both lists (syncinfo.join_rows); with none there yet, a copy of the source's. Returns {"papers", "added"}."""
    import csv
    import syncinfo
    with open(src_idx, newline="", encoding="utf-8") as f:
        theirs = list(csv.DictReader(f))
    mine = []
    if os.path.isfile(dst_idx):
        with open(dst_idx, newline="", encoding="utf-8") as f:
            mine = list(csv.DictReader(f))
    rows = syncinfo.join_rows(mine, theirs, dst_root)
    os.makedirs(os.path.dirname(dst_idx), exist_ok=True)
    syncinfo.write_index(dst_idx, rows)
    return {"papers": len(rows), "added": len(rows) - len(mine)}


CARRY = ("browser-profile", "session-cookies.json", "signins.json", "models")
# what a browser profile holds that must not be copied: the locks of a running Chromium (a copied one would make the new folder's browser think the profile is in use
# elsewhere) and the caches (large, and made again)
PROFILE_SKIP = shutil.ignore_patterns("Singleton*", "lockfile", "*.lock", "Cache", "Code Cache", "GPUCache", "DawnCache", "DawnGraphiteCache", "DawnWebGPUCache",
                                      "GrShaderCache", "GraphiteDawnCache", "ShaderCache", "Crashpad", "CacheStorage")


def carry_local(src_root, dst_root, local_of=None):
    """When THIS computer changes data folder: copy its per-computer items (CARRY) from `src_root`'s per-computer folder to `dst_root`'s. Copy only; an item the new
    per-computer folder already has is left as it is; nothing goes into a data folder (a run from a project folder keeps them in state/, which would be the shared folder).
    An older version kept them in the data folder's own state/: they are taken from there when the per-computer folder has none. local_of(root) gives a data folder's
    per-computer folder (paths.local_dir_for; tests pass their own). Returns {"carried": [names], "kept": [names], "errors": [(name, why)], "skipped": reason or None}."""
    if local_of is None:
        import paths
        local_of = paths.local_dir_for
    res = {"carried": [], "kept": [], "errors": [], "skipped": None}
    a, b = local_of(src_root), local_of(dst_root)
    if _same(a, b):
        res["skipped"] = "the same per-computer folder"
        return res
    if _nested(b, dst_root):
        res["skipped"] = "the new per-computer folder is inside the data folder"
        return res
    for name in CARRY:
        frm = os.path.join(a, name)
        if not os.path.lexists(frm):
            frm = os.path.join(src_root, STATE_REL, name)          # (where an older version kept it)
        if not os.path.lexists(frm) or os.path.islink(frm):
            continue
        to = os.path.join(b, name)
        if os.path.lexists(to):
            res["kept"].append(name)
            continue
        try:
            os.makedirs(b, exist_ok=True)
            if os.path.isdir(frm):
                shutil.copytree(frm, to, symlinks=True, ignore=PROFILE_SKIP if name == "browser-profile" else None)
            else:
                shutil.copy2(frm, to)
            res["carried"].append(name)
        except (OSError, shutil.Error) as e:
            shutil.rmtree(to, ignore_errors=True) if os.path.isdir(to) else None
            res["errors"].append((name, str(e)[:200]))
    return res


def describe(res, src):
    """One short paragraph of what copy_into did, for the page the app shows."""
    b = res["bytes"]
    size = "%.1f GB" % (b / 1e9) if b >= 1e9 else "%.0f MB" % (b / 1e6) if b >= 1e7 else "%.1f MB" % (b / 1e6) if b >= 1e6 else "%.0f KB" % (b / 1e3)
    parts = ["%d file%s copied (%s%s)" % (res["copied"], "" if res["copied"] == 1 else "s", size, (", %d PDF%s" % (res["pdfs"], "" if res["pdfs"] == 1 else "s")) if res["pdfs"] else "")]
    if res["kept"]:
        parts.append("%d already there and kept as they were" % res["kept"])
    if res["index"]:
        parts.append("the library list now has %d paper%s (%d added)" % (res["index"]["papers"], "" if res["index"]["papers"] == 1 else "s", res["index"]["added"]))
    s = "; ".join(parts) + "."
    s += " Per-computer items (the sign-in browser, caches, update state) were not copied into the folder; on this computer the sign-in browser and the language model come along when you open it. Nothing was changed in %s." % src
    if res["errors"]:
        s += " %d file%s could not be copied." % (len(res["errors"]), "" if len(res["errors"]) == 1 else "s")
    return s
