"""Claim check (W5): paste a paragraph (or take your related-work draft); every claim in it is checked against passages of your own papers.

A claim is a sentence of at least seven words. For each one the closest passages of the papers in scope are found with the local embeddings (free), and one model call
says whether they support it, only partly support it, contradict it, or do not address it. The model may only point at the passages it was given; the server drops anything
else, and a "supported" with no passage behind it becomes "not found". When a claim names a paper you have ("Singhal et al., 2026" / "Singhal et al. (2026)"), that paper's
own best passages are always among the candidates and the result says whether the paper you cited is really the one that says it.

    claims.split(text)                          -> [{n, text, own}]            the sentences that look like claims ("own" = about your own study)
    claims.estimate(rows, texts)                -> {papers, unindexed, seconds, checking, cost}
    for m in claims.stream(fid, rows, texts, scope, fresh): status | progress | claims{doc} | done | error      (saved as Papers/_folders/<id>.claims.json)
    claims.load(fid) / mark(fid, n, handled, rev) / markdown(doc, name) / docx(doc, name)
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
import ask  # noqa: E402
import blocks as bl  # noqa: E402
import embed  # noqa: E402
import evidence as evd  # noqa: E402
import folders as fo  # noqa: E402
import notes_store as ns  # noqa: E402
import relatedwork as rw  # noqa: E402

_lock = threading.RLock()
MAX_CLAIMS = 25
TOP = 5                      # passages offered per claim
VERDICTS = ("supported", "partly", "contradicted", "not_found")
STANCES = ("supports", "contradicts", "context")
SENT = re.compile(r"(?<=[.!?])(?<!\bal\.)(?<!\be\.g\.)(?<!\bi\.e\.)(?<!\bvs\.)(?<!\bcf\.)\s+(?=[A-Z“\"\[])")
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["claims"], "properties": {"claims": {"type": "array", "items": {
    "type": "object", "additionalProperties": False, "required": ["n", "verdict", "reason", "evidence"],
    "properties": {"n": {"type": "integer"}, "verdict": {"type": "string"}, "reason": {"type": "string"},
                   "evidence": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["id", "stance"], "properties": {"id": {"type": "string"}, "stance": {"type": "string"}}}}}}}}}
SYSTEM = """You check claims from a researcher's draft against passages taken from the researcher's own papers. Each claim comes with the passages that were found for it; their ids look like C3.2 (claim 3, passage 2).
For every claim give one verdict:
- supported: a passage states the claim or clearly implies it, in the same direction and for the same kind of setting;
- partly: the passages back only part of it, a weaker or narrower version, or only in some context. Say what differs;
- contradicted: a passage says something incompatible with it (the opposite result, a different size or condition). Say which;
- not_found: none of the passages speaks to it. That says nothing about whether the claim is true, only that these papers do not address it.
Rules:
- Use only the passages given for that claim. Never use outside knowledge, and never decide from vague topical overlap: a passage on the same topic that does not say the thing is not support.
- Check numbers, directions and conditions exactly; report any mismatch ("the paper reports 3.1%, not 13%").
- evidence lists the ids of the passages you relied on, each marked supports | contradicts | context (relevant, but it does not settle the claim). A not_found claim has an empty evidence list.
- reason is one or two plain sentences (at most 40 words) that name the paper and say what it reports. Do not repeat the claim.
- When a claim names a paper ("cited": ...), look at that paper's own passages first and say so if they do not say it, even if another paper does.
Output only the JSON."""


# ---------------------------------------------------------------- the sentences

def split(text):
    """The sentences of at least seven words: [{n, text, own}]. Headings, questions and list markers are dropped; "own" marks sentences about your own study (not ticked by default)."""
    out = []
    for para in str(text or "").replace("\r", "").split("\n"):
        t = re.sub(r"^\s*(?:[-*•]\s+|\d+[.)]\s+)", "", para).strip()
        if not t or t.startswith("#"):
            continue
        for s in SENT.split(t):
            s = re.sub(r"\s+", " ", s).strip()
            if len(s.split()) < 7 or s.endswith("?"):
                continue
            out.append({"n": len(out) + 1, "text": s[:600], "own": bool(rw.OWN.search(s))})
            if len(out) >= MAX_CLAIMS:
                return out
    return out


def cited_papers(text, rows):
    """The library papers a sentence names, as "Singhal et al., 2026" or "Singhal et al. (2026)": the files of those rows."""
    out = []
    for r in rows:
        try:
            lab, year = rw.label(r), str(r.get("year") or "")
        except Exception:  # noqa: BLE001
            continue
        if lab and year and re.search(re.escape(lab) + r"[\s,]*\(?\s*" + re.escape(year), text):
            out.append(r["file"])
    return out


# ---------------------------------------------------------------- the file

def path(fid):
    return os.path.join(fo.DIR, fid + ".claims.json")


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


# ---------------------------------------------------------------- finding the passages

def retrieve(papers, claims, cited):
    """Yields ("status", msg) while papers are read for the first time, then ("items", [[passage, ...] per claim]). cited[i] = files of the papers claim i names."""
    import numpy as np
    n = len(papers)
    for i, (r, pdf) in enumerate(papers):
        if not embed.is_indexed(pdf):
            yield "status", "Reading paper %d of %d for the first time: %s" % (i + 1, n, r.get("title", "")[:60])
            embed.index_paper(pdf)
    Q = ai.embed([c["text"] for c in claims], kind="query")                # (claims, d)
    per = [[] for _ in claims]
    for pi, (r, pdf) in enumerate(papers):
        blocks = bl.extract(pdf)
        arr = np.load(embed._paths(pdf)[0])
        if not len(blocks) or len(arr) != len(blocks):
            continue
        S = arr @ Q.T                                                    # (blocks, claims)
        S[[j for j, b in enumerate(blocks) if b.get("kind", "body") != "body"], :] = -1.0
        for ci in range(len(claims)):
            for j in np.argsort(-S[:, ci])[:2]:
                if S[j, ci] > -1.0:
                    per[ci].append((float(S[j, ci]), pi, int(j)))
    out = []
    for ci, cands in enumerate(per):
        cands.sort(reverse=True)
        chosen = cands[:TOP]
        for f in cited[ci]:                                              # the paper the claim names is always heard
            pi = next((k for k, (r, _) in enumerate(papers) if r["file"] == f), None)
            for c in [c for c in cands if c[1] == pi][:2]:
                if c not in chosen:
                    chosen.append(c)
        items = []
        for m, (sc, pi, j) in enumerate(chosen, 1):
            r, pdf = papers[pi]
            b = bl.extract(pdf)[j]
            items.append({"id": "C%d.%d" % (claims[ci]["n"], m), "file": r["file"], "cite": evd.short_cite(r), "title": r.get("title", ""), "page": b["page"] + 1,
                          "quote": ask.quote_of(b["text"]), "text": b["text"][:700], "score": round(sc, 3)})
        out.append(items)
    yield "items", out


def _prompt(claims, items, cited, titles):
    parts = []
    for c, its, cf in zip(claims, items, cited):
        head = "### Claim %d%s\n%s" % (c["n"], "  (cited: %s)" % "; ".join(titles[f] for f in cf) if cf else "", c["text"])
        parts.append(head + "\n\n" + ("\n".join("%s (%s, p. %s) %s" % (i["id"], i["cite"], i["page"], i["text"].replace("\n", " ")) for i in its) if its else "(no passage found)"))
    return "Check these %d claims.\n\n%s" % (len(claims), "\n\n".join(parts))


def estimate(rows, texts):
    ps = [(r, pdf) for r in rows for pdf in [ns.pdf_path(r.get("file"))] if pdf]
    unindexed = sum(1 for _, pdf in ps if not embed.is_indexed(pdf))
    n = len([t for t in texts if str(t).strip()])
    cost = ai.estimate("claims", n * (TOP * 650 + 400) + 1800, 120 * n + 200) if n else 0.0
    return {"papers": len(ps), "unindexed": unindexed, "seconds": unindexed * 4, "checking": n, "cost": cost}


# ---------------------------------------------------------------- checking

def _judge(claims, items, cited, titles, fresh):
    res = ai.chat("claims", [{"role": "user", "content": _prompt(claims, items, cited, titles)}], system=SYSTEM, schema=SCHEMA, max_tokens=300 + 190 * len(claims), cache=True, fresh=fresh,
                  cache_if=lambda t: isinstance(_json(t), dict) and isinstance(_json(t).get("claims"), list))
    d = _json(res.text)
    if not isinstance(d, dict) or not isinstance(d.get("claims"), list):
        raise ai.AIError("The model’s answer wasn’t valid JSON (it may have been cut off). Try again.")
    return d["claims"], res


def _json(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        return json.loads(m.group(0) if m else text)
    except (ValueError, TypeError):
        return None


def _s(x, n):
    return re.sub(r"\s+", " ", str(x or "")).strip()[:n]


def assemble(claims, items, cited, answers, titles=None):
    """The model's verdicts, held to what it was shown: only passages offered for that claim count, a verdict with no passage behind it is "not found", and for a claim that names a
    paper the result says whether that paper's own passages back it."""
    by = {}
    for a in answers:
        if isinstance(a, dict):
            try:
                by[int(a.get("n"))] = a
            except (TypeError, ValueError):
                pass
    out, dropped = [], 0
    for c, its, cf in zip(claims, items, cited):
        a, offered = by.get(c["n"]), {i["id"]: i for i in its}
        ev, seen = [], set()
        for e in (a.get("evidence") if a and isinstance(a.get("evidence"), list) else []):
            i = str(e.get("id") if isinstance(e, dict) else e).strip()
            if i in offered and i not in seen:
                seen.add(i)
                ev.append({k: offered[i][k] for k in ("file", "cite", "title", "page", "quote", "text")} | {"id": i, "stance": e.get("stance") if isinstance(e, dict) and e.get("stance") in STANCES else "context"})
            elif i not in offered:
                dropped += 1
        v = str(a.get("verdict") or "").strip().lower().replace(" ", "_").replace("-", "_") if a else ""
        note = ""
        if not a:
            v, note = "unchecked", "The model did not answer for this claim."
        elif v not in VERDICTS:
            v, note = "not_found", "The model gave no usable verdict."
        elif v == "supported" and not any(e["stance"] == "supports" for e in ev):
            v, note = "not_found", "The model gave no passage that supports this, so it is not counted as supported."
        elif v == "contradicted" and not any(e["stance"] == "contradicts" for e in ev):
            v, note = "not_found", "The model gave no passage that contradicts this, so it is not counted as contradicted."
        elif v == "partly" and not ev:
            v, note = "not_found", "The model gave no passage for this, so it is not counted as partly supported."
        if v == "unchecked":
            ev = []
        row = {"n": c["n"], "text": c["text"], "verdict": v, "reason": _s(a.get("reason") if a else "", 400), "note": note, "evidence": ev,
               "candidates": [{k: i[k] for k in ("id", "file", "cite", "page", "quote")} for i in its if i["id"] not in seen][:3]}
        if cf:
            heard = {e["file"] for e in ev if e["stance"] == "supports"}
            row["cited"] = cf
            row["cited_names"] = [(titles or {}).get(f, "a paper in your library") for f in cf]
            row["cited_ok"] = bool(heard & set(cf)) if v in ("supported", "partly") else (False if v != "unchecked" else None)
        out.append(row)
    return out, dropped


def stream(fid, rows, texts, scope="folder", fresh=False, text=""):
    """texts = the claims to check (sentences). Messages: status, progress{chars}, claims{doc}, done{cost, model, provider, cached}, error{error}."""
    try:
        texts = [_s(t, 600) for t in (texts or []) if _s(t, 600)][:MAX_CLAIMS]
        if not texts:
            yield {"type": "error", "error": "There is nothing to check: pick at least one sentence."}
            return
        papers = [(r, pdf) for r in rows for pdf in [ns.pdf_path(r.get("file"))] if pdf]
        if not papers:
            yield {"type": "error", "error": "There are no papers to check against."}
            return
        claims = [{"n": i + 1, "text": t} for i, t in enumerate(texts)]
        cited = [cited_papers(c["text"], [r for r, _ in papers]) for c in claims]
        titles = {r["file"]: evd.short_cite(r) for r, _ in papers}
        yield {"type": "status", "msg": "Looking for passages in %d paper%s…" % (len(papers), "" if len(papers) == 1 else "s")}
        items = None
        for kind, v in retrieve(papers, claims, cited):
            if kind == "status":
                yield {"type": "status", "msg": v}
            else:
                items = v
        n, out = [0], {}

        def run():
            try:
                out["ans"], out["res"] = _judge(claims, items, cited, titles, fresh)
            except Exception as e:  # noqa: BLE001
                out["err"] = e
        t = threading.Thread(target=act.wrap(run), daemon=True)
        t.start()
        yield {"type": "status", "msg": "Checking %d claim%s…" % (len(claims), "" if len(claims) == 1 else "s")}
        while t.is_alive():
            t.join(1.5)
            if t.is_alive():
                n[0] += 1
                yield {"type": "progress", "tick": n[0]}
        if "err" in out:
            raise out["err"]
        res = out["res"]
        done, dropped = assemble(claims, items, cited, out["ans"], titles)
        with _lock:
            old = load(fid) or {}
            doc = {"version": 1, "rev": old.get("rev", 0) + 1, "scope": scope, "papers": len(papers), "text": str(text or "")[:60000], "claims": done, "dropped": dropped,
                   "model": "%s · %s" % (res.provider, res.model), "cost": res.cost, "generated": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            _write(fid, doc)
        yield {"type": "claims", "doc": doc}
        yield {"type": "done", "cost": res.cost, "cached": res.cached, "model": res.model, "provider": res.provider}
    except ai.AIError as e:
        yield {"type": "error", "error": str(e)}
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        yield {"type": "error", "error": "Something went wrong: %s" % re.sub(r"\s+", " ", str(e))[:200]}


def mark(fid, n, handled, rev=None):
    """Tick a claim off as dealt with (or not): no model call."""
    with _lock:
        doc = load(fid)
        if not doc:
            raise KeyError("no check yet")
        if rev is not None and rev != doc["rev"]:
            raise ValueError("The check changed in another window.")
        c = next((c for c in doc["claims"] if c["n"] == n), None)
        if not c:
            raise KeyError("no such claim")
        c["handled"] = bool(handled)
        doc["rev"] += 1
        _write(fid, doc)
        return doc


# ---------------------------------------------------------------- export

LABEL = {"supported": "Supported", "partly": "Partly supported", "contradicted": "Contradicted", "not_found": "Not found in your papers", "unchecked": "Not checked"}
ORDER = ("contradicted", "partly", "not_found", "unchecked", "supported")


def markdown(doc, name=""):
    lines = ["# Claim check%s" % (" — " + name if name else ""), "", "Checked against %d paper%s (%s) · %s · %s" % (doc["papers"], "" if doc["papers"] == 1 else "s", "whole library" if doc.get("scope") == "library" else "this folder", doc["model"], doc["generated"][:10]), ""]
    for v in ORDER:
        cs = [c for c in doc["claims"] if c["verdict"] == v]
        if not cs:
            continue
        lines += ["## %s (%d)" % (LABEL[v], len(cs)), ""]
        for c in cs:
            lines.append("%d. %s" % (c["n"], c["text"]))
            if c["reason"] or c.get("note"):
                lines.append("   - %s" % (c["reason"] or c["note"]))
            if c.get("cited") and c.get("cited_ok") is False:
                lines.append("   - ⚠ The paper you cite does not clearly say this.")
            for e in c["evidence"]:
                lines.append("   - %s, p. %s (%s): “%s”" % (e["cite"], e["page"], e["stance"], re.sub(r"\s+", " ", e["quote"] or e["text"])[:200]))
        lines.append("")
    return "\n".join(lines)


def docx(doc, name=""):
    import io
    from docx import Document
    d = Document()
    d.add_heading("Claim check" + (" — " + name if name else ""), 1)
    d.add_paragraph("Checked against %d paper%s (%s) · %s · %s" % (doc["papers"], "" if doc["papers"] == 1 else "s", "whole library" if doc.get("scope") == "library" else "this folder", doc["model"], doc["generated"][:10]))
    for v in ORDER:
        cs = [c for c in doc["claims"] if c["verdict"] == v]
        if not cs:
            continue
        d.add_heading("%s (%d)" % (LABEL[v], len(cs)), 2)
        for c in cs:
            d.add_paragraph("%d. %s" % (c["n"], c["text"]))
            if c["reason"] or c.get("note"):
                d.add_paragraph(c["reason"] or c["note"], style="List Bullet")
            if c.get("cited") and c.get("cited_ok") is False:
                d.add_paragraph("The paper you cite does not clearly say this.", style="List Bullet")
            for e in c["evidence"]:
                d.add_paragraph("%s, p. %s (%s): “%s”" % (e["cite"], e["page"], e["stance"], re.sub(r"\s+", " ", e["quote"] or e["text"])[:200]), style="List Bullet")
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()
