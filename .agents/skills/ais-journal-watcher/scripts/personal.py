"""What to read next, and how well a new paper fits what you read (L5 reading queue, Q7 personalised screening). Everything is computed on this computer with the local embedding model: free, nothing is sent anywhere.

Your taste is learned from what you do, not from what you say: papers you finished, starred or marked up pull the profile toward them (a paper you opened and kept reading counts a little);
papers you skipped from the queue push it away. A paper's "fit" is how close its title, topics and takeaway are to that profile, shown as high / medium / lower within the list you are looking at.

    personal.queue(rows, n=8)            -> [{file, why, fit, score, folder}]   the next papers to read: your "to read" list first, then the newest and most relevant unread ones
    personal.fits(items)                 -> {id: {"score", "band"}} | {}        for candidates that are not in the library yet (the weekly check's borderline review): [{id, title, abstract, topics}]
    personal.profile_info(rows)          -> {"pos": n, "neg": n, "ready": bool}

The vectors are cached in state/personal_vecs.json (machine data, rebuilt when missing).
"""
import hashlib
import json
import os
import re
import sys
import threading
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ai  # noqa: E402
import download as dl  # noqa: E402
import folders as fo  # noqa: E402

CACHE = os.path.join(dl.LOCAL, "personal_vecs.json")
MIN_POS = 3                   # papers needed before a profile means anything
FOLDER_Z = 1.8                # a paper is suggested for a folder only when it is this many standard deviations closer to it than the library's other papers
_lock = threading.RLock()
_mem = {}


def _load():
    if not _mem:
        d = dl.load_json(CACHE, {}) if os.path.exists(CACHE) else {}
        _mem.update(d if isinstance(d, dict) else {})
    return _mem


def _save():
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    tmp = CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_mem, f)
    os.replace(tmp, CACHE)


def vectors(texts):
    """float32 (n, d) array of normalised vectors, with the cache doing most of the work."""
    import numpy as np
    with _lock:
        mem = _load()
        keys = [hashlib.sha1(t.encode()).hexdigest()[:20] for t in texts]
        todo = sorted({(k, t) for k, t in zip(keys, texts) if k not in mem})
        if todo:
            arr = ai.embed([t for _, t in todo], kind="passage")
            for (k, _), v in zip(todo, arr):
                mem[k] = [round(float(x), 4) for x in v]
            _save()
        return np.array([mem[k] for k in keys], dtype="float32") if keys else np.zeros((0, 1), dtype="float32")


def row_text(r):
    st = r.get("state") or {}
    return re.sub(r"\s+", " ", " ".join(x for x in (r.get("title", ""), r.get("matched_topics", ""), st.get("takeaway", "")) if x)).strip()[:600]


def _taste(rows):
    """[(row, weight)] positives and negatives from what you did."""
    pos, neg = [], []
    for r in rows:
        st = r.get("state") or {}
        w = (1.0 if st.get("status") == "read" else 0.0) + (1.0 if st.get("star") else 0.0) + (0.7 if r.get("notes") else 0.0) + (0.4 if st.get("status") == "reading" and st.get("progress", 0) > 0.15 else 0.0)
        if w:
            pos.append((r, w))
        if st.get("skip"):
            neg.append((r, 1.0))
    return pos, neg


def _centroid(items):
    import numpy as np
    if not items:
        return None
    v = vectors([row_text(r) for r, _ in items]) * np.array([[w] for _, w in items], dtype="float32")
    c = v.sum(axis=0)
    n = float(np.linalg.norm(c))
    return c / n if n else None


def profile(rows):
    pos, neg = _taste(rows)
    return {"pos": _centroid(pos) if len(pos) >= MIN_POS else None, "neg": _centroid(neg) if len(neg) >= 2 else None, "n_pos": len(pos), "n_neg": len(neg)}


def profile_info(rows):
    pos, neg = _taste(rows)
    return {"pos": len(pos), "neg": len(neg), "ready": len(pos) >= MIN_POS}


def _score(vecs, prof):
    s = vecs @ prof["pos"]
    if prof["neg"] is not None:
        s = s - 0.5 * (vecs @ prof["neg"])
    return s


def _bands(scores):
    """high / medium / lower by thirds of this list (a rough, honest scale: it says which are closest to what you read)."""
    import numpy as np
    if not len(scores):
        return []
    order = np.argsort(-np.asarray(scores))
    out = [""] * len(scores)
    for rank, i in enumerate(order):
        out[i] = "high" if rank < len(scores) / 3 else "medium" if rank < 2 * len(scores) / 3 else "lower"
    return out


def fits(items, rows):
    """Fit of candidates that are not in the library (title + abstract start + topics) against your profile. {} while there is not enough to learn from."""
    prof = profile(rows)
    items = [x for x in items if isinstance(x, dict) and x.get("id") and x.get("title")][:400]
    if prof["pos"] is None or not items:
        return {}
    texts = [re.sub(r"\s+", " ", "%s %s %s" % (x["title"], " ".join(x.get("topics") or []), str(x.get("abstract") or "")[:300])).strip()[:600] for x in items]
    s = _score(vectors(texts), prof)
    return {x["id"]: {"score": round(float(sc), 3), "band": b} for x, sc, b in zip(items, s, _bands(s))}


def _days(r):
    try:
        return (date.today() - datetime.strptime((r.get("date_checked") or "")[:10], "%Y-%m-%d").date()).days
    except ValueError:
        return 999


def queue(rows, n=8):
    """The next papers to read. Candidates: not read, not being read, not skipped. Order: papers you marked "to read", then by the check's relevance score (newer first), nudged by how close they are to what you read."""
    cands = [r for r in rows if (r.get("state") or {}).get("status", "") in ("", "toread") and not (r.get("state") or {}).get("skip") and not (r.get("state") or {}).get("opened")]
    if not cands:
        return {"items": [], "profile": profile_info(rows)}
    prof, fit = profile(rows), {}
    if prof["pos"] is not None:
        s = _score(vectors([row_text(r) for r in cands]), prof)
        fit = {r["file"]: (float(x), b) for r, x, b in zip(cands, s, _bands(s))}
    # the folder a paper is closest to (only for papers in no folder): how far its similarity to a folder's centroid stands above the other papers' (titles are short, so raw cosines are all high)
    import numpy as np
    names, folder_of = {f["id"]: f["name"] for f in fo.list_all()}, {}
    allv = vectors([row_text(r) for r in rows]) if rows else None
    for fid in names:
        mem = [i for i, r in enumerate(rows) if fid in (r.get("folders") or [])]
        if len(mem) < 2 or allv is None:
            continue
        c = allv[mem].sum(axis=0)
        c = c / (np.linalg.norm(c) or 1.0)
        rest = np.array([float(allv[i] @ c) for i in range(len(rows)) if i not in mem])
        if len(rest) >= 8 and rest.std() > 1e-6:
            folder_of[fid] = (c, float(rest.mean()), float(rest.std()))
    out = []
    for r in cands:
        st = r.get("state") or {}
        base = float(r.get("score") or 0) + (40 if st.get("status") == "toread" else 0) + (15 if st.get("star") else 0) + max(0, 14 - _days(r)) * 0.3
        fs, band = fit.get(r["file"], (None, ""))
        if fs is not None:
            base += {"high": 5, "medium": 1, "lower": -2}.get(band, 0)
        why = []
        if st.get("status") == "toread":
            why.append("you marked it to read")
        if r.get("matched_topics"):
            why.append("matches " + r["matched_topics"].replace(";", ","))
        if band == "high":
            why.append("close to what you read")
        if _days(r) <= 7:
            why.append("new this week")
        folder = None
        if folder_of and not r.get("folders"):
            v = vectors([row_text(r)])[0]
            zs = sorted(((float((v @ c) - mu) / sd, fid) for fid, (c, mu, sd) in folder_of.items()), reverse=True)
            if zs and zs[0][0] >= FOLDER_Z and (len(zs) < 2 or zs[0][0] - zs[1][0] >= 0.5):
                folder = {"id": zs[0][1], "name": names[zs[0][1]], "z": round(zs[0][0], 1)}
        out.append((base, {"file": r["file"], "title": r.get("title", ""), "journal": r.get("journal", ""), "year": r.get("year", ""), "topics": r.get("matched_topics", ""), "score": r.get("score", ""),
                           "status": st.get("status", ""), "why": "; ".join(why) or "in your library, not read yet", "fit": band, "folder": folder, "days": _days(r)}))
    out.sort(key=lambda x: -x[0])
    return {"items": [o for _, o in out[:n]], "total": len(out), "profile": profile_info(rows)}
