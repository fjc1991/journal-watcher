"""Related-work drafter (W4): a related-work section written from a folder's paper cards, with every claim tied to a note and a page.

The model never sees the PDFs and never writes a reference. It gets each chosen paper's card as numbered notes ("P2-3" = the third note of paper P2:
a finding, a hypothesis, the design…, each with the page it came from) and must end every claim with the ids of the notes that support it:
    [P2-3, P4-1]   a parenthetical citation  ->  (Singhal et al., 2026; Lee et al., 2026)
    {P2-3}         a narrative citation      ->  Singhal et al. (2026)
Afterwards the ids are checked (an id that was not given is dropped and counted), turned into author-year or numbered citations, the reference list is
built from your library's own data (not by the model), and the sentences that carry no citation are listed so you can check or cut them.

    relatedwork.estimate(rows, settings)            -> {papers, cost, words}
    for m in relatedwork.stream(fid, rows, settings, fresh): ...    status | progress | draft | done | error   (saved as Papers/_folders/<id>.draft.json)
    relatedwork.load(fid) / render(doc, style, pages) / markdown(doc) / docx(doc)
"""
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
import evidence as evd  # noqa: E402
import exports as ex  # noqa: E402
import folders as fo  # noqa: E402
import notes_store as ns  # noqa: E402
import papercard as pc  # noqa: E402

_lock = threading.RLock()
WORDS = (300, 600, 1000)
MAX_PAPERS = 30
ID = r"P\d+(?:-\d+)?"
PAREN = re.compile(r"\[\s*(%s(?:\s*[,;]\s*%s)*)\s*\]" % (ID, ID))
NARR = re.compile(r"\{\s*(%s)\s*\}" % ID)
OWN = re.compile(r"\b(?:the present|the current|this|our) (?:study|paper|research|work)\b|\bwe\b", re.I)

SYSTEM = """You write the RELATED WORK section of an information-systems journal article from structured notes on the papers the researcher has chosen.
Rules:
- Use only the notes. Every sentence that makes a claim about a study ends with the ids of the notes that support it. Two forms, and nothing else is ever a citation:
    [P2-3] or [P2-3, P4-1]   at the end of a clause or sentence, for a parenthetical citation;
    {P2-3}                   right after the study's name or a phrase that names it, for a narrative citation ("As {P2-3} show, ...").
  Never cite an id that was not given, never cite a study that is not in the notes, never write author-year yourself, never invent a number, a finding, a reference or a quotation.
- Synthesise, do not summarise one paper after another: group the studies into two to four themes (by question, theory, context or finding), say where they agree and where they differ (design, data, context, results), and mention a limitation only where it matters for the next point.
- Write like a journal article: third person; past tense for what a study found, present tense for what the literature shows. Paragraphs only: no bullet lists, no tables.
- Give each theme a short "## " heading. If the researcher described their own study, finish with a "## Gap and contribution" paragraph that says, from the limitations and future-research notes only, what remains open and how that study fits; otherwise leave that section out.
- Stay close to the target length. If the notes do not support a point, leave it out rather than guessing."""


# ---------------------------------------------------------------- the file

def path(fid):
    return os.path.join(fo.DIR, fid + ".draft.json")


def load(fid):
    d = None
    if os.path.exists(path(fid)):
        try:
            d = json.load(open(path(fid), encoding="utf-8"))
        except (OSError, ValueError):
            d = None
    return d if isinstance(d, dict) and d.get("version") == 1 else None


def _write(fid, doc):
    os.makedirs(fo.DIR, exist_ok=True)
    tmp = path(fid) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path(fid))


def clean_settings(s, files_ok=None):
    s = s if isinstance(s, dict) else {}
    out = {"topic": str(s.get("topic") or "").strip()[:1500], "themes": str(s.get("themes") or "").strip()[:400],
           "words": int(s["words"]) if s.get("words") in WORDS or str(s.get("words")) in map(str, WORDS) else 600,
           "style": "numbered" if s.get("style") == "numbered" else "author-year", "pages": bool(s.get("pages")),
           "files": [f for f in (s.get("files") or []) if isinstance(f, str)][:MAX_PAPERS]}
    if files_ok is not None:
        out["files"] = [f for f in out["files"] if f in files_ok]
    return out


# ---------------------------------------------------------------- the notes the model reads

def _page(card, cites):
    for c in cites or []:
        p = (card.get("passages") or {}).get(str(c))
        if p:
            return p["page"], p["quote"]
    return None, ""


def notes_of(card):
    """[(kind, text, page, quote)] — the statements of a card in a fixed order; their position + 1 is the number in 'P2-3'."""
    d, out = card["data"], []

    def add(kind, text, cites):
        text = re.sub(r"\s+", " ", str(text or "")).strip()
        if text:
            pg, q = _page(card, cites)
            out.append((kind, text[:420], pg, q))
    add("question", d["question"]["text"], d["question"]["cites"])
    for t in d["theories"][:3]:
        add("theory (%s)" % t.get("role", "uses"), "%s%s" % (t["name"], " — " + t["note"] if t.get("note") else ""), t["cites"])
    for h in d["hypotheses"][:5]:
        add("hypothesis %s, %s" % (h.get("label") or "", h.get("verdict") or "unclear"), h["text"], h["cites"])
    m = d["method"]
    add("method", "; ".join(x for x in (m["design"], m["data"] and "data: " + m["data"], m["sample"] and "sample: " + m["sample"], m["analysis"] and "analysis: " + m["analysis"]) if x), m["cites"])
    for f in d["findings"][:6]:
        add("finding", f["text"], f["cites"])
    add("contribution", d["contribution"]["text"], d["contribution"]["cites"])
    for l in d["limitations"][:4]:
        add("limitation" + ("" if l.get("stated", True) else " (not stated by the authors)"), l["text"], l["cites"])
    for f in d["future_research"][:3]:
        add("future research", f["text"], f["cites"])
    return out


def gather(rows, files):
    """Papers with a card among the chosen files: [{id, row, cite, notes}] and the ones without a card."""
    papers, missing = [], []
    for r in rows:
        if files and r["file"] not in files:
            continue
        pdf = ns.pdf_path(r.get("file"))
        card = pc.load(pdf) if pdf else None
        if not card:
            missing.append({"file": r["file"], "title": r.get("title", "")})
            continue
        papers.append({"id": "P%d" % (len(papers) + 1), "row": r, "cite": evd.short_cite(r), "card": card, "notes": notes_of(card)})
    return papers[:MAX_PAPERS], missing


def digest(papers):
    out = []
    for p in papers:
        r = p["row"]
        out.append("%s — %s. “%s”. %s. (%s)" % (p["id"], p["cite"], r.get("title", ""), r.get("journal", ""), p["card"]["data"]["kind"]))
        for i, (kind, text, pg, _) in enumerate(p["notes"], 1):
            out.append("  %s-%d [%s] %s%s" % (p["id"], i, kind, text, " (p. %d)" % pg if pg else ""))
        out.append("")
    return "\n".join(out)


def _prompt(papers, s):
    return ("TARGET LENGTH: about %d words in total.\n\nTHE RESEARCHER'S OWN STUDY (for the closing gap paragraph; may be empty): %s\n\nWHAT THE RESEARCHER WANTS THE THEMES TO BE (optional): %s\n\nNOTES ON THE PAPERS\n\n%s"
            % (s["words"], s["topic"] or "(not described: leave out the closing section)", s["themes"] or "(your choice)", digest(papers)))


def estimate(rows, settings):
    s = clean_settings(settings)
    papers, missing = gather(rows, set(s["files"]))
    chars = len(SYSTEM) + len(_prompt(papers, s))
    return {"papers": len(papers), "missing": len(missing), "words": s["words"], "cost": ai.estimate("draft", chars, int(s["words"] * 1.7) + 400) if papers else 0.0}


# ---------------------------------------------------------------- reading the answer: checking and formatting the citations

def _surnames(r):
    a = r.get("authors")
    names = [x.strip() for x in (a if isinstance(a, list) else re.split(r";\s*", str(a or ""))) if x.strip()]
    return [ex.split_name(n)[0] for n in names]


def label(r):
    """'Singhal et al.' / 'Singhal & Lee' / 'Singhal' for the in-text citation."""
    s = _surnames(r)
    if not s:
        words = str(r.get("title") or "").split()[:3]
        return "“%s…”" % " ".join(words) if words else "Anon."
    return s[0] if len(s) == 1 else "%s & %s" % (s[0], s[1]) if len(s) == 2 else s[0] + " et al."


def reference(r, entry):
    """APA-style reference text from the library's own data: (before the journal, journal, after)."""
    pre, journal, post = ex.apa(entry)
    return pre, journal, post


def check(raw, P, N):
    """Against the tables of the draft (papers P, notes N): how many cited ids do not exist, which sentences carry no citation, which chosen papers were never cited."""
    used, bad, bare = [], 0, []
    for m in list(PAREN.finditer(raw)) + list(NARR.finditer(raw)):
        for i in re.findall(ID, m.group(1)):
            if i in N or i in P:
                used.append(i)
            else:
                bad += 1
    own = False                                  # in the closing "Gap and contribution": a sentence about your own study has no source to cite
    for para in raw.split("\n"):
        t = para.strip()
        if not t:
            continue
        if t.startswith("#"):
            own = bool(re.search(r"gap|contribution", t, re.I))
            continue
        for sent in re.split(r"(?<=[.!?])\s+(?=[A-Z“\"(])", t):
            if len(sent.split()) >= 10 and not PAREN.search(sent) and not NARR.search(sent) and not (own and OWN.search(sent)):
                bare.append(sent[:220])
    used_papers = []
    for i in used:
        if i.split("-")[0] not in used_papers:
            used_papers.append(i.split("-")[0])
    return {"used": used, "invalid": bad, "uncited": bare, "used_papers": used_papers,
            "unused": [{"id": pid, "cite": p["short"], "title": p["title"]} for pid, p in P.items() if pid not in used_papers]}


def tables(papers):
    """What the page needs to make every citation clickable and to format them later: notes with pages, and the papers."""
    entries = {e["row"]["file"]: e for e in ex.entries([p["row"] for p in papers])}
    pt, nt = {}, {}
    for p in papers:
        r, e = p["row"], entries[p["row"]["file"]]
        pre, jr, post = ex.apa(e)
        pt[p["id"]] = {"file": r["file"], "title": r.get("title", ""), "label": label(r), "year": str(e["year"] or r.get("year") or "n.d."), "short": p["cite"],
                       "ref": [pre, jr, post], "key": e["key"], "doi": r.get("doi", "")}
        for i, (kind, text, pg, q) in enumerate(p["notes"], 1):
            nt["%s-%d" % (p["id"], i)] = {"kind": kind, "text": text, "page": pg, "quote": q}
    return pt, nt


def render(doc, style=None, pages=None):
    """The draft with its citations written out: {text (Markdown), refs [{n?, id, text}], cited}. Works from doc['raw'] + doc['papers'] + doc['notes'] only."""
    style = style or doc.get("style") or "author-year"
    pages = doc.get("pages") if pages is None else pages
    P, N, raw = doc["papers"], doc["notes"], doc["raw"]
    order = []                                           # papers in the order they are first cited (numbering, reference list)

    def pid_of(i):
        return i.split("-")[0]

    def seen(pid):
        if pid not in order:
            order.append(pid)
        return order.index(pid) + 1

    def pg(ids, pid):
        ps = sorted({N[i]["page"] for i in ids if i in N and i.startswith(pid + "-") and N[i].get("page")})
        return (", p. %d" % ps[0] if len(ps) == 1 else ", pp. %d–%d" % (ps[0], ps[-1])) if pages and ps else ""

    def paren(m):
        ids = [i for i in re.findall(ID, m.group(1)) if pid_of(i) in P]
        if not ids:
            return ""
        groups = {}
        for i in ids:
            groups.setdefault(pid_of(i), []).append(i)
        nums = {p: seen(p) for p in groups}
        if style == "numbered":
            return "[" + ", ".join(str(nums[p]) for p in groups) + "]"
        return "(" + "; ".join("%s, %s%s" % (P[p]["label"], P[p]["year"], pg(g, p)) for p, g in ((p, groups[p]) for p in groups)) + ")"

    def narr(m):
        i = m.group(1)
        if pid_of(i) not in P:
            return ""
        p = pid_of(i)
        n = seen(p)
        return "%s [%d]" % (P[p]["label"], n) if style == "numbered" else "%s (%s%s)" % (P[p]["label"], P[p]["year"], pg([i], p))
    # numbers follow the order of first citation in the text, so walk the text once in reading order
    pieces, last = [], 0
    for m in sorted(list(PAREN.finditer(raw)) + list(NARR.finditer(raw)), key=lambda m: m.start()):
        pieces.append(raw[last:m.start()])
        pieces.append((paren if m.re is PAREN else narr)(m))
        last = m.end()
    pieces.append(raw[last:])
    text = re.sub(r"\s+([,.;:)])", r"\1", re.sub(r"[ \t]{2,}", " ", "".join(pieces))).replace("( ", "(")
    refs = [{"n": n, "id": pid, "text": "".join(P[pid]["ref"]).strip(), "file": P[pid]["file"], "ref": P[pid]["ref"]} for n, pid in enumerate(order, 1)]
    if style != "numbered":
        refs.sort(key=lambda r: r["text"].lower())
    return {"text": text, "refs": refs, "cited": order}


# ---------------------------------------------------------------- writing it

def stream(fid, rows, settings, fresh=False):
    """Messages: status, progress{chars}, draft{doc}, done{cost, model, provider, cached}, error{error}."""
    try:
        s = clean_settings(settings)
        papers, missing = gather(rows, set(s["files"]))
        if len(papers) < 2:
            yield {"type": "error", "error": "Pick at least two papers that have a card (make the cards in the Evidence tab first)."}
            return
        yield {"type": "status", "msg": "Reading %d cards…" % len(papers)}
        pt, nt = tables(papers)
        n, out = [0], {}

        def run():
            try:
                text = ""
                for kind, v in ai.stream("draft", [{"role": "user", "content": _prompt(papers, s)}], system=SYSTEM, max_tokens=int(s["words"] * 2.2) + 1200, cache=True, fresh=fresh,
                                         cache_if=lambda t: bool(PAREN.search(t) or NARR.search(t))):
                    if kind == "text":
                        text += v
                        n[0] += len(v)
                    else:
                        out["res"] = v
                out["raw"] = text
            except Exception as e:  # noqa: BLE001
                out["err"] = e
        t = threading.Thread(target=act.wrap(run), daemon=True)
        t.start()
        yield {"type": "status", "msg": "Writing the section (about %d words)…" % s["words"]}
        while t.is_alive():
            t.join(1.5)
            if t.is_alive():
                yield {"type": "progress", "chars": n[0]}
        if "err" in out:
            raise out["err"]
        res, raw = out["res"], out["raw"].strip()
        ck = check(raw, pt, nt)
        if not ck["used"]:
            raise ai.AIError("The model wrote text without citing any of the notes, so it was not kept. Try again, or choose fewer papers.")
        with _lock:
            old = load(fid) or {}
            doc = {"version": 1, "rev": old.get("rev", 0) + 1, "settings": s, "style": s["style"], "pages": s["pages"], "raw": raw, "papers": pt, "notes": nt,
                   "model": "%s · %s" % (res.provider, res.model), "cost": res.cost, "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "words": len(re.findall(r"\w+", re.sub(r"#+\s.*", "", PAREN.sub("", NARR.sub("", raw))))), "invalid": ck["invalid"], "uncited": ck["uncited"][:30],
                   "unused": ck["unused"], "missing": missing, "cut_off": res.cut_off}       # cut_off: it stopped at the model's length limit; kept (you can finish it by hand) and marked as unfinished until you change the text
            _write(fid, doc)
        yield {"type": "draft", "doc": {**doc, "render": render(doc)}}
        yield {"type": "done", "cost": res.cost, "cached": res.cached, "model": res.model, "provider": res.provider, "cut_off": res.cut_off}
    except ai.AIError as e:
        yield {"type": "error", "error": str(e)}
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        yield {"type": "error", "error": "Something went wrong: %s" % re.sub(r"\s+", " ", str(e))[:200]}


def update(fid, raw=None, style=None, pages=None, rev=None):
    """Your edits to the text, or only how the citations are written (author-year / numbered, with or without pages): no model call. The ids are checked again."""
    with _lock:
        doc = load(fid)
        if not doc:
            raise KeyError("no draft yet")
        if rev is not None and rev != doc["rev"]:
            raise ValueError("The draft changed in another window.")
        if style in ("author-year", "numbered"):
            doc["style"] = style
        if pages is not None:
            doc["pages"] = bool(pages)
        if raw is not None:
            new = str(raw)[:60000]
            if new.replace("\r\n", "\n").strip() != doc["raw"].replace("\r\n", "\n").strip():
                doc["cut_off"] = False             # you changed the text, so it is yours now: the "cut off" note goes (a save that changes nothing keeps it)
            doc["raw"] = new
            doc["edited"] = True
            ck = check(doc["raw"], doc["papers"], doc["notes"])
            doc.update({"invalid": ck["invalid"], "uncited": ck["uncited"][:30], "unused": ck["unused"],
                        "words": len(re.findall(r"\w+", re.sub(r"#+\s.*", "", PAREN.sub("", NARR.sub("", doc["raw"])))))})
        doc["rev"] += 1
        _write(fid, doc)
        return {**doc, "render": render(doc)}


# ---------------------------------------------------------------- export

def markdown(doc, name=""):
    r = render(doc)
    refs = "\n".join("%s%s" % ("%d. " % x["n"] if doc["style"] == "numbered" else "", "".join(x["ref"][:1]) + "*" + x["ref"][1] + "*" + x["ref"][2] if x["ref"][1] else x["text"]) + "  " for x in r["refs"])
    return "# Related work%s\n\n%s\n\n## References\n\n%s\n" % (" — " + name if name else "", r["text"], refs)


_TEX = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}


def latex(doc, name=""):
    """The section as LaTeX for Overleaf: ## headings become \\subsection, [P2-3] becomes \\citep{key}, {P2-3} becomes \\citet{key} (natbib), with the same keys as the .bib export. Ids that were not given are dropped."""
    P, raw = doc["papers"], doc["raw"]
    esc = lambda t: "".join(_TEX.get(c, c) for c in t)  # noqa: E731
    key = lambda i: P[i.split("-")[0]]["key"] if i.split("-")[0] in P else None
    out, buf = [], []

    def para(b):
        pieces, last = [], 0
        for m in sorted(list(PAREN.finditer(b)) + list(NARR.finditer(b)), key=lambda m: m.start()):
            pieces.append(esc(b[last:m.start()]))
            keys = list(dict.fromkeys(k for k in (key(i) for i in re.findall(ID, m.group(1))) if k))
            if keys:
                pieces.append(("\\citep{%s}" if m.re is PAREN else "\\citet{%s}") % ",".join(keys))
            last = m.end()
        pieces.append(esc(b[last:]))
        return re.sub(r"[ \t]{2,}", " ", re.sub(r"\s+([,.;:])", r"\1", "".join(pieces))).replace("\n", " ").strip()

    def flush():
        if buf:
            out.append(para(" ".join(buf)))
            buf.clear()
    for line in raw.split("\n"):                       # a heading is a line of its own, even with no blank line after it
        t = line.strip()
        if t.startswith("#"):
            flush()
            out.append("\\subsection{%s}" % esc(re.sub(r"^#+\s*", "", t)))
        elif not t:
            flush()
        else:
            buf.append(t)
    flush()
    return "%% Related work%s: written from your paper cards; needs \\usepackage{natbib} and the .bib file exported next to it\n\n%s\n" % (" — " + name if name else "", "\n\n".join(out))


def docx(doc, name=""):
    import io
    from docx import Document
    r, d = render(doc), Document()
    d.add_heading("Related work" + (" — " + name if name else ""), 1)
    buf = []

    def flush():
        if buf:
            d.add_paragraph(" ".join(buf))
            buf.clear()
    for line in r["text"].split("\n"):                       # a heading is a line of its own, even with no blank line after it
        t = line.strip()
        if t.startswith("#"):
            flush()
            d.add_heading(re.sub(r"^#+\s*", "", t), 2)
        elif not t:
            flush()
        else:
            buf.append(t)
    flush()
    d.add_heading("References", 2)
    for x in r["refs"]:
        p = d.add_paragraph((("%d. " % x["n"]) if doc["style"] == "numbered" else "") + x["ref"][0])
        if x["ref"][1]:
            p.add_run(x["ref"][1]).italic = True
        p.add_run(x["ref"][2])
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()
