"""Everything the AI wrote or cached, in one place: what it is, how big, and a way to clear it. Plus a backup of your own data.

    aifiles.scan()              # [{id, name, what, human, regen, count, bytes, note}] for the Settings → AI files table
    aifiles.clear(kind)         # delete one kind; anything that is not freely regenerable is zipped first into state/trash/ (restore with aifiles.restore)
    aifiles.restore(name)       # put the files of a trash zip back (never over a file that exists again)
    aifiles.trash_list()
    aifiles.backup(include_pdfs) -> path of a temporary zip with your notes, highlights, cards, folders, tags, status, settings… (not the PDFs unless asked, never .env)

Highlights and notes (<paper>.notes.json) are never touched here: they are yours, not the AI's.
"""
import glob
import json
import os
import re
import shutil
import sys
import tempfile
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import download as dl  # noqa: E402

ROOT, PAPERS, STATE, BASE, LOCAL = dl.ROOT, dl.PAPERS, dl.STATE, dl.BASE, dl.LOCAL
TRASH = os.path.join(STATE, "trash")
KEEP_TRASH = 12


def _g(*parts, rec=False):
    return sorted(glob.glob(os.path.join(*parts), recursive=rec))


def _papers_files(suffix):
    return [p for p in _g(PAPERS, "**", "*" + suffix, rec=True) if os.sep + "_folders" + os.sep not in p and os.sep + "_removed" + os.sep not in p]


def kinds():
    """The kinds of AI file, each with the files it covers right now. human = holds things you may have edited or decided (so a zip is made before clearing)."""
    folders = _g(PAPERS, "_folders", "*.ask.json") + _g(PAPERS, "_folders", "*.matrix.json") + _g(PAPERS, "_folders", "*.ideas.json") + _g(PAPERS, "_folders", "*.draft.json") + _g(PAPERS, "_folders", "*.claims.json") + _g(PAPERS, "_folders", "*.synthesis.json") + _g(PAPERS, "_folders", "*.review.json") + _g(PAPERS, "_ask_library.json")
    return [
        {"id": "cards", "name": "Paper cards", "what": "One structured summary per paper (<name>.card.json next to the PDF). The Evidence tab, the matrix and the Ideas Lab read them; you can edit any line.", "human": True, "files": _papers_files(".card.json")},
        {"id": "skims", "name": "Skims", "what": "The sentences the AI marked in a paper, and which ones you dismissed (<name>.skim.json). Highlights you accepted stay: they are in your notes.", "human": True, "files": _papers_files(".skim.json")},
        {"id": "chats", "name": "Conversations about one paper", "what": "The Ask tab's history for each paper (<name>.chat.json).", "human": True, "files": _papers_files(".chat.json")},
        {"id": "folders", "name": "Folder conversations, matrices, idea boards, drafts, claim checks and syntheses", "what": "Ask history, comparison matrices (with the cells you wrote), Ideas Lab boards (with your stars and notes), related-work drafts, claim checks and syntheses, kept in Papers/_folders. The folders themselves stay.", "human": True, "files": folders},
        {"id": "old", "name": "Earlier versions of cards and summaries", "what": "The previous card or summary, kept whenever one is written again.", "human": False, "files": [p for d in ("card_backups", "summary_backups") for p in _g(STATE, d, "*")]},
        {"id": "cache", "name": "Saved answers", "what": "Every AI answer is kept, so asking exactly the same thing again is free and instant. Clearing it only makes repeats cost again.", "human": False, "regen": True, "files": _g(STATE, "ai_cache", "*")},
        {"id": "index", "name": "Search index for Ask", "what": "The paper text split into passages, and their vectors. It is read again in the background (free with the local model).", "human": False, "regen": True, "files": _g(STATE, "index", "*")},
        {"id": "lookups", "name": "OpenAlex and author lookups", "what": "What was fetched about citations, related work and authors, so the same lookup is not repeated.", "human": False, "regen": True, "files": [p for n in ("openalex_cache.json", "authors_cache.json", "crossref_cache.json") for p in _g(LOCAL, n)]},
    ]


def _note(kind):
    if kind["id"] == "cards":
        n = 0
        for p in kind["files"]:
            try:
                n += bool(json.dumps(json.load(open(p, encoding="utf-8")).get("data", {})).count('"edited": true'))
            except (OSError, ValueError):
                pass
        return "%d with lines you edited" % n if n else ""
    if kind["id"] == "folders":
        ideas = sum(1 for p in kind["files"] if p.endswith(".ideas.json"))
        return "%d idea board%s" % (ideas, "" if ideas == 1 else "s") if ideas else ""
    return ""


def scan():
    out = []
    for k in kinds():
        size = sum(os.path.getsize(p) for p in k["files"] if os.path.exists(p))
        out.append({"id": k["id"], "name": k["name"], "what": k["what"], "human": k["human"], "regen": bool(k.get("regen")), "count": len(k["files"]), "bytes": size, "note": _note(k)})
    return out


def _arc(path):
    return os.path.relpath(path, ROOT)


def clear(kind_id):
    k = next((x for x in kinds() if x["id"] == kind_id), None)
    if not k:
        raise KeyError("unknown kind")
    files = [p for p in k["files"] if os.path.isfile(p)]
    if not files:
        return {"removed": 0, "bytes": 0, "backup": None}
    backup = None
    if not k.get("regen"):                       # a zip first, for everything that cannot simply be made again for free
        os.makedirs(TRASH, exist_ok=True)
        backup = "%s-%s.zip" % (time.strftime("%Y%m%d-%H%M%S"), kind_id)
        with zipfile.ZipFile(os.path.join(TRASH, backup), "w", zipfile.ZIP_DEFLATED) as z:
            for p in files:
                z.write(p, _arc(p))
        for old in sorted(glob.glob(os.path.join(TRASH, "*.zip")))[:-KEEP_TRASH]:
            os.remove(old)
    size = 0
    for p in files:
        size += os.path.getsize(p)
        os.remove(p)
    return {"removed": len(files), "bytes": size, "backup": backup}


def trash_list():
    out = []
    for p in sorted(glob.glob(os.path.join(TRASH, "*.zip")), reverse=True):
        name = os.path.basename(p)
        m = re.match(r"(\d{8})-(\d{6})-(\w+)\.zip$", name)
        try:
            with zipfile.ZipFile(p) as z:
                n = len(z.namelist())
        except zipfile.BadZipFile:
            continue
        out.append({"name": name, "kind": m.group(3) if m else "", "when": time.strftime("%Y-%m-%d %H:%M", time.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")) if m else "", "files": n, "bytes": os.path.getsize(p)})
    return out


def restore(name):
    path = os.path.join(TRASH, os.path.basename(name))
    if not name.endswith(".zip") or not os.path.isfile(path):
        raise KeyError("no such backup")
    done = skipped = 0
    with zipfile.ZipFile(path) as z:
        for m in z.namelist():
            dest = os.path.realpath(os.path.join(ROOT, m))
            if not (dest.startswith(os.path.realpath(PAPERS) + os.sep) or dest.startswith(os.path.realpath(STATE) + os.sep) or dest.startswith(os.path.realpath(LOCAL) + os.sep)):
                continue                           # only ever back into Papers/ or state/
            if os.path.exists(dest):
                skipped += 1
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with z.open(m) as src, open(dest, "wb") as out:
                out.write(src.read())
            done += 1
    return {"restored": done, "skipped": skipped}


# ---------------------------------------------------------------- a backup of your own data

def _data_files(include_pdfs):
    out = []
    for dp, dn, fn in os.walk(PAPERS):
        for f in fn:
            if f.endswith(".pdf") and not include_pdfs:
                continue
            if f.startswith(".") or f.endswith(".tmp"):
                continue
            out.append(os.path.join(dp, f))
    for n in ("settings.json", "ui_prefs.json", "prompts.json", "library.json", "interests.json", "journals.json", "ai.json"):
        p = os.path.join(BASE, n)
        if os.path.exists(p):
            out.append(p)
    p = os.path.join(STATE, "ai_usage.jsonl")
    if os.path.exists(p):
        out.append(p)
    return out


def backup_size():
    pdf = other = 0
    for p in _data_files(True):
        try:
            sz = os.path.getsize(p)
        except OSError:
            continue
        if p.endswith(".pdf"):
            pdf += sz
        else:
            other += sz
    return {"data_bytes": other, "pdf_bytes": pdf, "files": len(_data_files(False))}


# ---------------------------------------------------------------- a snapshot of your own data, taken by itself once a day (kept on this computer, not in the synced folder)

SNAPSHOTS = os.path.join(LOCAL, "snapshots")
KEEP_SNAPSHOTS = 14
SNAP_RE = re.compile(r"jw-snapshot-\d{8}-\d{6}(?:-before-restore)?\.zip")


def snapshots():
    """The snapshots on this computer, newest first: [{name, when, bytes, before_restore}]."""
    out = []
    if os.path.isdir(SNAPSHOTS):
        for n in sorted(os.listdir(SNAPSHOTS), reverse=True):
            if SNAP_RE.fullmatch(n):
                p = os.path.join(SNAPSHOTS, n)
                out.append({"name": n, "when": "%s-%s-%s %s:%s" % (n[12:16], n[16:18], n[18:20], n[21:23], n[23:25]), "bytes": os.path.getsize(p), "before_restore": "before-restore" in n})
    return out


def snapshot(force=False, tag=""):
    """Keep a zip of your own files (notes, highlights, folders, tags, reading status, the library index, settings; no PDFs) in this computer's own folder, once a
    day (force: now). The oldest beyond KEEP_SNAPSHOTS are removed. Returns the snapshot's name, or None when today's is already there or there is nothing to keep."""
    today = time.strftime("%Y%m%d")
    if not force and any(s["name"][12:20] == today and not s["before_restore"] for s in snapshots()):
        return None
    if not _data_files(False):
        return None
    os.makedirs(SNAPSHOTS, exist_ok=True)
    name = "jw-snapshot-%s%s.zip" % (time.strftime("%Y%m%d-%H%M%S"), "-before-restore" if tag else "")
    n = 2
    while os.path.exists(os.path.join(SNAPSHOTS, name)):          # (two in the same second)
        time.sleep(1.0 / n)
        name = "jw-snapshot-%s%s.zip" % (time.strftime("%Y%m%d-%H%M%S"), "-before-restore" if tag else "")
        n += 1
    tmp = backup(False)
    shutil.move(tmp, os.path.join(SNAPSHOTS, name))
    for old in [s["name"] for s in snapshots()][KEEP_SNAPSHOTS:]:
        try:
            os.remove(os.path.join(SNAPSHOTS, old))
        except OSError:
            pass
    return name


def restore_snapshot(name):
    """Put the files of a snapshot back as they were then. What is there now is kept first, in a snapshot of its own (so a restore can be undone by restoring
    that one). Only files of the kinds a snapshot holds are written, only inside the data folder, never a PDF; files made since the snapshot are left alone."""
    if not SNAP_RE.fullmatch(str(name or "")) or not os.path.exists(os.path.join(SNAPSHOTS, name)):
        raise KeyError("no such snapshot")
    before = snapshot(force=True, tag="before-restore")
    root = os.path.realpath(ROOT)
    ok_dirs = [os.path.realpath(PAPERS) + os.sep, os.path.realpath(STATE) + os.sep]
    ok_files = {os.path.realpath(os.path.join(BASE, n)) for n in ("settings.json", "ui_prefs.json", "prompts.json", "library.json", "interests.json", "journals.json", "ai.json")}
    done = 0
    with zipfile.ZipFile(os.path.join(SNAPSHOTS, name)) as z:
        for m in z.infolist():
            if m.is_dir() or m.filename == "README-backup.txt" or m.filename.lower().endswith(".pdf") or m.file_size > 200 * 1024 * 1024:
                continue
            dest = os.path.realpath(os.path.join(root, m.filename))
            if not (any(dest.startswith(d) for d in ok_dirs) or dest in ok_files):
                continue                           # (only ever into the data folder's own places)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            tmp = dest + ".restore.tmp"
            with z.open(m) as src, open(tmp, "wb") as out:
                shutil.copyfileobj(src, out)
            os.replace(tmp, dest)
            done += 1
    return {"restored": done, "before": before}


def backup(include_pdfs=False):
    fd, path = tempfile.mkstemp(prefix="jw-backup-", suffix=".zip")
    os.close(fd)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED if not include_pdfs else zipfile.ZIP_STORED) as z:
        for p in _data_files(include_pdfs):
            try:
                z.write(p, _arc(p))
            except OSError:
                continue
        z.writestr("README-backup.txt", "Journal Watcher backup (%s)\n\nWhat is in it: your notes and highlights, paper cards, skims, conversations, folders (with matrices and idea boards), tags, reading status,\n"
                   "the library index (_index.csv), settings, prompts and the AI model settings (ai.json, which holds no API keys)%s.\nWhat is not: your .env file (API keys), cached answers and search indexes (they are rebuilt).\n\n"
                   "To restore: unzip it over the project folder (the paths inside are relative to it).\n" % (time.strftime("%Y-%m-%d %H:%M"), ", and the PDFs" if include_pdfs else "; the PDFs are not included (they stay in Papers/)"))
    return path
