"""Comparison matrix (Q2): papers as rows, questions as columns, every cell pointing at the page it comes from.

Two kinds of columns:
  built-in   read straight from the paper cards (research question, design, sample, findings, limitations…): free, always up to date, and
             each cell carries the pages its card line cites. Edit them in the reader's Card tab.
  custom     your own question ("Sample size?", "Which industry?", "Were moderators tested?"), answered per paper by the model from the
             paper's closest passages, one call per paper, with the passage's page and opening words as the source. "Not reported" is a
             valid answer. You can overwrite any custom cell yourself (marked ✎) and clear it to have it filled again.

The matrix is saved as Papers/_folders/<id>.matrix.json (columns, custom definitions, custom cells; keyed by paper key so it survives moves).

    matrix.table(fid, rows) -> {doc, columns:[{id, name, builtin, prompt?}], rows:[{key, file, title, cite, card, cells:{col: {text, cites, status?, edited?}}}]}
    matrix.save(fid, client, base_rev)                 columns / custom definitions / cell edits (rev-guarded)
    for m in matrix.fill(fid, rows): ...               fill the empty custom cells (3 papers at a time)
    matrix.csv(...) markdown(...) docx(...) latex(...) exports; matrix.estimate_fill(...)
"""
import csv
import io
import json
import os
import re
import sys
import threading
import traceback
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import activity as act  # noqa: E402
import ai  # noqa: E402
import blocks as bl  # noqa: E402
import download as dl  # noqa: E402
import embed  # noqa: E402
import evidence as evd  # noqa: E402
import folders as fo  # noqa: E402
import notes_store as ns  # noqa: E402
import papercard as pc  # noqa: E402

BUILTIN = [("question", "Research question"), ("kind", "Type"), ("topics", "Topics"), ("theories", "Theory"), ("constructs", "Constructs"), ("hypotheses", "Hypotheses"),
           ("design", "Design"), ("data", "Data"), ("sample", "Sample"), ("analysis", "Analysis"), ("findings", "Findings"), ("contribution", "Contribution"),
           ("implications", "Implications"), ("limitations", "Limitations"), ("future", "Future research")]
BUILTIN_IDS = [b[0] for b in BUILTIN]
DEFAULT_COLUMNS = ["question", "design", "sample", "findings", "limitations"]
MAX_COLUMNS, MAX_CUSTOM = 16, 12
_lock = threading.RLock()


def _o(**props):
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


FILL_SCHEMA = _o(cells={"type": "array", "items": _o(column={"type": "string"}, answer={"type": "string"}, cites={"type": "array", "items": {"type": "integer"}})})
FILL_SYSTEM = """You fill cells of a literature comparison table from numbered passages of ONE paper.
For every column, answer its question in at most 30 words, using only the passages: plain, specific, with numbers where the paper gives them. Prefer a partial answer to none: give what the passages do say (for example the number of campaigns, firms or participants when asked for a sample size). Answer exactly "Not reported" only when none of the passages bears on the question (never guess or use outside knowledge).
cites lists the numbers of the passages that support the answer (none for "Not reported"). "column" is the column's id exactly as given. Output only the JSON."""


# ---------------------------------------------------------------- the file

def path(fid):
    return os.path.join(fo.DIR, fid + ".matrix.json")


def _empty():
    return {"version": 1, "rev": 0, "columns": list(DEFAULT_COLUMNS), "custom": [], "cells": {}}


def load(fid):
    d = dl.load_json(path(fid), None) if os.path.exists(path(fid)) else None
    if not isinstance(d, dict):
        return _empty()
    out = _empty()
    out.update({k: v for k, v in d.items() if k in out})
    return out


def _write(fid, doc):
    os.makedirs(fo.DIR, exist_ok=True)
    tmp = path(fid) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path(fid))


def _s(v, n):
    return re.sub(r"\s+", " ", str(v or "")).strip()[:n]


def save(fid, client, base_rev):
    """What the matrix lets you change: which columns are shown and in what order, your custom columns (add / rename / delete), and the text of custom cells
    ({"edits": {paper key: {column id: text}}}; empty text clears the cell). Stale rev raises notes_store.Conflict."""
    if not fo.get(fid):
        raise KeyError("no such folder")
    if not isinstance(client, dict):
        raise ValueError("bad matrix")
    with _lock:
        cur = load(fid)
        if cur["rev"] != base_rev:
            raise ns.Conflict(cur)
        if isinstance(client.get("custom"), list):
            keep = {c["id"]: c for c in cur["custom"]}
            custom = []
            for c in client["custom"][:MAX_CUSTOM]:
                if not isinstance(c, dict) or not _s(c.get("name"), 60):
                    continue
                old = keep.get(c.get("id"))
                new = {"id": old["id"] if old else "c" + uuid.uuid4().hex[:6], "name": _s(c["name"], 60), "prompt": _s(c.get("prompt") or c["name"], 400)}
                if old and (old["prompt"] != new["prompt"]):
                    for cells in cur["cells"].values():          # a changed question: the old answers no longer answer it
                        cells.pop(old["id"], None)
                custom.append(new)
            gone = set(keep) - {c["id"] for c in custom}
            for cells in cur["cells"].values():
                for g in gone:
                    cells.pop(g, None)
            cur["custom"] = custom
            if "columns" not in client:
                cur["columns"] = [c for c in cur["columns"] if c in BUILTIN_IDS or c in {x["id"] for x in custom}]
        if isinstance(client.get("columns"), list):
            ok = set(BUILTIN_IDS) | {c["id"] for c in cur["custom"]}
            cur["columns"] = [c for c in dict.fromkeys(client["columns"]) if c in ok][:MAX_COLUMNS]
        for key, cols in (client.get("edits") or {}).items():
            if not isinstance(cols, dict):
                continue
            for col, text in cols.items():
                if col not in {c["id"] for c in cur["custom"]}:
                    continue
                if not _s(text, 600):
                    cur["cells"].get(key, {}).pop(col, None)
                else:
                    old = cur["cells"].setdefault(key, {}).get(col) or {}
                    cur["cells"][key][col] = {"text": _s(text, 600), "cites": old.get("cites", []), "status": "ok", "edited": True}
        cur["rev"] += 1
        _write(fid, cur)
        return cur


def add_custom(fid, name, prompt, base_rev):
    """Add a custom column and show it. Returns the new doc (stale rev raises Conflict)."""
    if not fo.get(fid):
        raise KeyError("no such folder")
    with _lock:
        cur = load(fid)
        if cur["rev"] != base_rev:
            raise ns.Conflict(cur)
        if len(cur["custom"]) >= MAX_CUSTOM:
            raise ValueError("at most %d custom columns" % MAX_CUSTOM)
        c = {"id": "c" + uuid.uuid4().hex[:6], "name": _s(name, 60), "prompt": _s(prompt or name, 400)}
        if not c["name"]:
            raise ValueError("a column needs a name")
        cur["custom"].append(c)
        if len(cur["columns"]) < MAX_COLUMNS:
            cur["columns"].append(c["id"])
        cur["rev"] += 1
        _write(fid, cur)
        return cur


# ---------------------------------------------------------------- built-in cells, read from the cards

def _pages(card, cite_lists):
    out, seen = [], set()
    for cs in cite_lists:
        for c in cs or []:
            p = (card.get("passages") or {}).get(str(c))
            if p and p["page"] not in seen:
                seen.add(p["page"]); out.append({"page": p["page"], "quote": p["quote"]})
    return sorted(out, key=lambda x: x["page"])[:4]


def card_cell(card, col):
    """{text, cites} for a built-in column from a card; None when the card says nothing there."""
    d = card["data"]
    lines, cites = [], []

    def one(x, fmt):
        t = fmt(x)
        if t:
            lines.append(t); cites.append(x.get("cites"))
    if col == "question":
        one(d["question"], lambda x: x["text"])
    elif col == "kind":
        lines.append(d["kind"])
    elif col == "topics":
        lines.append(", ".join(d["topics"]))
    elif col == "theories":
        for x in d["theories"]:
            one(x, lambda x: "%s (%s)" % (x["name"], x["role"]))
    elif col == "constructs":
        for x in d["constructs"]:
            one(x, lambda x: "%s: %s" % (x["name"], x["definition"]) if x["definition"] else x["name"])
    elif col == "hypotheses":
        for x in d["hypotheses"]:
            one(x, lambda x: "%s %s — %s" % (x["label"], x["text"], x["verdict"]))
    elif col in ("design", "data", "sample", "analysis"):
        if d["method"][col]:
            lines.append(d["method"][col]); cites.append(d["method"]["cites"])
    elif col == "findings":
        for x in d["findings"]:
            one(x, lambda x: x["text"])
    elif col == "contribution":
        one(d["contribution"], lambda x: x["text"])
    elif col == "implications":
        for x in d["implications"]:
            one(x, lambda x: x["text"])
    elif col == "limitations":
        for x in d["limitations"]:
            one(x, lambda x: x["text"] + ("" if x.get("stated", True) else " (inferred)"))
    elif col == "future":
        for x in d["future_research"]:
            one(x, lambda x: x["text"])
    lines = [l for l in lines if l]
    return {"text": "\n".join(lines), "cites": _pages(card, cites)} if lines else None


def table(fid, rows):
    doc = load(fid)
    custom = {c["id"]: c for c in doc["custom"]}
    cols = [{"id": c, "name": dict(BUILTIN)[c], "builtin": True} if c in BUILTIN_IDS else {"id": c, "name": custom[c]["name"], "builtin": False, "prompt": custom[c]["prompt"]}
            for c in doc["columns"] if c in BUILTIN_IDS or c in custom]
    out, missing = [], 0
    for r in rows:
        pdf = ns.pdf_path(r.get("file"))
        card = pc.load(pdf) if pdf and r.get("card") else None
        missing += 0 if card else 1
        mine = doc["cells"].get(r.get("key"), {})
        cells = {}
        for c in cols:
            cell = (card_cell(card, c["id"]) if card else None) if c["builtin"] else mine.get(c["id"])
            if cell:
                cells[c["id"]] = cell
        out.append({"key": r.get("key"), "file": r.get("file"), "title": r.get("title", ""), "cite": evd.short_cite(r), "year": r.get("year", ""), "journal": r.get("journal", ""),
                    "doi": r.get("doi", ""), "card": bool(card), "cells": cells})
    return {"doc": doc, "columns": cols, "rows": out, "missing_cards": missing, "builtin": [{"id": i, "name": n} for i, n in BUILTIN]}


# ---------------------------------------------------------------- custom columns: one model call per paper

def _fill_paper(r, pdf, cols):
    blocks = bl.extract(pdf)
    chosen = {}
    for c in cols:
        for b in embed.search(pdf, "%s: %s" % (c["name"], c["prompt"]), k=7):
            chosen[b["idx"]] = b
    card = pc.load(pdf)
    if card:                    # the paragraphs the card cites for its method (design, data, sample) answer most "sample / data" columns
        for c in card["data"]["method"]["cites"][:4]:
            p = (card.get("passages") or {}).get(str(c))
            if p and p["idx"] < len(blocks) and blocks[p["idx"]]["idx"] == p["idx"]:
                chosen.setdefault(p["idx"], blocks[p["idx"]])
    order = sorted(chosen.values(), key=lambda b: b["idx"])
    if not order:
        raise ai.AIError("No text could be read from this PDF (a scanned image?).")
    body = "\n\n".join("[%d] (p. %d) %s" % (i + 1, b["page"] + 1, b["text"]) for i, b in enumerate(order))
    asks = "\n".join("- id %s — %s: %s" % (c["id"], c["name"], c["prompt"]) for c in cols)
    prompt = "Paper: %s\n\nColumns to fill:\n%s\n\nPassages:\n\n%s" % (r.get("title", ""), asks, body)
    res = ai.chat("matrix", [{"role": "user", "content": prompt}], system=FILL_SYSTEM, schema=FILL_SCHEMA, max_tokens=700 + 300 * len(cols), cache=True,
                  cache_if=lambda t: isinstance(pc._json(t), dict) and isinstance(pc._json(t).get("cells"), list))
    d = pc._json(res.text)
    if not isinstance(d, dict) or not isinstance(d.get("cells"), list):
        raise ai.AIError("The model’s answer wasn’t valid JSON (it may have been cut off). Try again.")
    by = {str(x.get("column")): x for x in d["cells"] if isinstance(x, dict)}
    cells = {}
    for c in cols:
        x = by.get(c["id"]) or by.get(c["name"]) or {}
        text = _s(x.get("answer"), 400)
        nr = (not text) or text.lower().startswith("not reported")
        cites = []
        for n in (x.get("cites") if isinstance(x.get("cites"), list) else []):
            try:
                b = order[int(n) - 1]
            except (ValueError, TypeError, IndexError):
                continue
            if int(n) >= 1 and all(c2["page"] != b["page"] + 1 for c2 in cites):
                cites.append({"page": b["page"] + 1, "quote": b["text"] if len(b["text"]) <= 130 else b["text"][:130].rsplit(" ", 1)[0]})
        cells[c["id"]] = {"text": "Not reported" if nr else text, "cites": [] if nr else cites[:3], "status": "not reported" if nr else "ok",
                          "model": "%s · %s" % (res.provider, res.model), "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    return cells, res


def _missing(doc, rows, col_ids, fresh):
    custom = {c["id"]: c for c in doc["custom"]}
    want = [custom[c] for c in (col_ids or doc["columns"]) if c in custom]
    todo = []
    for r in rows:
        pdf = ns.pdf_path(r.get("file"))
        if not pdf:
            continue
        have = doc["cells"].get(r.get("key"), {})
        need = [c for c in want if fresh and not (have.get(c["id"]) or {}).get("edited") or c["id"] not in have]
        if need:
            todo.append((r, pdf, need))
    return todo


def estimate_fill(fid, rows, col_ids=None, fresh=False):
    """{cells, papers, cost}: what filling the empty custom cells involves."""
    todo = _missing(load(fid), rows, col_ids, fresh)
    cells = sum(len(n) for _, _, n in todo)
    chars = sum(1500 + 900 * min(len(n) * 7, 20) for _, _, n in todo)
    return {"cells": cells, "papers": len(todo), "cost": ai.estimate("matrix", chars, sum(150 + 80 * len(n) for _, _, n in todo)) if todo else 0.0}


def fill(fid, rows, col_ids=None, fresh=False, workers=3):
    """Messages: start{total, cells}, paper{file, key, cells, done, total, cost}, error{file, error}, done{made, cost}. Cells you wrote yourself are never overwritten."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    try:
        todo = _missing(load(fid), rows, col_ids, fresh)
        yield {"type": "start", "total": len(todo), "cells": sum(len(n) for _, _, n in todo)}
        ex = ThreadPoolExecutor(max_workers=workers)
        cost, done = 0.0, 0
        try:
            futs = {ex.submit(act.wrap(_fill_paper), r, pdf, need): r for r, pdf, need in todo}
            for f in as_completed(futs):
                r = futs[f]
                try:
                    cells, res = f.result()
                except ai.AIError as e:
                    yield {"type": "error", "file": r["file"], "title": r.get("title", ""), "error": str(e)}
                    if re.search(r"budget|isn’t set up|Local-only|No provider|Cancelled", str(e)):
                        break
                    continue
                except Exception as e:  # noqa: BLE001
                    traceback.print_exc()
                    yield {"type": "error", "file": r["file"], "title": r.get("title", ""), "error": re.sub(r"\s+", " ", str(e))[:160]}
                    continue
                with _lock:
                    cur = load(fid)
                    mine = cur["cells"].setdefault(r["key"], {})
                    for cid, cell in cells.items():
                        if not (mine.get(cid) or {}).get("edited"):
                            mine[cid] = cell
                    cur["rev"] += 1
                    _write(fid, cur)
                done += 1; cost += res.cost or 0
                yield {"type": "paper", "file": r["file"], "key": r["key"], "cells": cells, "done": done, "total": len(todo), "cost": res.cost}
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
        yield {"type": "done", "made": done, "cost": cost, "rev": load(fid)["rev"]}
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        yield {"type": "error", "error": re.sub(r"\s+", " ", str(e))[:200]}


# ---------------------------------------------------------------- export

def _txt(cell, pages=True):
    if not cell:
        return ""
    t = cell["text"].replace("\n", "; ")
    pg = sorted({c["page"] for c in cell.get("cites", [])})
    return t + (" (p. %s)" % ", ".join(map(str, pg)) if pages and pg else "")


def _title(fid):
    return (fo.get(fid) or {}).get("name", "Comparison")


def csv_bytes(fid, rows):
    t = table(fid, rows)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Paper", "Title", "Year", "Journal", "DOI"] + [c["name"] for c in t["columns"]])
    for r in t["rows"]:
        w.writerow([r["cite"], r["title"], r["year"], r["journal"], r["doi"]] + [_txt(r["cells"].get(c["id"])) for c in t["columns"]])
    return ("\ufeff" + out.getvalue()).encode("utf-8")           # the BOM makes Excel read it as UTF-8


def markdown(fid, rows):
    t = table(fid, rows)
    esc = lambda s: s.replace("|", "\\|").replace("\n", " ")  # noqa: E731
    o = "# %s — comparison\n\n| Paper | %s |\n|---|%s\n" % (_title(fid), " | ".join(esc(c["name"]) for c in t["columns"]), "---|" * len(t["columns"]))
    for r in t["rows"]:
        o += "| %s | %s |\n" % (esc("%s — %s" % (r["cite"], r["title"][:60])), " | ".join(esc(_txt(r["cells"].get(c["id"]))) for c in t["columns"]))
    return o


def docx_bytes(fid, rows):
    from docx import Document
    from docx.enum.section import WD_ORIENT
    from docx.shared import Pt
    t = table(fid, rows)
    d = Document()
    sec = d.sections[0]
    sec.orientation = WD_ORIENT.LANDSCAPE
    sec.page_width, sec.page_height = sec.page_height, sec.page_width
    d.add_heading("%s — comparison" % _title(fid), 1)
    tb = d.add_table(rows=1, cols=1 + len(t["columns"]))
    tb.style = "Table Grid"
    for i, h in enumerate(["Paper"] + [c["name"] for c in t["columns"]]):
        tb.rows[0].cells[i].text = ""
        tb.rows[0].cells[i].paragraphs[0].add_run(h).bold = True
    for r in t["rows"]:
        cells = tb.add_row().cells
        cells[0].text = "%s\n%s" % (r["cite"], r["title"])
        for i, c in enumerate(t["columns"]):
            cells[i + 1].text = _txt(r["cells"].get(c["id"])).replace("; ", "\n")
    for row in tb.rows:
        for cell in row.cells:
            for p in cell.paragraphs:
                for run in p.runs:
                    run.font.size = Pt(8)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _tex(s):
    s = (s or "").replace("\\", "\\textbackslash{}")
    s = re.sub(r"([&%$#_{}])", r"\\\1", s)
    return s.replace("~", "\\textasciitilde{}").replace("^", "\\textasciicircum{}")


def latex(fid, rows):
    """A booktabs/tabularx table; the first column cites the paper with \\citet{key} (the keys of this folder's BibTeX export)."""
    import exports
    t = table(fid, rows)
    try:
        keys = {e["row"].get("key"): e["key"] for e in exports.entries([r for r in rows if r.get("key")])}
    except Exception:  # noqa: BLE001  (no network for the Crossref lookup): the keys are still derived from the library data
        keys = {}
    n = len(t["columns"])
    o = ["% Needs: \\usepackage{booktabs,tabularx,array}. Keys match this folder's BibTeX export. For many columns use landscape (pdflscape) or drop a column.",
         "\\begin{table}[ht]", "\\centering\\scriptsize", "\\begin{tabularx}{\\textwidth}{>{\\raggedright\\arraybackslash}p{2.2cm}%s}" % (">{\\raggedright\\arraybackslash}X" * n), "\\toprule",
         "Paper & %s \\\\" % " & ".join(_tex(c["name"]) for c in t["columns"]), "\\midrule"]
    for r in t["rows"]:
        first = "\\citet{%s}" % keys[r["key"]] if r["key"] in keys else _tex(r["cite"])
        o.append("%s & %s \\\\" % (first, " & ".join(_tex(_txt(r["cells"].get(c["id"]))) for c in t["columns"])))
    o += ["\\bottomrule", "\\end{tabularx}", "\\caption{%s}" % _tex(_title(fid)), "\\end{table}", ""]
    return "\n".join(o)
