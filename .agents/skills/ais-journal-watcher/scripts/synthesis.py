"""Synthesis (Q3 map, Q4 gaps, Q8 digest): what a folder of papers says as a whole, from the papers' cards.

The model reads the cards' numbered notes (the same ids as the related-work drafter: "P2-3" = the third note of paper P2) and returns JSON:
    overview   a short digest of the folder, with the ids of the notes it rests on
    themes     the questions several papers speak to; for each, every paper's position (supports / qualifies / opposes / neutral, one short phrase, the notes behind it)
               and whether the papers agree: this is the map (themes down, papers across)
    gaps       what is left open: from the papers' own limitations and future-research notes, plus what no paper in the folder covers (marked as not grounded)
The server holds all of it to the notes it was given: an id that was not offered is dropped, a position or theme with no note behind it is dropped, a gap with no note is shown as
"not grounded in your papers", and a theme that only one paper speaks to is called that, whatever the model said.

    synthesis.estimate(rows, settings)   -> {papers, missing, cost}
    for m in synthesis.stream(fid, rows, settings, fresh): status | progress | synthesis{doc} | done | error      (saved as Papers/_folders/<id>.synthesis.json)
    synthesis.load(fid) / markdown(doc, name) / docx(doc, name)
"""
import json
import os
import re
import sys
import threading
import traceback
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import activity as act  # noqa: E402
import ai  # noqa: E402
import folders as fo  # noqa: E402
import relatedwork as rw  # noqa: E402

_lock = threading.RLock()
STANCES = ("supports", "qualifies", "opposes", "neutral")
CONSENSUS = ("agree", "mixed", "contested", "single")
KINDS = ("context", "method", "theory", "measurement", "time", "sample", "mechanism", "other")
ID = rw.ID
IDS = re.compile(ID)


def _o(**props):
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


_S, _L = {"type": "string"}, lambda x: {"type": "array", "items": x}
SCHEMA = _o(overview=_S, overview_ids=_L(_S),
            themes=_L(_o(name=_S, summary=_S, consensus=_S, tension=_S,
                         positions=_L(_o(paper=_S, claim=_S, stance=_S, ids=_L(_S))))),
            gaps=_L(_o(gap=_S, why=_S, kind=_S, question=_S, ids=_L(_S))))
SYSTEM = """You synthesise a set of academic papers for a researcher, from structured notes on each paper. Notes have ids like P2-3 (the third note of paper P2).
Return JSON with:
- overview: 3 to 5 plain sentences on what this set of papers is about, what it establishes and where it stops. overview_ids lists the ids of the notes it rests on.
- themes: 3 to 7 themes. A theme is a substantive question or construct that two or more papers speak to (not a paper, not a method label). For each: name (2 to 6 words); summary (one or two sentences on what the papers together say);
  consensus: agree | mixed | contested | single (single = only one paper speaks to it); tension: one sentence on where and why they differ (design, data, context, definition), or "" when they agree;
  positions: one entry for every paper that speaks to the theme (leave out papers that are silent) with paper (P1, P2…), claim (a short phrase, at most 14 words: what that paper says about it), stance toward the theme's main claim
  (supports | qualifies | opposes | neutral) and ids (the notes that show it).
- gaps: 3 to 6 things the set leaves open, each with gap (one sentence), why (why it matters, one sentence), kind (context | method | theory | measurement | time | sample | mechanism | other), question (a research question that would address it)
  and ids (the limitation / future research / finding notes it rests on). A gap you can only see from what is missing across the papers, with no note behind it, has an empty ids list.
Rules: name papers by author and year in every sentence ("Singhal et al. (2026)"), never as P1 or P2 (those are only for the ids lists); use only the notes; never invent a result, a number or a paper; every claim about a paper carries ids that were given; prefer fewer, sharper themes over many thin ones; say "mixed" or "contested" only when notes really conflict. Output only the JSON."""


def path(fid):
    return os.path.join(fo.DIR, fid + ".synthesis.json")


def load(fid):
    d = None
    if os.path.exists(path(fid)):
        try:
            d = json.load(open(path(fid), encoding="utf-8"))
        except (OSError, ValueError):
            d = None
    return d if isinstance(d, dict) and d.get("version") == 1 else None


def _write(fid, doc):
    os.makedirs(fo.DIR, exist_ok=True)
    tmp = path(fid) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path(fid))


def clean_settings(s, files_ok=None):
    s = s if isinstance(s, dict) else {}
    out = {"focus": str(s.get("focus") or "").strip()[:600], "files": [f for f in (s.get("files") or []) if isinstance(f, str)][:rw.MAX_PAPERS]}
    if files_ok is not None:
        out["files"] = [f for f in out["files"] if f in files_ok]
    return out


def _prompt(papers, s):
    return "WHAT THE RESEARCHER WANTS THE SYNTHESIS TO BE ABOUT (optional): %s\n\nNOTES ON THE %d PAPERS\n\n%s" % (s["focus"] or "(your choice)", len(papers), rw.digest(papers))


def estimate(rows, settings):
    s = clean_settings(settings)
    papers, missing = rw.gather(rows, set(s["files"]))
    chars = len(SYSTEM) + len(_prompt(papers, s))
    return {"papers": len(papers), "missing": len(missing), "cost": ai.estimate("synthesis", chars, 700 + 260 * len(papers)) if papers else 0.0}


# ---------------------------------------------------------------- holding the answer to the notes it was given

def _json(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        return json.loads(m.group(0) if m else text)
    except (ValueError, TypeError):
        return None


def _s(x, n, P=None):
    t = re.sub(r"\s+", " ", str(x or "")).strip()
    if P:                                   # a paper named by its id in a sentence ("P2 finds…") is named the way a reader knows it
        t = re.sub(r"\bP(\d+)\b(?!-)", lambda m: P["P" + m.group(1)]["short"] if "P" + m.group(1) in P else m.group(0), t)
    return t[:n]


def _ids(raw, P, N):
    """The ids in raw (a list, or text with ids in it) that were given, in order and without repeats; and how many were not given."""
    got = IDS.findall(" ".join(str(x) for x in raw) if isinstance(raw, list) else str(raw or ""))
    ok, bad = [], 0
    for i in got:
        if i in N and i not in ok:
            ok.append(i)
        elif i not in N and not (i in P):
            bad += 1
    return ok, bad


def assemble(d, P, N):
    """The model's JSON as the page shows it: {overview, overview_ids, themes, gaps, dropped}. Everything not backed by a given note is removed or marked."""
    dropped = 0
    ov_ids, bad = _ids(d.get("overview_ids") or [], P, N)
    dropped += bad
    themes = []
    for t in d.get("themes") if isinstance(d.get("themes"), list) else []:
        if not isinstance(t, dict):
            continue
        pos, seen = [], set()
        for p in t.get("positions") if isinstance(t.get("positions"), list) else []:
            if not isinstance(p, dict):
                continue
            ids, bad = _ids(p.get("ids") or [], P, N)
            dropped += bad
            pid = str(p.get("paper") or "").strip()
            if pid not in P:
                pid = ids[0].split("-")[0] if ids else ""
            ids = [i for i in ids if i.split("-")[0] == pid]
            if not ids or pid in seen:
                dropped += 1 if not ids and not bad else 0
                continue
            seen.add(pid)
            st = str(p.get("stance") or "").strip().lower()
            pos.append({"paper": pid, "claim": _s(p.get("claim"), 160, P), "stance": st if st in STANCES else "neutral", "ids": ids[:4]})
        if not pos or not _s(t.get("name"), 80):
            continue
        cons = str(t.get("consensus") or "").strip().lower()
        cons = "single" if len(pos) == 1 else (cons if cons in CONSENSUS and cons != "single" else "mixed")
        themes.append({"name": _s(t.get("name"), 80), "summary": _s(t.get("summary"), 500, P), "consensus": cons, "tension": _s(t.get("tension"), 400, P) if cons != "agree" else "", "positions": pos})
    gaps = []
    for g in d.get("gaps") if isinstance(d.get("gaps"), list) else []:
        if not isinstance(g, dict) or not _s(g.get("gap"), 400, P):
            continue
        ids, bad = _ids(g.get("ids") or [], P, N)
        dropped += bad
        k = str(g.get("kind") or "").strip().lower()
        gaps.append({"gap": _s(g.get("gap"), 400, P), "why": _s(g.get("why"), 400, P), "kind": k if k in KINDS else "other", "question": _s(g.get("question"), 300, P), "ids": ids[:6], "grounded": bool(ids)})
    return {"overview": _s(d.get("overview"), 1500, P), "overview_ids": ov_ids, "themes": themes, "gaps": gaps, "dropped": dropped}


# ---------------------------------------------------------------- making it

def stream(fid, rows, settings, fresh=False):
    """Messages: status, progress{tick}, synthesis{doc}, done{cost, model, provider, cached}, error{error}."""
    try:
        s = clean_settings(settings)
        papers, missing = rw.gather(rows, set(s["files"]))
        if len(papers) < 2:
            yield {"type": "error", "error": "Pick at least two papers that have a card (make the cards in the Evidence tab first)."}
            return
        yield {"type": "status", "msg": "Reading %d cards…" % len(papers)}
        pt, nt = rw.tables(papers)
        n, out = [0], {}

        def run():
            try:
                out["res"] = ai.chat("synthesis", [{"role": "user", "content": _prompt(papers, s)}], system=SYSTEM, schema=SCHEMA, max_tokens=1600 + 420 * len(papers), cache=True, fresh=fresh,
                                     cache_if=lambda t: isinstance(_json(t), dict) and isinstance(_json(t).get("themes"), list))
            except Exception as e:  # noqa: BLE001
                out["err"] = e
        t = threading.Thread(target=act.wrap(run), daemon=True)
        t.start()
        yield {"type": "status", "msg": "Looking across the papers…"}
        while t.is_alive():
            t.join(1.5)
            if t.is_alive():
                n[0] += 1
                yield {"type": "progress", "tick": n[0]}
        if "err" in out:
            raise out["err"]
        res = out["res"]
        d = _json(res.text)
        if not isinstance(d, dict) or not isinstance(d.get("themes"), list):
            raise ai.AIError("The model’s answer wasn’t valid JSON (it may have been cut off). Try again.")
        body = assemble(d, pt, nt)
        if not body["themes"] and not body["gaps"]:
            raise ai.AIError("The model’s answer did not point at any of the notes, so it was not kept. Try again, or choose other papers.")
        with _lock:
            old = load(fid) or {}
            doc = {"version": 1, "rev": old.get("rev", 0) + 1, "settings": s, **body, "papers": pt, "notes": nt, "missing": missing,
                   "model": "%s · %s" % (res.provider, res.model), "cost": res.cost, "generated": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            _write(fid, doc)
        yield {"type": "synthesis", "doc": doc}
        yield {"type": "done", "cost": res.cost, "cached": res.cached, "model": res.model, "provider": res.provider}
    except ai.AIError as e:
        yield {"type": "error", "error": str(e)}
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        yield {"type": "error", "error": "Something went wrong: %s" % re.sub(r"\s+", " ", str(e))[:200]}


# ---------------------------------------------------------------- export

def _cite(doc, i):
    p = doc["papers"].get(i.split("-")[0])
    n = doc["notes"].get(i)
    return "%s%s" % (p["short"] if p else i, ", p. %d" % n["page"] if n and n.get("page") else "")


def markdown(doc, name=""):
    P = doc["papers"]
    L = ["# Synthesis%s" % (" — " + name if name else ""), "", "%d papers · %s · %s" % (len(P), doc["model"], doc["generated"][:10]), "", "## Overview", "", doc["overview"] +
         ("  \n*Based on: %s*" % "; ".join(_cite(doc, i) for i in doc["overview_ids"][:8]) if doc["overview_ids"] else ""), ""]
    L += ["## Themes", ""]
    for t in doc["themes"]:
        L += ["### %s (%s)" % (t["name"], t["consensus"]), "", t["summary"], ""]
        if t["tension"]:
            L += ["*Where they differ:* " + t["tension"], ""]
        L += ["- **%s** (%s): %s — %s" % (P[p["paper"]]["short"], p["stance"], p["claim"], "; ".join(_cite(doc, i) for i in p["ids"])) for p in t["positions"]] + [""]
    L += ["## Gaps", ""]
    for g in doc["gaps"]:
        L.append("- **%s** (%s%s)" % (g["gap"], g["kind"], "" if g["grounded"] else ", not grounded in your papers"))
        if g["why"]:
            L.append("  - Why it matters: " + g["why"])
        if g["question"]:
            L.append("  - Question: " + g["question"])
        if g["ids"]:
            L.append("  - Based on: " + "; ".join(_cite(doc, i) for i in g["ids"]))
    return "\n".join(L) + "\n"


def docx(doc, name=""):
    import io
    from docx import Document
    d, P = Document(), doc["papers"]
    d.add_heading("Synthesis" + (" — " + name if name else ""), 1)
    d.add_paragraph("%d papers · %s · %s" % (len(P), doc["model"], doc["generated"][:10]))
    d.add_heading("Overview", 2)
    d.add_paragraph(doc["overview"])
    d.add_heading("Themes", 2)
    for t in doc["themes"]:
        d.add_heading("%s (%s)" % (t["name"], t["consensus"]), 3)
        d.add_paragraph(t["summary"])
        if t["tension"]:
            d.add_paragraph("Where they differ: " + t["tension"])
        for p in t["positions"]:
            d.add_paragraph("%s (%s): %s — %s" % (P[p["paper"]]["short"], p["stance"], p["claim"], "; ".join(_cite(doc, i) for i in p["ids"])), style="List Bullet")
    d.add_heading("Gaps", 2)
    for g in doc["gaps"]:
        d.add_paragraph("%s (%s%s)" % (g["gap"], g["kind"], "" if g["grounded"] else ", not grounded in your papers"), style="List Bullet")
        for lab, v in (("Why it matters", g["why"]), ("Question", g["question"]), ("Based on", "; ".join(_cite(doc, i) for i in g["ids"]))):
            if v:
                d.add_paragraph("%s: %s" % (lab, v))
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()
