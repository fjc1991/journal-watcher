"""A folder's workspace (X1): one board for a whole folder. Pull in what matters from many papers (your highlights, lines of the paper cards, whole papers), write notes between them,
group them in labelled frames and draw arrows. Every excerpt keeps its paper and page, so it opens the paper where it came from; the board exports as Markdown or Word with the cites.

Saved as Papers/_folders/<id>.workspace.json (yours: part of the backup, never written by the AI):
    {"version": 1, "rev": n, "cards": [{id, type: note|excerpt|paper, x, y, w, h, text, note, file, page, quote, kind, created}], "frames": [{id, x, y, w, h, label, color}], "links": [{id, a, b, label}]}

    workspace.load(fid) / save(fid, doc, base_rev) / sources(fid, rows) / groups(doc) / markdown(doc, name, rows) / docx(doc, name, rows)
"""
import json
import os
import re
import sys
import threading
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import evidence as evd  # noqa: E402
import folders as fo  # noqa: E402
import notes_store as ns  # noqa: E402
import papercard as pc  # noqa: E402
import relatedwork as rw  # noqa: E402

_lock = threading.RLock()
TYPES = ("note", "excerpt", "paper")
MAX_CARDS, MAX_FRAMES, MAX_LINKS = 600, 120, 1200


class Conflict(Exception):
    def __init__(self, current):
        super().__init__("stale rev")
        self.current = current


def path(fid):
    return os.path.join(fo.DIR, fid + ".workspace.json")


def _empty():
    return {"version": 1, "rev": 0, "cards": [], "frames": [], "links": []}


def load(fid):
    d = None
    if os.path.exists(path(fid)):
        try:
            d = json.load(open(path(fid), encoding="utf-8"))
        except (OSError, ValueError):
            d = None
    if not isinstance(d, dict) or d.get("version") != 1:
        return _empty()
    for k in ("cards", "frames", "links"):
        d[k] = d.get(k) if isinstance(d.get(k), list) else []
    d["rev"] = int(d.get("rev") or 0)
    return d


def _num(x, lo, hi, default):
    try:
        return max(lo, min(hi, float(x)))
    except (TypeError, ValueError):
        return default


def _s(x, n):
    return str(x or "")[:n]


def clean(doc):
    """What may be stored: known keys only, sane numbers, files that are really in Papers/."""
    out = {"cards": [], "frames": [], "links": []}
    ids = set()
    for c in (doc.get("cards") if isinstance(doc.get("cards"), list) else [])[:MAX_CARDS]:
        if not isinstance(c, dict) or c.get("type") not in TYPES or not c.get("id") or str(c["id"]) in ids:
            continue
        f = c.get("file")
        card = {"id": _s(c["id"], 40), "type": c["type"], "x": round(_num(c.get("x"), 0, 20000, 20)), "y": round(_num(c.get("y"), 0, 20000, 20)), "w": round(_num(c.get("w"), 160, 800, 280)),
                "h": round(_num(c.get("h"), 0, 4000, 0)), "text": _s(c.get("text"), 20000), "note": _s(c.get("note"), 20000), "created": _s(c.get("created"), 40)}
        if f and ns.safe_papers_path(f):
            card["file"] = _s(f, 400)
            card["page"] = int(_num(c.get("page"), 1, 5000, 1)) if c.get("page") else None
            card["quote"] = _s(c.get("quote"), 300)
            card["kind"] = _s(c.get("kind"), 40)
        ids.add(card["id"])
        out["cards"].append(card)
    for f in (doc.get("frames") if isinstance(doc.get("frames"), list) else [])[:MAX_FRAMES]:
        if isinstance(f, dict) and f.get("id"):
            out["frames"].append({"id": _s(f["id"], 40), "x": round(_num(f.get("x"), 0, 20000, 20)), "y": round(_num(f.get("y"), 0, 20000, 20)), "w": round(_num(f.get("w"), 120, 6000, 420)),
                                  "h": round(_num(f.get("h"), 80, 6000, 280)), "label": _s(f.get("label"), 80), "color": _s(f.get("color"), 12) or "#5b7fa6"})
    for l in (doc.get("links") if isinstance(doc.get("links"), list) else [])[:MAX_LINKS]:
        if isinstance(l, dict) and l.get("id") and l.get("a") in ids and l.get("b") in ids and l["a"] != l["b"]:
            out["links"].append({"id": _s(l["id"], 40), "a": l["a"], "b": l["b"], "label": _s(l.get("label"), 60)})
    return out


def save(fid, doc, base_rev):
    if not fo.get(fid):
        raise KeyError("no such folder")
    if not isinstance(doc, dict):
        raise ValueError("bad workspace")
    with _lock:
        cur = load(fid)
        if cur["rev"] != int(base_rev or 0):
            raise Conflict(cur)
        out = {"version": 1, "rev": cur["rev"] + 1, **clean(doc)}
        if not out["cards"] and not out["frames"]:
            if os.path.exists(path(fid)):
                os.remove(path(fid))
            return {**_empty(), "rev": out["rev"]}
        os.makedirs(fo.DIR, exist_ok=True)
        tmp = path(fid) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path(fid))
        return out


# ---------------------------------------------------------------- what can be pulled onto the board

def sources(rows):
    """Per paper of the folder: your highlights (with their notes), the lines of its paper card, and its takeaway, each with the page: what the board's tray lists."""
    out = []
    for r in rows:
        pdf = ns.pdf_path(r.get("file"))
        if not pdf:
            continue
        notes = ns.load(pdf)
        hls = [{"id": h.get("id"), "text": h.get("text", ""), "comment": h.get("comment", ""), "color": h.get("color", ""), "page": min((p["page"] for p in h.get("parts") or [{"page": 1}]), default=1)}
               for h in notes.get("highlights", []) if h.get("kind", "highlight") in ("highlight", "underline") and (h.get("text") or "").strip()]
        hls.sort(key=lambda h: (h["page"], h["text"]))
        card = pc.load(pdf)
        lines = []
        if card:
            for kind, text, pg, quote in rw.notes_of(card):
                lines.append({"kind": re.sub(r"\s*\(.*", "", kind).split(",")[0], "text": text, "page": pg, "quote": quote})
        out.append({"file": r["file"], "cite": evd.short_cite(r), "title": r.get("title", ""), "takeaway": (r.get("state") or {}).get("takeaway", ""), "highlights": hls, "lines": lines})
    return out


# ---------------------------------------------------------------- reading it as a document (export)

def groups(doc):
    """Your frames first (with the cards inside them, top to bottom), then cards that are in no frame, by paper. [{title, frame, cards}]"""
    cards, used, out = doc["cards"], set(), []
    for f in sorted(doc["frames"], key=lambda f: (f["y"], f["x"])):
        cs = [c for c in cards if c["id"] not in used and f["x"] <= c["x"] + c["w"] / 2 <= f["x"] + f["w"] and f["y"] <= c["y"] + (c["h"] or 100) / 2 <= f["y"] + f["h"]]
        cs.sort(key=lambda c: (c["y"], c["x"]))
        used.update(c["id"] for c in cs)
        out.append({"title": f["label"] or "Untitled group", "frame": True, "cards": cs})
    rest = sorted([c for c in cards if c["id"] not in used], key=lambda c: (c.get("file") or "~", c.get("page") or 0, c["y"], c["x"]))
    mine = [c for c in rest if not c.get("file")]
    if mine:
        out.append({"title": "My notes", "frame": False, "cards": mine})
    byfile = {}
    for c in rest:
        if c.get("file"):
            byfile.setdefault(c["file"], []).append(c)
    for f, cs in byfile.items():
        out.append({"title": f, "frame": False, "file": f, "cards": cs})
    return out


def _cite(c, cites):
    if not c.get("file"):
        return ""
    return "%s%s" % (cites.get(c["file"], "a paper"), ", p. %d" % c["page"] if c.get("page") else "")


def _cites(rows):
    return {r["file"]: evd.short_cite(r) for r in rows}


def markdown(doc, name, rows):
    cites, byid = _cites(rows), {c["id"]: c for c in doc["cards"]}
    L = ["# Workspace%s" % (" — " + name if name else ""), ""]
    for g in groups(doc):
        title = cites.get(g.get("file"), g["title"]) if g.get("file") else g["title"]
        L += ["## %s" % title, ""]
        for c in g["cards"]:
            to = ["  - %s%s" % ("*%s* → " % l["label"] if l["label"] else "→ ", re.sub(r"\s+", " ", (byid[l["b"]]["text"] or byid[l["b"]]["note"])[:60]).strip()) for l in doc["links"] if l["a"] == c["id"] and l["b"] in byid]
            cite = _cite(c, cites)
            if c["type"] == "excerpt":
                L.append("- > %s%s%s" % (re.sub(r"\s+", " ", c["text"]), " (%s)" % cite if cite else "", ("\n  " + c["note"].replace("\n", "\n  ")) if c["note"] else ""))
            elif c["type"] == "paper":
                L.append("- **%s**%s%s" % (re.sub(r"\s+", " ", c["text"]), " (%s)" % cite if cite else "", ("\n  " + c["note"].replace("\n", "\n  ")) if c["note"] else ""))
            else:
                L.append("- " + c["text"].replace("\n", "\n  "))
            L += to
        L.append("")
    return "\n".join(L)


def docx(doc, name, rows):
    import io
    from docx import Document
    cites, byid = _cites(rows), {c["id"]: c for c in doc["cards"]}
    d = Document()
    d.add_heading("Workspace" + (" — " + name if name else ""), 1)
    for g in groups(doc):
        d.add_heading(cites.get(g.get("file"), g["title"]) if g.get("file") else g["title"], 2)
        for c in g["cards"]:
            cite = _cite(c, cites)
            if c["type"] == "excerpt":
                p = d.add_paragraph(style="Intense Quote" if "Intense Quote" in [s.name for s in d.styles] else None)
                p.add_run("“%s”" % re.sub(r"\s+", " ", c["text"]))
                if cite:
                    p.add_run("  (%s)" % cite).italic = True
            elif c["type"] == "paper":
                p = d.add_paragraph()
                p.add_run(re.sub(r"\s+", " ", c["text"])).bold = True
                if cite:
                    p.add_run("  (%s)" % cite).italic = True
            else:
                d.add_paragraph(c["text"])
            if c.get("note"):
                d.add_paragraph(c["note"], style="List Bullet")
            for l in doc["links"]:
                if l["a"] == c["id"] and l["b"] in byid:
                    t = byid[l["b"]]
                    d.add_paragraph("%s%s" % ("%s → " % l["label"] if l["label"] else "→ ", re.sub(r"\s+", " ", (t["text"] or t["note"])[:80])), style="List Bullet")
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def new_id():
    return uuid.uuid4().hex[:10]


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
