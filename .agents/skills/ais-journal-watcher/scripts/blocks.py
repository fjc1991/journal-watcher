"""Paragraph blocks of a PDF, in reading order: the unit the AI features retrieve and cite.

    blocks.extract(pdf_path) -> [{idx, page (0-based), text, bbox [x0,y0,x1,y1] in PDF points, page_w, page_h, kind}]
    kind is "body", "ref" (bibliography) or "meta" (editor / DOI / licence lines); retrieval only uses "body".

Uses PyMuPDF (AGPL: fine for this local tool; swap for pdfminer.six if the app is ever distributed). Two-column pages are
read left column then right, running headers/footers and page numbers are dropped, line-end hyphens are joined, and
very long paragraphs are split at sentence ends. Cached at state/index/<sha1 of the PDF>.blocks.json (regenerable).
"""
import json
import os
import re
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import download as dl  # noqa: E402
import notes_store as ns  # noqa: E402

INDEX = os.path.join(dl.STATE, "index")        # shared with the other computers that use the data folder: a paper is read into the index once
LIG = {"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl", "­": "", " ": " "}
MAX_CHARS = 1400      # longer paragraphs are split
MIN_CHARS = 25        # shorter ones (page numbers, stray labels) are dropped
VERSION = 4           # bump when extraction changes: older caches are rebuilt
REF_START = re.compile(r"^(?:\d+[.)]?\s*)?(?:references|bibliography|works cited|literature cited)\s+(?:[A-Z][\w'’\-]+,\s+[A-Z]|\[?\d+\]?[.)]?\s|[A-Z][\w'’\-]+\s+[A-Z]{1,3}[.,\s(])", re.I)
APPENDIX = re.compile(r"^(?:online )?appendi(?:x|ces)\b|^about the authors?\b|^biograph(?:y|ies)\b|^author (?:bio|information)", re.I)
REF_HEAD = re.compile(r"(?:\d+[.)]?\s*)?(?:references|bibliography|works cited|literature cited)\s*:?", re.I)
META = re.compile(r"accepting senior editor|associate editor|^doi\b|©|copyright|all rights reserved|creative commons|licen[sc]e", re.I)


def _clean(t):
    t = re.sub(r"[ﬁﬂﬀﬃﬄ­ ]", lambda m: LIG[m.group()], t)
    t = re.sub(r"(\w)-\n(?=[a-z])", r"\1", t)         # "inno-\nvation" -> "innovation"
    return re.sub(r"\s+", " ", t).strip()


def _columns(blocks, W):
    """Reading order of one page's blocks: a block that straddles the middle ends a two-column stretch, and within a
    stretch the left column is read before the right one. Single-column pages come out top to bottom."""
    out, seg = [], []

    def flush():
        left = [b for b in seg if b["x1"] <= 0.56 * W]
        right = [b for b in seg if b not in left]
        out.extend(sorted(left, key=lambda b: b["y0"]) + sorted(right, key=lambda b: b["y0"]))
        seg.clear()

    for b in sorted(blocks, key=lambda b: (b["y0"], b["x0"])):
        if b["x0"] < 0.44 * W and b["x1"] > 0.56 * W:        # straddles the centre: full-width block
            flush(); out.append(b)
        else:
            seg.append(b)
    flush()
    return out


def _split(text):
    if len(text) <= MAX_CHARS:
        return [text]
    parts, cur = [], ""
    for s in re.split(r"(?<=[.!?])\s+(?=[A-Z(\[])", text):
        if cur and len(cur) + len(s) > 900:
            parts.append(cur); cur = s
        else:
            cur = (cur + " " + s).strip()
    return parts + [cur] if cur else parts


def extract(pdf_path, force=False):
    cache = os.path.join(INDEX, ns.sha1_of(pdf_path) + ".blocks.json")
    if not force and os.path.exists(cache):
        d = dl.load_json(cache, None)
        if d and isinstance(d.get("blocks"), list) and d.get("v") == VERSION:      # (also an empty list: a scan has no text, and is not parsed again each time to find that out)
            return d["blocks"]
    import pymupdf
    pages = []
    with pymupdf.open(pdf_path) as doc:
        for page in doc:
            W, H = page.rect.width, page.rect.height
            bl = [{"x0": b[0], "y0": b[1], "x1": b[2], "y1": b[3], "t": _clean(b[4])} for b in page.get_text("blocks") if b[6] == 0]
            pages.append({"W": W, "H": H, "b": [b for b in bl if b["t"]]})
    # running headers/footers: the same text (digits ignored) in the top/bottom margin of many pages
    seen = {}
    for pg in pages:
        for b in pg["b"]:
            if b["y1"] < 0.09 * pg["H"] or b["y0"] > 0.91 * pg["H"]:
                seen.setdefault(re.sub(r"\d+", "#", b["t"].lower()), set()).add(id(pg))
    junk = {k for k, v in seen.items() if len(v) >= max(3, 0.3 * len(pages))}
    out, pending_ref = [], False
    for pno, pg in enumerate(pages):
        keep = [b for b in pg["b"] if re.sub(r"\d+", "#", b["t"].lower()) not in junk and not re.fullmatch(r"(page )?\d+( of \d+)?", b["t"].lower())]
        ordered = _columns(keep, pg["W"])
        i = 0
        while i < len(ordered):
            b = dict(ordered[i])
            if REF_HEAD.fullmatch(b["t"].strip()):          # a "References" heading on its own line: the list starts with the next block
                pending_ref = True; i += 1; continue
            # a short line without a full stop is a heading: keep it with the paragraph that follows
            if len(b["t"]) < 90 and not b["t"].endswith((".", "?", "!", ":", ";", ",")) and i + 1 < len(ordered) and not REF_HEAD.fullmatch(ordered[i + 1]["t"].strip()):
                n = ordered[i + 1]
                b = {"x0": min(b["x0"], n["x0"]), "y0": min(b["y0"], n["y0"]), "x1": max(b["x1"], n["x1"]), "y1": max(b["y1"], n["y1"]),
                     "t": b["t"] + " " + n["t"]}
                i += 1
            i += 1
            if len(b["t"]) < MIN_CHARS:
                continue
            for part in _split(b["t"]):
                out.append({"idx": len(out), "page": pno, "text": part, "bbox": [round(b[k], 1) for k in ("x0", "y0", "x1", "y1")],
                            "page_w": round(pg["W"], 1), "page_h": round(pg["H"], 1), **({"refstart": True} if pending_ref else {})})
                pending_ref = False
    # kind: "ref" = the bibliography (until an appendix), "meta" = editor/DOI/licence lines on the first and last pages, else "body"
    in_refs, last = False, out[-1]["page"] if out else 0
    for b in out:
        if b.get("refstart") or REF_START.match(b["text"]):
            in_refs = True
        elif in_refs and APPENDIX.match(b["text"]):
            in_refs = False
        b["kind"] = "ref" if in_refs else "meta" if (b["page"] <= 1 or b["page"] == last) and len(b["text"]) < 400 and META.search(b["text"]) else "body"
    os.makedirs(INDEX, exist_ok=True)
    tmp = dl.tmp_path(cache)                            # (the indexer and an Ask may both write, here or on another computer)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"v": VERSION, "sha1": ns.sha1_of(pdf_path), "blocks": out}, f, ensure_ascii=False)
    os.replace(tmp, cache)
    return out
