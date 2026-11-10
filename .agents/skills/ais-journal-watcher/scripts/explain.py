"""One-click prompts on a passage or a figure/table (the reader's ✨ menu).

    for msg in explain.stream(pdf_path, meta, text="…", page=3, prompt_id="plain", image_b64=None, fresh=False): ...
        {"type": "status"|"text"|"done"|"error", ...}   (same shape as ask.stream)

The prompt comes from your prompt library (prompts.json, edited in Settings → Prompts) with {{text}}, {{context}} (the paragraph the
selection sits in), {{title}}, {{authors}}, {{year}} filled in. For a figure or table the picture is attached. Answers are cached
(same paper, same prompt, same text = free), "fresh" asks again.
"""
import os
import re
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ai  # noqa: E402
import blocks as bl  # noqa: E402
import prefs  # noqa: E402

SYSTEM = ("You are a research assistant reading an academic paper alongside the user. Do what the user asks about the passage or figure, "
          "grounded in the paper; if something is not in the paper, say so rather than guess. Be concise and precise; use Markdown.")


def _words(s):
    return set(re.findall(r"[a-z0-9]{3,}", s.lower()))


def context(pdf_path, page, text):
    """The paragraph of the paper that contains (or best overlaps) the selected text, or ''."""
    t = re.sub(r"\s+", " ", text or "").strip().lower()
    if not t:
        return ""
    best, score = None, 0
    for b in bl.extract(pdf_path):
        if b["page"] + 1 != page:
            continue
        if t[:80] in b["text"].lower():
            return b["text"][:1500]
        ov = len(_words(t) & _words(b["text"])) / max(1, len(_words(t)))
        if ov > score:
            best, score = b, ov
    return best["text"][:1500] if best and score > 0.5 else ""


def stream(pdf_path, meta, text="", page=1, prompt_id=None, custom=None, image_b64=None, fresh=False):
    try:
        spec = next((p for p in prefs.get_prompts()["prompts"] if p["id"] == prompt_id), None)
        template = spec["prompt"] if spec else (("{{custom}}\n\nPassage:\n{{text}}" if text else "{{custom}}") if custom else None)
        if not template:
            yield {"type": "error", "error": "Choose a prompt (or write your own)."}
            return
        ctx = context(pdf_path, page, text) if text else ""
        authors = meta.get("authors") or ""
        prompt = prefs.fill(template, text=text, context=ctx, title=meta.get("title", ""), authors=authors if isinstance(authors, str) else "; ".join(authors),
                            year=meta.get("year", ""), custom=custom or "")
        if ctx and "{{context}}" not in template:
            prompt += "\n\nThe paragraph of the paper this comes from:\n" + ctx
        content = [{"type": "text", "text": prompt}] + ([{"type": "image", "media_type": "image/png", "data": image_b64}] if image_b64 else [])
        yield {"type": "status", "msg": "Thinking…"}
        for kind, v in ai.stream("explain", [{"role": "user", "content": content}], system=SYSTEM, max_tokens=1800, cache=True, fresh=fresh):
            if kind == "text":
                yield {"type": "text", "delta": v}
            else:
                yield {"type": "done", "provider": v.provider, "model": v.model, "cost": v.cost, "cached": v.cached, "cut_off": v.cut_off}
    except ai.AIError as e:
        yield {"type": "error", "error": str(e)}
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        yield {"type": "error", "error": "Something went wrong: %s" % re.sub(r"\s+", " ", str(e))[:200]}
