"""Skim layer (G3): the sentences of a paper that matter, found by lens — research question, hypothesis, theory, sample & data, finding,
limitation, contribution — so you can skim a paper by reading only what is marked. The reader draws them as ghost highlights on the page;
a density slider chooses how many (key points / more / everything); you accept the ones you want and they become real highlights.

    skim.load(pdf) -> {items, dismissed, ...} | None          skim.estimate(pdf) -> {cost, ...}
    for m in skim.stream(pdf, meta, fresh=False): ...         {"type": "status"|"progress"|"skim"|"done"|"error", ...}
    skim.save_dismissed(pdf, ids)                             the proposals you said no to

The model reads the paper's body passages (numbered) and must copy each chosen sentence word for word. The program finds that text in the
passage — a sentence the model cannot point to is dropped, one it re-typed with different quotes or spacing is mapped back to the PDF's own
text — and ranks each pick 1 (the sentence to read if you read nothing else for that lens), 2 (important support) or 3 (useful detail).
The result is <name>.skim.json next to the PDF, so the lenses cost one model call per paper, ever (about the price of a paper card).
"""
import difflib
import hashlib
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
import notes_store as ns  # noqa: E402
import papercard as pc  # noqa: E402

VERSION = 1
OUT_TOKENS = 5000
MAX_PER_LENS = 6
MAX_QUOTE = 420
LENSES = [
    {"id": "question", "name": "Research question", "color": "blue", "hint": "what the paper asks or sets out to do"},
    {"id": "hypothesis", "name": "Hypothesis", "color": "purple", "hint": "hypotheses, propositions and formal predictions"},
    {"id": "theory", "name": "Theory", "color": "orange", "hint": "the theory or framework the paper uses, and how"},
    {"id": "sample", "name": "Sample & data", "color": "gray", "hint": "data source, sample size, setting, period, participants"},
    {"id": "finding", "name": "Finding", "color": "green", "hint": "the main results, with the numbers"},
    {"id": "limitation", "name": "Limitation", "color": "red", "hint": "limitations the authors acknowledge"},
    {"id": "contribution", "name": "Contribution", "color": "yellow", "hint": "what is new: contribution claims and implications"},
]
LENS_IDS = [l["id"] for l in LENSES]
_lock = threading.RLock()


def _o(**props):
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


SCHEMA = _o(items={"type": "array", "items": _o(lens={"type": "string", "enum": LENS_IDS}, passage={"type": "integer"}, quote={"type": "string"}, rank={"type": "integer"})})

SYSTEM = """You help a researcher skim an academic paper. You are given its numbered passages. For each lens below, pick the sentences a skimming reader should see, and copy each one EXACTLY as it appears in the passage: same words, same order, same punctuation, no paraphrase and no corrections. One sentence, or at most two consecutive sentences, and at most 400 characters per pick. "passage" is the number of the passage the sentence is in.
Lenses:
%s
rank: 1 = the sentence to read if you read nothing else for this lens (at most two per lens); 2 = important support; 3 = useful detail. At most %d picks per lens, fewer when the paper has less to say; if the paper has nothing for a lens (no hypotheses, say), pick nothing for it.
Prefer sentences from the body over the abstract when both say the same thing; use the abstract when the body does not state it. Never pick references, headings, captions or acknowledgements. Do not pick the same sentence for two lenses. Output only the JSON.""" % (
    "\n".join("- %s (%s): %s" % (l["id"], l["name"], l["hint"]) for l in LENSES), MAX_PER_LENS)


# ---------------------------------------------------------------- the file

def path(pdf):
    return ns.skim_path(pdf)


def load(pdf):
    try:
        with open(path(pdf), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or not isinstance(d.get("items"), list):
        return None
    d.setdefault("dismissed", [])
    d["stale"] = bool(d.get("sha1")) and d["sha1"] != ns.sha1_of(pdf)
    return d


def _write(pdf, d):
    tmp = path(pdf) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in d.items() if k != "stale"}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path(pdf))


def save_dismissed(pdf, ids):
    with _lock:
        d = load(pdf)
        if not d:
            raise ValueError("this paper has not been skimmed yet")
        have = {i["id"] for i in d["items"]}
        d["dismissed"] = [i for i in dict.fromkeys(ids) if i in have]
        _write(pdf, d)
        return d["dismissed"]


def estimate(pdf):
    chars = sum(len(b["text"]) for b in pc.passages(pdf)[0]) + len(SYSTEM) + 1500
    provider, model = ai.feature_model("skim")
    return {"chars": chars, "cost": ai.estimate("skim", chars, OUT_TOKENS * 0.4), "provider": provider, "model": model}


# ---------------------------------------------------------------- finding the model's sentences in the PDF's own text

_QUOTES = {"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-", "−": "-", "‐": "-", "‑": "-", " ": " "}


def norm_map(s):
    """(normalised text, index of each of its characters in s): lower case, straight quotes and dashes, one space between words."""
    out, idx, space = [], [], False
    for i, ch in enumerate(s):
        ch = _QUOTES.get(ch, ch)
        if ch.isspace():
            if space or not out:
                continue
            ch, space = " ", True
        else:
            space = False
        low = ch.lower()
        out.append(low if len(low) == 1 else ch); idx.append(i)
    return "".join(out), idx


def locate(passage, quote):
    """The text of `passage` that the model's `quote` stands for (the PDF's own wording), or None.
    Exact after normalisation; otherwise the one or two consecutive sentences of the passage that are at least 88% similar."""
    ps, pi = norm_map(passage)
    qs = norm_map(quote)[0].strip()
    if len(qs) < 12:
        return None
    k = ps.find(qs)
    if k >= 0:
        end = pi[k + len(qs) - 1] + 1
        m = re.match(r"[.!?][\"”’')\]]*", passage[end:]) if not re.search(r"[.!?][\"”’')\]]*$", qs) else None      # a quote that stops short of the full stop gets it back
        return passage[pi[k]:end + (m.end() if m else 0)].strip()
    spans, start = [], 0
    for m in re.finditer(r"(?<=[.!?])\s+(?=[A-Z(\[\"“‘'])", passage):
        spans.append((start, m.start())); start = m.end()
    spans.append((start, len(passage)))
    best, bs = None, 0.88
    for i in range(len(spans)):
        for j in (i, i + 1):
            if j >= len(spans):
                continue
            seg = passage[spans[i][0]:spans[j][1]]
            r = difflib.SequenceMatcher(None, qs, norm_map(seg)[0], autojunk=False).ratio()
            if r >= bs:
                best, bs = seg.strip(), r
    return best


def _id(lens, text):
    return hashlib.sha1((lens + "|" + re.sub(r"\W+", "", text.lower())).encode()).hexdigest()[:10]


def build_items(raw, chosen):
    """The model's picks -> checked, ranked, de-duplicated items in reading order. Returns (items, dropped)."""
    items, dropped, seen = [], 0, {}
    for x in raw if isinstance(raw, list) else []:
        if not isinstance(x, dict) or x.get("lens") not in LENS_IDS:
            dropped += 1
            continue
        q = re.sub(r"\s+", " ", str(x.get("quote") or "")).strip()
        try:
            first = int(x.get("passage")) - 1
        except (TypeError, ValueError):
            first = -1
        order = ([first] if 0 <= first < len(chosen) else []) + [i for i in range(len(chosen)) if i != first]
        hit = next(((i, t) for i in order for t in [locate(chosen[i]["text"], q)] if t), None)
        if not hit:
            dropped += 1
            continue
        i, text = hit
        text = text[:MAX_QUOTE].rsplit(" ", 1)[0] if len(text) > MAX_QUOTE else text
        rank = x.get("rank") if x.get("rank") in (1, 2, 3) else 2
        key = re.sub(r"\W+", "", text.lower())
        if key in seen:                                     # the same sentence picked for two lenses: keep the better-ranked one
            if rank < seen[key]["rank"]:
                seen[key].update(lens=x["lens"], rank=rank, id=_id(x["lens"], text))
            dropped += 1
            continue
        it = {"id": _id(x["lens"], text), "lens": x["lens"], "rank": rank, "page": chosen[i]["page"] + 1, "idx": chosen[i]["idx"], "quote": text}
        seen[key] = it
        items.append(it)
    out = []
    for lens in LENS_IDS:                                   # at most MAX_PER_LENS per lens, at most two key points
        mine = sorted((i for i in items if i["lens"] == lens), key=lambda i: (i["rank"], i["idx"]))[:MAX_PER_LENS]
        ones = 0
        for i in mine:
            if i["rank"] == 1:
                ones += 1
                if ones > 2:
                    i["rank"] = 2
        out += mine
    out.sort(key=lambda i: (i["page"], i["idx"], i["quote"]))
    return out, dropped


# ---------------------------------------------------------------- generating

def generate(pdf, meta, fresh=False, on_chars=None):
    chosen, truncated = pc.passages(pdf)
    if not chosen:
        raise ai.AIError("No text could be read from this PDF (a scanned image?).")
    body = "\n\n".join("[%d] (p. %d) %s" % (i + 1, b["page"] + 1, b["text"]) for i, b in enumerate(chosen))
    prompt = "Paper: %s\n\nNumbered passages:\n\n%s\n\nPick the sentences for each lens." % (pc._meta_line(meta), body)
    res = None
    for kind, v in ai.stream("skim", [{"role": "user", "content": prompt}], system=SYSTEM, schema=SCHEMA, max_tokens=OUT_TOKENS, cache=True, fresh=fresh,
                             cache_if=lambda t: isinstance((pc._json(t) or {}).get("items"), list)):
        if kind == "text" and on_chars:
            on_chars(len(v))
        elif kind == "done":
            res = v
    d = pc._json(res.text)
    if not isinstance(d, dict) or not isinstance(d.get("items"), list):
        raise ai.AIError("The model’s answer wasn’t valid JSON (it may have been cut off). Try again, or pick a model with a larger output limit in Settings → AI.")
    items, dropped = build_items(d["items"], chosen)
    if not items:
        raise ai.AIError("The model didn’t point at any sentence it could quote. Try again.")
    with _lock:
        old = load(pdf)
        keep = [i for i in (old or {}).get("dismissed", []) if i in {x["id"] for x in items}]       # a re-skim remembers what you dismissed
        doc = {"version": VERSION, "sha1": ns.sha1_of(pdf), "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "model": "%s · %s" % (res.provider, res.model),
               "cost": res.cost, "truncated": truncated, "dropped": dropped, "items": items, "dismissed": keep}
        _write(pdf, doc)
    return load(pdf), res


def stream(pdf, meta, fresh=False):
    try:
        old = load(pdf)
        if old and not fresh:
            yield {"type": "skim", "skim": old, "existing": True}
            yield {"type": "done", "cost": 0, "existing": True}
            return
        yield {"type": "status", "msg": "Reading the paper…"}
        n, out = [0], {}

        def run():
            try:
                out["doc"], out["res"] = generate(pdf, meta, fresh, lambda k: n.__setitem__(0, n[0] + k))
            except Exception as e:  # noqa: BLE001
                out["err"] = e
        t = threading.Thread(target=act.wrap(run), daemon=True)
        t.start()
        while t.is_alive():
            t.join(1.5)
            if t.is_alive():
                yield {"type": "progress", "chars": n[0]}
        if "err" in out:
            raise out["err"]
        r = out["res"]
        yield {"type": "skim", "skim": out["doc"]}
        yield {"type": "done", "provider": r.provider, "model": r.model, "cost": r.cost, "cached": r.cached}
    except ai.AIError as e:
        yield {"type": "error", "error": str(e)}
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        yield {"type": "error", "error": "Something went wrong: %s" % re.sub(r"\s+", " ", str(e))[:200]}
