"""Search the words inside the PDFs themselves (X2): every paper's text, not only titles, summaries, cards and your own notes (that is library_search.py).

    for m in fulltext.stream(rows, "platform governance"): ...      status{msg} | results{n, papers, items:[{file, title, cite, page, snippet, quote, score}]} | error
    fulltext.search(blocks_by_file, q)                              the ranking itself (BM25 over paragraphs; all words must be present; "quoted phrases" must match exactly)

Papers are read once (paragraph blocks, cached in state/index/) and then every search is instant; the first search after new papers arrive says how many it reads first. Nothing is sent anywhere.
"""
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blocks as bl  # noqa: E402
import evidence as evd  # noqa: E402
import notes_store as ns  # noqa: E402

STOP = set("the a an of to in on for and or is are was were be been with by as at from that this it its we our their not but if than then so such can may do does did has have had which who whom what when where how why into over under between within about also more most any all each both these those there here".split())
K1, B = 1.4, 0.75
PER_PAPER = 3


def tokens(q):
    return [t for t in re.findall(r"[a-z0-9][a-z0-9'’\-]*", q.lower()) if t not in STOP and len(t) > 1]


def parse(q):
    """(phrases, words): "quoted phrases" and the remaining words."""
    phrases = [re.sub(r"\s+", " ", p.strip().lower()) for p in re.findall(r'"([^"]{3,})"', q)]
    return phrases, tokens(re.sub(r'"[^"]*"', " ", q))


def _snippet(text, words, phrases, n=260):
    low = text.lower()
    keys = phrases + words
    pos = min([low.find(k) for k in keys if low.find(k) >= 0] or [0])
    a = max(0, pos - 90)
    s = text[a:a + n]
    return ("…" if a else "") + s + ("…" if a + n < len(text) else "")


def search(blocks_by_file, q, limit=40):
    phrases, words = parse(q)
    if not phrases and not words:
        return []
    docs = []
    for f, (row, bs) in blocks_by_file.items():
        for b in bs:
            if b.get("kind", "body") == "body":
                toks = tokens(b["text"])
                docs.append((f, row, b, toks, b["text"].lower()))
    N = max(1, len(docs))
    df = {w: sum(1 for d in docs if w in d[3]) for w in set(words)}
    avg = sum(len(d[3]) for d in docs) / N or 1
    scored = []
    for f, row, b, toks, low in docs:
        if any(p not in re.sub(r"\s+", " ", low) for p in phrases) or any(w not in toks and w not in low for w in words):
            continue
        sc, ln = 0.0, len(toks) or 1
        for w in words:
            tf = toks.count(w) or low.count(w)
            idf = math.log(1 + (N - df[w] + 0.5) / (df[w] + 0.5))
            sc += idf * tf * (K1 + 1) / (tf + K1 * (1 - B + B * ln / avg))
        sc += 2.0 * len(phrases)
        scored.append((sc, f, row, b))
    scored.sort(key=lambda x: -x[0])
    out, per = [], {}
    for sc, f, row, b in scored:
        if per.get(f, 0) >= PER_PAPER:
            continue
        per[f] = per.get(f, 0) + 1
        out.append({"file": f, "title": row.get("title", ""), "cite": evd.short_cite(row), "page": b["page"] + 1, "snippet": _snippet(b["text"], words, phrases), "quote": b["text"][:130].rsplit(" ", 1)[0] if len(b["text"]) > 130 else b["text"], "score": round(sc, 2)})
        if len(out) >= limit:
            break
    return out


def stream(rows, q):
    try:
        phrases, words = parse(q)
        if not phrases and not words:
            yield {"type": "error", "error": "Type a few words to look for (common words like “the” are ignored; put an exact phrase in quotes)."}
            return
        by, todo = {}, []
        for r in rows:
            pdf = ns.pdf_path(r.get("file"))
            if pdf and not os.path.exists(os.path.join(bl.INDEX, ns.sha1_of(pdf) + ".blocks.json")):
                todo.append((r, pdf))
        for i, (r, pdf) in enumerate(todo):
            yield {"type": "status", "msg": "Reading paper %d of %d for the first time: %s" % (i + 1, len(todo), r.get("title", "")[:60])}
            bl.extract(pdf)
        for r in rows:
            pdf = ns.pdf_path(r.get("file"))
            if pdf:
                by[r["file"]] = (r, bl.extract(pdf))
        items = search(by, q)
        yield {"type": "results", "n": len(items), "papers": len({i["file"] for i in items}), "searched": len(by), "items": items, "words": words, "phrases": phrases}
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        yield {"type": "error", "error": "Something went wrong: %s" % re.sub(r"\s+", " ", str(e))[:200]}
