"""Ask a question about one paper. Retrieval + prompt for the reader's Ask tab (the dashboard streams what this yields).

    for msg in ask.stream(pdf_path, "What is the sample?", mode="rag"):   # dicts, one JSON line each in the HTTP response
        {"type": "status", "msg": ...} {"type": "passages", "passages": [{n, page, quote}]} {"type": "text", "delta": ...}
        {"type": "done", "provider", "model", "cost"} | {"type": "error", "error": ...}

The model sees numbered passages and must cite them as (3) or (3,7); the reader turns those numbers into links that jump to
the passage. mode "rag" = the closest passages to the question (needs the local embedding index, built on first use);
mode "full" = every passage of the paper (long-context models; falls back to "rag" on very long papers). In that mode the paper is the first thing
sent, before the conversation and the question, and the same bytes for every question on it: a provider that reuses a repeated beginning then can, and
with "keep the paper ready" on (Settings → AI) Claude is asked to (ai.py: a text part marked "cache").

    ask.estimate(pdf_path, pages, history_chars)      what the next question costs in each mode, before it is asked (the line under the Ask box)
    ask.summary_estimate(pdf_path, meta, template)    what "Generate" in the Summary tab costs, and whether a saved answer makes it free
Neither sends anything, and neither writes next to the paper (only the passages kept in state/index, as reading the text does).
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
EST_OUT_TOKENS = 700     # the length of an answer the estimate assumes until OUT_HISTORY_MIN answers are on record (a short answer and a few evidence points; the limit is 2,500)
OUT_HISTORY_MIN = 5      # from this many answers in the usage record on, the estimate uses their mean length
HISTORY_MAX = 4 * (3000 + 6000)       # the most of the conversation that is sent with a question (the server lets four turns through, 3,000 + 6,000 characters each)
KEEP_MIN_TOKENS = 4096   # the shortest text (in tokens: about 13,500 characters at 3.3 characters a token) the estimate counts as kept ready between questions. A provider
                         # silently does not keep a shorter one (no error; every question then pays the whole text at the input price), and how short is too short
                         # differs by model (1,024 to 4,096 tokens): this is the largest, so that a further question is never shown cheaper than it is
NO_MODEL = "No model chosen — click ⚙"      # (the words the Ask tab's header uses)
EST_QUESTION = 300       # characters the estimate allows for the question and the words around the passages
SUMMARY_MAX_TOKENS = 16000
SUMMARY_OUT_TOKENS = 1500             # as summaries.OUT_TOKENS: a summary in the default template is about 1,000 to 1,700 tokens

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


def readable(pdf_path, pages=None):
    """The passages a question may be answered from (no reference list). pages: (first, last) file pages, both included: only that part (one chapter of a book)."""
    blocks = [b for b in bl.extract(pdf_path) if b.get("kind", "body") != "ref"]
    if pages:
        blocks = [b for b in blocks if pages[0] - 1 <= b["page"] <= pages[1] - 1]
    return blocks


def whole(blocks, mode):
    """Whether "whole paper" really sends every passage (not the closest ones it falls back to on a very long paper)."""
    return mode == "full" and sum(len(b["text"]) for b in blocks) <= FULL_LIMIT


def pick(pdf_path, question, mode, k=12, progress=None, pages=None):
    """pages: as in readable()."""
    blocks = readable(pdf_path, pages)
    if whole(blocks, mode):
        return blocks
    if not embed.is_indexed(pdf_path):
        embed.index_paper(pdf_path, progress)
    top = embed.search(pdf_path, question, k=k if mode != "full" else 40, pages=pages)
    # broad questions ("main contribution?") are best answered by the abstract, which rarely matches them by wording: always include it
    lead = max((b for b in blocks if b["page"] == 0 and b.get("kind", "body") == "body"), key=lambda b: len(b["text"]), default=None)
    if lead and not pages and len(lead["text"]) > 500 and all(lead["idx"] != t["idx"] for t in top):
        top.append(lead)
    return sorted(top, key=lambda b: b["idx"])   # document order reads better


def _claude(provider):
    """Whether this provider is Claude's own interface: it is sent a PDF itself (not its text), and it is the one "keep the paper ready" acts on."""
    return ((ai.config_data().get("providers") or {}).get(provider) or {}).get("type") == "anthropic"


def _out_tokens():
    """(tokens, basis) an answer is counted as before it is written: the mean length of your earlier answers ("history") once OUT_HISTORY_MIN of them are in
    the usage record (saved answers shown again and cancelled ones left out), else EST_OUT_TOKENS ("default")."""
    outs = [r["out"] for r in ai._rows() if r.get("feature") == "ask" and not r.get("cached") and not r.get("cancelled") and isinstance(r.get("out"), (int, float)) and r["out"] > 0]
    return (int(round(sum(outs) / len(outs))), "history") if len(outs) >= OUT_HISTORY_MIN else (EST_OUT_TOKENS, "default")


def estimate(pdf_path, pages=None, history_chars=0):
    """What the next question costs in each mode, before it is asked: {"provider", "model", "out_tokens", "out_basis", "rag": {...}, "full": {...}}, each
    mode {"passages", "chars", "cost"}; cost is None when the model has no price or there is nothing to read, never a silent 0. "full" also has "fallback": True
    when the paper is too long to read whole (the 40 closest passages are read), and "next" when the paper is kept ready between questions ("keep the paper
    ready" is on, the provider is Claude, the whole paper is read and it is at least KEEP_MIN_TOKENS long): cost is then the first question (the paper's text at the price of keeping it) and next a
    further question within five minutes (the paper's text at the price of reading it back). pages: as in readable(). history_chars: the length of the
    conversation that goes along. "blocked" is there when a question cannot be asked at all, and says why in the words Ask itself would use (no model chosen, no
    provider, no key, local-only mode): the page shows that and no figure. Nothing is sent and nothing is embedded: the passages are counted, not looked up
    (which ones are closest depends on the question)."""
    blocks = readable(pdf_path, pages)
    body = [b for b in blocks if b.get("kind", "body") == "body"]
    mean = sum(len(b["text"]) for b in body) / len(body) if body else 0
    lead = max((b for b in body if b["page"] == 0), key=lambda b: len(b["text"]), default=None)
    lead_chars = len(lead["text"]) if lead and not pages and len(lead["text"]) > 500 else 0      # (pick: the abstract always goes along)
    rest = EST_QUESTION + max(0, min(int(history_chars or 0), HISTORY_MAX))

    def closest(k):
        n = min(k, len(body))
        return n, int(mean * n) + (lead_chars if n < len(body) else 0)

    provider, model = ai.feature_model("ask")
    out_tokens, basis = _out_tokens()
    out = {"provider": provider, "model": model, "out_tokens": out_tokens, "out_basis": basis}
    try:
        if not model:
            raise KeyError("model")
        ai._route("ask")
    except ai.Blocked as e:              # no provider, no key, local-only mode: what Ask would answer
        out["blocked"] = str(e)
    except (KeyError, TypeError):        # nothing chosen for Ask (a damaged or hand-written ai.json)
        out["blocked"] = NO_MODEL
    for mode in ("rag", "full"):
        fits = whole(blocks, mode)
        n, chars = (len(blocks), sum(len(b["text"]) for b in blocks)) if fits else closest(12 if mode == "rag" else 40)
        paper = len(SYSTEM) + chars + 14 * n                 # 14: "[12] (p. 3) " and the blank line between two passages
        out[mode] = {"passages": n, "chars": paper + rest, "cost": ai.estimate("ask", paper + rest, out_tokens) if n else None}
        if mode == "full" and not fits:
            out[mode]["fallback"] = True
        price = ai.pricing.lookup(provider, model) if n and fits and ai.keep_paper() and _claude(provider) and int(paper / 3.3) >= KEEP_MIN_TOKENS else None
        if price:
            # kept ready: the paper is paid once at the price of keeping it and then at the price of reading it back (a price list without those two: a quarter
            # more than the input price, and a tenth of it, in prices.rates, which the usage record uses too); the question, the conversation and the answer cost what they always do
            rate = ai.pricing.rates(price); keep, back = rate["cache_write"], rate["cache_read"]
            other = int(rest / 3.3) * price["in"] + out_tokens * price["out"]
            out[mode]["cost"], out[mode]["next"] = (int(paper / 3.3) * keep + other) / 1e6, (int(paper / 3.3) * back + other) / 1e6
    return out


def invalid_citations(text, n):
    """Passage numbers the model cited that were never given to it (0, or more than n)."""
    bad = set()
    for g in re.findall(r"\((\d+(?:\s*,\s*\d+)*)\)", text):
        bad |= {int(x) for x in re.findall(r"\d+", g) if not 1 <= int(x) <= n}
    return sorted(bad)


def _summary_messages(pdf_path, meta, template_id=None):
    """What a summary from one of your templates sends: the PDF and the template's instructions (the estimate builds the same, so it can tell a saved answer)."""
    import prefs
    interests = ", ".join(t.get("label", t["name"]) for t in dl.load_json(os.path.join(dl.BASE, "interests.json"), {"topics": []})["topics"])
    prompt = prefs.summary_prompt(template_id or prefs.get_settings()["summary_template"], interests=interests, title=meta.get("title", ""),
                                  journal=meta.get("journal", ""), doi=meta.get("doi") or "n/a")
    return [{"role": "user", "content": [{"type": "pdf", "path": pdf_path}, {"type": "text", "text": prompt}]}]


def summary_estimate(pdf_path, meta, template_id=None):
    """What "Generate" in the reader's Summary tab costs before it is pressed: {"provider", "model", "chars", "cost" (None: no price for the model), "saved"}.
    chars is the paper as the provider will read it: for Claude, which is sent the PDF itself, the text of every page and each page as an image (ai.pdf_chars);
    for the others the text that is sent. saved: the answer to this very request (this paper, template and model) is already stored, so Generate shows it
    and costs nothing; cost is then what asking again (⟳) costs. Nothing is sent."""
    provider, model = ai.feature_model("summarize")
    msgs = _summary_messages(pdf_path, meta, template_id)
    if _claude(provider):
        bl.extract(pdf_path)                                 # (the passages are in the index after this, so pdf_chars counts this paper's own text and pages, not a guess from the file's size)
        chars = ai.pdf_chars(pdf_path, True, saved=True)[1]
    else:
        chars = len(ai._pdf_text(pdf_path))
    chars += len(msgs[0]["content"][1]["text"] or "")
    try:
        name, _p, f = ai._route("summarize")
        saved = os.path.exists(os.path.join(ai.CACHE, ai._cache_key(name, f, msgs, None, None, SUMMARY_MAX_TOKENS, False) + ".json"))
    except Exception:  # noqa: BLE001                        (no model chosen, no key, local-only: Generate will say so itself)
        saved = False
    return {"provider": provider, "model": model, "chars": chars, "cost": ai.estimate("summarize", chars, SUMMARY_OUT_TOKENS), "saved": saved}


def summary_stream(pdf_path, meta, template_id=None, fresh=False):
    """A summary of the whole paper from one of your templates, streamed (not saved: the reader offers to save it)."""
    try:
        msgs = _summary_messages(pdf_path, meta, template_id)
        yield {"type": "status", "msg": "Reading the whole paper…"}
        for kind, v in ai.stream("summarize", msgs, max_tokens=SUMMARY_MAX_TOKENS, cache=True, fresh=fresh):
            if kind == "text":
                yield {"type": "text", "delta": v}
            else:
                yield {"type": "done", "provider": v.provider, "model": v.model, "cost": v.cost, "cached": v.cached, "cut_off": v.cut_off}    # cut_off: it stopped at the model's length limit, so the page does not offer to save it as the paper's summary
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


def stream(pdf_path, question, mode="rag", history=None, override=None, fresh=False, pages=None):
    """history = [{"q", "a"}, …] earlier turns of this conversation (last four are used), so follow-ups make sense. pages = (first, last) file pages: the answer comes from that chapter only."""
    try:
        yield {"type": "status", "msg": "Reading the paper…"}
        marks = []
        history = [h for h in (history or []) if h.get("q") and h.get("a")][-4:]
        # a short follow-up ("and why?") is retrieved together with the previous question
        rq = question if not history or len(question.split()) > 8 else history[-1]["q"] + " " + question
        chosen = pick(pdf_path, rq, mode, progress=lambda d, t: marks.append((d, t)), pages=pages)
        if marks:
            yield {"type": "status", "msg": "Indexed %d passages (first time only)." % marks[-1][1]}
        if not chosen:
            yield {"type": "error", "error": "No text could be read from %s (a scanned image?)." % ("this chapter" if pages else "this PDF")}
            return
        import books
        info = books.info_at(pdf_path)                 # a book's passages say where they are in words you could cite (chapter and printed page, or location)
        passages = [{"n": i + 1, "page": b["page"] + 1, "quote": quote_of(b["text"]), **({"where": books.label(info, b["page"] + 1)} if info else {})} for i, b in enumerate(chosen)]
        yield {"type": "passages", "passages": passages}
        body = "\n\n".join("[%d] (p. %d) %s" % (i + 1, b["page"] + 1, b["text"]) for i, b in enumerate(chosen))
        messages = []
        if whole(readable(pdf_path, pages), mode):
            # whole paper: the text goes first, before the conversation and the question, and is the same bytes for every question (every passage in the paper's
            # order, nothing that depends on what was asked), so a provider can reuse it; the mark is what "keep the paper ready" acts on (ai._to_anthropic)
            qs = [h["q"] for h in history] + [question]
            messages.append({"role": "user", "content": [{"type": "text", "text": "Passages from the paper:\n\n%s" % body, "cache": True}, {"type": "text", "text": "Question: %s" % qs[0]}]})
            for h, q in zip(history, qs[1:]):
                messages += [{"role": "assistant", "content": h["a"]}, {"role": "user", "content": "Question: %s" % q}]
        else:
            for h in history:
                messages += [{"role": "user", "content": h["q"]}, {"role": "assistant", "content": h["a"]}]
            messages.append({"role": "user", "content": "Passages from the paper:\n\n%s\n\nQuestion: %s" % (body, question)})
        yield {"type": "status", "msg": "Asking…"}
        for kind, v in ai.stream("ask", messages, system=SYSTEM, max_tokens=2500, cache=True, fresh=fresh, override=override):
            if kind == "text":
                yield {"type": "text", "delta": v}
            else:
                yield {"type": "done", "provider": v.provider, "model": v.model, "cost": v.cost, "cached": v.cached, "cut_off": v.cut_off, "invalid": invalid_citations(v.text, len(chosen))}
    except ai.AIError as e:
        yield {"type": "error", "error": str(e)}
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        yield {"type": "error", "error": "Something went wrong: %s" % re.sub(r"\s+", " ", str(e))[:200]}
