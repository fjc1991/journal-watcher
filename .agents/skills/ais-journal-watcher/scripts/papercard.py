"""Paper cards (G4): one structured, cited summary per paper — research question, topics, theories, constructs, hypotheses (and
whether they held), method, findings, contribution, limitations, future research. The card is a sidecar next to the PDF
(<name>.card.json, like <name>.notes.json) and is the evidence the folder features read: clustering limitations, the Research
Ideas Lab. Every statement carries the numbers of the passages that support it; the card stores each cited passage's page and
opening words, so the reader can jump to it without the model's help.

    papercard.load(pdf) -> card | None            papercard.estimate(pdf) -> {chars, cost, provider, model}
    for m in papercard.stream(pdf, meta, fresh=False): ...     # {"type": "status"|"progress"|"card"|"done"|"error", ...}
    for m in papercard.batch(rows, fresh=False): ...           # one stream for many papers (3 at a time), skips papers that have a card
    papercard.save_edit(pdf, card, base_rev)      # your edits in the reader (rev-guarded, like notes)

The model sees only the paper's body passages (no reference list), numbered; it may leave a field empty rather than guess.
Edited items are marked "edited": true. Regenerating keeps the previous card in state/card_backups/.
"""
import json
import os
import re
import shutil
import sys
import threading
import traceback
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import activity as act  # noqa: E402
import ai  # noqa: E402
import blocks as bl  # noqa: E402
import download as dl  # noqa: E402
import notes_store as ns  # noqa: E402

VERSION = 1
FULL_LIMIT = 320_000          # characters sent; longer papers keep their start and end (question, discussion, limitations)
OUT_TOKENS = 6000
KINDS = ["empirical (quantitative)", "empirical (qualitative)", "mixed methods", "design science", "conceptual / theory", "literature review", "other"]
ROLES = ["uses", "extends", "tests", "challenges", "develops"]
VERDICTS = ["supported", "partly supported", "not supported", "not tested", "unclear"]
BACKUPS = os.path.join(dl.STATE, "card_backups")
_lock = threading.RLock()


def _o(**props):
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


_S, _CITES = {"type": "string"}, {"type": "array", "items": {"type": "integer"}}
_cited = lambda: _o(text=_S, cites=_CITES)  # noqa: E731
SCHEMA = _o(
    kind={"type": "string", "enum": KINDS},
    question=_cited(),
    topics={"type": "array", "items": _S},
    theories={"type": "array", "items": _o(name=_S, role={"type": "string", "enum": ROLES}, note=_S, cites=_CITES)},
    constructs={"type": "array", "items": _o(name=_S, definition=_S, cites=_CITES)},
    hypotheses={"type": "array", "items": _o(label=_S, text=_S, verdict={"type": "string", "enum": VERDICTS}, cites=_CITES)},
    method=_o(design=_S, data=_S, sample=_S, analysis=_S, cites=_CITES),
    findings={"type": "array", "items": _cited()},
    contribution=_cited(),
    implications={"type": "array", "items": _cited()},
    limitations={"type": "array", "items": _o(text=_S, stated={"type": "boolean"}, cites=_CITES)},
    future_research={"type": "array", "items": _cited()},
)
LIMITS = {"topics": 8, "theories": 8, "constructs": 12, "hypotheses": 15, "findings": 10, "implications": 6, "limitations": 10, "future_research": 10}

SYSTEM = """You build a structured "paper card" for an information-systems researcher from the numbered passages of ONE paper.
- Use only what the passages say. If the paper does not say it, leave the text empty ("") or the list empty: never guess or fill in from general knowledge.
- Every statement carries "cites": the numbers of the one to four passages that support it (never a number that was not given).
- Keep texts short: one or two sentences, at most about 45 words, in plain words but with the paper's own terms. Findings keep effect sizes, signs and significance when the paper reports them.
- topics: three to eight short noun phrases naming what the paper is about (for example "platform governance", "app store reviews").
- theories: the theories or frameworks the paper builds on, and the role each plays (uses, extends, tests, challenges, develops).
- hypotheses: each hypothesis or proposition as stated (label like H1 or P2) with the verdict the paper reports.
- limitations: the limitations the authors acknowledge (stated=true), plus at most two further threats to validity that the passages clearly reveal but the authors do not acknowledge (stated=false).
- future_research: what the authors themselves suggest for future work.
- method: design (for example "panel regression with firm fixed effects", "multiple case study", "design science"), the data source, the sample (size, period, unit), and the analysis.
Output only the JSON."""


# ---------------------------------------------------------------- the passages the card is built from

def passages(pdf):
    """(body blocks sent to the model, truncated?). Numbered 1..n in this order; n is what "cites" refer to."""
    bs = [b for b in bl.extract(pdf) if b.get("kind", "body") == "body"]
    if sum(len(b["text"]) for b in bs) <= FULL_LIMIT:
        return bs, False
    head, tail, n = [], [], 0
    for b in bs:
        n += len(b["text"])
        if n > FULL_LIMIT * 0.55:
            break
        head.append(b)
    n = 0
    for b in reversed(bs[len(head):]):
        n += len(b["text"])
        if n > FULL_LIMIT * 0.45:
            break
        tail.append(b)
    return head + tail[::-1], True


def _quote(text, n=130):
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0]


def estimate(pdf, quick=False):
    """What a card costs before you ask for one. cost is None when the model has no price in ai.json.
    quick=True never reads the PDF's text (for a whole folder at once): it uses the saved text if there is some, else a guess from the file size."""
    if quick and not os.path.exists(os.path.join(bl.INDEX, ns.sha1_of(pdf) + ".blocks.json")):
        chars = int(min(FULL_LIMIT, max(30_000, os.path.getsize(pdf) * 0.1))) + len(SYSTEM) + 2500
    else:
        chars = sum(len(b["text"]) for b in passages(pdf)[0]) + len(SYSTEM) + 2500
    provider, model = ai.feature_model("card")
    return {"chars": chars, "cost": ai.estimate("card", chars, OUT_TOKENS * 0.5), "provider": provider, "model": model}


# ---------------------------------------------------------------- the card file

def card_path(pdf):
    return ns.card_path(pdf)


def load(pdf):
    """The saved card, or None. "stale": true when the PDF was replaced after the card was written."""
    try:
        with open(card_path(pdf), encoding="utf-8") as f:
            card = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(card, dict) or not isinstance(card.get("data"), dict):
        return None
    card.setdefault("rev", 1)
    card.setdefault("passages", {})
    card["stale"] = bool(card.get("sha1")) and card["sha1"] != ns.sha1_of(pdf)
    return card


def exists(pdf):
    return os.path.exists(card_path(pdf))


def _write(pdf, card):
    out = {k: v for k, v in card.items() if k != "stale"}
    tmp = card_path(pdf) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    os.replace(tmp, card_path(pdf))


def _s(v, n=600):
    return re.sub(r"\s+", " ", str(v or "")).strip()[:n]


def _cites(v, valid):
    out = []
    for x in v if isinstance(v, list) else []:
        try:
            x = int(x)
        except (TypeError, ValueError):
            continue
        if x in valid and x not in out:
            out.append(x)
    return out[:4]


def clean(raw, valid):
    """Shape a model answer (or an edit from the reader) into the card layout; cites outside `valid` are dropped.
    Returns (data, dropped) where dropped counts the invalid citations that were removed."""
    dropped = [0]

    def cites(item):
        c = item.get("cites") if isinstance(item, dict) else None
        keep = _cites(c, valid)
        dropped[0] += max(0, len(c if isinstance(c, list) else []) - len(keep))
        return keep

    def mk(item, **fields):
        d = {k: fn(item.get(k)) for k, fn in fields.items()}
        d["cites"] = cites(item)
        if item.get("edited") is True:
            d["edited"] = True
        return d

    raw = raw if isinstance(raw, dict) else {}
    lst = lambda k: [x for x in (raw.get(k) if isinstance(raw.get(k), list) else []) if isinstance(x, dict)][:LIMITS[k]]  # noqa: E731
    text = lambda v: _s(v)  # noqa: E731
    pick_v = lambda opts: (lambda v: v if v in opts else "unclear")  # noqa: E731
    obj = lambda v: v if isinstance(v, dict) else {}  # noqa: E731
    data = {
        "kind": raw.get("kind") if raw.get("kind") in KINDS else "other",
        "question": mk(obj(raw.get("question")), text=text),
        "topics": [t for t in (_s(x, 60) for x in (raw.get("topics") if isinstance(raw.get("topics"), list) else [])) if t][:LIMITS["topics"]],
        "theories": [mk(x, name=lambda v: _s(v, 80), role=lambda v: v if v in ROLES else "uses", note=text) for x in lst("theories") if _s(x.get("name"))],
        "constructs": [mk(x, name=lambda v: _s(v, 80), definition=text) for x in lst("constructs") if _s(x.get("name"))],
        "hypotheses": [mk(x, label=lambda v: _s(v, 20), text=text, verdict=pick_v(VERDICTS)) for x in lst("hypotheses") if _s(x.get("text"))],
        "method": mk(obj(raw.get("method")), design=text, data=text, sample=text, analysis=text),
        "findings": [mk(x, text=text) for x in lst("findings") if _s(x.get("text"))],
        "contribution": mk(obj(raw.get("contribution")), text=text),
        "implications": [mk(x, text=text) for x in lst("implications") if _s(x.get("text"))],
        "limitations": [{**mk(x, text=text), "stated": bool(x.get("stated", True))} for x in lst("limitations") if _s(x.get("text"))],
        "future_research": [mk(x, text=text) for x in lst("future_research") if _s(x.get("text"))],
    }
    return data, dropped[0]


def _passage_table(chosen, used):
    return {str(n): {"page": chosen[n - 1]["page"] + 1, "quote": _quote(chosen[n - 1]["text"]), "idx": chosen[n - 1]["idx"]} for n in sorted(used)}


def used_numbers(data):
    out = set()

    def walk(v):
        if isinstance(v, dict):
            for k, x in v.items():
                if k == "cites":
                    out.update(x)
                else:
                    walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    walk(data)
    return out


def save_edit(pdf, card, base_rev):
    """Your edits from the reader. Same rule as notes: a stale rev raises ns.Conflict(current card)."""
    if not isinstance(card, dict) or not isinstance(card.get("data"), dict):
        raise ValueError("bad card")
    with _lock:
        cur = load(pdf)
        if not cur:
            raise ValueError("this paper has no card yet")
        if cur["rev"] != base_rev:
            raise ns.Conflict(cur)
        table = {k: v for k, v in cur["passages"].items()}
        data, _ = clean(card["data"], {int(k) for k in table})
        out = {**cur, "data": data, "rev": cur["rev"] + 1, "edited": True, "passages": {k: v for k, v in table.items() if int(k) in used_numbers(data)}}
        _write(pdf, out)
        return load(pdf)


def _backup(pdf, old):
    os.makedirs(BACKUPS, exist_ok=True)
    shutil.copy(card_path(pdf), os.path.join(BACKUPS, "%s-%s.json" % (ns.sha1_of(pdf)[:12], datetime.now().strftime("%Y%m%d-%H%M%S"))))


# ---------------------------------------------------------------- generating

def _meta_line(meta):
    a = meta.get("authors")
    a = "; ".join(a) if isinstance(a, list) else str(a or "")
    return "%s (%s, %s, %s)" % (meta.get("title", "Untitled"), a[:160], meta.get("year", ""), meta.get("journal", ""))


def _json(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        return json.loads(m.group(0) if m else text)
    except (ValueError, TypeError):
        return None


def generate(pdf, meta, fresh=False, on_chars=None):
    """Write the card (blocking) and save it; returns (card, Result). Raises ai.AIError."""
    chosen, truncated = passages(pdf)
    if not chosen:
        raise ai.AIError("No text could be read from this PDF (a scanned image?).")
    body = "\n\n".join("[%d] (p. %d) %s" % (i + 1, b["page"] + 1, b["text"]) for i, b in enumerate(chosen))
    prompt = "Paper: %s\n\nNumbered passages:\n\n%s\n\nFill in the card." % (_meta_line(meta), body)
    res = None
    for kind, v in ai.stream("card", [{"role": "user", "content": prompt}], system=SYSTEM, schema=SCHEMA, max_tokens=OUT_TOKENS, cache=True, fresh=fresh, cache_if=lambda t: _json(t) is not None):
        if kind == "text" and on_chars:
            on_chars(len(v))
        elif kind == "done":
            res = v
    raw = _json(res.text)
    if raw is None:
        raise ai.AIError("The model’s answer wasn’t valid JSON (it may have been cut off). Try again, or pick a model with a larger output limit in Settings → AI.")
    data, dropped = clean(raw, set(range(1, len(chosen) + 1)))
    with _lock:
        old = load(pdf)
        if old:
            _backup(pdf, old)
        card = {"version": VERSION, "rev": (old["rev"] + 1) if old else 1, "sha1": ns.sha1_of(pdf), "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "model": "%s · %s" % (res.provider, res.model), "cost": res.cost, "truncated": truncated, "invalid": dropped, "edited": False,
                "data": data, "passages": _passage_table(chosen, used_numbers(data))}
        _write(pdf, card)
    return load(pdf), res


def stream(pdf, meta, fresh=False):
    try:
        old = load(pdf)
        if old and not fresh:
            yield {"type": "card", "card": old, "existing": True}
            yield {"type": "done", "cost": 0, "existing": True}
            return
        yield {"type": "status", "msg": "Reading the paper…"}
        n = [0]
        out = {}

        def run():
            try:
                out["card"], out["res"] = generate(pdf, meta, fresh, lambda k: n.__setitem__(0, n[0] + k))
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
        yield {"type": "card", "card": out["card"]}
        yield {"type": "done", "provider": r.provider, "model": r.model, "cost": r.cost, "cached": r.cached}
    except ai.AIError as e:
        yield {"type": "error", "error": str(e)}
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        yield {"type": "error", "error": "Something went wrong: %s" % re.sub(r"\s+", " ", str(e))[:200]}


def batch(rows, fresh=False, workers=3):
    """Cards for many papers at once (library rows). Yields {"type": "start"|"paper"|"error"|"done", ...}; a problem that would hit every
    paper (no key, budget used up, local-only) stops the run, other errors are reported per paper."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    todo = []
    for r in rows:
        pdf = ns.pdf_path(r.get("file"))
        if pdf and (fresh or not exists(pdf)):
            todo.append((r, pdf))
    yield {"type": "start", "total": len(todo), "skipped": len(rows) - len(todo)}
    cost, done = 0.0, 0
    ex = ThreadPoolExecutor(max_workers=workers)
    try:
        futs = {ex.submit(act.wrap(generate), pdf, r, fresh): r for r, pdf in todo}
        for f in as_completed(futs):
            r = futs[f]
            try:
                card, res = f.result()
                done += 1; cost += res.cost or 0
                yield {"type": "paper", "file": r["file"], "title": r.get("title", ""), "done": done, "total": len(todo), "cost": res.cost}
            except ai.AIError as e:
                yield {"type": "error", "file": r["file"], "title": r.get("title", ""), "error": str(e)}
                if re.search(r"budget|isn’t set up|Local-only|No provider|Cancelled", str(e)):
                    break
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                yield {"type": "error", "file": r["file"], "title": r.get("title", ""), "error": re.sub(r"\s+", " ", str(e))[:160]}
        yield {"type": "done", "made": done, "cost": cost}
    finally:
        ex.shutdown(wait=False, cancel_futures=True)


# ---------------------------------------------------------------- reading a card as text

def text_of(card):
    """Everything a card says, as one string (library search, prompts)."""
    out = []

    def walk(v):
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, dict):
            for k, x in v.items():
                if k not in ("cites", "stated", "edited", "role", "verdict", "kind"):
                    walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    walk((card or {}).get("data"))
    return " ".join(out)


def _pg(card, cites):
    pages = sorted({card["passages"][str(c)]["page"] for c in cites if str(c) in card.get("passages", {})})
    return " (p. %s)" % ", ".join(map(str, pages)) if pages else ""


def markdown(card, title=""):
    d, o = card["data"], "# %s\n\n" % title if title else ""
    sec = lambda h, body: ("## %s\n\n%s\n\n" % (h, body)) if body else ""  # noqa: E731
    bullets = lambda items: "\n".join("- %s%s" % (x["text"], _pg(card, x["cites"])) for x in items)  # noqa: E731
    o += "**Type:** %s\n\n" % d["kind"]
    o += sec("Research question", (d["question"]["text"] + _pg(card, d["question"]["cites"])) if d["question"]["text"] else "")
    o += sec("Topics", ", ".join(d["topics"]))
    o += sec("Theory", "\n".join("- **%s** (%s)%s%s" % (t["name"], t["role"], " — " + t["note"] if t["note"] else "", _pg(card, t["cites"])) for t in d["theories"]))
    o += sec("Constructs", "\n".join("- **%s** — %s%s" % (c["name"], c["definition"], _pg(card, c["cites"])) for c in d["constructs"]))
    o += sec("Hypotheses", "\n".join("- **%s** %s — *%s*%s" % (h["label"], h["text"], h["verdict"], _pg(card, h["cites"])) for h in d["hypotheses"]))
    m = d["method"]
    o += sec("Method", "\n".join("- **%s:** %s" % (k.capitalize(), m[k]) for k in ("design", "data", "sample", "analysis") if m[k]) + _pg(card, m["cites"]))
    o += sec("Findings", bullets(d["findings"]))
    o += sec("Contribution", (d["contribution"]["text"] + _pg(card, d["contribution"]["cites"])) if d["contribution"]["text"] else "")
    o += sec("Implications", bullets(d["implications"]))
    o += sec("Limitations", "\n".join("- %s%s%s" % (x["text"], "" if x.get("stated", True) else " *(not acknowledged by the authors)*", _pg(card, x["cites"])) for x in d["limitations"]))
    o += sec("Future research", bullets(d["future_research"]))
    return o.rstrip() + "\n"
