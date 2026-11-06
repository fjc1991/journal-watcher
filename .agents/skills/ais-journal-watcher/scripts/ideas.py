"""Research Ideas Lab (Q5): brainstorm research ideas from a folder of papers.

What it combines (all of it shown to you before and after):
  1. the folder's paper cards -> topics, theories, methods, and *clusters of limitations and future-research suggestions* (evidence.py)
  2. current events, searched on the web by the provider (you can untick or add events before they are used)
  3. your focus, constraints and ambition
The model proposes ideas as JSON; each idea names the evidence it rests on (P1 papers, L2 limitation clusters, F3 future-research
clusters, E1 events) and the program checks those ids, so every idea is labelled grounded / partly / speculative honestly.
A second pass scores each idea like a tough editor, and a search of OpenAlex shows the closest existing work.

    ideas.load(fid) / ideas.save(fid, doc, base_rev)            the board, in Papers/_folders/<id>.ideas.json
    ideas.fetch_events(ev, focus) -> (events, Result)           web search (one call)
    for m in ideas.generate(fid, rows, brief, mode): ...        status / progress / ideas / done / error messages
    for m in ideas.develop(fid, idea_id): ...                   a research-proposal outline for one idea (streamed, saved)
    ideas.markdown(fid) / ideas.docx(fid)                       export the board
    ideas.estimate_*                                            what each paid step will cost, roughly
"""
import io
import json
import os
import re
import sys
import threading
import traceback
import uuid
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import activity as act  # noqa: E402
import ai  # noqa: E402
import download as dl  # noqa: E402
import evidence as evd  # noqa: E402
import folders as fo  # noqa: E402

AMBITIONS = {"safe": "safe: incremental extensions that a single researcher can finish with data that is easy to get",
             "balanced": "balanced: a mix of solid extensions and a few more original angles",
             "bold": "bold: unconventional framings, new constructs or contexts; accept higher risk"}
KINDS = ["theory", "empirical", "method", "practice", "design artifact"]
STATUSES = ("new", "shortlist", "rejected")
_lock = threading.RLock()


def _o(**props):
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


_S, _L = {"type": "string"}, {"type": "array", "items": {"type": "string"}}
IDEAS_SCHEMA = _o(ideas={"type": "array", "items": _o(
    title=_S, research_question=_S,
    why_now=_o(text=_S, events=_L),
    gap=_o(text=_S, evidence=_L),
    lens=_S,
    method=_o(approach=_S, identification=_S),
    data=_S,
    contribution=_o(kind={"type": "string", "enum": KINDS}, text=_S),
    journals=_L, risks=_L, first_steps=_L, closest_papers=_L, speculative=_L)})
CRITIQUE_SCHEMA = _o(critiques={"type": "array", "items": _o(
    index={"type": "integer"}, novelty={"type": "integer"}, feasibility={"type": "integer"}, significance={"type": "integer"}, fit={"type": "integer"},
    verdict=_S, main_risk=_S, search_query=_S)})

IDEAS_SYSTEM = """You are a senior information-systems researcher and journal reviewer helping a researcher find publishable research ideas.
You are given the evidence base of a folder of papers the researcher chose: the papers, their limitations and future-research suggestions grouped by meaning,
topics, theories, methods, and (sometimes) dated current events from a web search. Propose distinct research ideas that build on this evidence.
Rules:
- Every idea must rest on gaps visible in the evidence. In gap.evidence list the ids you use: L… (a limitation group), F… (a future-research group), P… (a paper). Use only ids that appear in the evidence; never invent one.
- Say what these papers do not address ("not addressed in these papers"). You cannot verify the wider literature, so do not claim a gap exists "in the literature".
- why_now: use a current event only if one in the list really motivates the idea, and give its E… id in events. If none fits, leave the text empty and events empty. Never invent events, dates or sources.
- Be concrete: a research question a reviewer could judge, the method with the identification or analysis strategy that fits it, and the data the researcher could realistically get.
- Vary the ideas (theory building, empirical test, extension to a new context, method or measurement, design artifact, replication with a twist); do not return near-duplicates.
- journals: choose from the journals offered; contribution.kind says what the paper would contribute.
- speculative: list the assumptions or parts of the idea that are NOT supported by the evidence, so the researcher knows what is a guess.
- first_steps: three concrete things to do in the first two weeks. risks: the two or three that matter.
- Respect the researcher's constraints and ambition. Be specific rather than generic; no filler."""

CRITIQUE_SYSTEM = """You are a tough but fair senior editor at a top information-systems journal. Score each research idea from 1 (poor) to 5 (excellent):
novelty (be sceptical: would a well-read reviewer say "this has been done"?), feasibility (for one researcher with the stated constraints), significance (would the IS community care),
fit (with the researcher's interests and constraints). verdict: one honest sentence. main_risk: the single most likely reason a reviewer would reject it.
search_query: 6 to 10 keywords that would find the closest existing work in a scholarly database. index is the idea's number as given."""

EVENTS_SYSTEM = """You find current events for an information-systems researcher, using web search. Today's date is given. Search for developments in about the last 12 months that bear on the
research topics (regulation and court cases, product launches, incidents and failures, market shifts, standards, notable company moves). Only report events you found in search results,
each with the URL of the page that reports it. Prefer reliable sources; skip rumours and promotional posts. At most 8 events, most relevant first.
Output only JSON: {"events":[{"title":"...","date":"YYYY-MM-DD or YYYY-MM or empty","summary":"at most 40 words","why_it_matters":"at most 30 words, tied to the topics","url":"https://..."}]}"""


# ---------------------------------------------------------------- the board file

def path(fid):
    return os.path.join(fo.DIR, fid + ".ideas.json")


def _default_brief():
    return {"focus": "", "constraints": "", "n": 5, "ambition": "balanced"}


def _empty():
    return {"version": 1, "rev": 0, "brief": _default_brief(), "events": {"fetched": None, "items": [], "cost": 0, "model": ""}, "ideas": [], "runs": {}}


def load(fid):
    d = dl.load_json(path(fid), None) if os.path.exists(path(fid)) else None
    if not isinstance(d, dict):
        return _empty()
    base = _empty()
    base.update({k: v for k, v in d.items() if k in base})
    base["brief"] = {**_default_brief(), **(d.get("brief") or {})}
    base["events"] = {**base["events"], **(d.get("events") or {})}
    return base


def _write(fid, doc):
    os.makedirs(fo.DIR, exist_ok=True)
    tmp = path(fid) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path(fid))


def _brief(b):
    b = b if isinstance(b, dict) else {}
    return {"focus": re.sub(r"\s+", " ", str(b.get("focus") or "")).strip()[:400], "constraints": re.sub(r"\s+", " ", str(b.get("constraints") or "")).strip()[:600],
            "n": max(1, min(10, int(b.get("n") or 5))), "ambition": b.get("ambition") if b.get("ambition") in AMBITIONS else "balanced"}


def _event(e, i):
    return {"id": "E%d" % i, "title": re.sub(r"\s+", " ", str(e.get("title") or "")).strip()[:200], "date": str(e.get("date") or "")[:10],
            "summary": re.sub(r"\s+", " ", str(e.get("summary") or "")).strip()[:400], "why": re.sub(r"\s+", " ", str(e.get("why") or "")).strip()[:300],
            "url": str(e.get("url") or "")[:500], "verified": e.get("verified"), "user": bool(e.get("user")), "keep": e.get("keep") is not False}


def save(fid, client, base_rev):
    """What the board lets you change: the brief, which events are used (and events you add), and per idea: star, status, note, deleting it.
    Everything the model wrote stays as it is. A stale rev raises notes_store.Conflict (same rule as notes)."""
    import notes_store as ns
    if not fo.get(fid):
        raise KeyError("no such folder")
    if not isinstance(client, dict):
        raise ValueError("bad board")
    with _lock:
        cur = load(fid)
        if cur["rev"] != base_rev:
            raise ns.Conflict(cur)
        if "brief" in client:
            cur["brief"] = _brief(client["brief"])
        if isinstance(client.get("events"), dict) and isinstance(client["events"].get("items"), list):
            by = {e["id"]: e for e in cur["events"]["items"]}
            items = []
            for e in client["events"]["items"][:30]:
                if not isinstance(e, dict):
                    continue
                if e.get("user"):
                    items.append({**e, "user": True})
                elif e.get("id") in by:
                    items.append({**by[e["id"]], "keep": e.get("keep") is not False})
            cur["events"]["items"] = [_event(e, i + 1) for i, e in enumerate(items)]
        if isinstance(client.get("ideas"), list):
            by = {x["id"]: x for x in client["ideas"] if isinstance(x, dict) and x.get("id")}
            keep = []
            for idea in cur["ideas"]:
                c = by.get(idea["id"])
                if c is None:
                    continue                                   # deleted on the board
                idea["star"] = bool(c.get("star"))
                idea["status"] = c.get("status") if c.get("status") in STATUSES else idea.get("status", "new")
                idea["note"] = str(c.get("note") or "")[:2000]
                keep.append(idea)
            cur["ideas"] = keep
        cur["rev"] += 1
        _write(fid, cur)
        return cur


# ---------------------------------------------------------------- helpers

def _json(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        return json.loads(m.group(0) if m else text)
    except (ValueError, TypeError):
        return None


def _norm_url(u):
    try:
        s = urlsplit(u.strip())
        return (s.netloc.lower().removeprefix("www.") + s.path.rstrip("/")).lower()
    except ValueError:
        return u


short_cite = evd.short_cite


def estimate_events():
    return ai.estimate("events", 3000, 2500, searches=5)


def estimate_generate(ev, events, brief):
    chars = len(evd.digest(ev, events)) + len(IDEAS_SYSTEM) + 1500
    n = brief["n"]
    g = ai.estimate("ideas", chars, 1100 * n + 500)
    c = ai.estimate("critique", 900 * n + 1500, 350 * n + 300)
    return None if g is None or c is None else g + c


# ---------------------------------------------------------------- current events (web search)

def _events_in(text):
    d = _json(text) or {}
    return [e for e in (d.get("events") if isinstance(d.get("events"), list) else [])[:8] if isinstance(e, dict) and str(e.get("title") or "").strip()]


def fetch_events(ev, focus=""):
    """Search the web for current events around the folder's topics. Returns (items, Result); items carry verified = the URL was among the
    sources the search actually returned (None when the provider gives no source list). If the first try finds nothing (the model sometimes answers
    without searching) it asks once more, insisting on the search tool; the cost of both tries is in the Result."""
    topics = [x["name"] for x in ev["topics"][:10]] + [x["name"] for x in ev["theories"][:4]]
    if not topics:
        topics = [p["title"] for p in ev["papers"][:6]]
    today = datetime.now().strftime("%Y-%m-%d")
    prompt = "Today is %s.\nResearch topics of the folder: %s.\n%s\nFind current events as instructed." % (
        today, "; ".join(dict.fromkeys(topics)) or "information systems", ("Researcher's focus: %s.\n" % focus) if focus else "")
    r = ai.chat("events", [{"role": "user", "content": prompt}], system=EVENTS_SYSTEM, max_tokens=5000, web_search=True, cache=True, cache_if=lambda t: bool(_events_in(t)))
    found = _events_in(r.text)
    if not found:
        r2 = ai.chat("events", [{"role": "user", "content": prompt + "\nYou have not searched yet: use the web search tool now (do not answer from memory), then answer with the JSON."}],
                     system=EVENTS_SYSTEM, max_tokens=5000, web_search=True, cache=True, fresh=True, cache_if=lambda t: bool(_events_in(t)))
        r2.cost = (r.cost or 0) + (r2.cost or 0)
        r, found = r2, _events_in(r2.text)
    src = {_norm_url(s["url"]) for s in r.sources}
    items = []
    for e in found:
        e = {**e, "why": e.get("why_it_matters"), "verified": (_norm_url(str(e.get("url") or "")) in src) if src and e.get("url") else None}
        items.append(e)
    return items, r


def refresh_events(fid, rows, focus=""):
    """Search for current events and save them on the board (events you added yourself stay). Messages: status, events{doc}, done, error."""
    try:
        yield {"type": "status", "msg": "Reading your papers’ topics…"}
        ev = evd.build(rows)
        yield {"type": "status", "msg": "Searching the web for current events (about a minute)…"}
        items, r = fetch_events(ev, focus)
        with _lock:
            cur = load(fid)
            mine = [e for e in cur["events"]["items"] if e.get("user")]
            cur["events"] = {"fetched": datetime.now(timezone.utc).isoformat(timespec="seconds"), "items": [_event(e, i + 1) for i, e in enumerate(mine + items)],
                             "cost": r.cost, "model": "%s · %s" % (r.provider, r.model), "searches": r.searches, "sources": len(r.sources)}
            cur["brief"] = _brief({**cur["brief"], "focus": focus})
            cur["rev"] += 1
            _write(fid, cur)
        yield {"type": "events", "doc": cur}
        yield {"type": "done", "cost": r.cost, "found": len(items), "searches": r.searches, "model": r.model, "provider": r.provider, "cached": r.cached,
               "note": "" if items else re.sub(r"\s+", " ", r.text or "")[:240]}
    except ai.AIError as e:
        yield {"type": "error", "error": str(e)}
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        yield {"type": "error", "error": "Something went wrong: %s" % re.sub(r"\s+", " ", str(e))[:200]}


# ---------------------------------------------------------------- generating ideas

def _snapshot(ev, events):
    """What the ids of one run stood for, kept with the run so the board still makes sense after the folder changes."""
    snap = {"papers": {p["id"]: {"file": p["file"], "title": p["title"], "cite": short_cite(p), "year": p["year"], "journal": p["journal"], "doi": p["doi"]} for p in ev["papers"]},
            "events": {e["id"]: {"title": e["title"], "date": e["date"], "url": e["url"]} for e in events}}
    for key in ("limitations", "future"):
        snap[key] = {c["id"]: {"label": c["label"], "n": c["n"], "papers": c["papers"],
                               "items": [{"paper": x["paper"], "text": x["text"], "page": x["page"], "quote": x["quote"]} for x in c["items"][:6]]} for c in ev[key][:30]}
    return snap


def _clean_idea(x, valid, event_ids):
    lst = lambda v, n: [re.sub(r"\s+", " ", str(i)).strip()[:300] for i in (v if isinstance(v, list) else []) if str(i).strip()][:n]  # noqa: E731
    s = lambda v, n=700: re.sub(r"\s+", " ", str(v or "")).strip()[:n]  # noqa: E731
    obj = lambda v: v if isinstance(v, dict) else {}  # noqa: E731
    gap, why = obj(x.get("gap")), obj(x.get("why_now"))
    ev_ids = [i.strip() for i in lst(gap.get("evidence"), 12) if i.strip() in valid]
    events = [i.strip() for i in lst(why.get("events"), 6) if i.strip() in event_ids]
    kind = obj(x.get("contribution")).get("kind")
    grounding = "grounded" if any(i[0] in "LF" for i in ev_ids) else "partly" if ev_ids or events else "speculative"
    return {"title": s(x.get("title"), 160), "research_question": s(x.get("research_question")),
            "why_now": {"text": s(why.get("text")) if events else "", "events": events},      # no event, no "why now": the model may not invent one
            "gap": {"text": s(gap.get("text")), "evidence": ev_ids},
            "lens": s(x.get("lens"), 300), "method": {"approach": s(obj(x.get("method")).get("approach")), "identification": s(obj(x.get("method")).get("identification"))},
            "data": s(x.get("data")), "contribution": {"kind": kind if kind in KINDS else "empirical", "text": s(obj(x.get("contribution")).get("text"))},
            "journals": lst(x.get("journals"), 5), "risks": lst(x.get("risks"), 4), "first_steps": lst(x.get("first_steps"), 4),
            "closest_papers": [i for i in lst(x.get("closest_papers"), 8) if i in valid and i[0] == "P"], "speculative": lst(x.get("speculative"), 6), "grounding": grounding}


def _openalex_search(query, n=6):
    """Works matching a query: [{title, year, doi, url, cited_by, abstract}] (OpenAlex; [] when it can't be reached)."""
    if os.environ.get("JW_OFFLINE"):          # tests and offline work: no scholarly-database lookups
        raise RuntimeError("offline mode")
    import fetch_new as fx
    import time
    query = re.sub(r"\s+", " ", re.sub(r"[^\w\s-]", " ", query))[:300].strip()          # ":" and "?" are search syntax in OpenAlex and make it answer with an error
    path = "works?search=%s&per-page=%d&select=id,doi,title,publication_year,cited_by_count,abstract_inverted_index" % (quote(query), n)
    d = fx._openalex(path)
    if d is None:                       # keyless OpenAlex answers a burst of requests with "slow down": wait a moment and ask once more
        time.sleep(1.5)
        d = fx._openalex(path)
    if d is None:
        raise RuntimeError("OpenAlex’s daily allowance is used up (a free key in Settings → Paper sources gives you your own)" if fx.openalex_spent() else "OpenAlex didn’t answer")
    out = []
    for w in d.get("results") or []:
        doi = (w.get("doi") or "").replace("https://doi.org/", "")
        out.append({"title": w.get("title") or "", "year": w.get("publication_year"), "doi": doi, "url": ("https://doi.org/" + doi) if doi else w.get("id", ""),
                    "cited_by": w.get("cited_by_count") or 0, "abstract": fx._inverted_to_text(w.get("abstract_inverted_index")) or ""})
    return out


def novelty(idea, query, folder_dois=()):
    """The closest existing work to an idea (OpenAlex), ranked by how close its title+abstract is to the idea's question (local embeddings).
    Searches with the editor's keywords and with the idea's title (a long keyword list often matches nothing). Papers already in the folder are
    shown but do not count towards "work exists": they are what the idea builds on."""
    try:
        seen, works = set(), []
        words = re.findall(r"[\w-]+", query)
        # a long keyword list often matches nothing (every word must match): fall back to the title, then to the first few keywords
        errors = []
        for q in dict.fromkeys(x.strip() for x in (query, idea["title"], " ".join(words[:6]), " ".join(words[:4])) if x.strip()):
            try:
                found = _openalex_search(q)
            except RuntimeError as e:        # one query failing is not the end: the next one may work
                errors.append(e); continue
            for w in found:
                k = re.sub(r"\W+", " ", w["title"].lower()).strip()        # the same paper often comes back under several records
                if w["title"] and k not in seen:
                    seen.add(k); works.append(w)
            if len(works) >= 4:
                break
        if not works and errors:
            raise errors[0]
        if not works:
            return {"checked": True, "works": [], "query": query, "level": "nothing found"}
        vecs = ai.embed(["%s. %s" % (idea["title"], idea["research_question"])] + ["%s. %s" % (w["title"], w["abstract"][:600]) for w in works])
        sims = vecs[1:] @ vecs[0]
        rows = sorted(({"title": w["title"], "year": w["year"], "doi": w["doi"], "url": w["url"], "cited_by": w["cited_by"], "sim": round(float(s), 2),
                        "in_folder": bool(w["doi"] and w["doi"].lower() in folder_dois)} for w, s in zip(works, sims)), key=lambda r: -r["sim"])[:6]
        outside = [r["sim"] for r in rows if not r["in_folder"]]
        top = max(outside) if outside else 0
        return {"checked": True, "works": rows, "query": query, "level": "very close work exists" if top >= 0.85 else "related work exists" if top >= 0.72 else "nothing very close"}
    except Exception as e:  # noqa: BLE001
        return {"checked": False, "error": re.sub(r"\s+", " ", str(e))[:120], "works": []}


def _critique(ideas, brief, interests):
    brief_txt = "Researcher's interests: %s\nConstraints: %s\nFocus: %s" % (interests or "not given", brief["constraints"] or "none", brief["focus"] or "none")
    body = "\n\n".join("Idea %d: %s\nQuestion: %s\nGap: %s\nMethod: %s; %s\nData: %s\nContribution: %s" % (
        i + 1, x["title"], x["research_question"], x["gap"]["text"], x["method"]["approach"], x["method"]["identification"], x["data"], x["contribution"]["text"]) for i, x in enumerate(ideas))
    r = ai.chat("critique", [{"role": "user", "content": brief_txt + "\n\n" + body}], system=CRITIQUE_SYSTEM, schema=CRITIQUE_SCHEMA, max_tokens=350 * len(ideas) + 1500, cache=True, cache_if=lambda t: _json(t) is not None)
    d = _json(r.text) or {}
    out = {}
    for c in d.get("critiques") if isinstance(d.get("critiques"), list) else []:
        try:
            i = int(c["index"]) - 1
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= i < len(ideas):
            sc = {k: max(1, min(5, int(c.get(k) or 3))) for k in ("novelty", "feasibility", "significance", "fit")}
            out[i] = {**sc, "score": round(sum(sc.values()) / 4, 2), "verdict": re.sub(r"\s+", " ", str(c.get("verdict") or "")).strip()[:300],
                      "main_risk": re.sub(r"\s+", " ", str(c.get("main_risk") or "")).strip()[:300], "search_query": re.sub(r"\s+", " ", str(c.get("search_query") or "")).strip()[:200]}
    return out, r


def generate(fid, rows, brief, mode="add"):
    """Messages: status, progress{chars}, ideas{doc}, done{cost, made}, error. Saves the board when it succeeds (mode "replace" drops earlier
    ideas you had not shortlisted or starred; "add" keeps everything)."""
    cost = 0.0
    try:
        brief = _brief(brief)
        yield {"type": "status", "msg": "Reading the evidence from your papers…", "step": 1, "steps": 4}
        ev = evd.build(rows)
        if len(ev["papers"]) < 2:
            yield {"type": "error", "error": "The Ideas Lab needs paper cards for at least two papers in this folder. Make them first (Evidence tab)."}
            return
        board = load(fid)
        events = [e for e in board["events"]["items"] if e.get("keep", True)]
        valid = {p["id"] for p in ev["papers"]} | {c["id"] for c in ev["limitations"]} | {c["id"] for c in ev["future"]} | {e["id"] for e in events}
        interests = ", ".join(t.get("label", t.get("name", "")) for t in dl.load_json(os.path.join(dl.BASE, "interests.json"), {"topics": []})["topics"])
        journals = "; ".join(j.get("name", "") for j in dl.load_json(os.path.join(dl.BASE, "journals.json"), {"journals": []}).get("journals", []))
        user = "EVIDENCE\n%s\n\nRESEARCHER\nInterests: %s\nFocus for this brainstorm: %s\nConstraints (methods, data access, time, target journals): %s\nAmbition: %s\nJournals to choose from: %s\n\nPropose exactly %d ideas." % (
            evd.digest(ev, events), interests or "not given", brief["focus"] or "none given: use what the evidence suggests", brief["constraints"] or "none given",
            AMBITIONS[brief["ambition"]], journals or "any good IS journal", brief["n"])
        yield {"type": "status", "msg": "Brainstorming %d ideas from %d papers…" % (brief["n"], len(ev["papers"])), "step": 2, "steps": 4}
        out, n = {}, [0]

        def run():
            try:
                for kind, v in ai.stream("ideas", [{"role": "user", "content": user}], system=IDEAS_SYSTEM, schema=IDEAS_SCHEMA, max_tokens=min(24000, 1500 * brief["n"] + 2500), cache=True, cache_if=lambda t: bool((_json(t) or {}).get("ideas"))):
                    if kind == "text":
                        n[0] += len(v)
                    else:
                        out["res"] = v
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
        res = out["res"]
        cost += res.cost or 0
        raw = (_json(res.text) or {}).get("ideas")
        if not isinstance(raw, list) or not raw:
            raise ai.AIError("The model didn’t return any ideas (the answer may have been cut off). Try fewer ideas, or a model with a larger output limit.")
        event_ids = {e["id"] for e in events}
        ideas = [x for x in (_clean_idea(i, valid, event_ids) for i in raw if isinstance(i, dict)) if x["title"] and x["research_question"]][:brief["n"] + 2]
        if not ideas:
            raise ai.AIError("The ideas came back empty. Try again.")
        yield {"type": "status", "msg": "Critiquing the ideas like a tough editor…", "step": 3, "steps": 4}
        crit, cres = _critique(ideas, brief, interests)
        cost += cres.cost or 0
        folder_dois = {p["doi"].lower() for p in ev["papers"] if p["doi"]}
        yield {"type": "status", "msg": "Checking what already exists (OpenAlex)…", "step": 4, "steps": 4}
        from concurrent.futures import ThreadPoolExecutor
        queries = [(crit.get(i) or {}).get("search_query") or "%s %s" % (x["title"], x["research_question"]) for i, x in enumerate(ideas)]
        with ThreadPoolExecutor(max_workers=2) as ex:
            nov = list(ex.map(lambda a: novelty(a[0], a[1], folder_dois), zip(ideas, queries)))
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        for i, x in enumerate(ideas):
            x.update({"id": "i" + uuid.uuid4().hex[:8], "run": run_id, "created": datetime.now(timezone.utc).isoformat(timespec="seconds"), "star": False, "status": "new", "note": "",
                      "critique": crit.get(i), "novelty": nov[i]})
        with _lock:
            cur = load(fid)
            cur["brief"] = brief
            if mode == "replace":
                cur["ideas"] = [x for x in cur["ideas"] if x.get("star") or x.get("status") == "shortlist"]
            cur["ideas"] += ideas
            cur["runs"][run_id] = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "model": "%s · %s" % (res.provider, res.model), "cost": round(cost, 4),
                                   "brief": brief, "n_papers": len(ev["papers"]), "evidence": _snapshot(ev, events)}
            used = {x["run"] for x in cur["ideas"]}
            cur["runs"] = {k: v for k, v in cur["runs"].items() if k in used}
            cur["rev"] += 1
            _write(fid, cur)
        yield {"type": "ideas", "doc": cur}
        yield {"type": "done", "cost": cost, "made": len(ideas), "model": res.model, "provider": res.provider, "cached": res.cached}
    except ai.AIError as e:
        yield {"type": "error", "error": str(e)}
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        yield {"type": "error", "error": "Something went wrong: %s" % re.sub(r"\s+", " ", str(e))[:200]}


DEVELOP_SYSTEM = """You are a senior information-systems researcher turning a research idea into a concrete proposal outline for a colleague. Write in Markdown with exactly these sections:
## Working title and abstract (about 120 words)
## Research questions (a main question and two or three sub-questions)
## Theory and hypotheses (the theoretical lens; hypotheses H1… each with the expected direction and the mechanism)
## Research design (sample and data, measures with how each is operationalised, identification or analysis strategy, robustness checks)
## Positioning (what the closest papers do, and what this adds; refer to the folder's papers by the short citations given, never invent references)
## Paper outline (section by section, one or two bullets each)
## Risks and fallbacks
## First two weeks
State clearly what is an assumption. Be specific; no filler."""


def develop(fid, idea_id):
    """Stream a proposal outline for one idea, then save it on the idea (idea["developed"])."""
    try:
        doc = load(fid)
        idea = next((x for x in doc["ideas"] if x["id"] == idea_id), None)
        if not idea:
            yield {"type": "error", "error": "That idea is no longer on the board."}
            return
        snap = (doc["runs"].get(idea.get("run")) or {}).get("evidence", {})
        cites = []
        for i in idea["gap"]["evidence"] + idea["closest_papers"]:
            if i in snap.get("papers", {}):
                cites.append("%s: %s — %s" % (i, snap["papers"][i]["cite"], snap["papers"][i]["title"]))
            elif i in snap.get("limitations", {}) or i in snap.get("future", {}):
                c = (snap.get("limitations", {}).get(i) or snap.get("future", {}).get(i))
                cites.append("%s: %s (shared by %d papers: %s)" % (i, c["label"], c["n"], ", ".join(snap["papers"][p]["cite"] for p in c["papers"] if p in snap["papers"])))
        brief = doc["brief"]
        user = "IDEA\nTitle: %s\nQuestion: %s\nGap: %s\nWhy now: %s\nLens: %s\nMethod: %s; %s\nData: %s\nContribution: %s — %s\nRisks: %s\nSpeculative parts: %s\n\nEVIDENCE IT RESTS ON\n%s\n\nPAPERS IN THE FOLDER (cite as given)\n%s\n\nResearcher's constraints: %s" % (
            idea["title"], idea["research_question"], idea["gap"]["text"], idea["why_now"]["text"] or "n/a", idea["lens"], idea["method"]["approach"], idea["method"]["identification"],
            idea["data"], idea["contribution"]["kind"], idea["contribution"]["text"], "; ".join(idea["risks"]), "; ".join(idea["speculative"]) or "none listed",
            "\n".join(cites) or "none", "\n".join("%s — %s" % (p["cite"], p["title"]) for p in snap.get("papers", {}).values()), brief["constraints"] or "none")
        yield {"type": "status", "msg": "Developing the idea…"}
        text, res = "", None
        for kind, v in ai.stream("ideas", [{"role": "user", "content": user}], system=DEVELOP_SYSTEM, max_tokens=7000, cache=True):
            if kind == "text":
                text += v
                yield {"type": "text", "delta": v}
            else:
                res = v
        with _lock:
            cur = load(fid)
            tgt = next((x for x in cur["ideas"] if x["id"] == idea_id), None)
            if tgt:
                tgt["developed"] = {"text": text, "model": "%s · %s" % (res.provider, res.model), "cost": res.cost, "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
                cur["rev"] += 1
                _write(fid, cur)
        yield {"type": "done", "cost": res.cost, "cached": res.cached, "model": res.model, "provider": res.provider, "rev": cur["rev"]}
    except ai.AIError as e:
        yield {"type": "error", "error": str(e)}
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        yield {"type": "error", "error": "Something went wrong: %s" % re.sub(r"\s+", " ", str(e))[:200]}


# ---------------------------------------------------------------- export

def _order(doc):
    """Board order: shortlisted and starred first, then by the editor's score; rejected ideas are left out."""
    keep = [x for x in doc["ideas"] if x.get("status") != "rejected"]
    return sorted(keep, key=lambda x: (x.get("status") != "shortlist", not x.get("star"), -((x.get("critique") or {}).get("score") or 0)))


def _evidence_lines(idea, doc):
    snap = (doc["runs"].get(idea.get("run")) or {}).get("evidence", {})
    out = []
    for i in idea["gap"]["evidence"]:
        if i in snap.get("papers", {}):
            p = snap["papers"][i]; out.append("%s — %s (%s)" % (i, p["title"], p["cite"]))
        else:
            c = snap.get("limitations", {}).get(i) or snap.get("future", {}).get(i)
            if c:
                where = ["%s%s" % (snap["papers"][x["paper"]]["cite"] if x["paper"] in snap["papers"] else x["paper"], ", p. %s" % x["page"] if x.get("page") else "") for x in c["items"][:4]]
                out.append("%s — %s [%s]" % (i, c["label"], "; ".join(where)))
    return out


def _events_lines(idea, doc):
    snap = (doc["runs"].get(idea.get("run")) or {}).get("evidence", {}).get("events", {})
    return ["%s (%s) %s" % (snap[i]["title"], snap[i]["date"] or "undated", snap[i]["url"]) for i in idea["why_now"]["events"] if i in snap]


def markdown(fid):
    f, doc = fo.get(fid), None
    doc = load(fid)
    o = "# Research ideas — %s\n\n*%d ideas from %d papers · focus: %s · exported %s*\n\n" % (f["name"], len(_order(doc)), len(f["papers"]), doc["brief"]["focus"] or "none", datetime.now().strftime("%Y-%m-%d"))
    for n, x in enumerate(_order(doc), 1):
        c, nv = x.get("critique") or {}, x.get("novelty") or {}
        o += "## %d. %s%s\n\n**Research question.** %s\n\n" % (n, x["title"], " ★" if x.get("star") else "", x["research_question"])
        o += "**Why now.** %s\n%s\n\n" % (x["why_now"]["text"] or "—", "".join("- %s\n" % l for l in _events_lines(x, doc)))
        o += "**The gap.** %s\n%s\n" % (x["gap"]["text"], "".join("- %s\n" % l for l in _evidence_lines(x, doc)))
        o += "\n**Lens.** %s\n\n**Method.** %s\n\n**Identification / analysis.** %s\n\n**Data.** %s\n\n**Contribution (%s).** %s\n\n" % (
            x["lens"], x["method"]["approach"], x["method"]["identification"], x["data"], x["contribution"]["kind"], x["contribution"]["text"])
        if x["journals"]:
            o += "**Fitting journals.** %s\n\n" % ", ".join(x["journals"])
        if c:
            o += "**Editor's view.** novelty %d · feasibility %d · significance %d · fit %d — %s *Main risk:* %s\n\n" % (c["novelty"], c["feasibility"], c["significance"], c["fit"], c["verdict"], c["main_risk"])
        if nv.get("works"):
            o += "**Closest existing work** (%s)\n%s\n" % (nv["level"], "".join("- %s (%s) %s\n" % (w["title"], w["year"], w["url"]) for w in nv["works"][:4]))
        for h, k in (("Risks", "risks"), ("First steps", "first_steps"), ("Not grounded in your papers (speculative)", "speculative")):
            if x[k]:
                o += "**%s**\n%s\n" % (h, "".join("- %s\n" % i for i in x[k]))
        o += "*Grounding: %s*\n\n" % x["grounding"]
        if x.get("note"):
            o += "**My note.** %s\n\n" % x["note"]
        if x.get("developed"):
            o += "### Developed outline\n\n%s\n\n" % re.sub(r"(?m)^## ", "#### ", x["developed"]["text"])
    return o


def docx(fid):
    from docx import Document
    f, doc = fo.get(fid), load(fid)
    d = Document()
    d.add_heading("Research ideas — %s" % f["name"], 0)
    d.add_paragraph("%d ideas from %d papers · focus: %s" % (len(_order(doc)), len(f["papers"]), doc["brief"]["focus"] or "none")).runs[0].italic = True
    bold = lambda label, text: (lambda p: (setattr(p.add_run(label + " "), "bold", True), p.add_run(text)))(d.add_paragraph())  # noqa: E731
    for n, x in enumerate(_order(doc), 1):
        c, nv = x.get("critique") or {}, x.get("novelty") or {}
        d.add_heading("%d. %s%s" % (n, x["title"], " ★" if x.get("star") else ""), 1)
        bold("Research question.", x["research_question"])
        bold("Why now.", x["why_now"]["text"] or "—")
        for l in _events_lines(x, doc):
            d.add_paragraph(l, style="List Bullet")
        bold("The gap.", x["gap"]["text"])
        for l in _evidence_lines(x, doc):
            d.add_paragraph(l, style="List Bullet")
        bold("Lens.", x["lens"]); bold("Method.", x["method"]["approach"]); bold("Identification / analysis.", x["method"]["identification"]); bold("Data.", x["data"])
        bold("Contribution (%s)." % x["contribution"]["kind"], x["contribution"]["text"])
        if x["journals"]:
            bold("Fitting journals.", ", ".join(x["journals"]))
        if c:
            bold("Editor’s view.", "novelty %d · feasibility %d · significance %d · fit %d — %s Main risk: %s" % (c["novelty"], c["feasibility"], c["significance"], c["fit"], c["verdict"], c["main_risk"]))
        if nv.get("works"):
            bold("Closest existing work (%s)." % nv["level"], "")
            for w in nv["works"][:4]:
                d.add_paragraph("%s (%s) %s" % (w["title"], w["year"], w["url"]), style="List Bullet")
        for h, k in (("Risks", "risks"), ("First steps", "first_steps"), ("Not grounded in your papers (speculative)", "speculative")):
            if x[k]:
                bold(h + ".", "")
                for i in x[k]:
                    d.add_paragraph(i, style="List Bullet")
        if x.get("note"):
            bold("My note.", x["note"])
        if x.get("developed"):
            d.add_heading("Developed outline", 2)
            for line in x["developed"]["text"].splitlines():
                if line.startswith("## "):
                    d.add_heading(line[3:], 3)
                elif line.strip():
                    d.add_paragraph(re.sub(r"[*_`]", "", line.lstrip("-• ").strip()), style="List Bullet" if line.lstrip().startswith(("-", "•", "*")) else None)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()
