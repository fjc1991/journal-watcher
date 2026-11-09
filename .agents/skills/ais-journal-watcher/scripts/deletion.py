"""Deleting what you added: a paper or a book from the library (for good), and the record of a past run.

    deletion.delete_item(file)   -> {"title", "deleted": [relative paths], "kind": "paper" | "book"}
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
KEEP_DAYS = 30
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


def _inside_papers(file):
    """The real path of a library file named by `file` (relative to the data folder), or a ValueError: it must be a PDF under Papers/, not a link out of it."""
    base = os.path.realpath(dl.PAPERS)
    full = os.path.realpath(os.path.join(dl.ROOT, file)) if isinstance(file, str) and file and "\0" not in file else ""
    if not full.startswith(base + os.sep) or not full.lower().endswith(".pdf") or not os.path.isfile(full):
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
        token = "deleted-%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), os.urandom(3).hex())
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
                    json.dump({"token": token, "file": file, "title": row.get("title") or "", "kind": kind, "when": time.time(), "row": was, "folders": was_folders, "state": was_state, "files": moved}, f, ensure_ascii=False)
            except OSError:
                token = ""
        else:
            token = ""
        if sha:                                                  # its passages and references in the shared search index (state/index/<sha1>.blocks.json, .vec, .vec.json, .refs.json)
            import blocks as bl
            for p in glob.glob(os.path.join(bl.INDEX, sha + ".*")):
                try:
                    os.remove(p)
                except OSError:
                    pass
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


def _undo_of(token):
    if not re.fullmatch(r"deleted-\d{8}-\d{6}-[0-9a-f]{6}", str(token or "")):
        return None, None
    d = os.path.join(_removed_root(), token)
    try:
        with open(os.path.join(d, "_undo.json"), encoding="utf-8") as f:
            return d, json.load(f)
    except (OSError, ValueError):
        return None, None


def recently_deleted():
    """What you deleted in the last KEEP_DAYS days and can still put back, newest first: [{token, title, kind, when, days_left}]."""
    out = []
    root = _removed_root()
    if os.path.isdir(root):
        for n in os.listdir(root):
            d, u = _undo_of(n)
            if u:
                left = KEEP_DAYS - int((time.time() - float(u.get("when") or 0)) // 86400)
                if left > 0:
                    out.append({"token": n, "title": u.get("title") or u.get("file") or "", "kind": u.get("kind") or "paper", "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(float(u.get("when") or 0))), "days_left": left})
    return sorted(out, key=lambda x: x["token"], reverse=True)


def purge_old():
    """Delete from disk what was deleted more than KEEP_DAYS days ago. Returns how many."""
    n = 0
    root = _removed_root()
    if os.path.isdir(root):
        for name in os.listdir(root):
            d, u = _undo_of(name)
            if u and time.time() - float(u.get("when") or 0) > KEEP_DAYS * 86400:
                shutil.rmtree(d, ignore_errors=True)
                n += 1
    return n


def restore(token):
    """Put a deleted paper or book back: its files where they were, its index row "ok" again, its folders and reading status as they were. Refused when the
    paper is in the library again meanwhile (you added it, or a run downloaded it after you restored it elsewhere). Returns {"title", "file"}."""
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
        if any(dl.index_key(r) == key and r.get("status") == "ok" and r.get("file") for r in rows):
            raise ValueError("“%s” is in your library again already." % (u.get("title") or "It")[:80])
        base = os.path.realpath(dl.PAPERS)
        for rel in u.get("files") or []:
            src, dest = os.path.join(d, rel), os.path.realpath(os.path.join(base, rel))
            if not dest.startswith(base + os.sep) or not os.path.exists(src):
                continue
            if os.path.exists(dest):
                raise ValueError("A file is in the way: %s" % rel)
        for rel in u.get("files") or []:
            src, dest = os.path.join(d, rel), os.path.realpath(os.path.join(base, rel))
            if dest.startswith(base + os.sep) and os.path.exists(src):
                os.makedirs(os.path.dirname(dest), exist_ok=True)
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
            except Exception:  # noqa: BLE001      (the folder was deleted meanwhile)
                pass
        if u.get("state"):
            try:
                st = rs.load()
                st[key] = u["state"]
                rs._save(st)
            except Exception:  # noqa: BLE001
                pass
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

