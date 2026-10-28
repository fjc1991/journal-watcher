"""Ask several papers at once (a folder, or the library filtered by journal / year / tag / status). Same idea as ask.py, but the
numbered passages come from different papers and each is labelled with its paper and page, so the answer can say who says what and
every citation opens the right paper at the right place.

    for m in multiask.stream(rows, "What do these papers find about transparency?", mode="passages"):   # rows = library rows
        {"type": "status", "msg"} {"type": "scope", ...} {"type": "passages", "passages": [{n, page, quote, file, cite, title, section}]}
        {"type": "text", "delta"} {"type": "done", provider, model, cost, cached, invalid} | {"type": "error", "error"}

Two ways to read:
  "passages"  the closest paragraphs to the question across all the papers (local embeddings; a paper is indexed the first time it is used)
  "cards"     the papers' cards (question, method, findings, limitations…): cheap, covers every paper that has a card, and each line still
              points at a page. Papers without a card are left out and reported.
The conversation is saved next to the folder (Papers/_folders/<id>.ask.json) or, for the whole library, in Papers/_ask_library.json.
"""
import json
import math
import os
import re
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ai  # noqa: E402
import ask  # noqa: E402
import blocks as bl  # noqa: E402
import download as dl  # noqa: E402
import embed  # noqa: E402
import evidence as evd  # noqa: E402
import folders as fo  # noqa: E402
import notes_store as ns  # noqa: E402
import papercard as pc  # noqa: E402

SYSTEM = """You are a careful research assistant answering a question from numbered passages taken from SEVERAL academic papers. Each passage is labelled with its paper (author, year) and page.
Answer using only these passages. After each claim, cite the passage numbers that support it in parentheses, like (4) or (2,7); never cite a number that was not given, and never credit a paper with something that appears in another paper's passage.
Name the papers in your sentences (for example "Rai et al. (2026) find…") so the reader can see who says what. When papers agree or disagree, say so explicitly and note differences in context, data or method.
If the passages do not answer the question, or only some of the papers address it, say what is missing instead of guessing.
Format (Markdown, concise, no preamble, never repeat the question):
1. First the direct answer in one to three sentences, naming the papers, with citations. No heading before it.
2. Then, when several papers speak to the question, a section "## By paper" with one bullet per paper (author, year: what it says, with citations); and, if they agree or disagree, a section "## Agreement and disagreement" in one or two sentences.
3. If the passages do not contain something the question asks for, or some papers do not address it, end with a section "## Not in the passages" saying what is missing.
Use no other headings."""
CARDS_NOTE = """\nThe passages are lines from structured summaries ("cards") of each paper, labelled with the section they come from. A card may leave out detail, so say when you would need to read the paper itself."""
CARD_CAP = 22              # lines per paper in cards mode
CARD_CHARS = 260_000       # characters of cards sent at most


# ---------------------------------------------------------------- the conversation file

def chat_path(fid):
    return os.path.join(fo.DIR, fid + ".ask.json") if fid else os.path.join(dl.PAPERS, "_ask_library.json")


def load_chat(fid):
    d = dl.load_json(chat_path(fid), None) if os.path.exists(chat_path(fid)) else None
    return {"version": 1, "turns": d["turns"]} if isinstance(d, dict) and isinstance(d.get("turns"), list) else {"version": 1, "turns": []}


def save_chat(fid, turns):
    if fid and not fo.get(fid):
        raise KeyError("no such folder")
    turns = [t for t in turns if isinstance(t, dict)][-100:] if isinstance(turns, list) else None
    if turns is None:
        raise ValueError("bad chat")
    p = chat_path(fid)
    if not turns:
        if os.path.exists(p):
            os.remove(p)
        return {"version": 1, "turns": []}
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "turns": turns}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)
    return {"version": 1, "turns": turns}


# ---------------------------------------------------------------- the papers in scope

def papers_of(rows):
    out = []
    for r in rows:
        pdf = ns.pdf_path(r.get("file"))
        if pdf:
            out.append((r, pdf))
    return out


def scope_info(rows):
    """What a question over these papers involves: how many papers, how many have a card, how many still need indexing (first use only)."""
    ps = papers_of(rows)
    unindexed = sum(1 for _, pdf in ps if not embed.is_indexed(pdf))
    return {"papers": len(ps), "cards": sum(1 for r, _ in ps if r.get("card")), "unindexed": unindexed, "seconds": unindexed * 4}


# ---------------------------------------------------------------- choosing what the model reads

def _passage_item(r, b, section=""):
    return {"file": r["file"], "cite": evd.short_cite(r), "title": r.get("title", ""), "page": b["page"] + 1, "quote": ask.quote_of(b["text"]), "text": b["text"], "section": section}


def retrieve(papers, question, progress=None):
    """Closest body paragraphs over all the papers, at most `per_paper` from each. Yields ("status", msg) while papers are indexed, then ("items", [...])."""
    import numpy as np
    n = len(papers)
    k = min(40, max(14, 3 * n))
    per = max(3, math.ceil(k / max(n, 1)) + 1)
    for i, (r, pdf) in enumerate(papers):
        if not embed.is_indexed(pdf):
            yield "status", "Reading paper %d of %d for the first time: %s" % (i + 1, n, r.get("title", "")[:60])
            embed.index_paper(pdf)
    q = ai.embed([question], kind="query")[0]
    cands = []
    for pi, (r, pdf) in enumerate(papers):
        blocks = bl.extract(pdf)
        arr = np.load(embed._paths(pdf)[0])
        if not len(blocks) or len(arr) != len(blocks):
            continue
        sc = arr @ q
        sc[[j for j, b in enumerate(blocks) if b.get("kind", "body") != "body"]] = -1.0
        for j in np.argsort(-sc)[:per]:
            if sc[j] > -1.0:
                cands.append((float(sc[j]), pi, int(j)))
    cands.sort(reverse=True)
    chosen = sorted(cands[:k], key=lambda c: (c[1], c[2]))               # reading order: paper by paper, top to bottom
    yield "items", [_passage_item(papers[pi][0], bl.extract(papers[pi][1])[j]) for _, pi, j in chosen]


SECTIONS = [("question", "question"), ("method", "method"), ("findings", "findings"), ("contribution", "contribution"), ("hypotheses", "hypotheses"), ("theories", "theory"),
            ("constructs", "constructs"), ("limitations", "limitations"), ("future_research", "future research"), ("implications", "implications")]


def card_items(papers):
    """The cards' lines as passages: [{file, cite, page, quote, text, section}], and the papers that have no card."""
    items, missing = [], []
    for r, pdf in papers:
        card = pc.load(pdf)
        if not card:
            missing.append(r)
            continue
        d, P, cite, mine = card["data"], card["passages"], evd.short_cite(r), []

        def add(text, cites, section):
            if not text:
                return
            p = next((P[str(c)] for c in cites or [] if str(c) in P), None)
            mine.append({"file": r["file"], "cite": cite, "title": r.get("title", ""), "page": p["page"] if p else None, "quote": p["quote"] if p else "", "text": text, "section": section})
        add("%s study. Topics: %s." % (d["kind"], ", ".join(d["topics"])) if d["topics"] else d["kind"], [], "type")
        add(d["question"]["text"], d["question"]["cites"], "question")
        m = d["method"]
        add("; ".join("%s: %s" % (k, m[k]) for k in ("design", "data", "sample", "analysis") if m[k]), m["cites"], "method")
        for key, label in SECTIONS[2:]:
            for x in d[key] if isinstance(d[key], list) else [d[key]]:
                if key == "hypotheses":
                    add("%s %s (%s)" % (x["label"], x["text"], x["verdict"]), x["cites"], label)
                elif key == "theories":
                    add("%s (%s)%s" % (x["name"], x["role"], ": " + x["note"] if x["note"] else ""), x["cites"], label)
                elif key == "constructs":
                    add("%s: %s" % (x["name"], x["definition"]), x["cites"], label)
                elif key == "limitations":
                    add(x["text"] + ("" if x.get("stated", True) else " (not acknowledged by the authors)"), x["cites"], label)
                else:
                    add(x["text"], x["cites"], label)
        items += mine[:CARD_CAP]
    return items, missing


def _label(it):
    return "%s%s%s" % (it["cite"], ", " + it["section"] if it.get("section") else "", ", p. %s" % it["page"] if it.get("page") else "")


def stream(rows, question, mode="passages", history=None, override=None, fresh=False):
    """history = [{"q", "a"}, …] earlier turns of this conversation (the last four are used)."""
    try:
        papers = papers_of(rows)
        if not papers:
            yield {"type": "error", "error": "There are no papers in this scope."}
            return
        history = [h for h in (history or []) if h.get("q") and h.get("a")][-4:]
        rq = question if not history or len(question.split()) > 8 else history[-1]["q"] + " " + question
        missing = []
        if mode == "cards":
            items, missing = card_items(papers)
            if not items:
                yield {"type": "error", "error": "None of these papers has a card yet. Make cards in the Evidence tab, or switch to reading passages."}
                return
            if sum(len(i["text"]) for i in items) > CARD_CHARS:             # a very large scope: keep the core lines of each card only
                items = [i for i in items if i["section"] in ("question", "method", "findings", "contribution", "limitations")]
        else:
            yield {"type": "status", "msg": "Searching %d paper%s…" % (len(papers), "" if len(papers) == 1 else "s")}
            items = []
            for kind, v in retrieve(papers, rq):
                if kind == "status":
                    yield {"type": "status", "msg": v}
                else:
                    items = v
        if not items:
            yield {"type": "error", "error": "No text could be read from these papers."}
            return
        yield {"type": "scope", "papers": len(papers), "used": len({i["file"] for i in items}), "missing_cards": [{"file": r["file"], "title": r.get("title", "")} for r in missing]}
        yield {"type": "passages", "passages": [{"n": n + 1, "page": i["page"] or 1, "quote": i["quote"], "file": i["file"], "cite": i["cite"], "title": i["title"], "section": i.get("section", "")} for n, i in enumerate(items)]}
        body = "\n\n".join("[%d] (%s) %s" % (n + 1, _label(i), i["text"]) for n, i in enumerate(items))
        messages = []
        for h in history:
            messages += [{"role": "user", "content": h["q"]}, {"role": "assistant", "content": h["a"]}]
        messages.append({"role": "user", "content": "Passages from %d papers:\n\n%s\n\nQuestion: %s" % (len({i["file"] for i in items}), body, question)})
        yield {"type": "status", "msg": "Asking…"}
        for kind, v in ai.stream("folder_ask", messages, system=SYSTEM + (CARDS_NOTE if mode == "cards" else ""), max_tokens=3000, cache=True, fresh=fresh, override=override):
            if kind == "text":
                yield {"type": "text", "delta": v}
            else:
                yield {"type": "done", "provider": v.provider, "model": v.model, "cost": v.cost, "cached": v.cached, "invalid": [n for n in ask.invalid_citations(v.text, len(items)) if n < 1000]}   # "(2026)" after an author's name is a year, not a citation
    except ai.AIError as e:
        yield {"type": "error", "error": str(e)}
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        yield {"type": "error", "error": "Something went wrong: %s" % re.sub(r"\s+", " ", str(e))[:200]}


def estimate(rows, mode="passages"):
    """Rough cost of one question (None when the model has no price)."""
    ps = papers_of(rows)
    if mode == "cards":
        chars = sum(5500 for r, _ in ps if r.get("card")) + 1500        # measured: a card contributes about 20 lines of ~250 characters
    else:
        chars = 700 * min(40, max(14, 3 * len(ps))) + 2000
    return ai.estimate("folder_ask", chars, 1100)
