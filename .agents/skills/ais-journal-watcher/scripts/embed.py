"""Per-paper semantic index: embed every block once, then find the passages closest to a question.

    embed.index_paper(pdf_path, progress=None)      # no-op if already indexed with the current embedding model
    embed.search(pdf_path, "question", k=12)        # -> blocks (see blocks.py) + "score", best first

Vectors are cached next to the block cache in state/index/ (<sha1>.vec = a float32 .npy, <sha1>.vec.json = which model made
them), so the Papers/ folder is never touched and the index can be rebuilt any time.
"""
import json
import os
import sys
import threading

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


def index_paper(pdf_path, progress=None):
    import numpy as np
    blocks = bl.extract(pdf_path)
    vec, meta = _paths(pdf_path)
    m = dl.load_json(meta, {})
    if os.path.exists(vec) and m.get("model") == _model_id() and m.get("n") == len(blocks):
        return {"n": len(blocks), "cached": True}
    out = []
    for i in range(0, len(blocks), 48):
        out.append(ai.embed([b["text"] for b in blocks[i:i + 48]]))
        if progress:
            progress(min(i + 48, len(blocks)), len(blocks))
    arr = np.concatenate(out) if out else np.zeros((0, 1), dtype="float32")
    os.makedirs(bl.INDEX, exist_ok=True)
    tmp = vec + ".%d.tmp" % threading.get_ident()          # (the background indexer and an Ask may both write)
    with open(tmp, "wb") as f:
        np.save(f, arr)
    os.replace(tmp, vec)
    with open(meta, "w") as f:
        json.dump({"model": _model_id(), "n": len(blocks), "dim": int(arr.shape[1])}, f)
    return {"n": len(blocks), "cached": False}


def is_indexed(pdf_path):
    vec, meta = _paths(pdf_path)
    m = dl.load_json(meta, {})
    return os.path.exists(vec) and m.get("model") == _model_id()


def search(pdf_path, query, k=12):
    import numpy as np
    index_paper(pdf_path)
    blocks = bl.extract(pdf_path)
    if not blocks:
        return []
    arr = np.load(_paths(pdf_path)[0])
    q = ai.embed([query], kind="query")[0]
    scores = arr @ q
    scores[[i for i, b in enumerate(blocks) if b.get("kind", "body") != "body"]] = -1.0     # never answer from references or licence lines
    top = [i for i in np.argsort(-scores)[:k] if scores[i] > -1.0]
    return [{**blocks[int(i)], "score": float(scores[int(i)])} for i in top]
