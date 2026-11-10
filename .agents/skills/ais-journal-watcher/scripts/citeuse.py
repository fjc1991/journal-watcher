"""How does this paper use that reference? (G8) The reader finds the sentences of the paper that cite a reference (no model: the text layer is searched), and this turns them into one or two plain sentences:
what the reference is used for, and whether the authors build on it, contrast with it, or just mention it. Cached; a fraction of a cent.

    citeuse.answer(ref_text, contexts, fresh=False) -> {"text", "cost", "cached", "model"}
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ai  # noqa: E402

SYSTEM = """You are reading an academic paper. You get one entry of its reference list and the sentences of the paper that cite it. In one or two plain sentences (at most 55 words) say how the paper uses that reference:
what idea, result, method or dataset it is cited for, and whether the authors build on it, contrast with it, criticise it, or only mention it. Use only the sentences given; if they do not say, say that the paper only mentions it. No preamble, no Markdown."""


def answer(ref_text, contexts, fresh=False):
    ctx = [re.sub(r"\s+", " ", str(c)).strip()[:420] for c in contexts if str(c).strip()][:6]
    if not ctx:
        raise ai.AIError("No sentence of the paper cites this reference in its text.")
    msg = "REFERENCE: %s\n\nSENTENCES THAT CITE IT:\n%s" % (re.sub(r"\s+", " ", str(ref_text))[:500], "\n".join("%d. %s" % (i + 1, c) for i, c in enumerate(ctx)))
    res = ai.chat("explain", [{"role": "user", "content": msg}], system=SYSTEM, max_tokens=220, cache=True, fresh=fresh)
    return {"text": res.text.strip(), "cost": res.cost, "cached": res.cached, "model": res.model, "cut_off": res.cut_off}
