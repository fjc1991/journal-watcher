"""Read a bibliography you already have (W6): RIS (EndNote, Mendeley, Zotero, most databases) or BibTeX (Overleaf, Mendeley, Google Scholar).

    bibimport.parse(text, name="") -> [{title, authors: ["Given Family", …], year, journal, doi, abstract, type, key}]       (format is detected from the content)
    bibimport.match(entries, rows) -> the same entries with "file" (the paper in your library, or "") and "has_doi"

The papers are not downloaded here: the page shows what you have and what you do not, and the ones you tick go through the same download routes as every other pick
(open access first, then publisher sign-ins and library access).
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import importer  # noqa: E402

MAX_ENTRIES = 3000
ACCENTS = {"`": "\u0300", "'": "\u0301", "^": "\u0302", "~": "\u0303", '"': "\u0308", "=": "\u0304", ".": "\u0307", "u": "\u0306", "v": "\u030c", "c": "\u0327", "H": "\u030b", "r": "\u030a"}
SPECIAL = {r"\ss": "ß", r"\o": "ø", r"\O": "Ø", r"\ae": "æ", r"\AE": "Æ", r"\aa": "å", r"\AA": "Å", r"\l": "ł", r"\L": "Ł", r"\&": "&", r"\%": "%", r"\_": "_", r"\#": "#", r"\$": "$", "---": "—", "--": "–"}


def _tex(s):
    """LaTeX in a BibTeX field -> plain text (accents, a few special letters, braces)."""
    import unicodedata
    s = str(s or "")
    s = re.sub(r"\\([`'^~\"=.uvcHr])\s*\{?\\?([A-Za-z])\}?", lambda m: unicodedata.normalize("NFC", m.group(2) + ACCENTS.get(m.group(1), "")), s)
    for k, v in SPECIAL.items():
        s = s.replace(k, v)
    s = re.sub(r"\\(?:textit|textbf|emph|textrm|mathrm|text)\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"[{}]", "", s).replace("~", " ")
    return re.sub(r"\s+", " ", s).strip()


def _name(a):
    """'Rai, Arun' / 'Arun Rai' / 'Rai, A.' -> 'Arun Rai'."""
    a = re.sub(r"\s+", " ", str(a or "")).strip()
    if not a:
        return ""
    if "," in a:
        last, _, first = a.partition(",")
        return (first.strip() + " " + last.strip()).strip()
    return a


def _year(s):
    m = re.search(r"(1[5-9]\d\d|20\d\d)", str(s or ""))
    return m.group(1) if m else ""


def _entry(**kw):
    e = {"title": "", "authors": [], "year": "", "journal": "", "doi": "", "abstract": "", "type": "", "key": ""}
    e.update(kw)
    e["doi"] = importer.clean_doi(e["doi"])
    e["title"] = re.sub(r"\s+", " ", e["title"]).strip().rstrip(".")
    return e


# ---------------------------------------------------------------- RIS

def parse_ris(text):
    out, cur = [], None
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        m = re.match(r"^([A-Z][A-Z0-9])\s{1,2}-\s?(.*)$", line)
        if not m:
            if cur is not None and line.strip() and cur.get("_last") in ("AB", "N2", "TI", "T1"):          # a wrapped line continues the last field
                cur[cur["_last"]] = cur.get(cur["_last"], "") + " " + line.strip()
            continue
        tag, val = m.group(1), m.group(2).strip()
        if tag == "TY":
            cur = {"TY": val, "AU": []}
            continue
        if cur is None:
            continue
        if tag == "ER":
            out.append(cur)
            cur = None
            continue
        if tag in ("AU", "A1"):
            cur.setdefault("AU", []).append(val)
        elif tag == "A2":
            pass                                           # editors are not authors
        else:
            cur[tag] = (cur.get(tag, "") + " " + val).strip() if tag in ("AB", "N2") else cur.get(tag) or val
        cur["_last"] = tag
    if cur is not None:
        out.append(cur)
    res = []
    for r in out:
        res.append(_entry(title=r.get("TI") or r.get("T1") or "", authors=[_name(a) for a in r.get("AU", []) if _name(a)], year=_year(r.get("PY") or r.get("Y1") or r.get("DA") or ""),
                          journal=r.get("JO") or r.get("JF") or r.get("T2") or r.get("JA") or "", doi=r.get("DO") or r.get("DI") or r.get("UR") or "", abstract=r.get("AB") or r.get("N2") or "", type=r.get("TY", ""), key=r.get("ID", "")))
    return res


# ---------------------------------------------------------------- BibTeX

def _balanced(s, i, open_="{", close="}"):
    depth, j = 0, i
    while j < len(s):
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == open_:
            depth += 1
        elif c == close:
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return len(s) - 1


def parse_bibtex(text):
    out, i = [], 0
    while True:
        m = re.compile(r"@(\w+)\s*([{(])", re.S).search(text, i)
        if not m:
            break
        kind = m.group(1).lower()
        end = _balanced(text, m.end() - 1, m.group(2), "}" if m.group(2) == "{" else ")")
        body = text[m.end():end]
        i = end + 1
        if kind in ("comment", "string", "preamble"):
            continue
        key, _, rest = body.partition(",")
        fields, j = {}, 0
        while j < len(rest):
            fm = re.compile(r"\s*([A-Za-z][\w:-]*)\s*=\s*").match(rest, j)
            if not fm:
                nxt = rest.find(",", j)
                if nxt < 0:
                    break
                j = nxt + 1
                continue
            name, j = fm.group(1).lower(), fm.end()
            if j < len(rest) and rest[j] == "{":
                k = _balanced(rest, j)
                val, j = rest[j + 1:k], k + 1
            elif j < len(rest) and rest[j] == '"':
                k = j + 1
                while k < len(rest) and not (rest[k] == '"' and rest[k - 1] != "\\"):
                    k += 1
                val, j = rest[j + 1:k], k + 1
            else:
                k = j
                while k < len(rest) and rest[k] not in ",}":
                    k += 1
                val, j = rest[j:k].strip(), k
            fields[name] = val
            c = rest.find(",", j)
            j = c + 1 if c >= 0 else len(rest)
        authors = [_name(_tex(a)) for a in re.split(r"\s+and\s+", fields.get("author", ""), flags=re.I) if _tex(a)]
        out.append(_entry(title=_tex(fields.get("title", "")), authors=authors, year=_year(fields.get("year") or fields.get("date") or ""), journal=_tex(fields.get("journal") or fields.get("journaltitle") or fields.get("booktitle") or ""),
                          doi=fields.get("doi") or fields.get("url") or "", abstract=_tex(fields.get("abstract", "")), type=kind, key=key.strip()))
    return out


def parse(text, name=""):
    """Entries from RIS or BibTeX text (detected from the content, the file name only breaks a tie). Entries without a title are dropped."""
    text = str(text or "").lstrip("\ufeff")
    is_bib = bool(re.search(r"^\s*@\w+\s*[{(]", text, re.M))
    is_ris = bool(re.search(r"^TY\s{1,2}-", text, re.M))
    if is_bib and is_ris:
        is_bib = name.lower().endswith((".bib", ".bibtex"))
    entries = parse_bibtex(text) if is_bib else parse_ris(text) if is_ris else []
    return [e for e in entries if e["title"]][:MAX_ENTRIES]


def _nt(t):
    return re.sub(r"[^a-z0-9]+", "", str(t or "").lower())


def match(entries, rows):
    """Each entry with the file of the library paper it is (by DOI, or by the same title when there is no DOI), or ""."""
    by_doi = {importer.clean_doi(r.get("doi")).lower(): r["file"] for r in rows if importer.clean_doi(r.get("doi"))}
    by_title = {_nt(r.get("title")): r["file"] for r in rows if _nt(r.get("title"))}
    out = []
    for e in entries:
        f = by_doi.get(e["doi"].lower(), "") if e["doi"] else ""
        f = f or by_title.get(_nt(e["title"]), "")
        out.append({**e, "file": f, "has_doi": bool(e["doi"])})
    return out
