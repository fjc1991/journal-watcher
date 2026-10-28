"""Take your library elsewhere.

    exports.bibtex(rows) / exports.ris(rows)       .bib for Overleaf/LaTeX, RIS for EndNote and Mendeley (rows = pipeline.library() rows)
    exports.annotated_pdf(pdf_path, notes)         the PDF with your highlights as standard annotations (Preview, Acrobat, Mendeley)
    exports.notes_markdown(rows) / notes_docx(rows) your highlights and notes, one section per paper, with a reference list

Volume, issue and pages are not in _index.csv, so they are fetched from Crossref once per DOI and cached in
state/crossref_cache.json; without a network the entries are simply less complete.
"""
import io
import json
import os
import re
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import download as dl  # noqa: E402
import importer  # noqa: E402
import library_search as ls  # noqa: E402
import notes_store as ns  # noqa: E402

CACHE = os.path.join(dl.STATE, "crossref_cache.json")
COLOR = {"yellow": "#ffe066", "green": "#8ce99a", "blue": "#74c0fc", "pink": "#faa2c1", "orange": "#ffc078",
         "purple": "#d0bfff", "red": "#ff8787", "gray": "#ced4da"}
PARTICLES = {"van", "von", "der", "den", "de", "del", "della", "di", "da", "dos", "du", "la", "le", "ten", "ter"}
STOP = {"the", "a", "an", "of", "on", "in", "for", "and", "to", "is", "are", "with", "from", "how", "why", "what", "does", "do", "can", "when", "using", "toward", "towards"}


# ---------------------------------------------------------------- metadata
def _crossref(rows):
    cache = dl.load_json(CACHE, {}) if os.path.exists(CACHE) else {}
    todo = sorted({r["doi"] for r in rows if r.get("doi") and r["doi"] not in cache})
    if todo:
        with ThreadPoolExecutor(6) as ex:
            for doi, meta in zip(todo, ex.map(importer.crossref_meta, todo)):
                if meta:
                    cache[doi] = {k: meta.get(k, "") for k in ("volume", "issue", "pages", "publisher", "container", "pub_date")}
        os.makedirs(dl.STATE, exist_ok=True)
        with open(CACHE + ".tmp", "w") as f:
            json.dump(cache, f)
        os.replace(CACHE + ".tmp", CACHE)
    return cache


def split_name(n):
    """'Jing Tian' -> ('Tian', 'Jing'); 'Ludwig van Beethoven' -> ('van Beethoven', 'Ludwig'); 'Tian, Jing' kept as is."""
    n = re.sub(r"\s+", " ", n).strip()
    if "," in n:
        a, b = n.split(",", 1)
        return a.strip(), b.strip()
    parts = n.split(" ")
    if len(parts) == 1:
        return parts[0], ""
    i = len(parts) - 1
    while i > 1 and parts[i - 1].lower() in PARTICLES:
        i -= 1
    return " ".join(parts[i:]), " ".join(parts[:i])


def entries(rows):
    """One dict per paper with everything a citation needs."""
    cr = _crossref(rows)
    out = []
    for r in rows:
        c = cr.get(r.get("doi", ""), {})
        names = [split_name(a) for a in re.split(r";\s*", r.get("authors", "")) if a.strip()]
        year = (c.get("pub_date") or "")[:4] or r.get("year", "")
        out.append({"row": r, "authors": names, "title": r.get("title", ""), "journal": c.get("container") or r.get("journal", ""), "year": year,
                    "volume": c.get("volume", ""), "issue": c.get("issue", ""), "pages": c.get("pages", ""), "doi": r.get("doi", ""),
                    "url": r.get("url", ""), "keywords": [t for t in re.split(r";\s*", r.get("matched_topics", "")) if t]})
    keys = set()
    for e in out:
        fam = re.sub(r"[^a-z]", "", unicodedata.normalize("NFKD", e["authors"][0][0] if e["authors"] else "anon").encode("ascii", "ignore").decode().lower()) or "anon"
        word = next((w for w in re.findall(r"[a-z]{4,}", unicodedata.normalize("NFKD", e["title"]).encode("ascii", "ignore").decode().lower()) if w not in STOP), "paper")
        k = base = "%s%s%s" % (fam, e["year"], word)
        n = 0
        while k in keys:
            n += 1; k = base + chr(96 + n)
        keys.add(k); e["key"] = k
    return out


# ---------------------------------------------------------------- BibTeX / RIS
def _tex(s):
    return re.sub(r"([&%$#_])", r"\\\1", s or "")


def bibtex(rows):
    out = []
    for e in entries(rows):
        f = [("author", " and ".join("%s, %s" % (a, g) if g else a for a, g in e["authors"])), ("title", "{%s}" % _tex(e["title"])),
             ("journal", _tex(e["journal"])), ("year", e["year"]), ("volume", e["volume"]), ("number", e["issue"]),
             ("pages", e["pages"].replace("-", "--") if "--" not in e["pages"] else e["pages"]), ("doi", e["doi"]), ("url", e["url"]),
             ("keywords", ", ".join(e["keywords"]))]
        out.append("@article{%s,\n%s\n}\n" % (e["key"], ",\n".join("  %s = {%s}" % (k, v) for k, v in f if v)))
    return "\n".join(out)


def ris(rows):
    out = []
    for e in entries(rows):
        L = ["TY  - JOUR"] + ["AU  - %s%s" % (a, ", " + g if g else "") for a, g in e["authors"]] + ["TI  - " + e["title"], "JO  - " + e["journal"]]
        for tag, v in (("PY", e["year"]), ("VL", e["volume"]), ("IS", e["issue"]), ("DO", e["doi"]), ("UR", e["url"])):
            if v:
                L.append("%s  - %s" % (tag, v))
        if e["pages"]:
            sp, _, ep = e["pages"].partition("-")
            L += ["SP  - " + sp] + (["EP  - " + ep.lstrip("-")] if ep else [])
        L += ["KW  - " + k for k in e["keywords"]] + ["ER  - ", ""]
        out.append("\n".join(L))
    return "\n".join(out)


def apa(e):
    """Plain-text APA 7 reference: (text, italic_span_text) so Word can italicise the journal."""
    def init(g):
        return " ".join(p[0] + "." for p in re.split(r"[\s-]+", g) if p)
    au = ["%s, %s" % (a, init(g)) if g else a for a, g in e["authors"]]
    who = au[0] if len(au) == 1 else ", ".join(au[:-1]) + ", & " + au[-1] if au else "Anonymous"
    vol = e["volume"] + ("(%s)" % e["issue"] if e["issue"] else "") if e["volume"] else ""
    tail = ", ".join(x for x in (vol, e["pages"].replace("--", "–").replace("-", "–")) if x)
    pre = "%s (%s). %s. " % (who, e["year"] or "n.d.", e["title"].rstrip("."))
    return pre, e["journal"], (", " + tail if tail else "") + "." + (" https://doi.org/" + e["doi"] if e["doi"] else "")


# ---------------------------------------------------------------- annotated PDF
def annotated_pdf(pdf_path, notes, tags=()):
    import pymupdf
    tg = {t["id"]: t for t in tags}
    doc = pymupdf.open(pdf_path)
    try:
        for h in notes.get("highlights", []):
            col = tuple(int(COLOR.get(h.get("color"), "#ffe066")[i:i + 2], 16) / 255 for i in (1, 3, 5))
            note = " ".join(x for x in [h.get("comment", ""), " ".join("#" + tg[t]["name"].replace(" ", "-") for t in h.get("tags", []) if t in tg)] if x)
            kind = h.get("kind", "highlight")
            for part in h.get("parts", []):
                if not 1 <= part["page"] <= len(doc):
                    continue
                page = doc[part["page"] - 1]
                r = page.rect
                rects = []
                for x, y, w, hh in part["rects"]:
                    rc = pymupdf.Rect(x * r.width, y * r.height, (x + w) * r.width, (y + hh) * r.height)
                    rects.append(rc * page.derotation_matrix if page.rotation else rc)
                if not rects:
                    continue
                if kind == "note":                      # a sticky note
                    a = page.add_text_annot(rects[0].tl, note or " ", icon="Comment")
                    a.set_colors(stroke=col)
                elif kind == "area":                    # a box around a figure or table
                    a = page.add_rect_annot(rects[0])
                    a.set_colors(stroke=col)
                    a.set_border(width=2)
                else:                                   # highlight, underline or strikethrough: standard text-markup annotations
                    quads = [x.quad for x in rects]
                    a = {"underline": page.add_underline_annot, "strike": page.add_strikeout_annot}.get(kind, page.add_highlight_annot)(quads=quads)
                    a.set_colors(stroke=col)
                a.set_info(content=note, title="Journal Watcher")
                a.update()
        return doc.tobytes(garbage=3, deflate=True)
    finally:
        doc.close()


# ---------------------------------------------------------------- notes as Markdown / Word
def _tags(ids, tg):
    return " ".join("#" + (tg[t].get("category", "") + "-" if tg[t].get("category") else "") .replace(" ", "-") + tg[t]["name"].replace(" ", "-") for t in ids or [] if t in tg)


def _label(h):
    """What to print for an annotation: the text, or a stand-in for a sticky note / a marked figure."""
    k = h.get("kind", "highlight")
    if k == "note":
        return "📝 " + (h.get("comment") or "(note)")
    if k == "area":
        return "[figure or table]" + (" — " + h["comment"] if h.get("comment") else "")
    return {"underline": "__%s__", "strike": "~~%s~~"}.get(k, "%s") % h.get("text", "")


def _paper_notes(row, tg):
    """Neutral structure: (excerpts/notes in reading order, highlights by page)."""
    n = ls.notes_of(row) or {"highlights": [], "cards": []}
    first = lambda h: (h.get("parts") or [{}])[0].get("page", 0)  # noqa: E731
    hls = {h["id"]: h for h in n["highlights"]}

    def page(c):
        s = c.get("src") or {}
        h = hls.get(s.get("hl"))
        return first(h) if h else s.get("page") or 9999
    cards = sorted(n["cards"], key=lambda c: (page(c), c.get("y", 0)))
    hs = sorted(n["highlights"], key=lambda h: (first(h), ((h.get("parts") or [{}])[0].get("rects") or [[0, 0]])[0][1]))
    return cards, hs, page, first


def notes_markdown(rows, title=None):
    tg = {t["id"]: t for t in ns.load_tags()["tags"]}
    es = entries(rows)
    o = "# %s\n\n" % title if title else ""
    for e in es:
        r = e["row"]
        cards, hs, page, first = _paper_notes(r, tg)
        o += "%s %s\n\n%s — %s %s%s\n\n" % ("##" if title else "#", e["title"], ", ".join(a + (", " + g if g else "") for a, g in e["authors"]),
                                              e["journal"], e["year"], " · https://doi.org/" + e["doi"] if e["doi"] else "")
        tk = (r.get("state") or {}).get("takeaway")
        if tk:
            o += "**My takeaway:** %s\n\n" % tk
        if cards:
            o += "**Notes**\n\n"
            for c in cards:
                if c.get("type") == "excerpt":
                    o += "> %s\n>\n> — p. %s %s\n\n%s" % (c.get("text", "").replace("\n", "\n> "), page(c), _tags(c.get("tags"), tg), c["note"] + "\n\n" if c.get("note") else "")
                elif c.get("type") == "ai":
                    o += "**Q: %s**\n\n%s\n\n*(%s)*\n\n" % (c.get("question", ""), c.get("text", ""), c.get("model", "AI"))
                else:
                    o += c.get("text", "") + "\n\n"
        if hs:
            o += "**Highlights**\n\n"
            pg = 0
            for h in hs:
                if first(h) != pg:
                    pg = first(h); o += "*Page %s*\n\n" % pg
                o += "- %s%s %s\n" % (_label(h), "\n  - *%s*" % h["comment"] if h.get("comment") and h.get("text") else "", _tags(h.get("tags"), tg))
            o += "\n"
    o += "\n## References\n\n" + "\n\n".join("".join(apa(e)) for e in sorted(es, key=lambda e: (e["authors"][0][0] if e["authors"] else "", e["year"]))) + "\n"
    return o


def notes_docx(rows, title=None):
    from docx import Document
    tg = {t["id"]: t for t in ns.load_tags()["tags"]}
    es = entries(rows)
    d = Document()
    d.add_heading(title or (es[0]["title"] if len(es) == 1 else "Reading notes"), 0)
    for e in es:
        cards, hs, page, first = _paper_notes(e["row"], tg)
        if len(es) > 1 or title:
            d.add_heading(e["title"], 1)
        p = d.add_paragraph("%s — %s %s" % (", ".join(a + (", " + g if g else "") for a, g in e["authors"]), e["journal"], e["year"]))
        p.runs[0].italic = True
        tk = (e["row"].get("state") or {}).get("takeaway")
        if tk:
            d.add_paragraph().add_run("My takeaway: ").bold = True
            d.paragraphs[-1].add_run(tk)
        if cards:
            d.add_heading("Notes", 2)
            for c in cards:
                if c.get("type") == "excerpt":
                    d.add_paragraph("%s (p. %s)" % (c.get("text", ""), page(c)), style="Quote")
                    if c.get("note"):
                        d.add_paragraph(c["note"])
                elif c.get("type") == "ai":
                    d.add_paragraph().add_run("Q: " + c.get("question", "")).bold = True
                    d.add_paragraph(c.get("text", ""))
                else:
                    d.add_paragraph(c.get("text", ""))
        if hs:
            d.add_heading("Highlights", 2)
            for h in hs:
                d.add_paragraph("%s%s (p. %s)" % (_label(h), " — " + h["comment"] if h.get("comment") and h.get("text") else "", first(h)), style="List Bullet")
    d.add_heading("References", 1)
    for e in sorted(es, key=lambda e: (e["authors"][0][0] if e["authors"] else "", e["year"])):
        pre, ital, post = apa(e)
        para = d.add_paragraph(pre)
        para.add_run(ital).italic = True
        para.add_run(post)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()
