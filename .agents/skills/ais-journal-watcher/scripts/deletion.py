"""Deleting what you added: a paper or a book from the library, a folder (with the folders inside it, and if you say so its papers), the PDF a newer copy
replaced, and the record of a past run. What is deleted waits in the bin (Papers/_removed/) for KEEP_DAYS days, then leaves the disk by itself.

    deletion.delete_item(file)   -> {"title", "deleted": [relative paths], "kind": "paper" | "book"}
    deletion.delete_folder(fid, papers=False) -> {"title", "folders": n, "papers": n, "kept": n, "undo"}
    deletion.replace_pdf(file, data) -> {"file", "undo"}    the paper keeps its place, notes and folders; its old PDF goes to the bin
    deletion.recently_deleted() / restore(token) / forget(token)   the bin: what is in it, put back, delete now
    deletion.deleted_keys()      -> the index keys of papers you deleted (a later run shows them as "deleted by you" and never downloads them)
    deletion.delete_run(rid)     -> the run's title

A deleted paper or book:
  * its files leave the library: the PDF (for a book its reading copy, the EPUB and its .book.json) and everything that belongs to it (notes and
    highlights, summary, card, skim, conversation: notes_store.sidecars) are moved to Papers/_removed/deleted-<time>/ and kept there for KEEP_DAYS
    days (deletion.restore puts them back, with the folders and reading status it had); after that they are deleted from disk. Its passages in the
    search index (state/index/<sha1>.*) are deleted at once (they are made again from the PDF);
  * it leaves every folder and loses its reading status, and the page forgets where you stopped reading it;
  * its row in Papers/_index.csv stays, with status "deleted" and no file: that is how a weekly check or an archive search knows not to fetch it again.
    Adding it yourself (drop or import) replaces the row, as for any paper.
"""
import csv
import glob
import json
import os
import re
import shutil
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import download as dl  # noqa: E402

DELETED = "deleted"
KEEP_DAYS = 7                     # how long the bin keeps what you deleted
OLD_KEEP_DAYS = 30                # what was deleted before 1.4.3 was promised 30 days; its record has no "keep" and keeps that promise


def _keep(u):
    """How many days this bin entry is kept: what its record says, else (a record written before 1.4.3) OLD_KEEP_DAYS."""
    try:
        return int(u.get("keep", OLD_KEEP_DAYS))
    except (TypeError, ValueError):
        return KEEP_DAYS
_lock = threading.Lock()


def _removed_root():
    return os.path.join(dl.PAPERS, "_removed")


def _index_path():
    return os.path.join(dl.PAPERS, "_index.csv")


def deleted_keys():
    """Index keys (download.index_key: DOI, else title) of the papers and books you deleted."""
    p = _index_path()
    if not os.path.exists(p):
        return set()
    with open(p, newline="", encoding="utf-8") as f:
        return {dl.index_key(r) for r in csv.DictReader(f) if r.get("status") == DELETED and dl.index_key(r)}


def _inside_papers(file, missing_ok=False):
    """The real path of a library file named by `file` (relative to the data folder), or a ValueError: it must be a PDF under Papers/, not a link out of it
    (missing_ok: it may not be there)."""
    base = os.path.realpath(dl.PAPERS)
    full = os.path.realpath(os.path.join(dl.ROOT, file)) if isinstance(file, str) and file and "\0" not in file else ""
    if not full.startswith(base + os.sep) or not full.lower().endswith(".pdf") or not (os.path.isfile(full) or (missing_ok and not os.path.exists(full))):
        raise ValueError("That file is not in your library.")
    if any(part.startswith("_") for part in os.path.relpath(full, base).split(os.sep)[:-1]):     # (Papers/_removed, _conflicts, _folders: not library papers)
        raise ValueError("That file is not in your library.")
    return full


def delete_item(file):
    """Delete a paper or a book from the library for good (see the module's docstring). `file` is its path as the library lists it ("Papers/…/x.pdf")."""
    import notes_store as ns
    import readstate as rs
    import folders as fo
    with _lock, dl.INDEX_LOCK:                  # (the index: not while a download writes its row)
        full = _inside_papers(file)
        idx = _index_path()
        with open(idx, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
            cols = list(rows[0].keys()) if rows else list(dl.INDEX_COLS)
        mine = [r for r in rows if r.get("file") == file and r.get("status") == "ok"]
        if not mine:
            raise ValueError("That file is not in your library index.")
        row = mine[0]
        key = dl.index_key(row)
        kind = "book" if os.path.exists(full[:-4] + ".book.json") or row.get("abbr") == "BOOK" else "paper"
        try:
            sha = ns.sha1_of(full)
        except OSError:
            sha = None
        files = [full] + ns.sidecars(full)
        root = os.path.realpath(dl.ROOT)
        was = dict(row)                                          # (the row, its folders and its reading status as they were: what an undo puts back)
        try:
            was_folders = list(fo.membership().get(key) or [])
        except Exception:  # noqa: BLE001
            was_folders = []
        try:
            was_state = rs.load().get(key)
        except Exception:  # noqa: BLE001
            was_state = None
        # the row first (a crash after this leaves files nobody lists, not a listed paper without its PDF)
        row["file"], row["status"] = "", DELETED
        row["note"] = "deleted by you on %s" % time.strftime("%Y-%m-%d")
        tmp = dl.tmp_path(idx)
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        os.replace(tmp, idx)
        deleted, moved = [], []
        token = _token("deleted")
        keep_dir = os.path.join(_removed_root(), token)
        base = os.path.realpath(dl.PAPERS)
        for p in files:
            rel = os.path.relpath(p, base)
            try:
                if rel.startswith(".."):
                    raise OSError("outside Papers")
                dest = os.path.join(keep_dir, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.move(p, dest)
                moved.append(rel)
                deleted.append(os.path.relpath(p, root))
            except FileNotFoundError:
                pass
            except OSError:                                      # (it could not be kept: it still leaves the library, as before)
                try:
                    os.remove(p)
                    deleted.append(os.path.relpath(p, root))
                except OSError:
                    pass
        if moved:
            try:
                with open(os.path.join(keep_dir, "_undo.json"), "w", encoding="utf-8") as f:
                    json.dump({"token": token, "file": file, "title": row.get("title") or "", "kind": kind, "when": time.time(), "keep": KEEP_DAYS, "row": was, "folders": was_folders, "state": was_state, "files": moved}, f, ensure_ascii=False)
            except OSError:
                token = ""
        else:
            token = ""
        _forget_index(sha)                                       # its passages and references in the shared search index (state/index/<sha1>.blocks.json, .vec, .vec.json, .refs.json)
        try:                                                     # out of every folder
            for fid in (fo.membership().get(key) or []):
                fo.remove(fid, [key])
        except Exception:  # noqa: BLE001
            pass
        try:                                                     # its reading status
            st = rs.load()
            if key in st:
                st.pop(key)
                rs._save(st)
        except Exception:  # noqa: BLE001
            pass
        try:                                                     # where you stopped reading it (the page's choices in the data folder)
            import uiprefs
            uiprefs.update({}, ["jw-pos:" + file, "jw-skim-on:" + file])
        except Exception:  # noqa: BLE001
            pass
        return {"title": row.get("title") or os.path.basename(file), "deleted": deleted, "kind": kind, "undo": token}


TOKEN = re.compile(r"(deleted|folder|replaced)-\d{8}-\d{6}-[0-9a-f]{6}")


def _token(kind):
    return "%s-%s-%s" % (kind, time.strftime("%Y%m%d-%H%M%S"), os.urandom(3).hex())


def _undo_of(token):
    if not TOKEN.fullmatch(str(token or "")):
        return None, None
    d = os.path.join(_removed_root(), token)
    try:
        with open(os.path.join(d, "_undo.json"), encoding="utf-8") as f:
            u = json.load(f)
    except (OSError, ValueError):
        return None, None
    return (d, u) if isinstance(u, dict) else (None, None)


def _when(u):
    """When it went to the bin; a record whose time cannot be read counts as just now (kept, rather than deleted or a crash)."""
    try:
        return float(u.get("when") or 0) or time.time()
    except (TypeError, ValueError):
        return time.time()


def _write_undo(d, u):
    tmp = dl.tmp_path(os.path.join(d, "_undo.json"))
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(u, fh, ensure_ascii=False)
    os.replace(tmp, os.path.join(d, "_undo.json"))


def _release(tokens):
    """Papers that went to the bin with a folder but did not come back with it: listed in the Bin on their own from now on."""
    for t in tokens:
        d2, u2 = _undo_of(t)
        if u2 and u2.pop("with", None):
            try:
                _write_undo(d2, u2)
            except OSError:
                pass


def recently_deleted():
    """What is in the bin and can still be put back, newest first: [{token, title, kind ("paper" | "book" | "folder" | "replaced"), when, days_left, papers (a folder:
    how many of its papers went with it), folders (a folder: how many folders, itself included)}]. A paper deleted with its folder is listed under the folder, not on
    its own. What is older than its keep (KEEP_DAYS days; 30 for what was deleted before 1.4.3) leaves the disk first."""
    purge_old()
    out = []
    root = _removed_root()
    if os.path.isdir(root):
        for n in os.listdir(root):
            d, u = _undo_of(n)
            if u and not u.get("with"):
                left = _keep(u) - int((time.time() - _when(u)) // 86400)
                if left > 0:
                    out.append({"token": n, "title": u.get("title") or u.get("file") or "", "kind": u.get("kind") or "paper",
                                "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(_when(u))), "days_left": left,
                                "papers": len(u.get("papers") or []), "folders": len(u.get("folder_files") or {}), "_t": _when(u)})
    out.sort(key=lambda x: x["_t"], reverse=True)              # (newest first, to the second: "when" shows the minute only)
    for x in out:
        del x["_t"]
    return out


def purge_old():
    """Delete from disk what has been in the bin longer than it is kept (KEEP_DAYS days; OLD_KEEP_DAYS for a record written before 1.4.3). Returns how many."""
    n = 0
    root = _removed_root()
    with _lock:                                                   # (not while something is being put back)
        if os.path.isdir(root):
            for name in os.listdir(root):
                d, u = _undo_of(name)
                if not u:
                    continue
                if u.get("with") and _undo_of(u["with"])[1]:
                    continue                                      # (a paper that went with a folder leaves with that folder, not a few minutes before it)
                if time.time() - _when(u) > _keep(u) * 86400:
                    for t in u.get("papers") or []:
                        d2, u2 = _undo_of(t)
                        if u2:
                            shutil.rmtree(d2, ignore_errors=True)
                    shutil.rmtree(d, ignore_errors=True)
                    n += 1
    return n


def forget(token):
    """Delete one thing in the bin from disk now (a folder with the papers that went with it). Returns its title."""
    with _lock:
        d, u = _undo_of(token)
        if not u:
            raise ValueError("That is not in the bin any more.")
        for t in u.get("papers") or []:
            d2, u2 = _undo_of(t)
            if u2:
                shutil.rmtree(d2, ignore_errors=True)
        shutil.rmtree(d, ignore_errors=True)
        return u.get("title") or ""


def restore(token):
    """Put back what is in the bin. A paper or a book: its files where they were, its index row "ok" again, its folders and reading status as they were; refused
    when the paper is in the library again meanwhile (you added it, or a run downloaded it). A folder: its files and the folders that were inside it, then the
    papers that went with it. A replaced PDF: back in its place, and the copy that replaced it goes to the bin in turn. Returns {"title", "file"}."""
    d, u = _undo_of(token)
    if u and u.get("kind") == "folder":
        return _restore_folder(token)
    if u and u.get("kind") == "replaced":
        return _restore_replaced(token)
    return _restore_item(token)


def _restore_item(token):
    import readstate as rs
    import folders as fo
    with _lock, dl.INDEX_LOCK:
        d, u = _undo_of(token)
        if not u:
            raise ValueError("That deleted item is not kept any more.")
        idx = _index_path()
        with open(idx, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
            cols = list(rows[0].keys()) if rows else list(dl.INDEX_COLS)
        key = dl.index_key(u["row"])
        if any(dl.index_key(r) == key and r.get("status") == "ok" and r.get("file") and os.path.exists(os.path.join(dl.ROOT, r["file"])) for r in rows):
            raise ValueError("“%s” is in your library again already." % (u.get("title") or "It")[:80])
        base = os.path.realpath(dl.PAPERS)
        for rel in u.get("files") or []:
            src, dest = os.path.join(d, rel), os.path.realpath(os.path.join(base, rel))
            if not dest.startswith(base + os.sep) or not os.path.exists(src):
                continue
            if os.path.exists(dest) and rel.lower().endswith(".pdf"):
                raise ValueError("A file is in the way: %s" % rel)
        for rel in u.get("files") or []:
            src, dest = os.path.join(d, rel), os.path.realpath(os.path.join(base, rel))
            if dest.startswith(base + os.sep) and os.path.exists(src):
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                if os.path.exists(dest):                         # (notes or a summary written there meanwhile stay; the kept ones go beside them)
                    stem, ext = os.path.splitext(dest)
                    dest = "%s.put-back-%s%s" % (stem, time.strftime("%Y%m%d-%H%M%S"), ext)
                shutil.move(src, dest)
        rows = [r for r in rows if dl.index_key(r) != key] + [u["row"]]
        tmp = dl.tmp_path(idx)
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        os.replace(tmp, idx)
        for fid in u.get("folders") or []:
            try:
                fo.add(fid, [key])
            except Exception:  # noqa: BLE001      (the folder was deleted meanwhile: if it is in the bin, the paper goes back into it with the folder)
                _hold_member(fid, key)
        if u.get("state"):
            try:
                st = rs.load()
                st[key] = u["state"]
                rs._save(st)
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(d, ignore_errors=True)
        return {"title": u.get("title") or "", "file": u.get("file") or ""}


def _hold_member(fid, key):
    """A paper put back while a folder it was in is in the bin: write it into that folder's kept copy, so it is in the folder again when the folder is put back."""
    root = _removed_root()
    if not os.path.isdir(root) or not re.fullmatch(r"[a-z0-9-]{1,48}", str(fid)):
        return
    for n in os.listdir(root):
        d, u = _undo_of(n)
        if u and u.get("kind") == "folder" and fid in (u.get("folder_files") or {}):
            p = os.path.join(d, "_folders", fid + ".json")
            try:
                with open(p, encoding="utf-8") as fh:
                    f = json.load(fh)
                if key not in f.get("papers", []):
                    f.setdefault("papers", []).append(key)
                    with open(p, "w", encoding="utf-8") as fh:
                        json.dump(f, fh, ensure_ascii=False, indent=1)
            except (OSError, ValueError, AttributeError):
                pass


# ---------------------------------------------------------------- folders into the bin

def delete_folder(fid, papers=False, children="delete"):
    """Delete a folder: it, and (children "delete") the folders inside it, go to the bin with what was made in them (Ideas Lab board, matrix, drafts…). With
    papers=True the papers they show go too, each as delete_item does, except a paper that is also in a folder outside them: that one stays (`kept` counts
    them). Returns {"title", "count" (folders), "papers", "kept", "undo"}. children "lift" (what the server does when not told): only this folder goes; the folders
    inside it move up one level, as folders.delete does."""
    import folders as fo
    import pipeline as pl
    f = fo.get(fid) if isinstance(fid, str) else None
    if not f:
        raise KeyError("no such folder")
    fam = fo.family(fid) if children == "delete" else [fid]
    gone, kept, tokens = 0, 0, []
    if papers:
        keys = set(fo.all_papers(fid) or []) if children == "delete" else set(f["papers"])
        member = fo.membership()
        rows = {r.get("key"): r for r in pl.library() if r.get("file")}
        for k in keys:
            if any(x not in fam for x in member.get(k) or []):
                kept += 1                                       # (also in a folder you keep: it stays in the library)
                continue
            r = rows.get(k)
            if not r:
                continue
            try:
                res = delete_item(r["file"])
                if res.get("undo"):
                    tokens.append(res["undo"])
                gone += 1
            except (ValueError, OSError):
                pass
    with _lock:
        token = _token("folder")
        keep_dir = os.path.join(_removed_root(), token, "_folders")
        os.makedirs(keep_dir, exist_ok=True)
        files = {}
        for x in fam:                                             # (copied now; folders.delete removes them, so a crash in between leaves the folder, not nothing)
            for suffix in FOLDER_SUFFIXES:
                p = os.path.join(fo.DIR, x + suffix)
                if os.path.exists(p):
                    shutil.copy2(p, os.path.join(keep_dir, x + suffix))
                    files.setdefault(x, []).append(suffix)
        with open(os.path.join(_removed_root(), token, "_undo.json"), "w", encoding="utf-8") as fh:
            json.dump({"token": token, "kind": "folder", "title": " › ".join(fo.path(fid)) or f["name"], "when": time.time(), "keep": KEEP_DAYS, "folder": fid, "folder_files": files,
                       "papers": tokens, "children": children}, fh, ensure_ascii=False)
        for t in tokens:                                          # (the papers that went with it are listed under it, and come back with it)
            d2, u2 = _undo_of(t)
            if u2:
                u2["with"] = token
                _write_undo(d2, u2)
    fo.delete(fid, children)
    return {"title": f["name"], "count": len(fam), "papers": gone, "kept": kept, "undo": token}


FOLDER_SUFFIXES = (".json", ".ideas.json", ".digest.md", ".ask.json", ".matrix.json", ".draft.json", ".claims.json", ".synthesis.json", ".workspace.json", ".review.json")


def _restore_folder(token):
    import folders as fo
    with _lock:
        d, u = _undo_of(token)
        if not u:
            raise ValueError("That folder is not kept any more.")
        src = os.path.join(d, "_folders")
        every = u.get("folder_files") or {}
        clash = {x: s for x, s in every.items() if fo.get(x)}     # (made again meanwhile under the same name and place: that one stays, and gets its papers back)
        os.makedirs(fo.DIR, exist_ok=True)
        for x, suffixes in every.items():
            if x in clash or not re.fullmatch(r"[a-z0-9-]{1,48}", x):
                continue
            for suffix in suffixes:
                if suffix in FOLDER_SUFFIXES and os.path.exists(os.path.join(src, x + suffix)):
                    shutil.copy2(os.path.join(src, x + suffix), os.path.join(fo.DIR, x + suffix))
        tokens = list(u.get("papers") or [])
        if clash:                                                 # (its own files, the board, drafts…, wait in the bin: delete the new folder and put this back to have them)
            u["folder_files"], u["papers"] = clash, []
            _write_undo(d, u)
    errors, failed = [], []
    for t in tokens:                                              # its papers, into the folders they were in (the folder is back now)
        try:
            _restore_item(t)
        except Exception as e:  # noqa: BLE001      (it stays in the bin, listed on its own)
            errors.append(str(e)[:200])
            failed.append(t)
    _release(failed)
    if clash:
        names = ", ".join((json.load(open(os.path.join(src, x + ".json"), encoding="utf-8")).get("name") if os.path.exists(os.path.join(src, x + ".json")) else x) for x in clash)[:120]
        errors.insert(0, "A folder with the same name and place is there again (%s): its papers are back in it, and the deleted folder's own files (board, drafts, "
                         "matrix…) stay in the Bin. To have those, delete the new folder and put this back again." % names)
    else:
        shutil.rmtree(d, ignore_errors=True)
    return {"title": u.get("title") or "", "file": "", "errors": errors}


# ---------------------------------------------------------------- a newer copy of a PDF you have

def replace_pdf(file, data):
    """Put another PDF in place of the one a paper has (a newer version of a paper you already have). The paper keeps its file name, notes, highlights, summary,
    card, folders and reading status; the old PDF goes to the bin, and putting it back swaps the two again. Its passages in the search index are made again from
    the new PDF. Returns {"file", "undo"}."""
    import notes_store as ns
    if not dl.is_pdf(data):
        raise ValueError("That doesn’t look like a complete PDF.")
    import kinds
    with _lock:
        full = _inside_papers(file, missing_ok=True)
        if not os.path.exists(full):                              # (the paper's PDF is missing: the new one takes its place, nothing to keep)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            tmp = dl.tmp_path(full)
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, full)
            return {"file": file, "undo": None}
        try:
            sha = ns.sha1_of(full)
        except OSError:
            sha = None
        token = _token("replaced")
        base = os.path.realpath(dl.PAPERS)
        rels = [os.path.relpath(p, base) for p in (full, kinds.sidecar_path(full)) if os.path.exists(p)]
        for rel in rels:                                          # (the old PDF and the details beside it are in the bin before the new one is written)
            dest = os.path.join(_removed_root(), token, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(os.path.join(base, rel), dest)
        rel = rels[0]
        tmp = dl.tmp_path(full)
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, full)
        title = os.path.basename(file)
        try:
            import pipeline as pl
            title = (pl.library_row(file) or {}).get("title") or title
        except Exception:  # noqa: BLE001
            pass
        with open(os.path.join(_removed_root(), token, "_undo.json"), "w", encoding="utf-8") as fh:
            json.dump({"token": token, "kind": "replaced", "title": title, "file": file, "when": time.time(), "keep": KEEP_DAYS, "files": rels}, fh, ensure_ascii=False)
        _forget_index(sha)
        return {"file": file, "undo": token}


def _forget_index(sha):
    """Its passages and references in the shared search index (made again from the PDF that is there now)."""
    if sha:
        import blocks as bl
        for p in glob.glob(os.path.join(bl.INDEX, sha + ".*")):
            try:
                os.remove(p)
            except OSError:
                pass


def _restore_replaced(token):
    d, u = _undo_of(token)
    if not u:
        raise ValueError("That PDF is not kept any more.")
    rel = (u.get("files") or [""])[0]
    src = os.path.join(d, rel)
    if not os.path.exists(src):
        raise ValueError("That PDF is not kept any more.")
    try:
        _inside_papers(u.get("file"))
    except ValueError:
        raise ValueError("The paper it belonged to is not in your library any more.")
    with open(src, "rb") as fh:
        data = fh.read()
    full = replace_pdf(u.get("file"), data) and _inside_papers(u.get("file"))      # (the copy there now goes to the bin in turn, with its details)
    import kinds
    side = os.path.join(d, os.path.splitext(rel)[0] + ".paper.json")
    if os.path.exists(side):                                      # (the details it had come back with it)
        shutil.copy2(side, kinds.sidecar_path(full))
    shutil.rmtree(d, ignore_errors=True)
    return {"title": u.get("title") or "", "file": u.get("file") or ""}


def delete_run(rid):
    """Delete the record of a past run (a weekly check or an archive search). The papers it downloaded stay in the library. Returns its title."""
    import pipeline as pl
    rid = os.path.basename(str(rid or ""))
    if not re.fullmatch(r"[\w.-]{1,120}", rid):
        raise ValueError("No such run.")
    run = pl.Run.load(rid)
    if not run:
        raise ValueError("No such run.")
    title = run.title()
    os.remove(os.path.join(pl.RUNS, rid + ".json"))
    return title

