"""Per-paper semantic index: embed every block once, then find the passages closest to a question.

    embed.index_paper(pdf_path, progress=None)      # no-op if already indexed with the current embedding model
    embed.search(pdf_path, "question", k=12)        # -> blocks (see blocks.py) + "score", best first

Vectors are cached next to the block cache in state/index/ (shared with the other computers that use the data folder; <sha1>.vec = a float32 .npy, <sha1>.vec.json = which model made
them), so the Papers/ folder is never touched and the index can be rebuilt any time.
"""
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ai  # noqa: E402
import blocks as bl  # noqa: E402
import download as dl  # noqa: E402
import notes_store as ns  # noqa: E402


def _paths(pdf_path):
    base = os.path.join(bl.INDEX, ns.sha1_of(pdf_path))
    return base + ".vec", base + ".vec.json"


def _model_id():
    c = ai.config_data()["embeddings"]
    return "%s:%s" % (c["provider"], c["model"])


def _valid(vec, meta, n):
    """True if the saved vectors are for the current embedding model and match the n passages: the files may have come from another computer through the shared folder, half way through
    a sync, or from another model, and a wrong shape would only fail later, inside a search."""
    import numpy as np
    m = dl.load_json(meta, {})
    if not (os.path.exists(vec) and m.get("model") == _model_id() and m.get("n") == n):
        return False
    try:
        a = np.load(vec, mmap_mode="r")
        return a.ndim == 2 and a.shape[0] == n and (not m.get("dim") or a.shape[1] == m["dim"])
    except Exception:  # noqa: BLE001      (unreadable or still arriving: make it again)
        return False


def index_paper(pdf_path, progress=None):
    """Make (or find) the vectors of one paper. Returns {"n", "cached"} and, when work was done, "extract_s", "embed_s" and "workers" (what the tray shows, so a slow Mac can be understood)."""
    import numpy as np
    t0 = time.time()
    blocks = bl.extract(pdf_path)
    t1 = time.time()
    vec, meta = _paths(pdf_path)
    if _valid(vec, meta, len(blocks)):
        return {"n": len(blocks), "cached": True}
    arr = ai.embed([b["text"] for b in blocks], progress=progress) if blocks else np.zeros((0, 1), dtype="float32")
    t2 = time.time()
    os.makedirs(bl.INDEX, exist_ok=True)
    tmp = dl.tmp_path(vec)
    with open(tmp, "wb") as f:
        np.save(f, arr)
    os.replace(tmp, vec)
    tmp = dl.tmp_path(meta)
    with open(tmp, "w") as f:
        json.dump({"model": _model_id(), "n": len(blocks), "dim": int(arr.shape[1])}, f)
    os.replace(tmp, meta)                 # (the description last: a reader that finds it finds finished vectors)
    return {"n": len(blocks), "cached": False, "extract_s": round(t1 - t0, 2), "embed_s": round(t2 - t1, 2),
            "workers": ai.embed_workers() if ai.config_data()["embeddings"]["provider"] == "local" else 1}


def is_indexed(pdf_path):
    vec, meta = _paths(pdf_path)
    m = dl.load_json(meta, {})
    return os.path.exists(vec) and m.get("model") == _model_id()


def search(pdf_path, query, k=12, pages=None):
    """pages: (first, last) file pages, 1-based and both included: only passages there can be found (one chapter of a book)."""
    import numpy as np
    index_paper(pdf_path)
    blocks = bl.extract(pdf_path)
    if not blocks:
        return []
    arr = np.load(_paths(pdf_path)[0])
    q = ai.embed([query], kind="query")[0]
    scores = arr @ q
    scores[[i for i, b in enumerate(blocks) if b.get("kind", "body") != "body"]] = -1.0     # never answer from references or licence lines
    if pages:
        scores[[i for i, b in enumerate(blocks) if not pages[0] - 1 <= b["page"] <= pages[1] - 1]] = -1.0
    top = [i for i in np.argsort(-scores)[:k] if scores[i] > -1.0]
    return [{**blocks[int(i)], "score": float(scores[int(i)])} for i in top]
