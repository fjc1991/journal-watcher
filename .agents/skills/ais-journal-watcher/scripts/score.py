#!/usr/bin/env python3
"""Score new papers against interests.json.

- Reads state/new_papers.json (or state/seen.json with --all)
- Writes state/scored.json (all) and state/shortlist.json (top N above min_score)
- Usage:
    python3 score.py            # score the new batch
    python3 score.py --all      # rescore recently seen papers (state/seen.json)
    python3 score.py --top 25   # show more rows (default 15)
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402

BASE, STATE = paths.BASE, paths.STATE
WEIGHTS = {"title": 3, "keywords": 2, "abstract": 1}


def load(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def term_regex(term):
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])",
                      re.IGNORECASE)


def main():
    args = sys.argv[1:]
    top_n = 15
    if "--top" in args:
        top_n = int(args[args.index("--top") + 1])
    all_mode = "--all" in args

    interests = load(os.path.join(BASE, "interests.json"), {"topics": [], "min_score": 3})
    min_score = interests.get("min_score", 3)
    neg_terms = [term_regex(t) for t in interests.get("negative_terms", [])]

    if all_mode:
        seen = load(os.path.join(STATE, "seen.json"), {})
        papers = [{"id": k, "journal": v.get("journal", "?"), "abbr": "",
                   "title": v.get("title", ""), "url": "", "doi": None,
                   "abstract": None, "keywords": None, "pub_date": "",
                   "publisher": ""} for k, v in list(seen.items())[-150:]]
        print("Rescoring %d recent seen papers (abstracts mostly unavailable in this mode)"
              % len(papers))
    else:
        papers = load(os.path.join(STATE, "new_papers.json"), [])
        if not papers:
            print("No new papers in state/new_papers.json — run fetch_new.py first.")
            return

    topics = []
    for t in interests["topics"]:
        topics.append({"name": t["name"], "label": t.get("label", t["name"]),
                       "terms": [(tm, term_regex(tm)) for tm in t["terms"]]})

    results = []
    for p in papers:
        blob = {
            "title": p.get("title") or "",
            "abstract": p.get("abstract") or "",
            "keywords": " ".join(p.get("keywords") or []),
        }
        score = 0
        topics_hit = {}
        matched = {}
        for t in topics:
            for tm, rx in t["terms"]:
                hits = 0
                for field, w in WEIGHTS.items():
                    if rx.search(blob[field]):
                        hits += w
                if hits:
                    score += hits
                    topics_hit.setdefault(t["label"], 0)
                    topics_hit[t["label"]] += hits
                    matched[tm] = matched.get(tm, 0) + hits
        neg = [tm for tm, rx in zip(interests.get("negative_terms", []), neg_terms)
               if any(rx.search(blob[f]) for f in blob)]
        if neg:
            score = max(0, score - 4 * len(neg))
        p2 = dict(p)
        p2["score"] = score
        p2["topics_hit"] = topics_hit
        p2["matched_terms"] = dict(sorted(matched.items(), key=lambda kv: -kv[1])[:8])
        p2["negative_terms"] = neg
        results.append(p2)

    results.sort(key=lambda x: -x["score"])
    with open(os.path.join(STATE, "scored.json"), "w") as f:
        json.dump(results, f, indent=1)

    # build the shortlist with a per-journal cap so one big feed (ISR/DSS)
    # cannot swamp the others
    maxj = interests.get("max_per_journal", 10)
    per_j = {}
    short = []
    for r in results:
        if r["score"] < min_score:
            continue
        jn = r.get("journal", "?")
        if per_j.get(jn, 0) >= maxj:
            continue
        per_j[jn] = per_j.get(jn, 0) + 1
        short.append(r)
        if len(short) >= top_n:
            break
    with open(os.path.join(STATE, "shortlist.json"), "w") as f:
        json.dump(short, f, indent=1)

    print("\n%-4s %-5s %-7s %-22s %s" % ("#", "JRN", "SCORE", "TOPICS", "TITLE"))
    for i, r in enumerate(results[:top_n], 1):
        tk = ",".join(list(r["topics_hit"].keys())[:3]) or "-"
        flag = "PASS" if r["score"] >= min_score else "     "
        print("%-4d %-5s %-7s %-22s %s %s" % (
            i, r.get("abbr", "?")[:5], r["score"], tk[:22], flag,
            (r.get("title") or "")[:75]))
    print("\nShortlist (score >= %d): %d papers -> state/shortlist.json"
          % (min_score, len(short)))


if __name__ == "__main__":
    main()
