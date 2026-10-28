"""Flashcards and a daily review (G10): questions and answers from your paper cards, scheduled so that what you read stays with you.

Cards live in Papers/_flashcards.json (yours: edit, suspend or delete any of them; nothing here is regenerated behind your back):
    {"version": 1, "settings": {"new_per_day": 10}, "cards": [{id, q, a, file, page, quote, source, created, due, interval, ease, reps, lapses, last, introduced, suspended}]}
Making them: one model call reads the numbered notes of the papers you pick (the same notes and ids as the drafter) and writes a few cards per paper, each with the ids of the notes it
rests on. An id that was not given drops the card; the page and the quote come from the note, so every card can open its paper at the right place. Cards you already have are skipped.
Scheduling is a plain SM-2: Again (back today, ease down), Hard (x1.2), Good (1 day, then 3, then x ease), Easy (4 days, then x ease x 1.3, ease up). At most `new_per_day` new cards a day.

    flashcards.load() / counts(doc) / queue(doc, files=None) / rate(id, rating) / add(q, a, file, page, quote) / update(id, ...) / delete(ids)
    flashcards.estimate(rows, files, per) / for m in flashcards.stream(rows, files, per, fresh): status | progress | cards{added, skipped} | done | error
"""
import json
import os
import re
import sys
import threading
import traceback
import uuid
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import activity as act  # noqa: E402
import ai  # noqa: E402
import download as dl  # noqa: E402
import relatedwork as rw  # noqa: E402

_lock = threading.RLock()
PATH = os.path.join(dl.PAPERS, "_flashcards.json")
NEW_PER_DAY = 10
PER_CALL = 6                 # papers per model call
PER_PAPER = (4, 8, 12)
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["cards"], "properties": {"cards": {"type": "array", "items": {
    "type": "object", "additionalProperties": False, "required": ["paper", "q", "a", "ids"], "properties": {
        "paper": {"type": "string"}, "q": {"type": "string"}, "a": {"type": "string"}, "ids": {"type": "array", "items": {"type": "string"}}}}}}}
SYSTEM = """You write flashcards that help a researcher remember what they read in academic papers, from structured notes on each paper. Notes have ids like P2-3 (the third note of paper P2).
Return JSON {"cards": [...]}; each card has paper (P1, P2…), q, a and ids (the notes it rests on).
Good cards test one idea each: what a construct means as this paper uses it, what a theory predicts, a hypothesis and whether it was supported, what was found (direction and size, with the setting), why a design or method was chosen, a limitation and why it matters.
Rules: the question must make sense on its own (name the paper's setting or author when the question would otherwise be ambiguous); the answer is at most 35 words and says something specific; no yes/no questions; no trivia (page numbers,
section names, the journal); use only the notes, never outside knowledge or a number that is not in a note; no two cards on the same point; write the number of cards per paper that you are asked for (fewer only when the notes of a paper cannot support that many; never more). Output only the JSON."""


# ---------------------------------------------------------------- the file

def _empty():
    return {"version": 1, "settings": {"new_per_day": NEW_PER_DAY}, "cards": []}


def load():
    d = None
    if os.path.exists(PATH):
        try:
            d = json.load(open(PATH, encoding="utf-8"))
        except (OSError, ValueError):
            d = None
    if not isinstance(d, dict) or not isinstance(d.get("cards"), list):
        return _empty()
    d.setdefault("settings", {}).setdefault("new_per_day", NEW_PER_DAY)
    return d


def _save(doc):
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PATH)


def today():
    return date.today()


def _d(s):
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- scheduling

def schedule(c, rating, now=None):
    """The card after an answer (rating 1 Again · 2 Hard · 3 Good · 4 Easy): a new dict with interval, ease, reps, lapses, due, last."""
    now = now or today()
    ease, iv, reps, lapses = float(c.get("ease") or 2.5), int(c.get("interval") or 0), int(c.get("reps") or 0), int(c.get("lapses") or 0)
    if rating == 1:
        reps, lapses, ease, iv = 0, lapses + 1, max(1.3, ease - 0.2), 0
    elif rating == 2:
        iv, ease, reps = (max(1, round(iv * 1.2)) if reps else 1), max(1.3, ease - 0.15), reps + 1
    elif rating == 3:
        iv, reps = (1 if reps == 0 else 3 if reps == 1 else max(iv + 1, round(iv * ease))), reps + 1
    elif rating == 4:
        iv, ease, reps = (4 if reps == 0 else max(iv + 2, round(iv * ease * 1.3))), ease + 0.15, reps + 1
    else:
        raise ValueError("rating must be 1 to 4")
    return {**c, "interval": iv, "ease": round(ease, 2), "reps": reps, "lapses": lapses, "due": (now + timedelta(days=iv)).isoformat(), "last": now.isoformat(), "introduced": c.get("introduced") or now.isoformat()}


def preview(c, now=None):
    """What each answer would mean, for the buttons: days until the card comes back."""
    return {r: schedule(c, r, now)["interval"] for r in (1, 2, 3, 4)}


def is_new(c):
    return not int(c.get("reps") or 0) and not c.get("introduced")


def counts(doc, files=None, now=None):
    now = now or today()
    cs = [c for c in doc["cards"] if not c.get("suspended") and (not files or c["file"] in files)]
    intro_today = sum(1 for c in doc["cards"] if c.get("introduced") == now.isoformat())
    left_new = max(0, int(doc["settings"].get("new_per_day", NEW_PER_DAY)) - intro_today)
    due = [c for c in cs if not is_new(c) and (_d(c.get("due")) or now) <= now]
    new = [c for c in cs if is_new(c)]
    return {"due": len(due), "new": len(new), "new_today": min(left_new, len(new)), "total": len(cs), "suspended": sum(1 for c in doc["cards"] if c.get("suspended")),
            "learned": sum(1 for c in cs if int(c.get("reps") or 0) >= 2), "left_new": left_new}


def queue(doc, files=None, now=None):
    """Today's cards: what is due (oldest first), then new cards up to today's allowance. Cards answered "Again" come back in the same session (the page re-queues them)."""
    now = now or today()
    cs = [c for c in doc["cards"] if not c.get("suspended") and (not files or c["file"] in files)]
    due = sorted([c for c in cs if not is_new(c) and (_d(c.get("due")) or now) <= now], key=lambda c: (c.get("due") or "", c["created"]))
    k = counts(doc, files, now)["new_today"]
    new = sorted([c for c in cs if is_new(c)], key=lambda c: (c["created"], c["id"]))[:k]
    return due + new


def _find(doc, cid):
    c = next((c for c in doc["cards"] if c["id"] == cid), None)
    if not c:
        raise KeyError("no such card")
    return c


def rate(cid, rating):
    with _lock:
        doc = load()
        c = _find(doc, cid)
        c.update(schedule(c, int(rating)))
        _save(doc)
        return c, counts(doc)


def _clean(s, n):
    return re.sub(r"\s+", " ", str(s or "")).strip()[:n]


def add(q, a, file="", page=None, quote="", source="manual"):
    q, a = _clean(q, 500), _clean(a, 900)
    if not q or not a:
        raise ValueError("A card needs a question and an answer.")
    with _lock:
        doc = load()
        c = {"id": uuid.uuid4().hex[:10], "q": q, "a": a, "file": str(file or ""), "page": int(page) if page else None, "quote": _clean(quote, 200), "source": source, "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "due": today().isoformat(), "interval": 0, "ease": 2.5, "reps": 0, "lapses": 0, "last": "", "introduced": "", "suspended": False}
        doc["cards"].append(c)
        _save(doc)
        return c


def update(cid, q=None, a=None, suspended=None):
    with _lock:
        doc = load()
        c = _find(doc, cid)
        if q is not None:
            c["q"] = _clean(q, 500) or c["q"]
        if a is not None:
            c["a"] = _clean(a, 900) or c["a"]
        if suspended is not None:
            c["suspended"] = bool(suspended)
        _save(doc)
        return c


def delete(ids):
    ids = set(ids)
    with _lock:
        doc = load()
        n = len(doc["cards"])
        doc["cards"] = [c for c in doc["cards"] if c["id"] not in ids]
        _save(doc)
        return n - len(doc["cards"])


def set_new_per_day(n):
    with _lock:
        doc = load()
        doc["settings"]["new_per_day"] = max(0, min(100, int(n)))
        _save(doc)
        return doc["settings"]


# ---------------------------------------------------------------- making cards from paper cards

def _prompt(papers, per):
    return "CARDS TO WRITE: %d for each paper (fewer only when a paper's notes cannot support that many).\n\nNOTES ON THE PAPERS\n\n%s" % (per, rw.digest(papers))


def estimate(rows, files, per=8):
    papers, missing = rw.gather(rows, set(files or []))
    per = per if per in PER_PAPER else 8
    chars = sum(len(SYSTEM) + len(_prompt(papers[i:i + PER_CALL], per)) for i in range(0, len(papers), PER_CALL))
    return {"papers": len(papers), "missing": len(missing), "calls": -(-len(papers) // PER_CALL) if papers else 0,
            "cost": ai.estimate("flashcards", chars, 90 * per * len(papers) + 200 * (-(-len(papers) // PER_CALL))) if papers else 0.0}


def _json(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        return json.loads(m.group(0) if m else text)
    except (ValueError, TypeError):
        return None


def assemble(d, pt, nt, per, have):
    """Cards from the model's JSON, held to the notes it was given: [{q, a, file, page, quote}], the number dropped for a missing note, and the number skipped as already yours."""
    out, bad, dup, count = [], 0, 0, {}
    seen = {(c["file"], re.sub(r"\W+", " ", c["q"].lower()).strip()) for c in have}
    for x in d.get("cards") if isinstance(d.get("cards"), list) else []:
        if not isinstance(x, dict):
            continue
        q, a = _clean(x.get("q"), 500), _clean(x.get("a"), 900)
        pid = str(x.get("paper") or "").strip()
        ids = [i for i in re.findall(rw.ID, " ".join(str(i) for i in x.get("ids") or [])) if i in nt and i.split("-")[0] in pt]
        if pid not in pt and ids:
            pid = ids[0].split("-")[0]
        if not q or not a or pid not in pt or not ids:
            bad += 1
            continue
        key = (pt[pid]["file"], re.sub(r"\W+", " ", q.lower()).strip())
        if key in seen:
            dup += 1
            continue
        if count.get(pid, 0) >= per:
            continue
        seen.add(key)
        count[pid] = count.get(pid, 0) + 1
        n = next((nt[i] for i in ids if nt[i].get("page")), nt[ids[0]])
        out.append({"q": q, "a": a, "file": pt[pid]["file"], "page": n.get("page"), "quote": n.get("quote") or ""})
    return out, bad, dup


def stream(rows, files, per=8, fresh=False):
    """Messages: status, progress{tick}, cards{added, dropped, skipped, total}, done{cost, …}, error{error}."""
    try:
        per = per if per in PER_PAPER else 8
        papers, missing = rw.gather(rows, set(files or []))
        if not papers:
            yield {"type": "error", "error": "None of the chosen papers has a card yet (make the cards in a folder's Evidence tab first)."}
            return
        pt, nt = rw.tables(papers)
        have = load()["cards"]
        added, bad, dup, cost, model = [], 0, 0, 0.0, ""
        chunks = [papers[i:i + PER_CALL] for i in range(0, len(papers), PER_CALL)]
        for k, ch in enumerate(chunks, 1):
            yield {"type": "status", "msg": "Writing cards for %d paper%s%s…" % (len(ch), "" if len(ch) == 1 else "s", " (%d of %d)" % (k, len(chunks)) if len(chunks) > 1 else "")}
            out = {}

            def run():
                try:
                    out["res"] = ai.chat("flashcards", [{"role": "user", "content": _prompt(ch, per)}], system=SYSTEM, schema=SCHEMA, max_tokens=700 + 120 * per * len(ch), cache=True, fresh=fresh,
                                         cache_if=lambda t: isinstance(_json(t), dict) and isinstance(_json(t).get("cards"), list))
                except Exception as e:  # noqa: BLE001
                    out["err"] = e
            t = threading.Thread(target=act.wrap(run), daemon=True)
            t.start()
            n = 0
            while t.is_alive():
                t.join(1.5)
                if t.is_alive():
                    n += 1
                    yield {"type": "progress", "tick": n}
            if "err" in out:
                raise out["err"]
            res = out["res"]
            d = _json(res.text)
            if not isinstance(d, dict) or not isinstance(d.get("cards"), list):
                raise ai.AIError("The model’s answer wasn’t valid JSON (it may have been cut off). Try again.")
            sub = {p["id"]: pt[p["id"]] for p in ch}
            new, b, dp = assemble(d, sub, nt, per, have + added)
            added += new
            bad += b
            dup += dp
            cost += res.cost or 0.0
            model = "%s · %s" % (res.provider, res.model)
        with _lock:
            doc = load()
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for c in added:
                doc["cards"].append({"id": uuid.uuid4().hex[:10], **c, "source": "card", "created": now, "due": today().isoformat(), "interval": 0, "ease": 2.5, "reps": 0, "lapses": 0, "last": "", "introduced": "", "suspended": False})
            _save(doc)
        yield {"type": "cards", "added": len(added), "dropped": bad, "skipped": dup, "missing": len(missing), "total": len(doc["cards"])}
        yield {"type": "done", "cost": cost, "model": model}
    except ai.AIError as e:
        yield {"type": "error", "error": str(e)}
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        yield {"type": "error", "error": "Something went wrong: %s" % re.sub(r"\s+", " ", str(e))[:200]}
