"""End-to-end smoke test of the dashboard in a real (headless) browser, on a throw-away copy of the project with three small
papers and a fake AI provider on localhost. Nothing in your real Papers/ folder is touched; no API key and no network needed.

    .venv/bin/python .agents/skills/ais-journal-watcher/scripts/test_ui.py

Library: search, filters, status, folders, highlights hub, exports, "reading alone writes nothing".
Reader: highlights, kinds (underline, strike, sticky note), undo/redo, links and citation cards, back button, area box -> picture card,
explain with your prompts (cached), Ask with history / regenerate / compare / follow-ups, summary templates, settings page and legend.
"""
import base64
import csv
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
PROJECT = os.path.abspath(os.path.join(SKILL, "..", "..", ".."))
TMP = tempfile.mkdtemp(prefix="jw-ui-")
FAILS = []
SEEN = []          # requests the fake AI provider received


def check(name, cond, extra=""):
    print("  %s  %s%s" % ("PASS" if cond else "FAIL", name, "  (%s)" % extra if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


# ---------------------------------------------------------------- a fake OpenAI-compatible provider
class Fake(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):      # the dashboard stopped reading (a cancelled answer): not an error
            pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        SEEN.append((self.path, body))
        if self.path.endswith("/embeddings"):
            rows = []
            for i, t in enumerate(body["input"]):
                v = [0.0] * 512
                for w in t.lower().replace(".", " ").replace("?", " ").replace(",", " ").split():
                    if len(w) > 3:
                        v[zlib.crc32(w.encode()) % 512] += 1
                rows.append({"index": i, "embedding": v})
            data = json.dumps({"data": rows}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
        system = body["messages"][0]["content"] if body["messages"][0]["role"] == "system" else ""
        if system.startswith(("You build a structured", "You find current events", "You are a senior information-systems researcher", "You are a tough but fair", "You fill cells",
                              "You are a careful research assistant answering a question from numbered passages", "You help a researcher skim", "You check claims", "You synthesise a set", "You write flashcards", 'You are "Reviewer 2"')):
            return self.canned(system, body)
        last = body["messages"][-1]["content"]
        parts = [] if isinstance(last, str) else last
        text = last if isinstance(last, str) else " ".join(p.get("text", "") for p in parts if p.get("type") == "text")
        has_img = any(p.get("type") == "image_url" for p in parts)
        if body.get("response_format"):
            out = json.dumps({"questions": ["What is the sample?", "What is the main finding?", "What are the limits?", "What next?"]})
        elif has_img:
            out = "FIGURE: this box shows a table of numbers."
        elif system.startswith("You write the script of a short spoken briefing"):
            out = "Here is the paper in two minutes. The authors ask how open rules change who joins a platform.\n\nThey study one hundred and twenty firms. Open rules attract more entrants. The effect is strongest for small firms.\n\nBe careful: it is one platform only. What would happen elsewhere?"
        elif system.startswith("You write an OVERVIEW"):
            out = "**The book's argument.** FAKE BOOK OVERVIEW from %d summaries (location 5)." % text.count("FAKE CHAPTER SUMMARY")
        elif system.startswith("You summarise ONE chapter"):
            out = "**Argument.** FAKE CHAPTER SUMMARY of %s.\n\n**Key points.**\n- A point about rules (p. 5).\n- Another point (pp. 6-7).\n" % text.split("\n")[1][:40]
        elif system.startswith("You write the RELATED WORK"):
            out = ("## Disclosure and trust\nPlatform rules shape how participants behave [P1-2, P2-3]. {P3-4} show that transparency changes entry. Governance can also backfire when rules are opaque [P2-5].\n\n"
                   "## Methods and contexts\nMost of these studies rely on observational panels from a single platform [P1-4]. Another sentence follows here that carries no citation at all, so it must be flagged.\n\n"
                   "## Gap and contribution\nWhat remains open is whether hybrid designs help [P9-1].")
        elif "SLOW" in text:           # a long answer that arrives slowly (to test Cancel in the activity tray)
            out = "slow answer words " * 400
        elif "Question:" in text:
            out = "The platform has 120 firms (1) and strong effects (2). [model %s]\n\n## Evidence\n- 120 firms were sampled (1).\n- Effects are strong (2).\n\n## Not in the passages\nThe paper does not say how many firms are active." % body["model"]
        elif "Full text of the paper" in text:
            out = "# Summary\n\n## TL;DR\nFAKE SUMMARY written with model %s.\n\n## Method\nA fake method.\n\n## Key findings\n- A fake finding." % body["model"]
        else:
            out = "EXPLAINED: " + text[:60].replace("\n", " ")
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.send_header("Connection", "close"); self.end_headers()
        for i in range(0, len(out), 24):
            self.wfile.write(("data: " + json.dumps({"choices": [{"delta": {"content": out[i:i + 24]}}]}) + "\n\n").encode())
            if "SLOW" in text:
                self.wfile.flush(); time.sleep(0.15)
        self.wfile.write(("data: " + json.dumps({"choices": [], "usage": {"prompt_tokens": 500, "completion_tokens": 40}}) + "\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n"); self.close_connection = True


    CARD = {"kind": "empirical (quantitative)", "question": {"text": "How does governance shape complementor entry?", "cites": [1, 99]}, "topics": ["platform governance", "complementors"],
            "theories": [{"name": "Transaction cost theory", "role": "uses", "note": "", "cites": [1]}], "constructs": [{"name": "Transparency", "definition": "How visible the rules are", "cites": [2]}],
            "hypotheses": [{"label": "H1", "text": "Transparency raises entry", "verdict": "supported", "cites": [3]}],
            "method": {"design": "fixed effects panel", "data": "one platform", "sample": "120 firms", "analysis": "OLS", "cites": [2]},
            "findings": [{"text": "Entry rises 12 percent.", "cites": [3]}], "contribution": {"text": "A theory of transparency.", "cites": [1]}, "implications": [],
            "limitations": [{"text": "Results may not generalize beyond the software platform industry sample", "stated": True, "cites": [2]}],
            "future_research": [{"text": "Future work should study governance changes in other platform markets such as health care", "stated": True, "cites": [3]}]}
    IDEAS = {"ideas": [
        {"title": "Transparency under algorithmic governance", "research_question": "Does algorithmic rule enforcement change the entry effect of transparency?", "why_now": {"text": "The EU act forces disclosure.", "events": ["E1"]},
         "gap": {"text": "Not addressed in these papers: algorithmic enforcement.", "evidence": ["L1", "F1", "P1"]}, "lens": "Transaction cost theory", "method": {"approach": "Difference-in-differences", "identification": "Staggered adoption"},
         "data": "App store metadata", "contribution": {"kind": "theory", "text": "Extends governance theory."}, "journals": ["MIS Quarterly"], "risks": ["Data access"], "first_steps": ["Pull data"], "closest_papers": ["P1"], "speculative": ["Enforcement is algorithmic"]},
        {"title": "A bold guess", "research_question": "What if?", "why_now": {"text": "", "events": []}, "gap": {"text": "None.", "evidence": []}, "lens": "", "method": {"approach": "", "identification": ""}, "data": "",
         "contribution": {"kind": "empirical", "text": ""}, "journals": [], "risks": [], "first_steps": [], "closest_papers": [], "speculative": []}]}
    CRIT = {"critiques": [{"index": 1, "novelty": 4, "feasibility": 3, "significance": 4, "fit": 5, "verdict": "Promising.", "main_risk": "Data.", "search_query": "algorithmic governance"},
                          {"index": 2, "novelty": 2, "feasibility": 2, "significance": 2, "fit": 2, "verdict": "Thin.", "main_risk": "Everything.", "search_query": "guess"}]}

    def canned(self, system, body):
        urls = [{"url_citation": {"url": "https://news.example.com/eu-act", "title": "EU act"}}] if body.get("plugins") else None
        if system.startswith("You build a structured"):
            out = json.dumps(self.CARD)
        elif system.startswith("You find current events"):
            out = json.dumps({"events": [{"title": "EU platform act adopted", "date": "2026-03-04", "summary": "New duties for gatekeepers.", "why_it_matters": "Rules change.", "url": "https://news.example.com/eu-act"},
                                         {"title": "Big app store outage", "date": "2026-05", "summary": "A day offline.", "why_it_matters": "Dependence.", "url": "https://news.example.com/outage"}]})
        elif system.startswith("You are a tough"):
            out = json.dumps(self.CRIT)
        elif system.startswith("You help a researcher skim"):
            out = json.dumps({"items": [{"lens": "question", "passage": 1, "quote": "Platform effects are large [1] and persistent (Lee, 2019).", "rank": 1},
                                        {"lens": "sample", "passage": 1, "quote": "See Table 1 for the sample.", "rank": 1},
                                        {"lens": "finding", "passage": 2, "quote": "Figure 1 shows the platform ecosystem and its complementors in detail.", "rank": 3},
                                        {"lens": "limitation", "passage": 2, "quote": "This sentence is not in the paper at all.", "rank": 1}]})
        elif system.startswith("You fill cells"):
            ids = re.findall(r"- id (\S+) — ", body["messages"][-1]["content"])
            out = json.dumps({"cells": [{"column": i, "answer": "Tenure moderates the effect.", "cites": [1]} for i in ids]})
        elif system.startswith("You write flashcards"):
            out = json.dumps({"cards": [{"paper": "P1", "q": "What does transparency mean in this paper?", "a": "How visible the platform's rules are.", "ids": ["P1-2"]},
                                        {"paper": "P1", "q": "Which theory does the paper build on?", "a": "Transaction cost theory.", "ids": ["P1-2", "P1-3"]},
                                        {"paper": "P2", "q": "What is the sample of the study?", "a": "120 platform firms.", "ids": ["P2-4"]},
                                        {"paper": "P2", "q": "What is the SAMPLE of the study?", "a": "The same card again.", "ids": ["P2-4"]},
                                        {"paper": "P3", "q": "A card with no real note behind it?", "a": "It must be dropped.", "ids": ["P77-1"]}]})
        elif system.startswith('You are "Reviewer 2"'):
            out = json.dumps({"summary": "The text claims that open rules attract entrants; it is short on identification.", "recommendation": "major",
                              "major": [{"point": "The causal claim is not identified.", "why": "Rules and entry may both follow platform maturity.", "suggestion": "Use the policy change as a natural experiment."}, {"point": "The mechanism is asserted, not shown.", "why": "No mediator is measured.", "suggestion": "Add a test of the proposed channel."}],
                              "minor": [{"point": "Define complementor on first use.", "suggestion": "One sentence."}],
                              "unsupported": [{"quote": "Platforms that publish clear rules attract more complementors", "need": "A citation or your own estimate."}, {"quote": "This sentence is nowhere in the text at all", "need": "n/a"}],
                              "literature": [{"paper": "P1", "point": "P1 finds the opposite for small platforms.", "ids": ["P1-2"]}, {"paper": "P88", "point": "A paper that was never given.", "ids": ["P88-1"]}],
                              "questions": ["What is the unit of analysis?", "Is the effect larger for small firms?"]})
        elif system.startswith("You synthesise a set"):
            out = json.dumps({"overview": "These papers study platform governance and trust.", "overview_ids": ["P1-1", "P2-2", "P99-1"],
                              "themes": [{"name": "Transparency and entry", "summary": "Rules that are visible attract entrants.", "consensus": "agree", "tension": "",
                                          "positions": [{"paper": "P1", "claim": "Open rules raise entry", "stance": "supports", "ids": ["P1-2"]}, {"paper": "P2", "claim": "Entry rises with clarity", "stance": "supports", "ids": ["P2-3"]},
                                                        {"paper": "P3", "claim": "Nothing is behind this", "stance": "supports", "ids": ["P77-1"]}]},
                                         {"name": "Context matters", "summary": "Effects vary.", "consensus": "mixed", "tension": "The platforms differ.",
                                          "positions": [{"paper": "P1", "claim": "One platform only", "stance": "qualifies", "ids": ["P1-4"]}, {"paper": "P3", "claim": "Another platform", "stance": "opposes", "ids": ["P3-2"]}]},
                                         {"name": "Ghost theme", "summary": "No notes.", "consensus": "agree", "tension": "", "positions": [{"paper": "P1", "claim": "x", "stance": "supports", "ids": ["P50-1"]}]}],
                              "gaps": [{"gap": "Long-run effects are unknown.", "why": "Rules change slowly.", "kind": "time", "question": "What happens after five years?", "ids": ["P1-3"]},
                                       {"gap": "Nobody studies small platforms.", "why": "Scale may matter.", "kind": "sample", "question": "Do small platforms differ?", "ids": []}]})
        elif system.startswith("You check claims"):
            res = []
            for blk in re.split(r"### Claim ", body["messages"][-1]["content"])[1:]:
                n = int(re.match(r"\d+", blk).group(0))
                ids = re.findall(r"^(C\d+\.\d+) \((.*?), p\. \d+\)", blk, re.M)
                if "SUPPORTME" in blk and ids:
                    res.append({"n": n, "verdict": "supported", "reason": "The paper reports exactly this.", "evidence": [{"id": ids[0][0], "stance": "supports"}]})
                elif "CONTRADICTME" in blk and ids:
                    res.append({"n": n, "verdict": "contradicted", "reason": "The paper finds the opposite.", "evidence": [{"id": ids[0][0], "stance": "contradicts"}]})
                elif "WRONGPAPER" in blk and ids:
                    other = [i for i in ids if "Tian" not in i[1]] or ids
                    res.append({"n": n, "verdict": "supported", "reason": "Another paper says this.", "evidence": [{"id": other[0][0], "stance": "supports"}]})
                elif "NOEVIDENCE" in blk:
                    res.append({"n": n, "verdict": "supported", "reason": "Trust me.", "evidence": []})
                elif "BADID" in blk and ids:
                    res.append({"n": n, "verdict": "partly", "reason": "Only part of it.", "evidence": [{"id": "C99.9", "stance": "supports"}, {"id": ids[0][0], "stance": "supports"}]})
                elif ids:
                    res.append({"n": n, "verdict": "not_found", "reason": "No passage speaks to it.", "evidence": []})
            out = json.dumps({"claims": res})
        elif system.startswith("You are a careful research assistant answering a question from numbered passages"):
            out = "Across the papers, governance matters (1) and (2). [multi %s]\n\n## By paper\n- First paper: governance matters (1).\n- Second paper: it matters too (2).\n\n## Agreement and disagreement\nThey agree." % body["model"]
        elif system.startswith("You are a senior information-systems researcher turning"):
            out = "## Working title and abstract\n\nA developed outline of the idea.\n\n## Research questions\n\n- RQ1"
        else:
            out = json.dumps(self.IDEAS)
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.send_header("Connection", "close"); self.end_headers()
        for k, i in enumerate(range(0, len(out), 200)):
            delta = {"content": out[i:i + 200]}
            if k == 0 and urls:
                delta["annotations"] = urls
            self.wfile.write(("data: " + json.dumps({"choices": [{"delta": delta}]}) + "\n\n").encode())
        self.wfile.write(("data: " + json.dumps({"choices": [], "usage": {"prompt_tokens": 800, "completion_tokens": 300}}) + "\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n"); self.close_connection = True


def _work(n, doi="", title=None, year=2020, cited=10, refs=(), related=(), venue="Journal of Tests", authors=("Ann Lee",)):
    W = lambda k: "https://openalex.org/W%d" % k  # noqa: E731
    return {"id": W(n), "doi": ("https://doi.org/" + doi) if doi else None, "title": title or "Work %d" % n, "publication_year": year, "publication_date": "%d-03-01" % year, "cited_by_count": cited,
            "primary_location": {"source": {"display_name": venue}}, "authorships": [{"author": {"display_name": a}} for a in authors], "referenced_works": [W(r) for r in refs], "related_works": [W(r) for r in related]}


OA = {w["id"]: w for w in (
    _work(1, "10.1/p0", "Open Platforms and Complementors", refs=(10, 11), related=(30,)), _work(2, "10.1/p1", "Chatbots and Customer Trust", refs=(10,), related=(30, 31)),
    _work(3, "10.1/p2", "Citations and Links", refs=()), _work(10, "10.1234/abc", "The platform classic", cited=900, authors=("Zed Quinn", "Amy Roe")), _work(11, "10.1234/eleven", "Governance of platform ecosystems", cited=300),
    _work(30, "10.1234/thirty", "Platform governance and AI", cited=120, year=2025), _work(31, "10.1234/thirtyone", "Marketplace design", cited=20))}


AUTHOR_ASKED = []
AUTHOR_EXTRA = [0]          # works OpenAlex lists for Ann Lee beyond the 45 (a new work, for the weekly check of a followed author)


class FakeOA(BaseHTTPRequestHandler):
    """A tiny OpenAlex: /works?filter=doi:a|b  or  openalex:W1|W2 (the dashboard is pointed here with JW_OPENALEX_URL)."""
    def log_message(self, *a):
        pass

    def do_GET(self):
        import urllib.parse as up
        q = up.parse_qs(up.urlsplit(self.path).query)
        if self.path.startswith("/authors?") or (self.path.startswith("/works?") and q.get("filter", [""])[0].startswith("author.id:")):     # author search (authors.py)
            AUTHOR_ASKED.append(self.path)
            if "A5000000222" in self.path and "A5000000111" not in self.path and self.path.startswith("/works?"):      # this person's works: OpenAlex does not answer
                self.send_response(500); self.end_headers(); return
            if self.path.startswith("/authors?"):
                res = [{"id": "https://openalex.org/A5000000111", "display_name": "Ann Lee", "orcid": "https://orcid.org/0000-0002-1825-0097", "works_count": 45, "cited_by_count": 300,
                        "last_known_institutions": [{"display_name": "MIT"}], "topics": [{"display_name": "Platforms"}]},
                       {"id": "https://openalex.org/A5000000222", "display_name": "Ann Lee", "works_count": 2, "last_known_institutions": [{"display_name": "Elsewhere University"}], "topics": []}]
                body = {"results": res}
            else:
                body = {"meta": {"count": 45 + AUTHOR_EXTRA[0], "next_cursor": None}, "results": [
                    {"id": "https://openalex.org/W77%03d" % k, "doi": "https://doi.org/10.7777/au.%d" % k, "display_name": "Author paper %d" % k, "publication_date": "2024-01-01",
                     "publication_year": 2024, "type": "article", "primary_location": {"source": {"display_name": "Author Test Journal", "type": "journal"}},
                     "authorships": [{"author": {"display_name": "Ann Lee"}}]} for k in range(45 + AUTHOR_EXTRA[0])]}
            data = json.dumps(body).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
        kind, vals = q["filter"][0].split(":", 1); vals = [up.unquote(v) for v in vals.split("|")]
        hits = [w for w in OA.values() if ((w["doi"] or "").replace("https://doi.org/", "") in vals if kind == "doi" else w["id"].rsplit("/", 1)[-1] in vals)]
        if "referenced_works" not in q["select"][0]:
            hits = [{k: v for k, v in w.items() if k not in ("referenced_works", "related_works")} for w in hits]
        data = json.dumps({"results": hits}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)


def build(fake_url):
    import pymupdf
    shutil.copytree(os.path.join(PROJECT, "dashboard"), os.path.join(TMP, "dashboard"), ignore=shutil.ignore_patterns("__pycache__"))
    sk = os.path.join(TMP, ".agents", "skills", "ais-journal-watcher")
    shutil.copytree(os.path.join(SKILL, "scripts"), os.path.join(sk, "scripts"), ignore=shutil.ignore_patterns("__pycache__"))
    for f in ("journals.json", "interests.json", "library.json", "prompts.json"):
        shutil.copy(os.path.join(SKILL, "defaults", f), sk)
    fake = {"type": "openai_compat", "base_url": fake_url, "key_env": "FAKE_KEY", "models": ["m1", "m2"]}
    feat = lambda: {"provider": "fake", "model": "m1"}  # noqa: E731
    web = {**fake, "web_body": {"plugins": [{"id": "web"}]}}
    json.dump({"providers": {"fake": fake, "websearch": web, "vault": {**fake, "key_env": "VAULT_API_KEY"}, "openrouter": {**fake, "key_env": "REEF_API_KEY"}}, "features": {**{k: feat() for k in ("ask", "explain", "suggest", "summarize")}, "events": {"provider": "websearch", "model": "m1"}}, "embeddings": {"provider": "fake", "model": "emb"},
               "prices": {"m1": {"in": 1.0, "out": 2.0, "cache_write": 0, "cache_read": 0}, "m2": {"in": 1.0, "out": 2.0}}, "privacy": {"local_only": False}, "budget": {"monthly_usd": None}},
              open(os.path.join(sk, "ai.json"), "w"))
    os.makedirs(os.path.join(sk, "state", "runs"))
    rows = []
    d = os.path.join(TMP, "Papers", "2026-04", "MIS Quarterly"); os.makedirs(d, exist_ok=True)
    papers = [("Open Platforms and Complementors", "Jing Tian; Ling Xue", "Transparent governance rules attract complementors to open platforms. " * 40),
              ("Chatbots and Customer Trust", "Grace Zhang", "Customers distrust service chatbots when failures are opaque. " * 40)]
    for n, (title, au, body) in enumerate(papers):
        rel = "Papers/2026-04/MIS Quarterly/2026-04_MISQ_10.1-p%d_paper.pdf" % n
        doc = pymupdf.open()
        for _ in range(3):
            pg = doc.new_page(width=595, height=842); pg.insert_textbox(pymupdf.Rect(60, 60, 535, 780), body, fontsize=11)
        doc.save(os.path.join(TMP, rel)); doc.close()
        rows.append((title, au, "10.1/p%d" % n, rel))
    # a paper with citations, a link to its reference list, a table caption and a figure to box
    rel = "Papers/2026-04/MIS Quarterly/2026-04_MISQ_10.1-p2_paper.pdf"
    doc = pymupdf.open()
    p1 = doc.new_page(width=595, height=842)
    p1.insert_textbox(pymupdf.Rect(60, 60, 535, 130), "Platform effects are large [1] and persistent (Lee, 2019). See Table 1 for the sample.", fontsize=12)
    p1.draw_rect(pymupdf.Rect(100, 300, 400, 500), color=(0.1, 0.2, 0.6), fill=(0.8, 0.85, 0.95))
    p1.insert_textbox(pymupdf.Rect(60, 520, 535, 560), "Figure 1 shows the platform ecosystem and its complementors in detail.", fontsize=11)
    hit = p1.search_for("[1]")[0]
    p2 = doc.new_page(width=595, height=842)
    p2.insert_textbox(pymupdf.Rect(60, 60, 535, 90), "Table 1. Descriptive statistics of the sample of platform firms", fontsize=11)
    p2.insert_textbox(pymupdf.Rect(60, 600, 535, 640), "References", fontsize=13)
    p2.insert_textbox(pymupdf.Rect(60, 650, 535, 690), "[1] Smith, J. (2020). A study of platform things. Journal of Tests, 1(2), 3-4. https://doi.org/10.1234/abc", fontsize=10)
    p2.insert_textbox(pymupdf.Rect(60, 700, 535, 740), "[2] Lee, K. (2019). Another study of complementors. Journal of Tests, 5(1), 10-20.", fontsize=10)
    doc[0].insert_link({"kind": pymupdf.LINK_GOTO, "from": hit, "page": 1, "to": pymupdf.Point(60, 650)})
    doc.save(os.path.join(TMP, rel)); doc.close()
    rows.append(("Citations and Links", "Ana Costa", "10.1/p2", rel))
    with open(os.path.join(TMP, "Papers", "_index.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, ["date_checked", "journal", "abbr", "title", "authors", "doi", "url", "matched_topics", "score", "file", "status", "note"]); w.writeheader()
        for title, au, doi, rel in rows:
            w.writerow({"date_checked": "2026-10-01", "journal": "MIS Quarterly", "abbr": "MISQ", "title": title, "authors": au, "doi": doi, "url": "", "matched_topics": "Platform Economics",
                        "score": "9", "file": rel, "status": "ok", "note": ""})
    return rows


JS_DROP = """([sel, files, pre]) => { const dt = new DataTransfer();      // drag PDFs (base64) over an element; pre = stop before dropping and report the overlay text
  for (const [b64, name, type] of files) { const bin = atob(b64), u = new Uint8Array(bin.length); for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i); dt.items.add(new File([u], name, {type})); }
  const t = document.querySelector(sel), o = {dataTransfer: dt, bubbles: true, cancelable: true};
  t.dispatchEvent(new DragEvent("dragenter", o)); t.dispatchEvent(new DragEvent("dragover", o));
  if (pre) { const p = document.querySelector(".di-ov:not(.hide) .di-pill"); return p ? p.textContent : null; }
  t.dispatchEvent(new DragEvent("drop", o)); return null; }"""
JS_SPAN_CENTER ="""([n, sub]) => { const q = [...document.querySelectorAll(`.rd-band[data-n="${n}"] .textLayer span`)].find(s => s.textContent.includes(sub)); if (!q) return null;
  const i = q.textContent.indexOf(sub), t = q.firstChild, r = document.createRange(); r.setStart(t, i); r.setEnd(t, i + sub.length); const b = r.getBoundingClientRect();
  return [b.left + b.width / 2, b.top + b.height / 2]; }"""
# did the reader turn to page n? A jump puts the page's top about 70 px below the top of the pane. (The page-number box is no judge of that: it shows n for a few milliseconds
# and then the page whose foot is still in view above, see docs/HISTORY.md §2n.)
JS_RANGE = """([sel, v]) => { const r = document.querySelector(sel); r.value = v; r.dispatchEvent(new Event('input', {bubbles: true})); }"""
JS_PAGE_AT_TOP = r"""(n) => { const b = document.querySelectorAll('.rd-band')[n - 1], p = document.querySelector('#rd-pdf'); if (!b || !p) return false; const d = b.getBoundingClientRect().top - p.getBoundingClientRect().top; return d >= 0 && d < 150; }"""
JS_A11Y = r"""() => {
  const out = {noname: [], dupid: [], tiny: [], smalltext: []};
  const vis = e => { const r = e.getBoundingClientRect(), cs = getComputedStyle(e); return r.width > 0 && r.height > 0 && cs.visibility !== "hidden" && cs.display !== "none"; };
  const lab = e => { const ids = (e.getAttribute("aria-labelledby") || "").split(/\s+/).filter(Boolean).map(i => (document.getElementById(i) || {}).textContent || "").join(" ");
    return (e.getAttribute("aria-label") || ids || (e.labels && e.labels.length ? [...e.labels].map(l => l.textContent).join(" ") : "") || e.textContent || e.getAttribute("title") || e.getAttribute("placeholder") || e.getAttribute("alt") || "").replace(/\s+/g, " ").trim(); };
  document.querySelectorAll("button, a[href], [role=button], [role=tab], input:not([type=hidden]), select, textarea, summary").forEach(e => {
    if (!vis(e)) return;
    if (!lab(e) && !(e.querySelector("img[alt]:not([alt=''])"))) out.noname.push(e.tagName.toLowerCase() + (e.id ? "#" + e.id : "") + (e.className && typeof e.className === "string" ? "." + e.className.split(" ")[0] : "") + (e.dataset && Object.keys(e.dataset).length ? " [data-" + Object.keys(e.dataset)[0] + "]" : ""));
    const r = e.getBoundingClientRect(); if ((r.width < 20 || r.height < 20) && e.tagName !== "INPUT") out.tiny.push(e.tagName.toLowerCase() + (e.id ? "#" + e.id : "") + "." + (typeof e.className === "string" ? e.className.split(" ")[0] : "") + " " + Math.round(r.width) + "x" + Math.round(r.height));
  });
  const seen = new Set(); document.querySelectorAll("[id]").forEach(e => { if (seen.has(e.id)) out.dupid.push(e.id); seen.add(e.id); });
  return out;
}"""
JS_CONTRAST = r"""(theme) => {
  const parse = c => { const m = c.match(/rgba?\(([^)]+)\)/); if (!m) return null; const p = m[1].split(/[ ,\/]+/).filter(Boolean).map(Number); return {r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1}; };
  const lum = c => { const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }; return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b); };
  const over = (top, bot) => ({r: top.r * top.a + bot.r * (1 - top.a), g: top.g * top.a + bot.g * (1 - top.a), b: top.b * top.a + bot.b * (1 - top.a), a: 1});
  const bgOf = el => { const chain = []; for (let e = el; e; e = e.parentElement) { const c = parse(getComputedStyle(e).backgroundColor); if (c && c.a > 0) { chain.push(c); if (c.a >= 1) break; } } let base = {r: 255, g: 255, b: 255, a: 1}; if (!chain.length || chain[chain.length - 1].a < 1) { const b = parse(getComputedStyle(document.body).backgroundColor); if (b && b.a > 0) base = over(b, base); } return chain.reverse().reduce((acc, c) => over(c, acc), base); };
  const res = new Map(), vis = e => { const r = e.getBoundingClientRect(), cs = getComputedStyle(e); return r.width > 0 && r.height > 0 && cs.visibility !== "hidden" && cs.display !== "none" && cs.opacity !== "0"; };
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n; (n = w.nextNode());) {
    const t = n.textContent.trim(); if (t.length < 2) continue; const el = n.parentElement; if (!el || !vis(el) || el.closest("svg, script, style, canvas, .textLayer, .rd-band")) continue;
    const cs = getComputedStyle(el), fg = parse(cs.color); if (!fg) continue; const bg = bgOf(el), f = over(fg, bg), L1 = lum(f), L2 = lum(bg), ratio = (Math.max(L1, L2) + 0.05) / (Math.min(L1, L2) + 0.05);
    const size = parseFloat(cs.fontSize), bold = parseInt(cs.fontWeight) >= 700, large = size >= 24 || (size >= 18.66 && bold), need = large ? 3 : 4.5;
    if (ratio < need) { const k = (el.className && typeof el.className === "string" ? el.tagName.toLowerCase() + "." + el.className.split(" ")[0] : el.tagName.toLowerCase()) + " " + cs.color + " on " + `rgb(${Math.round(bg.r)},${Math.round(bg.g)},${Math.round(bg.b)})`; const o = res.get(k) || {n: 0, ratio: 99, sample: t.slice(0, 30)}; o.n++; o.ratio = Math.min(o.ratio, +ratio.toFixed(2)); res.set(k, o); }
  }
  return [...res.entries()].sort((a, b) => b[1].n - a[1].n).slice(0, 12).map(([k, o]) => k + " → " + o.ratio + " ×" + o.n + " “" + o.sample + "”");
}"""
JS_SELECT = """([n, sub]) => { const q = [...document.querySelectorAll(`.rd-band[data-n="${n}"] .textLayer span`)].find(s => s.textContent.includes(sub)); if (!q) return false;
  q.scrollIntoView({block: 'center'}); const r = document.createRange(); r.selectNodeContents(q); const s = getSelection(); s.removeAllRanges(); s.addRange(r);
  document.querySelector('#rd-pdf').dispatchEvent(new MouseEvent('mouseup', {bubbles: true})); return true; }"""


FAKE_BROWSER = r"""import os, paths
_cmd = paths.cmd
def fake_cmd(script, *a):
    if script == 'browser.py' and a[:1] == ('library-test',):      # "Test my access": answers with whatever the test put in the file
        return [sys.executable, '-c', "import json\nprint(json.dumps(json.load(open(FILE_))))\n".replace('FILE_', repr(os.environ.get('JW_FAKE_LIBTEST', '')))]
    if script == 'browser.py':       # the sign-in window: it opens, waits until the test lets it finish (a file appears), then says it worked
        src = "import os, time\nt = time.time()\nwhile time.time() - t < 60 and not os.path.exists(GATE):\n    time.sleep(0.1)\nprint('Signed in to Test ' + ARGS + '.')\n"
        return [sys.executable, '-c', src.replace('GATE', repr(os.environ.get('JW_FAKE_SIGNIN_GATE', ''))).replace('ARGS', repr(' '.join(a)))]
    return _cmd(script, *a)
paths.cmd = fake_cmd
"""


SHOTS = os.environ.get("JW_SHOTS")      # set to a folder to also save a few screenshots while the test runs


def shot(page, name):
    if SHOTS:
        os.makedirs(SHOTS, exist_ok=True)
        page.screenshot(path=os.path.join(SHOTS, name + ".png"))


def main():
    srv_ai = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv_ai.serve_forever, daemon=True).start()
    rows = build("http://127.0.0.1:%d/v1" % srv_ai.server_address[1])
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    srv_oa = ThreadingHTTPServer(("127.0.0.1", 0), FakeOA)
    threading.Thread(target=srv_oa.serve_forever, daemon=True).start()
    zipb = os.urandom(2500)
    feed = {"app": "Journal Watcher", "version": "9.9.9", "released": "2026-10-28", "notes": ["A welcome guide for first-time users", "Update notices in the top bar"], "url": "", "sha256": hashlib.sha256(zipb).hexdigest(), "size": len(zipb),
            "history": [{"version": "9.9.8", "released": "2026-10-27", "notes": ["Calmer dark theme"]}]}

    FEED_PATH = "/o/r/releases/latest/download/appcast.json"          # shaped like the address built into the app, so a 404 here reads as "nothing published yet"
    fmode = {"mode": "ok"}                                               # what the feed does next: "ok", an HTTP status, or "garbage" (a web page where the feed should be)

    class FakeFeed(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = zipb if self.path == "/j.zip" else None
            if self.path == FEED_PATH:
                if fmode["mode"] == "ok":
                    body = json.dumps(feed).encode()
                elif fmode["mode"] == "garbage":
                    body = b"<html><body>Sign in to the Wi-Fi first</body></html>"
                elif fmode["mode"] == "evil":                                # a status text with markup in it: the page must show it as text
                    self.send_response(403, "<svg onload=window.__pwned=1>"); self.end_headers(); return
                else:
                    self.send_response(int(fmode["mode"])); self.end_headers(); return
            if body is None:
                self.send_response(404); self.end_headers(); return
            self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    srv_up = ThreadingHTTPServer(("127.0.0.1", 0), FakeFeed)
    threading.Thread(target=srv_up.serve_forever, daemon=True).start()
    feed["url"] = "http://127.0.0.1:%d/j.zip" % srv_up.server_address[1]
    env = {**os.environ, "FAKE_KEY": "good", "JW_UPDATE_FEED": "http://127.0.0.1:%d%s" % (srv_up.server_address[1], FEED_PATH), "JW_VERSION": "1.0.0", "JW_NO_UPDATE_CHECK": "1", "JW_SUPPORT_DELAY": "1", "JW_FAKE_SIGNIN_GATE": os.path.join(TMP, "signin-gate"), "JW_FAKE_LIBTEST": os.path.join(TMP, "libtest.json"), "JW_UPDATE_DIR": os.path.join(TMP, "updates"), "JW_OFFLINE": "1", "JW_NO_KEYCHAIN": "1", "JW_LOCAL": os.path.join(TMP, "local-state"), "JW_RELATED_SIM": "-1", "JW_OPENALEX_URL": "http://127.0.0.1:%d/" % srv_oa.server_address[1]}
    marker = os.path.join(TMP, "download-called.txt")
    # the server runs as usual, except that "download" only records what it was asked to fetch (no browser, no network)
    head = ("import sys, runpy\nsys.path.insert(0, %r)\nimport pipeline as pl\n"
            "import os\n"
            "def fake(run, ids, emit, stop, checks=None, **k):\n    open(%r, 'w').write(','.join(ids))\n"
            "    if checks is not None and os.path.exists(%r):      # a site wants a human check: wait (up to 20 s) for 'Open the check', write down which site was asked for\n"
            "        emit('challenge', {'host': 'www.sciencedirect.com', 'state': 'pending', 'site': 'ScienceDirect', 'msg': 'ScienceDirect wants to check you are human. Press Open the check.'})\n"
            "        import queue\n"
            "        try:\n            h = checks.get(timeout=20)\n        except queue.Empty:\n            h = ''\n"
            "        open(%r, 'w').write(h)\n"
            "        emit('challenge', {'host': h, 'state': 'open', 'msg': 'A Chromium window is open on ' + h})\n"
            "        emit('challenge', {'host': h, 'state': 'passed', 'msg': h + ' check passed'})\n"
            "    for i in ids:\n        p = run.papers[i]\n"
            "        wrong = os.path.exists(%r)      # a second round: the publisher's route gave another work's PDF, which the app refused\n"
            "        p['dl_status'] = 'wrong_paper' if wrong else 'failed'\n"
            "        p['dl_note'] = ('wrong paper via browser: this PDF is titled \\u201cTwo-stage differences in differences\\u201d: its title is not on the first pages (0%% found)' if wrong else 'test: no real downloads')\n"
            "        p['manual_url'] = 'https://example.org/the-paper'\n"
            "        emit('download', {'id': i, 'dl_status': p['dl_status'], 'dl_note': p['dl_note'], 'manual_url': p['manual_url']})\n"
            "pl.download_and_summarize = fake\n" % (os.path.join(TMP, ".agents", "skills", "ais-journal-watcher", "scripts"), marker, marker + ".check", marker + ".checked", marker + ".wrong"))
    tail = "sys.argv = ['server.py', '--port', %r, '--no-open']\nrunpy.run_path(%r, run_name='__main__')\n" % (str(port), os.path.join(TMP, "dashboard", "server.py"))
    search_fakes = r"""
import zlib, fetch_new as fx
def fake_cr(issn, y0, y1, progress=None, doi_prefix=None, work_type=None):      # three articles per journal and year: two about platforms, one about something else
    k = zlib.crc32(str(issn).encode()) % 1000
    return [{'title': 'Platform governance study %d-%d' % (k, i), 'doi': '10.9999/t.%d.%d.%d' % (y0, k, i), 'pub_date': '%d-03-01' % y0, 'abstract': 'We study platform governance and complementors.', 'keywords': ['platform']} for i in range(2)] + \
           [{'title': 'Something else %d' % k, 'doi': '10.9999/o.%d.%d' % (y0, k), 'pub_date': '%d-04-01' % y0, 'abstract': 'Unrelated.', 'keywords': []}]
fx.crossref_journal_range = fake_cr
def fake_ssrn(query, y0, y1, limit=200, progress=None):                             # two SSRN papers for any words (and what was asked is written down)
    open(SSRNLOG, 'a').write('%s|%s|%s|%s\n' % (query, y0, y1, limit))
    return [{'title': 'Platform governance on SSRN %d' % i, 'doi': '10.2139/ssrn.99000%d%d' % (y0, i), 'pub_date': '%d-01' % y0, 'abstract': 'We study platform governance on SSRN.', 'keywords': None, 'authors': ['A. Author']} for i in range(2)]
fx.crossref_ssrn = fake_ssrn
def fake_screen(system, batch, usage, override=None):                                          # what the model is asked, one line per batch (the test counts them)
    open(SCREENLOG, 'a').write('%d\n' % len(batch))
    return {n: {'n': n, 'verdict': 'download' if 'Platform' in p['title'] else 'skip', 'relevance': 80, 'topics': [], 'reason': 'test', 'method': None} for n, p in enumerate(batch, 1)}
pl._screen_batch = fake_screen
import importer as imp
def fake_imp_search(title, rows=4):                                               # Crossref's answer to a title search (and what was asked): two records for the "Alpha" PDF, none for anything else
    open(IMPLOG, 'a').write(title + '\n')
    if 'delta' in title.lower():
        return [{'title': 'Delta study of marketplace reputation systems and seller behaviour', 'authors': ['Dee Quill', 'Ed Fenwick'], 'container': 'Marketplace Letters', 'issn': [], 'doi': '10.9999/delta.2019.3', 'pub_date': '2019-09-01', 'url': 'https://doi.org/10.9999/delta.2019.3'}]
    if 'alpha' not in title.lower():
        return []
    return [{'title': 'Alpha study of onboarding complementors in digital platform ecosystems and their governance', 'authors': ['Ada Alpha', 'Bo Beta'], 'container': 'Journal of Test Platforms', 'issn': [], 'doi': '10.9999/alpha.2021.1', 'pub_date': '2021-05-01', 'url': 'https://doi.org/10.9999/alpha.2021.1'},
            {'title': 'Beta ecosystems and the economics of fish', 'authors': ['C. Gamma'], 'container': 'Fish Quarterly', 'issn': [], 'doi': '10.9999/fish.2020.7', 'pub_date': '2020-01-01', 'url': 'https://doi.org/10.9999/fish.2020.7'}]
imp.crossref_search = fake_imp_search
import books as bks
def fake_ol(title='', authors=None, isbn='', rows=4):                             # Open Library: two editions of the test book (the second with another ISBN), nothing for anything else
    if isbn == '9780201835953' or 'platforms and their governance' in (title or '').lower():
        return [bks._record('Platforms and Their Governance', ['Ada Lovelace'], 'Open Library Press', '2019', '9780201835953', '', 312, 'openlibrary', 'https://openlibrary.org/works/OL1W'),
                bks._record('Platforms and Their Governance (revised edition)', ['Ada Lovelace'], 'Open Library Press', '2022', '9780306406157', '', 340, 'openlibrary', 'https://openlibrary.org/works/OL1W')]
    return []
bks.openlibrary_search = fake_ol
bks.crossref_books = lambda title, rows=3: []
fx.fetch_journal = lambda j: []                                                   # the weekly check reads no real feed (only the followed authors bring papers)
""".replace('SCREENLOG', repr(os.path.join(TMP, 'screened.log'))).replace('SSRNLOG', repr(os.path.join(TMP, 'ssrn.log'))).replace('IMPLOG', repr(os.path.join(TMP, 'imp.log')))
    runner = head + search_fakes + FAKE_BROWSER + tail
    srv = subprocess.Popen([sys.executable, "-c", runner], cwd=TMP, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = "http://127.0.0.1:%d" % port
    sidecar = lambda i, ext: os.path.join(TMP, rows[i][3][:-4] + ext)  # noqa: E731
    api = lambda path, body=None: json.load(urllib.request.urlopen(urllib.request.Request(base + path, json.dumps(body).encode() if body is not None else None, {"Content-Type": "application/json"})))  # noqa: E731
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(base + "/api/config", timeout=1); break
            except Exception:  # noqa: BLE001
                time.sleep(0.3)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            br = pw.chromium.launch(headless=True)
            ctx = br.new_context(viewport={"width": 1400, "height": 950}, permissions=["clipboard-read", "clipboard-write"])
            ctx.add_init_script("try { if (!sessionStorage.getItem('jw-wl-test')) localStorage.setItem('jw-welcome', 'done'); } catch (e) {}")      # the welcome guide has its own test below
            page = ctx.new_page()
            if os.environ.get("JW_TEST_THROTTLE"):          # JW_TEST_THROTTLE=4 runs the page four times slower: how a busy CI machine behaves
                ctx.new_cdp_session(page).send("Emulation.setCPUThrottlingRate", {"rate": float(os.environ["JW_TEST_THROTTLE"])})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e) + " @ " + str(getattr(e, "stack", ""))[:300]))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "Failed to load resource" not in m.text else None)
            dialogs = []
            answers = []        # canned answers for the next prompt() dialogs (otherwise the default value, or "fake/m2" for the compare picker)
            page.on("dialog", lambda d: (dialogs.append(d.message), d.accept(answers.pop(0) if answers else (d.default_value or "fake/m2"))))
            page.goto(base + "/"); page.wait_for_selector("#libitem")

            print("library")
            page.click("#libitem"); page.wait_for_selector("#lf-q")
            check("library home shows search, filters and journal tiles", page.locator("#libtiles .tile").count() == 1)
            page.wait_for_selector("#lib-upnext .unx", timeout=15000); n_unx = page.locator("#lib-upnext .unx").count()
            check("the Library has an Up next shelf: unread papers with why they are there", n_unx >= 1 and "up next" in page.inner_text("#lib-upnext").lower() and page.locator("#lib-upnext .unx-why").first.inner_text().strip() != "")
            first_unx = page.locator("#lib-upnext .unx >> nth=0").get_attribute("data-file")
            page.click("#lib-upnext .unx >> nth=0 >> [data-unx=later]"); page.wait_for_function("document.querySelector('#lib-upnext .unx') && /in your list/.test(document.querySelector('#lib-upnext .unx').innerText)")
            check("＋ To read keeps a paper in your list (it goes to the front of Up next)", api("/api/queue")["items"][0]["status"] == "toread")
            page.click("#lib-upnext .unx >> nth=0 >> [data-unx=skip]"); page.wait_for_function("%d > document.querySelectorAll('#lib-upnext .unx').length || document.querySelector('#lib-upnext .unx') === null || document.querySelector('#lib-upnext .unx').dataset.file !== %s" % (n_unx, json.dumps(first_unx)))
            check("Skip hides a paper from Up next for good and is remembered", first_unx not in [i["file"] for i in api("/api/queue?n=30")["items"]] and any(v.get("skip") for v in json.load(open(os.path.join(TMP, "Papers", "_status.json")))["papers"].values()))
            page.fill("#lf-q", "complementors"); page.wait_for_selector("#lib-results .librow")
            check("search finds a paper by its text", "Open Platforms" in page.inner_text("#lib-results"))
            page.fill("#lf-q", "transparent governance rules"); page.wait_for_timeout(450)      # the box's own refresh fires 220 ms after the last keystroke; pressing the button sooner makes the search run twice, with the results blinking out and in
            page.click("#lf-ft"); page.wait_for_selector(".ft-i", timeout=30000)
            try:      # the results can be drawn again a moment after they first appear (the list refreshes), so wait for all that the check below needs, at once
                page.wait_for_function("document.querySelectorAll('.ft-i').length >= 1 && /p\\. 1/.test((document.querySelector('.ft-h') || {}).innerText || '') && document.querySelectorAll('.ft-s mark').length >= 2 && ((document.querySelector('.ft-s') || {}).innerText || '').includes('Transparent governance rules attract')", timeout=10000)
            except Exception:  # noqa: BLE001
                pass
            check("In the PDFs finds the words inside the papers (the passage, its paper and page, the words marked)", page.locator(".ft-i").count() >= 1 and "p. 1" in page.inner_text(".ft-h") and page.locator(".ft-s mark").count() >= 2 and "Transparent governance rules attract" in page.inner_text(".ft-s"),
                  page.evaluate("[document.querySelectorAll('.ft-i').length, (document.querySelector('.ft-h') || {}).innerText, document.querySelectorAll('.ft-s mark').length, ((document.querySelector('.ft-s') || {}).innerText || '').slice(0, 60)]"))
            page.fill("#lf-q", '"no such phrase anywhere"'); page.click("#lf-ft"); page.wait_for_selector("text=Not found in the PDFs")
            page.fill("#lf-q", "transparent governance"); page.click("#lf-ft"); page.wait_for_selector(".ft-i")
            page.click(".ft-i >> nth=0"); page.wait_for_selector(".rd-band .textLayer span", timeout=30000)
            check("a passage opens its paper at that page", page.locator("#rd-num").count() == 1)
            page.click(".rd-bar [data-a='back']"); page.wait_for_selector("#lf-q"); page.fill("#lf-q", "complementors"); page.wait_for_selector("#lib-results .librow")
            page.wait_for_selector("[data-savesearch]")
            answers.append("Platform work"); page.click("[data-savesearch]"); page.wait_for_selector('[data-smart]')
            check("a search can be saved under a name and shows next to your folders", "Platform work" in page.inner_text("#lf-fstrip") and api("/api/smart")["searches"][0]["filters"] == {"q": "complementors"})
            page.fill("#lf-q", ""); page.wait_for_function("!document.querySelector('#lib-results .librow')")
            page.click('[data-smart]'); page.wait_for_selector("#lib-results .librow")
            check("clicking the saved search runs it again (text and filters come back)", page.input_value("#lf-q") == "complementors" and "Open Platforms" in page.inner_text("#lib-results"))
            page.on("dialog", lambda dd: None) if False else None
            page.click('[data-smartdel]'); page.wait_for_function("document.querySelectorAll('[data-smart]').length === 0")
            check("…and it can be forgotten (the papers are untouched)", api("/api/smart")["searches"] == [] and not os.path.exists(os.path.join(TMP, "Papers", "_smart.json")))
            page.fill("#lf-q", ""); page.wait_for_timeout(450)       # the box's own refresh fires 220 ms after the last keystroke and draws the rows again; if it comes after the ⋯ menu is opened it closes the menu (measured: the menu is gone 100-250 ms after opening), so let it come first
            page.click("#lf-list"); page.wait_for_selector("#lib-results .librow")
            row0 = page.locator("#lib-results .librow").first
            check("a paper row shows only ☆, Read and ⋯; the rest is in the menu", [b.get_attribute("data-star") is not None or b.get_attribute("data-read") is not None or b.get_attribute("data-more") is not None for b in row0.locator(".actions > .btn, .actions > .menuwrap > .btn").all()] == [True, True, True] and row0.locator(".rowmenu").is_hidden())
            row0.locator("[data-more]").click(); page.wait_for_selector(".rowmenu:not(.hide)")
            check("the ⋯ menu offers status, the PDF, folders and a takeaway", row0.locator(".rowmenu .stsel").count() == 1 and row0.locator('.rowmenu a[href*="/files/"]').count() == 1 and row0.locator(".rowmenu [data-fold]").count() == 1 and row0.locator(".rowmenu [data-take]").count() == 1)
            row0.locator(".rowmenu .stsel").select_option("toread"); page.wait_for_timeout(500)
            check("reading status is saved to Papers/_status.json", "toread" in open(os.path.join(TMP, "Papers", "_status.json")).read())
            check("…and shows on the row as a pill", page.locator("#lib-results .librow").first.locator(".tag.stt").inner_text().strip() == "To read")
            page.locator("#lib-results .librow").first.locator("[data-more]").click(); page.locator("#lib-results .librow").first.locator("[data-fold]").click(); page.wait_for_selector(".fpop")
            check("Folders… opens the folder picker next to the ⋯ button", page.locator(".fpop").count() == 1 and page.locator(".rowmenu:not(.hide)").count() == 0)
            page.keyboard.press("Escape"); page.mouse.click(5, 300); page.wait_for_timeout(200)
            page.click("#lf-filters"); page.wait_for_selector("#lf-pop:not(.hide)"); page.select_option("#lf-status", "toread"); page.wait_for_selector("#lf-chips .chip"); page.wait_for_function("document.querySelectorAll('#lib-results .librow').length === 1")
            check("Filters ▾ holds status, folder, journal, year, tag and notes; an active filter shows as a chip and on the button", "Status: To read" in page.inner_text("#lf-chips") and page.inner_text("#lf-nf").strip() == "1" and page.locator("#lib-results .librow").count() == 1, "%r | %r | %d" % (page.inner_text("#lf-chips"), page.inner_text("#lf-nf"), page.locator("#lib-results .librow").count()))
            page.click('#lf-chips [data-unf="status"]'); page.wait_for_timeout(400)
            check("clicking the chip removes the filter", page.locator("#lf-chips .chip").count() == 0 and page.locator("#lib-results .librow").count() == 3)
            page.click("#lf-filters"); page.wait_for_selector("#lf-pop:not(.hide)"); page.keyboard.press("Escape"); page.wait_for_timeout(150)
            check("Escape closes the Filters popover", page.locator("#lf-pop.hide").count() == 1)
            check("folders: create via API, library rows carry membership",
                  api("/api/folders", {"action": "create", "name": "Gov", "files": [rows[0][3]]})["folders"][0]["count"] == 1 and any(r["folders"] == ["gov"] for r in api("/api/library")))
            before = sorted(f for f in os.listdir(os.path.dirname(sidecar(0, ".x"))) if not f.endswith(".json"))

            print("reader: highlights, kinds, undo")
            page.goto(base + "/"); page.click("#libitem"); page.wait_for_selector("#lf-q"); page.click("#lf-list"); page.wait_for_selector("#lib-results .librow")
            page.locator('#lib-results .librow:has-text("Open Platforms") [data-read]').click()
            page.wait_for_selector(".rd-band .textLayer span", timeout=30000)
            check("the paper opens in the reader with a text layer", page.locator(".rd-band").count() == 3)
            print("reader: the page follows the notes pane")
            bw = lambda: page.locator('.rd-band[data-n="1"]').bounding_box()["width"]
            fits = lambda: (lambda b, p: b["x"] >= p["x"] - 1 and b["x"] + b["width"] <= p["x"] + p["width"] + 1)(page.locator('.rd-band[data-n="1"]').bounding_box(), page.locator("#rd-pdf").bounding_box())
            w0 = bw(); page.wait_for_timeout(300)
            check("the page opens fitted to the width left by the notes pane, nothing cut off", fits() and "on" in (page.get_attribute("#rd-fit", "class") or ""), "band %.0f" % w0)
            page.click('#reader [data-a="side"]'); page.wait_for_timeout(800); w_hidden = bw()
            check("hiding the notes pane makes the fitted page wider", w_hidden > w0 + 150 and fits(), "%.0f -> %.0f" % (w0, w_hidden))
            page.click('#reader [data-a="side"]'); page.wait_for_timeout(800)
            check("showing the notes pane shrinks it back", abs(bw() - w0) < 4 and fits(), "%.0f vs %.0f" % (bw(), w0))
            sp = page.locator("#rd-split").bounding_box(); sx, sy = sp["x"] + sp["width"] / 2, sp["y"] + 200
            page.mouse.move(sx, sy); page.mouse.down(); page.mouse.move(sx - 220, sy, steps=10); page.wait_for_timeout(500)
            mid = bw(); page.mouse.up(); page.wait_for_timeout(600)
            check("dragging the divider to widen the notes narrows the page while you drag and after", mid < w0 - 100 and bw() < w0 - 100 and fits(), "%.0f / %.0f / %.0f" % (w0, mid, bw()))
            sp = page.locator("#rd-split").bounding_box(); sx = sp["x"] + sp["width"] / 2
            page.mouse.move(sx, sy); page.mouse.down(); page.mouse.move(sx + 220, sy, steps=10); page.mouse.up(); page.wait_for_timeout(800)
            check("dragging it back restores the page", abs(bw() - w0) < 8 and fits(), "%.0f vs %.0f" % (bw(), w0))
            page.click('#reader [data-a="zin"]'); page.wait_for_timeout(500); wz = bw()
            page.click('#reader [data-a="side"]'); page.wait_for_timeout(800)
            check("a manual zoom is left alone when the notes pane changes", abs(bw() - wz) < 2 and "on" not in (page.get_attribute("#rd-fit", "class") or ""), "%.0f vs %.0f" % (bw(), wz))
            page.click('#reader [data-a="fit"]'); page.wait_for_timeout(500)
            check("Fit goes back to following the pane", bw() > w0 + 150 and fits() and "on" in (page.get_attribute("#rd-fit", "class") or ""))
            page.click('#reader [data-a="fitpage"]'); page.wait_for_timeout(500)
            pb, pp = page.locator('.rd-band[data-n="1"]').bounding_box(), page.locator("#rd-pdf").bounding_box()
            check("Page fits the whole page on screen", pb["height"] <= pp["height"] and "on" in (page.get_attribute("#rd-fitpage", "class") or ""), "%.0f in %.0f" % (pb["height"], pp["height"]))
            page.click('#reader [data-a="side"]'); page.wait_for_timeout(800); page.click('#reader [data-a="fit"]'); page.wait_for_timeout(400)
            check("the reader keeps working after all that (still 3 pages)", page.locator(".rd-band").count() == 3 and abs(bw() - w0) < 12, "%.0f vs %.0f" % (bw(), w0))   # (the divider drag back is only accurate to a few pixels)
            print("reader: a jump shows the right page number, and zooming does not move the text")
            scroll_y = lambda: page.evaluate("document.querySelector('#rd-pdf').scrollTop")
            page.fill("#rd-num", "3"); page.press("#rd-num", "Enter"); seen_ = []
            for _ in range(8):
                page.wait_for_timeout(120); seen_.append(page.input_value("#rd-num"))
            check("typing a page number leaves the box on that page (it used to fall back to the page above a moment later) and the page is at the top", set(seen_) == {"3"} and page.evaluate(JS_PAGE_AT_TOP, 3), str(seen_))
            page.evaluate("document.querySelector('#rd-pdf').scrollTop = 700"); page.wait_for_timeout(500)
            y0, n0 = scroll_y(), page.input_value("#rd-num")
            for _ in range(3):
                page.click('#reader [data-a="zin"]'); page.wait_for_timeout(500); page.click('#reader [data-a="zout"]'); page.wait_for_timeout(500)
            check("zooming in and out again brings the text back to the same place (it used to creep up by about a hundred pixels each time)", abs(scroll_y() - y0) <= 3 and page.input_value("#rd-num") == n0, "%.0f -> %.0f, page %s -> %s" % (y0, scroll_y(), n0, page.input_value("#rd-num")))
            def box_is(n):
                try:
                    page.wait_for_function("(n) => document.querySelector('#rd-num').value === n", arg=n, timeout=4000)
                except Exception:  # noqa: BLE001
                    pass
                return page.input_value("#rd-num")
            page.fill("#rd-num", "2"); page.press("#rd-num", "Enter"); got_ = [box_is("2")]
            page.wait_for_timeout(500); page.click('#reader [data-a="next"]'); got_.append(box_is("3"))
            page.wait_for_timeout(500); page.click('#reader [data-a="prev"]'); got_.append(box_is("2"))
            check("after typing a page, Next goes to the following page and Previous back again (it used to stay on the same page once, because the box was a page behind)", got_ == ["2", "3", "2"], str(got_))
            page.evaluate(JS_SELECT, [1, "Transparent governance"]); page.wait_for_selector("#rd-sel:not(.hide)"); page.click('#rd-sel [data-c="green"]')
            for _ in range(50):
                if os.path.exists(sidecar(0, ".notes.json")):
                    break
                page.wait_for_timeout(200)
            check("a highlight writes <paper>.notes.json next to the PDF", os.path.exists(sidecar(0, ".notes.json")) and len(json.load(open(sidecar(0, ".notes.json")))["highlights"]) == 1)
            check("the other papers got no sidecar (reading writes nothing)", not os.path.exists(sidecar(1, ".notes.json")) and not os.path.exists(sidecar(2, ".notes.json")))
            page.evaluate(JS_SELECT, [2, "Transparent governance"]); page.wait_for_selector("#rd-sel:not(.hide)")
            page.click('#rd-sel [data-mode="underline"]'); page.click('#rd-sel [data-c="blue"]'); page.wait_for_timeout(300)
            page.evaluate(JS_SELECT, [3, "Transparent governance"]); page.wait_for_selector("#rd-sel:not(.hide)")
            page.click('#rd-sel [data-mode="strike"]'); page.click('#rd-sel [data-c="red"]'); page.wait_for_timeout(300)
            check("underline and strikethrough annotations are drawn", page.locator(".rd-ov .hl.underline").count() >= 1 and page.locator(".rd-ov .hl.strike").count() >= 1)
            page.click("#rd-t-note"); page.locator('.rd-band[data-n="1"]').scroll_into_view_if_needed(); page.wait_for_timeout(300); box = page.locator('.rd-band[data-n="1"]').bounding_box(); page.mouse.click(box["x"] + box["width"] * 0.8, box["y"] + box["height"] * 0.6)
            page.wait_for_selector("#rd-pop:not(.hide)"); page.fill("#rd-pc", "check this later"); page.wait_for_timeout(900)
            check("a sticky note can be dropped on the page", page.locator(".rd-ov .hl.note").count() == 1 and "sticky note" in page.inner_text("#rd-pop .pkind").lower(),
                  "%d notes, pop says %r" % (page.locator(".rd-ov .hl.note").count(), page.inner_text("#rd-pop .pkind")))
            n_before = page.locator(".rd-ov .hl").count()
            page.keyboard.press("Escape"); page.mouse.click(box["x"] + 5, box["y"] + 5)
            for _ in range(3):          # typing the note's text is one undo step, adding the note another
                if page.locator(".rd-ov .hl").count() < n_before:
                    break
                page.keyboard.press("Control+z"); page.wait_for_timeout(300)
            check("undo removes the last annotation", page.locator(".rd-ov .hl").count() < n_before)
            for _ in range(3):
                if page.locator(".rd-ov .hl").count() == n_before:
                    break
                page.keyboard.press("Control+Shift+z"); page.wait_for_timeout(300)
            check("redo brings it back", page.locator(".rd-ov .hl").count() == n_before)
            page.click("#rd-undo"); page.wait_for_timeout(200); page.click("#rd-redo"); page.wait_for_timeout(200)

            print("reader: a collapsed view keeps your place when the reading line is in a 'lines hidden' bar or below every strip")
            JS_PANE_H = "document.querySelector('#rd-pdf').getBoundingClientRect().height"
            JS_FLOW = """() => { const p = document.querySelector('#rd-pdf'); return {h: p.getBoundingClientRect().height, all: p.scrollHeight}; }"""
            JS_GAP_TO_LINE = """() => { const p = document.querySelector('#rd-pdf'), pr = p.getBoundingClientRect();       // the middle of the first "lines hidden" bar that has a strip after it goes on the reading line (70 px under the top of the pane)
              const g = [...document.querySelectorAll('#rd-flow .rd-gap')].find(g => g.nextElementSibling && g.nextElementSibling.classList.contains('rd-band')); if (!g) return false;
              const r = g.getBoundingClientRect(); p.scrollTop += r.top + r.height / 2 - (pr.top + 70); return true; }"""
            JS_STRIP_AFTER_LINE = """() => { const p = document.querySelector('#rd-pdf'), line = p.getBoundingClientRect().top + 70;
              const b = [...document.querySelectorAll('#rd-flow .rd-band')].find(b => b.getBoundingClientRect().top > line - 1); return b ? {k: b.dataset.n + '@' + (+b.dataset.y0).toFixed(3), off: Math.round(b.getBoundingClientRect().top - line)} : null; }"""
            JS_LAST_STRIP = """() => { const p = document.querySelector('#rd-pdf'), pr = p.getBoundingClientRect(), bs = [...document.querySelectorAll('#rd-flow .rd-band')], b = bs[bs.length - 1];
              const r = b.getBoundingClientRect(); return {n: b.dataset.n, endsAboveLine: r.bottom <= pr.top + 70, shown: r.bottom > pr.top && r.top < pr.bottom}; }"""
            JS_PAGE_SHOWN = """(n) => { const p = document.querySelector('#rd-pdf').getBoundingClientRect(), r = document.querySelector(`.rd-band[data-n="${n}"]`).getBoundingClientRect(); return r.bottom > p.top && r.top < p.bottom; }"""
            page.fill("#rd-num", "2"); page.press("#rd-num", "Enter"); page.wait_for_timeout(400)
            page.evaluate(JS_RANGE, ["#rd-ctx", 0]); page.wait_for_timeout(500)         # (the slider re-collapses after a pause: let that pass before collapsing)
            page.click('#reader [data-a="collapse"]'); page.wait_for_selector("#rd-collapse.on"); page.wait_for_timeout(900)
            strips_, bars_ = page.locator("#rd-flow .rd-band").count(), page.locator("#rd-flow .rd-gap").count()
            check("collapsed to the highlights with no context, the paper is a few short strips with 'lines hidden' bars between them", strips_ >= 3 and bars_ >= 2, "%d strips, %d bars" % (strips_, bars_))
            vh_ = 950 - (page.evaluate(JS_PANE_H) - 200); page.set_viewport_size({"width": 1400, "height": int(vh_)}); page.wait_for_timeout(600)      # a short window, so that the strips need scrolling
            for k_ in range(6):
                g_ = page.evaluate(JS_FLOW)
                if k_ >= 3 and g_["all"] >= g_["h"] + 150:
                    break
                page.click('#reader [data-a="zin"]'); page.wait_for_timeout(450)
            page.evaluate(JS_GAP_TO_LINE); page.wait_for_timeout(400)
            b0_ = page.evaluate(JS_STRIP_AFTER_LINE)
            page.click('#reader [data-a="zout"]'); page.wait_for_timeout(600); b1_ = page.evaluate(JS_STRIP_AFTER_LINE)
            page.click('#reader [data-a="zin"]'); page.wait_for_timeout(600); b2_ = page.evaluate(JS_STRIP_AFTER_LINE)
            check("zooming a collapsed view with the reading line in a 'lines hidden' bar leaves the strip below it where it was (it used to slide that strip up to the line)",
                  bool(b0_ and b1_ and b2_) and 5 < b0_["off"] < 80 and b0_["k"] == b1_["k"] == b2_["k"] and abs(b1_["off"] - b0_["off"]) <= 3 and abs(b2_["off"] - b0_["off"]) <= 3, str([b0_, b1_, b2_]))
            page.set_viewport_size({"width": 1400, "height": int(vh_ - (page.evaluate(JS_PANE_H) - 130))}); page.wait_for_timeout(600)
            page.evaluate("document.querySelector('#rd-pdf').scrollTop = 99999"); page.wait_for_timeout(500)
            last_ = page.evaluate(JS_LAST_STRIP)
            check("with the reading line below every strip (a very short window), the page number is the last strip's page, not page 1", last_["endsAboveLine"] and last_["shown"] and page.input_value("#rd-num") == last_["n"], "%s, box %s" % (last_, page.input_value("#rd-num")))
            page.click('#reader [data-a="collapse"]'); page.wait_for_selector("#rd-collapse:not(.on)"); page.wait_for_timeout(700)
            check("expanding from there stays at that passage (it used to jump to page 1)", page.input_value("#rd-num") == last_["n"] and page.evaluate(JS_PAGE_SHOWN, last_["n"]), "box %s, wanted %s" % (page.input_value("#rd-num"), last_["n"]))
            page.set_viewport_size({"width": 1400, "height": 950}); page.wait_for_timeout(500)
            page.evaluate(JS_RANGE, ["#rd-ctx", 2]); page.click('#reader [data-a="fit"]'); page.wait_for_timeout(600)

            print("reader: links, citations, area, explain")
            page.click(".rd-bar [data-a='back']"); page.wait_for_selector("#lf-q")
            page.locator('.librow:has-text("Citations and Links") [data-read]').click(); page.wait_for_selector(".rd-band .textLayer span", timeout=30000)
            page.wait_for_function("document.querySelectorAll('.rd-band[data-n=\"2\"] .textLayer span').length > 0", timeout=30000)
            xy = page.evaluate(JS_SPAN_CENTER, [1, "[1]"])
            page.mouse.move(xy[0] - 30, xy[1] + 80); page.mouse.move(xy[0], xy[1]); page.wait_for_selector(".rd-hov:not(.hide)", timeout=8000)
            try:
                page.wait_for_function("document.querySelector('.rd-hov').innerText.includes('platform things')", timeout=8000)
            except Exception:
                print("   hover card says:", page.inner_text(".rd-hov")[:200], "| refs:", json.dumps(api("/api/doc/refs?file=" + urllib.request.quote(rows[2][3]))["refs"])[:400])
                raise
            shot(page, "1-citation-card")
            check("hovering [1] shows the matching reference with its DOI", "Smith" in page.inner_text(".rd-hov") and page.locator(".rd-hov a[href*='10.1234/abc']").count() == 1)
            page.wait_for_selector(".rd-hov .hv-cx", timeout=8000)
            check("the reference card also shows where this paper cites it (the sentence and the page)", "cited in this paper" in page.inner_text(".rd-hov").lower() and "Platform effects are large" in page.inner_text(".rd-hov .hv-cx") and "p. 1" in page.inner_text(".rd-hov .hv-cx"))
            page.click('.rd-hov [data-h="use"]'); page.wait_for_function("document.querySelector('#hv-ans') && document.querySelector('#hv-ans').textContent.includes('EXPLAINED')", timeout=15000)
            reqc = [b for p_, b in SEEN if b.get("messages") and "SENTENCES THAT CITE IT" in str(b["messages"][-1].get("content", ""))][-1]
            check("✨ How is it used? sends the reference and those sentences, and shows a short answer", "Smith, J." in reqc["messages"][-1]["content"] and "Platform effects are large" in reqc["messages"][-1]["content"])
            page.mouse.click(xy[0], xy[1]); page.wait_for_selector(".rd-backpill:not(.hide)", timeout=8000); page.wait_for_timeout(400)
            check("clicking it jumps to the reference list and offers a way back", "Back" in page.inner_text(".rd-backpill") and int(page.input_value("#rd-num")) >= 1)
            page.click(".rd-backpill"); page.wait_for_timeout(500)
            check("the back button returns to page 1", page.input_value("#rd-num") == "1")
            xy = page.evaluate(JS_SPAN_CENTER, [1, "Lee, 2019"]); page.mouse.move(xy[0] - 30, xy[1] + 90); page.mouse.move(xy[0], xy[1])
            page.wait_for_function("document.querySelector('.rd-hov') && !document.querySelector('.rd-hov').classList.contains('hide') && document.querySelector('.rd-hov').innerText.includes('Another study')", timeout=8000)
            check("an author–year citation (Lee, 2019) resolves too", True)
            xy = page.evaluate(JS_SPAN_CENTER, [1, "Table 1"]); page.mouse.move(xy[0] - 30, xy[1] + 90); page.mouse.move(xy[0], xy[1])
            page.wait_for_selector('.rd-hov [data-h="explain"]', timeout=8000)
            page.click('.rd-hov [data-h="explain"]'); page.wait_for_selector(".rd-exp:not(.hide) .ex-act:not(.hide)", timeout=15000)
            req = [b for p_, b in SEEN if b.get("messages") and isinstance(b["messages"][-1].get("content"), list) and any(x.get("type") == "image_url" for x in b["messages"][-1]["content"])][-1]
            sent = " ".join(x.get("text", "") for x in req["messages"][-1]["content"] if x.get("type") == "text")
            check("hovering 'Table 1' offers ✨ Explain it: the caption's page goes to the AI as a picture, with the caption, and the answer opens in the explain card",
                  "Table 1" in sent and "Descriptive statistics" in sent and "page 2" in sent and "FIGURE" in page.inner_text(".rd-exp .ex-body") and "Table 1" in page.inner_text(".rd-exp .ex-quote"), sent[:300])
            page.click('.rd-exp [data-x="close"]'); page.mouse.move(5, 5)

            page.click("#rd-t-area"); b1 = page.locator('.rd-band[data-n="1"]').bounding_box()
            page.mouse.move(b1["x"] + b1["width"] * 0.15, b1["y"] + b1["height"] * 0.30); page.mouse.down(); page.mouse.move(b1["x"] + b1["width"] * 0.7, b1["y"] + b1["height"] * 0.58, steps=6); page.mouse.up()
            page.wait_for_selector(".rd-areamenu:not(.hide)"); shot(page, "2-area-menu"); page.click('.rd-areamenu [data-m="notes"]'); page.wait_for_selector(".nc-card.image canvas", timeout=15000)
            page.wait_for_function("document.querySelector('.nc-card.image canvas').width > 50", timeout=10000)
            for _ in range(50):
                if os.path.exists(sidecar(2, ".notes.json")):
                    break
                page.wait_for_timeout(200)
            cards = json.load(open(sidecar(2, ".notes.json")))["cards"]
            check("boxing a figure puts a picture card in the notes (page + box saved, no image data)", any(c["type"] == "image" and c["src"]["rect"] and "data:" not in json.dumps(c) for c in cards))

            page.evaluate(JS_SELECT, [1, "Platform effects are large"]); page.wait_for_selector("#rd-sel:not(.hide)")
            page.click('#rd-sel [data-s="explain"]'); page.wait_for_selector(".rd-expmenu:not(.hide)")
            check("the ✨ menu lists your prompts", page.locator(".rd-expmenu [data-id='plain']").count() == 1 and page.locator(".rd-expmenu [data-id='__custom']").count() == 1)
            n_req = len([x for x in SEEN if "chat" in x[0]]); shot(page, "3-explain-menu")
            page.click(".rd-expmenu [data-id='plain']"); page.wait_for_selector(".ex-act:not(.hide)", timeout=15000); shot(page, "4-explain-answer")
            check("explain streams an answer built from your prompt", "EXPLAINED" in page.inner_text(".ex-body") and len([x for x in SEEN if "chat" in x[0]]) == n_req + 1)
            page.click('.rd-exp [data-x="close"]')
            page.evaluate(JS_SELECT, [1, "Platform effects are large"]); page.wait_for_selector("#rd-sel:not(.hide)")
            page.click('#rd-sel [data-s="explain"]'); page.click(".rd-expmenu [data-id='plain']"); page.wait_for_selector(".ex-act:not(.hide)", timeout=15000)
            check("asking the same thing again is answered from the cache (no new request)", len([x for x in SEEN if "chat" in x[0]]) == n_req + 1 and "cached" in page.inner_text(".ex-meta"))
            page.click('.rd-exp [data-x="save"]'); page.wait_for_selector(".nc-card.ai", timeout=5000)
            page.click(".rd-exp [data-x='close']") if page.locator(".rd-exp:not(.hide)").count() else None

            print("two papers side by side")
            page.evaluate("() => { window.__opened = []; window.open = (...a) => { window.__opened.push(a); return {}; }; }")
            page.click('#reader [data-a="twin"]'); page.wait_for_selector(".twindlg .tw-r")
            check("⧉ Beside lists the library (this paper marked) and can be searched", page.locator(".tw-r").count() >= 3 and page.locator(".tw-r.cur").count() == 1)
            page.fill("#tw-q", "chatbots"); page.wait_for_function("document.querySelectorAll('.tw-r').length === 1")
            page.click(".tw-r"); opened = page.evaluate("window.__opened")
            check("choosing a paper opens a second window on the right half of the screen, addressed to that paper", len(opened) == 1 and "#read=" in opened[0][0] and opened[0][1] == "jw-beside" and "popup=yes" in opened[0][2] and "left=" in opened[0][2], str(opened))
            two = page.context.new_page(); two.goto(base + "/" + opened[0][0].lstrip("/")); two.wait_for_selector("#reader:not(.hide) .rd-band .textLayer span", timeout=30000)
            check("…and that window shows the paper in its own reader", "Chatbots" in two.inner_text("#rd-title")); two.close()

            print("the text view")
            page.click('#reader [data-a="reflow"]'); page.wait_for_selector(".rf-p")
            check("¶ Text shows the paper as flowing text with page markers, in reading order, and leaves the reference list out", page.locator(".rf-p").count() >= 3 and "Platform effects are large" in page.inner_text(".rf-doc") and page.locator(".rf-pg").count() >= 2 and page.locator(".rf-p.ref").count() == 0 and not page.locator("#rd-pdf").is_visible())
            fs0 = page.evaluate("parseFloat(getComputedStyle(document.querySelector('.rf-doc')).fontSize)"); page.click('[data-rf="bigger"]')
            check("the text can be made larger (and the choice is remembered)", page.evaluate("parseFloat(getComputedStyle(document.querySelector('.rf-doc')).fontSize)") > fs0)
            page.click('[data-rf="smaller"]')
            page.check("#rf-refs") if page.locator("#rf-refs").count() else None
            check("references can be shown when you want them", page.locator("#rf-refs").count() == 0 or page.locator(".rf-p.ref").count() >= 1)
            page.locator('.rf-p[data-page="2"] .rf-n').first.click(); page.wait_for_function("document.querySelector('#rd-pdf') && !document.querySelector('#rd-pdf').classList.contains('hide')")
            try:
                page.wait_for_function(JS_PAGE_AT_TOP, arg=2, timeout=8000)
            except Exception:  # noqa: BLE001
                pass
            check("clicking a page number leaves the text view and opens that page of the PDF", page.evaluate(JS_PAGE_AT_TOP, 2) and page.locator("#rd-pdf").is_visible() and not page.locator(".rd-reflow:not(.hide)").count())

            print("reader: the notes canvas: Markdown notes, zoom, arrows, frames, outline")
            page.click('.rd-tabs button[data-tab="notes"]'); page.wait_for_selector(".nc-card")
            n_cards = page.locator(".nc-card").count()
            page.click('[data-k="add"]'); page.wait_for_selector(".nc-card.note textarea.nc-main:not(.hide)")
            page.fill(".nc-card.note >> nth=0 >> textarea.nc-main", "**Big idea**\n\n- first point\n- second point"); page.click("#nc-pn"); page.wait_for_selector(".nc-card.note >> nth=0 >> .nc-md strong")
            check("a note is shown as formatted Markdown once you leave it", "Big idea" in page.inner_text(".nc-card.note >> nth=0 >> .nc-md") and page.locator(".nc-card.note >> nth=0 >> .nc-md li").count() == 2 and not page.locator(".nc-card.note >> nth=0 >> textarea.nc-main").is_visible())
            page.click(".nc-card.note >> nth=0 >> .nc-md"); page.wait_for_selector(".nc-card.note >> nth=0 >> textarea.nc-main:not(.hide)")
            check("…and a click brings the text back for editing", "**Big idea**" in page.input_value(".nc-card.note >> nth=0 >> textarea.nc-main"))
            page.click("#nc-pn"); page.click('[data-k="add"]'); page.wait_for_selector(".nc-card.note >> nth=1 >> textarea.nc-main:not(.hide)")
            page.fill(".nc-card.note >> nth=1 >> textarea.nc-main", "Second note, to be linked"); page.click("#nc-pn"); page.wait_for_timeout(600)
            check("there are now two more cards", page.locator(".nc-card").count() == n_cards + 2)
            x0 = page.evaluate("document.querySelector('.nc-card.note:nth-of-type(n)')?.offsetLeft ?? 0")
            page.click('[data-k="zin"]'); page.click('[data-k="zout"]'); page.click('[data-k="zout"]')
            check("zoom: + and − change the scale (shown in the toolbar) and the canvas follows", page.inner_text("#nc-zv") == "80%" and "scale(0.8" in page.get_attribute("#nc-world", "style"), page.inner_text("#nc-zv"))
            bb = page.locator(".nc-card.note >> nth=1 >> .nc-head").bounding_box(); l0 = page.evaluate("document.querySelectorAll('.nc-card.note')[1].offsetLeft")
            page.mouse.move(bb["x"] + 20, bb["y"] + 6); page.mouse.down(); page.mouse.move(bb["x"] + 100, bb["y"] + 6, steps=5); page.mouse.up(); page.wait_for_timeout(300)
            l1 = page.evaluate("document.querySelectorAll('.nc-card.note')[1].offsetLeft")
            check("dragging a card at 80% moves it by the pointer's distance in canvas units (80 px on screen = 100 in the canvas)", abs((l1 - l0) - 100) <= 3, str((l0, l1)))
            page.click('[data-k="zin"]'); check("back to 100%", page.inner_text("#nc-zv") == "100%")
            page.click(".nc-card.note >> nth=1 >> .nc-md"); page.wait_for_selector(".nc-card.note >> nth=1 >> textarea.nc-main:not(.hide)")
            ta2 = page.locator(".nc-card.note >> nth=1 >> textarea.nc-main"); ta2.press("End"); ta2.type(" See also //big")
            page.wait_for_selector(".nc-ref button")
            check("typing // in a note offers to link it to a card, a highlight or a page, filtered as you type", page.locator(".nc-ref button").count() >= 1 and "Big idea" in page.inner_text(".nc-ref"), page.inner_text(".nc-ref"))
            page.keyboard.press("Enter"); page.wait_for_function("!document.querySelector('.nc-ref')")
            check("Enter puts a Markdown link in the note", re.search(r"\[[^\]]*Big idea[^\]]*\]\(jw:card/\w+\)", ta2.input_value()) is not None, ta2.input_value())
            ta2.type("and //2"); page.wait_for_selector(".nc-ref button"); page.keyboard.press("Enter"); page.click("#nc-pn"); page.wait_for_selector('.nc-card.note >> nth=1 >> .nc-md a[href^="jw:page/2"]')
            check("//2 links to page 2", "jw:page/2" in ta2.input_value() or page.locator('.nc-card.note >> nth=1 >> .nc-md a[href="jw:page/2"]').count() == 1)
            page.click('.nc-card.note >> nth=1 >> .nc-md a[href="jw:page/2"]')
            try:
                page.wait_for_function(JS_PAGE_AT_TOP, arg=2, timeout=8000)
            except Exception:  # noqa: BLE001
                pass
            check("clicking a page link turns to that page", page.evaluate(JS_PAGE_AT_TOP, 2))
            page.click('.nc-card.note >> nth=1 >> .nc-md a[href^="jw:card/"]'); page.wait_for_selector(".nc-card.new")
            check("clicking a card link brings that card into view and lights it", page.locator(".nc-card.new").count() >= 1)
            page.click(".nc-card.note >> nth=0 >> [data-c=conn]"); page.wait_for_selector(".nc-connhint:not(.hide)")
            page.click(".nc-card.note >> nth=1 >> .nc-head"); page.wait_for_selector(".bd-l")
            check("an arrow joins the two cards (connect mode ends)", page.locator(".bd-l").count() == 1 and page.locator(".nc-connhint.hide").count() == 1)
            page.evaluate("""() => { const p = document.querySelector('.bd-l .bd-hit'), r = p.getBoundingClientRect(); p.dispatchEvent(new MouseEvent('click', {bubbles: true, clientX: r.left + r.width / 2, clientY: r.top + r.height / 2})); }""")
            page.wait_for_selector(".bd-pop:not(.hide) input"); page.fill(".bd-pop input", "refines"); page.keyboard.press("Enter"); page.wait_for_selector(".bd-lt")
            check("clicking an arrow lets you label it", "refines" in (page.text_content(".bd-lt") or ""))
            page.click('[data-k="frame"]'); page.wait_for_selector(".bd-frame")
            page.fill(".bd-fl", "Key ideas"); page.keyboard.press("Tab"); page.wait_for_timeout(300)
            check("a new frame is placed under the cards, inside the visible area, and can be named", page.input_value(".bd-fl") == "Key ideas" and page.locator(".bd-fg").bounding_box()["x"] + 18 <= page.viewport_size["width"])
            page.evaluate("""async () => { const N = await import('/static/notes.js'), {S} = await import('/static/state.js'); N.updateFrame(S.notes.frames[0].id, {x: 10, y: 500, w: 520, h: 300}); document.querySelector('#nc-scroll').scrollTop = 405; document.querySelector('#nc-scroll').scrollLeft = 0; }""")
            page.wait_for_timeout(300)
            lefts0 = page.evaluate("[...document.querySelectorAll('.nc-card.note')].map(e => e.offsetLeft)")
            fh = page.locator(".bd-fh").bounding_box(); page.mouse.move(fh["x"] + 400, fh["y"] + 8); page.mouse.down(); page.mouse.move(fh["x"] + 460, fh["y"] + 8, steps=5); page.mouse.up(); page.wait_for_timeout(400)
            lefts1 = page.evaluate("[...document.querySelectorAll('.nc-card.note')].map(e => e.offsetLeft)")
            check("moving a frame carries the cards inside it", all(abs((b - a) - 60) <= 3 for a, b in zip(lefts0, lefts1)), str((lefts0, lefts1, page.evaluate("[[...document.querySelectorAll('.bd-frame')].map(e => [e.offsetLeft, e.offsetTop, e.offsetWidth, e.offsetHeight]), [...document.querySelectorAll('.nc-card')].map(e => [e.className.split(' ')[1], e.offsetLeft, e.offsetTop, e.offsetWidth, e.offsetHeight])]"))))
            def notes_when(ok, secs=10):          # the notes file once ok(it) holds (or as it is after `secs`): the page saves a while after a change, after any save already under way
                d, end = {}, time.time() + secs
                while True:
                    try:
                        d = json.load(open(sidecar(2, ".notes.json")))
                    except (OSError, ValueError):
                        d = {}
                    if ok(d) or time.time() > end:
                        return d
                    time.sleep(0.2)
            page.fill("#nc-pn", "Overall: a solid paper about platforms.")
            saved = notes_when(lambda d: str(d.get("paper_note") or "").startswith("Overall"))         # (typing → 0.5 s → the note is set → 0.8 s → saved)
            check("frames, arrows and the note on the paper as a whole are saved with the notes", (saved.get("frames") or [{}])[0].get("label") == "Key ideas" and (saved.get("links") or [{}])[0].get("label") == "refines" and str(saved.get("paper_note") or "").startswith("Overall"), json.dumps({k: saved.get(k) for k in ("frames", "links", "paper_note")})[:300])
            page.click('[data-k="view"][data-v="outline"]'); page.wait_for_selector(".ol-g")
            titles_ = [t.strip().lower() for t in page.locator(".ol-g h4").all_inner_texts()]
            check("the outline lists your frame first (with its cards), then the rest by page, with the arrow and the note on the paper", titles_[0].startswith("key ideas") and page.locator(".ol-g.frame .ol-item").count() >= 2 and "refines" in page.inner_text(".ol-lks") and "solid paper" in page.inner_text(".ol-pn"), str(titles_))
            page.click(".ol-item.note >> nth=0 >> [data-o=edit]"); page.fill(".ol-ta", "Edited in the outline")
            edited = notes_when(lambda d: any(c.get("text") == "Edited in the outline" for c in d.get("cards") or []))
            check("a note can be edited in the outline and the canvas card follows", any(c.get("text") == "Edited in the outline" for c in edited.get("cards") or []))
            page.click('[data-k="view"][data-v="canvas"]'); page.wait_for_selector(".nc-card")
            page.click(".bd-fh [data-fc=del]"); page.wait_for_function("document.querySelectorAll('.bd-frame').length === 0")
            page.click('[data-k="undo"]'); page.wait_for_function("document.querySelectorAll('.bd-frame').length === 1")
            check("undo brings a deleted frame back", page.input_value(".bd-fl") == "Key ideas")
            page.click(".nc-card.note >> nth=0 >> [data-c=del]"); page.wait_for_function("document.querySelectorAll('.bd-l').length === 0")
            check("deleting a card removes its arrows", True)
            page.click(".rd-bar [data-a='back']") if False else None

            print("the section splitter copes with sloppy model output")
            cases = page.evaluate("""async () => { const {mdSections} = await import("/static/output.js"), t = x => mdSections(x).sections.map(s => s.title).join("|"), L = x => mdSections(x).lead;
              return [t("Intro\\n\\n## Evidence\\n- a\\n\\n### Not in the passages\\nx"), L("Intro\\n\\n## Evidence\\n- a"), t("**Short answer**\\nText\\n**Evidence:**\\n- a"), t("Text\\n```\\n## not a heading\\n```\\nmore"), t("## Title ##\\nbody"),
                      t("**Bold sentence with text**: more text, not a heading"), JSON.stringify(mdSections("")), t("# Title\\n## A\\n## B"), t("__Underscored__\\nx"),
                      t("## Evidence\\n- a\\n## Not in the passages\\nx"), t("## A\\ntext\\n### a1\\nmore\\n**Bold lead-in**\\nx\\n## B"), t("**Part**\\n## Real\\n### sub\\n**Other**")]; }""")
            check("…## and ### headings, bold-only lines, fenced code, trailing hashes, a bold lead-in sentence, empty text", cases == ["Evidence", "Intro", "Short answer|Evidence", "", "Title", "", '{"lead":"","sections":[]}', "Title|A|B", "Underscored", "Evidence|Not in the passages", "A|B", "Real"], str(cases))

            print("what went wrong is explained the same way everywhere")
            kinds_ = page.evaluate("""async () => { const {explainError} = await import("/static/state.js"), k = m => explainError(m).kind;
              return [k("Cancelled."), k(new TypeError("Failed to fetch")), k("This month’s AI budget ($2.00) is used up. Raise it in Settings → Usage."), k("anthropic isn’t set up: add ANTHROPIC_API_KEY=… to the .env file"),
                      k("Local-only mode is on, so “ask” can’t use openai."), k("No provider is set for “ask”. Choose one in Settings → AI."), k("429 rate limit exceeded"), k("Can’t reach openai (timed out)."), k("a folder with that name exists"),
                      k("Something went wrong: boom"), explainError("Something went wrong: boom").detail, explainError("a folder with that name exists").title]; }""")
            check("…cancelled, offline, budget, set-up, local-only, busy, unreachable and plain messages are told apart", kinds_ == ["cancelled", "offline", "budget", "setup", "setup", "setup", "busy", "net", "error", "error", "boom", "That didn’t work"], str(kinds_))

            print("reader: Ask, summary, settings")
            page.click('.rd-tabs button[data-tab="ask"]'); page.fill("#ask-q", "How many firms are on the platform?"); page.click('#reader [data-k="send"]')
            page.wait_for_selector(".ask-turn [data-t='regen']", timeout=30000)
            check("Ask answers with a clickable citation", page.locator(".ask-turn .cite").count() >= 1 and "120 firms" in page.inner_text(".ask-turn"))
            check("the answer is split into titled sections: Short answer, Evidence, Not in the passages (marked as a caveat), Sources",
                  [t.strip().lower() for t in page.locator(".ask-turn .of-sec h4").all_inner_texts()] == ["short answer", "evidence", "not in the passages", "sources"] and page.locator(".ask-turn .of-sec.warn").count() == 1 and page.locator(".ask-turn .of-jump button[data-of-j]").count() == 4,
                  str(page.locator(".ask-turn .of-sec h4").all_inner_texts()))
            page.click('.ask-turn .of-jump [data-of-j="sources"]'); page.wait_for_timeout(300)
            check("a jump button opens its section (Sources starts collapsed)", page.locator('.ask-turn .of-sec[data-sec="sources"]:not(.col)').count() == 1)
            page.click('.ask-turn .of-sec[data-sec="evidence-0"] .of-sh h4'); page.wait_for_timeout(150)
            check("a section collapses and expands by clicking its title", page.locator('.ask-turn .of-sec[data-sec="evidence-0"].col').count() == 1)
            page.click('.ask-turn .of-sec[data-sec="evidence-0"] .of-sh h4')
            page.fill("#ask-q", "And why is that?"); page.click('#reader [data-k="send"]'); page.wait_for_function("document.querySelectorAll('.ask-turn').length === 2 && document.querySelectorAll(\".ask-turn [data-t='regen']\").length === 2", timeout=30000)
            hist = [x for x in SEEN if "chat" in x[0] and "Question:" in json.dumps(x[1])][-1][1]["messages"]
            check("a follow-up sends the earlier turn as history", any(m["role"] == "user" and str(m["content"]).startswith("How many firms") for m in hist) and any(m["role"] == "assistant" and "120 firms" in str(m["content"]) for m in hist))
            page.locator(".ask-turn").last.locator("[data-t='compare']").click(); page.wait_for_selector(".ask-alt [data-t='usealt']", timeout=30000)
            check("compare asks a second model and shows it beside the first", "m2" in page.inner_text(".ask-alt") and any(x[1].get("model") == "m2" for x in SEEN if "chat" in x[0]))
            shot(page, "5-ask-compare"); page.click(".ask-alt [data-t='dropalt']")

            n_req = len([x for x in SEEN if "chat" in x[0]]); page.locator(".ask-turn").last.locator("[data-t='regen']").click()
            page.wait_for_function("document.querySelectorAll(\".ask-turn [data-t='regen']\").length === 2", timeout=30000)
            check("regenerate asks again instead of reusing the cache", len([x for x in SEEN if "chat" in x[0]]) == n_req + 1)
            page.locator(".ask-turn").last.locator("[data-t='follow']").click(); page.wait_for_selector(".ask-turn .ask-fu [data-starter]", timeout=15000)
            check("💡 suggests follow-up questions", page.locator(".ask-turn [data-starter]").count() == 4)
            page.wait_for_timeout(1200)
            check("the conversation is saved next to the paper (and the reader asks first, so nothing else is created)", os.path.exists(sidecar(2, ".chat.json")) and len(json.load(open(sidecar(2, ".chat.json")))["turns"]) == 2)
            page.click('.rd-tabs button[data-tab="sum"]'); page.select_option("#sum-t", "tldr"); page.click("#sum-go"); page.wait_for_selector("#sum-act:not(.hide)", timeout=30000)
            check("a summary can be generated with another template (not saved until you say so)", "FAKE SUMMARY" in page.inner_text("#sum-body") and not os.path.exists(sidecar(2, ".md")))
            check("…in titled sections, the TL;DR first and highlighted as the lead", [t.strip().lower() for t in page.locator("#sum-body .of-sec h4").all_inner_texts()] == ["tl;dr", "method", "key findings"] and page.locator("#sum-body .of-sec.lead").count() == 1 and "not saved yet" in page.inner_text("#sum-body"),
                  str(page.locator("#sum-body .of-sec h4").all_inner_texts()))
            page.click("#sum-act [data-sx='replace']"); page.wait_for_timeout(800)
            check("…and saved as the paper's summary on request", os.path.exists(sidecar(2, ".md")) and "FAKE SUMMARY" in open(sidecar(2, ".md")).read())
            page.reload(); page.wait_for_selector("#libitem"); page.click("#libitem"); page.wait_for_selector("#lf-q"); page.click("#lf-list"); page.wait_for_selector("#lib-results .librow")
            page.locator('.librow:has-text("Citations and Links") [data-read]').click(); page.wait_for_selector(".rd-band .textLayer span", timeout=30000)
            page.click('.rd-tabs button[data-tab="ask"]'); page.wait_for_selector(".ask-turn", timeout=10000)
            check("after a reload the conversation is still there", page.locator(".ask-turn").count() == 2)
            page.click('.rd-tabs button[data-tab="toc"]'); page.click("#toc-seg [data-v='pages']"); page.wait_for_function("document.querySelectorAll('#thumb-list .thumb canvas').length === 2", timeout=10000)
            page.wait_for_timeout(800)
            check("page thumbnails are drawn", page.locator("#thumb-list .thumb").count() == 2)
            page.click(".rd-bar [data-a='back']"); page.wait_for_selector("#lf-q")

            page.click("#settingsbtn"); page.wait_for_selector("#st-body tr[data-p]")
            check("settings: providers, per-feature models and a key-free status", page.locator("tr[data-f]").count() >= 17 and page.locator("tr[data-f='ideas']").count() == 1 and "key found" in page.inner_text("tr[data-p='fake']"))
            JS_TIDY = """(() => { const r = e => e.getBoundingClientRect(), mid = e => r(e).top + r(e).height / 2;
              const rows = [...document.querySelectorAll('tr[data-p]')], keyRows = rows.filter(t => { const i = t.querySelector('input[type=password]'); return i && i.offsetParent !== null; });
              const hs = rows.map(t => Math.round(r(t).height)), feat = [...document.querySelectorAll('tr[data-f]:not([data-f=emb])')];
              const clipped = [...document.querySelectorAll('tr[data-f] select, tr[data-f] input.fm, tr[data-p] input.pm')].filter(e => e.scrollWidth > e.clientWidth + 1 && e.tagName === 'SELECT');
              return {keyRows: keyRows.length, sameLine: keyRows.every(t => { const i = t.querySelector('input[type=password]'), b = t.querySelector('[data-savekey]'); return Math.abs(mid(i) - mid(b)) < 4 && r(i).right <= r(b).left + 1; }),
                      keyW: Math.min(...keyRows.map(t => r(t.querySelector('input[type=password]')).width)), rowH: Math.max(...hs) - Math.min(...hs),
                      selW: Math.min(...feat.map(t => r(t.querySelector('.fp')).width), ...feat.map(t => r(t.querySelector('.fe')).width)), modelW: Math.min(...feat.map(t => r(t.querySelector('.fm')).width)),
                      clipped: clipped.length, head: [...document.querySelectorAll('.st-prov th')].map(x => x.textContent.trim()), pageX: document.documentElement.scrollWidth <= window.innerWidth}; })()"""
            t_ = page.evaluate(JS_TIDY)
            check("the model's name is not shown under the logo (only the app's name)", page.locator("#modelname").count() == 0 and page.inner_text(".brand-t").strip() == "Journal Watcher", page.inner_text(".brand-t"))
            check("settings: the providers table is tidy: each key box and its Save key button are on one line, no dropdown is clipped, rows have the same height, the header is short", t_["keyRows"] >= 1 and t_["sameLine"] and t_["keyW"] >= 150 and t_["rowH"] <= 6 and t_["selW"] >= 80 and t_["modelW"] >= 180 and t_["clipped"] == 0 and t_["head"][4] == "Models you use", str(t_))
            fk = page.locator("tr[data-p='fake']"); fk.locator("[data-keychange]").click()
            kw = page.evaluate("""(() => { const t = document.querySelector("tr[data-p='fake']"), i = t.querySelector('input[type=password]'); return {w: i.getBoundingClientRect().width, ok: t.querySelector('.keyok').offsetParent === null}; })()""")
            check("clicking Change on a saved key hides the 'key found' status and gives the new key box as much room as an empty one", kw["ok"] and kw["w"] >= t_["keyW"] - 2, str(kw) + " vs " + str(t_["keyW"]))
            page.set_viewport_size({"width": 1000, "height": 900}); page.wait_for_timeout(200)
            check("…and in a narrow window the tables scroll inside their card instead of the page scrolling sideways", page.evaluate(JS_TIDY)["pageX"] is True)
            pm_w = page.evaluate("Math.min(...[...document.querySelectorAll('tr[data-p] input.pm')].map(e => e.getBoundingClientRect().width))")
            check("…and even then the 'Models you use' box keeps room for a model name (the table scrolls before it collapses)", pm_w >= 140, str(pm_w))
            page.set_viewport_size({"width": 1400, "height": 950})
            check("settings: 'Screening new papers' has its own model row (the budget and the usage page now cover it), pointing at a provider that exists", page.locator("tr[data-f='screen']").count() == 1 and page.input_value("tr[data-f='screen'] .fm") != "" and page.input_value("tr[data-f='screen'] .fp") in api("/api/ai/config")["providers"] and "screen" in api("/api/ai/config")["features"])
            check("Settings has ONE 'AI' tab with three pages under it (AI & privacy, Usage & cost, Prompts); they are not tabs of their own", page.locator(".st-tabs button").count() == 8 and [b.inner_text() for b in page.locator(".st-sub button").all()] == ["AI & privacy", "Usage & cost", "Prompts"] and page.inner_text(".st-tabs .on").strip() == "AI" and page.locator(".st-tabs button[data-t='usage']").count() == 0)
            page.click('.st-sub button[data-t="usage"]'); page.wait_for_selector(".st-stat"); page.click('.st-tabs button[data-t="reader"]'); page.wait_for_selector(".legend")
            check("the pages under AI show only on the AI tab", page.locator("#st-sub:not(.hide)").count() == 0)
            page.click('.st-tabs button[data-t="ai"]'); page.wait_for_selector(".st-stat")
            check("coming back to the AI tab returns to the page you were on there (Usage & cost)", page.locator(".st-sub button.on").inner_text() == "Usage & cost" and page.locator(".st-stat").count() > 0)
            page.click('.st-sub button[data-t="ai"]'); page.wait_for_selector("#st-main")
            before_ = api("/api/ai/config")["features"]; emb_before = api("/api/ai/config")["embeddings"]
            page.select_option("#mn-p", "websearch"); page.fill("#mn-m", "m2"); page.click("#mn-apply")
            rows_f = [(tr.get_attribute("data-f"), tr.locator(".fp").input_value(), tr.locator(".fm").input_value(), tr.locator(".fe").input_value()) for tr in page.locator("tr[data-f]").all() if tr.get_attribute("data-f") != "emb"]
            check("'Use one for everything' sets the provider and model of every feature row (not their effort); nothing is saved until Save", all(p_ == "websearch" and m_ == "m2" for _, p_, m_, _e in rows_f) and [e_ for *_, e_ in rows_f] == [(before_.get(k_) or before_["ask"]).get("effort", "low") for k_, *_r in rows_f] and api("/api/ai/config")["features"] == before_, str(rows_f[:3]))
            page.select_option("#mn-p", "fake"); page.fill("#mn-m", "m1"); page.click("#mn-apply")
            check("…a provider that cannot search the web leaves the Ideas Lab's current events alone and says so", page.locator("tr[data-f='events'] .fp").input_value() == "websearch" and "current events" in page.inner_text("#mn-note").lower() and page.locator("tr[data-f='ask'] .fp").input_value() == "fake", page.inner_text("#mn-note"))
            page.select_option("#mn-p", "websearch"); page.fill("#mn-m", "m2"); page.click("#mn-apply"); page.click("#st-save"); page.wait_for_selector("#st-msg:not(:empty)", timeout=5000)
            feats_ = api("/api/ai/config")["features"]
            check("…and Save keeps it: every feature now uses that provider and model (embeddings untouched)", all(f_["provider"] == "websearch" and f_["model"] == "m2" for f_ in feats_.values()) and api("/api/ai/config")["embeddings"] == emb_before, str({k_: (v_["provider"], v_["model"]) for k_, v_ in feats_.items() if (v_["provider"], v_["model"]) != ("websearch", "m2")}))
            page.select_option("#mn-p", "fake"); page.fill("#mn-m", "m1"); page.click("#mn-apply"); page.click("#st-save"); page.wait_for_timeout(600)
            # the row starts at what most features use: the provider most use, and the model most of THAT provider's features use (never another provider's model)
            keys_ = [tr.get_attribute("data-f") for tr in page.locator("tr[data-f]").all() if tr.get_attribute("data-f") != "emb"]
            split_ = {k_: {"provider": "fake", "model": "m1" if i_ < 5 else "m3"} if i_ < 10 else {"provider": "websearch", "model": "m2"} for i_, k_ in enumerate(keys_)}
            saved_feats_ = api("/api/ai/config")["features"]
            api("/api/ai/config", {"features": split_})
            page.click('.st-tabs button[data-t="reader"]'); page.click('.st-tabs button[data-t="ai"]'); page.wait_for_selector("#st-main")
            check("'Use one for everything' starts at a provider and a model that belong together (10 features on fake split over m1 and m3, 8 on websearch sharing m2: it starts at fake and m1 or m3, never fake with m2)", page.input_value("#mn-p") == "fake" and page.input_value("#mn-m") in ("m1", "m3"), page.input_value("#mn-p") + " / " + page.input_value("#mn-m"))
            api("/api/ai/config", {"features": {k_: {"provider": v_["provider"], "model": v_["model"]} for k_, v_ in saved_feats_.items()}})
            page.fill("#st-budget", "5"); page.click("#st-save"); page.wait_for_timeout(600)
            check("settings: the monthly budget is saved", api("/api/ai/config")["budget"]["monthly_usd"] == 5)
            check("settings: 'reuse earlier screening verdicts for N days' shows its default (90) and is saved", page.input_value("#st-reuse") == "90" and api("/api/ai/config")["screening"]["reuse_days"] == 90)
            page.fill("#st-reuse", "30"); page.click("#st-save"); page.wait_for_timeout(600)
            check("…the number of days is kept, and an impossible one is refused with a reason", api("/api/ai/config")["screening"]["reuse_days"] == 30 and page.inner_text("#st-msg").strip() == "Saved.")
            page.fill("#st-reuse", "99999"); page.click("#st-save"); page.wait_for_function("document.querySelector('#st-msg.bad')", timeout=5000)
            check("…(refused: 0 to 3650 days)", api("/api/ai/config")["screening"]["reuse_days"] == 30 and "3650" in page.inner_text("#st-msg"), page.inner_text("#st-msg"))
            page.fill("#st-reuse", "90"); page.click("#st-save"); page.wait_for_timeout(600)
            page.click('.st-sub button[data-t="usage"]'); page.wait_for_selector(".st-stat")
            check("usage panel shows spend, cached answers and what was sent", "cache" in page.inner_text("#st-body").lower() and page.locator(".st-table.log tr").count() > 3)
            check("the usage chart always shows the last 30 days, not just the days with spend", page.locator(".st-chart > div").count() == 30)
            shot(page, "6-usage"); page.click('.st-tabs button[data-t="reader"]'); page.fill('.legend input[data-c="yellow"]', "Claim"); page.click("#st-save"); page.wait_for_timeout(600)
            page.click('.st-tabs button[data-t="ai"]'); page.click('.st-sub button[data-t="prompts"]'); page.click("#pa"); page.locator(".st-item .pn").last.fill("Summarise in one line"); page.click("#st-save"); page.wait_for_timeout(600)
            check("prompts: a new prompt is saved to prompts.json", "Summarise in one line" in open(os.path.join(TMP, ".agents/skills/ais-journal-watcher/prompts.json")).read())
            page.click("#st-back")
            page.locator('.librow:has-text("Citations and Links") [data-read]').click(); page.wait_for_selector(".rd-band .textLayer span", timeout=30000)
            page.evaluate(JS_SELECT, [1, "Platform effects are large"]); page.wait_for_selector("#rd-sel:not(.hide)")
            check("the renamed colour shows in the reader", "Claim" in (page.get_attribute('#rd-sel [data-c="yellow"]', "title") or ""))
            page.click('#rd-sel [data-s="explain"]'); page.wait_for_selector(".rd-expmenu:not(.hide)")
            check("…and the new prompt appears in the ✨ menu", "Summarise in one line" in page.inner_text(".rd-expmenu"))


            print("round 2: paper cards, folder evidence, Research Ideas Lab")
            lab_files = [r[3] for r in rows]
            api("/api/folders", {"action": "create", "name": "Lab", "files": lab_files})
            page.goto(base + "/"); page.click("#libitem"); page.wait_for_selector("#lf-q"); page.click("#lf-list"); page.wait_for_selector("#lib-results .librow")
            page.locator('#lib-results .librow:has-text("Open Platforms") [data-read]').click(); page.wait_for_selector(".rd-band .textLayer span", timeout=30000)
            page.click('.rd-tabs button[data-tab="card"]'); page.wait_for_selector('[data-k="make"]')
            check("the Card tab offers to make a card and says what it will cost", "m1" in page.inner_text("#reader .cd-est") and ("¢" in page.inner_text("#reader .cd-est") or "$" in page.inner_text("#reader .cd-est")), page.inner_text("#reader .cd-est"))
            page.click('[data-k="make"]'); page.wait_for_selector(".cd-item", timeout=30000)
            body = page.inner_text("#reader .cd")
            check("the card shows question, method, findings and limitations", all(t in body for t in ("How does governance shape complementor entry?", "fixed effects panel", "Entry rises 12 percent.", "generalize beyond")), [t for t in ("How does governance shape complementor entry?", "fixed effects panel", "Entry rises 12 percent.", "generalize beyond") if t not in body])
            check("every statement shows the pages it cites; the invented passage was dropped", page.locator("#reader .cd-cite").count() >= 6 and "citation" in body.lower() and "removed" in body.lower())
            titles = [t.strip().lower() for t in page.locator("#reader .of-sec h4").all_inner_texts()]
            check("the card is grouped in titled sections with a jump bar, and empty groups are folded", all(x in titles for x in ["research question", "topics", "method", "findings", "limitations"]) and page.locator("#reader .of-jump button[data-of-j]").count() == len(titles), str(titles))
            dup = page.evaluate("[...document.querySelectorAll('#reader .cd-item')].filter(it => { const t = [...it.querySelectorAll('.cd-cite')].map(c => c.textContent); return new Set(t).size !== t.length; }).length")
            check("a statement shows each page once (no 'p. 2 p. 2')", dup == 0, str(dup))
            page.click('#reader .of-jump [data-of-j="findings"]'); page.wait_for_timeout(300)
            check("a jump button in the card opens and lights its section", page.locator('#reader .of-sec[data-sec="findings"]:not(.col)').count() == 1)
            check("a paper card is saved beside the PDF", os.path.exists(sidecar(0, ".card.json")) and json.load(open(sidecar(0, ".card.json")))["rev"] == 1)
            page.evaluate("""() => { const sp = []; window.__spoken = sp; Object.defineProperty(window, "speechSynthesis", {configurable: true, value: {getVoices() { return []; }, speak(u) { sp.push(u.text); setTimeout(() => u.onend && u.onend(), 5); }, cancel() {}, pause() {}, resume() {}, speaking: false, addEventListener() {}}});
              Object.defineProperty(window, "SpeechSynthesisUtterance", {configurable: true, value: function (t) { this.text = t; }}); }""")
            page.click('#reader [data-k="listen"]'); page.wait_for_selector(".lsn-s", timeout=30000)
            req = [b for p_, b in SEEN if b.get("messages") and str(b["messages"][0].get("content", "")).startswith("You write the script of a short spoken briefing")][-1]
            check("🎧 Listen on the Card tab writes a short script from the card (the model is told to write for the ear)", "PAPER:" in req["messages"][-1]["content"] and "for the ear" in req["messages"][0]["content"] and page.locator(".lsn-s").count() >= 6 and "two minutes" in page.inner_text("#lsn-body"))
            page.click("#lsn-play")
            try:
                page.wait_for_function("window.__spoken.length >= document.querySelectorAll('.lsn-s').length", timeout=10000)      # all of them: the fake voice speaks one sentence every few milliseconds
            except Exception:  # noqa: BLE001
                pass
            check("play reads it sentence by sentence with the browser's voice", page.evaluate("window.__spoken[0]").startswith("Here is the paper") and len(page.evaluate("window.__spoken")) == page.locator(".lsn-s").count(),
                  page.evaluate("[window.__spoken.length, document.querySelectorAll('.lsn-s').length]"))
            page.click('[data-l="close"]'); check("the player closes", page.locator(".lsn").count() == 0)
            page.locator("#reader .cd-cite").first.click(); page.wait_for_timeout(500)
            page.click('#reader .cd-t[data-path="findings.0.text"]'); page.fill("#reader .cd-ed", "Entry rises by about twelve percent."); page.keyboard.press("Enter")
            for _ in range(30):
                if json.load(open(sidecar(0, ".card.json")))["rev"] == 2:
                    break
                page.wait_for_timeout(200)
            saved = json.load(open(sidecar(0, ".card.json")))
            check("editing a line saves it and marks it as yours", saved["data"]["findings"][0]["text"] == "Entry rises by about twelve percent." and saved["data"]["findings"][0]["edited"] is True and saved["edited"] is True)
            shot(page, "7-card")
            page.click('#reader [data-k="copy"]'); page.wait_for_timeout(400)
            check("the card can be copied as Markdown", "## Limitations" in page.evaluate("navigator.clipboard.readText()"))
            page.goto(base + "/"); page.click("#libitem"); page.wait_for_selector("#lf-q"); page.click("#lf-list"); page.wait_for_selector("#lib-results .librow")
            check("the Library marks papers that have a card", page.locator('#lib-results .librow:has-text("Open Platforms") [data-card]').count() == 1 and page.locator("#lib-results [data-card]").count() == 1)
            page.click('.fchip[data-openfolder="lab"]'); page.wait_for_selector(".fvtabs")
            page.click('.fvtabs [data-ft="evidence"]'); page.wait_for_selector("#ev-make")
            check("Evidence: 1 of 3 papers has a card, with a button for the rest", "1 of 3" in page.inner_text("#fv-body") and "Make 2 cards" in page.inner_text("#ev-make"))
            page.click("#ev-make"); page.wait_for_selector("#fv-body:has-text('3 of 3')", timeout=60000); page.wait_for_selector(".ev-cl", timeout=20000)
            check("three cards exist after the batch; the shared limitation is one group of 3 papers", all(os.path.exists(sidecar(i, ".card.json")) for i in range(3)) and "3 papers" in page.inner_text(".ev-cl >> nth=0"))
            check("the folder header counts the new cards without a reload", "3 with a card" in page.inner_text(".pagehead .meta"), page.inner_text(".pagehead .meta"))
            check("Evidence lists topics and theories with how many papers share them", "platform governance" in page.inner_text("#fv-body") and "Transaction cost theory" in page.inner_text("#fv-body"))
            shot(page, "8-evidence")
            page.click('.ev-cl >> nth=0 >> summary'); page.locator(".ev-cl >> nth=0 >> .linkbtn").first.click(); page.wait_for_selector(".rd-band .textLayer span", timeout=30000)
            check("a sentence in the evidence opens its paper at that page", page.evaluate("document.querySelector('#rd-num').value") in ("1", "2", "3"))
            page.click('#reader [data-a="back"]'); page.wait_for_selector(".fvtabs")
            page.click('.fvtabs [data-ft="lab"]'); page.wait_for_selector("#lb-focus")
            check("the lab says it is built from three cards", "3" in page.inner_text("#lb-cards") and "paper card" in page.inner_text("#lb-cards"))
            page.fill("#lb-focus", "AI and platform governance"); page.fill("#lb-cons", "public data only")
            page.click('[data-k="events"]'); page.wait_for_selector(".lab-event >> nth=1", timeout=60000)
            ev_text = page.inner_text("#lab-events")
            check("current events come back with their links checked against the search sources", "EU platform act adopted" in ev_text and "found in the search results" in ev_text and "not among the search results" in ev_text, ev_text[:600])
            page.evaluate("window.scrollTo(0, 260)"); shot(page, "10-lab-top")
            page.locator('input[data-ev="1"]').uncheck(); page.wait_for_timeout(900)
            check("unticking an event is saved on the board", json.load(open(os.path.join(TMP, "Papers", "_folders", "lab.ideas.json")))["events"]["items"][1]["keep"] is False)
            page.click('[data-k="gen"]'); page.wait_for_selector(".idea", timeout=90000)
            check("two ideas land on the board", page.locator(".idea").count() == 2)
            first = page.inner_text(".idea >> nth=0")
            check("an idea shows its question, evidence chips, grounding and the editor's score", "Transparency under algorithmic governance" in first and "grounded" in first and page.locator(".idea >> nth=0 >> .lab-score").count() == 1 and page.locator(".idea >> nth=0 >> .ev-chip").count() == 3)
            check("an idea card shows its score as a number with four labelled meters", page.locator(".idea >> nth=0 >> .lab-sc").count() == 4 and "novelty" in page.inner_text(".idea >> nth=0 >> .idea-scores").lower() and page.locator(".idea >> nth=0 >> .idea-tabs button").count() == 5)
            page.click('.idea >> nth=0 >> [data-itab="design"]'); page.wait_for_timeout(200)
            check("the Design tab of an idea shows lens, method, data and journals; the Overview is hidden", page.locator(".idea >> nth=0 >> .idea-pane.on").get_attribute("data-pane") == "design" and "method" in page.inner_text(".idea >> nth=0 >> .idea-pane.on").lower())
            page.click('.idea >> nth=0 >> [data-itab="overview"]')
            check("the guess is labelled as a guess", "speculative" in page.inner_text(".idea >> nth=1"))
            ask = [b for _, b in SEEN if b.get("messages") and b["messages"][0].get("role") == "system" and b["messages"][0]["content"].startswith("You are a senior information-systems researcher and journal reviewer")][-1]
            prompt = ask["messages"][-1]["content"]
            check("the ideas prompt had the evidence ids, your brief, and only the event you left ticked", "L1 [n=3" in prompt and "AI and platform governance" in prompt and "public data only" in prompt and "EU platform act adopted" in prompt and "app store outage" not in prompt, [t for t in ("L1 [n=3", "AI and platform governance", "public data only", "EU platform act adopted") if t not in prompt] + (["outage present"] if "app store outage" in prompt else []) + [prompt[:700]])
            page.click(".idea >> nth=0 >> .ev-chip >> nth=0"); page.wait_for_selector(".ev-detail .ev-it")
            check("clicking an evidence chip lists the sentences behind it", "generalize" in page.inner_text(".ev-detail"))
            shot(page, "9-ideas")
            page.click('.idea >> nth=0 >> [data-a="star"]'); page.fill(".idea >> nth=0 >> .idea-note", "Talk to my supervisor"); page.wait_for_timeout(1200)
            board = json.load(open(os.path.join(TMP, "Papers", "_folders", "lab.ideas.json")))
            check("star and note are saved in Papers/_folders/lab.ideas.json", any(x["star"] and x["note"] == "Talk to my supervisor" for x in board["ideas"]) and board["runs"])
            page.click('.idea >> nth=0 >> [data-a="develop"]'); page.wait_for_selector(".idea-dev .of-sec", timeout=60000)
            page.wait_for_function("(document.querySelector('.idea .idea-tabs .on') || {}).textContent.includes('✓')", timeout=60000)   # (the sections show while the answer still streams; the ✓ only once the server has saved it on the board)
            check("the outline appears in sections on the Outline tab of the idea card", page.locator('.idea >> nth=0 >> .idea-tabs .on').inner_text().strip().startswith("Outline") and page.locator(".idea-dev .of-sec").count() >= 2)
            check("Develop turns the idea into an outline saved on the board", "working title" in page.inner_text(".idea-dev").lower() and "developed" in open(os.path.join(TMP, "Papers", "_folders", "lab.ideas.json")).read())
            page.reload(); page.wait_for_selector("#libitem"); page.click("#libitem"); page.wait_for_selector("#lf-q"); page.click('.fchip[data-openfolder="lab"]'); page.wait_for_selector(".fvtabs")
            page.click('.fvtabs [data-ft="lab"]'); page.wait_for_selector(".idea")
            check("after a reload the board is as you left it", page.locator(".idea").count() == 2 and page.locator(".idea .star.on").count() == 1 and "Talk to my supervisor" in page.input_value(".idea >> nth=0 >> .idea-note"))
            md = urllib.request.urlopen(base + "/api/export/ideas?folder=lab&fmt=md").read().decode()
            check("the board exports as Markdown with its evidence", "# Research ideas — Lab" in md and "Transparency under algorithmic governance ★" in md and "L1 —" in md and "EU platform act adopted" in md)
            check("the board exports as Word", urllib.request.urlopen(base + "/api/export/ideas?folder=lab&fmt=docx").read()[:2] == b"PK")
            page.click('.idea >> nth=1 >> [data-a="reject"]'); page.wait_for_timeout(900)
            check("a rejected idea is hidden and left out of the export", page.locator(".idea").count() == 1 and "A bold guess" not in urllib.request.urlopen(base + "/api/export/ideas?folder=lab&fmt=md").read().decode())


            print("round 3: related-work drafter")
            page.goto("about:blank"); page.goto(base + "/#/folder/lab/draft")
            page.wait_for_selector('[data-dr="go"]', timeout=15000)
            check("the folder has a Related work tab that lists its papers (all with cards ticked) and what the draft will cost", page.locator('.fvtabs [data-ft="draft"]').count() == 1 and page.locator("input[data-pf]:checked").count() == 3)
            page.fill("#dr-topic", "We study how transparency rules change who joins a platform."); page.select_option("#dr-words", "300")
            page.wait_for_function("document.querySelector('#dr-est').textContent.includes('·')", timeout=10000)
            page.click('[data-dr="go"]'); page.wait_for_selector('.of[data-tool="draft"]', timeout=60000)
            req = [b for p_, b in SEEN if b.get("messages") and str(b["messages"][0].get("content", "")).startswith("You write the RELATED WORK")][-1]
            user = req["messages"][1]["content"] if len(req["messages"]) > 1 else ""
            check("the model got the cards as numbered notes, your study and the length, and nothing else", "P1-1 [question]" in user and "P3-" in user and "transparency rules" in user and "about 300 words" in user.lower(), user[:300])
            titles = [t.strip().lower() for t in page.locator(".of[data-tool=draft] .of-sec h4").all_inner_texts()]
            check("the draft is split by theme, then References and a Check section", titles == ["disclosure and trust", "methods and contexts", "gap and contribution", "references", "check before you use it"], str(titles))
            txt = page.inner_text(".of[data-tool=draft] .of-sec >> nth=0")
            check("its citations are written out as author-year and are clickable", re.search(r"\([A-Z][\w &.]+, 20\d\d; [A-Z]", txt) is not None and "Tian" in txt and page.locator(".dr-c").count() >= 4, txt[:300])
            check("the check says what is wrong: one citation to a note that does not exist, one sentence without a source", page.locator(".dr-unc li").count() == 1 and "1 citation" in page.inner_text('.of-sec[data-sec="check"]'), page.inner_text('.of-sec[data-sec="check"]')[:300])
            check("the references come from the library's own data, only for the papers cited", page.locator(".dr-refs li").count() == 3 and "Tian, J." in page.inner_text(".dr-refs") or page.locator(".dr-refs li").count() == 3, page.inner_text(".dr-refs"))
            page.select_option("#dr-cs", "numbered"); page.wait_for_function("document.querySelectorAll('.dr-refs li[value]').length === 3")
            check("numbered citations: [1], [2] in order of first citation, with a numbered reference list", page.locator(".dr-refs li[value]").count() == 3 and "[" in page.inner_text(".of[data-tool=draft] .of-sec >> nth=0"))
            page.select_option("#dr-cs", "author-year")
            want_ = re.search(r"\(p\. (\d+)\)", page.locator(".dr-c").first.get_attribute("title") or "")      # the chip says which page its note comes from
            page.locator(".dr-c").first.click(); page.wait_for_selector(".rd-band .textLayer span", timeout=30000)
            if want_:
                try:
                    page.wait_for_function("(n) => document.querySelector('#rd-num').value === n", arg=want_.group(1), timeout=5000)
                except Exception:  # noqa: BLE001
                    pass
            check("a citation opens the paper in the reader at the page its note names (the old reader showed the page above)", bool(want_) and page.input_value("#rd-num") == want_.group(1),
                  [want_.group(1) if want_ else None, page.input_value("#rd-num"), page.evaluate("[...document.querySelectorAll('.rd-band')].map(b => Math.round(b.getBoundingClientRect().top - document.querySelector('#rd-pdf').getBoundingClientRect().top))")])
            page.click('#reader [data-a="back"]'); page.wait_for_selector('[data-dr="edit"]')
            page.click('[data-dr="edit"]'); page.fill("#dr-raw", page.input_value("#dr-raw") + "\n\nOne more claim that is backed by a note [P1-3].\n"); page.click('[data-dr="saveedit"]'); page.wait_for_selector(".of-m.edited")
            check("you can edit the text; the citations are checked again and the draft says it is yours", "edited by you" in page.inner_text(".of-meta") and json.load(open(os.path.join(TMP, "Papers", "_folders", "lab.draft.json")))["edited"] is True)
            md_ = urllib.request.urlopen(base + "/api/export/draft?folder=lab&fmt=md").read().decode()
            check("the export turns ids into citations, with the reference list; Word works too", "## References" in md_ and "[P1-" not in md_ and "{P" not in md_ and "Tian & Xue (2026)" in md_ and "(Costa, 2026" in md_ and urllib.request.urlopen(base + "/api/export/draft?folder=lab&fmt=docx").read()[:2] == b"PK", md_[:300])
            tex_ = urllib.request.urlopen(base + "/api/export/draft?folder=lab&fmt=tex").read().decode(); bib_ = urllib.request.urlopen(base + "/api/export/draft?folder=lab&fmt=bib").read().decode()
            keys_in_tex = set(re.findall(r"\\cite[pt]\{([^}]*)\}", tex_)); keys_in_tex = {k for ks in keys_in_tex for k in ks.split(",")}
            check("the draft exports as LaTeX for Overleaf (\\subsection, \\citep{keys}, \\citet{key}) with a .bib that has those keys", "\\subsection{Disclosure and trust}" in tex_ and "\\citep{" in tex_ and "\\citet{" in tex_ and "[P" not in tex_ and "{P" not in tex_ and keys_in_tex and all("{" + k + "," in bib_ for k in keys_in_tex), tex_[:400] + " || " + bib_[:200])
            check("it ran as a job in the activity list", any(j["kind"] == "draft" and j["status"] == "done" for j in api("/api/activity")["jobs"]))

            print("round 2b: ask a folder, comparison matrix")
            page.click('.fvtabs [data-ft="ask"]'); page.wait_for_selector("#am-q")
            page.wait_for_selector("#am-info:has-text('3 with a card')")
            check("the folder's Ask tab says how many papers it reads and what is new to it", "3 papers" in page.inner_text("#am-info") and "for the first time" in page.inner_text("#am-info"))
            page.fill("#am-q", "What do the papers find about governance?"); page.click('[data-k="send"]'); page.wait_for_selector(".am-log .ask-a .cite", timeout=90000)
            check("the answer cites passages, and they come from several papers", "[multi m1]" in page.inner_text(".am-log") and "from 3 papers" in page.inner_text(".am-log .ask-src") or "from 2 papers" in page.inner_text(".am-log .ask-src"))
            check("the folder answer has the same sections: short answer, by paper, agreement and disagreement, sources", [t.strip().lower() for t in page.locator(".am-log .ask-turn .of-sec h4").all_inner_texts()] == ["short answer", "by paper", "agreement and disagreement", "sources"], str(page.locator(".am-log .ask-turn .of-sec h4").all_inner_texts()))
            req = [b for p_, b in SEEN if b.get("messages") and str(b["messages"][0].get("content", "")).startswith("You are a careful research assistant answering a question from numbered passages")][-1]
            check("every passage sent is labelled with its paper and page", "(Costa et al. (2026), p. " in req["messages"][-1]["content"] or "(Costa (2026), p. " in req["messages"][-1]["content"] or "Tian et al. (2026), p. " in req["messages"][-1]["content"], req["messages"][-1]["content"][:300])
            page.click(".am-log .ask-a .cite >> nth=0"); page.wait_for_selector(".rd-band .textLayer span", timeout=30000)
            check("a citation opens that paper in the reader", page.locator("#rd-title").inner_text() != "")
            page.click('#reader [data-a="back"]'); page.wait_for_selector(".fvtabs")
            page.wait_for_timeout(1200); page.click('.fvtabs [data-ft="ask"]')
            try:
                page.wait_for_selector(".am-log .ask-turn", timeout=8000)
            except Exception:  # noqa: BLE001
                print("   chat file:", os.path.exists(os.path.join(TMP, "Papers", "_folders", "lab.ask.json")), "| errors:", errors[-3:], "| log:", page.inner_text(".am-log")[:200]); raise
            check("the conversation is saved with the folder and comes back", os.path.exists(os.path.join(TMP, "Papers", "_folders", "lab.ask.json")) and page.locator(".am-log .ask-turn").count() == 1)
            page.select_option('[data-s="mode"]', "cards"); page.wait_for_timeout(600)
            page.fill("#am-q", "Which methods do they use?"); page.click('[data-k="send"]'); page.wait_for_function("document.querySelectorAll('.am-log .ask-turn').length === 2 && document.querySelectorAll('.am-log .ask-turn')[1].querySelector('[data-t=regen]')", timeout=90000)
            req = [b for p_, b in SEEN if b.get("messages") and str(b["messages"][0].get("content", "")).startswith("You are a careful research assistant answering a question from numbered passages")][-1]
            check("reading the cards sends their lines with section and page", "findings, p. " in req["messages"][-1]["content"] and "cards" in req["messages"][0]["content"])
            check("the second question carries the first as history", [m["role"] for m in req["messages"]][1:3] == ["user", "assistant"])
            shot(page, "11-ask-folder")
            page.click('[data-k="clear"]'); page.wait_for_selector(".am-log .empty"); page.wait_for_timeout(900)
            check("clearing the conversation deletes its file", not os.path.exists(os.path.join(TMP, "Papers", "_folders", "lab.ask.json")))

            page.click('.fvtabs [data-ft="matrix"]'); page.wait_for_selector("table.mx tbody tr", timeout=30000)
            check("the matrix has a row per paper and the default columns from the cards", page.locator("table.mx tbody tr").count() == 3 and page.locator("table.mx thead th[data-col]").count() == 5 and "fixed effects panel" in page.inner_text("table.mx"))
            check("cells carry the pages they come from", page.locator("table.mx .mx-cite").count() >= 6)
            answers.extend(["Moderators", "Which moderators did the paper test?"]); page.click('[data-k="newcol"]') if page.locator("#mx-colmenu:not(.hide)").count() else (page.click('[data-k="cols"]'), page.click('[data-k="newcol"]'))
            page.wait_for_selector('table.mx thead th:has-text("Moderators")')
            check("a new column of your own appears (marked as yours) and the fill button says how many cells it needs", page.locator('th:has-text("Moderators") .cd-tag').count() == 1 and "Fill 3 empty cells" in page.inner_text('[data-k="fill"]'))
            check("matrix headers are tinted by where the column comes from (the paper cards or you), with a legend", page.locator("table.mx th.mx-b").count() >= 5 and page.locator("table.mx th.mx-y").count() == 1 and page.locator(".mx-legend").count() == 1)
            page.click('[data-k="fill"]'); page.wait_for_function("[...document.querySelectorAll('table.mx td')].filter(td => td.innerText.includes('Tenure moderates the effect.')).length === 3", timeout=90000)
            check("filling answers every paper with a page link", page.locator("table.mx td:has-text('Tenure moderates the effect.')").count() == 3, [json.dumps(json.load(open(os.path.join(TMP, "Papers", "_folders", "lab.matrix.json")))["cells"])[:500], page.evaluate("[...document.querySelectorAll('.toast, #toast, [class*=toast]')].map(e => e.innerText).join('|')")])
            mx = json.load(open(os.path.join(TMP, "Papers", "_folders", "lab.matrix.json")))
            check("the matrix is saved beside the folder", len(mx["custom"]) == 1 and sum(len(v) for v in mx["cells"].values()) == 3 and all(c["cites"] for v in mx["cells"].values() for c in v.values()))
            page.wait_for_selector('[data-k="fill"]', state="detached")      # the fill ends with one more redraw; a cell opened for editing before it would be thrown away
            page.locator('table.mx td .mx-edit >> nth=0').click(); page.fill("td textarea.cd-ed, textarea.cd-ed", "Tenure, from my own reading"); page.keyboard.press("Control+Enter"); page.wait_for_selector('td:has-text("from my own reading")')
            check("writing in a cell keeps your words and marks them", any(c.get("edited") for v in json.load(open(os.path.join(TMP, "Papers", "_folders", "lab.matrix.json")))["cells"].values() for c in v.values()))
            page.click('[data-k="cols"]'); page.locator('#mx-colmenu label:has-text("Research question") input').uncheck(); page.wait_for_function("document.querySelectorAll('table.mx thead th[data-col]').length === 5")
            check("columns can be switched off from the Columns menu", "Research question" not in page.inner_text("table.mx thead"))
            page.click('[data-k="cols"]') if not page.locator("#mx-colmenu:not(.hide)").count() else None
            page.click('[data-k="preset"]'); page.wait_for_selector('table.mx thead th:has-text("Identification strategy")')
            check("the Methods & measures set adds four ready-made columns of your own (and a second press adds nothing twice)", all(n in page.inner_text("table.mx thead") for n in ("Constructs & measures", "Data source & period", "Identification strategy", "Robustness checks")) and len(api("/api/folder/matrix?id=lab")["doc"]["custom"]) == 5)
            page.click('[data-k="cols"]'); page.click('[data-k="preset"]'); page.wait_for_function("document.querySelector('#toast') && /already there/.test(document.querySelector('#toast').textContent)")
            mdoc = api("/api/folder/matrix?id=lab")["doc"]; keep = [c for c in mdoc["custom"] if c["name"] == "Moderators"]; drop_ = {c["id"] for c in mdoc["custom"] if c["name"] != "Moderators"}
            api("/api/folder/matrix", {"id": "lab", "rev": mdoc["rev"], "doc": {"custom": keep, "columns": [c for c in mdoc["columns"] if c not in drop_]}}); page.reload(); page.wait_for_selector("#libitem"); page.click("#libitem"); page.wait_for_selector("#lf-q"); page.click('.fchip[data-openfolder="lab"]'); page.wait_for_selector(".fvtabs"); page.click('.fvtabs [data-ft="matrix"]'); page.wait_for_selector("table.mx")
            shot(page, "12-matrix")
            csv_ = urllib.request.urlopen(base + "/api/export/matrix?folder=lab&fmt=csv").read().decode("utf-8-sig")
            check("export: CSV with the paper columns first", csv_.startswith("Paper,Title,Year,Journal,DOI,") and "Moderators" in csv_.splitlines()[0] and "(p. " in csv_)
            check("export: LaTeX table and Word table", "\\begin{tabularx}" in urllib.request.urlopen(base + "/api/export/matrix?folder=lab&fmt=tex").read().decode() and urllib.request.urlopen(base + "/api/export/matrix?folder=lab&fmt=docx").read()[:2] == b"PK")

            page.goto(base + "/"); page.click("#libitem"); page.wait_for_selector("#lf-q"); page.click('.libtabs [data-tab="ask"]'); page.wait_for_selector("#lib-ask #am-q")
            page.wait_for_selector("#lib-ask #am-info:has-text('papers')")
            check("the Library has an Ask tab over all papers, with filters", "3 papers" in page.inner_text("#lib-ask #am-info") and page.locator('#lib-ask [data-s="journal"]').count() == 1)
            page.select_option('#lib-ask [data-s="folder"]', "gov"); page.wait_for_function("document.querySelector('#lib-ask #am-info').innerText.startsWith('1 paper')")
            check("narrowing the scope to a folder changes the count", "1 paper" in page.inner_text("#lib-ask #am-info"))
            page.select_option('#lib-ask [data-s="folder"]', ""); page.fill("#lib-ask #am-q", "Compare the papers."); page.click('#lib-ask [data-k="send"]'); page.wait_for_selector("#lib-ask .ask-a .cite", timeout=90000)
            check("a library-wide answer is saved in Papers/_ask_library.json", os.path.exists(os.path.join(TMP, "Papers", "_ask_library.json")))


            print("round 3: claim check")
            page.goto("about:blank"); page.goto(base + "/#/folder/lab/claims"); page.wait_for_selector("#cl-text")
            check("the folder has a Claim check tab, with a button that takes the text of the related-work draft", page.locator('.fvtabs [data-ft="claims"]').count() == 1 and page.locator('[data-cl="draft"]').count() == 1)
            page.click('[data-cl="draft"]'); page.wait_for_selector(".cl-s")
            check("the draft's text is split into sentences you can tick", "Tian & Xue (2026)" in page.input_value("#cl-text") and page.locator(".cl-s").count() >= 3)
            page.fill("#cl-text", "Platforms that publish clear rules attract more complementors, SUPPORTME as several studies report across markets.\n"
                      "Disclosure of AI use always increases backer funding in every context, CONTRADICTME according to my reading of the field.\n"
                      "Tian & Xue (2026) show that open governance WRONGPAPER lowers entry costs for many small developers.\n"
                      "This sentence has no evidence behind it but the model claims support anyway, NOEVIDENCE for the test.\n"
                      "We study how transparency rules change who joins a platform in this paper for the test.\n"
                      "Another claim about platforms with a bad passage reference BADID that the model half supports in the test.")
            page.wait_for_function("document.querySelectorAll('.cl-s').length === 6", timeout=10000)
            check("sentences about your own study are not ticked; the one that names a paper in your library says so", page.locator(".cl-s input:checked").count() == 5 and "about your study" in page.locator(".cl-s.off").inner_text() and "names" in page.locator(".cl-s >> nth=2").inner_text())
            page.wait_for_function("document.querySelector('#cl-gob') && /Check 5 claims/.test(document.querySelector('#cl-gob').textContent)", timeout=10000)
            check("the button says how many claims it will check, and the cost", "·" in page.inner_text("#cl-gob") and "Check 5 claims" in page.inner_text("#cl-gob"), page.inner_text("#cl-gob"))
            page.click('[data-cl="go"]'); page.wait_for_selector('.of[data-tool="claims"]', timeout=60000)
            req = [b for p_, b in SEEN if b.get("messages") and str(b["messages"][0].get("content", "")).startswith("You check claims")][-1]
            user = req["messages"][-1]["content"]
            check("the model got each ticked claim with its own numbered passages, and not the sentence about your own study", "### Claim 5" in user and "C1.1 (" in user and "We study how transparency" not in user and "(cited: " in user, user[:400])
            secs = [t.strip().lower() for t in page.locator(".of[data-tool=claims] .of-sec h4").all_inner_texts()]
            check("the claims are sorted into needs attention, supported, not found", secs == ["needs your attention", "supported by your papers", "not found in your papers"], str(secs))
            check("supported, contradicted and partly-supported claims carry the passage they rest on; a verdict with no passage becomes 'not found'",
                  page.locator(".cl-row.v-supported").count() == 2 and page.locator(".cl-row.v-contradicted").count() == 1 and page.locator(".cl-row.v-partly").count() == 1 and page.locator(".cl-row.v-not_found").count() == 1
                  and page.locator(".cl-row.v-contradicted .cl-e.contradicts").count() == 1 and "not counted as supported" in page.locator(".cl-row.v-not_found").inner_text())
            check("a passage the model was never given is dropped, and the page says so", "had not been given" in page.inner_text(".of[data-tool=claims] .of-notices") if page.locator(".of-notices").count() else "had not been given" in page.inner_text(".of[data-tool=claims]"))
            check("a claim that names a paper warns when that paper's own passages do not say it", "do not clearly say this" in page.locator('.cl-row[data-n="3"]').inner_text() and page.locator('.of-sec[data-sec="attn"] .cl-row[data-n="3"]').count() == 1)
            page.locator(".cl-row.v-contradicted .cl-e").first.click(); page.wait_for_selector(".rd-band .textLayer span", timeout=30000)
            check("a passage opens its paper in the reader", page.locator("#rd-num").count() == 1)
            page.click('#reader [data-a="back"]'); page.wait_for_function("!document.body.classList.contains('reading')")
            page.check('[data-hd="2"]'); page.wait_for_timeout(500)
            doc_ = json.load(open(os.path.join(TMP, "Papers", "_folders", "lab.claims.json")))
            check("ticking a claim off as dealt with is saved", any(c["n"] == 2 and c.get("handled") for c in doc_["claims"]) and len(doc_["claims"]) == 5)
            md_ = urllib.request.urlopen(base + "/api/export/claims?folder=lab&fmt=md").read().decode()
            check("the result exports as Markdown (by verdict, with the passages) and as Word", "## Contradicted (1)" in md_ and "## Supported (2)" in md_ and "We study how transparency" not in md_ and urllib.request.urlopen(base + "/api/export/claims?folder=lab&fmt=docx").read()[:2] == b"PK")
            page.wait_for_function("document.querySelector('#cl-rvest') && document.querySelector('#cl-rvest').textContent.includes('$') || document.querySelector('#cl-rvest').textContent.includes('¢')", timeout=10000)
            page.click("#cl-rvb"); page.wait_for_selector("#cl-rev .of", timeout=60000)
            reqr = [b for p_, b in SEEN if b.get("messages") and str(b["messages"][0].get("content", "")).startswith('You are "Reviewer 2"')][-1]
            check("Reviewer 2 gets your whole text and the folder's paper notes (with ids) as context", "Platforms that publish clear rules" in reqr["messages"][-1]["content"] and "We study how transparency rules" in reqr["messages"][-1]["content"] and "P1-1 [question]" in reqr["messages"][-1]["content"])
            secs_ = [t.strip().lower() for t in page.locator("#cl-rev .of-sec h4").all_inner_texts()]
            check("the review is sorted into verdict, major and minor concerns, claims that need evidence, your own papers, and questions", secs_ == ["verdict", "major concerns", "minor concerns", "claims that need evidence", "your own papers bear on this", "questions the reviewer would ask"], str(secs_))
            check("it ends in a recommendation", "major revision" in page.inner_text("#cl-rev .rv2-rec").lower())
            check("a quotation that is not in your text and a paper it was not given are left out, and the page says so", page.locator("#cl-rev .rv2-i blockquote").count() == 1 and page.locator('#cl-rev .of-sec[data-sec="lit"] .rv2-i').count() == 1 and "left out" in page.inner_text("#cl-rev .of-notices") if page.locator("#cl-rev .of-notices").count() else "left out" in page.inner_text("#cl-rev"))
            page.locator("#cl-rev [data-rvcite]").first.click(); page.wait_for_selector(".rd-band .textLayer span", timeout=30000)
            check("a paper it points to opens in the reader at the note's page", page.locator("#rd-num").count() == 1)
            page.click('#reader [data-a="back"]'); page.wait_for_function("!document.body.classList.contains('reading')")
            check("the review exports as Markdown", "# Reviewer 2 — Lab" in urllib.request.urlopen(base + "/api/export/review?folder=lab").read().decode() and os.path.exists(os.path.join(TMP, "Papers", "_folders", "lab.review.json")))
            page.reload(); page.wait_for_selector("#libitem"); page.click("#libitem"); page.wait_for_selector("#lf-q"); page.click('.fchip[data-openfolder="lab"]'); page.wait_for_selector(".fvtabs")
            page.click('.fvtabs [data-ft="claims"]'); page.wait_for_selector(".cl-row")
            try:
                page.wait_for_function("document.querySelectorAll('.cl-s input').length === 5", timeout=10000)      # the sentence list comes from a second request (the estimate)
            except Exception:  # noqa: BLE001
                pass
            check("after a reload the review is still there", page.locator("#cl-rev .rv2-i").count() >= 3)
            check("after a reload the result and your text are as you left them", page.locator(".cl-row").count() == 5 and "SUPPORTME" in page.input_value("#cl-text") and page.locator(".cl-row.done").count() == 1 and page.locator(".cl-s input:checked").count() == 5,
                  page.evaluate("[document.querySelectorAll('.cl-row').length, document.querySelectorAll('.cl-row.done').length, document.querySelectorAll('.cl-s input:checked').length, document.querySelector('#cl-text').value.slice(0, 40)]"))
            check("it ran as a job in the activity list", any(j["kind"] == "claims" and j["status"] == "done" for j in api("/api/activity")["jobs"]))

            print("round 3: synthesis")
            page.goto("about:blank"); page.goto(base + "/#/folder/lab/synthesis"); page.wait_for_selector('[data-sy="go"]', timeout=15000)
            check("the folder has a Synthesis tab that lists its papers (all with cards ticked)", page.locator('.fvtabs [data-ft="synthesis"]').count() == 1 and page.locator("input[data-pf]:checked").count() == 3)
            page.fill("#sy-focus", "how disclosure rules change trust and entry"); page.wait_for_function("document.querySelector('#sy-est').textContent.includes('·')", timeout=10000)
            page.click('[data-sy="go"]'); page.wait_for_selector('.of[data-tool="synthesis"]', timeout=60000)
            req = [b for p_, b in SEEN if b.get("messages") and str(b["messages"][0].get("content", "")).startswith("You synthesise a set")][-1]
            user = req["messages"][-1]["content"]
            check("the model got every card as numbered notes, and what you asked it to organise the themes around", "NOTES ON THE 3 PAPERS" in user and "P1-1 [question]" in user and "P3-" in user and "disclosure rules" in user, user[:300])
            titles = [t.strip().lower() for t in page.locator(".of[data-tool=synthesis] .of-sec h4").all_inner_texts()]
            check("the result has an overview, the map, what is still open and a check", titles == ["overview", "map of the papers", "what is still open", "check"], str(titles))
            check("the map has a row per theme and a cell per paper that has a position; silent papers get a dot", page.locator(".sy-map tbody tr").count() == 2 and page.locator(".sy-cell").count() == 4 and page.locator(".sy-no").count() == 2, str(page.locator(".sy-cell").count()))
            check("each theme says whether the papers agree, and where they differ", "They agree" in page.inner_text(".sy-map tbody tr >> nth=0") and "Mixed" in page.inner_text(".sy-map tbody tr >> nth=1") and "platforms differ" in page.inner_text(".sy-map tbody tr >> nth=1"))
            check("a position or theme with no real note behind it is removed, and a gap with no note is marked as not grounded", page.locator(".sy-map").inner_text().count("Nothing is behind this") == 0 and "Ghost theme" not in page.inner_text(".sy-map") and page.locator(".sy-gap").count() == 2 and page.locator(".sy-gap.ung").count() == 1 and "not grounded" in page.locator(".sy-gap.ung").inner_text())
            check("the page says how many notes the model pointed at that it was never given", "3 notes it had not been given" in page.inner_text('.of-sec[data-sec="check"]') or "3 notes it had not been given" in page.inner_text(".of[data-tool=synthesis]"))
            page.click('[data-sy="listen"]'); page.wait_for_selector(".lsn-s", timeout=30000)
            reqf = [b for p_, b in SEEN if b.get("messages") and str(b["messages"][0].get("content", "")).startswith("You write the script of a short spoken briefing")][-1]
            check("🎧 Listen on a folder's Synthesis writes the script from the themes and gaps, not from the raw notes", "FOLDER:" in reqf["messages"][-1]["content"] and "Transparency and entry" in reqf["messages"][-1]["content"] and "SET of academic papers" in reqf["messages"][0]["content"])
            page.click('[data-l="close"]')
            page.locator(".sy-cell >> nth=0").click(); page.wait_for_selector(".rd-band .textLayer span", timeout=30000)
            check("a cell opens its paper in the reader", page.locator("#rd-num").count() == 1)
            page.click('#reader [data-a="back"]'); page.wait_for_function("!document.body.classList.contains('reading')")
            md_ = urllib.request.urlopen(base + "/api/export/synthesis?folder=lab&fmt=md").read().decode()
            check("it exports as Markdown (themes with their papers' positions, gaps) and as Word", "### Transparency and entry (agree)" in md_ and "## Gaps" in md_ and "Nobody studies small platforms" in md_ and urllib.request.urlopen(base + "/api/export/synthesis?folder=lab&fmt=docx").read()[:2] == b"PK")
            page.reload(); page.wait_for_selector("#libitem"); page.click("#libitem"); page.wait_for_selector("#lf-q"); page.click('.fchip[data-openfolder="lab"]'); page.wait_for_selector(".fvtabs")
            page.click('.fvtabs [data-ft="synthesis"]'); page.wait_for_selector(".sy-map")
            check("after a reload the synthesis and your focus are as you left them", page.locator(".sy-cell").count() == 4 and "disclosure rules" in page.input_value("#sy-focus"))
            check("it ran as a job in the activity list", any(j["kind"] == "synthesis" and j["status"] == "done" for j in api("/api/activity")["jobs"]))

            print("round 3: flashcards and the daily review")
            page.goto("about:blank"); page.goto(base + "/#/library/review"); page.wait_for_selector(".rv-head", timeout=15000)
            check("the Library has a Review tab; with no cards yet it offers to make some", page.locator('.libtabs [data-tab="review"]').count() == 1 and "No flashcards yet" in page.inner_text("#rv-body"))
            for _ in range(4):      # the Library draws the Review tab again when it has finished loading, and that puts the view back to Study (also between two of these steps): so the whole of "Make cards", "all without cards yet" and its result is one bounded attempt, repeated as a person would
                try:
                    page.click('[data-rv="view"][data-v="make"]'); page.click('[data-rv="selall"]', timeout=3000); page.wait_for_selector("[data-rvp]:checked", timeout=3000)
                    break
                except Exception:  # noqa: BLE001
                    pass
            n_sel = page.locator("[data-rvp]:checked").count()
            check("the papers that have a card can be ticked (only those)", n_sel >= 3 and page.locator("[data-rvp]").count() == n_sel, str(n_sel))
            page.wait_for_function("document.querySelector('#rv-est') && document.querySelector('#rv-est').textContent.includes('·')", timeout=10000)
            page.click('[data-rv="go"]'); page.wait_for_selector(".rv-card", timeout=60000)
            req = [b for p_, b in SEEN if b.get("messages") and str(b["messages"][0].get("content", "")).startswith("You write flashcards")][-1]
            user = req["messages"][-1]["content"]
            check("the model got the notes with ids and the number of cards per paper", "CARDS TO WRITE: 8 for each paper" in user and "P1-1 [question]" in user, user[:200])
            fcs = json.load(open(os.path.join(TMP, "Papers", "_flashcards.json")))
            check("cards were made from notes it was given: a card with no real note, and a repeat, were not kept; each remembers its page", [c["q"] for c in fcs["cards"]] == ["What does transparency mean in this paper?", "Which theory does the paper build on?", "What is the sample of the study?"]
                  and all(c["file"] and c["source"] == "card" and c["reps"] == 0 for c in fcs["cards"]) and any(c["page"] for c in fcs["cards"]), str([c["q"] for c in fcs["cards"]]))
            check("the Review page counts what is due today and the tab shows it", "3 cards for today" in page.inner_text(".rv-head") and page.locator('.libtabs [data-tab="review"] .nn').inner_text().strip() == "3")
            q1 = page.inner_text(".rv-q"); page.keyboard.press("Space"); page.wait_for_selector(".rv-a")
            check("space shows the answer and the four answers say when the card would come back", q1 and page.locator(".rv-r").count() == 4 and "again today" in page.inner_text(".rv-r.again") and "1 day" in page.inner_text(".rv-r.good") and "4 days" in page.inner_text(".rv-r.easy"))
            page.keyboard.press("3"); page.wait_for_function("document.querySelector('.rv-q').textContent !== %s" % json.dumps(q1))
            fcs = json.load(open(os.path.join(TMP, "Papers", "_flashcards.json"))); c1 = next(c for c in fcs["cards"] if c["q"] == q1)
            check("Good schedules the card for tomorrow and the file keeps it", c1["interval"] == 1 and c1["reps"] == 1 and c1["due"] > c1["introduced"] and c1["introduced"], str(c1))
            q2 = page.inner_text(".rv-q"); page.keyboard.press("Enter"); page.keyboard.press("1"); page.wait_for_function("document.querySelector('.rv-q') && document.querySelector('.rv-q').textContent !== %s" % json.dumps(q2))
            check("Again brings the card back later in the same session", "repeated" in page.inner_text(".rv-prog + .pmeta") and next(c for c in json.load(open(os.path.join(TMP, "Papers", "_flashcards.json")))["cards"] if c["q"] == q2)["lapses"] == 1)
            for _ in range(3):
                if page.locator(".rv-done").count():
                    break
                page.keyboard.press("Space"); page.keyboard.press("3"); page.wait_for_timeout(500)
            page.wait_for_selector(".rv-done", timeout=10000)
            check("when the queue is empty it says so", "all for today" in page.inner_text(".rv-done"))
            page.click('[data-rv="view"][data-v="browse"]'); page.wait_for_selector(".rv-item")
            check("Browse lists every card with the paper it came from", page.locator(".rv-item").count() == 3 and "p. " in page.inner_text(".rv-item >> nth=0"))
            page.fill("#rv-q", "sample"); page.wait_for_function("document.querySelectorAll('.rv-item').length === 1")
            page.click('.rv-item [data-rv="edit"]'); page.fill("#rv-eq", "How many firms are in the sample?"); page.click('[data-rv="saveedit"]'); page.wait_for_selector(".rv-item .rv-iq")
            check("a card can be edited; the change is saved", "How many firms" in page.inner_text(".rv-iq") and any(c["q"] == "How many firms are in the sample?" for c in json.load(open(os.path.join(TMP, "Papers", "_flashcards.json")))["cards"]))
            page.click('.rv-item [data-rv="susp"]'); page.wait_for_function("document.querySelectorAll('.rv-item').length === 0")
            check("a suspended card disappears from review and from the count", json.load(open(os.path.join(TMP, "Papers", "_flashcards.json")))["cards"][2]["suspended"] is True and "1 suspended" in page.inner_text(".rv-stats"))
            page.fill("#rv-q", ""); page.click('[data-rv="view"][data-v="add"]'); page.fill("#rv-nq", "What is a complementor?"); page.fill("#rv-na", "A firm that adds products to a platform."); page.click('[data-rv="addcard"]'); page.wait_for_selector(".rv-item")
            check("you can add your own card", any(c["source"] == "manual" and c["q"] == "What is a complementor?" for c in json.load(open(os.path.join(TMP, "Papers", "_flashcards.json")))["cards"]))
            page.locator('.rv-item:has-text("complementor") [data-rv="del"]').click(); page.wait_for_function("document.querySelectorAll('.rv-item').length === 2")
            check("a card can be deleted", all(c["q"] != "What is a complementor?" for c in json.load(open(os.path.join(TMP, "Papers", "_flashcards.json")))["cards"]))
            page.locator('.rv-item [data-rv="open"] >> nth=0').click(); page.wait_for_selector(".rd-band .textLayer span", timeout=30000)
            check("the source link opens the paper in the reader", page.locator("#rd-num").count() == 1)
            page.click('#reader [data-a="back"]'); page.wait_for_function("!document.body.classList.contains('reading')")
            check("it ran as a job in the activity list", any(j["kind"] == "flashcards" and j["status"] == "done" for j in api("/api/activity")["jobs"]))

            print("round 3: folder workspace")
            page.goto("about:blank"); page.goto(base + "/#/folder/lab/workspace"); page.wait_for_selector(".ws-list", timeout=15000)
            check("the folder has a Workspace tab with a tray of what its papers hold and an empty board", page.locator('.fvtabs [data-ft="workspace"]').count() == 1 and page.locator("#ws-hint").is_visible() and page.locator(".ws-card").count() == 0)
            page.click('[data-wt="ln"]'); page.wait_for_selector(".ws-it")
            n_items = page.locator(".ws-it").count()
            check("the tray lists the lines of each paper card, grouped by paper, and can be searched", n_items >= 6 and page.locator(".ws-pt").count() == 3)
            page.fill("#ws-q", "sample"); page.wait_for_function("document.querySelectorAll('.ws-it').length < %d" % n_items); page.fill("#ws-q", ""); page.wait_for_function("document.querySelectorAll('.ws-it').length === %d" % n_items)
            page.locator(".ws-it >> nth=0").click(); page.wait_for_selector(".ws-card.excerpt")
            page.locator(".ws-it >> nth=0").click(); page.wait_for_timeout(300)
            check("clicking an item puts it on the board as an excerpt with its paper and page; the same item is not added twice", page.locator(".ws-card.excerpt").count() == 1 and "p. " in page.inner_text(".ws-card.excerpt .nc-head") and "already on the board" in page.inner_text("#toast"))
            page.click('[data-wt="pp"]'); page.locator(".ws-it >> nth=0").click(); page.wait_for_function("document.querySelectorAll('.ws-card').length === 2")
            page.click('[data-w="note"]'); page.wait_for_selector(".ws-card.note textarea.nc-main:not(.hide)"); page.fill(".ws-card.note textarea.nc-main", "**Synthesis idea**: compare the two."); page.click("#ws-q"); page.wait_for_selector(".ws-card.note .nc-md strong")
            check("the board takes papers and your own Markdown notes too", page.locator(".ws-card").count() == 3 and page.locator(".ws-card.note .nc-md strong").count() == 1)
            ex = page.locator(".ws-card.excerpt >> nth=0"); nt = page.locator(".ws-card.note")
            page.click(".ws-card.excerpt >> nth=0 >> [data-w=conn]"); page.click(".ws-card.note .nc-head"); page.wait_for_selector(".bd-l", state="attached")
            page.click('[data-w="frame"]'); page.wait_for_selector(".bd-frame"); page.fill(".bd-fl", "Compare"); page.keyboard.press("Tab")
            check("cards can be joined by arrows and grouped in a named frame", page.locator(".bd-l").count() == 1 and page.input_value(".bd-fl") == "Compare")
            page.click('[data-w="zin"]'); check("the board zooms", page.inner_text("#ws-zv") == "125%"); page.click('[data-w="zout"]')
            for _ in range(40):
                if os.path.exists(os.path.join(TMP, "Papers", "_folders", "lab.workspace.json")):
                    break
                page.wait_for_timeout(250)
            page.wait_for_timeout(800)
            wsd = json.load(open(os.path.join(TMP, "Papers", "_folders", "lab.workspace.json")))
            check("it is saved in Papers/_folders/lab.workspace.json (cards with their paper and page, frames, arrows)", len(wsd["cards"]) == 3 and wsd["frames"][0]["label"] == "Compare" and len(wsd["links"]) == 1 and any(c.get("file") and c.get("page") for c in wsd["cards"]) and wsd["rev"] >= 1, json.dumps(wsd)[:300])
            page.click('.ws-card.excerpt >> nth=0 >> [data-w=open]'); page.wait_for_selector(".rd-band .textLayer span", timeout=30000)
            check("an excerpt's header opens its paper in the reader", page.locator("#rd-num").count() == 1)
            page.click('#reader [data-a="back"]'); page.wait_for_function("!document.body.classList.contains('reading')")
            page.click('[data-w="view"][data-v="outline"]'); page.wait_for_selector(".ol-g")
            check("the outline shows the frame first, then your notes, then papers", [t.strip().lower() for t in page.locator(".ol-g h4").all_inner_texts()][0].startswith("compare") or page.locator(".ol-g").count() >= 2, str(page.locator(".ol-g h4").all_inner_texts()))
            page.click('[data-w="view"][data-v="board"]')
            md_ = urllib.request.urlopen(base + "/api/export/workspace?folder=lab&fmt=md").read().decode()
            check("it exports as Markdown with the cites, and as Word", "# Workspace — Lab" in md_ and "Synthesis idea" in md_ and re.search(r"\(.*, p\. \d+\)|\(.*\)", md_) is not None and urllib.request.urlopen(base + "/api/export/workspace?folder=lab&fmt=docx").read()[:2] == b"PK", md_[:300])
            page.click('.ws-card.note [data-w=del]'); page.wait_for_function("document.querySelectorAll('.ws-card').length === 2")
            page.click('[data-w="undo"]'); page.wait_for_function("document.querySelectorAll('.ws-card').length === 3")
            check("undo puts a removed card back", page.locator(".ws-card.note").count() == 1)
            page.wait_for_timeout(1500); page.reload(); page.wait_for_selector("#libitem"); page.click("#libitem"); page.wait_for_selector("#lf-q"); page.click('.fchip[data-openfolder="lab"]'); page.wait_for_selector(".fvtabs"); page.click('.fvtabs [data-ft="workspace"]'); page.wait_for_selector(".ws-card")
            check("after a reload the board is as you left it", page.locator(".ws-card").count() == 3 and page.locator(".bd-l").count() == 1 and page.locator(".bd-frame").count() == 1)

            print("round 3: import a bibliography; LaTeX keys; a folder as a directory")
            page.goto("about:blank"); page.goto(base + "/#/library"); page.wait_for_selector("#libitem")
            page.evaluate("window.LibraryUI.addDialog()"); page.wait_for_selector("#ad-bib"); page.click("#ad-bib"); page.wait_for_selector("#bibdlg #bd-file", state="attached")
            bib = ("@article{one2026,\n  author = {Singhal, S. and M{\\\"u}ller, J.},\n  title = {%s},\n  journal = {MIS Quarterly}, year = 2026\n}\n"
                   "@article{two2025,\n  author = {Lee, H.},\n  title = {Another paper that is not here},\n  journal = {ISR}, year = 2025, doi = {10.9999/not-here}\n}\n"
                   "@book{three2020, author = {Xue, K.}, title = {A book without a DOI}, year = 2020}\n" % rows[0][0])
            page.set_input_files("#bd-file", files=[{"name": "my-refs.bib", "mimeType": "text/plain", "buffer": bib.encode()}]); page.wait_for_selector(".bd-sum")
            check("a .bib file is read: how many references, how many you have, how many have no DOI", "3 references" in page.inner_text(".bd-sum") and "1 already in your library" in page.inner_text(".bd-sum") and "1 without a DOI" in page.inner_text(".bd-sum"), page.inner_text(".bd-sum"))
            check("the ones you don't have are ticked, the one you have cannot be fetched again", page.locator(".bd-r input:checked").count() == 2 and page.locator(".bd-r.have input:disabled").count() == 0 and "Fetch 2 papers" in page.inner_text('[data-bd="go"]'))
            page.uncheck("#bd-only"); page.wait_for_selector(".bd-r.have")
            check("…and with the filter off the one you have is listed, marked 'in your library' and not tickable", page.locator(".bd-r.have input:disabled").count() == 1 and "in your library" in page.inner_text(".bd-r.have"))
            sent = []
            page.route("**/api/related/download", lambda route: (sent.append(json.loads(route.request.post_data)), route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "n": 2, "have": [], "run": {"id": "x", "kind": "picks", "papers": []}}))))
            page.click('[data-bd="go"]'); page.wait_for_function("document.querySelector('#toast') && /Fetching 2 papers/.test(document.querySelector('#toast').textContent)", timeout=8000)
            check("Fetch sends the ticked references (title, authors, DOI, journal) to the download routes", len(sent) == 1 and [i["doi"] for i in sent[0]["items"]] == ["10.9999/not-here", ""] and sent[0]["items"][0]["venue"] == "ISR" and sent[0]["items"][0]["authors"] == ["H. Lee"] and sent[0]["label"].startswith("Imported: my-refs"), json.dumps(sent)[:300])
            page.unroute("**/api/related/download")
            keys_ = urllib.request.urlopen(base + "/api/export/cites?folder=lab").read().decode().strip()
            check("the LaTeX export gives one \\citep{…} with the keys of the .bib file", keys_.startswith("\\citep{") and keys_.endswith("}") and keys_.count(",") == 2, keys_)
            exp_dir = os.path.join(TMP, "exported"); api("/api/settings", {"prefs": {"export_dir": exp_dir}})
            r_ = api("/api/export/folderdir", {"folder": "lab"})
            lab_dir = os.path.join(exp_dir, "Lab")
            check("a folder can be written out as a real directory: its PDFs, references.bib and notes.md in your export folder", r_["pdfs"] == 3 and len([f for f in os.listdir(lab_dir) if f.endswith(".pdf")]) == 3 and os.path.exists(os.path.join(lab_dir, "references.bib")) and os.path.exists(os.path.join(lab_dir, "notes.md")), str(r_))
            check("doing it again copies nothing twice", api("/api/export/folderdir", {"folder": "lab"})["pdfs"] == 0)
            api("/api/settings", {"prefs": {"export_dir": ""}})

            print("round 2c: discover related and missing papers")
            page.goto(base + "/"); page.click("#libitem"); page.wait_for_selector("#lf-q"); page.click('.fchip[data-openfolder="lab"]'); page.wait_for_selector(".fvtabs")
            page.click('.fvtabs [data-ft="discover"]'); page.wait_for_selector(".dc-row", timeout=60000)
            txt = page.inner_text("#dc-out")
            check("Discover lists what several papers cite that you don't have, with how many cite it", "The platform classic" in txt and "cited by 3 of your papers" in txt and "Governance of platform ecosystems" in txt and "cited by 1 of your papers" in txt, txt[:500])
            check("a folder with only three papers starts at 'cited by at least 1', so the page is not empty", page.eval_on_selector('[data-s="min"]', "e => e.value") == "1")
            check("…and what OpenAlex calls related (not the papers you have)", "Platform governance and AI" in txt and "related to 2 of your papers" in txt and "Open Platforms and Complementors" not in page.inner_text(".dc-row >> nth=0"))
            check("a paper without OpenAlex references used the DOIs in its own bibliography", "from the PDF" in txt)
            page.click('[data-via="W10"]'); page.wait_for_selector(".dc-vialist:not(.hide)")
            check("the list of citing papers can be opened", page.locator(".dc-vialist:not(.hide) [data-openpaper]").count() == 3)
            page.check('[data-sel="W10"]'); page.wait_for_selector(".dc-bar:not(.hide)")
            check("ticking a paper shows the Download bar", page.inner_text(".dc-bar .dc-n") == "1")
            page.click('.dc-bar [data-k="download"]'); page.wait_for_selector("#runtitle:has-text('Picks')", timeout=30000)
            for _ in range(40):
                if os.path.exists(marker):
                    break
                page.wait_for_timeout(250)
            check("one click creates a 'Picks' run and starts the download queue for it", os.path.exists(marker) and open(marker).read() == "doi:10.1234/abc" and "Picks · this folder" in page.inner_text("#runtitle"), open(marker).read() if os.path.exists(marker) else "no marker")
            page.wait_for_function("document.querySelector('#listview') && /Couldn’t download/.test(document.querySelector('#listview').innerText)", timeout=10000)      # (not just any card: the empty-state card "No papers in this run" also matched `.card`, and the check then read the list before the paper was in it)
            check("the Download queue shows it with its status", "The platform classic" in page.inner_text("#listview") and "Cited by 3 of your papers" in page.inner_text("#listview") and "Couldn’t download" in page.inner_text("#listview"), page.inner_text("#listview")[:300])
            runs = [f for f in os.listdir(os.path.join(TMP, ".agents", "skills", "ais-journal-watcher", "state", "runs")) if f.endswith("picks.json")]
            check("the run is saved like any other (state/runs)", len(runs) == 1)
            open(marker + ".wrong", "w").write("1")                                                   # the next round of the queue: the app refuses a different work's PDF
            page.click("#qbtn")
            try:
                page.wait_for_function("document.querySelector('#listview').innerText.includes('Wrong PDF refused')", timeout=15000)
            except Exception:  # noqa: BLE001
                pass
            card_ = page.locator("#listview .paper:has-text('The platform classic'), #listview .card:has-text('The platform classic')").first
            check("a refused wrong PDF shows as 'Wrong PDF refused' with the reason as its tooltip, keeps the paper in the queue (selectable) and offers the publisher's page in the browser",
                  "Wrong PDF refused" in page.inner_text("#listview") and "Two-stage differences in differences" in (card_.locator(".dl.bad").first.get_attribute("title") or "")
                  and card_.locator("input[type=checkbox]:not([disabled])").count() == 1 and card_.locator("a:has-text('Open in my browser')").count() == 1,
                  page.inner_text("#listview")[:300])
            os.remove(marker + ".wrong")
            # a site wants a human check during a download: the page offers "Open the check" (a reload keeps it), the press reaches the running download
            def post_code(path, body):
                try:
                    urllib.request.urlopen(urllib.request.Request(base + path, json.dumps(body).encode(), {"Content-Type": "application/json"})); return 200
                except urllib.error.HTTPError as e_:
                    return e_.code
            open(marker + ".check", "w").write("1")
            page.click("#qbtn")
            page.wait_for_selector("#banner:not(.hide) [data-check]", timeout=15000)
            check("a human check during a download shows 'Open the check' in the banner, and nothing opens by itself", page.inner_text("#banner [data-check]").startswith("Open the check") and "ScienceDirect" in page.inner_text("#banner") and not os.path.exists(marker + ".checked"), page.inner_text("#banner"))
            page.reload(); page.wait_for_selector("#banner:not(.hide) [data-check]", timeout=10000)
            check("…a reloaded page still offers it", page.locator('#banner [data-check="www.sciencedirect.com"]').count() == 1)
            check("…a press for a site with no check waiting is refused", post_code("/api/check", {"host": "elsewhere.example"}) == 400)
            page.click("#banner [data-check]")
            for _ in range(60):
                if os.path.exists(marker + ".checked"):
                    break
                page.wait_for_timeout(250)
            check("…pressing it asks the running download to open that site's check", os.path.exists(marker + ".checked") and open(marker + ".checked").read() == "www.sciencedirect.com", open(marker + ".checked").read() if os.path.exists(marker + ".checked") else "not asked")
            page.wait_for_function("document.querySelector('#banner').classList.contains('hide')", timeout=15000)
            page.wait_for_function("document.querySelector('#statustext').textContent === 'Idle'", timeout=15000)
            check("…and once the check is passed and the download is over, the banner is gone; with no download running a press is refused", post_code("/api/check", {"host": "www.sciencedirect.com"}) == 409)
            os.remove(marker + ".check"); os.remove(marker + ".checked")
            # author search: find the person, tick them, every work goes straight into a folder named after them (no confirmation, no screening)
            print("author search: an author's works into their folder")
            page.goto("about:blank"); page.goto(base + "/#/watch"); page.wait_for_selector('.tabs button[data-tab="authors"]', state="attached")
            page.click('.tabs button[data-tab="authors"]'); page.fill("#authname", "Ann Lee"); page.click("#authgo")
            page.wait_for_selector("#authlist .authc", timeout=10000)
            first_ = page.inner_text("#authlist .authc >> nth=0")
            check("the Authors tab finds the people of that name on OpenAlex, each with institution, works, topics and ORCID", page.locator("#authlist .authc").count() == 2 and "MIT" in first_ and "45 works" in first_
                  and "Platforms" in first_ and page.locator('#authlist a[href="https://orcid.org/0000-0002-1825-0097"]').count() == 1 and page.locator("#authdl").is_hidden(), first_)
            page.check("#authlist [data-ai='0']")
            check("…ticking one offers 'Download all into their folder'", page.locator("#authdl").is_visible() and page.inner_text("#authdl").strip() == "Download all into their folder", page.inner_text("#authdl"))
            if os.path.exists(marker): os.remove(marker)
            n_dlg = len(dialogs); n_asked = len(AUTHOR_ASKED)
            page.click("#authdl")
            for _ in range(80):
                if os.path.exists(marker): break
                page.wait_for_timeout(250)
            ids_ = open(marker).read().split(",") if os.path.exists(marker) else []
            fdir_ = os.path.join(TMP, "Papers", "_folders")
            fnames_ = [json.load(open(os.path.join(fdir_, f_)))["name"] for f_ in os.listdir(fdir_) if f_.endswith(".json") and "." not in f_[:-5]] if os.path.isdir(fdir_) else []
            check("…pressing it lists every work (45, more than the 40 a Discover download takes) and downloads them all, with no question asked, as a run 'Author · Ann Lee' with a folder 'Ann Lee'",
                  len(ids_) == 45 and all(i.startswith("doi:10.7777/au.") for i in ids_) and len(dialogs) == n_dlg and "Ann Lee" in fnames_
                  and any("author.id:A5000000111" in a_ for a_ in AUTHOR_ASKED[n_asked:]), "%d ids, dialogs %s, folders %s" % (len(ids_), dialogs[n_dlg:], fnames_))
            page.wait_for_function("document.querySelector('#runtitle') && document.querySelector('#runtitle').textContent.includes('Author · Ann Lee')", timeout=10000)
            check("…the run says whose works they are, which folder they go to, and what the listing found", "Works by Ann Lee" in page.inner_text("#runmeta") and "45 works listed" in page.inner_text("#runmeta")
                  and "45 to download" in page.inner_text("#runmeta"), page.inner_text("#runmeta"))
            arun_ = next(r_ for r_ in api("/api/runs") if r_["kind"] == "author")
            page.goto("about:blank"); page.goto(base + "/#/watch"); page.wait_for_selector("#weeklybtn", state="attached"); page.wait_for_timeout(300)
            page.evaluate("openRun({id: %s})" % json.dumps(arun_["id"])); page.wait_for_selector("#listview:not(.hide)", timeout=10000)
            check("…opened again from Past runs it shows its papers (not tiles of the watched journals) with each paper's own journal", page.locator("#tilesview").is_hidden()
                  and page.locator("#papers > .paper").count() > 0 and "Author Test Journal" in page.inner_text("#papers"), page.inner_text("#papers")[:200])
            page.wait_for_function("document.querySelector('#statustext').textContent === 'Idle'", timeout=20000)
            page.click('.tabs button[data-tab="authors"]'); page.fill("#authname", "Ann Lee"); page.click("#authgo"); page.wait_for_selector("#authlist .authc"); page.check("#authlist [data-ai='0']")
            os.remove(marker); page.click("#authdl")
            for _ in range(80):
                if os.path.exists(marker): break
                page.wait_for_timeout(250)
            fnames2_ = [json.load(open(os.path.join(fdir_, f_)))["name"] for f_ in os.listdir(fdir_) if f_.endswith(".json") and "." not in f_[:-5]]
            check("…searching the same person again fills the same folder (no second 'Ann Lee')", fnames2_.count("Ann Lee") == 1 and not any(n_.startswith("Ann Lee ·") for n_ in fnames2_), str(fnames2_))
            page.wait_for_function("document.querySelector('#statustext').textContent === 'Idle'", timeout=20000)
            page.click('.tabs button[data-tab="authors"]'); page.fill("#authname", "Ann Lee"); page.click("#authgo"); page.wait_for_selector("#authlist .authc"); page.check("#authlist [data-ai='1']")
            page.click("#authdl")
            page.wait_for_function("document.querySelector('#runmeta').textContent.includes('listing failed')", timeout=20000)
            check("…when OpenAlex does not answer for the works, the run says the listing failed and why (not just 'No papers in this run')",
                  "didn't answer" in page.inner_text("#runmeta") and "could not be listed" in page.inner_text("#papers"), page.inner_text("#runmeta"))
            page.wait_for_function("document.querySelector('#statustext').textContent === 'Idle'", timeout=20000)
            print("follow an author: all their works now, then the new ones with the weekly check")
            store_p = os.path.join(TMP, "Papers", "_authors.json")
            rdir_ = os.path.join(TMP, ".agents", "skills", "ais-journal-watcher", "state", "runs")
            st_dir = os.path.join(TMP, ".agents", "skills", "ais-journal-watcher", "state")
            saved_state = {n_: (open(os.path.join(st_dir, n_)).read() if os.path.exists(os.path.join(st_dir, n_)) else None) for n_ in ("seen.json", "judged.json", "new_papers.json")}
            runs_before = set(os.listdir(rdir_))
            ann = lambda: next(r_ for r_ in json.load(open(store_p))["authors"] if "A5000000111" in r_["ids"])      # noqa: E731
            if os.path.exists(marker): os.remove(marker)
            page.click('.tabs button[data-tab="authors"]'); page.fill("#authname", "Ann Lee"); page.click("#authgo"); page.wait_for_selector("#authlist .authc"); page.check("#authlist [data-ai='0']")
            check("ticking an author offers Follow beside Download all", page.is_visible("#authfollow") and "Follow" in page.inner_text("#authfollow"))
            page.click("#authfollow")
            for _ in range(80):
                if os.path.exists(marker): break
                page.wait_for_timeout(250)
            fids_ = open(marker).read().split(",") if os.path.exists(marker) else []
            check("…Follow downloads all their works now, as Download all does, and notes what was listed (only newer works are new)",
                  len(fids_) == 45 and ann().get("follow") is True and len(ann().get("follow_known") or []) == 45 and "followed" in page.inner_text("#runmeta"), "%d ids, %s" % (len(fids_), {k_: (v_ if not isinstance(v_, list) else len(v_)) for k_, v_ in ann().items()}))
            page.wait_for_function("document.querySelector('#statustext').textContent === 'Idle'", timeout=20000)
            ffid = ann()["folder"]
            page.click('.tabs button[data-tab="search"]'); page.click('.tabs button[data-tab="authors"]'); page.wait_for_selector('#authsaved [data-follow="%s"]' % ffid, timeout=10000)
            check("'Your authors' lists them, with Follow ticked", page.is_checked('#authsaved [data-follow="%s"]' % ffid) and "Ann Lee" in page.inner_text("#authsaved"), page.inner_text("#authsaved"))
            os.remove(marker)
            page.uncheck('#authsaved [data-follow="%s"]' % ffid)
            for _ in range(40):
                if ann().get("follow") is False: break
                page.wait_for_timeout(250)
            check("…unticking stops following: no job starts, and the folder stays", ann().get("follow") is False and api("/api/state")["busy"] is False and not os.path.exists(marker))
            page.check('#authsaved [data-follow="%s"]' % ffid)
            for _ in range(40):
                if ann().get("follow") is True: break
                page.wait_for_timeout(250)
            page.wait_for_timeout(500)
            check("…ticking it again follows again without downloading everything again", ann().get("follow") is True and api("/api/state")["busy"] is False and not os.path.exists(marker))
            AUTHOR_EXTRA[0] = 1                                                                            # OpenAlex now lists a 46th work
            open(marker + ".check", "w").write("1")                                                        # (and its download meets a human check)
            page.click('.tabs button[data-tab="weekly"]'); page.click("#weeklybtn")
            st_ = {}
            for _ in range(80):
                st_ = api("/api/state")
                if st_.get("checks"): break
                time.sleep(0.25)
            check("the weekly check downloads the followed author's new work as a download: its human check is offered with 'Open the check' (it does not open by itself)",
                  st_.get("busy") and st_.get("kind") == "download" and any(c_["host"] == "www.sciencedirect.com" for c_ in st_.get("checks") or []), str({k_: st_.get(k_) for k_ in ("busy", "kind", "checks")}))
            ok_chk = api("/api/check", {"host": "www.sciencedirect.com"}).get("ok")
            for _ in range(80):
                if os.path.exists(marker + ".checked"): break
                time.sleep(0.25)
            check("…'Open the check' reaches that download", ok_chk is True and os.path.exists(marker + ".checked") and open(marker + ".checked").read() == "www.sciencedirect.com")
            page.wait_for_function("document.querySelector('#statustext').textContent === 'Idle'", timeout=30000)
            wk_ = api("/api/runs/" + api("/api/state")["run"]["id"])
            fp_ = [p_ for p_ in wk_["papers"] if p_.get("openalex") == "W77045"]
            check("…only the new work (the 46th) was downloaded, into their folder, without screening, and the run says so",
                  open(marker).read() == "doi:10.7777/au.45" and wk_["kind"] == "weekly" and fp_ and fp_[0]["verdict"] == "download" and fp_[0]["folder"] == ffid and fp_[0]["reason"].startswith("followed author")
                  and "W77045" not in (ann().get("follow_known") or []), str((open(marker).read() if os.path.exists(marker) else "no marker", [(p_.get("id"), p_.get("verdict"), p_.get("reason")) for p_ in wk_["papers"]][:3])))
            check("…and the page knows its summary comes from the Library (the queue does not count it for a summary)", bool(fp_) and fp_[0].get("summary_by_hand") is True, str(fp_[:1])[:300])
            AUTHOR_EXTRA[0] = 0
            for x_ in (marker + ".check", marker + ".checked"):
                if os.path.exists(x_): os.remove(x_)
            for f_ in set(os.listdir(rdir_)) - runs_before:                                                # (this week's run and the state files back as they were, for the checks after this one)
                os.remove(os.path.join(rdir_, f_))
            for n_, txt_ in saved_state.items():
                if txt_ is None:
                    os.path.exists(os.path.join(st_dir, n_)) and os.remove(os.path.join(st_dir, n_))
                else:
                    open(os.path.join(st_dir, n_), "w").write(txt_)
            page.goto("about:blank"); page.goto(base + "/#/watch"); page.wait_for_selector("#weeklybtn", state="attached")
            for f_ in os.listdir(fdir_):                                                                   # (leave the folders and runs the later checks count as they were)
                if f_.endswith(".json") and "." not in f_[:-5] and json.load(open(os.path.join(fdir_, f_)))["name"].startswith("Ann Lee"):
                    os.remove(os.path.join(fdir_, f_))
            rdir_ = os.path.join(TMP, ".agents", "skills", "ais-journal-watcher", "state", "runs")
            for f_ in os.listdir(rdir_):
                if f_.endswith(".json") and json.load(open(os.path.join(rdir_, f_))).get("kind") == "author":
                    os.remove(os.path.join(rdir_, f_))
            check("…(the test's author runs are gone again)", not any(r_["kind"] == "author" for r_ in api("/api/runs")))
            if os.path.exists(os.path.join(TMP, "Papers", "_authors.json")):
                os.remove(os.path.join(TMP, "Papers", "_authors.json"))
            os.remove(marker)
            page.goto(base + "/"); page.click("#libitem"); page.wait_for_selector("#lf-q"); page.click('.fchip[data-openfolder="lab"]'); page.wait_for_selector(".fvtabs"); page.click('.fvtabs [data-ft="discover"]'); page.wait_for_selector(".dc-row", timeout=60000)
            page.select_option('[data-s="min"]', "1"); page.click("#dc-find"); page.wait_for_selector('[data-sel="W11"]', timeout=60000)
            check("'at least 1' also lists papers cited by only one of them", page.locator('[data-sel="W11"]').count() == 1)
            shot(page, "13-discover")
            page.click('[data-ign="W11"]'); page.wait_for_function("!document.querySelector('[data-sel=\"W11\"]')")
            check("'not interested' hides a paper and remembers it", "W11" in open(os.path.join(TMP, "Papers", "_related.json")).read())
            page.click('[data-k="unhide"]'); page.wait_for_timeout(1000); page.click("#dc-find"); page.wait_for_selector('[data-sel="W11"]', timeout=60000)
            check("hidden papers can be brought back", not os.path.exists(os.path.join(TMP, "Papers", "_related.json")))

            page.goto(base + "/"); page.click("#libitem"); page.wait_for_selector("#lf-q"); page.click('.libtabs [data-tab="discover"]'); page.wait_for_selector("#dc-find")
            check("the Library has a Discover tab (whole library, with filters; it waits for you to press Find)", page.locator("#lib-discover .dc-row").count() == 0 and page.locator('#lib-discover [data-s="journal"]').count() == 1)
            page.wait_for_function("document.querySelectorAll('#lf-folder option').length >= 2")      # (the Library finishes loading its folders and redraws the tab once: a click made before that is lost)
            for _try in range(4):
                page.click("#dc-find")
                try:
                    page.wait_for_function("document.querySelector('#dc-find').disabled || document.querySelector('#lib-discover .dc-row')", timeout=3000); break
                except Exception:  # noqa: BLE001      (no trace of the click: the tab was redrawn under it; press again)
                    continue
            page.wait_for_selector("#lib-discover .dc-row", timeout=60000)
            check("…and finds the same missing paper across the whole library", "The platform classic" in page.inner_text("#lib-discover #dc-out"))

            page.click('.libtabs [data-tab="papers"]'); page.click("#lf-list"); page.wait_for_selector("#lib-results .librow")
            page.locator('#lib-results .librow:has-text("Open Platforms") [data-read]').click(); page.wait_for_selector(".rd-band .textLayer span", timeout=30000)
            page.click('.rd-tabs button[data-tab="related"]'); page.wait_for_selector(".dc-paper .dc-row", timeout=60000)
            body = page.inner_text(".dc-paper")
            check("the reader's Related tab: cited-but-missing, related, and similar papers you have", "The platform classic" in body and "Platform governance and AI" in body and "Chatbots and Customer Trust" in body and "Governance of platform ecosystems" in body, body[:600])
            shot(page, "14-related-tab")
            page.click('#reader [data-a="back"]')


            print("round 2d: skim layer")
            page.goto(base + "/"); page.click("#libitem"); page.wait_for_selector("#lf-q"); page.click("#lf-list"); page.wait_for_selector("#lib-results .librow")
            page.locator('#lib-results .librow:has-text("Citations and Links") [data-read]').click(); page.wait_for_selector(".rd-band .textLayer span", timeout=30000)
            page.click("#rd-skimbtn"); page.wait_for_selector('#rd-skim [data-sk="make"]')
            check("the Skim button offers to skim the paper and says what it costs", "Skim this paper" in page.inner_text("#rd-skim") and "¢" in page.inner_text("#rd-skim"))
            page.click('#rd-skim [data-sk="make"]'); page.wait_for_selector("#rd-skim .sk-chip", timeout=60000); page.wait_for_selector(".rd-ov .skim-g", timeout=15000)
            sk_file = sidecar(2, ".skim.json")
            check("the skim is saved beside the PDF; a sentence the model invented was dropped", os.path.exists(sk_file) and len(json.load(open(sk_file))["items"]) == 3 and json.load(open(sk_file))["dropped"] == 1)
            chips = page.inner_text("#rd-skim .sk-lenses")
            check("one chip per lens with its count at the default density", all(l in chips for l in ("Research question", "Hypothesis", "Theory", "Sample & data", "Finding", "Limitation", "Contribution")) and "Research question 1" in chips.replace("\n", " ") and "Finding 0" in chips.replace("\n", " "))
            check("the key sentences are marked on the page", page.locator(".rd-ov .skim-g").count() >= 2 and "2 shown" in page.inner_text("#rd-skim .sk-sum"))
            page.eval_on_selector("#sk-d", "e => { e.value = 3; e.dispatchEvent(new Event('input', {bubbles: true})); }"); page.wait_for_function("document.querySelector('#rd-skim .sk-sum').innerText.startsWith('3 shown')")
            check("the density slider shows more sentences (and keeps its label)", "Everything" in page.inner_text(".sk-dens") and "Finding 1" in page.inner_text("#rd-skim .sk-lenses").replace("\n", " "))
            page.click('.sk-chip[data-lens="sample"]'); page.wait_for_function("document.querySelector('#rd-skim .sk-sum').innerText.startsWith('2 shown')")
            check("a lens can be switched off", "Sample & data 0" in page.inner_text("#rd-skim .sk-lenses").replace("\n", " "))
            page.click('.sk-chip[data-lens="sample"]'); page.wait_for_function("document.querySelector('#rd-skim .sk-sum').innerText.startsWith('3 shown')")
            box = page.locator('.rd-ov .skim-g.r1').first.bounding_box(); page.locator('.rd-ov .skim-g.r1').first.scroll_into_view_if_needed(); box = page.locator('.rd-ov .skim-g.r1').first.bounding_box()
            page.evaluate("document.querySelectorAll('#hv-ctx').forEach(e => e.remove())"); page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2); page.wait_for_selector(".rd-skimpop:not(.hide)")
            check("clicking a marked sentence offers to highlight or dismiss it", "Highlight" in page.inner_text(".rd-skimpop") and "Not useful" in page.inner_text(".rd-skimpop") and "key point" in page.inner_text(".rd-skimpop"))
            page.evaluate("document.querySelectorAll('#hv-ctx').forEach(e => e.remove())")          # (the 'Explain it' hover note from the Table 1 check may still be over the page: a click lands on it, not on the button)
            page.click('.rd-skimpop [data-p="yes"]')
            for _ in range(40):
                if os.path.exists(sidecar(2, ".notes.json")) and any(h.get("skim") for h in json.load(open(sidecar(2, ".notes.json")))["highlights"]):
                    break
                page.wait_for_timeout(200)
            hs = [h for h in json.load(open(sidecar(2, ".notes.json")))["highlights"] if h.get("skim")]
            tg = {t["id"]: t for t in json.load(open(os.path.join(TMP, "Papers", "_tags.json")))["tags"]}
            check("accepting makes a real highlight, coloured and tagged by lens", len(hs) == 1 and hs[0]["color"] in ("blue", "gray") and hs[0]["text"] and len(hs[0]["tags"]) == 1 and tg[hs[0]["tags"][0]]["category"] == "lens", hs)
            check("the accepted sentence is no longer a ghost, and the bar counts it", "1 highlighted" in page.inner_text("#rd-skim .sk-sum") and "2 shown" in page.inner_text("#rd-skim .sk-sum"))
            page.locator('.rd-ov .skim-g').first.scroll_into_view_if_needed(); box = page.locator('.rd-ov .skim-g').first.bounding_box()
            page.evaluate("document.querySelectorAll('#hv-ctx').forEach(e => e.remove())"); page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2); page.wait_for_selector(".rd-skimpop:not(.hide)"); page.click('.rd-skimpop [data-p="no"]')
            for _ in range(20):
                if json.load(open(sk_file))["dismissed"]:
                    break
                page.wait_for_timeout(250)
            check("dismissing is remembered in the skim file", len(json.load(open(sk_file))["dismissed"]) == 1 and "1 dismissed" in page.inner_text("#rd-skim .sk-sum"))
            shot(page, "15-skim")
            page.click('[data-sk="all"]'); page.wait_for_function("document.querySelector('#rd-skim .sk-sum').innerText.startsWith('0 shown')")
            page.keyboard.press("Control+z"); page.wait_for_function("!document.querySelector('#rd-skim .sk-sum').innerText.startsWith('0 shown')")
            check("'Highlight all shown' is one step: Ctrl Z takes all of them back", "1 highlighted" in page.inner_text("#rd-skim .sk-sum"))
            page.click('#reader [data-a="back"]'); page.wait_for_selector("#lf-q")
            page.locator('#lib-results .librow:has-text("Citations and Links") [data-read]').click(); page.wait_for_selector(".rd-band .textLayer span", timeout=30000)
            page.wait_for_selector("#rd-skim .sk-chip", timeout=15000)
            check("the layer reopens with the paper, at the density you left it", "Everything" in page.inner_text(".sk-dens") and "1 highlighted" in page.inner_text("#rd-skim .sk-sum") and "1 dismissed" in page.inner_text("#rd-skim .sk-sum"))
            page.click('.rd-tabs button[data-tab="skim"]'); page.wait_for_selector("#skl-body .skl-it", timeout=10000)
            check("the Skim tab lists the marked sentences by lens; the highlighted one is marked as done, the dismissed one is gone", page.locator("#skl-body .of-sec").count() >= 2 and page.locator("#skl-body .skl-it.done").count() == 1 and page.locator("#skl-body .skl-it").count() == 2, str(page.locator("#skl-body .skl-it").count()))
            page.locator("#skl-body .skl-it:not(.done) [data-skl='yes']").first.click(); page.wait_for_timeout(500)
            check("✓ in the list highlights a sentence for real", page.locator("#skl-body .skl-it.done").count() == 2)
            page.click('#reader [data-a="back"]')


            print("folders page")
            import base64
            import pymupdf as _pm
            _d = _pm.open(); _pg = _d.new_page(); _pg.insert_textbox(_pm.Rect(60, 60, 535, 200), "A dropped paper about platform governance and complementors in software ecosystems.", fontsize=12)
            _d.embfile_add("padding", os.urandom(45000)); drop_b64 = base64.b64encode(_d.tobytes()).decode(); _d.close()      # (the importer refuses PDFs under 40 KB as probably cut off)
            page.goto(base + "/"); page.click("#libitem"); page.wait_for_selector("#lf-q"); page.click("#folditem"); page.wait_for_selector(".fp-tile")
            check("the Library has a Folders tab showing each folder as a tile with its paper count", page.locator(".fp-tile[data-id]").count() == 2 and "Lab" in page.inner_text(".fp-tile[data-id='lab'] .fp-name") and page.text_content(".fp-tile[data-id='lab'] .fp-num") == "3")
            check("…and what is inside: cards and what else the folder holds", "3 papers · 3 with a card" in page.inner_text(".fp-tile[data-id='lab'] .fp-meta") and "💡" in page.inner_text(".fp-tile[data-id='lab'] .fp-flags") and "▦" in page.inner_text(".fp-tile[data-id='lab'] .fp-flags"))
            shot(page, "17-folders")
            page.evaluate("document.documentElement.dataset.theme = 'dark'"); page.wait_for_timeout(300); shot(page, "18-folders-dark"); page.evaluate("document.documentElement.dataset.theme = 'light'")
            names = lambda: page.eval_on_selector_all(".fp-tile[data-id] .fp-name", "es => es.map(e => e.textContent)")
            check("sorted by name by default", names() == ["Gov", "Lab"])
            page.select_option(".fp-sort", "papers"); check("sort by most papers", names() == ["Lab", "Gov"])
            page.select_option(".fp-sort", "name-desc"); check("sort by name, descending", names() == ["Lab", "Gov"])
            page.select_option(".fp-sort", "name"); page.fill(".fp-find", "la"); page.wait_for_function("document.querySelectorAll('.fp-tile[data-id]').length === 1")
            check("find a folder by typing", names() == ["Lab"]); page.fill(".fp-find", ""); page.wait_for_function("document.querySelectorAll('.fp-tile[data-id]').length === 2")
            page.click('[data-fp="view"]'); page.wait_for_selector(".fp-row[data-id]"); shot(page, "19-folders-list")
            check("switch to a list with counts and when it changed", page.locator(".fp-row[data-id]").count() == 2 and "3 with a card" in page.inner_text(".fp-row[data-id='lab']") and ("ago" in page.inner_text(".fp-row[data-id='lab']") or "just now" in page.inner_text(".fp-row[data-id='lab']")))
            page.click('[data-fp="view"]'); page.wait_for_selector(".fp-tile[data-id]")
            page.click('.fp-bar [data-fp="new"]'); page.wait_for_selector(".fp-newinput"); page.fill(".fp-newinput", "Reading group"); page.keyboard.press("Enter")
            page.wait_for_selector(".fp-tile[data-id='reading-group']")
            check("a new folder is made right on the page (type a name, Enter)", os.path.exists(os.path.join(TMP, "Papers", "_folders", "reading-group.json")) and page.locator(".fp-tile[data-id]").count() == 3)
            page.click('[data-fp="edit"]'); page.click(".fp-tile[data-id='reading-group']"); page.wait_for_selector(".fp-editbar"); shot(page, "20-folders-edit")
            check("Edit mode selects folders and offers Rename and Delete", "1 selected" in page.inner_text(".fp-editbar") and page.locator(".fp-tile.sel").count() == 1 and page.locator("[data-fp='rename']:not([disabled])").count() == 1)
            page.click('[data-fp="rename"]'); page.wait_for_selector(".fp-rename"); page.fill(".fp-rename", "Journal club"); page.keyboard.press("Enter"); page.wait_for_selector(".fp-tile[data-id='reading-group'] .fp-name:has-text('Journal club')")
            check("a folder is renamed in place", json.load(open(os.path.join(TMP, "Papers", "_folders", "reading-group.json")))["name"] == "Journal club")
            page.click(".fp-tile[data-id='reading-group']"); page.click('[data-fp="delete"]'); page.wait_for_function("!document.querySelector(\".fp-tile[data-id='reading-group']\")")
            check("and deleted (the papers stay)", not os.path.exists(os.path.join(TMP, "Papers", "_folders", "reading-group.json")) and len(api("/api/library")) >= 3)
            page.click('[data-fp="edit"]')
            page.evaluate("""([b64, id]) => { const bin = atob(b64), u = new Uint8Array(bin.length); for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i);
              const dt = new DataTransfer(); dt.items.add(new File([u], "dropped-paper.pdf", {type: "application/pdf"})); const t = document.querySelector(`[data-drop="${id}"]`);
              t.dispatchEvent(new DragEvent("dragover", {dataTransfer: dt, bubbles: true, cancelable: true})); t.dispatchEvent(new DragEvent("drop", {dataTransfer: dt, bubbles: true, cancelable: true})); }""", [drop_b64, "gov"])
            page.wait_for_selector("#ir-dlg[open]"); page.wait_for_selector("#ir-c0 .ir-f")
            check("dropping a PDF from the desktop onto a folder tile first shows what was found, with the destination fixed to that folder; nothing is filed yet", "“Gov”" in page.inner_text("#ir-dlg .ir-dest") and page.locator("#ir-to").count() == 0 and len(api("/api/library")) == 3 and page.input_value('[data-k="title"]').startswith("A dropped paper"), page.inner_text("#ir-dlg")[:200])
            page.click("#ir-go")
            try:
                page.wait_for_function("document.querySelector(\".fp-tile[data-id='gov'] .fp-num\")?.textContent === '2'", timeout=15000)
            except Exception:  # noqa: BLE001
                print("   toast:", page.evaluate("[...document.querySelectorAll('[id*=toast],[class*=toast]')].map(e => e.innerText).join('|')"), "| folders:", api("/api/folders"), "| library:", len(api("/api/library")), "| errors:", errors[-3:]); raise
            check("dropping a PDF from the desktop onto a folder adds it to the library and to that folder", api("/api/folders")[0]["count"] == 2 and len(api("/api/library")) == 4)
            page.click('[data-fp="rail-ask"]'); page.wait_for_selector("#lib-ask #am-q")
            check("the quick actions on the left jump to Ask", page.locator("#lib-ask:not(.hide)").count() == 1)
            page.click("#folditem"); page.wait_for_selector(".fp-tile")
            for _ in range(4):      # the Folders page redraws itself when its tab is shown; a click that begins just before is lost (no dialog is ever made), so click again, as a person would
                page.click('[data-fp="rail-add"]')
                try:
                    page.wait_for_selector("#adddlg[open]", timeout=2500); break
                except Exception:  # noqa: BLE001
                    pass
            else:
                print("  DEBUG the Add paper dialog did not open:", page.evaluate("JSON.stringify({dialog: !!document.querySelector('#adddlg'), open: [...document.querySelectorAll('dialog[open]')].map(d => d.id || d.className), tiles: document.querySelectorAll('.fp-tile').length, visibleRail: [...document.querySelectorAll('[data-fp=\"rail-add\"]')].map(b => b.getBoundingClientRect().width), hash: location.hash, body: document.body.className, toast: (document.querySelector('#toast') || {}).textContent})"))
                raise AssertionError("the Add paper dialog did not open")
            page.click("#ad-close")
            check("…and open the Add paper dialog", True)
            page.click(".fp-tile[data-id='lab']"); page.wait_for_selector(".fvtabs"); shot(page, "21-folder-view")
            check("opening a folder tile opens the folder, with its icon in the header", page.locator(".pagehead .fv-ico .fp-svg").count() == 1 and "Lab" in page.inner_text(".pagehead h1") and page.locator(".fvtabs button").count() == 10)

            print("navigation: sidebar, addresses, one page at a time")
            check("the sidebar offers Watch, Library, Review and Folders, with Settings at its foot; inside a folder, Folders is lit",
                  page.eval_on_selector_all(".mainnav .navbtn", "es => es.map(e => e.querySelector('.nt').textContent)") == ["Watch", "Library", "Review", "Folders"] and page.inner_text(".sb-foot #settingsbtn .nt") == "Settings" and page.eval_on_selector(".navbtn.on", "e => e.dataset.nav") == "folders")
            check("the sidebar is there on a wide window, lists the folders as pages, and the folder you are in is lit", page.locator("#sidebar").is_visible() and page.locator("#sbfolders .sbf").count() == len(api("/api/folders")) and page.locator("#sbfolders .sbf.on").count() == 1 and "Lab" in page.inner_text("#sbfolders .sbf.on"))
            check("the top bar says where you are (Folders › Lab)", "Lab" in page.inner_text("#tbtitle") and "Folders" in page.inner_text("#tbtitle"), page.inner_text("#tbtitle"))
            check("a folder has its own address, and the Library and Folders pages use the whole content width (the Watch panel is gone)", page.url.endswith("#/folder/lab") and not page.locator(".side").is_visible() and page.locator("#libview").bounding_box()["width"] > 900, page.url)
            check("the counts beside Library and Folders are the real ones", page.inner_text("#libcount") == str(len(api("/api/library"))) and page.inner_text("#foldcount") == str(len(api("/api/folders"))))
            page.click('.fvtabs [data-ft="matrix"]'); page.wait_for_timeout(300)
            check("a folder's tab is part of its address", page.url.endswith("#/folder/lab/matrix"), page.url)
            page.click("#libitem"); page.wait_for_selector("#lf-q"); page.click('.libtabs [data-tab="ask"]'); page.wait_for_timeout(300)
            check("Library is lit with its tab in the address", page.eval_on_selector(".navbtn.on", "e => e.dataset.nav") == "library" and page.url.endswith("#/library/ask"), page.url)
            page.click("#reviewitem"); page.wait_for_selector("#lib-review", timeout=10000)
            check("Review is its own entry in the sidebar: it opens today's flashcards, lights up, and has its own address", page.eval_on_selector(".navbtn.on", "e => e.dataset.nav") == "review" and page.url.endswith("#/library/review"), page.url)
            page.click("#watchitem"); page.wait_for_selector(".side")
            check("Watch brings its side panel (weekly check, past runs) back", page.locator(".side").is_visible() and page.url.endswith("#/watch") and page.eval_on_selector(".navbtn.on", "e => e.dataset.nav") == "watch")
            page.go_back(); page.wait_for_selector("#lib-review:not(.hide)"); page.go_back(); page.wait_for_selector('.libtabs [data-tab="ask"].on')
            check("the browser's Back button returns to the Library tab you were on", page.url.endswith("#/library/ask") and page.locator('.libtabs [data-tab="ask"].on').count() == 1, page.url)
            page.click("#folditem"); page.wait_for_selector(".fp-tile"); page.reload(); page.wait_for_selector(".fp-tile"); page.wait_for_selector(".fp", state="visible"); page.wait_for_function("location.hash === '#/folders'")
            try:
                page.wait_for_function("location.hash === '#/folders' && !!document.querySelector('.fp') && document.querySelector('.fp').offsetParent !== null", timeout=10000); on_folders = True
            except Exception:  # noqa: BLE001
                on_folders = False
            check("reloading the page keeps you on the Folders page", on_folders and page.url.endswith("#/folders") and page.locator(".fp").first.is_visible(), "%s | .fp: %d" % (page.url, page.locator(".fp").count()))
            page.goto("about:blank"); page.goto(base + "/#/folder/lab/matrix"); page.wait_for_selector(".fvtabs")
            check("an address like #/folder/lab/matrix opens that folder on that tab", page.locator('.fvtabs [data-ft="matrix"].on').count() == 1 and "Lab" in page.inner_text(".pagehead h1"))
            page.click("#settingsbtn"); page.wait_for_selector("#st-body")
            check("Settings opens from the top bar and lights up there; clicking it again closes it", page.eval_on_selector(".navbtn.on", "e => e.dataset.nav") == "settings" and page.locator("#settings:not(.hide)").count() == 1)
            page.click('.st-tabs button[data-t="look"]'); page.wait_for_selector("#lk-theme")
            check("Settings → Appearance is about the theme only (no Subtle/Bold look), and Light is the one chosen by default", page.locator("#lk-look").count() == 0 and page.locator('#lk-theme input[value="light"]').is_checked() and page.evaluate("document.documentElement.dataset.theme") == "light")
            page.click('#lk-theme label:has-text("Dark")'); page.wait_for_timeout(200)
            check("choosing Dark switches the whole app and remembers it", page.evaluate("document.documentElement.dataset.theme") == "dark" and page.evaluate("localStorage.getItem('jw-theme')") == "dark" and page.evaluate("getComputedStyle(document.body).backgroundColor") != "rgb(251, 250, 247)")
            page.click('#lk-theme label:has-text("Light")'); page.wait_for_timeout(200)
            check("…and back to Light", page.evaluate("document.documentElement.dataset.theme") == "light" and page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(251, 250, 247)")
            tool_cols = page.evaluate("[...document.querySelectorAll('.fvtabs button')].map(b => getComputedStyle(b.querySelector('.ico')).color)")
            check("each tool in a folder has its own colour on its tab icon", len(set(tool_cols)) >= 8 and len(tool_cols) == 10, str(tool_cols))
            page.click("#settingsbtn"); page.wait_for_selector("#settings.hide", state="attached")
            check("…and the page you were on is lit again", page.eval_on_selector(".navbtn.on", "e => e.dataset.nav") == "folders")

            print("drag and drop PDFs you already have")
            def mkpdf(text, authors=None):
                d = _pm.open(); pg = d.new_page(); pg.insert_textbox(_pm.Rect(60, 60, 535, 200), text, fontsize=16 if authors else 12)
                if authors:
                    pg.insert_textbox(_pm.Rect(60, 210, 535, 240), authors, fontsize=10)
                d.embfile_add("padding", os.urandom(45000)); b = base64.b64encode(d.tobytes()).decode(); d.close(); return b
            pa, pd, pb, TXT = mkpdf("Alpha study of onboarding complementors in digital platform ecosystems and their governance.", "Ada Alpha and Bo Beta"), mkpdf("Delta study of marketplace reputation systems and seller behaviour.", "Zed Unrelated and Yan Other"), mkpdf("Bravo analysis of algorithmic management among gig workers and their coping strategies."), base64.b64encode(b"just notes").decode()
            PDF = lambda b, n: [b, n, "application/pdf"]
            n_lib = lambda: len(api("/api/library")); folder = lambda i: next(f for f in api("/api/folders") if f["id"] == i)
            page.goto(base + "/"); page.click("#libitem"); page.wait_for_selector("#lf-q"); page.click("#folditem"); page.wait_for_selector(".fp-tile"); n0 = n_lib()
            STAGING = os.path.join(TMP, "local-state", "staging")       # (the server runs with JW_LOCAL: staged PDFs are per-computer files)
            staged = lambda: len([n for n in os.listdir(STAGING) if n.endswith(".pdf")]) if os.path.isdir(STAGING) else 0
            imp_log = lambda: open(os.path.join(TMP, "imp.log")).read() if os.path.exists(os.path.join(TMP, "imp.log")) else ""
            ov = page.evaluate(JS_DROP, [".fp-path", [PDF(pa, "alpha.pdf")], True])
            check("dragging files over the Folders page shows an overlay saying you will check the details and choose where they go", bool(ov) and "choose" in ov and "check the details" in ov, repr(ov))
            page.evaluate(JS_DROP, [".fp-path", [PDF(pa, "alpha.pdf"), [TXT, "notes.txt", "text/plain"]], False]); page.wait_for_selector("#ir-dlg[open]"); page.wait_for_selector("#ir-c0 .ir-cand")
            shot(page, "22-drop-review")
            lookups = imp_log()
            check("dropping on the Folders page opens ONE dialog with a card per PDF; nothing is filed yet and the text file is skipped",
                  n_lib() == n0 and page.locator("#ir-dlg .ir-card").count() == 1 and "notes.txt" in page.inner_text("#ir-dlg") and "not a PDF or EPUB, skipped" in page.inner_text("#ir-dlg") and page.locator("#di-dlg").count() == 0 and "Alpha study of onboarding" in lookups)
            check("…the PDF was looked up by the title it carries, and the staged copy waits outside the library", sum(1 for l in lookups.splitlines() if l.startswith("Alpha study")) == 1 and staged() == 1 and n_lib() == n0)
            check("…it shows the records found (best first, the unrelated one marked), a 'None of these' choice and boxes filled from the first record",
                  page.locator("#ir-c0 .ir-cand").count() == 3 and page.eval_on_selector('input[name="ir-c0"]:checked', "e => e.value") == "0" and page.locator("#ir-c0 .ir-cand.mis").count() == 1 and "None of these" in page.inner_text("#ir-c0")
                  and page.input_value('[data-k="authors"]') == "Ada Alpha; Bo Beta" and page.input_value('[data-k="journal"]') == "Journal of Test Platforms" and page.input_value('[data-k="pub_date"]') == "2021-05-01" and page.input_value('[data-k="doi"]') == "10.9999/alpha.2021.1")
            check("…and says what backs the record up: its title AND authors are on this PDF, the unrelated record does not look like it", "backs up" in page.inner_text("#ir-c0 .ir-st") and "Title and authors are on the PDF’s first pages" in page.inner_text("#ir-c0") and "Does not look like this PDF" in page.inner_text("#ir-c0"))
            check("…the destination is a choice here (Library only, each folder, a new folder)", page.locator("#ir-to option").count() == 4 and page.locator("#ir-go:not([disabled])").count() == 1)
            page.select_option("#ir-to", "new"); page.fill("#ir-name", "Dropped batch"); page.click("#ir-go"); page.wait_for_selector(".di-snack:has-text('Added 1 paper to')", timeout=15000); page.wait_for_selector(".fp-tile[data-id='dropped-batch']")
            filed = next((r for r in api("/api/library") if r.get("doi") == "10.9999/alpha.2021.1"), {})
            check("choosing New folder makes it and files the PDF in it, with the details from the record you confirmed", folder("dropped-batch")["count"] == 1 and n_lib() == n0 + 1 and "“Dropped batch”" in page.inner_text(".di-snack")
                  and filed.get("title", "").startswith("Alpha study") and "Ada Alpha" in str(filed.get("authors")) and filed.get("journal") == "Journal of Test Platforms" and filed.get("file", "").startswith("Papers/2021-05/") and "confirmed by you" in filed.get("note", ""), str(filed)[:300])
            check("…and the staged copy is gone", staged() == 0)
            page.click('.di-snack [data-di="undo"]'); page.wait_for_function("!document.querySelector(\".fp-tile[data-id='dropped-batch']\")")
            check("Undo takes the new folder away again; the paper stays in the library", not any(f["id"] == "dropped-batch" for f in api("/api/folders")) and n_lib() == n0 + 1)
            page.evaluate(JS_DROP, [".fp-path", [PDF(pa, "alpha-copy.pdf")], False]); page.wait_for_selector("#ir-dlg[open]"); page.wait_for_selector("#ir-c0 .ir-cand")
            check("a PDF you already have is flagged in its card before anything happens", page.locator("#ir-x0:not(.hide)").count() == 1 and "already have this paper" in page.inner_text("#ir-x0"))
            page.check('input[name="ir-c0"][value="1"]'); page.wait_for_selector("#ir-x0.hide", state="attached", timeout=5000)
            check("…the warning goes when you pick another record (a different paper), and returns when you pick the first again", page.locator("#ir-x0:not(.hide)").count() == 0)
            page.check('input[name="ir-c0"][value="0"]'); page.wait_for_selector("#ir-x0:not(.hide)", timeout=5000)
            check("…and it follows what you type: another title and DOI clears it, the paper's own DOI brings it back", page.locator("#ir-x0:not(.hide)").count() == 1)
            page.fill('[data-k="doi"]', ""); page.fill('[data-k="title"]', "Something nobody has filed yet"); page.wait_for_selector("#ir-x0.hide", state="attached", timeout=5000)
            page.fill('[data-k="doi"]', "10.9999/alpha.2021.1"); page.wait_for_selector("#ir-x0:not(.hide)", timeout=5000)
            page.check('input[name="ir-c0"][value="1"]'); page.check('input[name="ir-c0"][value="0"]')
            page.select_option("#ir-to", "f:lab"); page.click("#ir-go")
            page.wait_for_selector(".di-snack:has-text('already in your library')", timeout=15000)
            check("…and it is not imported twice, only added to the folder", n_lib() == n0 + 1 and folder("lab")["count"] == 4 and "Added 1 paper to “Lab”" in page.inner_text(".di-snack") and staged() == 0)
            page.evaluate(JS_DROP, [".fp-path", [PDF(pb, "bravo.pdf")], False]); page.wait_for_selector("#ir-dlg[open]"); page.wait_for_selector("#ir-c0 .ir-f"); page.select_option("#ir-to", "new"); page.fill("#ir-name", "gov"); page.click("#ir-go")
            check("a folder name that already exists is refused with a hint, and the dialog stays open", page.locator("#ir-dlg[open]").count() == 1 and "already exists" in page.inner_text("#ir-err") and n_lib() == n0 + 1)
            check("a PDF nothing is known about says so, offers no records, and fills the title from the PDF for you to correct", page.locator("#ir-c0 .ir-cand").count() == 0 and "No record was found online" in page.inner_text("#ir-c0 .ir-st") and page.input_value('[data-k="title"]').startswith("Bravo analysis"))
            page.keyboard.press("Escape"); page.wait_for_selector("#ir-dlg", state="detached")
            page.wait_for_timeout(300)
            check("Escape cancels without importing anything, and the staged copy is thrown away", n_lib() == n0 + 1 and staged() == 0)
            page.evaluate(JS_DROP, [".fp-path", [[TXT, "notes.txt", "text/plain"]], False]); page.wait_for_selector("#toast:has-text('aren’t PDFs')", timeout=5000)
            check("files that are not PDFs are turned away with a message, no dialog", page.locator("#ir-dlg").count() == 0 and n_lib() == n0 + 1)
            page.evaluate(JS_DROP, [".fp-path", [PDF(pb, "bravo.pdf")], False]); page.wait_for_selector("#ir-dlg[open]"); page.wait_for_selector("#ir-c0 .ir-f"); page.uncheck("[data-inc='0']")
            check("a PDF you untick is not added (the Add button is off) …", page.locator("#ir-go[disabled]").count() == 1 and n_lib() == n0 + 1)
            page.click("#ir-cancel"); page.wait_for_selector("#ir-dlg", state="detached"); page.wait_for_timeout(300)
            check("… and cancelling leaves nothing behind", n_lib() == n0 + 1 and staged() == 0)
            page.click(".fp-tile[data-id='gov']"); page.wait_for_selector("#fv-body .librow"); gov_rows = page.locator("#fv-body .librow").count()
            ov = page.evaluate(JS_DROP, ["#fv-body", [PDF(pb, "bravo.pdf")], True])
            check("inside an open folder the overlay says the PDFs go to that folder", ov == "Add to this folder", repr(ov))
            page.evaluate(JS_DROP, ["#fv-body", [PDF(pb, "bravo.pdf")], False]); page.wait_for_selector("#ir-dlg[open]"); page.wait_for_selector("#ir-c0 .ir-f")
            check("inside an open folder the dialog still opens for the details, but the destination is fixed to that folder (no list to pick from)", page.locator("#ir-to").count() == 0 and "“Gov”" in page.inner_text("#ir-dlg .ir-dest") and n_lib() == n0 + 1)
            page.fill('[data-k="title"]', "Bravo checked by hand"); page.fill('[data-k="authors"]', "Dee Bravo; Eli Charlie"); bad_dates = []
            for bd in ("March 2022", "2022-13", "2022-02-31", "2022-03-01x"):
                page.fill('[data-k="pub_date"]', bd); page.click("#ir-go"); bad_dates.append("must be a year" in page.inner_text("#ir-e0") and page.locator("#ir-dlg[open]").count() == 1 and n_lib() == n0 + 1)
            check("a date that is not a real year, month or day (March 2022, 2022-13, 2022-02-31, 2022-03-01x) is refused next to the paper and nothing is filed", all(bad_dates) and len(bad_dates) == 4, str(bad_dates))
            page.fill('[data-k="pub_date"]', "2022-03"); page.click("#ir-go"); page.wait_for_selector(".di-snack:has-text('Added 1 paper to')", timeout=15000)
            page.wait_for_function("document.querySelectorAll('#fv-body .librow').length === %d" % (gov_rows + 1))
            hand = next((r for r in api("/api/library") if r.get("title") == "Bravo checked by hand"), {})
            check("what you typed is what is filed: title, authors, date (under Papers/2022-03) and a note saying you typed it", n_lib() == n0 + 2 and folder("gov")["count"] == gov_rows + 1 and "Dee Bravo" in str(hand.get("authors")) and hand.get("file", "").startswith("Papers/2022-03/") and "typed by you" in hand.get("note", ""), str(hand)[:300])
            page.click('.di-snack [data-di="undo"]'); page.wait_for_function("document.querySelectorAll('#fv-body .librow').length === %d" % gov_rows)
            check("Undo takes it back out of the folder", folder("gov")["count"] == gov_rows and n_lib() == n0 + 2)
            page.click("#fv-add"); page.click('#fv-addmenu [data-add="lib"]'); page.wait_for_selector("#di-pick[open] .di-opt")
            n_pick = page.locator("#di-pick .di-opt").count(); page.fill("#dp-find", "zzzz-nothing"); no_hit = page.locator("#di-pick .di-opt").count(); page.fill("#dp-find", "")
            page.locator("#di-pick .di-opt input").first.check(); page.click("#dp-go"); page.wait_for_selector("#di-pick", state="detached")
            page.wait_for_function("document.querySelectorAll('#fv-body .librow').length === %d" % (gov_rows + 1))
            check("＋ Add papers → papers already in my library lists the ones not in the folder, filters them and adds the ticked one", n_pick == n_lib() - gov_rows and no_hit == 0 and folder("gov")["count"] == gov_rows + 1, "%d / %d / %d" % (n_pick, n_lib(), gov_rows))
            page.goto(base + "/"); page.click("#libitem"); page.wait_for_selector("#lf-q")
            page.evaluate(JS_DROP, ["#lib-papers", [PDF(pa, "alpha.pdf")], False]); page.wait_for_selector("#ir-dlg[open]"); page.wait_for_selector("#ir-c0 .ir-cand")
            check("anywhere else in the Library the dialog opens with Library only preselected", page.input_value("#ir-to") == "lib")
            page.click("#ir-go"); page.wait_for_selector(".di-snack:has-text('Nothing new to add')", timeout=15000)
            check("…and a PDF already in the library says so", "already in your library" in page.inner_text(".di-snack") and n_lib() == n0 + 2)
            page.evaluate(JS_DROP, ["#lib-papers", [PDF(pd, "delta.pdf")], False]); page.wait_for_selector("#ir-dlg[open]"); page.wait_for_selector("#ir-c0 .ir-cand")
            check("a record whose title is on the PDF but whose authors are not is shown as 'please check', not as confirmed", "authors could not be confirmed" in page.inner_text("#ir-c0 .ir-st") and "authors not confirmed" in page.inner_text("#ir-c0 .ir-cand") and "backs up" not in page.inner_text("#ir-c0 .ir-st"), page.inner_text("#ir-c0")[:300])
            page.click("#ir-cancel"); page.wait_for_selector("#ir-dlg", state="detached")
            page.click("#lf-add"); page.wait_for_selector("#adddlg[open]")
            page.set_input_files("#ad-file", files=[{"name": "alpha.pdf", "mimeType": "application/pdf", "buffer": base64.b64decode(pa)}]); page.wait_for_selector("#ir-dlg[open]"); page.wait_for_selector("#ir-c0 .ir-cand")
            check("Add a paper → choosing a PDF opens the same review dialog (Library only), not a silent import", page.locator("#adddlg[open]").count() == 0 and page.input_value("#ir-to") == "lib" and n_lib() == n0 + 2)
            page.click("#ir-cancel"); page.wait_for_selector("#ir-dlg", state="detached")

            print("computers and sync")
            import datetime as _dt
            base_ = os.path.join(TMP, ".agents", "skills", "ais-journal-watcher"); devdir = os.path.join(base_, "devices"); os.makedirs(devdir, exist_ok=True)
            seen_now = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
            json.dump({"id": "a" * 32, "name": "Work Mac", "seen": seen_now, "pid": 1, "version": "1.2.2"}, open(os.path.join(devdir, "a" * 32 + ".json"), "w"))
            json.dump({"id": "b" * 32, "name": "Old desk", "seen": (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=3)).isoformat(timespec="seconds"), "pid": 2, "version": "1.2.1"}, open(os.path.join(devdir, "b" * 32 + ".json"), "w"))
            idx_path = os.path.join(TMP, "Papers", "_index.csv"); idx_rows = list(csv.DictReader(open(idx_path, newline="")))
            extra = dict(idx_rows[0], title="A paper only the other computer knew", doi="10.7777/other.1", file="", status="failed")
            with open(os.path.join(TMP, "Papers", "_index (Work Mac's conflicted copy 2026-11-03).csv"), "w", newline="") as f:
                w_ = csv.DictWriter(f, list(idx_rows[0].keys())); w_.writeheader(); w_.writerows(idx_rows + [extra])
            api("/api/sync")          # (the status call below uses the scan of the last 20 seconds: this one makes a fresh one)
            st_ = api("/api/sync/status")
            check("the server knows which other computer has the folder open and how many conflicted copies there are", st_["open"] == ["Work Mac"] and st_["conflicts"] == 1, str(st_))
            page.goto(base + "/"); page.wait_for_selector("#syncpill:not(.hide)", timeout=10000)
            check("the top bar says so, in words: 'Also open on Work Mac · 1 conflicting copy'", "Also open on Work Mac" in page.inner_text("#syncpill") and "1 conflicting copy" in page.inner_text("#syncpill"), page.inner_text("#syncpill"))
            page.click("#syncpill"); page.wait_for_selector("#sy-comps", timeout=10000)
            check("pressing it opens Settings → Computers & sync", page.inner_text(".st-tabs .on").strip() == "Computers & sync" and page.locator(".st-tabs button").count() == 8)
            check("…which lists the other computers (open now / last seen), warns that both open at once can collide, and says what stays on this computer", "open now" in page.inner_text("#sy-comps") and "Old desk" in page.inner_text("#sy-comps") and "3 days ago" in page.inner_text("#sy-comps") and "Work Mac right now" in page.inner_text("#st-body") and "Kept on this computer only" in page.inner_text("#st-body"))
            check("…and shows the conflicted copy with what it is a copy of", "_index (Work Mac" in page.inner_text("#sy-conf") and "Papers/_index.csv" in page.inner_text("#sy-conf") and page.locator("[data-merge]").count() == 1)
            page.fill("#sy-name", "Home laptop"); page.click("#sy-save"); page.wait_for_function("document.querySelector('#toast') && document.querySelector('#toast').textContent === 'Name saved.'", timeout=10000)          # (the page's own confirmation: the input already held the new name after fill(), so waiting on its value never waited for the save)
            me_ = api("/api/sync")["me"]
            mine_ = [json.load(open(os.path.join(devdir, n)))["name"] for n in os.listdir(devdir) if n == me_["id"] + ".json"]          # (not the .tmp file the heartbeat writes first)
            check("this computer can be renamed, and the other computers see the new name at once", me_["name"] == "Home laptop" and mine_ == ["Home laptop"], str((me_, mine_)))
            n_before = len(list(csv.DictReader(open(idx_path, newline=""))))
            page.click("[data-merge]"); page.wait_for_selector("#sy-conf .hint.ok", timeout=10000)
            after_ = list(csv.DictReader(open(idx_path, newline="")))
            check("'Join the two lists' keeps every paper of both copies, and sets the copy aside instead of deleting it", len(after_) == n_before + 1 and any(r["doi"] == "10.7777/other.1" for r in after_) and not os.path.exists(os.path.join(TMP, "Papers", "_index (Work Mac's conflicted copy 2026-11-03).csv"))
                  and any("conflicted copy" in n for _, _, fs_ in os.walk(os.path.join(TMP, "Papers", "_conflicts")) for n in fs_))
            open(os.path.join(base_, "interests (Work Mac's conflicted copy 2026-11-03).json"), "w").write("{}")
            page.click('.st-tabs button[data-t="look"]'); page.click('.st-tabs button[data-t="sync"]'); page.wait_for_selector("[data-aside]", timeout=10000)
            check("another kind of file (settings) can only be set aside, not 'joined'", page.locator("[data-merge]").count() == 0 and page.locator("[data-aside]").count() == 1)
            page.click("[data-aside]"); page.wait_for_selector("#sy-conf .hint.ok", timeout=10000)
            check("setting it aside moves it to Papers/_conflicts and leaves the real file alone", not os.path.exists(os.path.join(base_, "interests (Work Mac's conflicted copy 2026-11-03).json")) and os.path.exists(os.path.join(base_, "interests.json")) and api("/api/sync/status")["conflicts"] == 0)
            check("without the Mac's Keychain the API keys card says they are kept in the .env file", ".env" in page.inner_text("#st-body") and page.locator("#sy-keys").count() == 0)
            os.remove(os.path.join(devdir, "a" * 32 + ".json")); page.goto(base + "/"); page.wait_for_timeout(1200)
            check("when nothing needs saying the top bar says nothing", page.locator("#syncpill.hide").count() == 1)

            print("books")
            import io as _io, zipfile as _zf

            def mk_epub(title, isbn, authors=("Lovelace, Ada",), chapters=3, drm=False):
                b_ = _io.BytesIO()
                with _zf.ZipFile(b_, "w") as z_:
                    z_.writestr("mimetype", "application/epub+zip", compress_type=_zf.ZIP_STORED)
                    z_.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
                    if drm:
                        z_.writestr("META-INF/encryption.xml", '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><EncryptedData><EncryptionMethod Algorithm="http://www.w3.org/2001/04/xmlenc#aes128-cbc"/></EncryptedData></encryption>')
                    z_.writestr("OEBPS/content.opf", '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">urn:isbn:%s</dc:identifier><dc:title>%s</dc:title>%s<dc:publisher>Test University Press</dc:publisher><dc:date>2019-05-01</dc:date></metadata><manifest>%s<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/></manifest><spine>%s</spine></package>'
                                % (isbn, title, "".join("<dc:creator>%s</dc:creator>" % a for a in authors), "".join('<item id="c%d" href="c%d.xhtml" media-type="application/xhtml+xml"/>' % (i, i) for i in range(chapters)), "".join('<itemref idref="c%d"/>' % i for i in range(chapters))))
                    z_.writestr("OEBPS/nav.xhtml", '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc"><ol>%s</ol></nav></body></html>' % "".join('<li><a href="c%d.xhtml">Chapter %d</a></li>' % (i, i + 1) for i in range(chapters)))
                    for i in range(chapters):
                        z_.writestr("OEBPS/c%d.xhtml" % i, '<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>Chapter %d</h1>%s</body></html>' % (i + 1, "".join("<p>%s</p>" % ("Zymurgy ecosystems and complementors, paragraph %d of chapter %d. " % (j, i + 1) + "Open platforms need clear rules. " * 20) for j in range(20))))
                return base64.b64encode(b_.getvalue()).decode()
            EP = lambda b_, n_: [b_, n_, "application/epub+zip"]
            book_b64 = mk_epub("Platforms and Their Governance", "978-0-201-83595-3")
            n_all = lambda: len(api("/api/library")); n_before_books = n_all()
            page.goto(base + "/"); page.click("#libitem"); page.wait_for_selector("#lf-q")
            check("the Library has a Books tab next to Papers", [b_.inner_text().strip() for b_ in page.locator(".libtabs button").all()][:2] == ["Papers", "Books"])
            page.click('.libtabs [data-tab="books"]'); page.wait_for_selector("#bk-add")
            check("it is empty at first and says how to add books", "No books yet" in page.inner_text("#lib-books") and page.locator("#lib-papers:not(.hide)").count() == 0)
            ov = page.evaluate(JS_DROP, ["#lib-books", [EP(book_b64, "governance.epub")], True])
            check("dragging files over the Books tab says they are added as books", bool(ov) and "books" in ov and "check the details" in ov, repr(ov))
            page.evaluate(JS_DROP, ["#lib-books", [EP(book_b64, "governance.epub")], False]); page.wait_for_selector("#ir-dlg[open]"); page.wait_for_selector("#ir-c0 .ir-cand", timeout=20000)
            shot(page, "23-book-review")
            check("dropping an EPUB opens the review dialog: it is a book (no paper/book choice for an EPUB), with the file's own details first", "Book" in page.inner_text("#ir-c0 .ir-kind") and page.locator("#ir-c0 [data-kind]").count() == 0 and "file’s own details" in page.inner_text("#ir-c0 .ir-cand >> nth=0") and "Check the details of 1 EPUB" in page.inner_text("#ir-h"), page.inner_text("#ir-c0")[:300])
            check("…the boxes hold title, authors (as 'Ada Lovelace'), publisher, year and ISBN, and the dialog counts chapters", page.input_value('[data-k="title"]') == "Platforms and Their Governance" and page.input_value('[data-k="authors"]') == "Ada Lovelace" and page.input_value('[data-k="publisher"]') == "Test University Press"
                  and page.input_value('[data-k="pub_date"]') == "2019" and page.input_value('[data-k="isbn"]') == "9780201835953" and "outline of 3 items" in page.inner_text("#ir-c0 .ir-h"), page.inner_text("#ir-c0 .ir-h"))
            check("…and the other record found online (Open Library) is offered too; nothing is filed yet", page.locator("#ir-c0 .ir-cand").count() >= 2 and n_all() == n_before_books and "Add 1 book" == page.inner_text("#ir-go").strip())
            page.fill('[data-k="isbn"]', "9780201835954"); page.click("#ir-go")
            check("an ISBN with a wrong check digit is refused next to the book", "not valid" in page.inner_text("#ir-e0") and page.locator("#ir-dlg[open]").count() == 1 and n_all() == n_before_books)
            page.fill('[data-k="isbn"]', "9780201835953"); page.click("#ir-go"); page.wait_for_selector(".di-snack:has-text('Added 1 book')", timeout=30000)
            bk_row = next((r_ for r_ in api("/api/library") if r_["journal"] == "Books"), {})
            bk_full = os.path.join(TMP, bk_row.get("file", "x"))
            check("the book is filed under the Books journal: the PDF the reader shows, the EPUB as given, and its details (ISBN, publisher, chapters)",
                  bk_row.get("title") == "Platforms and Their Governance" and bk_row["file"].startswith("Papers/2019-01/Books/") and os.path.exists(bk_full) and os.path.exists(bk_full[:-4] + ".epub")
                  and bk_row["book"]["isbn"] == "9780201835953" and bk_row["book"]["publisher"] == "Test University Press" and bk_row["book"]["source"] == "epub" and len(bk_row["book"]["toc"]) == 3 and n_all() == n_before_books + 1, str(bk_row)[:300])
            page.wait_for_selector("#bk-list .librow", timeout=10000)
            check("the Books tab lists it, with publisher, year, pages, format and chapters", all(x_ in page.inner_text("#bk-list .librow") for x_ in ("Test University Press", "2019", "EPUB", "pages", "outline of 3 items")), page.inner_text("#bk-list .librow")[:300])
            page.fill("#bk-q", "nothing like this"); check("…and searching by title, author, publisher or ISBN narrows the list", "No book matches" in page.inner_text("#bk-list")); page.fill("#bk-q", "9780201835953")
            check("…the ISBN finds it", page.locator("#bk-list .librow").count() == 1); page.fill("#bk-q", "")
            page.click('.libtabs [data-tab="papers"]'); page.fill("#lf-q", "Platforms and Their Governance"); page.wait_for_selector("#lib-results .reshead, #lib-results .empty", timeout=10000)
            check("a book is not listed among the papers (it has its own tab)", "Platforms and Their Governance" not in [t_.strip() for t_ in page.locator("#lib-results .ptitle").all_inner_texts()] and "Test University Press" not in page.inner_text("#lib-results"), page.inner_text("#lib-results")[:200])
            check("…and 'Journal' in the paper filters does not offer Books", "Books" not in page.inner_text("#lf-journal")); page.fill("#lf-q", "")
            page.click('.libtabs [data-tab="books"]'); page.wait_for_selector("#bk-list .librow")
            page.fill("#bk-q", "zymurgy ecosystems"); page.click("#bk-ft"); page.wait_for_selector("#bk-out .ft-i", timeout=60000)
            check("'In the books' finds passages inside the converted book, each named by chapter and place in the reading copy ('Chapter 1 · location N')", page.locator("#bk-out .ft-i").count() >= 1 and "Platforms and Their Governance" in page.inner_text("#bk-out .ft-i >> nth=0") and "· location " in page.inner_text("#bk-out .ft-h >> nth=0"))
            page.fill("#bk-q", ""); page.click("#bk-list [data-read]"); page.wait_for_selector(".rd-band .textLayer span", timeout=60000)
            page.click('.rd-tabs button[data-tab="toc"]'); page.wait_for_selector("#toc-list, .toc-list, #rd-toc", timeout=10000)
            check("the book opens in the reader, and its chapters are the outline", "Platforms and Their Governance" in page.inner_text("#rd-title") and "Chapter 2" in page.inner_text("body"), page.inner_text("#rd-title"))
            page.click('.rd-bar [data-a="back"]'); page.wait_for_selector("#bk-add")
            # the same book again; a PDF dropped on the Books tab is a book unless you say it is a paper; DRM is refused in plain words
            page.click('.libtabs [data-tab="books"]'); page.wait_for_selector("#bk-add")
            page.evaluate(JS_DROP, ["#lib-books", [EP(mk_epub("Platforms and Their Governance, second printing", "978-0-201-83595-3"), "again.epub")], False]); page.wait_for_selector("#ir-dlg[open]"); page.wait_for_selector("#ir-c0 .ir-f", timeout=20000)
            check("the same book (same ISBN) is flagged as already in the library before anything happens", page.locator("#ir-x0:not(.hide)").count() == 1 and "already have this book" in page.inner_text("#ir-x0"))
            page.click("#ir-go"); page.wait_for_selector(".di-snack:has-text('already in your library')", timeout=20000)
            check("…and it is not filed twice", n_all() == n_before_books + 1 and len([r_ for r_ in api("/api/library") if r_["journal"] == "Books"]) == 1)
            page.evaluate(JS_DROP, ["#lib-books", [EP(mk_epub("Locked Book", "978-0-306-40615-7", drm=True), "locked.epub")], False]); page.wait_for_selector("#ir-dlg[open]"); page.wait_for_selector("#ir-c0 .ir-st.bad", timeout=20000)
            check("a copy-protected EPUB is turned away in plain words and nothing is staged", "copy-protected" in page.inner_text("#ir-c0") and page.locator("#ir-go[disabled]").count() == 1 and staged() == 0)
            page.click("#ir-cancel"); page.wait_for_selector("#ir-dlg", state="detached")
            page.evaluate(JS_DROP, ["#lib-books", [PDF(pb, "bravo-book.pdf")], False]); page.wait_for_selector("#ir-dlg[open]"); page.wait_for_selector("#ir-c0 .ir-f", timeout=20000)
            check("a PDF dropped on the Books tab starts as a book (publisher and ISBN boxes), and can be switched to a paper", page.locator('[data-k="publisher"]').count() == 1 and page.locator('[data-k="journal"]').count() == 0 and page.locator('#ir-c0 [data-kind][value="book"]:checked').count() == 1)
            page.check('#ir-c0 [data-kind][value="paper"]'); page.wait_for_selector('[data-k="journal"]', timeout=20000)
            check("…switching looks at it again as a paper (journal box, no publisher), nothing filed, nothing left staged but its own copy", page.locator('[data-k="publisher"]').count() == 0 and n_all() == n_before_books + 1 and staged() == 1)
            page.click("#ir-cancel"); page.wait_for_selector("#ir-dlg", state="detached"); page.wait_for_timeout(300)
            check("cancelling forgets the staged copy", staged() == 0)
            page.goto(base + "/"); page.click("#libitem"); page.wait_for_selector("#lf-q"); page.click("#folditem"); page.wait_for_selector(".fp-tile")
            page.evaluate(JS_DROP, [".fp-path", [EP(mk_epub("A Second Book on Rules", "978-0-262-03384-8"), "rules.epub"), PDF(pb, "paper-on-the-side.pdf")], False]); page.wait_for_selector("#ir-dlg[open]"); page.wait_for_selector("#ir-c1 .ir-f", timeout=30000)
            check("dropped on the Folders page an EPUB is a book and a PDF stays a paper (default); one dialog, a destination to choose", page.locator("#ir-c0 [data-k='publisher']").count() == 1 and page.locator("#ir-c1 [data-k='journal']").count() == 1 and page.locator("#ir-to").count() == 1)
            page.select_option("#ir-to", "f:lab"); n_lab = folder("lab")["count"]; page.click("#ir-go"); page.wait_for_selector(".di-snack:has-text('Added 2')", timeout=30000)
            check("both are filed and added to the folder", folder("lab")["count"] == n_lab + 2 and len([r_ for r_ in api("/api/library") if r_["journal"] == "Books"]) == 2)

            print("book chapters")
            ep_row = next(r_ for r_ in api("/api/library") if r_["title"] == "Platforms and Their Governance")
            bi_ = api("/api/book?file=" + urllib.parse.quote(ep_row["file"]))
            check("the server gives a book's chapters as page ranges (a chapter runs to where the next starts)", [c_["title"] for c_ in bi_["chapters"]] == ["Chapter 1", "Chapter 2", "Chapter 3"] and bi_["source"] == "epub" and bi_["chapters"][0]["end"] == bi_["chapters"][1]["start"] and bi_["chapters"][2]["end"] == bi_["pages"], str(bi_)[:300])
            page.goto(base + "/"); page.click("#libitem"); page.wait_for_selector("#lf-q"); page.wait_for_function("document.querySelectorAll('#lf-folder option').length >= 2")      # (the Library finishes loading its folders and redraws its tab: wait for that, or a click made just before is lost)
            page.click('.libtabs [data-tab="books"]'); page.wait_for_selector("#bk-list .librow")
            page.fill("#bk-q", "Platforms and Their Governance"); page.wait_for_selector('#bk-list .librow:has-text("Platforms and Their Governance")')
            page.click("#bk-list [data-chap]"); page.wait_for_selector("#bk-list .chap-ol li", timeout=10000)
            check("the Chapters button lists the chapters; an EPUB gives places as 'location N' and has no printed-page box", page.locator("#bk-list .chap-ol li").count() == 3 and "location" in page.inner_text("#bk-list .chap-ol li >> nth=1") and page.locator("[data-chap-offv]").count() == 0 and "no printed pages" in page.inner_text("#bk-list .chaplist"), page.inner_text("#bk-list .chaplist")[:300])
            c2 = bi_["chapters"][1]
            page.click('#bk-list [data-chap-open] >> nth=1'); page.wait_for_selector(".rd-band .textLayer span", timeout=60000); page.wait_for_function("document.querySelector('#rd-num') && +document.querySelector('#rd-num').value === %d" % c2["start"], timeout=20000)
            check("Open on a chapter opens the book at that chapter's first page", int(page.input_value("#rd-num")) == c2["start"])
            page.click('.rd-bar [data-a="back"]'); page.wait_for_selector("#bk-add"); page.fill("#bk-q", "Platforms and Their Governance"); page.click("#bk-list [data-chap]"); page.wait_for_selector("#bk-list .chap-ol li")
            page.click('#bk-list [data-chap-find="0"]'); page.wait_for_selector("#bk-scope:not(.hide)")
            check("'Search in it' shows that the search is limited to that chapter", "Chapter 1" in page.inner_text("#bk-scope") and "Platforms and Their Governance" in page.inner_text("#bk-scope"))
            page.fill("#bk-q", '"of chapter 3"'); page.click("#bk-ft"); page.wait_for_selector("#bk-out .reshead", timeout=60000); page.wait_for_function("document.querySelector('#bk-out .empty') || document.querySelector('#bk-out .ft-i')", timeout=60000)
            check("words that are only in a later chapter are not found inside Chapter 1", page.locator("#bk-out .ft-i").count() == 0 and "Not found" in page.inner_text("#bk-out"))
            page.click('#bk-list [data-chap-find="1"]'); page.fill("#bk-q", '"of chapter 2"'); page.click("#bk-ft"); page.wait_for_selector("#bk-out .ft-i", timeout=60000)
            heads = page.locator("#bk-out .ft-h").all_inner_texts(); locs = [int(h_.rsplit("location ", 1)[1]) for h_ in heads]
            check("…inside Chapter 2 its own words are found, each hit named by chapter and place, all within its pages (a page shared with the next chapter is named by the later one), and more than the usual three passages",
                  len(heads) > 3 and all(c2["start"] <= n_ <= c2["end"] for n_ in locs) and any("Chapter 2 · location " in h_ for h_ in heads) and "chapter 2" in page.inner_text("#bk-out .reshead").lower(), "locations %s, chapter 2 is %s-%s, %d hits" % (sorted(set(locs)), c2["start"], c2["end"], len(heads)))
            page.click("#bk-scope-x"); page.fill("#bk-q", '"of chapter 3"'); page.click("#bk-ft"); page.wait_for_selector("#bk-out .ft-i", timeout=60000)
            check("clearing the chapter searches all the books again", page.locator("#bk-scope.hide").count() == 1 and "Chapter 3 · location " in page.inner_text("#bk-out .ft-h >> nth=0"))
            # a PDF book: file pages until you say where printed page 1 is
            _dc = _pm.open()
            for i_ in range(12):
                _pc = _dc.new_page()
                if i_ == 0:
                    _pc.insert_textbox(_pm.Rect(60, 60, 535, 200), "Charlie study of institutional logics in open collaboration communities and their rules.", fontsize=16); _pc.insert_textbox(_pm.Rect(60, 210, 535, 240), "Cy Charlie and Di Delta", fontsize=10)
                else:
                    _pc.insert_textbox(_pm.Rect(60, 60, 535, 700), "Page %d of the handbook on logics. " % (i_ + 1) * 20, fontsize=11)
            _dc.embfile_add("padding", os.urandom(45000)); charlie = base64.b64encode(_dc.tobytes()).decode(); _dc.close()
            page.evaluate(JS_DROP, ["#lib-books", [PDF(charlie, "charlie-book.pdf")], False]); page.wait_for_selector("#ir-dlg[open]"); page.wait_for_selector("#ir-c0 .ir-f", timeout=30000)
            page.fill('[data-k="title"]', "Institutional Logics Handbook"); page.click("#ir-go"); page.wait_for_selector(".di-snack:has-text('Added 1 book')", timeout=30000)
            pdf_row = next(r_ for r_ in api("/api/library") if r_["title"] == "Institutional Logics Handbook")
            page.fill("#bk-q", "Institutional Logics"); page.wait_for_selector('#bk-list .librow:has-text("Institutional Logics Handbook")'); page.click("#bk-list [data-chap]"); page.wait_for_selector("[data-chap-offv]", timeout=10000)
            check("a PDF book without an outline says so and offers 'Printed page 1 is file page …' (file pages until you set it)", "no outline" in page.inner_text("#bk-list .chaplist") and page.input_value("[data-chap-offv]") == "1" and "file pages" in page.inner_text("#bk-list .chaplist"))
            page.fill("[data-chap-offv]", "0"); page.click("[data-chap-off]"); page.wait_for_function("document.querySelector('.chap-off .st-msg').textContent.length > 0")
            check("a printed-page position that cannot be right is refused with a reason and nothing changes", "between 0 and" in page.inner_text(".chap-off .st-msg") and api("/api/book?file=" + urllib.parse.quote(pdf_row["file"]))["offset"] == 0)
            page.fill("[data-chap-offv]", "13"); page.click("[data-chap-off]"); page.wait_for_function("document.querySelector('.chap-off .st-msg').textContent.includes('between')")
            check("…also past the last page", "between 0 and 11" in page.inner_text(".chap-off .st-msg") and api("/api/book?file=" + urllib.parse.quote(pdf_row["file"]))["offset"] == 0)
            page.fill("[data-chap-offv]", "5"); page.click("[data-chap-off]"); page.wait_for_function("document.querySelector('.chap-off .st-msg').textContent === 'Saved.'")
            check("'printed page 1 is file page 5' is kept (offset 4) and the page now says how printed pages relate to file pages", api("/api/book?file=" + urllib.parse.quote(pdf_row["file"]))["offset"] == 4 and "printed page = file page − 4" in page.inner_text("#bk-list .chaplist") and page.input_value("[data-chap-offv]") == "5")

            print("book chapters: ask and summary")
            chat_calls = lambda marker: [b_ for pth_, b_ in SEEN if pth_.endswith("/chat/completions") and b_["messages"] and b_["messages"][0]["role"] == "system" and b_["messages"][0]["content"].startswith(marker)]
            page.goto(base + "/"); page.click("#libitem"); page.wait_for_selector("#lf-q"); page.wait_for_function("document.querySelectorAll('#lf-folder option').length >= 2")
            page.click('.libtabs [data-tab="books"]'); page.wait_for_selector("#bk-list .librow"); page.fill("#bk-q", "Platforms and Their Governance"); page.wait_for_selector('#bk-list .librow:has-text("Platforms and Their Governance")')
            page.click("#bk-list [data-chap]"); page.wait_for_selector("#bk-list .chap-ol li", timeout=10000)
            n_sum0 = len(chat_calls("You summarise ONE chapter"))
            page.click('#bk-list [data-chap-sum="1"]'); page.wait_for_selector("#bk-list .sum-est", timeout=10000)
            est_txt = page.inner_text("#bk-list .sum-est")
            check("'Summarise…' shows what it would do and what it costs BEFORE anything is sent (provider and model, pages, cost), with a way to cancel", "fake · m1" in est_txt and "Cost:" in est_txt and "page" in est_txt and page.locator("[data-sum-go]").count() == 1 and page.locator("[data-sum-no]").count() == 1 and len(chat_calls("You summarise ONE chapter")) == n_sum0, est_txt)
            page.click("[data-sum-no]"); page.wait_for_function("document.querySelector('[data-sumbox=\"1\"]').classList.contains('hide')")
            check("cancelling asks nothing and leaves the chapter as it was", len(chat_calls("You summarise ONE chapter")) == n_sum0 and "Summarise" in page.inner_text('[data-chap-sum="1"]'))
            page.click('#bk-list [data-chap-sum="1"]'); page.wait_for_selector("[data-sum-go]"); page.click("[data-sum-go]"); page.wait_for_selector("#bk-list .chap-sum .pmeta:has-text('Written by')", timeout=60000)
            sum_txt = page.inner_text('[data-sumbox="1"]')
            check("pressing 'Write the summary' makes ONE request, shows the summary with its pages said as locations (an EPUB), who wrote it and what it cost", len(chat_calls("You summarise ONE chapter")) == n_sum0 + 1 and "FAKE CHAPTER SUMMARY" in sum_txt and "(location 5)" in sum_txt and "(location 6 – location 7)" in sum_txt and "(p. 5)" not in sum_txt and "Written by m1" in sum_txt, sum_txt[:300])
            check("…and the button now says Summary", page.inner_text('[data-chap-sum="1"]').strip() == "Summary")
            page.goto(base + "/"); page.click("#libitem"); page.wait_for_selector("#lf-q"); page.wait_for_function("document.querySelectorAll('#lf-folder option').length >= 2")
            page.click('.libtabs [data-tab="books"]'); page.wait_for_selector("#bk-list .librow"); page.fill("#bk-q", "Platforms and Their Governance"); page.wait_for_selector('#bk-list .librow:has-text("Platforms and Their Governance")')
            page.click("#bk-list [data-chap]"); page.wait_for_selector("#bk-list .chap-ol li"); n_sum1 = len(chat_calls("You summarise ONE chapter"))
            try:                                                                    # (the Library can redraw its list once more after loading: wait for the chapter list to settle)
                page.wait_for_function("(() => { const b = document.querySelector('#bk-list [data-chap-sum=\"1\"]'); return b && b.textContent.trim() === 'Summary'; })()", timeout=10000)
            except Exception:  # noqa: BLE001
                pass
            check("after a reload the summary is still there (kept with the book) and is shown without asking again", page.inner_text('[data-chap-sum="1"]').strip() == "Summary" and "FAKE CHAPTER SUMMARY" in page.inner_text('[data-sumbox="1"]') and page.locator('[data-sumbox="1"].hide').count() == 0 and len(chat_calls("You summarise ONE chapter")) == n_sum1)
            page.click('#bk-list [data-chap-sum="1"]'); page.click('#bk-list [data-chap-sum="1"]')
            check("opening and closing it again sends nothing", len(chat_calls("You summarise ONE chapter")) == n_sum1)
            page.click("[data-chap-redo]"); page.wait_for_selector("[data-sum-go]"); page.click("[data-sum-go]"); page.wait_for_function("document.querySelector('[data-sumbox=\"1\"] .pmeta') && document.querySelector('[data-sumbox=\"1\"] .pmeta').textContent.includes('Written by')", timeout=60000)
            check("'Write it again' asks again (after showing the cost again)", len(chat_calls("You summarise ONE chapter")) == n_sum1 + 1)
            # ask about one chapter: the reader's Ask tab opens limited to it
            page.click('#bk-list [data-chap-ask="1"]'); page.wait_for_selector(".rd-band .textLayer span", timeout=60000); page.wait_for_selector("#ask-chapsel", timeout=10000)
            check("'Ask about it' opens the book in the reader on the Ask tab with that chapter chosen", page.eval_on_selector("#ask-chapsel", "e => e.value") == "1" and page.locator('.rd-tabs button[data-tab="ask"].on').count() == 1 and "Chapter 2" in page.inner_text("#ask-chapsel"), page.inner_text("#ask-chapsel"))
            page.select_option("#ask-mode", "full"); page.fill("#ask-q", "What does this chapter say about rules?"); page.click('.ask-box [data-k="send"]')
            page.wait_for_function("document.querySelector('.ask-turn') && document.querySelector('.ask-turn').innerText.includes('Sources')", timeout=60000)
            ask_req = [b_ for pth_, b_ in SEEN if pth_.endswith("/chat/completions") and "Passages from the paper" in str(b_["messages"][-1]["content"])][-1]
            sent_pages = sorted({int(x_) for x_ in re.findall(r"\(p\. (\d+)\)", ask_req["messages"][-1]["content"])})
            check("the question reaches the model with only that chapter's pages", sent_pages and all(c2["start"] <= n_ <= c2["end"] for n_ in sent_pages), "pages sent %s, chapter %s-%s" % (sent_pages, c2["start"], c2["end"]))
            turn_html = page.locator(".ask-turn").first.inner_html()
            check("…and the answer's sources say where they are in words (chapter · location) and that only the chapter was read", "Only “Chapter 2” was read" in turn_html and "Chapter 2 · location " in turn_html, turn_html[-400:])
            page.click('.rd-bar [data-a="back"]'); page.wait_for_selector("#bk-add")

            print("book: summarise the whole book")
            def open_chapters_():
                page.goto(base + "/"); page.click("#libitem"); page.wait_for_selector("#lf-q"); page.wait_for_function("document.querySelectorAll('#lf-folder option').length >= 2")
                page.click('.libtabs [data-tab="books"]'); page.wait_for_selector("#bk-list .librow"); page.fill("#bk-q", "Platforms and Their Governance"); page.wait_for_selector('#bk-list .librow:has-text("Platforms and Their Governance")')
                page.click("#bk-list [data-chap]"); page.wait_for_selector("#bk-list .chap-ol li"); page.wait_for_selector("#bk-list [data-chap-whole]")
            open_chapters_()
            n_ch0, n_ov0 = len(chat_calls("You summarise ONE chapter")), len(chat_calls("You write an OVERVIEW"))
            saved_ = sum(1 for c_ in api("/api/book?file=" + urllib.parse.quote(ep_row["file"]))["chapters"] if c_.get("summary"))
            check("the Chapters panel offers 'Summarise the whole book…'", "Summarise the whole book" in page.inner_text("#bk-list [data-chap-whole]") and saved_ >= 1, page.inner_text("#bk-list .chap-h"))
            page.click("#bk-list [data-chap-whole]"); page.wait_for_selector("#bk-list .whole-est", timeout=15000)
            we_ = page.inner_text("#bk-list .whole-est")
            check("…pressing it shows what it would do and what it costs BEFORE anything is sent (parts to do, those already done, one overview, provider and model)",
                  "fake · m1" in we_ and "Cost:" in we_ and ("%d parts" % (3 - saved_) in we_ or "1 part" in we_) and "%d of 3 already" % saved_ in we_ and "one overview" in we_
                  and len(chat_calls("You summarise ONE chapter")) == n_ch0 and len(chat_calls("You write an OVERVIEW")) == n_ov0, we_)
            page.click("[data-whole-no]"); page.wait_for_function("document.querySelector('#bk-list [data-whole]').classList.contains('hide')")
            check("…Cancel sends nothing", len(chat_calls("You summarise ONE chapter")) == n_ch0 and len(chat_calls("You write an OVERVIEW")) == n_ov0)
            page.click("#bk-list [data-chap-whole]"); page.wait_for_selector("[data-whole-go]"); page.click("[data-whole-go]")
            ok_ = False
            for _ in range(160):
                st_ = api("/api/book/whole?file=%s&status=1" % urllib.parse.quote(ep_row["file"]))["status"]
                if not st_["running"] and st_.get("ended"):
                    ok_ = True; break
                time.sleep(0.25)
            check("…'Summarise' runs in the background: the parts without a summary once each, then one overview", ok_ and len(chat_calls("You summarise ONE chapter")) - n_ch0 == 3 - saved_
                  and len(chat_calls("You write an OVERVIEW")) - n_ov0 == 1 and st_["overview"] is True and not st_["failed"], str(st_))
            try:
                page.wait_for_selector("#bk-list .chap-overview:not(.hide)", timeout=15000)
            except Exception:  # noqa: BLE001
                pass
            check("…the panel then shows the overview, every part says 'Summary', and it says it finished",
                  "FAKE BOOK OVERVIEW from 3 summaries" in page.inner_text("#bk-list .chap-overview") and all(t_.strip() == "Summary" for t_ in page.locator("#bk-list [data-chap-sum]").all_inner_texts())
                  and "Finished" in page.inner_text("#bk-list [data-whole]"), page.inner_text("#bk-list .chaplist")[:500])
            check("…and it was one job in the AI activity tray", any(j_["kind"] == "summary" and j_["label"].startswith("Book summary") for j_ in api("/api/activity")["jobs"]), str([j_["label"] for j_ in api("/api/activity")["jobs"]])[:300])
            open_chapters_()
            check("after a reload the overview is still there and the button says 'Book summary' (nothing left to do; pressing it sends nothing)",
                  "FAKE BOOK OVERVIEW" in page.inner_text("#bk-list .chap-overview") and page.inner_text("#bk-list [data-chap-whole]").strip() == "Book summary")
            page.click("#bk-list [data-chap-whole]"); page.wait_for_selector("#bk-list [data-whole] .ok", timeout=10000)
            check("…pressing it says everything is done, and asks nothing", len(chat_calls("You write an OVERVIEW")) - n_ov0 == 1 and "up to date" in page.inner_text("#bk-list [data-whole]"))

            print("command palette (Ctrl+K)")
            page.goto("about:blank"); page.goto(base + "/#/watch"); page.wait_for_selector("#libitem"); page.wait_for_function("!document.querySelector('#palettebtn').disabled")
            page.wait_for_selector("#sbfolders .sbf")      # the palette lists the folders the page has loaded by then
            page.keyboard.press("Control+k"); page.wait_for_selector("#pal:not(.hide)")
            check("Ctrl+K opens the palette; with nothing typed it offers pages, folders and actions", page.locator("#pal .pal-it").count() >= 8 and all(x in page.inner_text("#pal-list") for x in ("Library", "Folders", "Lab", "Settings")) and page.evaluate("document.activeElement.id") == "pal-q", page.evaluate("[document.querySelectorAll('#pal .pal-it').length, document.activeElement.id, document.querySelector('#pal-list').innerText.slice(0, 160)]"))
            page.keyboard.type("complement")
            check("typing finds a paper by words in its title, with the match marked", "Open Platforms and Complementors" in page.inner_text("#pal-list") and page.locator("#pal mark").count() >= 1)
            page.keyboard.press("Enter"); page.wait_for_selector(".rd-band .textLayer span", timeout=30000)
            check("Enter opens it in the reader", page.locator("#pal.hide").count() == 1 and "Open Platforms" in page.inner_text("#rd-title"))
            page.wait_for_timeout(350)
            check("while you read, the sidebar gives its place to the paper (the reader starts at the left edge)", not page.locator("#sidebar").is_visible() and page.evaluate("document.querySelector('#reader').getBoundingClientRect().left") == 0)
            page.keyboard.press("Control+k"); page.wait_for_selector("#pal:not(.hide)"); page.keyboard.type("lab matrix")
            check("a folder's tools are listed by name ('Lab › Matrix')", "Lab › Matrix" in page.inner_text("#pal .pal-it >> nth=0"), page.inner_text("#pal-list")[:200])
            page.keyboard.press("Enter"); page.wait_for_selector("table.mx", timeout=20000)
            check("from inside the reader it closes the reader and opens that folder's tab", page.url.endswith("#/folder/lab/matrix") and page.evaluate("!document.body.classList.contains('reading')"), page.url)
            page.keyboard.press("Control+k"); page.keyboard.type("settings usage"); page.keyboard.press("Enter"); page.wait_for_selector("#st-body .st-stat")
            check("Settings › Usage & cost opens that tab", "usage" in page.inner_text(".st-sub .on").lower() and page.inner_text(".st-tabs .on").strip() == "AI")
            JS_TABROW = "(() => { const b = [...document.querySelectorAll('.st-tabs button')]; const tops = new Set(b.map(x => Math.round(x.getBoundingClientRect().top))); return [b.length, tops.size]; })()"
            rows_ = {}
            for w_ in (1000, 1200, 1500, 2000):
                page.set_viewport_size({"width": w_, "height": 900}); page.wait_for_timeout(150); rows_[w_] = page.evaluate(JS_TABROW)
            page.set_viewport_size({"width": 1400, "height": 950})
            check("the eight Settings tabs are always in one row, aligned (a window too narrow for them scrolls the row; no tab sits alone on a second line)", all(n == 8 and r == 1 for n, r in rows_.values()), str(rows_))
            page.keyboard.press("Control+k"); page.wait_for_selector("#pal:not(.hide)"); page.keyboard.press("Escape")
            check("Esc closes the palette", page.locator("#pal.hide").count() == 1)
            page.click("#settingsbtn"); page.wait_for_selector("#settings.hide", state="attached")
            th0 = page.evaluate("getComputedStyle(document.body).backgroundColor"); page.keyboard.press("Control+k"); page.keyboard.type("dark light"); page.keyboard.press("Enter"); page.wait_for_timeout(300)
            check("the 'Switch between light and dark' action works", page.evaluate("getComputedStyle(document.body).backgroundColor") != th0)
            page.click("#themebtn")
            page.keyboard.press("Control+k"); page.keyboard.type("Transparent governance")
            page.wait_for_selector("#pal .pal-g:has-text('Mentioned')", timeout=10000)
            check("text from your highlights and notes is searched too, and shown with where it was found", "highlight" in page.inner_text("#pal-list").lower() and "Open Platforms" in page.inner_text("#pal-list"))
            page.keyboard.press("Escape"); page.keyboard.press("Control+k"); page.keyboard.type("Open Platforms"); page.keyboard.press("Alt+Enter"); page.wait_for_selector(".rd-band .textLayer span", timeout=30000)
            check("Alt+Enter opens a paper at its card", page.locator('.rd-tabs button[data-tab="card"].on').count() == 1)
            page.click('#reader [data-a="back"]'); page.wait_for_selector("#palettebtn"); page.click("#palettebtn"); page.wait_for_selector("#pal:not(.hide)")
            check("the Search box at the top of the sidebar opens it too", page.locator("#pal .pal-it").count() >= 8); page.keyboard.press("Escape")

            print("AI activity tray: progress, cancel, background indexing")
            page.goto("about:blank"); page.goto(base + "/#/library"); page.wait_for_selector("#lf-q"); page.wait_for_function("!document.querySelector('#aibtn').disabled"); page.click("#lf-list"); page.wait_for_selector("#lib-results .librow")
            page.locator('#lib-results .librow:has-text("Chatbots and Customer Trust") [data-read]').click(); page.wait_for_selector(".rd-band .textLayer span", timeout=30000)
            page.click('.rd-tabs button[data-tab="ask"]'); page.fill("#ask-q", "SLOW please tell me everything"); page.click('#reader [data-k="send"]')
            page.wait_for_selector("#aibtn.busy", timeout=10000)
            check("while an answer is being written the top bar says the AI is running", "1 running" in page.inner_text("#aibtn"))
            page.click("#aibtn"); page.wait_for_selector("#aitray:not(.hide) .at-job.live")
            check("the tray lists the running job, with a Cancel button", "Ask · SLOW" in page.inner_text("#aitray") and page.locator("#aitray [data-at=cancel]").count() == 1)
            page.wait_for_timeout(900); page.click("#aitray [data-at=cancel]")
            page.wait_for_selector("#aitray .at-job.cancelled", timeout=10000)
            check("Cancel stops it: the job moves to Recent as Cancelled and the answer says so", "Cancelled" in page.inner_text("#aitray .at-job.cancelled") and "Cancelled" in page.inner_text(".ask-turn .errbox.cancelled"), page.inner_text("#aitray")[:200])
            lg = api("/api/ai/log?limit=5")
            check("a cancelled call still lands in the cost ledger, as an estimate", any(r.get("cancelled") and r.get("estimated") and r.get("out", 0) > 0 and r.get("cost", 0) > 0 for r in lg), str(lg)[:300])
            page.fill("#ask-q", "How many firms are on the platform?"); page.click('#reader [data-k="send"]'); page.wait_for_selector(".ask-turn [data-t='regen']", timeout=30000); page.click("#aibtn"); page.wait_for_selector("#aitray:not(.hide) .at-job.done")
            check("a finished job stays in the list with what it cost and a way back to it", page.locator("#aitray .at-job.done").count() >= 1 and page.locator("#aitray .at-job.done [data-at=open]").count() >= 1 and "$" in page.inner_text("#aitray .at-job.done .at-c"))
            api("/api/ai/config", {"budget": {"monthly_usd": 0.0001}})
            n_t = page.locator(".ask-turn").count(); page.fill("#ask-q", "Does local-only stop this?"); page.click('#reader [data-k="send"]'); page.wait_for_selector(".ask-turn >> nth=-1 >> .errbox.budget", timeout=15000)
            check("an AI problem is a box that says what to do: here the monthly budget is used up, with buttons to the usage and the AI settings, and Try again", "budget" in page.inner_text(".ask-turn >> nth=-1 >> .errbox").lower() and page.locator(".ask-turn >> nth=-1 >> .errbox [data-eb=settings]").count() == 1 and page.locator(".ask-turn >> nth=-1 >> .errbox [data-eb=usage]").count() == 1 and page.locator(".ask-turn >> nth=-1 >> .errbox [data-t=regen]").count() == 1, page.inner_text(".ask-turn >> nth=-1 >> .errbox"))
            shot(page, "25-error-box")
            page.click(".ask-turn >> nth=-1 >> .errbox [data-eb=settings]"); page.wait_for_selector("#st-body tr[data-p]")
            check("…and the button opens Settings → AI & privacy", "ai & privacy" in page.inner_text(".st-sub .on").lower())
            gem = page.locator('tr[data-p="vault"]'); had_key = "key found" in gem.inner_text()
            check("a provider without a key offers a field to paste one (and no text that says to edit .env by hand)", not had_key and gem.locator('.keyform input[type=password]').count() == 1 and gem.locator('[data-savekey]').count() == 1)
            gem.locator('.keyform input').fill("AIza-test-key-123"); gem.locator("[data-savekey]").click(); page.wait_for_selector('tr[data-p="vault"] :text("key found")', timeout=10000)
            envp = os.path.join(TMP, ".env"); envt = open(envp).read() if os.path.exists(envp) else ""
            check("Save key writes it to the .env file of the data folder, readable by you only, and the page shows ✓ key found but never the key", "VAULT_API_KEY=AIza-test-key-123" in envt and (os.stat(envp).st_mode & 0o077) == 0 and "AIza-test-key-123" not in page.content() and api("/api/ai/config")["providers"]["vault"]["configured"], envt[-80:])
            res_ = urllib.request.Request(base + "/api/ai/key", data=json.dumps({"provider": "vault", "key": "has space"}).encode(), headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(res_); bad_key = False
            except urllib.error.HTTPError as e_:
                bad_key = e_.code == 400
            check("a key with spaces or quotes is refused", bad_key)
            page.click("#st-back")
            api("/api/ai/config", {"budget": {"monthly_usd": None}})
            page.click(".ask-turn >> nth=-1 >> .errbox [data-t=regen]"); page.wait_for_function("document.querySelectorAll('.ask-turn').length === %d && !document.querySelector('.ask-turn:last-of-type .errbox') && document.querySelector('.ask-turn:last-of-type .of')" % (n_t + 1), timeout=30000)
            check("Try again asks again and the answer replaces the box", page.locator(".ask-turn >> nth=-1 >> .errbox").count() == 0)
            page.goto("about:blank"); page.goto(base + "/#/library"); page.wait_for_selector("#lf-q"); page.wait_for_function("!document.querySelector('#aibtn').disabled")
            idx0 = api("/api/activity?pending=1")["indexing"]
            check("the server reports the index: this test uses a paid-style embedding model, so nothing starts by itself", idx0["local"] is False and idx0["pending"] >= 1 and not idx0["running"], str(idx0))
            page.click("#aibtn"); page.wait_for_selector("#aitray [data-at=index]"); page.wait_for_function("document.querySelector('#aitray .at-ix').innerText.includes('still to read')")
            check("the tray offers Index now, and says the automatic option needs the local model", "still to read" in page.inner_text("#aitray .at-ix") and page.locator("#aitray input[data-at=auto]").is_disabled())
            page.click("#aitray [data-at=index]"); page.wait_for_selector("#aitray .at-job[data-tool=library]", timeout=15000)
            page.wait_for_function("document.querySelector('#aitray .at-job[data-tool=library].done')", timeout=60000)
            idx1 = api("/api/activity?pending=1")["indexing"]
            check("Index now reads the unindexed papers as one job with progress; afterwards nothing is left", idx1["pending"] == 0 and "up to date" in page.inner_text("#aitray .at-ix") or idx1["pending"] == 0, str(idx1))
            page.keyboard.press("Escape"); page.mouse.click(5, 400)

            print("updates: every kind of problem is worded for people")
            tbl = page.evaluate("""async () => { const m = await import('/static/state.js');
              const base = {feed: true, auto: true, source: 'built-in', current: '1.0.0', latest: '', available: false, checked: '2026-10-29T10:00:00+00:00', checked_ok: false, error: 'raw urllib text', problem: null, no_release: false, feed_url: 'https://x/y'};
              const kinds = ['not_found', 'denied', 'busy', 'http', 'offline', 'timeout', 'tls', 'garbled', 'unsigned', 'bad_signature', 'no_verifier', 'address', 'other'];
              const row = (k, over = {}) => { const U = {...base, ...over, problem: {kind: k, code: 418, url: ''}}; const x = m.explainUpdate(U); return {k, kind: x.kind, tone: x.tone, title: x.title, detail: x.detail, toast: m.updateToast(U)}; };
              return {on: kinds.map(k => row(k)), off: ['busy', 'offline', 'unsigned', 'not_found'].map(k => row(k, {auto: false})), norel: row('not_found', {no_release: true}), norel_off: row('not_found', {no_release: true, auto: false}),
                      yours: row('not_found', {source: 'yours'}), current: m.explainUpdate({...base, checked_ok: true, error: ''}), never: m.explainUpdate({...base, checked: ''}), nofeed: m.explainUpdate({...base, feed: false})}; }""")
            on, tones = tbl["on"], {"bad_signature": "bad", "no_verifier": "bad", "other": "bad", "unsigned": "neutral"}
            check("every kind of update problem has its own plain title, a tone (red only for a bad signature or the unexpected, grey for 'not signed yet'), no raw error text, and a toast short enough to read in 4 seconds",
                  len({r["title"] for r in on}) == len(on) and all(r["kind"] == r["k"] and r["tone"] == tones.get(r["k"], "info") and r["title"] and r["detail"] and "urllib" not in r["detail"] + r["title"] and "HTTP Error" not in r["detail"] + r["title"] and len(r["toast"]) <= 110 for r in on), str(on)[:600])
            check("sentences that promise something by itself are only said when the automatic check is on; a typed address is told to check it; 'no update found yet' is neutral and says nothing is needed",
                  all(not re.search(r"tries again|by itself every|every six hours|will be offered|button appears", r["detail"]) for r in tbl["off"]) and "Update address" in tbl["yours"]["detail"] and tbl["norel"]["kind"] == "no_release" and tbl["norel"]["tone"] == "neutral" and "button appears" in tbl["norel"]["detail"] and "button appears" not in tbl["norel_off"]["detail"]
                  and tbl["current"]["title"] == "You have the newest version (1.0.0)." and "newest version" not in tbl["never"]["title"] and tbl["nofeed"]["kind"] == "no_feed", str(tbl)[:600])
            print("updates: a new version is announced, with what is new, and you choose")
            page.goto("about:blank"); page.goto(base + "/#/library"); page.wait_for_selector("#lf-q"); page.wait_for_timeout(1800)
            check("with no news the Update button is not shown", page.locator("#updatepill:not(.hide)").count() == 0 and api("/api/update")["available"] is False)
            def open_about():
                page.click("#settingsbtn"); page.wait_for_selector("#st-body"); page.click('.st-tabs button[data-t="about"]'); page.wait_for_selector("#ab-go")
            open_about()
            t0 = page.inner_text("#st-body")
            check("before the first look at the feed, Settings → About does not claim 'the newest version'; it says it has not looked yet", "Haven’t looked for updates yet" in t0 and "newest version" not in t0 and api("/api/update")["checked"] == "", t0[:500])
            fmode["mode"] = 404
            page.click("#ab-go"); page.wait_for_function("document.querySelector('#ab-status') && /No update found yet/.test(document.querySelector('#ab-status').textContent)", timeout=10000)
            t1, u1 = page.inner_text("#st-body"), api("/api/update")
            check("a 404 from the built-in release address that never answered is said calmly ('No update found yet'): nothing red, no raw 'HTTP Error', no 'newest version', and the toast says the same",
                  u1["no_release"] is True and u1["problem"]["kind"] == "not_found" and u1["problem"]["code"] == 404 and "HTTP Error" not in t1 and "newest version" not in t1 and page.locator("#ab-problem").count() == 0
                  and page.locator("#st-body .hint.bad").count() == 0 and "No update found yet" in page.inner_text("#toast"), t1[:500])
            page.click("#ab-facts summary")
            check("…and the facts are under 'Details': what the server answered and the address asked", "HTTP 404" in page.inner_text("#ab-facts") and "releases/latest/download/appcast.json" in page.inner_text("#ab-facts") and "Last attempt" in page.inner_text("#ab-facts"), page.inner_text("#ab-facts"))
            check("…and the Update button stays hidden", page.locator("#updatepill:not(.hide)").count() == 0)
            fmode["mode"] = "ok"; page.keyboard.press("Escape"); page.wait_for_selector("#settings.hide", state="attached")
            u = api("/api/update/check", {})
            check("checking the feed finds version 9.9.9, with its notes and the history since yours", u["available"] and u["latest"] == "9.9.9" and "welcome guide" in " ".join(u["notes"]) and u["history"][0]["version"] == "9.9.8" and u["current"] == "1.0.0", str(u))
            page.evaluate("JWUpdate.refresh()"); page.wait_for_selector("#updatepill:not(.hide)")
            check("the top bar offers 'Update to 9.9.9'", "9.9.9" in page.inner_text("#updatepill"))
            page.click("#updatepill"); page.wait_for_selector("dialog.upd[open]")
            check("its dialog says what is new (this version and the one skipped over) and offers Update, Remind me later and Skip", "9.9.9" in page.inner_text("dialog.upd h2") and "Update notices in the top bar" in page.inner_text("dialog.upd") and "Calmer dark theme" in page.inner_text("dialog.upd") and page.locator('dialog.upd [data-u="go"]').count() == 1 and page.locator('dialog.upd [data-u="skip"]').count() == 1 and page.locator('dialog.upd [data-u="later"]').count() == 1)
            check("a copy that is not the Mac app says it cannot install itself, rather than pretending", "can’t install itself" in page.inner_text("dialog.upd"))
            page.click('dialog.upd [data-u="go"]'); page.wait_for_selector("dialog.upd .errbox", timeout=20000)
            check("pressing it anyway downloads and checks the file, and says plainly why it was refused", "didn’t work" in page.inner_text("dialog.upd .errbox") and "not a Journal Watcher app" in page.inner_text("dialog.upd .errbox"), page.inner_text("dialog.upd"))
            feed["sha256"] = "0" * 64; api("/api/update/check", {}); r_ = api("/api/update/install", {})
            for _ in range(40):
                if api("/api/update")["progress"]["phase"] == "error":
                    break
                time.sleep(0.25)
            check("a download whose checksum does not match the feed is refused as damaged", "damaged" in api("/api/update")["progress"]["error"], str(api("/api/update")["progress"]))
            feed["sha256"] = hashlib.sha256(zipb).hexdigest(); api("/api/update/check", {})
            page.keyboard.press("Escape"); page.wait_for_selector("dialog.upd:not([open])", state="attached")
            check("'Remind me later' closes it and the button stays in the top bar", page.locator("#updatepill:not(.hide)").count() == 1)
            page.click("#updatepill"); page.wait_for_selector("dialog.upd[open]"); page.click('dialog.upd [data-u="skip"]'); page.wait_for_selector("#updatepill.hide", state="attached")
            check("'Skip this version' hides the button and remembers it (the file in the data folder says which)", api("/api/update")["skipped"] is True and json.load(open(os.path.join(TMP, ".agents", "skills", "ais-journal-watcher", "update.json")))["skipped"] == "9.9.9")
            api("/api/update/settings", {"skipped": ""})
            page.click("#settingsbtn"); page.wait_for_selector("#st-body"); page.click('.st-tabs button[data-t="about"]'); page.wait_for_selector("#ab-go")
            page.wait_for_function("/9[.]9[.]9/.test(document.querySelector('#st-body').textContent)")        # (the card from the early block above is drawn again by the click)
            check("Settings → About & updates shows the version, where your data lives and what the update status is", "1.0.0" in page.inner_text("#st-body") and TMP in page.inner_text("#st-body") and "9.9.9" in page.inner_text("#st-body"), page.inner_text("#st-body")[:300])
            check("…and says whether the update's digital signature is checked (this test copy has no signing key built in, so it says it only checks the checksum)", "does not check" in page.inner_text("#ab-sig") and api("/api/update")["signed"] is False, page.inner_text("#ab-sig"))
            feed["version"] = "1.0.0"; api("/api/update/check", {}); page.click('.st-tabs button[data-t="about"]'); page.wait_for_selector("#ab-go")
            page.wait_for_function("/newest version/.test((document.querySelector('#ab-status') || {}).textContent || '')")
            check("a feed that names the version you have is 'the newest version', with when it was checked (and no problem box)", "You have the newest version (1.0.0)." in page.inner_text("#ab-status") and page.locator("#ab-problem").count() == 0 and api("/api/update")["checked_ok"] is True, page.inner_text("#ab-status"))
            page.click("#ab-go"); page.wait_for_selector("#toast:has-text('You have the newest version (1.0.0).')", timeout=8000)
            fmode["mode"] = 403
            page.click("#ab-go"); page.wait_for_selector("#ab-problem", timeout=10000)
            t2, u2 = page.inner_text("#st-body"), api("/api/update")
            check("when the feed refuses (403) after it had worked, the card says so in plain words in an amber notice and does not claim 'the newest version'; the raw error is not the headline",
                  "wouldn’t hand over the file" in page.inner_text("#ab-problem") and page.locator("#ab-problem.net").count() == 1 and page.locator("#ab-status").count() == 0 and "newest version" not in t2
                  and "HTTP Error" not in page.inner_text("#ab-problem") and u2["problem"]["kind"] == "denied" and u2["checked_ok"] is False and "wouldn’t hand over the file" in page.inner_text("#toast"), t2[:500])
            page.click("#ab-facts summary")
            check("…with the facts under 'Details' (HTTP 403, the raw message, the address)", "HTTP 403" in page.inner_text("#ab-facts") and "Forbidden" in page.inner_text("#ab-facts") and "appcast.json" in page.inner_text("#ab-facts"), page.inner_text("#ab-facts"))
            fmode["mode"] = "evil"
            page.click("#ab-go"); page.wait_for_function("document.querySelector('#ab-facts') && /svg onload/.test(document.querySelector('#ab-facts').textContent)", timeout=10000)
            page.evaluate("document.querySelector('#ab-facts').open = true")
            check("whatever the server writes in its status line is shown as text, never as markup (Details, headline and toast)", page.locator("#ab-facts svg, #ab-problem svg:not(.ico)").count() == 0 and page.evaluate("window.__pwned") is None and "<svg onload" in page.inner_text("#ab-facts"))
            fmode["mode"] = "garbage"
            page.click("#ab-go"); page.wait_for_function("document.querySelector('#ab-problem') && /can’t read/.test(document.querySelector('#ab-problem').textContent)", timeout=10000)
            check("a web page where the feed should be (a Wi-Fi sign-in page, say) reads 'sent something the app can’t read', not a parser message", "Expecting value" not in page.inner_text("#ab-problem") and page.locator("#ab-problem.net").count() == 1 and "Details" in page.inner_text("#ab-updblock"))
            fmode["mode"] = 404
            page.evaluate("JWUpdate.show(true)"); page.wait_for_function("/doesn’t lead to an update file/.test(document.querySelector('#toast').textContent)", timeout=10000)
            page.wait_for_function("document.querySelector('#ab-problem') && /could not find the file/.test(document.querySelector('#ab-problem').textContent)", timeout=5000)
            check("Help → Check for Updates after a failure says the same plain thing (not 'HTTP Error 404'), redraws the Settings card that is open behind it (no click on its button needed), and once the feed has worked a 404 is a problem and not 'nothing published yet'",
                  "HTTP Error" not in page.inner_text("#toast") and api("/api/update")["no_release"] is False and page.locator("#updatepill:not(.hide)").count() == 0 and page.locator("dialog.upd[open]").count() == 0, page.inner_text("#toast"))
            feed["version"] = "9.9.9"; fmode["mode"] = "ok"; api("/api/update/check", {}); page.click('.st-tabs button[data-t="about"]'); page.wait_for_selector("#ab-go")
            page.uncheck("#ab-auto"); page.wait_for_timeout(300)
            check("the automatic check can be switched off, and stays off", api("/api/update")["auto"] is False); page.check("#ab-auto"); page.wait_for_timeout(300)
            page.click("#ab-welcome"); page.wait_for_selector("dialog.wl[open]")
            check("…and the welcome guide can be opened again from there", "Welcome to Journal Watcher" in page.inner_text("dialog.wl h2") and page.locator("#settings.hide").count() == 1)
            page.keyboard.press("Escape")

            print("the welcome guide: API keys, publisher sign-ins, a map of the app")
            api("/api/library/access", {"name": "Test University Library", "mode": "openathens", "openathens": "testuni.ac.uk", "ill_email": "ill@testuni.ac.uk"})
            page.wait_for_function("!document.querySelector('dialog[open]')"); page.wait_for_timeout(300)           # the close handler (it marks the guide as seen) runs a moment after Escape
            page.evaluate("sessionStorage.setItem('jw-wl-test', '1'); localStorage.removeItem('jw-welcome')"); page.goto("about:blank"); page.goto(base + "/#/watch")
            try:
                page.wait_for_selector("dialog.wl[open]", timeout=15000)
            except Exception:  # noqa: BLE001
                print("DEBUG welcome:", page.evaluate("JSON.stringify({seen: localStorage.getItem('jw-welcome'), wl: sessionStorage.getItem('jw-wl-test'), ready: document.readyState, open: [...document.querySelectorAll('dialog[open]')].map(d => d.className), welcome: typeof window.Welcome})"), "|", errors[:3]); raise
            check("on a first start the guide opens by itself at Welcome, and says where your data lives and that nothing goes to a server of ours", "Welcome to Journal Watcher" in page.inner_text("dialog.wl h2") and TMP in page.inner_text("dialog.wl") and "no server" not in page.inner_text("dialog.wl").lower() and "there is none" in page.inner_text("dialog.wl"))
            check("it shows five steps", page.locator("dialog.wl .wl-dots li").count() == 5)
            page.click('dialog.wl [data-wl="next"]')
            t_ = page.inner_text("dialog.wl")
            check("step 2 explains what an API key is, what it costs, and how to get one", "API key" in t_ and "secret code" in t_ and "You pay the service directly" in t_ and "Create key" in t_)
            page.select_option("#wl-prov", "openrouter")
            check("each service has its own keys page, opened in your browser", "openrouter.ai" in page.inner_text("dialog.wl") and page.locator('dialog.wl a[target="_blank"][href="https://openrouter.ai/keys"]').count() == 1)
            check("…and it says where the key is kept and that it is never shown again", "readable only by you" in page.inner_text("dialog.wl") and ".env" in page.inner_text("dialog.wl"))
            page.fill("#wl-key", "has space"); page.click("#wl-save"); page.wait_for_selector("#wl-keymsg.bad, #wl-keymsg .bad", timeout=10000)
            check("a pasted key with a space in it is refused with an explanation", "doesn’t look like an API key" in page.inner_text("#wl-keymsg"))
            page.fill("#wl-key", "or-test-key-456"); page.press("#wl-key", "Enter"); page.wait_for_selector("#wl-keymsg:has-text('Saved')", timeout=10000)
            envp = os.path.join(TMP, ".env"); envt = open(envp).read()
            check("Enter in the key field saves it, in the data folder's .env (readable by you only), and the key never appears on the page", "REEF_API_KEY=or-test-key-456" in envt and (os.stat(envp).st_mode & 0o077) == 0 and "or-test-key-456" not in page.content() and api("/api/ai/config")["providers"]["openrouter"]["configured"])
            page.fill("#wl-budget", "5"); page.click("#wl-bsave"); page.wait_for_timeout(500)
            check("the monthly limit can be set in the guide", api("/api/ai/config")["budget"]["monthly_usd"] == 5, str(api("/api/ai/config")["budget"])); api("/api/ai/config", {"budget": {"monthly_usd": None}})
            r_ = page.evaluate(JS_A11Y)
            check("every control in the guide has a name a screen reader can say", not r_["noname"], str(r_["noname"][:4]))
            page.click('dialog.wl [data-wl="next"]')
            t_ = page.inner_text("dialog.wl")
            check("step 3 explains the publisher sign-ins: you type your password into their page, the app never sees it, the session stays on this Mac, any university works, and a green tick shows when it worked", "never sees or stores your password" in t_ and "on this Mac" in t_ and "150 MB" in t_ and "Any" in t_ and "green tick" in t_ and "Example University" not in page.inner_text("dialog.wl .wl-two > div:first-child"))
            check("…with a button each for your library, INFORMS, AIS and other publishers (not pressed here: they open a real browser), and no university built in", page.locator("dialog.wl [data-si]").count() == 4 and {b.get_attribute("data-si") for b in page.locator("dialog.wl [data-si]").all()} == {"library", "informs", "ais", "publishers"})
            page.click('dialog.wl [data-wl="next"]')
            check("step 4 is a map: Watch, Library, Folders, Review, Settings and Search", all(x in page.inner_text("dialog.wl") for x in ("Watch", "Library", "Folders", "Review", "Settings", "Search")))
            page.click('dialog.wl [data-wl="next"]')
            check("the last step lists what is set up (the key just saved) and what is not", "REEF" not in page.inner_text("dialog.wl") and "AI key:" in page.inner_text("dialog.wl") and "saved for" in page.inner_text("dialog.wl") and "Sign-ins:" in page.inner_text("dialog.wl"))
            page.click('dialog.wl [data-wl="back"]'); page.click('dialog.wl [data-go="0"]')
            check("Back and the step names move around freely", "Welcome to Journal Watcher" in page.inner_text("dialog.wl h2"))
            page.click('dialog.wl [data-go="4"]'); page.click('dialog.wl [data-wl="finish"]'); page.wait_for_selector("dialog.wl:not([open])", state="attached")
            check("finishing remembers it", page.evaluate("localStorage.getItem('jw-welcome')") == "done")
            page.reload(); page.wait_for_selector("#lf-q, #libitem", state="attached"); page.wait_for_timeout(2200)
            check("…so the guide does not open by itself again", page.locator("dialog.wl[open]").count() == 0)
            page.evaluate("Welcome.open(1)"); page.wait_for_selector("dialog.wl[open]")
            for scheme in ("light", "dark"):
                page.evaluate("window.setTheme(%r)" % scheme)
                con_ = page.evaluate(JS_CONTRAST, scheme)
                check("the guide's text is readable (WCAG AA) in the %s theme" % scheme, not con_, str(con_[:3]))
            page.evaluate("window.setTheme('light')"); page.keyboard.press("Escape")

            print("sign-ins: a green tick for each one that worked, any university")
            page.evaluate("sessionStorage.setItem('jw-wl-test', '1'); localStorage.setItem('jw-welcome', 'done')")
            api("/api/library/access", {"name": "Test University Library", "mode": "openathens", "openathens": "testuni.ac.uk", "ill_email": "ill@testuni.ac.uk"})
            page.goto("about:blank"); page.goto(base + "/#/library"); page.wait_for_selector("#lf-q"); page.wait_for_timeout(800)
            page.click("#signinbtn"); page.wait_for_selector("#signinmenu:not(.hide) [data-which=informs]")
            t_ = page.inner_text("#signinmenu")
            check("the Sign-ins menu has a row for INFORMS, AIS, your library (by its own name) and other publishers, each saying it is not signed in yet", all(x in t_ for x in ("INFORMS member account", "AIS member account", "Test University Library", "Other publisher sites")) and t_.count("Not signed in") == 4 and "Example University" not in t_, t_)
            check("no tick and no green dot before any sign-in", page.locator("#signinmenu .si-st.ok").count() == 0 and page.locator("#sidot.hide").count() == 1)
            gate = os.path.join(TMP, "signin-gate"); open(gate, "w").close()          # the fake sign-in window finishes at once …
            page.click("#signinmenu [data-which=informs]")
            try:
                page.wait_for_function("document.querySelector('#toast') && /✓ Signed in to INFORMS/.test(document.querySelector('#toast').textContent)", timeout=15000)
            except Exception:  # noqa: BLE001
                print("DEBUG sign-in toast:", page.inner_text("#toast"), "|", json.dumps(api("/api/signins"))[:400], "|", os.path.exists(gate)); raise
            check("when a sign-in window finishes, a message with a tick says so", "✓ Signed in" in page.inner_text("#toast"), page.inner_text("#toast"))
            page.click("#signinbtn"); page.wait_for_selector("#signinmenu:not(.hide) .si-st.ok")
            check("…and its row gets a green tick and 'Signed in · just now', and the Sign-ins button shows a green dot", "Signed in · just now" in page.inner_text("#signinmenu [data-which=informs]") and page.locator("#sidot:not(.hide)").count() == 1 and page.locator("#signinmenu [data-which=ais] .si-st.ok").count() == 0)
            for _ in range(40):
                if not api("/api/state")["busy"]:
                    break
                time.sleep(0.1)
            os.remove(gate)                                                          # … and this one stays open until the test says so
            page.click("#signinmenu [data-which=library]"); page.click("#signinbtn"); page.wait_for_selector("#signinmenu:not(.hide) .si-st.wait")
            check("while a sign-in window is open the menu still opens, its row says it is waiting for you, and the other rows wait their turn", "Waiting for you to sign in" in page.inner_text("#signinmenu [data-which=library]") and page.locator("#signinmenu [data-which=informs]").is_disabled() and page.locator("#signinbtn").is_enabled())
            open(gate, "w").close(); page.wait_for_selector("#signinmenu [data-which=library] .si-st.ok", timeout=15000)
            st_ = api("/api/signins")
            check("the server remembers it (state/signins.json) for as long as that kind of session lasts", st_["informs"]["signed_in"] and st_["library"]["signed_in"] and st_["library"]["ttl_hours"] == 8 and not st_["ais"]["signed_in"] and st_["pending"] == "", str(st_))
            page.keyboard.press("Escape"); page.mouse.click(5, 400)
            page.evaluate("window.setTheme('dark')"); page.click("#signinbtn"); page.wait_for_selector("#signinmenu:not(.hide) .si-st.ok")
            con_ = page.evaluate(JS_CONTRAST, "dark")
            check("the sign-in rows are readable (WCAG AA) in the dark theme too", not con_, str(con_[:3])); page.evaluate("window.setTheme('light')")
            page.reload(); page.wait_for_selector("#lf-q"); page.wait_for_timeout(1200)
            check("after a reload the green dot is still there", page.locator("#sidot:not(.hide)").count() == 1)
            page.click("#signinbtn"); page.click("#signinmenu .si-more"); page.wait_for_selector("#st-body")
            check("'Library access settings…' opens Settings → Library access, with the university you set", "Library access" in page.inner_text(".st-tabs .on") and page.input_value("#la-name") == "Test University Library" and page.locator('input[name="la-mode"][value="openathens"]').is_checked() and page.input_value("#la-oa") == "testuni.ac.uk")
            page.check('input[name="la-mode"][value="ezproxy"]'); page.fill("#la-ez", "http://insecure.example/login?url="); page.click("#la-save"); page.wait_for_selector("#la-msg.bad")
            check("an insecure EZproxy address is refused with a reason", "https://" in page.inner_text("#la-msg"), page.inner_text("#la-msg"))
            page.fill("#la-ez", "https://ezproxy.testuni.edu/login?url="); page.click("#la-save"); page.wait_for_selector("#la-mode, .la-mode"); page.wait_for_timeout(500)
            check("a valid EZproxy address is saved", api("/api/library/access")["mode"] == "ezproxy" and api("/api/library/access")["ezproxy"].startswith("https://ezproxy.testuni.edu") and api("/api/library/access")["signs_in"])
            LT = os.path.join(TMP, "libtest.json")
            def libtest(expect, **kw):          # the answer is a file the fake test reads; wait for THIS answer (the card still shows the last one until it arrives)
                json.dump({"state": "ok", "message": "", "host": "", "saw": "", "mode": "ezproxy", **kw}, open(LT, "w"))
                page.click("#la-test"); page.wait_for_function("t => document.querySelector('#la-test-res').innerText.includes(t) && !document.querySelector('#la-test').disabled", arg=expect, timeout=20000)
            libtest("asked for a password", state="login", host="ezproxy.testuni.edu", message="Not signed in, or the sign-in has expired: ezproxy.testuni.edu asked for a password. Press “Sign in now” and sign in.")
            t_login = page.inner_text("#la-test-res")
            check("‘Test my access’ says in plain words that the library asked for a password (an amber notice), and the earlier green tick is taken away because that sign-in has expired",
                  "asked for a password" in t_login and page.locator("#la-test-res .errbox").count() == 1 and api("/api/signins")["library"]["signed_in"] is False and api("/api/signins")["library"]["test"]["state"] == "login" and page.locator("#la-signed").count() == 0, t_login)
            libtest("let Journal Watcher through", state="ok", host="www.sciencedirect.com", message="Signed in: your library let Journal Watcher through to www.sciencedirect.com without asking for a password.")
            check("…and once it gets through, the card says 'Signed in · just now' and the Sign-ins row is green again (the dot on the button too)",
                  "Signed in: your library let Journal Watcher through" in page.inner_text("#la-test-res") and "Signed in · just now" in page.inner_text("#la-signed") and api("/api/signins")["library"]["signed_in"] is True and page.wait_for_selector("#sidot:not(.hide)", timeout=5000) is not None)
            libtest("does not prove", state="unknown", host="www.sciencedirect.com", message="www.sciencedirect.com opened, but it does not say that your university gives you access. That does not prove you have none.")
            check("a page that says nothing is reported as 'does not prove', and changes no tick", "does not prove" in page.inner_text("#la-test-res") and page.locator("#la-test-res .errbox").count() == 0 and api("/api/signins")["library"]["signed_in"] is True)
            page.check('input[name="la-mode"][value="institutional"]'); page.fill("#la-oa", "testuni.ac.uk")
            check("typing an OpenAthens domain chooses the OpenAthens option (it used to be saved under 'each publisher’s page' and ignored)", page.is_checked('input[name="la-mode"][value="openathens"]') and not page.is_checked('input[name="la-mode"][value="institutional"]'))
            page.click("#la-save")
            try:
                page.wait_for_selector("#la-msg:has-text('Saved.')", timeout=8000)
            except Exception:  # noqa: BLE001  (the check below says what is wrong)
                pass
            page.wait_for_timeout(700)
            check("‘Saved.’ stays visible after Save (the card used to be drawn again at once and the message vanished)", "Saved." in page.inner_text("#la-msg") and api("/api/library/access")["mode"] == "openathens" and api("/api/library/access")["openathens"] == "testuni.ac.uk")
            page.fill("#la-oa", ""); page.click("#la-save")
            try:
                page.wait_for_function("/OpenAthens domain/.test(document.querySelector('#la-msg').textContent)", timeout=5000)
            except Exception:  # noqa: BLE001
                pass
            check("choosing OpenAthens with no domain is refused with what to enter, and nothing is overwritten", page.locator("#la-msg.bad").count() == 1 and api("/api/library/access")["openathens"] == "testuni.ac.uk")
            page.fill("#la-oa", "testuni.ac.uk")
            page.check('input[name="la-mode"][value="institutional"]'); page.click("#la-save"); page.wait_for_timeout(600)
            check("with 'through each publisher' chosen, the library has no sign-in of its own and says so", api("/api/library/access")["signs_in"] is False and page.locator("#la-go:disabled").count() == 1)
            page.click("#st-back"); page.click("#signinbtn"); page.wait_for_selector("#signinmenu:not(.hide)")
            check("…and the menu row offers to use each publisher's institution page instead of a library sign-in", "Uses each publisher" in page.inner_text("#signinmenu") and page.locator("#signinmenu [data-which=library]").count() == 0)
            try:
                api("/api/login", {"which": "library"}); refused = False
            except urllib.error.HTTPError as e_:
                refused = e_.code == 400
            check("pressing the library sign-in without one set up is refused, with the way forward", refused)
            page.keyboard.press("Escape"); page.mouse.click(5, 400)
            api("/api/library/access", {"mode": "openathens", "openathens": "testuni.ac.uk"})
            page.evaluate("Welcome.open(2)"); page.wait_for_selector("dialog.wl[open]")
            check("the welcome guide shows the same ticks (INFORMS signed in, AIS not)", page.locator('dialog.wl [data-si=informs].done').count() == 1 and page.locator('dialog.wl [data-si=ais].done').count() == 0 and "Test University Library" in page.inner_text("dialog.wl"))
            page.keyboard.press("Escape")

            print("narrow windows: no page scrolls sideways; the sidebar becomes a drawer")
            wide = []
            for w in (1100, 800, 520):
                page.set_viewport_size({"width": w, "height": 900})
                for h in ("#/watch", "#/library", "#/library/ask", "#/folders", "#/folder/lab", "#/folder/lab/matrix", "#/folder/lab/evidence", "#/folder/lab/lab", "#/folder/lab/discover"):
                    page.goto("about:blank"); page.goto(base + "/" + h); page.wait_for_selector("#libitem", state="attached"); page.wait_for_timeout(900)
                    over = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
                    if over > 1:
                        wide.append((w, h, over, page.evaluate("[...document.querySelectorAll('body *')].filter(e => e.getBoundingClientRect().right > window.innerWidth + 1 && getComputedStyle(e).position !== 'fixed').slice(0, 3).map(e => e.tagName + '.' + e.className)")))
                    if w == 800 and h in ("#/library", "#/folder/lab", "#/folders"):
                        shot(page, "23-narrow-" + h.strip("#/").replace("/", "-"))
            page.set_viewport_size({"width": 800, "height": 900}); page.goto("about:blank"); page.goto(base + "/#/watch"); page.wait_for_selector("#libitem", state="attached"); page.wait_for_timeout(500)
            check("on a narrow window the sidebar is tucked away behind the menu button", not page.locator("#sidebar").is_visible() and page.locator("#sbtoggle").is_visible())
            page.click("#sbtoggle"); page.wait_for_selector("#sidebar", state="visible")
            check("…the button slides it in over the page, with a veil behind it", page.locator("#sidebar").is_visible() and page.locator("#sbscrim").is_visible())
            page.click("#libitem"); page.wait_for_selector("#lf-q"); page.wait_for_timeout(500)
            check("…and choosing a page closes it again", not page.locator("#sidebar").is_visible() and page.url.endswith("#/library"), page.url)
            page.set_viewport_size({"width": 1400, "height": 950})
            check("no page scrolls sideways at 1100, 800 or 520 px wide (Watch, Library, Folders, a folder's tabs)", not wide, str(wide[:4]))

            print("large screens: the page grows with the window instead of leaving empty margins")
            MEASURE = """() => { const w = s => { const e = document.querySelector(s); return e ? e.getBoundingClientRect().width : 0; };
                return {content: w('#content'), layout: w('#layout'), body: getComputedStyle(document.body).fontSize, over: document.documentElement.scrollWidth - window.innerWidth}; }"""
            ROUTES = (("#/watch", 1200), ("#/library", 1160), ("#/folders", 1160), ("#/folder/lab", 1160), ("#/folder/lab/matrix", 1160))          # the route and how wide its column was before (and still is below 1500 px)
            WIDTHS, bad_old, bad_share, bad_over = {}, [], [], []
            for w in (1280, 1440, 1499, 1920, 2560, 3440, 5120):
                page.set_viewport_size({"width": w, "height": 1440})
                for h, was in ROUTES:
                    page.goto("about:blank"); page.goto(base + "/" + h); page.wait_for_selector("#libitem", state="attached"); page.wait_for_timeout(500)
                    m = page.evaluate(MEASURE); WIDTHS[(w, h)] = m
                    if m["over"] > 1:
                        bad_over.append((w, h, m["over"]))
                    if w < 1500 and (abs(m["layout"] - min(was, m["content"])) > 1 or m["body"] != "14px"):
                        bad_old.append((w, h, m["layout"], min(was, m["content"]), m["body"]))                  # below 1500 px the column is what it always was, and the text size is untouched
                    if 1900 <= w <= 3500 and m["layout"] / m["content"] < 0.65:
                        bad_share.append((w, h, round(m["layout"] / m["content"], 2)))
            check("below 1500 px wide nothing changed: each page's column is the same width as before and the text is still 14 px", not bad_old, str(bad_old[:4]))
            check("at 1920, 2560 and 3440 px the page uses at least 65 % of the space beside the sidebar (Watch, Library, Folders, a folder, its Matrix); before, it was 35-62 %", not bad_share, str(bad_share[:4]))
            check("…it grows with the window up to a limit (the Watch column is wider at 2560 than at 1920, and no wider at 5120 than 2200 px; Library no wider than 2400 px) and nothing scrolls sideways at any of these widths",
                  WIDTHS[(2560, "#/watch")]["layout"] > WIDTHS[(1920, "#/watch")]["layout"] > 1200 and WIDTHS[(5120, "#/watch")]["layout"] <= 2200 and WIDTHS[(5120, "#/library")]["layout"] <= 2400 and not bad_over,
                  str((WIDTHS[(1920, "#/watch")]["layout"], WIDTHS[(2560, "#/watch")]["layout"], WIDTHS[(5120, "#/watch")]["layout"], WIDTHS[(5120, "#/library")]["layout"], bad_over[:3])))
            page.set_viewport_size({"width": 3440, "height": 1440}); page.goto("about:blank"); page.goto(base + "/#/library"); page.wait_for_selector("#libitem", state="attached"); page.wait_for_timeout(500)
            shot(page, "24-wide-library")
            page.evaluate("window.Settings.open('ai')"); page.wait_for_selector("#st-body"); page.wait_for_timeout(300)
            sw = page.evaluate("""() => { const r = s => { const e = document.querySelector(s); return e ? e.getBoundingClientRect() : null; };
                return {back: r('#settings .st-head .back').left, card: r('#settings .st-body .st-card').left, body: r('#settings .st-body').width, content: r('#content').width}; }""")
            check("in Settings the heading line starts where the cards start (before, at 3440 px it was at the left edge and the cards a thousand pixels to the right), and the cards use more of a large window", abs(sw["back"] - sw["card"]) <= 41 and sw["body"] >= 0.55 * sw["content"], str(sw))
            page.keyboard.press("Escape"); page.evaluate("window.About.open()"); page.wait_for_selector("#au-body .st-card"); page.wait_for_timeout(300)
            ab_ = page.evaluate("""() => { const r = s => { const e = document.querySelector(s); return e ? e.getBoundingClientRect() : null; };
                return {back: r('#about .st-head .back').left, card: r('#au-body .st-card').left, body: r('#about .st-body').width}; }""")
            check("…and the About page keeps its reading column (760 px), with its heading above it", abs(ab_["back"] - ab_["card"]) <= 41 and ab_["body"] <= 761, str(ab_))
            page.keyboard.press("Escape")
            page.evaluate("""() => { const d = document.createElement('div'); d.id = 'probe-abs'; d.className = 'abs'; d.textContent = 'word '.repeat(600); document.querySelector('#layout').append(d); }""")
            pw_ = page.evaluate("document.querySelector('#probe-abs').getBoundingClientRect().width")
            check("a paragraph (an abstract, a reason) keeps a readable line length on a wide column", 400 < pw_ < 1000, str(pw_))
            page.evaluate("document.querySelector('#probe-abs').remove()")
            con_w = []
            for th in ("light", "dark"):
                page.evaluate("window.setTheme(%r)" % th)
                for h in ("#/watch", "#/library"):
                    page.goto("about:blank"); page.goto(base + "/" + h); page.wait_for_selector("#libitem", state="attached"); page.wait_for_timeout(400)
                    con_w += [(th, h, c) for c in page.evaluate(JS_CONTRAST, th)[:2]]
            page.evaluate("window.setTheme('light')")
            check("at 3440 px the text is as readable as at 1400 px (WCAG AA, light and dark, Watch and Library)", not con_w, str(con_w[:3]))
            page.set_viewport_size({"width": 1400, "height": 950})

            print("the Watch page uses the width, its journal tiles are bigger, and a run of 1000 papers is drawn in batches")
            runs_dir_ = os.path.join(TMP, ".agents", "skills", "ais-journal-watcher", "state", "runs")
            jl_ = [j["abbr"] for j in api("/api/config")["journals"]][:4]
            rid_ = "2025-W03_weekly"                    # a past week of its own (removed at the end of this block)
            pp_ = [{"id": "bt-%04d" % k, "title": "Batch paper %04d" % k, "abbr": jl_[k % len(jl_)], "journal": "Test", "pub_date": "2025-01-01", "abstract": "We study platforms.",
                    "authors": ["A. Author"], "verdict": "download" if k < 700 else "borderline" if k < 800 else "skip", "relevance": 1000 - k, "reason": "test", "topics": []} for k in range(1000)]
            fx_ = os.path.join(runs_dir_, rid_ + ".json")
            json.dump({"id": rid_, "kind": "weekly", "params": {"topics": [], "query": ""}, "created": "2025-01-13T10:00:00+00:00", "stage": "ready", "status": "done",
                       "usage": {"in": 0, "out": 0, "cache_write": 0, "cache_read": 0}, "week": "2025-W03", "checks": ["2025-01-13T10:00:00+00:00"], "errors": [], "jstate": {}, "papers": pp_}, open(fx_, "w"))
            ncards = lambda: page.locator("#papers > .paper").count()
            more = lambda: (page.evaluate("document.querySelector('#moremore').click()"), page.wait_for_timeout(300))     # ('Show more' itself: a click that does not scroll the button into view, where the scroll trigger would draw too)
            def wait_cards(cond, timeout=10000):
                try:
                    page.wait_for_function("(c) => { const n = document.querySelectorAll('#papers > .paper').length; return eval(c); }", arg=cond, timeout=timeout); return True
                except Exception:  # noqa: BLE001
                    return False
            def open_batch(width):
                page.set_viewport_size({"width": width, "height": 950}); page.goto("about:blank"); page.goto(base + "/#/watch"); page.wait_for_selector("#weeklybtn", state="attached"); page.wait_for_timeout(400)
                page.evaluate("document.body.classList.remove('sb-folded')")
                page.evaluate("openRun({id: %s})" % json.dumps(rid_)); page.wait_for_selector("#tiles .tile", timeout=10000); page.wait_for_timeout(300)
            try:
                open_batch(1580)
                g_ = page.evaluate("""() => ({layout: document.querySelector('#layout').getBoundingClientRect().width, content: document.querySelector('#content').getBoundingClientRect().width,
                                              tiles: [...document.querySelectorAll('#tiles .tile')].map(t => Math.round(t.getBoundingClientRect().width)), fs: getComputedStyle(document.querySelector('#tiles .tname')).fontSize})""")
                check("at 1580 px the Watch page uses all the space beside the sidebar, and its journal tiles are bigger (about 440 px, two a row; they were 380)",
                      abs(g_["layout"] - g_["content"]) <= 1 and len(g_["tiles"]) >= 2 and min(g_["tiles"]) >= 400 and g_["fs"] == "17px", str(g_))
                shot(page, "25-watch-tiles-1580")
                page.set_viewport_size({"width": 1400, "height": 950}); page.goto("about:blank"); page.goto(base + "/"); page.click("#libitem"); page.wait_for_selector("#libtiles .tile", timeout=10000)
                check("…the Library's journal tiles keep their size", page.evaluate("getComputedStyle(document.querySelector('#libtiles .tname')).fontSize") == "15px")
                open_batch(1400)
                page.evaluate("go({j: '*'})"); page.wait_for_selector("#papers > .paper"); page.wait_for_timeout(300)
                check("a run with 1000 papers draws the first 60 (none built and hidden), with a 'Show more' button below",
                      ncards() == 60 and page.locator("#papers .paper.hide").count() == 0 and page.locator("#moremore").is_visible() and "more" in page.inner_text("#moremore"), "%d cards, %s" % (ncards(), page.inner_text("#papersmore")))
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                wait_cards("n > 60"); page.wait_for_timeout(300)
                n1 = ncards()
                check("…scrolling near the end draws the next 60", 60 < n1 <= 120, str(n1))
                page.evaluate("window.scrollTo(0, 0)"); page.wait_for_timeout(200); more()
                n2 = ncards()
                check("…'Show more' draws exactly 60 more and says how many are left", n2 == n1 + 60 and "left" in page.inner_text("#moremore"), "%d → %d, %s" % (n1, n2, page.inner_text("#moremore")))
                page.click('#vfilter button[data-v="skip"]'); page.wait_for_timeout(600)
                check("a filter starts again with its own first 60 (Skipped: 60 of 200, none hidden)", ncards() == 60 and page.locator("#papers > .paper.skip").count() == 60, str(ncards()))
                more()                                                                                  # (120 drawn: a search must still start again)
                page.fill("#q", "Batch paper 0999"); page.wait_for_timeout(600)
                check("…a search finds a paper beyond the first batch, and nothing more is offered", ncards() == 1 and "0999" in page.inner_text("#papers") and not page.locator("#moremore").is_visible(), str(ncards()))
                page.fill("#q", ""); page.wait_for_timeout(600)
                check("…clearing it starts again at 60", ncards() == 60, str(ncards()))
                page.click('#vfilter button[data-v="borderline"]'); page.wait_for_timeout(600); more()
                nb_ = ncards()
                card_ = page.locator("#papers > .paper").nth(70); card_.scroll_into_view_if_needed(); page.wait_for_timeout(200)
                y0 = page.evaluate("window.scrollY")
                card_.locator('[data-act="queue"]').click(); page.wait_for_timeout(800)
                y1 = page.evaluate("window.scrollY")
                check("moving a paper to the download queue keeps what is drawn and where you are (no jump back to the first 60 at the top)", nb_ == 100 and ncards() == 99 and abs(y1 - y0) < 950, "%d → %d cards, scroll %d → %d" % (nb_, ncards(), y0, y1))
                page.click('#vfilter button[data-v="download"]'); page.wait_for_timeout(600)
                page.click("#selnone"); page.wait_for_timeout(300)                                       # (a weekly run selects its download papers when it opens: start from none)
                more(); n_sel = ncards()
                page.click("#selrec"); page.wait_for_timeout(300)
                check("'Select all' selects every download paper, drawn or not (701: one was just moved to the queue), and keeps what is drawn",
                      page.inner_text("#selcount").strip() == "701 selected" and ncards() == n_sel == 120, "%s, %d → %d cards" % (page.inner_text("#selcount"), n_sel, ncards()))
                more()
                later_ = page.evaluate("[...document.querySelectorAll('#papers > .paper')].slice(120).map(c => c.querySelector('input[type=checkbox]').checked)")
                check("…and the papers drawn after it come ticked", later_ and all(later_), str(later_[:5]))
                page.click("#selnone"); page.click('#vfilter button[data-v="download"]'); page.wait_for_timeout(600)
                left0 = int(re.search(r"\((\d+) left\)", page.inner_text("#moremore")).group(1))
                page.evaluate("""(a) => { for (let k = 0; k < 5; k++) addPaper({id: 'fresh-' + k, title: 'Fresh paper ' + k, abbr: a, journal: 'Test', pub_date: '2025-01-01', abstract: 'x',
                                                                     authors: ['A. Author'], verdict: 'download', relevance: 1, reason: 'test', topics: []}); }""", jl_[0])
                left1 = int(re.search(r"\((\d+) left\)", page.inner_text("#moremore")).group(1))
                check("a paper found during a run is not drawn past the first batch: it is counted in 'left'", ncards() == 60 and left1 == left0 + 5, "%d cards, %d → %d left" % (ncards(), left0, left1))
                page.fill("#q", "Fresh paper 3"); page.wait_for_timeout(600)
                check("…and a search finds it", ncards() == 1 and "Fresh paper 3" in page.inner_text("#papers"), str(ncards()))
                page.fill("#q", "")
            finally:
                if os.path.exists(fx_):
                    os.remove(fx_)
                page.set_viewport_size({"width": 1400, "height": 950}); page.goto("about:blank"); page.goto(base + "/#/watch"); page.wait_for_selector("#weeklybtn", state="attached"); page.wait_for_timeout(400)

            print("an archive search is remembered: a repeat is not paid for twice")
            screened = os.path.join(TMP, "screened.log")
            def batches():
                return open(screened).read().split() if os.path.exists(screened) else []
            def wait_idle(n_papers, min_checks=1, timeout=60, not_ids=()):          # the search run itself (not the run that was open before it started), idle, with this many papers and times started
                for _ in range(timeout * 4):
                    s_ = api("/api/state")
                    r_ = s_["run"]
                    if not s_["busy"] and r_ and r_["kind"] == "search" and r_["n_papers"] >= n_papers and len(r_.get("checks") or []) >= min_checks and r_["stage"] == "ready" and r_["id"] not in not_ids:
                        return r_
                    time.sleep(0.25)
                raise AssertionError("the search did not finish: %s" % json.dumps(api("/api/state"))[:300])
            page.set_viewport_size({"width": 1400, "height": 950}); page.goto("about:blank"); page.goto(base + "/#/watch"); page.wait_for_selector("#searchbtn", state="attached"); page.wait_for_timeout(600)
            page.click('.tabs button[data-tab="search"]')
            page.select_option("#y0", "2024"); page.select_option("#y1", "2024"); page.click("#jnone")
            first_two = [c.get_attribute("data-abbr") for c in page.locator("#journalchips .chip:not(.off)").all()[:2]]
            page.click('#journalchips .chip[data-abbr="%s"]' % first_two[0]); page.click("#topicchips .chip >> nth=0")
            check("before the first search the form does not claim to have run it", "already ran" not in page.inner_text("#est"))
            n0 = len([r_ for r_ in api("/api/runs") if r_["kind"] == "search"])
            page.click("#searchbtn"); run_a = wait_idle(3)
            check("the search finds 3 papers and screens them once (one question to the model)", run_a["n_papers"] == 3 and run_a["n_download"] == 2 and len(batches()) == 1 and batches() == ["3"], str((run_a["n_papers"], batches())))
            # the skipped paper is listed under "Skipped" like any other card. A skipped card once shared its class (skip) with the hidden "Skip to the page" link, whose
            # rule put every skipped card 60 px above the window, all stacked over the header
            page.wait_for_function("RUN && RUN.id === %s" % json.dumps(run_a["id"]), timeout=8000)
            page.evaluate("go({j: '*'})"); page.click('#vfilter button[data-v="skip"]')
            page.wait_for_selector("#papers .paper.skip:not(.hide)", timeout=8000)
            box = page.evaluate("(() => { const c = document.querySelector('#papers .paper.skip:not(.hide)'), a = c.getBoundingClientRect(), b = document.querySelector('#papers').getBoundingClientRect(); return {pos: getComputedStyle(c).position, top: a.top, ltop: b.top, h: a.height, n: document.querySelectorAll('#papers .paper.skip:not(.hide)').length}; })()")
            check("the Skipped filter lists the skipped paper inside the list, where it can be read and clicked (not hidden above the window)", box["pos"] == "static" and box["top"] >= box["ltop"] and box["h"] > 30 and box["n"] == 1, str(box))
            page.click('#vfilter button[data-v="relevant"]'); page.evaluate("go({j: null})")
            page.click('#runtabs button[data-rt="search"]'); page.wait_for_function("document.querySelector('#n-search').textContent.trim() !== '' && /You already ran this search/.test(document.querySelector('#est').innerText)", timeout=15000)
            check("it is in the history under 'Archive searches', and the form now says you already ran this search", run_a["title"] in page.inner_text("#runs") and "You already ran this search" in page.inner_text("#est"), page.inner_text("#est"))
            page.click("#searchbtn"); run_a2 = wait_idle(3, min_checks=2)
            runs_ = [r_ for r_ in api("/api/runs") if r_["kind"] == "search"]
            try:
                page.wait_for_function("/updated/.test(document.querySelector('#runs').innerText)", timeout=15000)
            except Exception:  # noqa: BLE001  (the check below says what is wrong)
                pass
            check("pressing Search again for the same search continues the saved one: no second copy, nothing screened again, and it says when it was updated",
                  len(runs_) == n0 + 1 and run_a2["id"] == run_a["id"] and batches() == ["3"] and len(api("/api/runs/" + run_a["id"])["checks"]) == 2 and "updated" in page.inner_text("#runs"), str((len(runs_), n0, batches(), run_a2["id"] == run_a["id"], len(api("/api/runs/" + run_a["id"])["checks"]), page.inner_text("#runs")[:200])))
            page.click('#journalchips .chip[data-abbr="%s"]' % first_two[1])
            page.click("#searchbtn"); run_b = wait_idle(6)
            check("a wider search (one more journal) keeps what it can: the first journal's three papers reuse their verdicts and only the new journal's are screened (and the page says so)",
                  run_b["id"] != run_a["id"] and run_b["reused"] == 3 and batches() == ["3", "3"], str((run_b["id"], run_a["id"], run_b["reused"], run_b["n_papers"], batches(), run_b["params"], run_a["params"])))
            page.wait_for_function("/reused a verdict/.test(document.querySelector('#runmeta').textContent)", timeout=8000)
            check("…the run's description says how many papers reused a verdict", "3 papers reused a verdict from an earlier screening" in page.inner_text("#runmeta"), page.inner_text("#runmeta"))
            page.click('#runs .runitem[data-id="%s"]' % run_a["id"]); page.wait_for_selector("#againbtn:not(.hide)")
            page.fill("#focus", "zzz"); page.click("#jall")
            page.click("#againbtn")
            check("‘Search again…’ fills the form with that search's settings (years, journals, topics, focus) and says that it continues the saved search",
                  page.input_value("#y0") == "2024" and page.input_value("#focus") == "" and page.locator("#journalchips .chip.on").count() == 1 and page.get_attribute("#journalchips .chip.on", "data-abbr") == first_two[0]
                  and page.locator("#topicchips .chip.on").count() == 1 and "You already ran this search" in page.inner_text("#est"), page.inner_text("#est"))
            page.evaluate("RUN = null; NAV.view = 'run'; render()")
            page.wait_for_selector("#resume:not(.hide)")
            check("with nothing open, the Watch page offers the last runs to pick up ('Pick up where you left off')", page.locator("#resumelist .runitem").count() >= 2 and "Archive search" in page.inner_text("#resumelist"), page.inner_text("#resume"))
            page.click('#resumelist .runitem >> nth=0'); page.wait_for_function("document.querySelector('#resume').classList.contains('hide')")
            check("…a click opens it (the page now shows that run, with its papers)", page.evaluate("RUN && RUN.id") == run_b["id"] and page.evaluate("papers.size") == 6, str(page.evaluate("RUN && RUN.id")))

            print("SSRN is searched by words")
            ssrnlog = os.path.join(TMP, "ssrn.log")
            page.goto("about:blank"); page.goto(base + "/#/watch"); page.wait_for_selector("#searchbtn", state="attached"); page.wait_for_timeout(500)
            page.click('.tabs button[data-tab="search"]')
            page.click("#jall"); page.fill("#focus", "")
            check("SSRN is a choice among the journals, off by default and not switched on by 'all', with a note that it needs words", page.locator('#journalchips .chip[data-abbr="SSRN"]').count() == 1 and page.locator('#journalchips .chip[data-abbr="SSRN"].on').count() == 0)
            page.click('#journalchips .chip[data-abbr="SSRN"]')
            check("choosing SSRN says it is searched by words and that a focus phrase is needed; the estimate counts its best matches, not a whole year", "focus phrase" in page.inner_text("#est") and "200 best matches" in page.inner_text("#est"), page.inner_text("#est"))
            page.click("#searchbtn")
            try:
                page.wait_for_function("document.querySelector('#toast') && /SSRN is searched by words/.test(document.querySelector('#toast').textContent)", timeout=8000)
            except Exception:  # noqa: BLE001  (the check below says what is wrong)
                pass
            check("pressing Search with SSRN and no focus phrase is refused with what to do, and starts nothing", "SSRN is searched by words" in page.inner_text("#toast") and not os.path.exists(ssrnlog) and not api("/api/state")["busy"])
            page.click("#jnone"); page.click('#journalchips .chip[data-abbr="SSRN"]'); page.select_option("#y0", "2024"); page.select_option("#y1", "2024"); page.fill("#focus", "platform governance")
            n_runs = len([r_ for r_ in api("/api/runs") if r_["kind"] == "search"])
            page.click("#searchbtn"); run_s = wait_idle(2, not_ids=(run_a["id"], run_b["id"]))
            check("with a focus phrase, SSRN is searched by those words and its papers are screened and listed as SSRN's", open(ssrnlog).read().splitlines() == ["platform governance|2024|2024|200"] and run_s["n_papers"] == 2 and run_s["params"]["journals"] == ["SSRN"] and len([r_ for r_ in api("/api/runs") if r_["kind"] == "search"]) == n_runs + 1,
                  str((open(ssrnlog).read() if os.path.exists(ssrnlog) else None, run_s["n_papers"])))
            check("the page is told that SSRN is a words-only source (so it never joins the weekly check or an 'all journals' choice)", [j for j in api("/api/config")["journals"] if j["abbr"] == "SSRN"][0]["needs_query"] is True and [j for j in api("/api/config")["journals"] if j["abbr"] == "SSRN"][0]["default_off"] is True)
            cfg_s = api("/api/config")["journals"]
            tiles_w = page.evaluate("(() => { const old = RUN; RUN = {kind: 'weekly', params: {}}; try { return runJournals().map(j => j.abbr); } finally { RUN = old; } })()")
            tiles_s = page.evaluate("(() => { const old = RUN; RUN = {kind: 'search', params: {journals: ['SSRN']}}; try { return runJournals().map(j => j.abbr); } finally { RUN = old; } })()")
            check("a weekly check shows no SSRN tile (it is never read there, so it would claim 'No new papers'), while an archive search that chose SSRN still shows it",
                  [j for j in cfg_s if j["abbr"] == "SSRN"][0]["weekly"] is False and all(j["weekly"] is True for j in cfg_s if j["abbr"] != "SSRN") and "SSRN" not in tiles_w and len(tiles_w) == len(cfg_s) - 1 and tiles_s == ["SSRN"], str((tiles_w, tiles_s)))

            print("Check my PDFs: a PDF that is not the paper it is filed under can be found, kept or taken out")
            import pymupdf as _pmu
            idxp, folderp = os.path.join(TMP, "Papers", "_index.csv"), os.path.join(TMP, "Papers", "2025-03", "CheckTest")
            saved_idx = open(idxp, newline="").read(); os.makedirs(folderp, exist_ok=True)
            def mkpdf_(name, headline):
                d_ = _pmu.open(); pg_ = d_.new_page(); pg_.insert_textbox(_pmu.Rect(60, 60, 540, 130), headline, fontsize=18); pg_.insert_textbox(_pmu.Rect(60, 150, 540, 790), "Jane Doe and Richard Roe. " + "We study how platforms govern their complementors. " * 40, fontsize=9)
                d_.save(os.path.join(folderp, name)); d_.close()
            mkpdf_("right.pdf", "Governing Open Platforms Through Transparent Rules"); mkpdf_("wrong1.pdf", "Two-stage differences in differences"); mkpdf_("wrong2.pdf", "A Completely Different Paper About Hotel Reviews")
            for nm in ("wrong1", "wrong2"):
                open(os.path.join(folderp, nm + ".md"), "w").write("summary of " + nm)
            with open(idxp, "a", newline="") as f_:
                w_ = csv.writer(f_)
                for nm, ttl, doi in (("right", "Governing Open Platforms Through Transparent Rules", "10.9999/pc.right"), ("wrong1", "The Pitfalls of Review Solicitation on TripAdvisor", "10.9999/pc.wrong1"), ("wrong2", "Platform Pricing and Seller Entry Decisions", "10.9999/pc.wrong2")):
                    w_.writerow(["2026-10-29", "Test Journal", "TJ", ttl, "Jane Doe; Richard Roe", doi, "", "", "", "Papers/2025-03/CheckTest/%s.pdf" % nm, "ok", "browser"])
            page.goto("about:blank"); page.goto(base + "/#/library"); page.wait_for_selector("#lf-q"); page.wait_for_timeout(500)
            page.evaluate("window.Settings.open('files')"); page.wait_for_selector("#pc-go")
            check("Settings → AI files & backup has 'Check my PDFs', and it has not looked yet (it says nothing about your files before you press it)", "PDFs checked" not in page.inner_text("#pc-card") and page.locator("#pc-go:not([disabled])").count() == 1)
            page.click("#pc-go"); page.wait_for_function("document.querySelector('#pc-res') && /PDFs? checked/.test(document.querySelector('#pc-res').innerText)", timeout=30000)
            res_t = page.inner_text("#pc-res")
            check("it reads every PDF and lists the two that are another paper (and not the right one), each with what the PDF is titled",
                  page.locator('.pc-row[data-f$="wrong1.pdf"]').count() == 1 and page.locator('.pc-row[data-f$="wrong2.pdf"]').count() == 1 and page.locator('.pc-row[data-f$="/right.pdf"]').count() == 0
                  and "Two-stage differences in differences" in page.inner_text('.pc-row[data-f$="wrong1.pdf"]'), res_t[:600])
            check("…each row can show the PDF, keep it ('it is the right paper') or remove it and download again", page.locator('.pc-row[data-f$="wrong1.pdf"] a[href*="CheckTest/wrong1.pdf"]').count() == 1 and page.locator('.pc-row[data-f$="wrong1.pdf"] [data-pc-keep]').count() == 1 and page.locator('.pc-row[data-f$="wrong1.pdf"] [data-pc-rm]').count() == 1)
            try:
                api("/api/library/check/remove", {"file": "Papers/2025-03/CheckTest/right.pdf"}); refused_ = False
            except urllib.error.HTTPError as e_:
                refused_ = e_.code == 404
            check("only a PDF that the check listed can be removed: the right one is refused and stays where it is", refused_ and os.path.exists(os.path.join(folderp, "right.pdf")))
            page.click('[data-pc-keep$="wrong2.pdf"]'); page.wait_for_function("!document.querySelector('.pc-row[data-f$=\"wrong2.pdf\"]')", timeout=5000)
            page.evaluate("document.querySelector('#pc-go').dataset.old = '1'"); page.click("#pc-go")           # (the button is drawn again with the new answer: wait for that one, not for the old one still on the page)
            page.wait_for_function("(() => { const b = document.querySelector('#pc-go'); return b && !b.dataset.old && !b.disabled && /PDFs? checked/.test(document.querySelector('#pc-res').innerText); })()", timeout=30000)
            check("'It is the right paper' hides it, and it stays hidden when you check again (counted as 'you marked as right')", page.locator('.pc-row[data-f$="wrong2.pdf"]').count() == 0 and page.locator('.pc-row[data-f$="wrong1.pdf"]').count() == 1 and "you marked as right" in page.inner_text("#pc-res"), page.inner_text("#pc-res")[:400])
            if os.path.exists(marker): os.remove(marker)
            page.click('[data-pc-rm$="wrong1.pdf"]'); page.wait_for_function("!document.querySelector('.pc-row[data-f$=\"wrong1.pdf\"]')", timeout=15000)
            for _ in range(40):
                if os.path.exists(marker): break
                page.wait_for_timeout(250)
            rows_after = list(csv.DictReader(open(idxp, newline=""))); r1 = [r_ for r_ in rows_after if r_["doi"] == "10.9999/pc.wrong1"][0]
            removed_dirs = os.listdir(os.path.join(TMP, "Papers", "_removed")) if os.path.isdir(os.path.join(TMP, "Papers", "_removed")) else []
            check("'Remove and download again' moves that PDF and its summary aside (Papers/_removed, nothing deleted), the library forgets it, and the paper is queued for download again",
                  not os.path.exists(os.path.join(folderp, "wrong1.pdf")) and not os.path.exists(os.path.join(folderp, "wrong1.md")) and len(removed_dirs) == 1
                  and os.path.exists(os.path.join(TMP, "Papers", "_removed", removed_dirs[0], "2025-03", "CheckTest", "wrong1.pdf")) and os.path.exists(os.path.join(TMP, "Papers", "_removed", removed_dirs[0], "2025-03", "CheckTest", "wrong1.md"))
                  and r1["status"] == "removed" and os.path.exists(os.path.join(folderp, "wrong2.pdf")) and os.path.exists(marker) and open(marker).read() == "doi:10.9999/pc.wrong1", str((removed_dirs, r1["status"], open(marker).read() if os.path.exists(marker) else None)))
            page.keyboard.press("Escape")
            lib_titles = [x["title"] for x in api("/api/library")]
            check("the Library no longer lists the removed paper, and still lists the others", "The Pitfalls of Review Solicitation on TripAdvisor" not in lib_titles and "Governing Open Platforms Through Transparent Rules" in lib_titles and "Platform Pricing and Seller Entry Decisions" in lib_titles)
            for _ in range(60):                                     # let the (pretend) download finish before the test files are taken away
                if not api("/api/state")["busy"]: break
                time.sleep(0.25)
            open(idxp, "w", newline="").write(saved_idx); shutil.rmtree(folderp, ignore_errors=True); shutil.rmtree(os.path.join(TMP, "Papers", "_removed"), ignore_errors=True)
            try: os.remove(os.path.join(TMP, ".agents", "skills", "ais-journal-watcher", "state", "identity_ok.json"))
            except OSError: pass

            print("one dashboard per project")
            second = subprocess.run([sys.executable, os.path.join(TMP, "dashboard", "server.py"), "--port", str(port + 1), "--no-open"], cwd=TMP, env=env, capture_output=True, text=True, timeout=60)
            check("a second dashboard on the same project does not start; it says where the first one runs", second.returncode == 0 and "already running" in second.stdout and ":%d" % port in second.stdout, second.stdout[:300] + second.stderr[-200:])
            lock = json.load(open(os.path.join(TMP, "local-state", "server.lock")))
            check("the lock that says a dashboard runs here is a per-computer file: it is not in the data folder (a synced folder must not carry it to another computer)", not os.path.exists(os.path.join(TMP, ".agents", "skills", "ais-journal-watcher", "state", "server.lock")))
            check("the lock file names the running one, and it is unharmed", lock["port"] == port and lock["pid"] == srv.pid and api("/api/config")["model"])

            print("your library as an MCP server (read-only)")
            def listing():
                return sorted(os.path.join(dp, f) + str(os.path.getmtime(os.path.join(dp, f))) for dp, _, fs in os.walk(os.path.join(TMP, "Papers")) for f in fs)
            mcp_before = listing()
            script = os.path.join(TMP, ".agents", "skills", "ais-journal-watcher", "scripts", "mcp_server.py")
            msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}, {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "library_stats", "arguments": {}}},
                    {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "search_library", "arguments": {"query": "complementors"}}},
                    {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "get_paper", "arguments": {"paper": rows[0][3]}}},
                    {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "read_paper_text", "arguments": {"paper": "Open Platforms", "from_page": 1, "to_page": 1}}},
                    {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "get_paper", "arguments": {"paper": "zzzz no such paper"}}}, {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "delete_everything", "arguments": {}}},
                    {"jsonrpc": "2.0", "id": 9, "method": "resources/list"}]
            out_ = subprocess.run([sys.executable, script], input="\n".join(json.dumps(m) for m in msgs) + "\n", capture_output=True, text=True, timeout=60, cwd=TMP).stdout
            rep = {m["id"]: m for m in (json.loads(l) for l in out_.splitlines() if l.strip())}
            tx = lambda i: json.loads(rep[i]["result"]["content"][0]["text"])   # noqa: E731
            check("MCP: the server introduces itself and lists read-only tools (no reply to the 'initialized' notification)", rep[1]["result"]["serverInfo"]["name"] == "journal-watcher" and len(rep) == 9 and {t["name"] for t in rep[2]["result"]["tools"]} >= {"search_library", "get_paper", "read_paper_text", "search_highlights", "list_folders", "get_notes"} and all(t["annotations"]["readOnlyHint"] for t in rep[2]["result"]["tools"]))
            check("MCP: it answers from the library — counts, a search with where the words matched, a paper, a page of text", tx(3)["papers"] == len(api("/api/library")) and tx(4)["papers"][0]["title"] == "Open Platforms and Complementors" and tx(5)["title"] == rows[0][0] and "[p. 1]" in tx(6)["text"], json.dumps([tx(3), tx(4)["papers"][0]["title"], tx(5)["title"], rows[0][0], tx(6)["text"][:80]]))
            check("MCP: a wrong request is an error the model can read, not a crash; unknown tools and methods are refused", rep[7]["result"]["isError"] and "No paper" in rep[7]["result"]["content"][0]["text"] and rep[8]["error"]["code"] == -32602 and rep[9]["error"]["code"] == -32601)
            check("MCP: it never writes (nothing under Papers/ changed)", listing() == mcp_before)

            print("Settings → AI files & backup")
            af = api("/api/aifiles"); kinds_by = {k["id"]: k for k in af["kinds"]}
            check("the AI files are counted by kind, with sizes and a note on what holds your edits", kinds_by["cards"]["count"] >= 3 and kinds_by["cache"]["bytes"] > 0 and kinds_by["skims"]["count"] == 1 and kinds_by["cards"]["human"] and kinds_by["cache"]["regen"], str(af["kinds"])[:300])
            import zipfile, io
            z0 = zipfile.ZipFile(io.BytesIO(urllib.request.urlopen(base + "/api/backup").read())); names0 = z0.namelist()
            check("the backup zip holds your notes, cards, folders and index, but no PDFs and no .env", any(n.endswith(".notes.json") for n in names0) and any(n.endswith(".card.json") for n in names0) and any("_folders/" in n for n in names0) and "Papers/_index.csv" in names0 and not any(n.endswith(".pdf") or n.endswith(".env") for n in names0) and "README-backup.txt" in names0, str(names0[:6]))
            z1 = zipfile.ZipFile(io.BytesIO(urllib.request.urlopen(base + "/api/backup?pdfs=1").read()))
            check("…and with the PDFs when asked", sum(n.endswith(".pdf") for n in z1.namelist()) >= 4)
            page.goto("about:blank"); page.goto(base + "/#/library"); page.wait_for_selector("#lf-q"); page.click("#settingsbtn"); page.wait_for_selector("#st-body"); page.click('.st-tabs button[data-t="files"]'); page.wait_for_selector("table.af")
            shot(page, "24-ai-files")
            check("Settings shows how to connect Claude Desktop or Claude Code to the library (MCP), with the real paths", "claude mcp add journal-watcher" in page.inner_text("#mcp-cc") and "mcp_server.py" in page.inner_text("#mcp-cd") and sys.executable in page.inner_text("#mcp-cd"))
            check("Settings has an AI files & backup tab listing every kind with a Clear button", page.locator("table.af tr[data-k]").count() == 8 and "Back up my data" in page.inner_text("#st-body") and page.locator("#af-dl").count() == 1)
            sk0 = os.path.exists(sidecar(2, ".skim.json")); page.click('[data-clear="skims"]'); page.wait_for_selector(".st-card:has-text('Recently cleared')")
            check("Clear removes the skim files, after a confirmation; a zip is kept and listed under Recently cleared", sk0 and not os.path.exists(sidecar(2, ".skim.json")) and len(api("/api/aifiles")["trash"]) == 1 and api("/api/aifiles")["trash"][0]["kind"] == "skims")
            page.click("[data-restore]"); page.wait_for_function("document.querySelector('table.af tr[data-k=skims] td:nth-child(2)').textContent.trim() === '1'")
            check("Put back restores them", os.path.exists(sidecar(2, ".skim.json")))
            n_trash = len(api("/api/aifiles")["trash"]); page.click('[data-clear="cache"]'); page.wait_for_function("document.querySelector('table.af tr[data-k=cache] td:nth-child(2)').textContent.trim() === '0'")
            check("saved answers are cleared without a zip (they come back by themselves)", len(api("/api/aifiles")["trash"]) == n_trash and api("/api/aifiles")["kinds"][5]["count"] == 0)
            page.click("#st-back")

            print("about the author and the support reminder")
            static_ = os.path.join(TMP, "dashboard", "static"); skd = os.path.join(TMP, ".agents", "skills", "ais-journal-watcher")
            author_file = os.path.join(static_, "author.json"); author_orig = open(author_file, encoding="utf-8").read()
            json.dump({"name": "", "role": "", "university": "", "university_url": "", "photo": "", "bio": [], "links": [], "support": {"service": "Ko-fi", "url": ""}, "repo": ""}, open(author_file, "w"))      # an empty page, whatever the repository ships
            page.goto("about:blank"); page.goto(base + "/#/library"); page.wait_for_function("window.Settings && window.About && document.querySelector('#aboutbtn')")
            page.click("#aboutbtn"); page.wait_for_selector("#about:not(.hide) #au-body .st-card")
            check("About opens from the sidebar, lights its button and names itself in the top bar", page.eval_on_selector(".navbtn.on", "e => e.dataset.nav") == "about" and "About" in page.inner_text("#tbtitle") and page.locator("#settings:not(.hide)").count() == 0)
            check("with the author's page left empty it shows only the app: no 'Made by', no support card, no reminder switch, and no reminder is due",
                  "Made by" not in page.inner_text("#au-body") and page.locator("#au-remind").count() == 0 and "GNU Affero" in page.inner_text("#au-body") and api("/api/support")["due"] is False)
            open(os.path.join(static_, "me.png"), "wb").write(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="))
            json.dump({"name": "Ada Lovelace", "role": "Researcher in Information Systems", "university": "Test University", "university_url": "https://example.edu", "photo": "me.png",
                       "bio": ["First paragraph about the author.", "Second paragraph."], "links": [{"label": "Website", "url": "https://example.org/ada"}, {"label": "Email", "url": "mailto:ada@example.org"}, {"label": "Unsafe", "url": "javascript:alert(1)"}],
                       "support": {"service": "Ko-fi", "url": "https://ko-fi.com/adatest"}, "repo": "https://github.com/example/repo"}, open(author_file, "w"))
            page.click("#au-back"); page.click("#aboutbtn"); page.wait_for_selector(".au-who")
            t_ = page.inner_text("#au-body")
            check("a filled-in page shows name, role, university (as a link), the text and the links; an unsafe link is left out",
                  all(x in t_ for x in ("Ada Lovelace", "Researcher in Information Systems", "Test University", "First paragraph", "Second paragraph", "Website", "Email")) and "Unsafe" not in t_ and page.get_attribute(".au-role a", "href") == "https://example.edu", t_[:300])
            page.wait_for_function("(() => { const i = document.querySelector('.au-photo'); return !!i && i.complete; })()", timeout=15000)
            check("outside links open in another window without handing it this page, and the photo loads",
                  all(page.get_attribute(sel, "rel") == "noopener noreferrer" and page.get_attribute(sel, "target") == "_blank" for sel in (".au-pill >> nth=0", ".au-tipbtn", ".au-role a")) and page.evaluate("document.querySelector('.au-photo').naturalWidth") == 1,
                  page.evaluate("[...document.querySelectorAll('.au-pill, .au-tipbtn, .au-role a')].map(a => [a.textContent.trim(), a.target, a.rel]).concat([[document.querySelector('.au-photo').src, document.querySelector('.au-photo').naturalWidth]])"))
            check("the support card names the service, links to the tip page, says what is counted and that it stays here, and has the reminder switch on",
                  "Ko-fi" in page.inner_text(".au-tip") and page.get_attribute(".au-tipbtn", "href") == "https://ko-fi.com/adatest" and "never sent anywhere" in page.inner_text(".au-tip") and page.is_checked("#au-remind"))
            page.uncheck("#au-remind"); page.wait_for_function("document.querySelector('#au-msg').textContent.includes('No more')")
            check("switching the reminders off is saved", api("/api/support")["optout"] is True)
            page.check("#au-remind"); page.wait_for_function("document.querySelector('#au-msg').textContent.includes('remind you')")
            check("…and on again", api("/api/support")["optout"] is False)
            ctx.route("https://ko-fi.com/**", lambda r: r.fulfill(status=200, content_type="text/html", body="<title>tip</title>"))
            with ctx.expect_page() as np_:
                page.click(".au-tipbtn")
            np_.value.close()
            for _ in range(20):
                if api("/api/support")["supported"]:
                    break
                time.sleep(0.25)
            check("pressing the tip link opens it in another window and remembers that you did", api("/api/support")["supported"] is True)
            api("/api/support/action", {"action": "reminders", "value": True})
            page.keyboard.press("Escape"); page.wait_for_selector("#about.hide", state="attached")
            check("Escape closes About", page.locator("body.about-open").count() == 0)
            page.click("#aboutbtn"); page.wait_for_selector("#about:not(.hide)"); page.click("#settingsbtn"); page.wait_for_selector("#settings:not(.hide)")
            check("opening Settings closes About", page.locator("#about.hide").count() == 1 and page.eval_on_selector(".navbtn.on", "e => e.dataset.nav") == "settings")
            page.click("#aboutbtn"); page.wait_for_selector("#about:not(.hide)")
            check("opening About closes Settings", page.locator("#settings.hide").count() == 1 and page.eval_on_selector(".navbtn.on", "e => e.dataset.nav") == "about")
            page.click("#libitem"); page.wait_for_selector("#about.hide", state="attached")
            check("going to another page closes About and lights that page", page.eval_on_selector(".navbtn.on", "e => e.dataset.nav") == "library")
            page.keyboard.press("Control+k"); page.wait_for_selector("#pal:not(.hide)"); page.keyboard.type("coffee"); page.wait_for_selector("#pal .pal-it"); page.keyboard.press("Enter"); page.wait_for_selector("#about:not(.hide)")
            check("the palette finds About by 'coffee' and opens it", "Ada Lovelace" in page.inner_text("#au-body"))
            page.click("#au-back")

            now_ = int(time.time())
            def set_support(**kw):
                json.dump({"first": now_ - 30 * 86400, "last": now_ - 3600, "visits": 6, "shown": 0, "last_shown": 0, "optout": False, "supported": False, **kw}, open(os.path.join(skd, "support.json"), "w"))
            def open_watch():
                page.goto("about:blank"); page.goto(base + "/#/watch")
            set_support(); open_watch(); page.wait_for_selector(".sup", timeout=30000)
            check("after 14 days and 5 visits the reminder appears in the corner, says who made the app, and offers the tip link, 'Maybe later' and 'Don't ask again'",
                  "saving you time" in page.inner_text(".sup") and "made by Ada Lovelace at Test University" in page.inner_text(".sup") and page.get_attribute("#sup-go", "href") == "https://ko-fi.com/adatest" and page.locator("#sup-later").count() == 1 and page.locator("#sup-never").count() == 1)
            check("it is a dialog with a name that does not take the focus away from what you are doing",
                  page.get_attribute(".sup", "role") == "dialog" and page.get_attribute(".sup", "aria-modal") == "false" and "saving you time" in page.inner_text("#" + page.get_attribute(".sup", "aria-labelledby")) and page.evaluate("document.activeElement.closest('.sup') === null"))
            for _ in range(40):
                if api("/api/support")["shown"] == 1:
                    break
                time.sleep(0.25)
            check("it counts as shown the moment it appears", api("/api/support")["shown"] == 1 and api("/api/support")["due"] is False)
            bad_ = []
            for scheme in ("light", "dark"):
                page.evaluate("window.setTheme(%r)" % scheme)
                r_ = page.evaluate(JS_A11Y); bad_ += [("name", x) for x in r_["noname"]] + [("id", x) for x in r_["dupid"]] + [(scheme, x) for x in page.evaluate(JS_CONTRAST, scheme)]
            page.evaluate("window.setTheme('light')")
            check("every control of the reminder has a name, and its text is readable (WCAG AA) in the light and the dark theme", not bad_, str(bad_[:4]))
            page.set_viewport_size({"width": 520, "height": 800})
            JS_FIT = "(() => { const r = document.querySelector('.sup').getBoundingClientRect(); return document.documentElement.scrollWidth <= window.innerWidth && r.left >= 0 && r.right <= window.innerWidth; })()"
            try:                                   # the page re-lays itself out after the resize: wait for it instead of guessing 200 ms (a slow runner measured too early)
                page.wait_for_function(JS_FIT, timeout=5000)
            except Exception:  # noqa: BLE001  (the check below says what is wrong, with the numbers)
                pass
            check("on a narrow window it still fits the screen and the page does not scroll sideways", page.evaluate(JS_FIT),
                  str(page.evaluate("[document.documentElement.scrollWidth, window.innerWidth, document.querySelector('.sup').getBoundingClientRect().toJSON()]")))
            page.set_viewport_size({"width": 1400, "height": 950})
            page.focus("#sup-later"); page.keyboard.press("Escape"); page.wait_for_selector(".sup", state="detached")
            check("Escape closes it", page.locator(".sup").count() == 0)
            open_watch(); page.wait_for_function("document.querySelector('#aboutbtn')"); page.wait_for_timeout(4500)
            check("having been shown, it does not come back on the next visit", page.locator(".sup").count() == 0)

            set_support(); open_watch(); page.wait_for_selector(".sup", timeout=30000); page.click("#sup-later"); page.wait_for_selector(".sup", state="detached")
            check("'Maybe later' just closes it (it is not asked again for 60 days)", api("/api/support")["optout"] is False and api("/api/support")["due"] is False and api("/api/support")["shown"] == 1)
            set_support(); open_watch(); page.wait_for_selector(".sup", timeout=30000); page.click("#sup-never"); page.wait_for_selector(".sup", state="detached"); page.wait_for_function("document.querySelector('#toast').textContent.includes('ask again')")
            check("'Don't ask again' ends the reminders and says where the link is", api("/api/support")["optout"] is True and "won’t ask again" in page.inner_text("#toast"))
            set_support(); open_watch(); page.wait_for_selector(".sup", timeout=30000)
            with ctx.expect_page() as np_:
                page.click("#sup-go")
            np_.value.close(); page.wait_for_selector(".sup", state="detached")
            check("pressing the tip link opens it, closes the card and ends the reminders", api("/api/support")["supported"] is True and api("/api/support")["due"] is False)

            set_support(); page.goto("about:blank"); page.goto(base + "/#/watch"); page.wait_for_function("window.Settings && document.querySelector('#settingsbtn')"); page.click("#settingsbtn"); page.wait_for_selector("#settings:not(.hide)")
            page.wait_for_timeout(4500)
            check("it does not appear over Settings", page.locator(".sup").count() == 0 and api("/api/support")["shown"] == 0)
            page.click("#st-back"); page.wait_for_selector(".sup", timeout=30000)
            check("…but follows once Settings is closed", api("/api/support")["shown"] == 1)
            set_support(first=now_ - 5 * 86400); open_watch(); page.wait_for_timeout(4500)
            check("before 14 days it never appears", page.locator(".sup").count() == 0)
            os.remove(os.path.join(skd, "support.json")); open(author_file, "w", encoding="utf-8").write(author_orig); os.remove(os.path.join(static_, "me.png"))
            page.goto("about:blank"); page.goto(base + "/#/library"); page.wait_for_selector("#libitem")

            print("accessibility: every control has a name, no repeated ids, readable text in both themes, a visible focus, a skip link, calm motion on request")
            bad_names, bad_ids, bad_con = [], [], []
            pages_ = ["/#/watch", "/#/library", "/#/library/hl", "/#/library/review", "/#/library/ask", "/#/folders", "/#/folder/lab", "/#/folder/lab/ask", "/#/folder/lab/matrix", "/#/folder/lab/synthesis", "/#/folder/lab/workspace", "/#/folder/lab/claims", "/#/folder/lab/draft"]
            page.emulate_media(color_scheme="dark"); page.evaluate("localStorage.removeItem('jw-theme')"); page.goto("about:blank"); page.goto(base + "/#/library"); page.wait_for_selector("#lf-q")
            check("the app starts in Light even when the computer is set to dark (unless you chose otherwise)", page.evaluate("document.documentElement.dataset.theme") == "light")
            page.evaluate("window.setTheme('auto')"); page.wait_for_timeout(150)
            check("…and 'Match my computer' follows the system setting", page.evaluate("document.documentElement.dataset.theme") == "dark" and page.evaluate("document.documentElement.dataset.themepref") == "auto")
            page.emulate_media(color_scheme="light")
            try:      # the page learns of the change through the browser's media-query event, which a busy runner can deliver well after 150 ms (about 8 ms here, 120 ms with the CPU slowed 10x): wait for it
                page.wait_for_function("document.documentElement.dataset.theme === 'light'", timeout=4000)
            except Exception:  # noqa: BLE001
                pass
            check("…live, when the system changes", page.evaluate("document.documentElement.dataset.theme") == "light")
            for scheme in ("light", "dark"):
                page.evaluate("window.setTheme(%r)" % scheme)
                for url in (pages_ if scheme == "light" else pages_[:2] + pages_[5:8] + pages_[9:11]):
                    page.goto("about:blank"); page.goto(base + url); page.wait_for_timeout(1000)
                    r = page.evaluate(JS_A11Y); bad_names += [(url, x) for x in r["noname"]]; bad_ids += [(url, x) for x in r["dupid"]]
                    bad_con += [(scheme, url, x) for x in page.evaluate(JS_CONTRAST, scheme)]
                page.goto("about:blank"); page.goto(base + "/#/library"); page.wait_for_selector("#settingsbtn"); page.click("#settingsbtn"); page.wait_for_selector("#st-body")
                for t in ("ai", "usage", "prompts", "reader", "files", "library", "look", "about"):
                    page.click('.st-%s button[data-t="%s"]' % ("sub" if t in ("ai", "usage", "prompts") else "tabs", t) if t != "ai" else '.st-tabs button[data-t="ai"]'); page.wait_for_timeout(300)
                    r = page.evaluate(JS_A11Y); bad_names += [("settings " + t, x) for x in r["noname"]]; bad_ids += [("settings " + t, x) for x in r["dupid"]]
                    bad_con += [(scheme, "settings " + t, x) for x in page.evaluate(JS_CONTRAST, scheme)]
            page.evaluate("window.setTheme('light')")
            page.goto("about:blank"); page.goto(base + "/#read=" + urllib.request.quote(rows[2][3], safe="")); page.wait_for_selector("#reader:not(.hide) .rd-band .textLayer span", timeout=30000); page.wait_for_timeout(800)
            for t in ("notes", "ask", "sum", "card"):
                if page.locator('.rd-tabs button[data-tab="%s"]' % t).count():
                    page.click('.rd-tabs button[data-tab="%s"]' % t); page.wait_for_timeout(300)
                    r = page.evaluate(JS_A11Y); bad_names += [("reader " + t, x) for x in r["noname"]]; bad_ids += [("reader " + t, x) for x in r["dupid"]]
            check("every button, link, field and select has a name a screen reader can say (Watch, Library, Folders, every folder tool, Settings)", not bad_names, str(bad_names[:6]))
            check("no id is used twice on a page", not bad_ids, str(bad_ids[:6]))
            check("text is readable (WCAG AA contrast) on those pages in the light and the dark theme", not bad_con, str(bad_con[:4]))
            page.goto("about:blank"); page.goto(base + "/#/library"); page.wait_for_selector("#libitem"); page.wait_for_timeout(800)
            page.evaluate("document.activeElement && document.activeElement.blur()"); page.keyboard.press("Tab")
            check("the first Tab offers to skip to the page, and Enter moves the focus there", page.evaluate("document.activeElement.id") == "skipnav" and page.evaluate("document.activeElement.getBoundingClientRect().top") >= 0)
            page.keyboard.press("Enter"); check("…the page itself", page.evaluate("document.activeElement.id") == "main-content")
            page.keyboard.press("Tab"); fo_ = page.evaluate("(() => { const e = document.activeElement, cs = getComputedStyle(e); return [e.tagName, cs.outlineStyle, parseFloat(cs.outlineWidth), cs.boxShadow]; })()")
            check("a control reached by keyboard shows where the focus is", fo_[1] != "none" and fo_[2] >= 1 or fo_[3] != "none", str(fo_))
            check("messages are announced (status region) and the page has one main landmark and a language", page.get_attribute("#toast", "role") == "status" and page.locator("main").count() == 1 and page.get_attribute("html", "lang") == "en")
            page.emulate_media(reduced_motion="reduce"); tdur = page.evaluate("parseFloat(getComputedStyle(document.querySelector('.btn')).transitionDuration)"); page.emulate_media(reduced_motion="no-preference")
            check("with 'reduce motion' on, transitions are switched off", tdur < 0.01, str(tdur))

            print("your choices follow you between computers (the page keeps them in the data folder)")
            prefs_now = lambda: api("/api/uiprefs")["prefs"]  # noqa: E731

            def until(cond, secs=8.0):
                end = time.time() + secs
                while time.time() < end:
                    try:
                        if cond():
                            return True
                    except Exception:  # noqa: BLE001
                        pass
                    time.sleep(0.15)
                return False
            POS_KEY = "jw-pos:Papers/Journal/Smith 2020 – Équilibre.pdf"
            page.evaluate("localStorage.setItem('jw-fp-view', 'list'); localStorage.setItem('jw-theme', 'dark'); localStorage.setItem('jw-sb', 'folded'); localStorage.setItem('jw-rd-side', '321')")
            check("a view choice and the theme reach the data folder within a moment", until(lambda: prefs_now().get("jw-fp-view") == "list" and prefs_now().get("jw-theme") == "dark"), str(prefs_now()))
            time.sleep(0.8)
            check("…but what depends on this screen (sidebar folded, reader panel width) stays in this browser", "jw-sb" not in prefs_now() and "jw-rd-side" not in prefs_now(), str(prefs_now()))
            # a second computer = a browser with nothing in it
            ctx2 = br.new_context(viewport={"width": 1400, "height": 950})
            p2 = ctx2.new_page(); p2.goto(base + "/#/watch"); p2.wait_for_selector(".navbtn", state="attached")
            st2 = p2.evaluate("({view: localStorage.getItem('jw-fp-view'), theme: localStorage.getItem('jw-theme'), sb: localStorage.getItem('jw-sb'), side: localStorage.getItem('jw-rd-side'), welcome: localStorage.getItem('jw-welcome'), painted: document.documentElement.dataset.theme})")
            check("a second computer starts with the same theme (already painted in it), view choice and 'guide seen' - and without the first one's sidebar and panel width", st2 == {"view": "list", "theme": "dark", "sb": None, "side": None, "welcome": "done", "painted": "dark"}, str(st2))
            p2.evaluate("localStorage.setItem('jw-fp-sort', 'date'); localStorage.removeItem('jw-fp-view')")
            p2.goto("about:blank")                      # (closing a page sends what is waiting at once)
            check("a change on the second computer, a removal too, reaches the folder when its page closes", until(lambda: prefs_now().get("jw-fp-sort") == "date" and "jw-fp-view" not in prefs_now()), str(prefs_now()))
            page.goto("about:blank"); page.goto(base + "/#/watch"); page.wait_for_selector(".navbtn", state="attached")
            st1 = page.evaluate("({sort: localStorage.getItem('jw-fp-sort'), view: localStorage.getItem('jw-fp-view'), sb: localStorage.getItem('jw-sb')})")
            check("the first computer picks both up when it next opens, and keeps its own sidebar choice", st1 == {"sort": "date", "view": None, "sb": "folded"}, str(st1))
            page.evaluate("localStorage.setItem(%s, JSON.stringify({n: 4, y: 0.25}))" % json.dumps(POS_KEY))
            time.sleep(1.2)
            check("where you stopped reading is sent in batches, not at every scroll (nothing within a second)", POS_KEY not in prefs_now(), str(list(prefs_now())))
            page.goto("about:blank")
            check("…and at once when the page closes", until(lambda: POS_KEY in prefs_now()) and json.loads(prefs_now()[POS_KEY]) == {"n": 4, "y": 0.25})
            # a browser that had choices of its own before it was ever in step: they are added, but the folder's own values win
            ctx3 = br.new_context(viewport={"width": 1400, "height": 950})
            ctx3.add_init_script("try { if (!localStorage.getItem('jw-prefsync')) { localStorage.setItem('jw-lsn-seed', '7'); localStorage.setItem('jw-theme', 'light'); } } catch (e) {}")
            p3 = ctx3.new_page(); p3.goto(base + "/#/watch"); p3.wait_for_selector(".navbtn", state="attached")
            st3 = p3.evaluate("({theme: localStorage.getItem('jw-theme'), painted: document.documentElement.dataset.theme})")
            check("a browser that had its own choices adds the new ones to the folder, and where both have one the folder's wins", until(lambda: prefs_now().get("jw-lsn-seed") == "7") and prefs_now().get("jw-theme") == "dark" and st3 == {"theme": "dark", "painted": "dark"}, "%s %s" % (st3, prefs_now()))
            api("/api/uiprefs", {"remove": ["jw-lsn-seed"]})                              # (removed on another computer)
            p3.reload(); p3.wait_for_selector(".navbtn", state="attached")
            check("a choice removed on another computer is removed here the next time the page opens", p3.evaluate("localStorage.getItem('jw-lsn-seed')") is None)
            check("only the named keys can be set through the server", api("/api/uiprefs", {"set": {"jw-sb": "x", "evil": "y", "jw-theme": "dark"}})["changed"] == 0 and "jw-sb" not in prefs_now() and "evil" not in prefs_now())
            ctx2.close(); ctx3.close()
            api("/api/uiprefs", {"remove": ["jw-fp-sort", "jw-fp-view", "jw-theme", POS_KEY, "jw-lsn-seed"]})                  # (back to the start for the sections after this one)
            page.goto("about:blank"); page.goto(base + "/#/watch"); page.wait_for_selector(".navbtn", state="attached")
            page.evaluate("localStorage.removeItem('jw-theme'); localStorage.removeItem('jw-fp-sort'); localStorage.removeItem('jw-sb'); localStorage.removeItem('jw-rd-side'); localStorage.removeItem('jw-fp-view'); localStorage.removeItem(%s)" % json.dumps(POS_KEY))
            page.goto("about:blank"); page.goto(base + "/#/watch"); page.wait_for_selector(".navbtn", state="attached")
            page.evaluate("document.documentElement.dataset.theme = 'light'")

            print("a failed screening says why and can be retried; the Library summarises the papers that have no summary")
            runs_dir = os.path.join(TMP, ".agents", "skills", "ais-journal-watcher", "state", "runs")
            rid = "2025-W02_weekly"                     # a past week, so that no weekly check of this test merges into it
            why404 = "openrouter: No endpoints found that can handle the requested parameters. (HTTP 404)"
            abbr0 = api("/api/config")["journals"][0]["abbr"]
            pp = [{"id": "rt-%d" % k, "title": t, "abbr": abbr0, "journal": "Test", "pub_date": "2025-01-01", "abstract": "We study platform governance.", "authors": ["A. Author"],
                   **({"verdict": "error", "relevance": 0, "reason": "screening failed: " + why404, "topics": []} if k < 2 else {})}
                  for k, t in enumerate(["Platform retry study A", "Platform retry study B", "Other retry study C"])]
            json.dump({"id": rid, "kind": "weekly", "params": {"topics": [], "query": ""}, "created": "2025-01-06T10:00:00+00:00", "stage": "ready", "status": "done",
                       "usage": {"in": 0, "out": 0, "cache_write": 0, "cache_read": 0}, "week": "2025-W02", "checks": ["2025-01-06T10:00:00+00:00"], "errors": [], "jstate": {},
                       "screen_error": why404, "papers": pp}, open(os.path.join(runs_dir, rid + ".json"), "w"))
            n_batches = len(batches())
            page.goto("about:blank"); page.goto(base + "/#/watch"); page.wait_for_selector("#weeklybtn", state="attached"); page.wait_for_timeout(400)
            page.evaluate("openRun({id: %s})" % json.dumps(rid)); page.wait_for_selector("#screenbtn:not(.hide)", timeout=8000)
            stats_t = page.inner_text("#stats")
            check("a weekly run whose screening failed offers 'Retry screening' for the failed and the unscreened papers, with the cost", "Retry screening (3" in page.inner_text("#screenbtn") and "$" in page.inner_text("#screenbtn"), page.inner_text("#screenbtn"))
            check("…the failed papers are counted ('Screening failed'), and the page says why the last screening stopped", re.search(r"2\s*SCREENING FAILED", stats_t.upper()) is not None and "No endpoints found" in page.inner_text("#screenerr") and page.locator("#screenerr:not(.hide)").count() == 1, stats_t)
            page.evaluate("go({j: '*'})"); page.wait_for_selector("#papers .paper")
            check("…and each failed card shows the provider's own words, not only 'screening failed'", "No endpoints found that can handle the requested parameters" in page.inner_text("#papers"), page.inner_text("#papers")[:300])
            page.click("#screenbtn")
            ok_ = False
            for _ in range(80):
                st_ = api("/api/state"); r_ = api("/api/runs/" + rid)
                if not st_["busy"] and all(x.get("verdict") in ("download", "skip") for x in r_["papers"]):
                    ok_ = True; break
                time.sleep(0.25)
            r_ = api("/api/runs/" + rid)
            check("pressing it asks the model again about those three papers only (one batch), and they get their verdicts", ok_ and len(batches()) == n_batches + 1 and batches()[-1] == "3"
                  and [x["verdict"] for x in r_["papers"]] == ["download", "download", "skip"] and r_["screen_error"] == "", str(([x.get("verdict") for x in r_["papers"]], batches()[n_batches:], r_.get("screen_error"))))
            page.wait_for_function("document.querySelector('#screenbtn').classList.contains('hide') && document.querySelector('#screenerr').classList.contains('hide')", timeout=8000)
            check("…then the button and the reason are gone, and 'Screening failed' is no longer counted", "SCREENING FAILED" not in page.inner_text("#stats").upper())
            page.evaluate("go({j: null})")
            # a run screened by a backup model (the chosen one cannot answer in a JSON schema): the page says which and why; one click makes it the screening model
            rid_b = "2025-W03_weekly"
            bk_ = {"provider": "openrouter", "model": "cheap/json", "name": "cheap/json", "price": 0.1, "current_price": 0.04, "within": False, "instead_of": "m1",
                   "why": "openrouter: m1: response_format is not supported by this model (HTTP 400)", "at": "2025-01-13T10:00:00+00:00"}
            json.dump({"id": rid_b, "kind": "weekly", "params": {"topics": [], "query": ""}, "created": "2025-01-13T10:00:00+00:00", "stage": "ready", "status": "done",
                       "usage": {"in": 0, "out": 0, "cache_write": 0, "cache_read": 0}, "week": "2025-W03", "checks": ["2025-01-13T10:00:00+00:00"], "errors": [], "jstate": {},
                       "screen_error": "", "screen_backup": bk_, "papers": [dict(p_, id="bk-%d" % k, verdict="download", relevance=70, reason="fits", topics=[]) for k, p_ in enumerate(pp)]},
                      open(os.path.join(runs_dir, rid_b + ".json"), "w"))
            aij = os.path.join(TMP, ".agents", "skills", "ais-journal-watcher", "ai.json"); aij_saved = open(aij).read()
            page.evaluate("openRun({id: %s})" % json.dumps(rid_b)); page.wait_for_selector("#screenbackup:not(.hide)", timeout=8000)
            nt_ = page.inner_text("#screenbackup")
            check("a run screened by a backup model says so: which model, instead of which, why, and that the setting is unchanged; the price note when it costs more than twice as much",
                  "cheap/json" in nt_ and "m1" in nt_ and "response_format" in nt_ and "unchanged" in nt_ and "twice its price" in nt_ and page.locator("#screenerr.hide").count() == 1
                  and page.is_visible("#makebackup") and "Make cheap/json the screening model" in page.inner_text("#makebackup"), nt_)
            page.click("#makebackup")
            page.wait_for_function("document.querySelector('#makebackup').classList.contains('hide')", timeout=8000)
            scr_ = json.load(open(aij))["features"].get("screen") or {}
            check("…'Make it the screening model' sets Settings → AI → Screening new papers to that provider and model (nothing else changes), and the note then says so",
                  scr_.get("provider") == "openrouter" and scr_.get("model") == "cheap/json" and "is now your screening model" in page.inner_text("#screenbackup")
                  and json.load(open(aij))["features"]["ask"] == {"provider": "fake", "model": "m1"}, str(scr_))
            open(aij, "w").write(aij_saved); os.remove(os.path.join(runs_dir, rid_b + ".json"))
            page.evaluate("go({j: null})")
            # Settings → AI: the screening Test sends a real screening request (the fake model answers that one with other JSON: it cannot screen)
            page.evaluate("window.Settings.open('ai')"); page.wait_for_selector('tr[data-f="screen"] [data-testf]')
            page.click('tr[data-f="screen"] [data-testf]'); page.wait_for_function("(() => { const t = document.querySelector('tr[data-f=\"screen\"] .tres'); return t && t.textContent && t.textContent !== '…'; })()", timeout=15000)
            check("Settings → AI: Test on 'Screening new papers' finds a model that answers but cannot give screening verdicts", "not with the structured verdicts" in page.inner_text('tr[data-f="screen"] .tres') and page.locator('tr[data-f="screen"] .tres.bad').count() == 1, page.inner_text('tr[data-f="screen"] .tres'))
            page.click("#mn-test"); page.wait_for_function("(() => { const t = document.querySelector('#st-main .tres'); return t && t.textContent && t.textContent !== '…'; })()", timeout=15000)
            check("…and 'Use one for everything' → Test says the model works but cannot screen, before it is applied to screening too", "can’t screen papers" in page.inner_text("#st-main .tres") and page.inner_text("#st-main .tres").startswith("✓"), page.inner_text("#st-main .tres"))
            page.keyboard.press("Escape"); page.evaluate("window.Settings && window.Settings.close && window.Settings.close()")
            # the Library: one more downloaded paper without a summary, so that there is always one to do
            idxp = os.path.join(TMP, "Papers", "_index.csv"); saved_idx = open(idxp, newline="").read()
            sumdir = os.path.join(TMP, "Papers", "2025-04", "SumTest"); os.makedirs(sumdir, exist_ok=True)
            import pymupdf as _pm2
            d2 = _pm2.open(); pg2 = d2.new_page(); pg2.insert_textbox(_pm2.Rect(60, 60, 540, 790), "Summaries for papers that have none. " * 60, fontsize=10); d2.save(os.path.join(sumdir, "sumtest.pdf")); d2.close()
            with open(idxp, "a", newline="") as f_:
                csv.writer(f_).writerow(["2026-10-29", "Test Journal", "TJ", "A paper waiting for its summary", "Jane Doe", "10.9999/sum.1", "", "", "", "Papers/2025-04/SumTest/sumtest.pdf", "ok", "browser"])
            est_ = api("/api/summaries")
            check("the server lists the downloaded papers without a summary (books left out), with the model and a cost guess that says what it rests on",
                  est_["n"] >= 1 and "Papers/2025-04/SumTest/sumtest.pdf" in est_["files"] and est_["provider"] == "fake" and est_["model"] == "m1" and est_["basis"] in ("history", "prices") and est_["cost"] is not None
                  and not any("/Books/" in f for f in est_["files"]), str({k: est_[k] for k in ("n", "basis", "cost", "provider", "model")}))
            page.goto("about:blank"); page.goto(base + "/#/library"); page.wait_for_selector("#lf-sum:not(.hide)", timeout=8000)
            check("the Library shows 'Summarise N papers…' with that count", ("Summarise %d paper" % est_["n"]) in page.inner_text("#lf-sum"), page.inner_text("#lf-sum"))
            page.click("#lf-sum"); page.wait_for_selector("#lf-sumgo", timeout=8000)
            box_t = page.inner_text("#lf-sumbox")
            check("…pressing it first says how many, which provider and model, and what it may cost, and spends nothing", ("%d paper" % est_["n"]) in box_t and "fake · m1" in box_t and "Cost:" in box_t and "no summary yet" in box_t
                  and not os.path.exists(os.path.join(sumdir, "sumtest.md")), box_t)
            page.click("#lf-sumno"); page.wait_for_selector("#lf-sumbox.hide", state="attached")
            check("…Cancel closes it and writes nothing", not os.path.exists(os.path.join(sumdir, "sumtest.md")))
            page.click("#lf-sum"); page.wait_for_selector("#lf-sumgo"); page.click("#lf-sumgo")
            done_ = False
            for _ in range(160):
                if not api("/api/summaries")["status"]["running"] and os.path.exists(os.path.join(sumdir, "sumtest.md")):
                    done_ = True; break
                time.sleep(0.25)
            check("'Write the summaries' writes each summary beside its PDF in the background (the fake model's summary)", done_ and "FAKE SUMMARY" in open(os.path.join(sumdir, "sumtest.md")).read(), str(api("/api/summaries")["status"]))
            try:
                page.wait_for_function("/Summaries finished/.test(document.querySelector('#lf-sumbox').innerText)", timeout=15000)
            except Exception:  # noqa: BLE001  (the check below says what is wrong)
                pass
            check("…the Library says what came of it, and the button is gone once nothing is left", "Summaries finished" in page.inner_text("#lf-sumbox") and api("/api/summaries")["n"] == 0
                  and page.locator("#lf-sum.hide").count() == 1, page.inner_text("#lf-sumbox"))
            check("…and the job was listed in the AI activity tray", any(j["kind"] == "summary" for j in api("/api/activity")["jobs"]), str([j["kind"] for j in api("/api/activity")["jobs"]][:8]))
            open(idxp, "w", newline="").write(saved_idx); shutil.rmtree(sumdir, ignore_errors=True)
            page.goto("about:blank"); page.goto(base + "/#/watch"); page.wait_for_selector(".navbtn", state="attached")

            print("costs say which models ran and where their prices come from")
            page.goto("about:blank"); page.goto(base + "/#/watch"); page.wait_for_selector("#cost", state="attached")
            show_ = lambda js: page.evaluate("(() => { setCost(" + js + "); const e = document.querySelector('#cost'); return [e.innerText, e.title]; })()")   # (in one step: the page's own run loading may call setCost too)
            t_, tip_ = show_("""0.0202, {cost: 0.0202, unpriced: 1, models: [{name: 'Claude Sonnet 5.5', calls: 2, in: 2000, out: 200, cost: 0.02, unpriced: 0},
                {name: 'scaledown/summarize', calls: 1, in: 1000, out: 100, cost: 0.0002, unpriced: 0}, {name: 'gpt-x', calls: 1, in: 900, out: 100, cost: 0, unpriced: 1}]}""")
            check("the run's cost names the models that ran, and says 'at least' when a call has no price", t_.startswith("AI cost this run: at least $0.02") and "Claude Sonnet 5.5, scaledown/summarize +1" in t_ and "1 call without a price" in t_ and "Sonnet cost" not in t_, t_)
            check("hovering it lists each model's calls, tokens and cost", "scaledown/summarize · 1 call · 1,000 tokens in, 100 out · $0.0002" in tip_ and "gpt-x · 1 call" in tip_ and "(and 1 without a price)" in tip_, tip_)
            t_, _ = show_("0.25, {cost: 0.25, unpriced: 0, models: [{name: 'Claude Sonnet 5.5', calls: null, in: 100000, out: 5000, cost: 0.25, unpriced: 0}]}")
            check("an old run shows its cost with its model and without 'at least'", t_ == "AI cost this run: $0.25 · Claude Sonnet 5.5", t_)
            t_, _ = show_("0, {cost: 0, unpriced: 0, models: []}")
            check("a run that cost nothing says nothing", t_ == "", t_)
            page.evaluate("setCost(0.01, {cost: 0.01, unpriced: 2, models: [{name: 'gpt-x', calls: 2, in: 1, out: 1, cost: 0, unpriced: 2}]}); document.querySelector('[data-costprices]').click()")
            page.wait_for_selector("#st-prices", timeout=10000)
            rows_ = page.evaluate("[...document.querySelectorAll('#st-prices tr[data-m]')].map(r => [r.dataset.m, r.querySelector('.psrc').innerText])")
            check("'calls without a price' opens Settings → AI at the prices, where each model says where its price comes from (m1: typed in Settings)", any(m == "m1" and s.startswith("yours") for m, s in rows_), str(rows_))
            cfg_ = api("/api/config")
            check("the screening estimate is the screening model's price, and the page knows that model's name", cfg_.get("est_cost_per_paper") is not None and abs(cfg_["est_cost_per_paper"] - (1500 / 3.3 * 1.0 + 60 * 2.0) / 1e6) < 1e-6 and cfg_.get("screen_model") == "m1", str({k: cfg_.get(k) for k in ("est_cost_per_paper", "screen_model")}))
            page.evaluate("window.Settings && Settings.close()")

            print("you can delete what you added")
            import pymupdf as _pmu                                         # (a paper of its own, so the papers later checks use stay)
            delp = os.path.join(TMP, "Papers", "2025-04", "DelTest"); os.makedirs(delp, exist_ok=True)
            d_ = _pmu.open(); d_.new_page().insert_text((72, 72), "A Paper Added Only To Be Deleted"); d_.save(os.path.join(delp, "gone.pdf")); d_.close()
            open(os.path.join(delp, "gone.notes.json"), "w").write('{"notes": [{"text": "my note"}]}'); open(os.path.join(delp, "gone.md"), "w").write("its summary")
            hdr_ = next(csv.reader(open(os.path.join(TMP, "Papers", "_index.csv"), newline="")))
            with open(os.path.join(TMP, "Papers", "_index.csv"), "a", newline="") as f_:
                csv.DictWriter(f_, hdr_, extrasaction="ignore").writerow({"date_checked": "2026-10-29", "journal": "Test Journal", "abbr": "TJ", "title": "A Paper Added Only To Be Deleted", "authors": "Jane Doe", "doi": "10.9999/del.gone", "file": "Papers/2025-04/DelTest/gone.pdf", "status": "ok", "note": "browser"})
            page.goto("about:blank"); page.goto(base + "/"); page.click("#libitem"); page.wait_for_selector("#lf-q"); page.wait_for_function("document.querySelectorAll('#lf-folder option').length >= 2")
            victim = [r_ for r_ in api("/api/library") if r_["file"] == "Papers/2025-04/DelTest/gone.pdf"][0]
            page.fill("#lf-q", victim["title"][:40]); page.wait_for_function("(t) => [...document.querySelectorAll('.librow')].some(e => e.innerText.includes(t))", arg=victim["title"][:25])
            row_ = page.locator(".librow", has_text=victim["title"][:25]).first
            page.wait_for_timeout(600)                                    # (the search re-draws the list after a pause: open the menu once it has settled)
            row_.locator("[data-more]").click(); row_.locator(".rowmenu:not(.hide)").wait_for(timeout=5000)
            check("each paper's menu has 'Delete this paper…'", row_.locator("[data-delitem]").inner_text().strip().startswith("Delete this paper"))
            n_dlg = len(dialogs)                                          # (the page's dialogs are accepted and recorded by the handler set up above)
            row_.locator("[data-delitem]").click()
            page.wait_for_function("(t) => ![...document.querySelectorAll('.librow')].some(e => e.innerText.includes(t))", arg=victim["title"][:25], timeout=10000)
            dlg_ = dialogs[n_dlg:]
            check("deleting asks first, saying what goes and that it is not downloaded again", dlg_ and "for good" in dlg_[0] and "notes, highlights, summary and card" in dlg_[0] and "will not download it again" in dlg_[0], dlg_[:1])
            check("…and the paper, its notes and its summary are gone, and the index says you deleted it", victim["file"] not in [r_["file"] for r_ in api("/api/library")] and not any(os.path.exists(os.path.join(delp, n_)) for n_ in ("gone.pdf", "gone.notes.json", "gone.md"))
                  and any(r_["doi"] == "10.9999/del.gone" and r_["status"] == "deleted" and not r_["file"] for r_ in csv.DictReader(open(os.path.join(TMP, "Papers", "_index.csv"), newline=""))), str(os.listdir(delp)))
            page.fill("#lf-q", "")
            n_runs = len(api("/api/runs"))
            if n_runs:
                page.click("#watchitem"); page.wait_for_selector("#runs .runitem [data-rundel]", state="attached")
                rid_ = page.get_attribute("#runs .runitem [data-rundel]", "data-rundel")       # (the first run in the tab that is shown)
                n_dlg = len(dialogs)
                page.evaluate("(id) => document.querySelector(`#runs [data-rundel=\"${id}\"]`).click()", rid_)
                page.wait_for_function("(id) => !document.querySelector(`#runs [data-rundel=\"${id}\"]`)", arg=rid_, timeout=10000)
                check("a past run can be deleted from the Watch list after asking (its papers stay)", len(api("/api/runs")) == n_runs - 1 and rid_ not in [x["id"] for x in api("/api/runs")] and any("papers it downloaded stay" in m for m in dialogs[n_dlg:]), str(dialogs[n_dlg:])[:200])
            page.evaluate("window.Settings.open('ai')"); page.wait_for_selector('tr[data-p="fake"]')
            has_rm = page.locator('tr[data-p="fake"] [data-keyremove]').count() == 1
            check("a provider with a saved key has 'Remove' next to 'Change'", has_rm)
            page.evaluate("window.Settings && Settings.close()")

            print("exports and the library layout")
            bib = urllib.request.urlopen(base + "/api/export/bib?file=" + urllib.request.quote(rows[0][3])).read().decode()
            check("BibTeX export works", "@article{" in bib and "Open Platforms" in bib)
            check("no JavaScript errors in the console", not errors, "; ".join(errors[:3]))
            br.close()
        after = sorted(f for f in os.listdir(os.path.dirname(sidecar(0, ".x"))) if not f.endswith(".json") and not f.endswith(".md"))
        check("papers and filenames in Papers/ unchanged apart from sidecars", after == before, str((before, after)))
    finally:
        srv.terminate(); srv.wait(timeout=10); srv_ai.shutdown(); srv_oa.shutdown(); srv_up.shutdown(); shutil.rmtree(TMP, ignore_errors=True)
    print("\n%d failed" % len(FAILS) if FAILS else "\nall passed")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
