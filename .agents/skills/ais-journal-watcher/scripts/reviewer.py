"""Reviewer 2 (G6): a tough but fair review of a text you wrote (an abstract, a section, a proposal), before a real reviewer sees it.

The reviewer gets your text and, as context, the numbered notes of the papers in the folder (the same ids as the drafter: "P2-3"), so it can say "you ignore what Singhal et al. (2026) found" and point at the note.
What it returns is held to what it was given: a quoted sentence must really be in your text, a paper it says you overlooked must be one it was given with a real note, and anything else is dropped.

    for m in reviewer.stream(fid, rows, text, fresh): status | progress | review{doc} | done | error      (saved as Papers/_folders/<id>.review.json)
    reviewer.load(fid) / markdown(doc, name)
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
MAX_TEXT = 8000
MAX_PAPERS = 10
RECS = ("accept", "minor", "major", "reject")
_S = {"type": "string"}
_L = lambda x: {"type": "array", "items": x}  # noqa: E731


def _o(**props):
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


SCHEMA = _o(summary=_S, recommendation=_S, major=_L(_o(point=_S, why=_S, suggestion=_S)), minor=_L(_o(point=_S, suggestion=_S)), unsupported=_L(_o(quote=_S, need=_S)),
            literature=_L(_o(paper=_S, point=_S, ids=_L(_S))), questions=_L(_S))
SYSTEM = """You are "Reviewer 2": an experienced, sceptical but fair reviewer for an information-systems journal, reading a text the author wrote (an abstract, a section or a short proposal). You are rigorous and specific, never rude, and you say what would fix each problem.
You also get numbered notes on papers the author has collected (ids like P2-3). Use them to judge the text against the literature the author has: say where the text contradicts a note, ignores one, or overstates what the papers show.
Return JSON:
- summary: two or three sentences: what the text claims and your overall impression.
- recommendation: accept | minor | major | reject (revisions, as for a journal submission of this text as it stands).
- major: up to 4 major concerns (point; why it matters; a concrete suggestion). Real weaknesses of argument, identification, measurement, contribution or logic, not style.
- minor: up to 5 minor concerns (point; suggestion): clarity, definitions, structure, wording.
- unsupported: up to 5 claims in the text that need evidence or a citation; quote must be copied exactly from the text; need says what kind of evidence would convince you.
- literature: up to 4 places where the author's own papers bear on the text (paper = P1, P2…; point = what the paper says and how it affects the text; ids = the notes you rely on). Only papers and notes you were given.
- questions: up to 5 questions you would put to the author.
Rules: be specific to this text, never generic; never invent a study, number or quotation; if the text is too short to judge something, say so in the summary instead of guessing."""


def path(fid):
    return os.path.join(fo.DIR, fid + ".review.json")


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


def _json(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        return json.loads(m.group(0) if m else text)
    except (ValueError, TypeError):
        return None


def _s(x, n):
    return re.sub(r"\s+", " ", str(x or "")).strip()[:n]


def _who(text, P):
    """A paper named by its id in a sentence ("P3 finds…") is named the way a reader knows it."""
    return re.sub(r"\bP(\d+)\b(?!-)", lambda m: P["P" + m.group(1)]["short"] if "P" + m.group(1) in P else m.group(0), text)


def _norm(t):
    return re.sub(r"[^a-z0-9]+", " ", str(t or "").lower()).strip()


def _context(rows):
    papers, _ = rw.gather(rows, set())
    papers = papers[:MAX_PAPERS]
    return papers, (rw.digest(papers) if papers else "")


def estimate(rows, text):
    papers, dig = _context(rows)
    return {"papers": len(papers), "cost": ai.estimate("reviewer", len(SYSTEM) + len(dig) + min(len(text), MAX_TEXT) + 400, 1400) if text.strip() else 0.0}


def assemble(d, text, P, N):
    """The review as shown: quotes that are not in the text and notes that were not given are dropped (counted)."""
    dropped, nt = 0, _norm(text)
    ok = lambda x: isinstance(x, dict)  # noqa: E731
    w = lambda t, n: _who(_s(t, n), P)  # noqa: E731
    major = [{"point": w(x.get("point"), 400), "why": w(x.get("why"), 400), "suggestion": w(x.get("suggestion"), 400)} for x in (d.get("major") or []) if ok(x) and _s(x.get("point"), 5)][:4]
    minor = [{"point": w(x.get("point"), 300), "suggestion": w(x.get("suggestion"), 300)} for x in (d.get("minor") or []) if ok(x) and _s(x.get("point"), 5)][:5]
    unsup = []
    for x in d.get("unsupported") or []:
        if not ok(x):
            continue
        q = _s(x.get("quote"), 300)
        if len(_norm(q)) >= 12 and _norm(q) in nt:
            unsup.append({"quote": q, "need": w(x.get("need"), 300)})
        else:
            dropped += 1
    lit = []
    for x in d.get("literature") or []:
        if not ok(x):
            continue
        ids = [i for i in dict.fromkeys(re.findall(rw.ID, " ".join(str(i) for i in x.get("ids") or []))) if i in N]
        pid = str(x.get("paper") or "").strip()
        if pid not in P and ids:
            pid = ids[0].split("-")[0]
        ids = [i for i in ids if i.split("-")[0] == pid]
        if pid in P and ids and _s(x.get("point"), 5):
            lit.append({"paper": pid, "point": w(x.get("point"), 500), "ids": ids[:4]})
        else:
            dropped += 1
    rec = str(d.get("recommendation") or "").strip().lower()
    return {"summary": w(d.get("summary"), 800), "recommendation": rec if rec in RECS else "major", "major": major, "minor": minor, "unsupported": unsup[:5], "literature": lit[:4],
            "questions": [w(q, 300) for q in (d.get("questions") or []) if _s(q, 5)][:5], "dropped": dropped}


def stream(fid, rows, text, fresh=False):
    """Messages: status, progress{tick}, review{doc}, done{cost, model, provider, cached}, error{error}."""
    try:
        text = str(text or "").strip()[:MAX_TEXT]
        if len(text.split()) < 25:
            yield {"type": "error", "error": "Give the reviewer something to read: at least a few sentences."}
            return
        papers, dig = _context(rows)
        pt, nt = rw.tables(papers) if papers else ({}, {})
        yield {"type": "status", "msg": "Reviewer 2 is reading%s…" % (" with %d of your papers in mind" % len(papers) if papers else "")}
        n, out = [0], {}
        prompt = "THE TEXT TO REVIEW\n\n%s\n\n%s" % (text, ("NOTES ON THE AUTHOR'S PAPERS\n\n" + dig) if dig else "(The author has no paper cards in this folder: review the text on its own, and leave literature empty.)")

        def run():
            try:
                out["res"] = ai.chat("reviewer", [{"role": "user", "content": prompt}], system=SYSTEM, schema=SCHEMA, max_tokens=2400, cache=True, fresh=fresh,
                                     cache_if=lambda t: isinstance(_json(t), dict) and isinstance(_json(t).get("major"), list))
            except Exception as e:  # noqa: BLE001
                out["err"] = e
        t = threading.Thread(target=act.wrap(run), daemon=True)
        t.start()
        while t.is_alive():
            t.join(1.5)
            if t.is_alive():
                n[0] += 1
                yield {"type": "progress", "tick": n[0]}
        if "err" in out:
            raise out["err"]
        res = out["res"]
        d = _json(res.text)
        if not isinstance(d, dict) or not isinstance(d.get("major"), list):
            raise ai.AIError("The model’s answer wasn’t valid JSON (it may have been cut off). Try again.")
        body = assemble(d, text, pt, nt)
        with _lock:
            doc = {"version": 1, "text": text, **body, "papers": pt, "notes": nt, "model": "%s · %s" % (res.provider, res.model), "cost": res.cost, "generated": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            _write(fid, doc)
        yield {"type": "review", "doc": doc}
        yield {"type": "done", "cost": res.cost, "cached": res.cached, "model": res.model, "provider": res.provider}
    except ai.AIError as e:
        yield {"type": "error", "error": str(e)}
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        yield {"type": "error", "error": "Something went wrong: %s" % re.sub(r"\s+", " ", str(e))[:200]}


def markdown(doc, name=""):
    L = ["# Reviewer 2%s" % (" — " + name if name else ""), "", "Recommendation: **%s** · %s · %s" % (doc["recommendation"], doc["model"], doc["generated"][:10]), "", doc["summary"], ""]
    if doc["major"]:
        L += ["## Major concerns", ""] + ["%d. **%s** %s%s" % (i, m["point"], m["why"], "  \n   *Suggestion:* " + m["suggestion"] if m["suggestion"] else "") for i, m in enumerate(doc["major"], 1)] + [""]
    if doc["minor"]:
        L += ["## Minor concerns", ""] + ["- %s%s" % (m["point"], " — " + m["suggestion"] if m["suggestion"] else "") for m in doc["minor"]] + [""]
    if doc["unsupported"]:
        L += ["## Claims that need evidence", ""] + ["- “%s” — %s" % (u["quote"], u["need"]) for u in doc["unsupported"]] + [""]
    if doc["literature"]:
        L += ["## Your own papers bear on this", ""] + ["- %s (%s)" % (l["point"], "; ".join(("%s, p. %s" % (doc["papers"][i.split("-")[0]]["short"], doc["notes"][i]["page"]) if doc["notes"][i].get("page") else doc["papers"][i.split("-")[0]]["short"]) for i in l["ids"])) for l in doc["literature"]] + [""]
    if doc["questions"]:
        L += ["## Questions for the author", ""] + ["- " + q for q in doc["questions"]] + [""]
    return "\n".join(L)
