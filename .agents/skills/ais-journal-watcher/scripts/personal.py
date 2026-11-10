"""What to read next, and how well a new paper fits what you read (L5 reading queue, Q7 personalised screening). Everything is computed on this computer with the local embedding model: free, nothing is sent anywhere.

Your taste is learned from what you do, not from what you say: papers you finished, starred or marked up pull the profile toward them (a paper you opened and kept reading counts a little);
papers you skipped from the queue push it away. A paper's "fit" is how close its title, topics and takeaway are to that profile, shown as high / medium / lower within the list you are looking at.

    personal.queue(rows, n=8)            -> [{file, why, fit, score, folder}]   the next papers to read: your "to read" list first, then the newest and most relevant unread ones
    personal.fits(items)                 -> {id: {"score", "band"}} | {}        for candidates that are not in the library yet (the weekly check's borderline review): [{id, title, abstract, topics}]
    personal.profile_info(rows)          -> {"pos": n, "neg": n, "ready": bool}

The vectors are kept in personal_vecs.bin in this computer's own folder (paths.LOCAL: machine data, never synced, made again when missing) and, while the app runs, as one
float32 matrix in memory. See _Store. (Versions before this one kept them in personal_vecs.json there: it is read once, see _Store.carry, and never written or removed.)
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

CACHE = os.path.join(dl.LOCAL, "personal_vecs.bin")      # a 256-byte description, then one fixed-size record per text; new texts are appended
OLD = os.path.join(dl.LOCAL, "personal_vecs.json")       # what versions before this one wrote ({key: [floats]}): read once into CACHE when there is no CACHE yet, and left as it is
HDR = 256
KEEP_DAYS = 60                # a vector nothing has asked for since the app started is forgotten when it is older than this (see _Store.prune)
MIN_POS = 3                   # papers needed before a profile means anything
FOLDER_Z = 1.8                # a paper is suggested for a folder only when it is this many standard deviations closer to it than the library's other papers
_lock = threading.RLock()


def _model_id():
    c = ai.config_data()["embeddings"]
    return "%s:%s" % (c["provider"], c["model"])


def _today():
    return (date.today() - date(2020, 1, 1)).days


def _sig():
    """What tells that CACHE is still the file this process last read or wrote (None: there is no file)."""
    try:
        s = os.stat(CACHE)
        return (s.st_ino, s.st_size, s.st_mtime_ns)
    except OSError:
        return None


class _Store:
    """The vectors of every text met so far. In memory: mat (n, dim) float32, pos {key: row of mat}, day (n,) the day each was made, hit (n,) asked for since the app started.
    On disk (CACHE): {"v": 1, "model", "dim"} as JSON padded to HDR bytes, then records (key 20 bytes, day uint16, dim float32). A key is the start of the SHA-1 of the text, and the
    file says which embedding model made the vectors, so an entry can never be out of date: it is either the vector of that text or absent. Every method is called with _lock held."""

    def __init__(self):
        self.clear()

    def clear(self):
        """Forget what is in memory (the file stays): the next call reads it again."""
        self.loaded, self.sig, self.model, self.dim, self.pos, self.old = False, None, None, None, {}, False
        self.mat = self.day = self.hit = None

    def _rec(self, dim):
        import numpy as np
        return np.dtype([("k", "S20"), ("d", "<u2"), ("v", "<f4", (dim,))])

    def _set(self, keys, mat, day):
        import numpy as np
        self.dim, self.mat, self.day, self.hit = mat.shape[1], mat, day, np.zeros(len(keys), dtype=bool)
        self.pos = {k: i for i, k in enumerate(keys)}                    # (a key written twice, by two processes: the later record counts)

    def ready(self):
        """Read the file if this process has not, or if it is no longer the file it read or wrote last (another process wrote it), or if Settings now names another embedding model.
        A file that was deleted meanwhile changes nothing: what is in memory is still right, and the next new vector writes the file again from it."""
        model, sig = _model_id(), _sig()
        if self.loaded and model == self.model and (sig == self.sig or sig is None):
            return self
        import numpy as np
        asked = {k for k, i in self.pos.items() if self.hit[i]} if self.hit is not None and model == self.model else ()     # (another process wrote the file: what this one asked for since it started is still in use)
        for again in (True, True, False):
            self.clear()
            self.loaded, self.model = True, model
            self.old = sig is None and os.path.exists(OLD)               # (no file yet, but the one of earlier versions: vectors() hands it to carry() with the first texts asked for)
            if sig is None:
                break
            try:
                with open(CACHE, "rb") as f:
                    s = os.fstat(f.fileno())
                    sig = (s.st_ino, s.st_size, s.st_mtime_ns)           # the file that is read, which is not the one looked at above when another process put a new one in its place in between
                    h = json.loads(f.read(HDR).decode("utf-8"))
                    if h.get("v") == 1 and h.get("model") == model and isinstance(h.get("dim"), int) and 0 < h["dim"] < 100000:
                        rec = self._rec(h["dim"])
                        buf = f.read()
                        a = np.frombuffer(buf, dtype=rec, count=len(buf) // rec.itemsize)                    # (a record cut short at the end is not counted; add() removes it)
                        self._set([k.decode("ascii", "replace") for k in a["k"].tolist()], np.ascontiguousarray(a["v"]), a["d"].copy())
            except (OSError, ValueError, AttributeError):                 # unreadable, or not this format: start again (add() replaces the file)…
                self.clear()
                self.loaded, self.model = True, model
                now = _sig()
                if again and now != sig:                                  # …unless it is another file by now (replaced or removed while it was read): that one is read
                    sig = now
                    continue
            self.sig = sig
            break
        if asked and self.hit is not None:
            self.hit[[i for k, i in self.pos.items() if k in asked]] = True
        return self

    def carry(self, keys, texts):
        """The JSON file of earlier versions, once (when there is no CACHE yet): it did not say which model made its vectors, so one of the texts asked for now is embedded with the model
        in Settings and compared with what the file has for it. The same: its vectors (those of the most common length) are written to CACHE. Not the same, unreadable, or none of these
        texts in it: nothing is taken from it and the vectors are made again. The old file itself is not changed or removed."""
        import numpy as np
        from collections import Counter
        try:
            d = dl.load_json(OLD, {})
            d = {k: v for k, v in d.items() if isinstance(v, list) and v and len(k) == 20} if isinstance(d, dict) else {}
            dim = Counter(len(v) for v in d.values()).most_common(1)[0][0] if d else 0
            probe = next(((k, t) for k, t in zip(keys, texts) if len(d.get(k) or ()) == dim), None) if d else None
        except (OSError, ValueError, TypeError):
            probe = None                                                   # unreadable: nothing is kept from it
        if probe:
            now = _rounded(ai.embed([probe[1]], kind="passage"))          # (outside the try: if the model does not answer, or answers badly, this raises whatever the error is, and the old file is looked at again next time)
            try:
                was = np.array([d[probe[0]]], dtype="float32")
                if now.shape == was.shape and float(np.abs(now - was).max()) <= 0.002:
                    got = [k for k, v in d.items() if len(v) == dim]
                    self.add(got, np.array([d[k] for k in got], dtype="float32"))
            except (OSError, ValueError, TypeError):
                pass
        self.old = False

    def _write(self, rows):
        """Replace the file by the description and these rows of memory (a new file, a new model, or pruning)."""
        import numpy as np
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        head = json.dumps({"v": 1, "model": self.model, "dim": self.dim}).encode("utf-8")
        tmp = dl.tmp_path(CACHE)
        with open(tmp, "wb") as f:
            f.write(head.ljust(HDR - 1) + b"\n")
            if len(rows):
                rec = np.zeros(len(rows), dtype=self._rec(self.dim))
                by_row = {i: k for k, i in self.pos.items()}
                rec["k"], rec["d"], rec["v"] = [by_row[i].encode("ascii") for i in rows], self.day[rows], self.mat[rows]
                f.write(rec.tobytes())
        os.replace(tmp, CACHE)
        self.sig = _sig()

    def add(self, keys, new):
        """Append new vectors (float32 (m, dim)): only they are written, the rest of the file is not touched."""
        import numpy as np
        if self.dim is not None and new.shape[1] != self.dim:             # vectors of another length under the same model name: what is kept is of no use
            self.dim = self.mat = None
        if self.dim is None:
            self._set([], np.zeros((0, new.shape[1]), dtype="float32"), np.zeros(0, dtype="<u2"))
            self._write([])                                               # (a new file, or in place of one that could not be used)
        elif _sig() is None:
            self._write(sorted(self.pos.values()))                        # the file was deleted while the app ran: write what is in memory again
        rec = np.zeros(len(keys), dtype=self._rec(self.dim))
        rec["k"], rec["d"], rec["v"] = [k.encode("ascii") for k in keys], min(_today(), 65535), new
        moved = _sig() != self.sig                                        # another process wrote the file since this one read it (while the model was working)
        size = os.path.getsize(CACHE)
        whole = HDR + (size - HDR) // rec.itemsize * rec.itemsize
        if whole != size:
            os.truncate(CACHE, whole)                                     # a record cut short by a crash while appending
        with open(CACHE, "ab") as f:
            f.write(rec.tobytes())
        n = len(self.mat)
        self.mat, self.day, self.hit = np.concatenate([self.mat, new]), np.concatenate([self.day, rec["d"]]), np.concatenate([self.hit, np.ones(len(keys), dtype=bool)])
        self.pos.update((k, n + i) for i, k in enumerate(keys))
        self.sig = None if moved else _sig()                              # (None: the next ready() reads the whole file, theirs and this one's, and prune() does nothing until then)

    def prune(self):
        """Forget the vectors that nothing has asked for since the app started and that are older than KEEP_DAYS (texts that were edited or deleted, candidates of old runs), once they
        are a fifth of the file: the file is then written again without them. Returns how many went."""
        import numpy as np
        if not self.loaded or self.mat is None or not len(self.mat) or _sig() != self.sig:
            return 0
        live = np.zeros(len(self.mat), dtype=bool)
        live[list(self.pos.values())] = True
        keep = live & (self.hit | (_today() - self.day.astype("int64") <= KEEP_DAYS))
        gone = len(self.mat) - int(keep.sum())
        if gone < max(50, len(self.mat) // 5):
            return 0
        rows = np.flatnonzero(keep)
        self._write([int(i) for i in rows])
        by_row = {i: k for k, i in self.pos.items()}
        hit = self.hit[rows]
        self._set([by_row[int(i)] for i in rows], np.ascontiguousarray(self.mat[rows]), self.day[rows])
        self.hit = hit
        return gone


_mem = _Store()


def _rounded(arr):
    """What ai.embed gave, with 4 decimals, as float32 (n, dim): the numbers the JSON file of earlier versions kept, so the ranking is the same as before."""
    import numpy as np
    return np.array([[round(float(x), 4) for x in v] for v in arr], dtype="float32").reshape(len(arr), -1)


def vectors(texts):
    """(n, d) array of normalised vectors (a copy: the caller may change it), with the cache doing most of the work. They are kept as float32 and handed out
    as float64: sums and products of float32 come out a hair differently on different computers (each has its own fast arithmetic library), enough to move a
    rounded fit or a suggested folder at a threshold; in float64 the difference is far below anything that is rounded."""
    import numpy as np
    with _lock:
        st = _mem.ready()
        keys = [hashlib.sha1(t.encode()).hexdigest()[:20] for t in texts]
        if st.old and keys:
            st.carry(keys, texts)
        while True:
            todo = sorted({(k, t) for k, t in zip(keys, texts) if k not in st.pos})
            if not todo:
                break
            arr = ai.embed([t for _, t in todo], kind="passage")
            st.add([k for k, _ in todo], _rounded(arr))
        if not keys:
            return np.zeros((0, 1), dtype="float32")
        idx = [st.pos[k] for k in keys]
        st.hit[idx] = True
        return st.mat[idx].astype("float64")


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
    # Papers whose scores are equal to six decimals count as equal and keep the order they came in. Without that, which of several equally close papers
    # were the last "high" ones was left to the last digit of the arithmetic, which differs from one computer to the next (macOS and Linux disagreed).
    order = np.argsort(-np.round(np.asarray(scores, dtype="float64"), 6), kind="stable")
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


def _days(r, today=None):
    try:
        return ((today or date.today()) - datetime.strptime((r.get("date_checked") or "")[:10], "%Y-%m-%d").date()).days
    except ValueError:
        return 999


def queue(rows, n=8):
    """The next papers to read. Candidates: not read, not being read, not skipped. Order: papers you marked "to read", then by the check's relevance score (newer first), nudged by how close they are to what you read."""
    cands = [r for r in rows if (r.get("state") or {}).get("status", "") in ("", "toread") and not (r.get("state") or {}).get("skip") and not (r.get("state") or {}).get("opened")]
    if not cands:
        return {"items": [], "profile": profile_info(rows)}
    import numpy as np
    allv = vectors([row_text(r) for r in rows])                           # one look-up for the whole library; the candidates are rows of it
    at = {id(r): i for i, r in enumerate(rows)}
    cv = allv[[at[id(r)] for r in cands]]
    prof, fit = profile(rows), {}
    if prof["pos"] is not None:
        s = _score(cv, prof)
        fit = {r["file"]: (float(x), b) for r, x, b in zip(cands, s, _bands(s))}
    # the folder a paper is closest to (only for papers in no folder): how far its similarity to a folder's centroid stands above the other papers' (titles are short, so raw cosines are all high)
    names, folder_of = {f["id"]: f["name"] for f in fo.list_all()}, {}
    members = {}
    for i, r in enumerate(rows):
        for fid in r.get("folders") or []:
            members.setdefault(fid, []).append(i)
    for fid in names:
        mem = sorted(set(members.get(fid, ())))
        if len(mem) < 2:
            continue
        c = allv[mem].sum(axis=0)
        c = c / (np.linalg.norm(c) or 1.0)
        mask = np.ones(len(rows), dtype=bool)
        mask[mem] = False
        rest = (allv @ c)[mask].astype("float64")                         # one product for the whole library instead of one per paper
        if len(rest) >= 8 and rest.std() > 1e-6:
            folder_of[fid] = (c, float(rest.mean()), float(rest.std()))
    out, today = [], date.today()
    for j, r in enumerate(cands):
        st, days = r.get("state") or {}, _days(r, today)
        base = float(r.get("score") or 0) + (40 if st.get("status") == "toread" else 0) + (15 if st.get("star") else 0) + max(0, 14 - days) * 0.3
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
        if days <= 7:
            why.append("new this week")
        folder = None
        if folder_of and not r.get("folders"):
            v = cv[j]
            zs = sorted(((float((v @ c) - mu) / sd, fid) for fid, (c, mu, sd) in folder_of.items()), reverse=True)
            if zs and zs[0][0] >= FOLDER_Z and (len(zs) < 2 or zs[0][0] - zs[1][0] >= 0.5):
                folder = {"id": zs[0][1], "name": names[zs[0][1]], "z": round(zs[0][0], 1)}
        out.append((base, {"file": r["file"], "title": r.get("title", ""), "journal": r.get("journal", ""), "year": r.get("year", ""), "topics": r.get("matched_topics", ""), "score": r.get("score", ""),
                           "status": st.get("status", ""), "why": "; ".join(why) or "in your library, not read yet", "fit": band, "folder": folder, "days": days}))
    out.sort(key=lambda x: -x[0])
    with _lock:
        _mem.prune()
    return {"items": [o for _, o in out[:n]], "total": len(out), "profile": profile_info(rows)}
