"""Ask a question about one paper. Retrieval + prompt for the reader's Ask tab (the dashboard streams what this yields).

    for msg in ask.stream(pdf_path, "What is the sample?", mode="rag"):   # dicts, one JSON line each in the HTTP response
        {"type": "status", "msg": ...} {"type": "passages", "passages": [{n, page, quote}]} {"type": "text", "delta": ...}
        {"type": "done", "provider", "model", "cost"} | {"type": "error", "error": ...}

The model sees numbered passages and must cite them as (3) or (3,7); the reader turns those numbers into links that jump to
the passage. mode "rag" = the closest passages to the question (needs the local embedding index, built on first use);
mode "full" = every passage of the paper (long-context models; falls back to "rag" on very long papers).
"""
import json
import os
import re
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ai  # noqa: E402
import blocks as bl  # noqa: E402
import download as dl  # noqa: E402
import embed  # noqa: E402

FULL_LIMIT = 450_000     # characters (~110k tokens): above this "whole paper" falls back to retrieval

SYSTEM = """You are a careful research assistant answering questions about ONE academic paper. You are given numbered passages extracted from the paper.
Answer using only those passages. After each claim, cite the passage numbers that support it in parentheses, like (4) or (2,7); never cite a number that was not given.
If the passages do not contain the answer, say so plainly instead of guessing, and say what is missing. Questions about the paper's overall argument may be answered less reliably
from a few passages; say so when it matters.
Format (Markdown, concise, no preamble, never repeat the question):
1. First the direct answer in one to three sentences, with its citations. No heading before it.
2. Then, only when the answer needs support or has several parts, a section "## Evidence" with short bullet points, each ending with its citations.
3. If the passages do not contain something the question asks for, end with a section "## Not in the passages" saying exactly what is missing (never fill the gap from general knowledge).
Use no other headings."""


def quote_of(text, n=130):
    """A short, findable start of a passage (cut at a word) that the reader uses to locate it on the page."""
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0]


def pick(pdf_path, question, mode, k=12, progress=None):
    blocks = [b for b in bl.extract(pdf_path) if b.get("kind", "body") != "ref"]
    if mode == "full" and sum(len(b["text"]) for b in blocks) <= FULL_LIMIT:
        return blocks
    if not embed.is_indexed(pdf_path):
        embed.index_paper(pdf_path, progress)
    top = embed.search(pdf_path, question, k=k if mode != "full" else 40)
    # broad questions ("main contribution?") are best answered by the abstract, which rarely matches them by wording: always include it
    lead = max((b for b in blocks if b["page"] == 0 and b.get("kind", "body") == "body"), key=lambda b: len(b["text"]), default=None)
    if lead and len(lead["text"]) > 500 and all(lead["idx"] != t["idx"] for t in top):
        top.append(lead)
    return sorted(top, key=lambda b: b["idx"])   # document order reads better


def invalid_citations(text, n):
    """Passage numbers the model cited that were never given to it (0, or more than n)."""
    bad = set()
    for g in re.findall(r"\((\d+(?:\s*,\s*\d+)*)\)", text):
        bad |= {int(x) for x in re.findall(r"\d+", g) if not 1 <= int(x) <= n}
    return sorted(bad)


def summary_stream(pdf_path, meta, template_id=None, fresh=False):
    """A summary of the whole paper from one of your templates, streamed (not saved: the reader offers to save it)."""
    import prefs
    try:
        interests = ", ".join(t.get("label", t["name"]) for t in dl.load_json(os.path.join(dl.BASE, "interests.json"), {"topics": []})["topics"])
        prompt = prefs.summary_prompt(template_id or prefs.get_settings()["summary_template"], interests=interests, title=meta.get("title", ""),
                                      journal=meta.get("journal", ""), doi=meta.get("doi") or "n/a")
        yield {"type": "status", "msg": "Reading the whole paper…"}
        msgs = [{"role": "user", "content": [{"type": "pdf", "path": pdf_path}, {"type": "text", "text": prompt}]}]
        for kind, v in ai.stream("summarize", msgs, max_tokens=16000, cache=True, fresh=fresh):
            if kind == "text":
                yield {"type": "text", "delta": v}
            else:
                yield {"type": "done", "provider": v.provider, "model": v.model, "cost": v.cost, "cached": v.cached}
    except ai.AIError as e:
        yield {"type": "error", "error": str(e)}
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        yield {"type": "error", "error": "Something went wrong: %s" % re.sub(r"\s+", " ", str(e))[:200]}


SUGGEST_SCHEMA = {"type": "object", "properties": {"questions": {"type": "array", "items": {"type": "string"}}}, "required": ["questions"], "additionalProperties": False}


def suggest(pdf_path, last_q=None, last_a=None):
    """Four questions worth asking about this paper (or, after an answer, worth asking next). Cached."""
    blocks = [b for b in bl.extract(pdf_path) if b.get("kind", "body") == "body"]
    lead = "\n\n".join(b["text"] for b in blocks[:8])[:5000]
    if last_q:
        prompt = ("Paper excerpt:\n%s\n\nThe reader just asked: %s\nAnswer given: %s\n\nSuggest four short follow-up questions that dig deeper, each answerable from the paper." % (lead, last_q, (last_a or "")[:1500]))
    else:
        prompt = "Paper excerpt:\n%s\n\nSuggest four short, specific questions a researcher would want answered by reading this paper (contribution, theory, method and data, findings, limitations)." % lead
    r = ai.chat("suggest", [{"role": "user", "content": prompt}], system="You propose useful questions about an academic paper. Short, specific, no numbering.",
                schema=SUGGEST_SCHEMA, max_tokens=500, cache=True)
    try:
        return [q.strip() for q in json.loads(r.text)["questions"] if q.strip()][:5]
    except (ValueError, KeyError, TypeError):
        return []


def stream(pdf_path, question, mode="rag", history=None, override=None, fresh=False):
    """history = [{"q", "a"}, …] earlier turns of this conversation (last four are used), so follow-ups make sense."""
    try:
        yield {"type": "status", "msg": "Reading the paper…"}
        marks = []
        history = [h for h in (history or []) if h.get("q") and h.get("a")][-4:]
        # a short follow-up ("and why?") is retrieved together with the previous question
        rq = question if not history or len(question.split()) > 8 else history[-1]["q"] + " " + question
        chosen = pick(pdf_path, rq, mode, progress=lambda d, t: marks.append((d, t)))
        if marks:
            yield {"type": "status", "msg": "Indexed %d passages (first time only)." % marks[-1][1]}
        if not chosen:
            yield {"type": "error", "error": "No text could be read from this PDF (a scanned image?)."}
            return
        passages = [{"n": i + 1, "page": b["page"] + 1, "quote": quote_of(b["text"])} for i, b in enumerate(chosen)]
        yield {"type": "passages", "passages": passages}
        body = "\n\n".join("[%d] (p. %d) %s" % (i + 1, b["page"] + 1, b["text"]) for i, b in enumerate(chosen))
        messages = []
        for h in history:
            messages += [{"role": "user", "content": h["q"]}, {"role": "assistant", "content": h["a"]}]
        messages.append({"role": "user", "content": "Passages from the paper:\n\n%s\n\nQuestion: %s" % (body, question)})
        yield {"type": "status", "msg": "Asking…"}
        for kind, v in ai.stream("ask", messages, system=SYSTEM, max_tokens=2500, cache=True, fresh=fresh, override=override):
            if kind == "text":
                yield {"type": "text", "delta": v}
            else:
                yield {"type": "done", "provider": v.provider, "model": v.model, "cost": v.cost, "cached": v.cached, "invalid": invalid_citations(v.text, len(chosen))}
    except ai.AIError as e:
        yield {"type": "error", "error": str(e)}
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        yield {"type": "error", "error": "Something went wrong: %s" % re.sub(r"\s+", " ", str(e))[:200]}
