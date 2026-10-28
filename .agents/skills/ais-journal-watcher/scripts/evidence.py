"""What a folder of papers says, from their paper cards: topics, theories, constructs, methods, and — the useful part — the
limitations and the future-research suggestions grouped by meaning (a limitation several papers share is a gap).

    ev = evidence.build(rows)       # rows = library rows of the papers (pipeline.library()); papers without a card are listed in ev["missing"]
    ev["papers"]      [{id "P1", file, key, title, authors, year, journal, doi, kind, question, topics, design, findings, hypotheses}]
    ev["limitations"] [{id "L1", label, n (papers), papers ["P1", ...], items [{paper, text, page, quote, stated}]}]  (also "future": F1…)
    ev["topics"|"theories"|"constructs"|"methods"]   [{name, n, papers}]  most shared first

Grouping uses the local embedding model (free, offline); if it is not available, identical wording is grouped instead.
Nothing is written to disk: it is computed from the cards each time (a few seconds), with an in-memory cache.
"""
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ai  # noqa: E402
import notes_store as ns  # noqa: E402
import papercard as pc  # noqa: E402

SIM_TEXT = 0.80       # cosine above which two limitations / future-research suggestions are "the same point"
SIM_NAME = 0.84       # … and two topic / theory / construct / method names
_cache = {}


def short_cite(p):
    """'Rai et al. (2024)' from a paper entry (authors as a list or a ';'-separated string)."""
    a = p.get("authors")
    first = (a[0] if isinstance(a, list) and a else re.split(r";", str(a or ""))[0]).strip()
    surname = first.split(",")[0].strip() if "," in first else (first.split()[-1] if first.split() else "")
    many = (len(a) > 1) if isinstance(a, list) else (";" in str(a or ""))
    if not surname:                      # no author in the library data: the first words of the title say which paper it is
        words = str(p.get("title") or "").split()[:4]
        surname = "“%s…”" % " ".join(words) if words else "Anon."
    return "%s%s (%s)" % (surname, " et al." if many else "", p.get("year") or "n.d.")


def cluster(texts, thresh):
    """Greedy grouping by meaning: indices of `texts`, grouped. Order of first appearance is kept."""
    if not texts:
        return []
    try:
        import numpy as np
        vecs = ai.embed(texts)
    except Exception:  # noqa: BLE001  (no model yet, offline…): fall back to identical wording
        groups = {}
        for i, t in enumerate(texts):
            groups.setdefault(re.sub(r"\W+", " ", t.lower()).strip(), []).append(i)
        return list(groups.values())
    groups, cents = [], []
    for i in range(len(texts)):
        best, bs = -1, thresh
        for g, c in enumerate(cents):
            s = float(vecs[i] @ c)
            if s >= bs:
                best, bs = g, s
        if best < 0:
            groups.append([i]); cents.append(vecs[i].copy())
        else:
            groups[best].append(i)
            m = vecs[groups[best]].mean(axis=0)
            cents[best] = m / max(float(np.linalg.norm(m)), 1e-9)
    return groups


def _label(texts, idxs):
    """The member closest to what most members share: the most frequent wording, else the shortest that is not trivial."""
    c = Counter(re.sub(r"\W+", " ", texts[i].lower()).strip() for i in idxs)
    top, n = c.most_common(1)[0]
    if n > 1:
        return next(texts[i] for i in idxs if re.sub(r"\W+", " ", texts[i].lower()).strip() == top)
    return sorted((texts[i] for i in idxs), key=lambda t: abs(len(t) - 90))[0]


def _names(entries, thresh=SIM_NAME):
    """entries [(text, paper id, extra)] -> [{name, n, papers, extra: Counter}] most shared first."""
    groups = cluster([e[0] for e in entries], thresh)
    out = []
    for g in groups:
        papers = list(dict.fromkeys(entries[i][1] for i in g))
        out.append({"name": _label([e[0] for e in entries], g), "n": len(papers), "papers": papers, "mentions": len(g),
                    "extra": dict(Counter(entries[i][2] for i in g if entries[i][2]))})
    return sorted(out, key=lambda x: (-x["n"], -x["mentions"], x["name"].lower()))


def _clusters(items, prefix):
    """items [{paper, text, page, quote, stated}] -> [{id, label, n, papers, items}] ; most shared first."""
    groups = cluster([x["text"] for x in items], SIM_TEXT)
    out = []
    for g in groups:
        members = [items[i] for i in g]
        out.append({"label": _label([x["text"] for x in items], g), "papers": list(dict.fromkeys(m["paper"] for m in members)), "items": members})
    out.sort(key=lambda c: (-len(c["papers"]), -len(c["items"]), c["label"].lower()))
    for i, c in enumerate(out):
        c["id"], c["n"] = "%s%d" % (prefix, i + 1), len(c["papers"])
    return out


def _where(card, cites):
    for c in cites or []:
        p = (card.get("passages") or {}).get(str(c))
        if p:
            return p["page"], p["quote"]
    return None, ""


def build(rows):
    papers, missing, stale = [], [], []
    lim, fut, topics, theories, constructs, methods = [], [], [], [], [], []
    key = []
    for r in rows:
        pdf = ns.pdf_path(r.get("file"))
        card = pc.load(pdf) if pdf else None
        if not card:
            missing.append({"file": r.get("file"), "title": r.get("title", "")})
            continue
        key.append((r["file"], card["rev"], card.get("generated")))
        if card.get("stale"):
            stale.append(r["file"])
        pid = "P%d" % (len(papers) + 1)
        d = card["data"]
        authors = r.get("authors")
        papers.append({"id": pid, "file": r["file"], "key": r.get("key"), "title": r.get("title", ""), "authors": authors if isinstance(authors, list) else str(authors or ""),
                       "year": r.get("year", ""), "journal": r.get("journal", ""), "doi": r.get("doi", ""), "cite": short_cite(r), "kind": d["kind"], "question": d["question"]["text"],
                       "topics": d["topics"], "design": d["method"]["design"], "findings": [x["text"] for x in d["findings"][:3]],
                       "hypotheses": [{"label": h["label"], "text": h["text"], "verdict": h["verdict"]} for h in d["hypotheses"]]})
        for x in d["limitations"]:
            pg, q = _where(card, x["cites"])
            lim.append({"paper": pid, "text": x["text"], "page": pg, "quote": q, "stated": x.get("stated", True)})
        for x in d["future_research"]:
            pg, q = _where(card, x["cites"])
            fut.append({"paper": pid, "text": x["text"], "page": pg, "quote": q})
        topics += [(t, pid, None) for t in d["topics"]]
        theories += [(t["name"], pid, t["role"]) for t in d["theories"]]
        constructs += [(c["name"], pid, None) for c in d["constructs"]]
        if d["method"]["design"]:
            methods.append((d["method"]["design"], pid, None))
    ck = (tuple(key), ai.config_data()["embeddings"]["model"])
    if ck in _cache:
        hit = _cache[ck]
    else:
        hit = {"limitations": _clusters(lim, "L"), "future": _clusters(fut, "F"), "topics": _names(topics), "theories": _names(theories),
               "constructs": _names(constructs), "methods": _names(methods, 0.78)}
        _cache.clear(); _cache[ck] = hit
    return {"papers": papers, "missing": missing, "stale": stale, **hit}


def digest(ev, events=None, limit_clusters=14):
    """The evidence as compact text for a prompt: every id here (P1, L2, F3, E1) is one the model may cite."""
    o = ["PAPERS"]
    for p in ev["papers"]:
        a = p["authors"] if isinstance(p["authors"], str) else "; ".join(p["authors"][:2]) + (" et al." if len(p["authors"]) > 2 else "")
        o.append("%s: %s — %s (%s, %s). Question: %s Design: %s. Findings: %s" % (
            p["id"], p["title"], a, p["journal"], p["year"], p["question"] or "n/a", p["design"] or "n/a", " | ".join(p["findings"]) or "n/a"))
        hv = [h for h in p["hypotheses"] if h["verdict"] in ("not supported", "partly supported")]
        if hv:
            o.append("    hypotheses not (fully) supported: " + "; ".join("%s %s [%s]" % (h["label"], h["text"][:120], h["verdict"]) for h in hv[:4]))
    for title, key in (("LIMITATIONS, grouped by meaning (n = number of papers that share the point)", "limitations"), ("FUTURE-RESEARCH SUGGESTIONS, grouped by meaning", "future")):
        o.append("\n" + title)
        for c in ev[key][:limit_clusters]:
            ex = "; ".join(dict.fromkeys(x["text"][:170] for x in c["items"][:3]))
            o.append("%s [n=%d, %s]: %s" % (c["id"], c["n"], ",".join(c["papers"]), ex))
    for title, key in (("TOPICS", "topics"), ("THEORIES", "theories"), ("METHODS", "methods"), ("CONSTRUCTS", "constructs")):
        xs = ev[key][:16]
        if xs:
            o.append("\n%s: %s" % (title, "; ".join("%s (%d: %s)" % (x["name"], x["n"], ",".join(x["papers"])) for x in xs)))
    if events:
        o.append("\nCURRENT EVENTS (dated, from web search; the researcher chose these)")
        for e in events:
            o.append("%s [%s]: %s — %s" % (e["id"], e.get("date") or "undated", e["title"], (e.get("summary") or "")[:260]))
    return "\n".join(o)
