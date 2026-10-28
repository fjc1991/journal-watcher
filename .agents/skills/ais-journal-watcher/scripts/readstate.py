"""Reading state of each paper, shared by the Library: status (toread / reading / read), star, a one-line takeaway,
when it was last opened and how far you got. One small file, Papers/_status.json, keyed by download.index_key (DOI,
else title) so it survives a re-download; nothing is added next to the PDFs.

    readstate.load()                    # {key: {status, star, takeaway, opened, progress, page}}
    readstate.update(key, {"status": "read", "star": True})
    readstate.touch_open(key, page, total)   # opening a paper: stamp it and start "reading" if it had no status
"""
import json
import os
import sys
import threading
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import download as dl  # noqa: E402

PATH = os.path.join(dl.PAPERS, "_status.json")
STATUSES = ("", "toread", "reading", "read")
_lock = threading.RLock()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load():
    d = dl.load_json(PATH, {}) if os.path.exists(PATH) else {}
    return d.get("papers", {}) if isinstance(d, dict) else {}


def _save(papers):
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "papers": papers}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PATH)


def update(key, patch):
    """Set status / star / skip / takeaway / progress / page / opened for one paper; empty entries are removed."""
    if not key:
        raise ValueError("no paper")
    with _lock:
        papers = load()
        e = dict(papers.get(key) or {})
        if "status" in patch:
            if patch["status"] not in STATUSES:
                raise ValueError("bad status")
            e["status"] = patch["status"]
        if "star" in patch:
            e["star"] = bool(patch["star"])
        if "skip" in patch:                                  # "not for me": leaves the Up next list and teaches the fit
            e["skip"] = bool(patch["skip"])
        if "takeaway" in patch:
            e["takeaway"] = str(patch["takeaway"] or "")[:400]
        if "progress" in patch:
            e["progress"] = max(0.0, min(1.0, float(patch["progress"])))
        if "page" in patch:
            e["page"] = max(1, int(patch["page"]))
        if patch.get("opened"):
            e["opened"] = _now()
        e = {k: v for k, v in e.items() if v not in ("", None, False)}
        if e:
            papers[key] = e
        else:
            papers.pop(key, None)
        _save(papers)
        return e


def touch_open(key, page=None, total=None):
    with _lock:
        cur = load().get(key) or {}
        patch = {"opened": True}
        if not cur.get("status"):
            patch["status"] = "reading"
        if page and total:
            patch.update(page=page, progress=page / total)
        return update(key, patch)
