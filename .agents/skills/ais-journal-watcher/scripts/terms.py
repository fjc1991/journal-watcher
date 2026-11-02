"""Search by terms: which papers contain one of the words or phrases you typed (Watch → Search terms).

A paper matches when its title, abstract or keywords contain ANY of the terms. How a term is matched:

  * case is ignored ("Difference-in-Differences" finds "difference-in-differences");
  * hyphens, dashes, slashes and spaces between the words of a term are interchangeable or may be missing
    ("difference-in-differences" also finds "difference in differences" and "difference‐in‐differences" with a Unicode hyphen);
  * each word of four letters or more may end in -s or -es ("difference-in-differences" also finds "difference-in-difference" and "differences-in-differences");
  * whole words only: "did" does not match inside "candidate";
  * a short acronym typed with a capital after its first letter (DiD, DID, SEM, RDD; a single word of at most five letters or digits) is matched only where
    the text, too, has a capital after its first letter: DiD finds "DiD" and "DID" but not the ordinary word "did" (nor "Did" at the start of a sentence).
    Typed in lower case ("did"), it is matched whatever its case, and then the ordinary word matches too.

Nothing here calls the network or an AI model: matching is instant and free.
"""
import html
import re
import unicodedata

_SEP = r"[\s\-‐-―−_/]*"          # between the words of a term: spaces, hyphens (ASCII and Unicode), dashes, underscores, slashes, or nothing
_WORD_SPLIT = re.compile(r"[\s\-‐-―−_/]+")
MAX_TERMS = 12


def parse(text_or_list):
    """The terms from what was typed: a list, or one string separated by commas, semicolons, new lines or a "+" with spaces on both sides ("a + b"; "C++" stays one
    term). Quotes, brackets and punctuation around a term are dropped, duplicates (ignoring
    case, except for acronyms) removed, at most MAX_TERMS kept, each at most 80 characters. A term needs at least two letters or digits (or one with + or #: C++, C#)."""
    raw = text_or_list if isinstance(text_or_list, list) else re.split(r"[,;\n]|\s\+\s", str(text_or_list or ""))
    out, seen = [], set()
    for t in raw:
        t = re.sub(r"\s+", " ", str(t or "")).strip().strip("\"'“”‘’()[]{}.:!?").strip()[:80]    # (brackets and punctuation around a term: "(DiD)" is DiD)
        n = len(re.sub(r"[^0-9A-Za-zÀ-ɏ]", "", t))
        if n < 2 and not (n and re.search(r"[+#]", t)):                    # (C++ and C# are terms; a lone letter is not)
            continue
        key = t if is_acronym(t) else t.lower()
        if key not in seen:
            seen.add(key)
            out.append(t)
    return out[:MAX_TERMS]


def is_acronym(term):
    """A short acronym typed with a capital after its first letter: DiD, DID, SEM, RDD, LLMs is not (six letters), "Did" is not (only its first letter)."""
    t = (term or "").strip()
    return bool(re.fullmatch(r"[0-9A-Za-z]{2,5}", t)) and any(c.isupper() for c in t[1:])


def _word(w):
    """One word of a term, with its singular and plural, whichever was typed: difference ↔ differences, focus ↔ focuses, approach ↔ approaches, status ↔ statuses.
    Words under four letters as typed."""
    if not (len(w) >= 4 and w.isalpha()):
        return re.escape(w)
    lw, forms = w.lower(), {w}
    if re.search(r"(s|x|z|ch|sh)es$", lw) and len(w) >= 6:
        forms.add(w[:-2])                                                   # approaches → approach, statuses → status
    elif lw.endswith("s") and not lw.endswith("ss") and len(w) >= 5:
        forms.add(w[:-1])                                                   # differences → difference
    return "(?:" + "|".join(re.escape(f) + "(?:e?s)?" for f in sorted(forms, key=len, reverse=True)) + ")"


def compile_term(term):
    """(regex, acronym?) for one term. An acronym may also be written in its plural (DiDs, SEMs)."""
    words = [w for w in _WORD_SPLIT.split(term.strip()) if w]
    body = re.escape(term.strip()) + "s?" if is_acronym(term) else _SEP.join(_word(w) for w in words)
    return re.compile(r"(?<![0-9A-Za-zÀ-ɏ])(" + body + r")(?![0-9A-Za-zÀ-ɏ])", re.I), is_acronym(term)


def _clean(text):
    """Plain text: tags (<jats:p>, <i>) removed, but only tag-shaped ones: abstracts often hold "p < 0.01 … n > 500", which a looser rule would delete."""
    t = re.sub(r"</?[A-Za-z][\w:.-]*(?:\s[^<>]*)?/?>", " ", str(text or ""))
    return unicodedata.normalize("NFKC", html.unescape(t))


def _shouting(text):
    """Mostly capital letters (some Crossref titles are written in capitals): there a capital tells nothing, so an acronym is not matched in such a field."""
    letters = [c for c in text if c.isalpha()]
    return len(letters) >= 12 and sum(c.isupper() for c in letters) > 0.8 * len(letters)


def hits(paper, terms, compiled=None):
    """[{term, field, text}] for every term found in the paper's title, abstract or keywords (the first place each term is found; `text` is how the paper wrote it)."""
    compiled = compiled or [(t, *compile_term(t)) for t in terms]
    kw = paper.get("keywords")
    fields = [("title", _clean(paper.get("title"))), ("abstract", _clean(paper.get("abstract"))),
              ("keywords", _clean("; ".join(kw) if isinstance(kw, list) else kw))]
    out = []
    for term, rx, acro in compiled:
        for name, text in fields:
            if acro and _shouting(text):
                continue
            found = next((m.group(1) for m in rx.finditer(text) if not acro or any(c.isupper() for c in m.group(1)[1:])), None)
            if found:
                out.append({"term": term, "field": name, "text": found})
                break
    return out


def matcher(terms):
    """A function paper -> hits (compiled once, for a long list of papers)."""
    compiled = [(t, *compile_term(t)) for t in terms]
    return lambda p: hits(p, terms, compiled)


def describe(h):
    """“Matched: difference-in-differences (abstract), DiD (title)” for a paper's hits."""
    return "Matched: " + ", ".join("%s (%s)" % (x["text"], x["field"]) for x in h) if h else ""
