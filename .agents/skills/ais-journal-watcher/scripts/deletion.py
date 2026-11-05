"""Deleting what you added: a paper or a book from the library (for good), and the record of a past run.

    deletion.delete_item(file)   -> {"title", "deleted": [relative paths], "kind": "paper" | "book"}
    deletion.deleted_keys()      -> the index keys of papers you deleted (a later run shows them as "deleted by you" and never downloads them)
    deletion.delete_run(rid)     -> the run's title

A deleted paper or book:
  * its files are deleted from disk, not moved: the PDF (for a book its reading copy, the EPUB and its .book.json) and everything that belongs to it
    (notes and highlights, summary, card, skim, conversation: notes_store.sidecars), and its passages in the search index (state/index/<sha1>.*);
  * it leaves every folder and loses its reading status, and the page forgets where you stopped reading it;
  * its row in Papers/_index.csv stays, with status "deleted" and no file: that is how a weekly check or an archive search knows not to fetch it again.
    Adding it yourself (drop or import) replaces the row, as for any paper.
"""
import csv
import glob
import os
import re
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import download as dl  # noqa: E402

DELETED = "deleted"
_lock = threading.Lock()


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
    with _lock:
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
        # the row first (a crash after this leaves files nobody lists, not a listed paper without its PDF)
        row["file"], row["status"] = "", DELETED
        row["note"] = "deleted by you on %s" % time.strftime("%Y-%m-%d")
        tmp = dl.tmp_path(idx)
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        os.replace(tmp, idx)
        deleted = []
        for p in files:
            try:
                os.remove(p)
                deleted.append(os.path.relpath(p, root))
            except FileNotFoundError:
                pass
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
        return {"title": row.get("title") or os.path.basename(file), "deleted": deleted, "kind": kind}


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

