"""Your journals and your interests, as Settings → Journals & interests shows and changes them.

journals.json and interests.json in your data folder are yours: this module only ever changes what you asked for. A journal is switched on or off for the
weekly check ("weekly") and for a search by year ("default_off"); the journals of the lists this version ships (the AIS basket of eleven, the UTD 24) that your
file does not have can be added, switched off, without touching any entry of yours. Topics, their terms and the methods are checked before they are written:
a damaged interests.json would stop screening. Both files are written whole and then put in place, so a quit in between leaves the old one.
"""
import json
import os
import re
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import download as dl  # noqa: E402
import paths  # noqa: E402

BASE = dl.BASE
JOURNALS = os.path.join(BASE, "journals.json")
INTERESTS = os.path.join(BASE, "interests.json")
SETS = {"ais": "AIS basket of eleven", "utd": "UTD 24"}
MAX_TOPICS, MAX_TERMS, MAX_TERM = 40, 200, 80
_lock = threading.RLock()        # one change at a time: each reads the file, changes it and writes it back, and two at once (two switches flipped quickly) would lose one


def _defaults():
    return dl.load_json(os.path.join(paths.DEFAULTS, "journals.json"), {"journals": []}).get("journals") or []


def _same(a, b):
    """The same journal: the same ISSN, or (without one) the same short name."""
    return bool(a.get("issn")) and a.get("issn") == b.get("issn") or a.get("abbr") == b.get("abbr")


def sets_of(j, defaults=None):
    """The lists a journal belongs to. A journals.json from before the lists existed has no "sets": they are then taken from the shipped list, by ISSN."""
    if isinstance(j.get("sets"), list):
        return [s for s in j["sets"] if s in SETS]
    for d in (_defaults() if defaults is None else defaults):
        if d.get("issn") and d.get("issn") == j.get("issn"):
            return [s for s in d.get("sets") or [] if s in SETS]
    return []


def _write(path, obj):
    tmp = dl.tmp_path(path)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _load_journals():
    d = dl.load_json(JOURNALS, {"journals": []}) if os.path.exists(JOURNALS) else {"journals": []}
    if not isinstance(d, dict) or not isinstance(d.get("journals"), list):
        raise ValueError("journals.json in your data folder is not a list of journals; put it right by hand, or remove it to start from the shipped list.")
    return d


def _can_search(j):
    """Whether a journal can be searched by year. HBR can, whatever an entry copied before its year search existed says (pipeline.journals puts that right too)."""
    return bool(j.get("archive", True)) or (j.get("publisher") == "hbr" and not j.get("archive_source"))


def journals():
    """What the page shows: every journal of your file with its switches and lists, and the shipped journals your file does not have."""
    d, defaults = _load_journals(), _defaults()
    mine = [j for j in d["journals"] if isinstance(j, dict) and j.get("abbr")]
    rows = [{"abbr": j["abbr"], "name": j.get("name") or j["abbr"], "publisher": j.get("publisher") or "", "issn": j.get("issn") or "", "per_year": j.get("per_year"),
             "weekly": bool(j.get("weekly", True)), "search": _can_search(j) and not j.get("default_off"), "can_search": _can_search(j),
             "sets": sets_of(j, defaults)} for j in mine]
    missing = [{"abbr": x["abbr"], "name": x.get("name") or x["abbr"], "sets": sets_of(x, defaults)} for x in defaults if not any(_same(x, j) for j in mine)]
    return {"journals": rows, "missing": missing, "sets": SETS}


def add_missing():
    """Add the shipped journals your file does not have, each switched off (no weekly check, not ticked for a search) until you switch it on. No entry of yours
    is changed, and a journal you have under another short name (the same ISSN) is not added twice. Returns the names added."""
    with _lock:
        d = _load_journals()
        mine = [j for j in d["journals"] if isinstance(j, dict)]
        taken = {j.get("abbr") for j in mine}
        added = []
        for x in _defaults():
            if any(_same(x, j) for j in mine) or x.get("abbr") in taken:
                continue
            d["journals"].append(dict(x, weekly=False, default_off=True))
            added.append(x.get("name") or x["abbr"])
        if added:
            _write(JOURNALS, d)
        return added


def switch(abbrs, weekly=None, search=None):
    """Switch journals on or off for the weekly check and/or for a search by year. Only the two switches of the named journals change. A journal that cannot be
    read weekly (SSRN: it is only searched by words) or searched by year keeps that. Returns how many entries changed."""
    want = {str(a) for a in (abbrs or [])}
    with _lock:
        d = _load_journals()
        n = 0
        for j in d["journals"]:
            if not isinstance(j, dict) or j.get("abbr") not in want:
                continue
            before = (j.get("weekly", True), bool(j.get("default_off")))
            if weekly is not None and not j.get("needs_query"):
                if weekly:
                    j.pop("weekly", None)
                else:
                    j["weekly"] = False
            if search is not None and _can_search(j):
                if search:
                    j.pop("default_off", None)
                else:
                    j["default_off"] = True
            n += before != (j.get("weekly", True), bool(j.get("default_off")))
        if n:
            _write(JOURNALS, d)
        return n


def _clean(s, limit):
    return re.sub(r"\s+", " ", str(s or "")).strip()[:limit]


def _name(label, taken):
    base = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") or "topic"
    name, n = base, 2
    while name in taken:
        name, n = "%s_%d" % (base, n), n + 1
    return name


def interests():
    d = dl.load_json(INTERESTS, {"topics": []}) if os.path.exists(INTERESTS) else {"topics": []}
    return {"min_score": d.get("min_score", 3), "topics": [{"name": t.get("name"), "label": t.get("label") or t.get("name"), "terms": list(t.get("terms") or [])}
                                                          for t in d.get("topics") or [] if isinstance(t, dict)],
            "methods": [{"name": m.get("name"), "label": m.get("label") or m.get("name"), "description": m.get("description") or "", "terms": list(m.get("terms") or [])}
                        for m in d.get("methods") or [] if isinstance(m, dict)],
            "negative_terms": list(d.get("negative_terms") or [])}


def _group(items, what, describe=False, was=()):
    """The topics (or methods) as they will be written. An entry keeps its inner name (saved verdicts and runs know a topic by it) and anything else your file
    holds for it that this page does not show."""
    out, taken, labels = [], set(), set()
    old_by = {t.get("name"): t for t in was if isinstance(t, dict) and t.get("name")}
    if not isinstance(items, list):
        raise ValueError("The %ss are not a list." % what)
    if len(items) > MAX_TOPICS:
        raise ValueError("That is more than %d %ss." % (MAX_TOPICS, what))
    for t in items:
        if not isinstance(t, dict):
            raise ValueError("A %s is not filled in." % what)
        label = _clean(t.get("label"), 60)
        if not label:
            raise ValueError("A %s has no name." % what)
        if label.lower() in labels:
            raise ValueError("Two %ss are called “%s”." % (what, label))
        labels.add(label.lower())
        terms = t.get("terms")
        terms = re.split(r"[\n,;]+", terms) if isinstance(terms, str) else terms
        if not isinstance(terms, list):
            raise ValueError("“%s” has no list of words." % label)
        seen, keep = set(), []
        for x in terms:
            x = _clean(x, MAX_TERM)
            if x and x.lower() not in seen:
                seen.add(x.lower())
                keep.append(x)
        if not keep:
            raise ValueError("“%s” needs at least one word or phrase to look for." % label)
        if len(keep) > MAX_TERMS:
            raise ValueError("“%s” has more than %d words and phrases." % (label, MAX_TERMS))
        old = str(t.get("name") or "")
        name = old if old in old_by and old not in taken else _name(label, taken)     # (a topic keeps its inner name, as your file has it; a new one gets one from its label)
        taken.add(name)
        row = dict(old_by.get(name) or {}, name=name, label=label, terms=keep)
        if describe:
            row["description"] = _clean(t.get("description"), 400)
        out.append(row)
    return out


def save_interests(patch):
    """Write your topics (name, words and phrases), methods and the score a paper needs. Everything is checked first; anything else in your file (a comment,
    words to leave out, fields this page does not show) is kept as it is."""
    if not isinstance(patch, dict):
        raise ValueError("Nothing to save.")
    with _lock:
        d = dl.load_json(INTERESTS, {}) if os.path.exists(INTERESTS) else {}
        if not isinstance(d, dict):
            d = {}
        if "topics" in patch:
            topics = _group(patch["topics"], "topic", was=d.get("topics") or [])
            if not topics:
                raise ValueError("Keep at least one topic: papers are screened against your topics.")
            d["topics"] = topics
        if "methods" in patch:
            d["methods"] = _group(patch["methods"], "method", describe=True, was=d.get("methods") or [])
        if "min_score" in patch:
            try:
                score = int(patch["min_score"])
            except (TypeError, ValueError):
                raise ValueError("The score a paper needs is a whole number.")
            if not 1 <= score <= 20:
                raise ValueError("The score a paper needs is between 1 and 20.")
            d["min_score"] = score
        _write(INTERESTS, d)
        return interests()
