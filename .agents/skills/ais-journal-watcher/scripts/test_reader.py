"""Offline tests for the reader's backend: notes store, AI layer (against a fake OpenAI-compatible server), PDF blocks,
embedding search and the Ask pipeline. No network, no API key, no paid calls, and the real library is never touched.

    .venv/bin/python .agents/skills/ais-journal-watcher/scripts/test_reader.py
"""
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import traceback
import urllib.parse
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
# nothing in this file may touch a real data folder: the modules below find their folders through JW_HOME, so point it at a scratch home (with the starting settings) first
import atexit  # noqa: E402
_HOME = tempfile.mkdtemp(prefix="jw-reader-home-")
os.environ["JW_HOME"] = _HOME
atexit.register(shutil.rmtree, _HOME, ignore_errors=True)
sys.path.insert(0, HERE)
import paths  # noqa: E402
paths.seed_home()
import ai  # noqa: E402
import ask  # noqa: E402
import bibimport as bi  # noqa: E402
import blocks as bl  # noqa: E402
import claims as cl  # noqa: E402
import download as dl  # noqa: E402
import embed  # noqa: E402
import evidence as evd  # noqa: E402
import flashcards as fc  # noqa: E402
import folders as fo  # noqa: E402
import fulltext as ft  # noqa: E402
import ideas as idl  # noqa: E402
import authorcache as ac  # noqa: E402
import matrix as mx  # noqa: E402
import mcp_server as mcp  # noqa: E402
import related as rl  # noqa: E402
import skim as sk  # noqa: E402
import smart as sm  # noqa: E402
import synthesis as sy  # noqa: E402
import signins as si  # noqa: E402
import updater as up  # noqa: E402
import browser as brw  # noqa: E402
import workspace as wk  # noqa: E402
import multiask as ma  # noqa: E402
import notes_store as ns  # noqa: E402
import papercard as pc  # noqa: E402
import personal as pe  # noqa: E402
import relatedwork as rw  # noqa: E402
import reviewer as rv  # noqa: E402

TMP = tempfile.mkdtemp(prefix="jw-test-")
PAPERS = os.path.join(TMP, "Papers")
ns.PAPERS = ns.ROOT = None  # set below (ROOT = TMP so "Papers/..." paths resolve inside the temp dir)
ns.ROOT, ns.PAPERS, ns.TAGS_PATH = TMP, PAPERS, os.path.join(PAPERS, "_tags.json")
dl.PAPERS = PAPERS
bl.INDEX = os.path.join(TMP, "index")
ai.STATE, ai.LEDGER, ai.CONFIG, ai.MODELS = TMP, os.path.join(TMP, "ai_usage.jsonl"), os.path.join(TMP, "ai.json"), os.path.join(TMP, "models")
ai.CACHE = os.path.join(TMP, "ai_cache")
fo.DIR = os.path.join(PAPERS, "_folders")
pc.BACKUPS = os.path.join(TMP, "card_backups")
FAILS = []


def check(name, fn):
    try:
        fn()
        print("  PASS  " + name)
    except Exception:  # noqa: BLE001
        FAILS.append(name)
        print("  FAIL  " + name)
        traceback.print_exc()


# ---------------------------------------------------------------- fake OpenAI-compatible server
SEEN = []


class Fake(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        SEEN.append((self.path, body, self.headers.get("Authorization")))
        if self.headers.get("Authorization") == "Bearer bad":
            data = json.dumps({"error": {"message": "Incorrect API key"}}).encode()
            self.send_response(401); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
        if self.path.endswith("/embeddings"):
            rows = []
            for i, t in enumerate(body["input"]):
                v = [0.0] * 512     # a crude bag of words: enough to tell passages apart in a test
                for w in t.lower().replace(".", " ").replace("?", " ").replace(",", " ").split():
                    if len(w) > 3:
                        v[zlib.crc32(w.encode()) % 512] += 1
                rows.append({"index": i, "embedding": v})
            data = json.dumps({"data": rows}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
        # chat completions, streamed
        if "stream_options" in body and getattr(Fake, "reject_stream_options", False):
            data = json.dumps({"error": {"message": "unknown field stream_options"}}).encode()
            self.send_response(400); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.send_header("Connection", "close"); self.end_headers()
        pieces = Fake.responder(body) if getattr(Fake, "responder", None) else getattr(Fake, "pieces", ["The sample has ", "120 firms (2)."])
        for k, piece in enumerate(pieces):
            delta = {"content": piece}
            if k == 0 and getattr(Fake, "web_urls", None):      # web-search citations, as OpenRouter / OpenAI send them
                delta["annotations"] = [{"url_citation": {"url": u, "title": "page"}} for u in Fake.web_urls]
            self.wfile.write(("data: " + json.dumps({"choices": [{"delta": delta}]}) + "\n\n").encode())
        if "stream_options" in body:
            self.wfile.write(("data: " + json.dumps({"choices": [], "usage": {"prompt_tokens": 1000, "completion_tokens": 50,
                              "prompt_tokens_details": {"cached_tokens": 200}}}) + "\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n"); self.close_connection = True


srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = "http://127.0.0.1:%d/v1" % srv.server_address[1]
os.environ["FAKE_KEY"] = "good"
CFG = {"providers": {"fake": {"type": "openai_compat", "base_url": URL, "key_env": "FAKE_KEY", "models": ["m1"]},
                     "nokey": {"type": "openai_compat", "base_url": URL, "key_env": "NO_SUCH_KEY_XYZ"},
                     "bad": {"type": "openai_compat", "base_url": URL, "key_env": "BAD_KEY"}},
       "features": {"ask": {"provider": "fake", "model": "m1"}},
       "embeddings": {"provider": "fake", "model": "emb"},
       "prices": {"m1": {"in": 1.0, "out": 2.0, "cache_write": 0, "cache_read": 0.1}}}
os.environ["BAD_KEY"] = "bad"


def write_cfg(cfg):
    with open(ai.CONFIG, "w") as f:
        json.dump(cfg, f)


# ---------------------------------------------------------------- a small two-column PDF
def make_pdf(path):
    import pymupdf
    doc = pymupdf.open()
    left = ["Open innovation platforms attract complementors when governance rules are transparent and stable over time.",
            "Platform owners who open their interfaces too widely risk losing control of quality, which is a central tension."]
    right = ["Our sample covers 120 firms observed over six years, drawn from the software platform industry.",
             "We estimate fixed effects regressions and find that transparency raises complementor entry by twelve percent."]
    for pno in range(4):
        pg = doc.new_page(width=595, height=842)
        pg.insert_textbox(pymupdf.Rect(60, 20, 535, 40), "Journal of Test Platforms 2026", fontsize=9)           # running header
        pg.insert_textbox(pymupdf.Rect(60, 800, 535, 820), "Page %d" % (pno + 1), fontsize=9)                   # page number
        if pno == 0:
            pg.insert_textbox(pymupdf.Rect(60, 70, 535, 130), "Governing Open Platforms: Evidence from Software Ecosystems", fontsize=18)
        for i, t in enumerate(left):
            pg.insert_textbox(pymupdf.Rect(60, 140 + i * 120, 290, 250 + i * 120), t, fontsize=11)
        for i, t in enumerate(right):
            pg.insert_textbox(pymupdf.Rect(310, 140 + i * 120, 540, 250 + i * 120), t, fontsize=11)
    doc.save(path); doc.close()


def main():
    print("notes store")
    d = os.path.join(PAPERS, "2026-04", "MIS Quarterly"); os.makedirs(d)
    pdf = os.path.join(d, "x.pdf"); make_pdf(pdf)
    rel = "Papers/2026-04/MIS Quarterly/x.pdf"

    def t_paths():
        assert ns.pdf_path(rel) == os.path.realpath(pdf)
        assert ns.pdf_path("Papers/../../etc/passwd") is None and ns.pdf_path("Papers/2026-04/\0x.pdf") is None
        assert ns.pdf_path("Papers/2026-04/MIS Quarterly/missing.pdf") is None
    check("paths are jailed to Papers/", t_paths)

    def t_notes():
        n = ns.load(pdf, {"id": "10.1/x"}); assert n["rev"] == 0 and not os.path.exists(ns.notes_path(pdf))    # nothing written yet
        n["highlights"].append({"id": "h1", "page": 1}); out = ns.save(pdf, n, 0); assert out["rev"] == 1
        assert ns.counts(pdf) == {"highlights": 1, "cards": 0}
        try:
            ns.save(pdf, n, 0); raise AssertionError("expected conflict")
        except ns.Conflict as c:
            assert c.current["rev"] == 1
        assert ns.save(pdf, out, 1)["rev"] == 2 and len(ns.sidecars(pdf)) == 1
        assert ns.save_tags([{"id": "t1", "name": "a"}, {"bad": 1}], 0)["tags"] == [{"id": "t1", "name": "a"}]
    check("sidecar created on first save, rev conflict, tags", t_notes)

    print("blocks")
    bs = bl.extract(pdf)

    def t_blocks():
        texts = [b["text"] for b in bs]
        assert not any("Journal of Test Platforms" in t or t.startswith("Page ") for t in texts), texts[:3]
        i_left = next(i for i, b in enumerate(bs) if b["page"] == 1 and "transparent and stable" in b["text"])
        i_right = next(i for i, b in enumerate(bs) if b["page"] == 1 and "120 firms" in b["text"])
        assert i_left < i_right, "left column must be read before the right one"
        assert any("Governing Open Platforms" in b["text"] for b in bs if b["page"] == 0)
        assert [b["idx"] for b in bs] == list(range(len(bs)))
        assert os.path.exists(os.path.join(bl.INDEX, ns.sha1_of(pdf) + ".blocks.json")) and bl.extract(pdf) == bs
    check("reading order, headers dropped, cached", t_blocks)

    print("ai layer")
    write_cfg(CFG)

    def t_stream():
        got = list(ai.stream("ask", [{"role": "user", "content": "hi"}], system="sys"))
        assert [k for k, _ in got] == ["text", "text", "done"]
        r = got[-1][1]
        assert r.text == "The sample has 120 firms (2)." and r.provider == "fake" and r.model == "m1"
        assert r.usage == {"in": 800, "out": 50, "cache_write": 0, "cache_read": 200}, r.usage
        assert abs(r.cost - (800 * 1.0 + 50 * 2.0 + 200 * 0.1) / 1e6) < 1e-12
        path, body, auth = SEEN[-1]
        assert path == "/v1/chat/completions" and auth == "Bearer good" and body["messages"][0] == {"role": "system", "content": "sys"}
        assert body["stream"] is True and body["max_tokens"] == 4000
    check("openai_compat streaming, usage, cost, request shape", t_stream)

    def t_ledger():
        rows = [json.loads(x) for x in open(ai.LEDGER)]
        assert rows[-1]["feature"] == "ask" and rows[-1]["model"] == "m1" and rows[-1]["out"] == 50
        s = ai.usage_summary(); assert s["calls"] == len(rows) and s["by_feature"]["ask"]["calls"] == len(rows)
    check("usage ledger", t_ledger)

    def t_schema():
        ai.chat("ask", [{"role": "user", "content": "x"}], schema={"type": "object"})
        assert SEEN[-1][1]["response_format"]["json_schema"]["schema"] == {"type": "object"}
    check("json schema output is requested", t_schema)

    def t_fallback():
        Fake.reject_stream_options = True
        try:
            r = ai.chat("ask", [{"role": "user", "content": "x"}])
            assert r.text.startswith("The sample") and "stream_options" not in SEEN[-1][1]
        finally:
            Fake.reject_stream_options = False
    check("retries without stream_options when the server rejects it", t_fallback)

    def t_errors():
        c = json.loads(json.dumps(CFG)); c["features"]["x"] = {"provider": "nokey", "model": "m"}; c["features"]["y"] = {"provider": "bad", "model": "m"}
        write_cfg(c)
        try:
            ai.chat("x", [{"role": "user", "content": "x"}]); raise AssertionError("expected AIError")
        except ai.AIError as e:
            assert "NO_SUCH_KEY_XYZ" in str(e)
        try:
            ai.chat("y", [{"role": "user", "content": "x"}]); raise AssertionError("expected AIError")
        except ai.AIError as e:
            assert "Incorrect API key" in str(e) and "401" in str(e)
        write_cfg(CFG)
    check("missing key and HTTP errors are readable", t_errors)

    def t_config():
        c = ai.config()
        assert c["providers"]["fake"]["configured"] and not c["providers"]["nokey"]["configured"]
        assert "good" not in json.dumps(c)      # the variable *name* (FAKE_KEY) may show, never its value
        out = ai.save_config({"features": {"ask": {"provider": "nokey", "model": " m2 "}}})
        assert out["features"]["ask"] == {"provider": "nokey", "model": "m2"}
        for bad in ({"features": {"ask": {"provider": "zzz", "model": "m"}}}, {"features": {"ask": {"provider": "fake", "model": ""}}}):
            try:
                ai.save_config(bad); raise AssertionError("expected ValueError")
            except ValueError:
                pass
        write_cfg(CFG)
    check("config never exposes keys; save validates", t_config)

    print("embeddings + ask")

    def t_embed():
        v = ai.embed(["alpha beta", "gamma"])
        assert v.shape == (2, 512) and abs(float((v[0] ** 2).sum()) - 1) < 1e-5
        r = embed.index_paper(pdf); assert r["n"] == len(bs) and not r["cached"]
        assert embed.index_paper(pdf)["cached"]
        top = embed.search(pdf, "How many firms are in the sample?", k=3)
        assert "120 firms" in top[0]["text"], top[0]["text"]
        assert top[0]["score"] >= top[1]["score"]
    check("index once, nearest passage found", t_embed)

    def t_ask():
        msgs = list(ask.stream(pdf, "How many firms are in the sample?", "rag"))
        kinds = [m["type"] for m in msgs]
        assert kinds[0] == "status" and "passages" in kinds and "text" in kinds and kinds[-1] == "done", kinds
        ps = next(m for m in msgs if m["type"] == "passages")["passages"]
        assert all(set(p) == {"n", "page", "quote"} for p in ps) and ps[0]["n"] == 1
        sent = SEEN[-1][1]["messages"][-1]["content"]
        assert "[1] (p." in sent and "Question: How many firms" in sent
        assert any("120 firms" in sent for _ in [0]) and msgs[-1]["model"] == "m1"
        full = list(ask.stream(pdf, "Summarise.", "full")); sent = SEEN[-1][1]["messages"][-1]["content"]
        assert sent.count("[") >= len(bs) and full[-1]["type"] == "done"        # every passage is sent in whole-paper mode
    check("ask pipeline: passages numbered, cited, streamed", t_ask)

    def t_ask_err():
        write_cfg({**CFG, "features": {"ask": {"provider": "nokey", "model": "m"}}})
        msgs = list(ask.stream(pdf, "q", "rag"))
        assert msgs[-1]["type"] == "error" and "NO_SUCH_KEY_XYZ" in msgs[-1]["error"]
        write_cfg(CFG)
    check("ask reports a missing key as an error message", t_ask_err)


    print("library: folders, status, search, import, exports")
    import exports
    import folders as fo
    import importer
    import library_search as ls
    import readstate as rs
    fo.DIR = os.path.join(PAPERS, "_folders"); rs.PATH = os.path.join(PAPERS, "_status.json"); ls.ROOT = TMP; dl.ROOT = TMP
    exports.CACHE = os.path.join(TMP, "crossref_cache.json")

    def t_folders():
        f = fo.create("Platform governance"); assert f["id"] == "platform-governance"
        try:
            fo.create("platform GOVERNANCE"); raise AssertionError("duplicate name")
        except ValueError:
            pass
        fo.add(f["id"], ["10.1/a", "10.1/b", "10.1/a"]); assert fo.get(f["id"])["papers"] == ["10.1/a", "10.1/b"]
        fo.remove(f["id"], ["10.1/a"]); assert fo.membership() == {"10.1/b": ["platform-governance"]}
        assert fo.rename(f["id"], "Governance")["name"] == "Governance" and fo.list_all()[0]["count"] == 1
        assert fo.get("../x") is None and fo.get("nope") is None
        fo.delete(f["id"]); assert fo.list_all() == [] and not os.path.exists(os.path.join(fo.DIR, f["id"] + ".json"))
    check("folders: create, duplicate name, add/remove, rename, delete", t_folders)

    def t_state():
        assert rs.touch_open("k1", 3, 10)["status"] == "reading"
        assert rs.update("k1", {"status": "read", "star": True, "takeaway": "x" * 500})["status"] == "read"
        assert rs.touch_open("k1")["status"] == "read" and len(rs.load()["k1"]["takeaway"]) == 400      # opening never changes a status you set
        try:
            rs.update("k1", {"status": "bogus"}); raise AssertionError("bad status")
        except ValueError:
            pass
        rs.update("k1", {"status": "", "star": False, "takeaway": ""}); assert "k1" not in rs.load() or "status" not in rs.load()["k1"]
    check("reading status: auto 'reading', explicit status wins, validation, empty entries dropped", t_state)

    # three papers; the first has highlights and a note card, the second a summary
    os.makedirs(os.path.join(PAPERS, "2026-04", "MIS Quarterly"), exist_ok=True)
    rows = []
    for n, (title, au, summ) in enumerate([("Open Platforms and Complementors", "Jing Tian; Ling Xue", None), ("Governance of Ecosystems", "Raul Castro Fernandez", "# Governance\n\n**Authors**: x\n\nThis paper studies **platform governance** in depth."),
                                          ("Chatbots and Trust", "Grace Zhang", None)]):
        rel = "Papers/2026-04/MIS Quarterly/p%d.pdf" % n
        shutil.copy(pdf, os.path.join(TMP, rel))
        if summ:
            open(os.path.join(TMP, rel[:-4] + ".md"), "w").write(summ)
        rows.append({"file": rel, "key": "10.1/p%d" % n, "title": title, "authors": au, "journal": "MIS Quarterly", "abbr": "MISQ", "year": "2026", "doi": "10.1/p%d" % n,
                     "url": "", "matched_topics": "Platform Economics", "summary": rel[:-4] + ".md" if summ else None, "notes": None, "state": {}, "folders": []})
    n0 = ns.load(os.path.join(TMP, rows[0]["file"]))
    n0["highlights"] = [{"id": "h1", "color": "green", "parts": [{"page": 2, "rects": [[0.1, 0.2, 0.5, 0.02]]}], "text": "transparent governance rules attract complementors", "comment": "check endogeneity", "tags": ["t1"], "created": "2026-10-01T10:00:00Z"}]
    n0["cards"] = [{"id": "c1", "type": "excerpt", "text": "x", "note": "my idea about two-sided markets", "tags": [], "src": {"hl": "h1"}, "x": 0, "y": 0, "w": 280}]
    ns.save(os.path.join(TMP, rows[0]["file"]), n0, 0); rows[0]["notes"] = {"highlights": 1, "cards": 1}
    rows[1]["state"] = {"status": "read", "star": True, "takeaway": "defines governance mechanisms", "opened": "2026-10-02T00:00:00+00:00"}; rows[1]["folders"] = ["f1"]

    def t_search():
        def files(**k):
            return [x["file"].rsplit("/", 1)[-1] for x in ls.search(rows, **k)]
        assert files(q="complementors") == ["p0.pdf"]                          # found only through *your highlight*
        assert files(q="endogeneity") == ["p0.pdf"] and files(q="two-sided") == ["p0.pdf"]    # highlight comment, note card
        r = ls.search(rows, q="governance")
        assert [x["file"][-6:] for x in r][0] == "p1.pdf" and any(s["kind"] == "summary" and "**" not in s["text"] for s in r[0]["snippets"]) and len(r) == 2
        assert sorted(files(q="platform governance")) == ["p0.pdf", "p1.pdf"]       # every word must appear somewhere (p0: topic + your highlight)
        assert files(q='"platform governance"') == ["p1.pdf"] and files(q="zzzz") == []     # a quoted phrase must appear as written
        assert files(q="tian") == ["p0.pdf"] and files(status="read") == ["p1.pdf"] and files(star=True) == ["p1.pdf"] and files(folder="f1") == ["p1.pdf"]
        assert files(status="none") == ["p0.pdf", "p2.pdf"] and files(notes=True) == ["p0.pdf"] and files(tag="t1") == ["p0.pdf"] and files(tag="nope") == []
        assert files(sort="title")[0] == "p2.pdf"      # "Chatbots…" first
        assert files(sort="opened")[0] == "p1.pdf"
        h = ls.highlights(rows)
        assert h["n"] == 1 and h["items"][0]["text"].startswith("transparent") and h["items"][0]["rects"] and ls.highlights(rows, color="red")["n"] == 0
        assert ls.highlights(rows, q="endogeneity")["n"] == 1 and ls.tag_usage(rows) == {"t1": 1}
    check("search finds titles, summaries AND your own highlights/notes; filters and sorts", t_search)

    importer.crossref_meta = lambda doi: {"title": "A Paper About Test Platforms", "authors": ["Ada Lovelace", "Alan van Turing"], "container": "Test Journal of IS", "issn": ["0000-0000"],
        "doi": doi, "pub_date": "2025-03", "url": "https://doi.org/" + doi, "volume": "12", "issue": "3", "pages": "100-120", "publisher": "P", "type": "journal-article", "abstract": None, "keywords": []}

    def t_import():
        import pymupdf
        d = pymupdf.open()
        for i in range(60):
            pg = d.new_page(); pg.insert_textbox(pymupdf.Rect(60, 60, 540, 780), ("doi: 10.1234/test.5678  " if i == 0 else "") + "Platforms and governance of ecosystems. " * 120, fontsize=10)
        data = d.tobytes(); d.close()
        r = importer.add_pdf(data, "download (3).pdf")
        assert r["file"].startswith("Papers/2025-03/Test Journal of IS/2025-03_TJI_10.1234-test.5678_") and os.path.exists(os.path.join(TMP, r["file"])) and not r["existing"], r
        import csv
        idx = list(csv.DictReader(open(os.path.join(PAPERS, "_index.csv"))))
        assert idx[-1]["status"] == "ok" and idx[-1]["doi"] == "10.1234/test.5678" and idx[-1]["file"] == r["file"] and "Ada Lovelace; Alan van Turing" == idx[-1]["authors"]
        assert importer.add_pdf(data, "again.pdf")["existing"] is True and len(list(csv.DictReader(open(os.path.join(PAPERS, "_index.csv"))))) == len(idx)    # no duplicate
        try:
            importer.add_pdf(b"not a pdf at all", "x.pdf"); raise AssertionError("expected ValueError")
        except ValueError:
            pass
        assert importer.clean_doi("https://doi.org/10.1287/isre.2024.0984.") == "10.1287/isre.2024.0984" and importer.clean_doi("see doi:10.1234/x") == "10.1234/x" and importer.clean_doi("nothing") == ""
    check("import a PDF: DOI sniffed, filed like the pipeline does, indexed, no duplicates", t_import)

    def t_exports():
        es = exports.entries(rows[:2]); assert [e["key"] for e in es] == ["tian2025open", "fernandez2025governance"], [e["key"] for e in es]
        assert exports.split_name("Alan van Turing") == ("van Turing", "Alan") and exports.split_name("Lovelace, Ada") == ("Lovelace", "Ada")
        bib = exports.bibtex(rows[:2]); assert "@article{tian2025open," in bib and "author = {Tian, Jing and Xue, Ling}" in bib and bib.count("@article") == 2
        r = exports.ris(rows[:1]); assert r.startswith("TY  - JOUR") and "AU  - Tian, Jing" in r and r.rstrip().endswith("ER  -")
        md = exports.notes_markdown(rows[:1]); assert "> x" in md and "check endogeneity" in md and "## References" in md and "Tian, J., & Xue, L." in md
        assert exports.notes_docx(rows[:1])[:2] == b"PK"
        out = exports.annotated_pdf(os.path.join(TMP, rows[0]["file"]), ns.load(os.path.join(TMP, rows[0]["file"])), [])
        import pymupdf
        with pymupdf.open(stream=out, filetype="pdf") as dd:
            an = [(a.type[1], a.info["content"]) for p in dd for a in p.annots()]
            assert an == [("Highlight", "check endogeneity")], an
    check("exports: BibTeX keys, RIS, notes as Markdown/Word, annotated PDF round-trip", t_exports)

    def t_retag():
        p0 = os.path.join(TMP, rows[0]["file"])
        n = ns.retag("t1", "t2"); assert n == 1 and ns.load(p0)["highlights"][0]["tags"] == ["t2"]
        n = ns.retag("t2"); assert ns.load(p0)["highlights"][0]["tags"] == [] and ns.load(p0)["rev"] >= 3
    check("library-wide tag merge/delete rewrites every paper's notes", t_retag)


    print("ai plumbing: images, pdf, web search, cache, privacy, budget, prompts")
    import prefs
    prefs.PROMPTS, prefs.SETTINGS = os.path.join(TMP, "prompts.json"), os.path.join(TMP, "settings.json")
    write_cfg({**CFG, "providers": {**CFG["providers"], "web": {"type": "openai_compat", "base_url": URL, "key_env": "FAKE_KEY", "web_body": {"plugins": [{"id": "web"}]}},
                                    "remote": {"type": "openai_compat", "base_url": "http://example.invalid/v1", "key_env": ""}},
               "features": {**CFG["features"], "explain": {"provider": "fake", "model": "m1"}}})

    def t_parts():
        import base64
        ai.chat("ask", [{"role": "user", "content": [{"type": "text", "text": "what is this?"}, {"type": "image", "media_type": "image/png", "data": base64.b64encode(b"\x89PNGxx").decode()},
                                                     {"type": "pdf", "path": pdf}]}])
        msg = SEEN[-1][1]["messages"][-1]["content"]
        assert msg[0] == {"type": "text", "text": "what is this?"} and msg[1]["type"] == "image_url" and msg[1]["image_url"]["url"].startswith("data:image/png;base64,")
        assert msg[2]["type"] == "text" and "Full text of the paper" in msg[2]["text"] and "120 firms" in msg[2]["text"]
        a = ai._to_anthropic([{"role": "user", "content": [{"type": "image", "media_type": "image/png", "data": "QQ=="}, {"type": "pdf", "path": pdf}, {"type": "text", "text": "hi"}]}])[0]["content"]
        assert a[0]["source"] == {"type": "base64", "media_type": "image/png", "data": "QQ=="} and a[1]["type"] == "document" and a[1]["source"]["media_type"] == "application/pdf" and a[2] == {"type": "text", "text": "hi"}
    check("image and PDF parts: native for Claude, image_url / extracted text for others", t_parts)

    def t_web():
        ai.chat("ask", [{"role": "user", "content": "news?"}], web_search=True, override={"provider": "web", "model": "m1"})
        assert SEEN[-1][1]["plugins"] == [{"id": "web"}]
        try:
            ai.chat("ask", [{"role": "user", "content": "news?"}], web_search=True); raise AssertionError("provider without web search must refuse")
        except ai.AIError as e:
            assert "can’t search the web" in str(e)
        assert ai.config()["providers"]["web"]["web_search"] and not ai.config()["providers"]["fake"]["web_search"]
    check("web search flag reaches the provider or is refused clearly", t_web)

    def t_cache():
        ai.cache_clear(); n0 = len(SEEN)
        r1 = ai.chat("explain", [{"role": "user", "content": "explain X"}], system="s", cache=True)
        r2 = ai.chat("explain", [{"role": "user", "content": "explain X"}], system="s", cache=True)
        assert len(SEEN) == n0 + 1 and not r1.cached and r2.cached and r2.text == r1.text and r2.cost == 0.0
        ai.chat("explain", [{"role": "user", "content": "explain X"}], system="s", cache=True, fresh=True); assert len(SEEN) == n0 + 2     # regenerate skips the cache
        ai.chat("explain", [{"role": "user", "content": "explain Y"}], system="s", cache=True); assert len(SEEN) == n0 + 3                   # different request, different entry
        assert ai.cache_stats()["entries"] == 2 and json.loads(open(ai.LEDGER).readlines()[-3])["cached"] is True
        assert ai.cache_clear() == 2 and ai.cache_stats()["entries"] == 0
    check("identical requests are answered from the cache at no cost; regenerate bypasses it", t_cache)

    def t_override_log():
        ai.chat("ask", [{"role": "user", "content": "compare me"}], override={"provider": "fake", "model": "m2"})
        assert SEEN[-1][1]["model"] == "m2"
        row = ai.usage_log(1)[0]
        assert row["model"] == "m2" and row["preview"] == "compare me" and row["chars"] >= 10
        s = ai.usage_summary()
        assert s["month"]["calls"] >= 1 and "fake" in s["by_provider"] and s["budget"] is None and not s["warn"]
    check("per-call model override; the usage log shows what was sent", t_override_log)

    def t_privacy_budget():
        c = json.loads(json.dumps(ai.config_data())); c["features"]["ask"] = {"provider": "remote", "model": "m"}; c["privacy"] = {"local_only": True}; write_cfg(c)
        try:
            ai.chat("ask", [{"role": "user", "content": "x"}]); raise AssertionError("local-only must block a remote provider")
        except ai.AIError as e:
            assert "Local-only" in str(e)
        c["features"]["ask"] = {"provider": "fake", "model": "m1"}; write_cfg(c)
        assert ai.chat("ask", [{"role": "user", "content": "x"}]).text      # 127.0.0.1 counts as on this computer
        ai.save_config({"privacy": {"local_only": False}, "budget": {"monthly_usd": 0.0000001}})
        try:
            ai.chat("ask", [{"role": "user", "content": "x"}]); raise AssertionError("budget must stop the call")
        except ai.AIError as e:
            assert "budget" in str(e)
        assert ai.usage_summary()["warn"] is True
        ai.save_config({"budget": {"monthly_usd": None}}); write_cfg(CFG)
    check("local-only mode blocks remote providers; the monthly budget stops calls", t_privacy_budget)

    def t_prompts():
        out = prefs.save_prompts({"prompts": [{"name": "Explain it", "prompt": "Explain {{text}} from {{title}}", "pinned": True}, {"id": "explain-it", "name": "Again", "prompt": "x", "scope": "area"}],
                                  "summaries": [{"id": "brief", "name": "Brief", "prompt": "Summarize {{title}} for {{interests}}"}]})
        assert [x["id"] for x in out["prompts"]] == ["explain-it", "explain-it-2"] and out["prompts"][1]["scope"] == "area"
        assert prefs.fill(out["prompts"][0]["prompt"], text="T", title="P") == "Explain T from P" and prefs.fill("a {{nope}} b") == "a  b"
        assert prefs.summary_prompt("brief", title="Q", interests="OSS") == "Summarize Q for OSS" and prefs.summary_prompt("missing", title="Q", interests="x").startswith("Summarize")
        for bad in ({"prompts": [{"name": "", "prompt": "x"}]}, {"summaries": []}):
            try:
                prefs.save_prompts(bad); raise AssertionError("expected ValueError")
            except ValueError:
                pass
        st = prefs.save_settings({"legend": {"yellow": " Claim ", "nonsense": "x"}, "summary_template": "brief", "wpm": 5000})
        assert st["legend"] == {"yellow": "Claim"} and st["summary_template"] == "brief" and st["wpm"] == 600
    check("prompt library and settings: validate, fill placeholders, templates", t_prompts)


    print("round 1b backend: references, chat, history, explain, summaries")
    import explain
    import pymupdf
    import refs

    def t_refs():
        d = pymupdf.open()
        p1 = d.new_page(width=595, height=842)
        p1.insert_textbox(pymupdf.Rect(60, 60, 535, 130), "Platform effects are large [1] and persistent (Lee, 2019).", fontsize=12)
        p2 = d.new_page(width=595, height=842)
        p2.insert_textbox(pymupdf.Rect(60, 60, 535, 90), "Table 1. Descriptive statistics of the sample of platform firms", fontsize=11)
        p2.insert_textbox(pymupdf.Rect(60, 600, 535, 640), "References", fontsize=13)
        p2.insert_textbox(pymupdf.Rect(60, 650, 535, 690), "[1] Smith, J. (2020). A study of platform things. Journal of Tests, 1(2), 3-4. https://doi.org/10.1234/abc", fontsize=10)
        p2.insert_textbox(pymupdf.Rect(60, 700, 535, 740), "[2] Lee, K. (2019). Another study of complementors. Journal of Tests, 5(1), 10-20.", fontsize=10)
        rp = os.path.join(TMP, "refs.pdf"); d.save(rp); d.close()
        bs2 = bl.extract(rp)
        assert [b["kind"] for b in bs2] == ["body", "body", "ref", "ref"], [b["kind"] for b in bs2]      # a caption before "References" must not swallow the heading
        r = refs.parse(rp)
        assert [(e["n"], e["surname"], e["year"], e["doi"], e["page"]) for e in r] == [(1, "Smith", "2020", "10.1234/abc", 2), (2, "Lee", "2019", "", 2)], r
        m = refs.match_library(r, [{"doi": "10.1234/ABC", "file": "Papers/x.pdf", "title": "A study of platform things"}])
        assert m[0]["in_library"] == "Papers/x.pdf" and m[1]["in_library"] is None
        apa = refs.parse(pdf)      # a paper without a reference list gives an empty list, not an error
        assert apa == [] or isinstance(apa, list)
    check("reference list: heading found, numbered entries split, DOI/year/surname parsed, matched to the library", t_refs)

    def t_split():
        text = ("Abbasi, A., Parsons, J., Pant, G. (2024). Pathways for design research. Information Systems Research, 35(2), 441-459. https://doi.org/10.1287/isre.2023.1234 "
                "Asatiani, A., Malo, P. (2021). Sociotechnical envelopment of AI. Journal of the AIS, 22(2), 325-352. Bender EM, Gebru T (2021) On the dangers of parrots. Proc. FAccT 610-623. "
                "van der Berg, M. (2020). Platform things. MIS Quarterly, 44(1), 1-20.")
        starts = [m.start() for m in refs.START.finditer(text)]
        parts = [text[a:b].strip() for a, b in zip([0] + starts, starts + [len(text)])]
        assert len(parts) == 4 and parts[1].startswith("Asatiani") and parts[2].startswith("Bender EM") and parts[3].startswith("van der Berg"), parts
        assert "Parsons, J." not in [p[:11] for p in parts]      # commas inside an author list never start a new entry
    check("reference splitting: APA, INFORMS style and particles; never inside an author list", t_split)

    def t_chat():
        ns.save_chat(pdf, [{"q": "q1", "text": "a1"}, "junk", {"q": "q2", "text": "a2"}])
        c = ns.load_chat(pdf); assert [t["q"] for t in c["turns"]] == ["q1", "q2"] and os.path.exists(ns.chat_path(pdf)) and ns.chat_path(pdf) in ns.sidecars(pdf)
        ns.save_chat(pdf, []); assert not os.path.exists(ns.chat_path(pdf)) and ns.load_chat(pdf)["turns"] == []
    check("saved conversation: stored next to the paper, removed when cleared", t_chat)

    def t_history():
        Fake.pieces = ["Because transparency raises entry (99) and (1)."]
        msgs = list(ask.stream(pdf, "and why?", "rag", history=[{"q": "How many firms are in the sample?", "a": "There are 120 firms (2)."}]))
        sent = SEEN[-1][1]["messages"]
        assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"] and sent[1]["content"].startswith("How many") and "120 firms" in sent[2]["content"]
        done = msgs[-1]; assert done["type"] == "done" and done["invalid"] == [99] and ask.invalid_citations("(1,2) and (0)", 2) == [0]
        msgs = list(ask.stream(pdf, "and why?", "rag", history=[{"q": "How many firms are in the sample?", "a": "x"}], override={"provider": "fake", "model": "m2"}, fresh=True))
        assert SEEN[-1][1]["model"] == "m2"
        Fake.pieces = ["The sample has ", "120 firms (2)."]
    check("follow-ups carry history; invalid citations are flagged; override and fresh pass through", t_history)

    def t_suggest():
        Fake.pieces = [json.dumps({"questions": ["What is the sample?", "What is the finding?", " ", "What next?"]})]
        qs = ask.suggest(pdf); assert qs == ["What is the sample?", "What is the finding?", "What next?"] and "response_format" in SEEN[-1][1]
        Fake.pieces = ["The sample has ", "120 firms (2)."]
    check("suggested questions come back as a clean list (schema requested)", t_suggest)

    def t_explain():
        prefs.save_prompts({"prompts": [{"id": "plain", "name": "Plain", "prompt": "Explain: {{text}} (from {{title}})"}, {"id": "fig", "name": "Fig", "prompt": "What does this show in {{title}}?", "scope": "area"}]})
        n0 = len(SEEN)
        out = list(explain.stream(pdf, {"title": "Platforms", "authors": "A; B", "year": "2026"}, text="Our sample covers 120 firms", page=1, prompt_id="plain"))
        sent = SEEN[-1][1]["messages"][-1]["content"]
        text = sent if isinstance(sent, str) else " ".join(x.get("text", "") for x in sent)
        assert "Explain: Our sample covers 120 firms (from Platforms)" in text and "paragraph of the paper this comes from" in text and out[-1]["type"] == "done" and not out[-1]["cached"]
        again = list(explain.stream(pdf, {"title": "Platforms"}, text="Our sample covers 120 firms", page=1, prompt_id="plain"))
        assert len(SEEN) == n0 + 1 and again[-1]["cached"] is True
        img = list(explain.stream(pdf, {"title": "Platforms"}, page=1, prompt_id="fig", image_b64="QUJD"))
        parts = SEEN[-1][1]["messages"][-1]["content"]; assert any(x["type"] == "image_url" for x in parts) and img[-1]["type"] == "done"
        bad = list(explain.stream(pdf, {}, text="x", page=1, prompt_id="nope")); assert bad[-1]["type"] == "error"
        cust = list(explain.stream(pdf, {"title": "P"}, text="X", page=1, custom="Rewrite as a haiku")); assert "Rewrite as a haiku" in str(SEEN[-1][1]["messages"][-1]["content"])
    check("explain: your prompt filled in with the passage and its paragraph, cached, images attached, custom prompts", t_explain)

    def t_summary():
        import pipeline as pl
        prefs.save_prompts({"summaries": [{"id": "brief", "name": "Brief", "prompt": "Summarize {{title}} for {{interests}} ({{journal}}, {{doi}})"}]})
        Fake.pieces = ["# Summary\n\nA fake summary."]
        usage = pl.Usage(); target = os.path.join(TMP, "sum.pdf"); shutil.copy(pdf, target)
        text = pl.summarize({"title": "Platforms", "journal": "MISQ", "doi": "10.1/x"}, target, usage)
        assert text.startswith("# Summary") and open(target[:-4] + ".md").read().startswith("# Summary")
        sent = SEEN[-1][1]["messages"][-1]["content"]; flat = " ".join(x.get("text", "") for x in sent)
        assert "Full text of the paper" in flat and "Summarize Platforms for" in flat and SEEN[-1][1]["max_completion_tokens" if "max_completion_tokens" in SEEN[-1][1] else "max_tokens"] == 16000
        assert usage.cost() > 0 and usage.d.get("extra", 0) > 0                      # a non-Claude model is added as dollars
        assert pl.summarize({"title": "Platforms"}, target, usage).strip() == text          # an existing summary is never overwritten
        Fake.pieces = ["The sample has ", "120 firms (2)."]
    check("pipeline summaries go through the AI layer: your template, any provider, cost added to the run", t_summary)


    print("round 2 backend: paper cards, folder evidence, Research Ideas Lab")
    CFG2 = json.loads(json.dumps(CFG)); CFG2["providers"]["websearch"] = {"type": "openai_compat", "base_url": URL, "key_env": "FAKE_KEY", "models": ["m1"], "web_body": {"plugins": [{"id": "web"}]}}
    CFG2["features"]["events"] = {"provider": "websearch", "model": "m1"}; write_cfg(CFG2)       # only "events" may search the web; every other feature falls back to Ask
    import pymupdf as _mu

    def make_paper(path, sentences):
        doc = _mu.open()
        for k in range(2):
            pg = doc.new_page(width=595, height=842)
            for i, t in enumerate(sentences[k * 3:k * 3 + 3]):
                pg.insert_textbox(_mu.Rect(60, 80 + i * 120, 535, 190 + i * 120), t, fontsize=11)
        doc.save(path); doc.close()

    d2 = os.path.join(PAPERS, "2026-05", "ISR"); os.makedirs(d2)
    SENT = {
        "a": ["We ask how platform governance shapes complementor entry in software ecosystems.", "Our sample covers 120 firms over six years of panel data from one platform.",
              "Transparency raises complementor entry by twelve percent in fixed effects models.", "Results may not generalize beyond the software platform industry sample.",
              "Future work should study governance changes in other platform markets such as health care.", "We contribute a theory of transparent platform governance."],
        "b": ["We study how app store review policies affect developer innovation behavior.", "We interview forty developers in a multiple case study across three stores.",
              "Stricter review reduces risky experiments but raises quality according to developers.", "Results may not generalize beyond the software platform industry sample studied.",
              "Future research should test the mechanism with quantitative field experiments.", "The contribution extends institutional theory to digital platforms."],
        "c": ["We examine how generative AI changes knowledge work in consulting firms.", "A survey of 300 consultants measures AI use and productivity.",
              "AI use increases productivity for novices but not for experts in our survey.", "Causal identification relies on an instrument that may be weak.",
              "Future work should follow the same consultants over time.", "We contribute evidence on skill-biased effects of generative AI."]}
    PAP = {}
    for k, sents in SENT.items():
        pth = os.path.join(d2, k + ".pdf"); make_paper(pth, sents)
        PAP[k] = {"pdf": pth, "row": {"file": "Papers/2026-05/ISR/%s.pdf" % k, "title": "Paper %s on platforms" % k.upper(), "authors": "Smith, Jane; Lee, Kim", "year": "2026", "journal": "ISR",
                                      "key": "10.1/%s" % k, "doi": "10.1/%s" % k}}

    def card_json(k, lim, fut, extra_cite=99):
        return json.dumps({"kind": "empirical (quantitative)", "question": {"text": "How does governance shape entry?", "cites": [1, extra_cite]}, "topics": ["platform governance", "software ecosystems"],
                           "theories": [{"name": "Transaction cost theory", "role": "uses", "note": "", "cites": [1]}], "constructs": [{"name": "Transparency", "definition": "How visible rules are", "cites": [2]}],
                           "hypotheses": [{"label": "H1", "text": "Transparency raises entry", "verdict": "supported", "cites": [3]}],
                           "method": {"design": "fixed effects panel regression", "data": "one platform", "sample": "120 firms", "analysis": "OLS", "cites": [2]},
                           "findings": [{"text": "Entry rises 12%.", "cites": [3]}], "contribution": {"text": "A theory.", "cites": [6]}, "implications": [],
                           "limitations": [{"text": lim, "stated": True, "cites": [4]}], "future_research": [{"text": fut, "cites": [5]}]})

    LIM = {"a": "Results may not generalize beyond the software platform industry sample", "b": "Results may not generalize beyond the software platform industry sample studied",
           "c": "Causal identification relies on an instrument that may be weak"}
    FUT = {"a": "Future work should study governance changes in other platform markets such as health care", "b": "Future research should test the mechanism with quantitative field experiments",
           "c": "Future work should follow the same consultants over time"}

    def t_card():
        Fake.responder = lambda body: [card_json("a", LIM["a"], FUT["a"])]
        n0 = len(SEEN)
        card, res = pc.generate(PAP["a"]["pdf"], PAP["a"]["row"])
        assert os.path.exists(ns.card_path(PAP["a"]["pdf"])) and ns.card_path(PAP["a"]["pdf"]) in ns.sidecars(PAP["a"]["pdf"]) and card["rev"] == 1 and "response_format" in SEEN[-1][1]
        d = card["data"]
        assert d["question"]["cites"] == [1] and card["invalid"] == 1                       # the invented passage 99 was dropped and counted
        assert d["limitations"][0]["cites"] == [4] and card["passages"]["4"]["page"] >= 1 and card["passages"]["4"]["quote"]
        assert set(card["passages"]) == {"1", "2", "3", "4", "5", "6"}                       # only the cited passages are kept
        sent = SEEN[-1][1]["messages"]; assert "[1] (p. 1)" in sent[-1]["content"] and "Smith, Jane" in sent[-1]["content"] and "References" not in sent[-1]["content"]
        out = list(pc.stream(PAP["a"]["pdf"], PAP["a"]["row"])); assert out[0]["type"] == "card" and out[0]["existing"] and len(SEEN) == n0 + 1      # a saved card costs nothing
        assert "fixed effects" in pc.text_of(card) and "supported" not in pc.text_of(card)
        out = list(pc.stream(PAP["a"]["pdf"], PAP["a"]["row"], fresh=True)); assert out[-1]["type"] == "done" and out[-2]["card"]["rev"] == 2 and len(os.listdir(pc.BACKUPS)) == 1
        assert pc.load(PAP["a"]["pdf"])["stale"] is False
        md = pc.markdown(pc.load(PAP["a"]["pdf"]), "T"); assert "## Limitations" in md and "(p. " in md
    check("paper card: JSON from the model, citations validated, passages stored, saved beside the PDF, regenerate keeps a backup", t_card)

    def t_card_bad():
        Fake.responder = lambda body: ['{"kind": "empirical (quant']
        out = list(pc.stream(PAP["b"]["pdf"], PAP["b"]["row"])); assert out[-1]["type"] == "error" and "valid JSON" in out[-1]["error"] and not pc.exists(PAP["b"]["pdf"])
        Fake.responder = lambda body: [card_json("b", LIM["b"], FUT["b"])]
        assert pc.generate(PAP["b"]["pdf"], PAP["b"]["row"])[0]["rev"] == 1           # the cut-off answer was not cached: asking again really asks again
        os.remove(ns.card_path(PAP["b"]["pdf"])); Fake.responder = None
    check("a cut-off answer is an error, not a half-written card, and is not cached", t_card_bad)

    def t_card_edit():
        c = pc.load(PAP["a"]["pdf"]); c["data"]["findings"][0]["text"] = "Entry rises by about 12 percent."; c["data"]["findings"][0]["edited"] = True
        c["data"]["limitations"].append({"text": "My own worry", "stated": False, "cites": [4, 77]})
        out = pc.save_edit(PAP["a"]["pdf"], c, c["rev"])
        assert out["rev"] == c["rev"] + 1 and out["edited"] and out["data"]["findings"][0]["edited"] is True and out["data"]["limitations"][1]["cites"] == [4]
        try:
            pc.save_edit(PAP["a"]["pdf"], c, c["rev"]); raise AssertionError("stale rev must conflict")
        except ns.Conflict as e:
            assert e.current["rev"] == out["rev"]
        try:
            pc.save_edit(PAP["a"]["pdf"], {"data": "x"}, 1); raise AssertionError("bad card")
        except ValueError:
            pass
    check("card edits: rev-guarded like notes, invented citations dropped, edited items marked", t_card_edit)

    def t_estimate():
        e = pc.estimate(PAP["a"]["pdf"]); assert e["cost"] > 0 and e["model"] == "m1" and e["chars"] > 500
        assert ai.estimate("ideas", 4000, 1000) > 0 and ai.feature_model("nothing") == ("fake", "m1")
    check("cost estimate before a paid step", t_estimate)

    def t_batch():
        Fake.responder = lambda body: [card_json("x", LIM["b"], FUT["b"])]
        rows = [PAP[k]["row"] for k in "abc"]
        msgs = list(pc.batch(rows))
        assert msgs[0] == {"type": "start", "total": 2, "skipped": 1}, msgs[0]              # paper a already has a card
        assert sorted(m["type"] for m in msgs[1:3]) == ["paper", "paper"] and msgs[-1]["type"] == "done" and msgs[-1]["made"] == 2, msgs
        assert all(pc.exists(PAP[k]["pdf"]) for k in "abc")
        Fake.responder = lambda body: ['{"kind": "other"']
        again = list(pc.batch(rows, fresh=True)); assert [m["type"] for m in again].count("error") == 3 and again[-1]["made"] == 0
        Fake.responder = None
    check("batch: skips papers that have a card, runs the rest, reports per-paper errors", t_batch)

    # the three cards say different things; give b and c their own limitation / future-research wording
    for k in "bc":
        Fake.responder = lambda body, k=k: [card_json(k, LIM[k], FUT[k])]
        pc.generate(PAP[k]["pdf"], PAP[k]["row"], fresh=True)
    Fake.responder = None
    ROWS = [{**PAP[k]["row"], "card": True} for k in "abc"] + [{"file": "Papers/2026-05/ISR/none.pdf", "title": "No card"}]

    def t_evidence():
        ev = evd.build(ROWS)
        assert [p["id"] for p in ev["papers"]] == ["P1", "P2", "P3"] and [m["title"] for m in ev["missing"]] == ["No card"]
        lim = ev["limitations"]; assert lim[0]["n"] == 2 and set(lim[0]["papers"]) == {"P1", "P2"} and lim[0]["id"] == "L1" and lim[0]["items"][0]["page"], lim     # the shared limitation comes first
        assert len(lim) == 3 and lim[1]["papers"] == ["P3"] and lim[2]["label"] == "My own worry" and lim[2]["items"][0]["stated"] is False, [(c["id"], c["papers"], c["label"]) for c in lim]
        assert ev["future"][0]["id"] == "F1" and len(ev["future"]) >= 2
        assert ev["topics"][0]["name"] == "platform governance" and ev["topics"][0]["n"] == 3 and ev["theories"][0]["extra"] == {"uses": 3}
        text = evd.digest(ev, [{"id": "E1", "date": "2026-03", "title": "EU rule", "summary": "x"}])
        assert "P1:" in text and "L1 [n=2, P1,P2]" in text and "E1 [2026-03]: EU rule" in text and "LIMITATIONS" in text
    check("folder evidence: limitations grouped by meaning (shared ones first), topics and theories counted, papers without a card listed", t_evidence)

    fid = fo.create("Platforms")["id"]
    fo.add(fid, [PAP[k]["row"]["key"] for k in "abc"])
    EV_URL, BAD_URL = "https://news.example.com/eu-platform-act/", "https://invented.example.org/x"

    def t_events():
        Fake.web_urls = [EV_URL]
        Fake.responder = lambda body: [json.dumps({"events": [
            {"title": "EU platform act adopted", "date": "2026-03-04", "summary": "New duties for gatekeepers.", "why_it_matters": "Governance rules change.", "url": "https://www.news.example.com/eu-platform-act"},
            {"title": "A made-up story", "date": "", "summary": "x", "why_it_matters": "y", "url": BAD_URL}, {"title": "", "url": "x"}]})]
        n0 = len(SEEN)
        out = list(idl.refresh_events(fid, ROWS[:3], "AI and platforms"))
        assert out[-1]["type"] == "done" and out[-2]["type"] == "events" and "plugins" in SEEN[n0][1]            # the provider's web-search switch was sent
        board = idl.load(fid); items = board["events"]["items"]
        assert [e["id"] for e in items] == ["E1", "E2"] and items[0]["verified"] is True and items[1]["verified"] is False and board["brief"]["focus"] == "AI and platforms"
        assert "AI and platforms" in str(SEEN[n0][1]["messages"]) and "Platform" not in "" and "platform governance" in str(SEEN[n0][1]["messages"])
        Fake.web_urls = None; Fake.responder = None
    check("current events: web search flag sent, topics + focus in the prompt, every URL checked against the sources the search returned", t_events)

    def t_events_retry():
        calls = []
        def responder(body):
            calls.append(body["messages"][-1]["content"])
            return ['{"events": []}'] if len(calls) == 1 else [json.dumps({"events": [{"title": "Second try event", "date": "2026-04", "summary": "x", "why_it_matters": "y", "url": "https://a.test/z"}]})]
        Fake.web_urls = ["https://a.test/z"]; Fake.responder = responder
        items, r = idl.fetch_events({"topics": [{"name": "platforms"}], "theories": [], "papers": []}, "")
        assert len(calls) == 2 and "use the web search tool now" in calls[1] and [e["title"] for e in items] == ["Second try event"] and items[0]["verified"] is True
        assert r.cost > 0 and r.cost == ai.cost_of("m1", {"in": 800, "out": 50, "cache_read": 200}) * 2         # both tries are paid for and counted
        calls.clear(); idl.fetch_events({"topics": [{"name": "platforms"}], "theories": [], "papers": []}, "")      # the empty first answer was not cached: it asks again
        assert len(calls) == 2      # the empty answer was not cached, so the first prompt goes out again
        Fake.web_urls = None; Fake.responder = None
    check("events: an empty first answer triggers one more try that insists on searching; costs add up; empty answers are not cached", t_events_retry)

    def t_cite_fallback():
        assert evd.short_cite({"authors": "", "title": "Will your AI turn backers away? Evidence", "year": "2026"}) == "“Will your AI turn…” (2026)"
        assert evd.short_cite({"authors": "Smith, Jane; Lee, Kim", "year": "2026"}) == "Smith et al. (2026)" and evd.short_cite({"authors": ["Jane Smith"], "year": ""}) == "Smith (n.d.)"
    check("a paper without authors is cited by the first words of its title", t_cite_fallback)

    def t_novelty():
        calls = []
        idl._openalex_search = lambda q, n=6: (calls.append(q) or ([] if q == "keywords" else [{"title": "A title", "year": 2025, "doi": "10.1/IN", "url": "u1", "cited_by": 1, "abstract": "Does X change Y?"},
                                                                                              {"title": "Other work", "year": 2020, "doi": "10.9/o", "url": "u2", "cited_by": 3, "abstract": "Tomatoes grow."}]))
        idea = {"title": "A title", "research_question": "Does X change Y?"}
        nv = idl.novelty(idea, "keywords", {"10.1/in"})
        assert calls == ["keywords", "A title"] and nv["works"][0]["in_folder"] is True and nv["level"] == "nothing very close", nv      # the folder's own paper does not count as existing work
        nv = idl.novelty(idea, "keywords", set()); assert nv["level"] in ("very close work exists", "related work exists"), nv
        idl._openalex_search = lambda q, n=6: (_ for _ in ()).throw(RuntimeError("offline mode"))
        assert idl.novelty(idea, "k") == {"checked": False, "error": "offline mode", "works": []}
    check("closest work: tries the editor's keywords then the title; the folder's own papers don't count; offline is reported, not hidden", t_novelty)

    def t_board_save():
        b = idl.load(fid); r0 = b["rev"]
        b["events"]["items"][1]["keep"] = False
        b["events"]["items"].append({"title": "My own: vendor lock-in lawsuit", "url": "https://x.test/y", "user": True})
        out = idl.save(fid, {"brief": {"focus": "Governance", "n": 99, "ambition": "wild"}, "events": b["events"]}, r0)
        assert out["rev"] == r0 + 1 and out["brief"]["n"] == 10 and out["brief"]["ambition"] == "balanced"
        its = out["events"]["items"]; assert [e["keep"] for e in its] == [True, False, True] and its[2]["user"] and its[2]["id"] == "E3"
        try:
            idl.save(fid, {"brief": {}}, r0); raise AssertionError("stale")
        except ns.Conflict:
            pass
        try:
            idl.save("nope", {}, 0); raise AssertionError("no folder")
        except KeyError:
            pass
    check("board saves: brief clamped, events ticked off or added by you, stale rev conflicts", t_board_save)

    IDEAS = {"ideas": [
        {"title": "Transparency under AI-driven governance", "research_question": "Does algorithmic rule enforcement change the entry effect of transparency?",
         "why_now": {"text": "The EU act forces algorithm disclosure.", "events": ["E1", "E2", "E9"]}, "gap": {"text": "Not addressed in these papers: AI-based enforcement.", "evidence": ["L1", "F1", "L99", "P2"]},
         "lens": "Transaction cost theory", "method": {"approach": "Panel DiD around the act", "identification": "Staggered adoption across platforms"}, "data": "App store metadata",
         "contribution": {"kind": "theory", "text": "Extends governance theory."}, "journals": ["ISR", "MISQ"], "risks": ["Data access"], "first_steps": ["Pull data", "Pilot", "Draft"], "closest_papers": ["P1", "P7"], "speculative": ["Enforcement is algorithmic"]},
        {"title": "Skill-biased AI in consulting", "research_question": "Do experts lose out when AI diffuses?", "why_now": {"text": "An invented story.", "events": ["E9"]},
         "gap": {"text": "Weak instrument.", "evidence": ["P3", "Z5"]}, "lens": "Skill-biased change", "method": {"approach": "Experiment", "identification": "Randomised access"}, "data": "Firm partners",
         "contribution": {"kind": "empirical", "text": "Causal evidence."}, "journals": [], "risks": [], "first_steps": [], "closest_papers": ["P3"], "speculative": []},
        {"title": "Pure speculation", "research_question": "What if?", "why_now": {"text": "", "events": []}, "gap": {"text": "None.", "evidence": ["L77"]}, "lens": "", "method": {"approach": "", "identification": ""},
         "data": "", "contribution": {"kind": "bogus", "text": ""}, "journals": [], "risks": [], "first_steps": [], "closest_papers": [], "speculative": []},
        {"title": "", "research_question": "dropped for having no title", "why_now": {}, "gap": {}, "lens": "", "method": {}, "data": "", "contribution": {}, "journals": [], "risks": [], "first_steps": [], "closest_papers": [], "speculative": []}]}
    CRIT = {"critiques": [{"index": 1, "novelty": 9, "feasibility": 3, "significance": 4, "fit": 4, "verdict": "Promising.", "main_risk": "Data.", "search_query": "algorithmic governance platform transparency"},
                          {"index": 2, "novelty": 2, "feasibility": 2, "significance": 3, "fit": 1, "verdict": "Thin.", "main_risk": "Access.", "search_query": ""}, {"index": 8, "novelty": 1}]}

    def ideas_responder(body):
        system = body["messages"][0]["content"]
        if system.startswith("You are a senior information-systems researcher and journal reviewer"):
            return [json.dumps(IDEAS)]
        if system.startswith("You are a tough but fair senior editor"):
            return [json.dumps(CRIT)]
        return ["## Working title and abstract\n\nA developed outline.\n\n## Research questions\n\n- RQ1"]

    def t_generate():
        asked = []
        idl._openalex_search = lambda q, n=6: (asked.append(q) or [{"title": "Algorithmic governance and platform transparency", "year": 2025, "doi": "10.9/near", "url": "https://doi.org/10.9/near", "cited_by": 7,
                                                                    "abstract": "Does algorithmic rule enforcement change the entry effect of transparency?"},
                                                                   {"title": "Unrelated gardening", "year": 2020, "doi": "", "url": "https://x/y", "cited_by": 0, "abstract": "Tomatoes."}])
        Fake.responder = ideas_responder
        n0 = len(SEEN)
        msgs = list(idl.generate(fid, ROWS, {"focus": "Governance", "constraints": "No interviews", "n": 3, "ambition": "bold"}))
        assert msgs[-1]["type"] == "done" and msgs[-1]["made"] == 3 and any(m["type"] == "ideas" for m in msgs), msgs[-1]
        prompt = SEEN[n0][1]["messages"][-1]["content"]
        assert "L1 [n=2" in prompt and "E1 [2026-03-04]: EU platform act adopted" in prompt and "made-up story" not in prompt and "vendor lock-in" in prompt    # the event you unticked is not offered
        assert "No interviews" in prompt and "Propose exactly 3 ideas" in prompt and "bold" in prompt and "Governance" in prompt
        doc = idl.load(fid); ids = doc["ideas"]; assert len(ids) == 3 and os.path.exists(idl.path(fid)) and [f["id"] for f in fo.list_all()] == [fid]
        a, b, c = ids
        assert a["grounding"] == "grounded" and a["gap"]["evidence"] == ["L1", "F1", "P2"] and a["why_now"]["events"] == ["E1", "E3"][:1] + [] or a["why_now"]["events"] == ["E1"]
        assert a["why_now"]["events"] == ["E1"] and a["closest_papers"] == ["P1"]                  # E2 is unticked, E9 and P7 and L99 never existed
        assert b["grounding"] == "partly" and b["why_now"]["text"] == "" and b["gap"]["evidence"] == ["P3"]       # no real event -> no "why now" story
        assert c["grounding"] == "speculative" and c["contribution"]["kind"] == "empirical"
        assert a["critique"]["novelty"] == 5 and a["critique"]["score"] == 4.0 and b["critique"]["fit"] == 1 and c["critique"] is None       # scores clamped to 1-5
        assert "algorithmic governance platform transparency" in asked and any("Skill-biased" in q for q in asked)       # the editor's keywords, and the idea's own title as a second try
        assert a["novelty"]["works"][0]["doi"] == "10.9/near" and a["novelty"]["works"][0]["sim"] > a["novelty"]["works"][1]["sim"] and a["novelty"]["level"] != "nothing very close"
        run = doc["runs"][a["run"]]; ev = run["evidence"]
        assert ev["papers"]["P1"]["cite"] == "Smith et al. (2026)" and ev["limitations"]["L1"]["n"] == 2 and ev["events"]["E1"]["url"] == EV_URL.rstrip("/") + "" or ev["events"]["E1"]["url"].startswith("https://www.news")
        assert run["cost"] > 0 and run["n_papers"] == 3 and doc["brief"]["n"] == 3
        cost_first = doc["runs"][a["run"]]["cost"]
        # "add" keeps earlier ideas, "replace" drops the ones you did not shortlist or star
        board = idl.load(fid); board["ideas"][0]["star"] = True; board["ideas"][1]["status"] = "shortlist"; idl.save(fid, board, board["rev"])
        list(idl.generate(fid, ROWS, {"n": 2}, "replace"))
        d2 = idl.load(fid); kept = [x for x in d2["ideas"] if x["run"] == a["run"]]
        assert len(kept) == 2 and {x["title"] for x in kept} == {a["title"], b["title"]} and len(d2["ideas"]) > 2, ([(x["run"], x["title"], x.get("star"), x.get("status")) for x in d2["ideas"]], a["run"])
        Fake.responder = None
    check("ideas: prompt carries the evidence ids, your constraints and only the events you kept; ids validated; grounding honest; scores clamped; closest work ranked", t_generate)

    def t_generate_guard():
        ok = [m for m in idl.generate(fid, ROWS[:1], {"n": 2})]
        assert ok[-1]["type"] == "error" and "at least two" in ok[-1]["error"]
        Fake.responder = lambda body: [json.dumps({"ideas": []})]
        bad = list(idl.generate(fid, ROWS, {"n": 4}, "add")); assert bad[-1]["type"] == "error" and "any ideas" in bad[-1]["error"], bad[-1]
        Fake.responder = None
    check("the lab refuses to run without two cards, and an empty answer is an error", t_generate_guard)

    def t_develop():
        Fake.responder = ideas_responder
        doc = idl.load(fid); target = doc["ideas"][0]
        out = list(idl.develop(fid, target["id"]))
        assert out[-1]["type"] == "done" and "".join(m.get("delta", "") for m in out).startswith("## Working title")
        sent = str(SEEN[-1][1]["messages"][-1]["content"]); assert target["research_question"] in sent and "Smith et al. (2026)" in sent and "Panel DiD" in sent
        assert idl.load(fid)["ideas"][0]["developed"]["text"].startswith("## Working title")
        assert list(idl.develop(fid, "gone"))[-1]["type"] == "error"
        Fake.responder = None
    check("develop an idea: outline streamed, built from the idea and its cited evidence, saved on the idea", t_develop)

    def t_ideas_export():
        doc = idl.load(fid)
        for x in doc["ideas"]:
            if x["title"].startswith("Skill-biased"):
                x["status"] = "rejected"
        idl.save(fid, doc, doc["rev"])
        md = idl.markdown(fid)
        assert "# Research ideas — Platforms" in md and "Transparency under AI-driven governance ★" in md and "**Why now.**" in md and "L1 — Results may not generalize" in md
        assert "Smith et al. (2026), p." in md and "Closest existing work" in md and "Developed outline" in md and "Skill-biased" not in md, md[:3000]       # rejected ideas stay out
        assert "Grounding: grounded" in md and idl.docx(fid)[:2] == b"PK"
    check("export the board as Markdown and Word: evidence with pages, closest work, developed outline; rejected ideas left out", t_ideas_export)

    def t_folder_cleanup():
        fo.delete(fid); assert not os.path.exists(idl.path(fid)) and fo.list_all() == []
    check("deleting a folder deletes its board", t_folder_cleanup)


    print("round 2b backend: ask a folder, comparison matrix")
    pd_ = os.path.join(d2, "d.pdf"); make_paper(pd_, ["We study how open source vendors govern contributors in a large survey.", "A survey of 90 vendors measures contributor governance.",
                                                       "Governance reduces free riding in vendor communities.", "Our survey covers one country only.", "Future work should add other countries.", "We contribute a governance typology."])
    D_ROW = {"file": "Papers/2026-05/ISR/d.pdf", "title": "Paper D on vendors", "authors": "Garcia, Ana", "year": "2026", "journal": "ISR", "key": "10.1/d", "doi": "10.1/d"}
    ROWS4 = [{**PAP[k]["row"], "card": True} for k in "abc"] + [D_ROW]            # D has a PDF but no card
    fid2 = fo.create("Governance")["id"]; fo.add(fid2, [r["key"] for r in ROWS4])

    def t_scope():
        info = ma.scope_info(ROWS4); assert info["papers"] == 4 and info["cards"] == 3 and info["unindexed"] == 4 and info["seconds"] > 0, info
        assert ma.estimate(ROWS4) > 0 and ma.estimate(ROWS4, "cards") > 0
        assert ma.papers_of([{"file": "Papers/nope.pdf"}, {"file": "../x.pdf"}]) == []
    check("scope: papers, cards, what still needs reading (indexing), cost estimate; paths stay jailed", t_scope)

    def t_multi_passages():
        Fake.responder = lambda body: ["Smith et al. (2026) cover 120 firms (1), and others agree (2,99)."]
        msgs = list(ma.stream(ROWS4, "How large is the sample of firms?", "passages"))
        kinds = [m["type"] for m in msgs]
        assert any(m["type"] == "status" and "for the first time" in m["msg"] for m in msgs) and kinds[-1] == "done" and "scope" in kinds and "passages" in kinds, msgs[:3]
        ps = next(m for m in msgs if m["type"] == "passages")["passages"]
        files = {p["file"] for p in ps}; assert len(files) >= 3 and all(p["quote"] and p["cite"] and p["page"] >= 1 for p in ps) and [p["n"] for p in ps] == list(range(1, len(ps) + 1))
        sent = SEEN[-1][1]["messages"][-1]["content"]
        assert "Passages from" in sent and "(Smith et al. (2026), p. " in sent and "Question: How large" in sent and SEEN[-1][1]["messages"][0]["content"].startswith("You are a careful research assistant answering a question from numbered passages")
        assert msgs[-1]["invalid"] == [99] or 99 in msgs[-1]["invalid"]
        chats = lambda: sum(1 for x in SEEN if x[0].endswith("/chat/completions"))  # noqa: E731
        n0 = chats(); again = list(ma.stream(ROWS4, "How large is the sample of firms?", "passages"))
        assert not any(m["type"] == "status" and "first time" in m["msg"] for m in again) and again[-1]["cached"] is True and chats() == n0     # indexed once, answer cached
        h = list(ma.stream(ROWS4, "and why?", "passages", history=[{"q": "How large is the sample of firms?", "a": "120 firms (1)."}], fresh=True))
        assert [m["role"] for m in SEEN[-1][1]["messages"]] == ["system", "user", "assistant", "user"] and h[-1]["type"] == "done"
        Fake.responder = None
    check("ask several papers: passages from different papers, labelled with paper and page; indexed once; follow-ups carry history; invented citations flagged", t_multi_passages)

    def t_multi_year():
        Fake.responder = lambda body: ["Smith et al. (2026) report 120 firms (1); Lee (2019) agrees (2)."]
        done = list(ma.stream(ROWS4, "Compare the samples of firms.", "passages", fresh=True))[-1]
        assert done["type"] == "done" and done["invalid"] == [], done              # "(2026)" after a name is a year, not a citation
        Fake.responder = None
    check("a year in parentheses after an author's name is not reported as an invented citation", t_multi_year)

    def t_multi_cards():
        Fake.responder = lambda body: ["Papers differ (1) (3)."]
        msgs = list(ma.stream(ROWS4, "Which methods do they use?", "cards"))
        sc = next(m for m in msgs if m["type"] == "scope"); ps = next(m for m in msgs if m["type"] == "passages")["passages"]
        assert sc["papers"] == 4 and sc["used"] == 3 and [m["title"] for m in sc["missing_cards"]] == ["Paper D on vendors"], sc
        assert {p["section"] for p in ps} >= {"question", "method", "findings", "limitations"} and all(p["page"] >= 1 for p in ps) and any(p["quote"] for p in ps)
        sent = SEEN[-1][1]["messages"][-1]["content"]; assert "findings, p. " in sent and "fixed effects panel" in sent and "cards" in SEEN[-1][1]["messages"][0]["content"]
        none = list(ma.stream([D_ROW], "x?", "cards")); assert none[-1]["type"] == "error" and "card" in none[-1]["error"]
        assert list(ma.stream([], "x?", "passages"))[-1]["type"] == "error"
        Fake.responder = None
    check("ask the cards: every paper with a card is covered, lines carry their section and page, papers without a card are reported", t_multi_cards)

    def t_multi_chat():
        ma.save_chat(fid2, [{"q": "q1", "text": "a1"}, "junk"]); assert ma.load_chat(fid2)["turns"] == [{"q": "q1", "text": "a1"}] and os.path.exists(os.path.join(fo.DIR, fid2 + ".ask.json"))
        assert [f["id"] for f in fo.list_all()] == [fid2]                       # side files are not folders
        ma.save_chat(None, [{"q": "lib", "text": "x"}]); assert ma.load_chat(None)["turns"][0]["q"] == "lib" and os.path.exists(os.path.join(PAPERS, "_ask_library.json"))
        ma.save_chat(None, []); assert not os.path.exists(os.path.join(PAPERS, "_ask_library.json"))
        try:
            ma.save_chat("nope", [{"q": "x"}]); raise AssertionError("unknown folder")
        except KeyError:
            pass
    check("conversations: saved beside the folder (or in Papers/_ask_library.json), cleared when emptied", t_multi_chat)

    def t_matrix_table():
        t = mx.table(fid2, ROWS4)
        assert [c["id"] for c in t["columns"]] == mx.DEFAULT_COLUMNS and all(c["builtin"] for c in t["columns"]) and t["missing_cards"] == 1
        r = t["rows"][0]; assert r["card"] and r["cells"]["design"]["text"].startswith("fixed effects panel") and r["cells"]["sample"]["text"] == "120 firms"
        assert r["cells"]["findings"]["cites"] and r["cells"]["findings"]["cites"][0]["page"] >= 1 and "quote" in r["cells"]["findings"]["cites"][0]
        assert r["cells"]["limitations"]["text"].startswith("Results may not generalize") and t["rows"][3]["card"] is False and t["rows"][3]["cells"] == {}
        assert mx.card_cell(pc.load(PAP["a"]["pdf"]), "hypotheses")["text"].startswith("H1 Transparency raises entry — supported")
    check("matrix: built-in columns are read from the cards, each cell with its pages; papers without a card show empty", t_matrix_table)

    def t_matrix_columns():
        doc = mx.load(fid2); r0 = doc["rev"]
        doc = mx.add_custom(fid2, "Moderators tested", "Which moderators or boundary conditions did the paper test?", r0)
        cid = doc["custom"][0]["id"]; assert cid in doc["columns"] and doc["rev"] == r0 + 1
        try:
            mx.add_custom(fid2, "x", "y", r0); raise AssertionError("stale rev")
        except ns.Conflict:
            pass
        doc = mx.save(fid2, {"columns": ["findings", "nonsense", cid, "findings", "design"]}, doc["rev"])
        assert doc["columns"] == ["findings", cid, "design"]                         # unknown ids dropped, duplicates removed, order kept
        t = mx.table(fid2, ROWS4); assert [c["id"] for c in t["columns"]] == ["findings", cid, "design"] and not t["columns"][1]["builtin"] and t["columns"][1]["prompt"].startswith("Which moderators")
        try:
            mx.add_custom(fid2, "  ", "", doc["rev"]); raise AssertionError("name required")
        except ValueError:
            pass
    check("matrix columns: add your own question, choose and order columns, stale rev conflicts", t_matrix_columns)

    def t_matrix_fill():
        doc = mx.load(fid2); cid = doc["custom"][0]["id"]
        est = mx.estimate_fill(fid2, ROWS4); assert est["cells"] == 4 and est["papers"] == 4 and est["cost"] > 0, est
        prompts = []

        def responder(body):
            system = body["messages"][0]["content"]; user = body["messages"][-1]["content"]; prompts.append(user)
            assert system.startswith("You fill cells of a literature comparison table")
            ids = re.findall(r"- id (\S+) — ", user)
            if "Paper B on platforms" in user:
                return [json.dumps({"cells": [{"column": i, "answer": "Not reported", "cites": [1]} for i in ids]})]
            return [json.dumps({"cells": [{"column": i, "answer": "Tenure moderates the effect (1,2); firm size too (99).", "cites": [1, 2, 99]} for i in ids]})]
        Fake.responder = responder; n0 = len(SEEN)
        msgs = list(mx.fill(fid2, ROWS4))
        quote = pc.load(PAP["a"]["pdf"])["passages"][str(pc.load(PAP["a"]["pdf"])["data"]["method"]["cites"][0])]["quote"]
        assert any(quote in u for u in prompts if "Paper A on platforms" in u), "the paragraph the card cites for its method must be among the passages"
        assert msgs[0] == {"type": "start", "total": 4, "cells": 4} and msgs[-1]["type"] == "done" and msgs[-1]["made"] == 4 and len(SEEN) - n0 >= 4 + 4 * 0, msgs[-1]
        t = mx.table(fid2, ROWS4); cells = {r["file"]: r["cells"].get(cid) for r in t["rows"]}
        a = cells[PAP["a"]["row"]["file"]]; b = cells[PAP["b"]["row"]["file"]]
        assert a["text"].startswith("Tenure moderates") and a["status"] == "ok" and a["cites"] and all(c["page"] >= 1 and c["quote"] for c in a["cites"]) and len(a["cites"]) <= 3
        assert b["text"] == "Not reported" and b["status"] == "not reported" and b["cites"] == []                    # "not reported" is an answer, with no source
        assert mx.estimate_fill(fid2, ROWS4)["cells"] == 0 and list(mx.fill(fid2, ROWS4))[0]["total"] == 0                # nothing left to fill: no calls
        # your own edit survives a refresh; clearing a cell has it filled again
        doc = mx.load(fid2); key = PAP["a"]["row"]["key"]
        doc = mx.save(fid2, {"edits": {key: {cid: "Tenure only (my reading)", "question": "ignored: not a custom column"}}}, doc["rev"])
        assert doc["cells"][key][cid]["edited"] is True and "question" not in doc["cells"][key]
        list(mx.fill(fid2, ROWS4, fresh=True)); assert mx.load(fid2)["cells"][key][cid]["text"] == "Tenure only (my reading)"
        doc = mx.save(fid2, {"edits": {key: {cid: ""}}}, mx.load(fid2)["rev"]); assert cid not in doc["cells"][key]
        assert mx.estimate_fill(fid2, ROWS4)["cells"] == 1
        # a changed question invalidates old answers; deleting the column removes them
        doc = mx.save(fid2, {"custom": [{"id": cid, "name": "Moderators tested", "prompt": "A different question?"}]}, mx.load(fid2)["rev"]); assert all(cid not in v for v in doc["cells"].values())
        Fake.responder = None
    check("matrix fill: one call per paper, answers with page + quote, 'Not reported' kept honest, your edits survive, cleared cells refill", t_matrix_fill)

    def t_matrix_export():
        import exports as ex_; ex_._crossref = lambda rows: {}
        doc = mx.load(fid2); key = PAP["a"]["row"]["key"]; cid = doc["custom"][0]["id"]
        mx.save(fid2, {"edits": {key: {cid: "Costs & 50% savings_a"}}}, doc["rev"])
        csv_b = mx.csv_bytes(fid2, ROWS4); assert csv_b.startswith(b"\xef\xbb\xbfPaper,Title,Year,Journal,DOI,") and b"(p. " in csv_b and b"Costs & 50%" in csv_b
        md = mx.markdown(fid2, ROWS4); assert md.startswith("# Governance — comparison") and "| Paper |" in md and md.count("\n|") >= 6
        tex = mx.latex(fid2, ROWS4); assert "\\begin{tabularx}" in tex and "\\toprule" in tex and "\\citet{smith2026" in tex and "Costs \\& 50\\% savings\\_a" in tex and "Paper D" not in tex, tex[:900]
        from docx import Document as Doc
        d = Doc(io.BytesIO(mx.docx_bytes(fid2, ROWS4))); assert len(d.tables[0].rows) == 5 and d.sections[0].orientation == 1
    check("matrix export: CSV (Excel-safe), Markdown, Word (landscape table), LaTeX with \\citet keys and escaped special characters", t_matrix_export)

    def t_authorcache():
        ac.CACHE = os.path.join(TMP, "authors_cache.json"); ac._mem.clear()
        rows = [{"doi": "10.1/z", "authors": ""}, {"doi": "10.1/y", "authors": "A B"}, {"doi": "", "authors": ""}, {"doi": "10.1/none", "authors": " "}]
        calls = []
        def lookup(doi):
            calls.append(doi)
            return {"10.1/z": {"authors": ["Ann Lee", "Bo Tan"]}, "10.1/none": {"authors": []}}.get(doi)
        assert ac.backfill(rows, lookup) == 1 and sorted(calls) == ["10.1/none", "10.1/z"]
        ac.apply(rows); assert rows[0]["authors"] == "Ann Lee; Bo Tan" and rows[1]["authors"] == "A B" and rows[2]["authors"] == "" and rows[3]["authors"] == " "
        calls.clear(); assert ac.backfill(rows, lookup) == 0 and calls == []                 # known answers (and recent empty ones) are not asked again
        def offline(doi):
            raise OSError("no network")
        assert ac.backfill([{"doi": "10.1/new", "authors": ""}], offline) == 0 and "10.1/new" not in ac.cached()       # a failed lookup is not remembered
    check("authors the feeds left out are looked up once (Crossref), cached, and filled into library rows; the index file is not touched", t_authorcache)

    print("round 2c backend: related and missing papers")
    rl.CACHE, rl.IGNORED = os.path.join(TMP, "openalex_cache.json"), os.path.join(PAPERS, "_related.json"); rl._mem.clear()
    # a paper whose own PDF lists references with DOIs (OpenAlex knows nothing about it yet)
    de = _mu.open(); pg = de.new_page(width=595, height=842)
    pg.insert_textbox(_mu.Rect(60, 60, 535, 200), "We study platform governance across ecosystems and build on earlier work about transparency and complementors in depth.", fontsize=12)
    pg2 = de.new_page(width=595, height=842)
    pg2.insert_textbox(_mu.Rect(60, 60, 535, 90), "References", fontsize=13)
    pg2.insert_textbox(_mu.Rect(60, 100, 535, 140), "[1] Quinn, Z. (2020). The platform classic. Journal of Tests, 1(2), 3-4. https://doi.org/10.1234/ten", fontsize=10)
    pg2.insert_textbox(_mu.Rect(60, 150, 535, 190), "[2] Wu, B. (2021). Complementor entry revisited. Journal of Tests, 2(1), 5-9. https://doi.org/10.9/ref2", fontsize=10)
    e_pdf = os.path.join(d2, "e.pdf"); de.save(e_pdf); de.close()
    E_ROW = {"file": "Papers/2026-05/ISR/e.pdf", "title": "Paper E on platform governance", "authors": "Ito, Mei", "year": "2026", "journal": "ISR", "key": "10.1/e", "doi": "10.1/e"}
    W = lambda n: "https://openalex.org/W%d" % n  # noqa: E731

    def work(n, doi="", title=None, year=2020, cited=10, refs=(), related=(), venue="Journal of Tests", authors=("Ann Lee",)):
        return {"id": W(n), "doi": ("https://doi.org/" + doi) if doi else None, "title": title or "Work %d on platform things" % n, "publication_year": year, "publication_date": "%d-03-01" % year,
                "cited_by_count": cited, "primary_location": {"source": {"display_name": venue}}, "authorships": [{"author": {"display_name": a}} for a in authors],
                "referenced_works": [W(r) for r in refs], "related_works": [W(r) for r in related]}
    OA = {w["id"]: w for w in (
        work(1, "10.1/a", "Paper A on platforms", refs=(10, 11, 3), related=(30, 5)), work(2, "10.1/b", "Paper B on platforms", refs=(10, 12), related=(30, 31)),
        work(3, "10.1/c", "Paper C on platforms", refs=(10, 11, 13), related=(30,)), work(4, "10.1/d", "Paper D on vendors", refs=()),                          # D: no references known
        work(10, "10.1234/ten", "The platform classic", cited=900, authors=("Zed Quinn", "Amy Roe")), work(11, "10.9/eleven", "Governance of platform ecosystems", cited=300),
        work(12, "10.9/twelve", "Only B cites this platform paper", cited=50), work(13, "", "Only C cites this one", cited=5), work(5, "10.9/five", "Related and cited by nobody", cited=1),
        work(14, "10.9/ref2", "Complementor entry revisited", cited=40), work(30, "10.9/thirty", "Platform governance and AI", cited=120, year=2025),
        work(31, "10.9/thirtyone", "Marketplace design", cited=20))}
    CALLS = []

    def fake_oa(path):
        CALLS.append(path)
        q = urllib.parse.parse_qs(urllib.parse.urlsplit("http://x/" + path).query); kind, vals = q["filter"][0].split(":", 1)
        vals = [urllib.parse.unquote(v) for v in vals.split("|")]
        hits = [w for w in OA.values() if ((w["doi"] or "").replace("https://doi.org/", "") in vals if kind == "doi" else short_id(w["id"]) in vals)]
        if "referenced_works" not in q["select"][0]:
            hits = [{k: v for k, v in w.items() if k not in ("referenced_works", "related_works")} for w in hits]
        return {"results": hits}
    short_id = rl.short
    rl._fetch = fake_oa
    LIBRARY = [{**r, "card": False} for r in ROWS4[:3]] + [D_ROW, E_ROW]

    def t_oa_cache():
        got = rl.lookup("doi", ["10.1/A", "10.1/b", "10.1/nope", "bad,doi"], full=True)
        assert sorted(got) == ["10.1/a", "10.1/b"] and got["10.1/a"]["refs"] == ["W10", "W11", "W3"] and got["10.1/a"]["authors"] == ["Ann Lee"] and got["10.1/a"]["venue"] == "Journal of Tests"
        n = len(CALLS); assert rl.lookup("doi", ["10.1/a", "10.1/nope"], full=True).keys() == {"10.1/a"} and len(CALLS) == n          # answers (and "not found") are cached
        light = rl.lookup("id", ["W10", "https://openalex.org/W11"]); assert sorted(light) == ["W10", "W11"] and light["W10"]["refs"] == [] and "referenced_works" not in CALLS[-1]
        rl._fetch = lambda path: None
        assert rl.lookup("id", ["W12"]) == {} and "l:id:W12" not in rl._cache()              # unreachable: nothing remembered, so the next try asks again
        rl._fetch = fake_oa
        n = len(CALLS); many = rl.lookup("doi", ["10.%d/x" % i for i in range(95)]); assert many == {} and len(CALLS) - n == 3      # 95 DOIs = 3 requests of at most 40
        assert rl.lookup("doi", ["10.1/c"], full=False)["10.1/c"]["refs"] == []                # the light and full variants are cached separately
    check("OpenAlex lookups: batched, cached (also 'not found'), light vs full, unreachable is not remembered", t_oa_cache)

    def t_ignore():
        assert rl.ignore([{"id": "W11", "title": "Governance"}, {"id": "bogus"}]) == {"W11": {"title": "Governance", "ts": rl.ignored()["W11"]["ts"]}} and os.path.exists(rl.IGNORED)
        assert rl.unignore(["W11"]) == {} and not os.path.exists(rl.IGNORED)
    check("not interested: remembered in Papers/_related.json, bogus ids refused, undo removes the file when empty", t_ignore)

    def t_scope():
        rl.REL_SIM = -1.0                      # (the bag-of-words test embeddings are far cruder than the real model: the threshold has its own test below)
        msgs = list(rl.scope_stream(LIBRARY, LIBRARY, min_n=2))
        res = msgs[-1]; assert res["type"] == "result", msgs[-1]
        ms = res["missing"]; assert [(m["id"], m["n"]) for m in ms] == [("W10", 4), ("W11", 2)], ms           # W3 is in the library; W12 and W13 are cited once
        assert {c["file"] for c in ms[0]["via"]} == {"Papers/2026-05/ISR/%s.pdf" % k for k in "abce"} and {c["file"] for c in ms[1]["via"]} == {"Papers/2026-05/ISR/%s.pdf" % k for k in "ac"}
        assert ms[0]["doi"] == "10.1234/ten" and ms[0]["authors"] == ["Zed Quinn", "Amy Roe"] and ms[0]["cited_by"] == 900 and ms[0]["year"] == 2020
        assert res["stats"]["via_pdf"] == 1 and res["stats"]["papers"] == 5 and res["stats"]["no_refs"] == ["Paper D on vendors"], res["stats"]    # E's bibliography DOIs stood in for OpenAlex
        assert [(r["id"], r["n"]) for r in res["related"]] == [("W30", 3), ("W31", 1), ("W5", 1)], res["related"]
        one = list(rl.scope_stream(LIBRARY, LIBRARY, min_n=1))[-1]["missing"]; assert {"W12", "W13"} <= {m["id"] for m in one} and len(one) > 2
        rl.ignore([{"id": "W11"}]); hidden = list(rl.scope_stream(LIBRARY, LIBRARY, min_n=2))[-1]["missing"]; assert [m["id"] for m in hidden] == ["W10"]; rl.unignore(["W11"])
        sub = list(rl.scope_stream(LIBRARY[:1], LIBRARY, min_n=1))[-1]; assert {m["id"] for m in sub["missing"]} == {"W10", "W11"} and sub["missing"][0]["n"] == 1       # only A's own references
        assert list(rl.scope_stream([], LIBRARY))[-1]["type"] == "error"
    check("missing papers: cited by several of your papers, not in the library; bibliography DOIs fill in for papers OpenAlex has no references for; thresholds and 'not interested' respected", t_scope)

    def t_relsim():
        rl.REL_SIM = 0.74
        res = list(rl.scope_stream(LIBRARY, LIBRARY, min_n=2))[-1]
        assert res["related"] == [] and [m["id"] for m in res["missing"]] == ["W10", "W11"]          # OpenAlex's "related" titles are far from these papers: dropped; what the papers cite is never filtered
        rl.REL_SIM = -1.0; res = list(rl.scope_stream(LIBRARY, LIBRARY, min_n=2))[-1]
        assert res["related"] and all(isinstance(c["sim"], float) for c in res["related"])
    check("OpenAlex 'related' papers are kept only when their title is close to the paper that points at them", t_relsim)

    def t_paper():
        rl.REL_SIM = -1.0
        r = rl.paper(LIBRARY[0], LIBRARY)
        assert [c["id"] for c in r["cited"]] == ["W10", "W11"] and r["cited"][0]["cited_by"] == 900                  # most cited first; W3 is yours
        assert [c["id"] for c in r["related"]] == ["W30", "W5"] and r["stats"] == {"openalex": True, "refs": 3, "via_pdf": False, "related": 2}
        assert r["similar"] and all(x["file"] != LIBRARY[0]["file"] and -1 <= x["sim"] <= 1 for x in r["similar"]) and [x["sim"] for x in r["similar"]] == sorted((x["sim"] for x in r["similar"]), reverse=True)
        rl._fetch = lambda path: None; rl._mem.clear(); os.rename(rl.CACHE, rl.CACHE + ".bak")                   # no network and nothing cached
        off = rl.paper(LIBRARY[0], [LIBRARY[0], LIBRARY[1]]); assert off["cited"] == [] and off["related"] == [] and off["stats"]["openalex"] is False and off["similar"]     # offline: the local list still works
        os.rename(rl.CACHE + ".bak", rl.CACHE) if os.path.exists(rl.CACHE + ".bak") and not os.path.exists(rl.CACHE) else None; rl._mem.clear(); rl._fetch = fake_oa
    check("one paper: similar papers you have (local), what it cites and what is related that you don't have; works offline for the local part", t_paper)

    def t_run_papers():
        import importer
        orig_meta, orig_ex = importer.crossref_meta, importer._existing
        importer.crossref_meta = lambda doi: {"title": "The platform classic", "authors": ["Zed Quinn"], "container": "Information Systems Research", "issn": ["1047-7047"], "doi": doi, "pub_date": "2020-03-01", "url": "https://doi.org/" + doi,
                                              "abstract": "x", "volume": "", "issue": "", "pages": "", "publisher": "", "type": "journal-article", "keywords": []} if doi == "10.1234/ten" else (
            {"title": "When Is a Liability Not a Liability?", "authors": ["T L"], "container": "The Journal of Finance", "issn": ["0022-1082"], "doi": doi, "pub_date": "2011", "url": "https://doi.org/" + doi, "abstract": None} if doi.startswith("10.1111") else None)
        importer._existing = lambda p: "Papers/2026-05/ISR/a.pdf" if p["title"] == "Paper A on platforms" else None
        items = [{"id": "W10", "doi": "https://doi.org/10.1234/ten", "title": "The platform classic", "reason": "Cited by 4 of your papers"},
                 {"id": "W13", "doi": "", "title": "Only C cites this one", "authors": ["Q R"], "year": 2021, "venue": "Journal of Odd Things"},
                 {"id": "W1", "doi": "10.1/a", "title": "Paper A on platforms"}, {"id": "W99", "title": ""},
                 {"id": "W77", "doi": "10.1111/j.1540-6261.2010.01625.x", "title": "When Is a Liability Not a Liability?"}]
        papers, have = rl.run_papers(items)
        assert [p["title"] for p in papers] == ["The platform classic", "Only C cites this one", "When Is a Liability Not a Liability?"] and [h["title"] for h in have] == ["Paper A on platforms"], (papers, have)
        a, b, c = papers
        assert c["journal"] == "The Journal of Finance" and c["abbr"] == "JF" and c["publisher"] == "wiley" and c["pub_date"] == "2011-01"      # Wiley's DOI prefix is not the Information Systems Journal; its route is still Wiley's
        assert rl._abbr("Econometrica") == "ECONO" and rl._abbr("Advances in Neural Information Processing Systems") == "ANIPS"
        assert a["abbr"] == "ISR" and a["publisher"] == "informs" and a["id"] == "doi:10.1234/ten" and a["verdict"] == "download" and a["reason"].startswith("Cited by 4") and a["authors"] == ["Zed Quinn"] and a["pub_date"].startswith("2020")
        assert b["publisher"] == "" and b["abbr"] == "JOT" and b["pub_date"] == "2021-01" and b["id"].startswith("t:") and b["journal"] == "Journal of Odd Things"
        import pipeline as pl_
        run = pl_.Run("picks", {"label": "Cited by Platforms"}); [run.add_paper(p) for p in papers]
        d = run.to_dict(); assert d["title"] == "Picks · Cited by Platforms" and d["n_download"] == 3 and d["kind"] == "picks" and run.id.endswith("_picks")
        importer.crossref_meta, importer._existing = orig_meta, orig_ex
    check("one click into the pipeline: chosen papers become a 'picks' run with the watched journals' publisher routes and verdict 'download'; papers you have are skipped", t_run_papers)

    print("round 2d backend: skim layer")

    def t_skim_locate():
        P = "Our sample covers 120 firms over six years. We estimate “fixed effects” models, and find that transparency raises entry by 12%."
        assert sk.locate(P, "our sample covers 120 firms over six years.") == "Our sample covers 120 firms over six years."
        assert sk.locate(P, 'We estimate "fixed effects" models,  and find that transparency raises entry by 12%.') == "We estimate “fixed effects” models, and find that transparency raises entry by 12%."      # straight quotes, double spaces
        assert sk.locate(P, "Our sample covers 120 firms across six years.") == "Our sample covers 120 firms over six years."                                                                    # a paraphrase maps to the nearest sentence
        assert sk.locate(P, "Firms with unionised workforces responded differently to the rule.") is None and sk.locate(P, "x") is None
        assert sk.norm_map("A  b\n—c")[0] == "a b -c"
    check("skim: the model's sentence is mapped back to the PDF's own wording (quotes, spaces, near-paraphrases); invented text is refused", t_skim_locate)

    chosen_a, _ = pc.passages(PAP["a"]["pdf"])
    txt = {i: b["text"] for i, b in enumerate(chosen_a)}
    q_q, q_s, q_f, q_l, q_f2 = (SENT["a"][0], SENT["a"][1], SENT["a"][2], SENT["a"][3], SENT["a"][5])

    def pick(lens, quote, rank=1, passage=1):
        return {"lens": lens, "passage": passage, "quote": quote, "rank": rank}

    def t_skim_build():
        raw = [pick("sample", q_s, 1, 1),                                                    # right sentence, wrong passage number: found anyway
               pick("question", q_q.replace("platform governance", "platform  governance").rstrip("."), 1),            # spacing / missing full stop
               pick("finding", "The paper shows that governance is irrelevant to entry.", 1),                          # invented: dropped
               pick("nonsense", q_f), pick("finding", q_f, 2), pick("limitation", q_f, 1),                              # the same sentence for two lenses: the better rank wins
               pick("limitation", q_l, 3), pick("contribution", q_f2, 1), {"lens": "finding"}, "junk"]
        items, dropped = sk.build_items(raw, chosen_a)
        by = {i["quote"]: i for i in items}
        assert dropped >= 4 and q_s in by and by[q_s]["lens"] == "sample" and by[q_s]["rank"] == 1 and by[q_s]["page"] >= 1 and by[q_s]["id"] == sk._id("sample", q_s)
        assert q_q in by and by[q_q]["lens"] == "question"                                         # the PDF's own sentence, full stop included
        assert by[q_f]["lens"] == "limitation" and by[q_f]["rank"] == 1 and sum(1 for i in items if i["quote"] == q_f) == 1
        assert [(i["page"], i["idx"]) for i in items] == sorted((i["page"], i["idx"]) for i in items)             # reading order
        many = [pick("finding", t, 1) for t in list(txt.values())[:5]] + [pick("finding", t, 2) for t in list(txt.values())[5:]]
        capped, _ = sk.build_items(many, chosen_a)
        assert sum(1 for i in capped if i["rank"] == 1) <= 2 and len(capped) <= sk.MAX_PER_LENS                  # at most two key points and six picks per lens
        assert sk.build_items("nope", chosen_a) == ([], 0)
    check("skim: picks are checked against the passages, de-duplicated across lenses, ranked (two key points per lens) and put in reading order", t_skim_build)

    def t_skim_generate():
        calls = []
        def responder(body):
            calls.append(body); assert body["messages"][0]["content"].startswith("You help a researcher skim an academic paper")
            return [json.dumps({"items": [pick("question", q_q, 1), pick("sample", q_s, 1), pick("finding", q_f, 1), pick("finding", q_f, 3), pick("limitation", q_l, 2), pick("contribution", q_f2, 1), pick("finding", "Made up sentence about nothing in the paper.", 1)]})]
        Fake.responder = responder
        pdf_ = PAP["a"]["pdf"]; assert sk.load(pdf_) is None
        e = sk.estimate(pdf_); assert e["cost"] > 0 and e["model"] == "m1"
        msgs = list(sk.stream(pdf_, PAP["a"]["row"]))
        assert msgs[-1]["type"] == "done" and msgs[-2]["type"] == "skim", msgs[-1]
        d = msgs[-2]["skim"]; assert d["dropped"] >= 1 and {i["lens"] for i in d["items"]} == {"question", "sample", "finding", "limitation", "contribution"} and os.path.exists(ns.skim_path(pdf_)) and ns.skim_path(pdf_) in ns.sidecars(pdf_)
        sent = calls[0]["messages"][-1]["content"]; assert "[1] (p. 1)" in sent and "Paper A on platforms" in sent and "response_format" in calls[0]
        assert "- finding (Finding)" in calls[0]["messages"][0]["content"] and all(l["id"] in calls[0]["messages"][0]["content"] for l in sk.LENSES)
        n = len(calls); again = list(sk.stream(pdf_, PAP["a"]["row"])); assert again[0]["existing"] and len(calls) == n             # a saved skim costs nothing
        ids = [i["id"] for i in d["items"]]
        assert sk.save_dismissed(pdf_, [ids[0], "bogus", ids[0]]) == [ids[0]]
        fresh = list(sk.stream(pdf_, PAP["a"]["row"], fresh=True)); assert len(calls) == n + 1 and fresh[-2]["skim"]["dismissed"] == [ids[0]]            # a re-skim remembers what you dismissed
        Fake.responder = lambda body: ['{"items": [{"lens": "finding", "passage": 1, "quo']
        bad = list(sk.stream(PAP["b"]["pdf"], PAP["b"]["row"])); assert bad[-1]["type"] == "error" and "valid JSON" in bad[-1]["error"] and sk.load(PAP["b"]["pdf"]) is None
        Fake.responder = lambda body: [json.dumps({"items": [pick("finding", "Nothing here is in the paper at all.", 1)]})]
        assert "didn’t point at any sentence" in list(sk.stream(PAP["b"]["pdf"], PAP["b"]["row"]))[-1]["error"]
        try:
            sk.save_dismissed(PAP["c"]["pdf"], ["x"]); raise AssertionError("not skimmed")
        except ValueError:
            pass
        Fake.responder = None
    check("skim: one call per paper, saved beside the PDF, cached, dismissals survive a re-skim; cut-off or ungrounded answers are errors", t_skim_generate)

    def t_folder_cleanup2():
        fo.delete(fid2); assert not any(os.path.exists(os.path.join(fo.DIR, fid2 + x)) for x in (".json", ".ask.json", ".matrix.json"))
    check("deleting a folder also deletes its conversation and matrix", t_folder_cleanup2)

    def t_related_work():
        P = {"P1": {"file": "a.pdf", "title": "A", "label": "Tian & Xue", "year": "2026", "short": "Tian & Xue (2026)", "ref": ["Tian, J., & Xue, K. (2026). A. ", "Journal", "."], "key": "k1", "doi": ""},
             "P2": {"file": "b.pdf", "title": "B", "label": "Zhang", "year": "2025", "short": "Zhang (2025)", "ref": ["Zhang, L. (2025). B. ", "Journal", "."], "key": "k2", "doi": ""},
             "P3": {"file": "c.pdf", "title": "C", "label": "Lee et al.", "year": "2024", "short": "Lee et al. (2024)", "ref": ["Lee, H. (2024). C. ", "Journal", "."], "key": "k3", "doi": ""}}
        N = {"P1-1": {"kind": "finding", "text": "x", "page": 4, "quote": ""}, "P1-2": {"kind": "finding", "text": "y", "page": 6, "quote": ""}, "P2-1": {"kind": "question", "text": "z", "page": 2, "quote": ""}}
        raw = ("## Trust\nPlatform rules shape how participants behave and what they decide to disclose to others [P1-1, P2-1]. {P1-2} show that entry rises after a rule change [P9-1, P1-2].\n"
               "A claim with no source at all that is long enough to be flagged by the check here.\n\n"
               "## Gap and contribution\nThe present study examines how disclosure shapes trust and funding decisions in platforms.\nNobody has measured this across platforms or over a longer time window than a year.")
        c = rw.check(raw, P, N)
        assert c["invalid"] == 1 and c["used_papers"] == ["P1", "P2"], c
        assert [u["id"] for u in c["unused"]] == ["P3"], c["unused"]
        assert len(c["uncited"]) == 2 and c["uncited"][0].startswith("A claim with no source") and c["uncited"][1].startswith("Nobody has measured"), c["uncited"]   # your own study is not flagged, but a bare claim in the same section is
        doc = {"raw": raw, "papers": P, "notes": N, "style": "author-year", "pages": False}
        r = rw.render(doc)
        assert "(Tian & Xue, 2026; Zhang, 2025)" in r["text"] and "Tian & Xue (2026)" in r["text"] and "P9" not in r["text"] and "[P" not in r["text"], r["text"]
        assert [x["id"] for x in r["refs"]] == ["P1", "P2"] and r["refs"][0]["text"].startswith("Tian"), r["refs"]
        n = rw.render(doc, style="numbered", pages=True)
        assert "[1, 2]" in n["text"] and "Tian & Xue [1]" in n["text"] and [x["n"] for x in n["refs"]] == [1, 2], n["text"]
        pg = rw.render(doc, pages=True)
        assert "Tian & Xue, 2026, pp. 4–6" not in pg["text"] and "(Tian & Xue, 2026, p. 4; Zhang, 2025, p. 2)" in pg["text"], pg["text"]
    check("related work: ids are checked, own-study sentences are not flagged, citations are written author-year or numbered, with pages and references", t_related_work)

    def t_claims():
        sp = cl.split("## Heading\nPlatform rules shape how participants behave on digital platforms (Costa, 2026; Zhang, 2026). Tian et al. (2026) show that transparency changes entry into the market. "
                      "Is this a question about platforms and their rules? We study how disclosure shapes trust in this paper.\n- A bullet sentence that is long enough to count as a claim in this test.\nShort one.")
        assert [c["n"] for c in sp] == [1, 2, 3, 4] and [c["own"] for c in sp] == [False, False, True, False], sp           # "et al. (2026)" is not a sentence end; questions, headings and short lines are dropped
        assert len(cl.split("This is a sentence with enough words in it. " * 60)) == cl.MAX_CLAIMS
        rows = [{"file": "a.pdf", "authors": "Tian, Jing; Xue, Kai", "year": "2026", "title": "A"}, {"file": "b.pdf", "authors": "Zhang, Li", "year": "2025", "title": "B"}]
        assert cl.cited_papers("Tian & Xue (2026) show this.", rows) == ["a.pdf"] and sorted(cl.cited_papers("This holds (Zhang, 2025; Tian & Xue, 2026).", rows)) == ["a.pdf", "b.pdf"] and cl.cited_papers("Zhang (2024) says so.", rows) == []
        claims = [{"n": 1, "text": "c1"}, {"n": 2, "text": "c2"}, {"n": 3, "text": "c3"}, {"n": 4, "text": "c4"}, {"n": 5, "text": "c5"}]
        mk = lambda n, f: [{"id": "C%d.%d" % (n, k), "file": f, "cite": "X", "title": "t", "page": k, "quote": "q", "text": "text %d" % k} for k in (1, 2)]   # noqa: E731
        items = [mk(1, "a.pdf"), mk(2, "b.pdf"), mk(3, "b.pdf"), mk(4, "a.pdf"), mk(5, "a.pdf")]
        cited = [[], [], ["a.pdf"], [], []]
        ans = [{"n": 1, "verdict": "Supported", "reason": "r1", "evidence": [{"id": "C1.1", "stance": "supports"}, {"id": "C9.9", "stance": "supports"}]},
               {"n": 2, "verdict": "supported", "reason": "no passage", "evidence": []},
               {"n": 3, "verdict": "supported", "reason": "other paper", "evidence": [{"id": "C3.2", "stance": "supports"}]},
               {"n": 4, "verdict": "contradicted", "reason": "r4", "evidence": [{"id": "C4.1", "stance": "context"}]}]
        out, dropped = cl.assemble(claims, items, cited, ans)
        v = {c["n"]: c for c in out}
        assert v[1]["verdict"] == "supported" and [e["id"] for e in v[1]["evidence"]] == ["C1.1"] and dropped == 1, v[1]              # the passage that was never offered is dropped
        assert v[2]["verdict"] == "not_found" and "not counted as supported" in v[2]["note"], v[2]                                    # supported with no passage behind it
        assert v[3]["verdict"] == "supported" and v[3]["cited_ok"] is False and v[3]["cited"] == ["a.pdf"], v[3]                       # the cited paper is not the one that says it
        assert v[4]["verdict"] == "not_found" and "contradicts" in v[4]["note"], v[4]                                                   # contradicted with only a context passage
        assert v[5]["verdict"] == "unchecked" and v[5]["evidence"] == [], v[5]                                                         # the model skipped it
        assert v[1].get("cited_ok") is None and "cited" not in v[1]
        doc = {"papers": 2, "scope": "folder", "model": "m", "generated": "2026-10-27T10:00:00", "claims": out}
        md = cl.markdown(doc, "Lab")
        assert "## Supported (2)" in md and "## Not found in your papers (2)" in md and "does not clearly say this" in md and md.index("## Not found") < md.index("## Supported"), md
    check("claim check: sentences, named papers, and verdicts held to the passages the model was given", t_claims)

    def t_synthesis():
        P = {"P1": {"short": "Tian & Xue (2026)", "file": "a.pdf", "title": "A"}, "P2": {"short": "Zhang (2025)", "file": "b.pdf", "title": "B"}}
        N = {"P1-1": {"kind": "finding", "text": "x", "page": 3}, "P1-2": {"kind": "limitation", "text": "y", "page": 5}, "P2-1": {"kind": "finding", "text": "z", "page": 2}}
        d = {"overview": "Overview.", "overview_ids": ["P1-1", "P9-9"],
             "themes": [{"name": "Trust", "summary": "P2 agrees with P1 here.", "consensus": "agree", "tension": "ignored when they agree",
                         "positions": [{"paper": "P1", "claim": "c1", "stance": "supports", "ids": ["P1-1"]}, {"paper": "P2", "claim": "c2", "stance": "WEIRD", "ids": ["P2-1", "P1-1"]}, {"paper": "P2", "claim": "again", "stance": "supports", "ids": ["P2-1"]}]},
                        {"name": "Solo", "summary": "s", "consensus": "contested", "tension": "t", "positions": [{"paper": "P1", "claim": "c", "stance": "opposes", "ids": ["P1-2"]}]},
                        {"name": "Ghost", "summary": "s", "consensus": "agree", "positions": [{"paper": "P1", "claim": "c", "stance": "supports", "ids": ["P9-1"]}]}],
             "gaps": [{"gap": "g1", "why": "w", "kind": "time", "question": "q", "ids": ["P1-2"]}, {"gap": "g2", "why": "w", "kind": "nonsense", "question": "q", "ids": []}]}
        r = sy.assemble(d, P, N)
        assert r["dropped"] == 2 and r["overview_ids"] == ["P1-1"], r                                            # the two ids that were never given
        assert [t["name"] for t in r["themes"]] == ["Trust", "Solo"], r["themes"]                                  # a theme with no real note behind it is gone
        t1, t2 = r["themes"]
        assert [(p["paper"], p["stance"], p["ids"]) for p in t1["positions"]] == [("P1", "supports", ["P1-1"]), ("P2", "neutral", ["P2-1"])] and t1["tension"] == "", t1      # an unknown stance is neutral, an id of another paper is dropped, one position per paper
        assert t1["summary"] == "Zhang (2025) agrees with Tian & Xue (2026) here.", t1["summary"]                  # a paper named by its id in a sentence is named the way a reader knows it
        assert t2["consensus"] == "single" and t2["tension"] == "t", t2                                            # one paper only is called that, whatever the model said
        assert [(g["kind"], g["grounded"]) for g in r["gaps"]] == [("time", True), ("other", False)], r["gaps"]
        md = sy.markdown({**r, "papers": P, "notes": N, "model": "m", "generated": "2026-10-27T00:00:00"}, "Lab")
        assert "### Trust (agree)" in md and "Tian & Xue (2026), p. 3" in md and "not grounded in your papers" in md, md
    check("synthesis: positions, themes and gaps are held to the notes the model was given", t_synthesis)

    def t_flashcards():
        from datetime import date, timedelta
        fc.PATH = os.path.join(PAPERS, "_flashcards.json")
        t = date(2026, 10, 27)
        new = {"ease": 2.5, "interval": 0, "reps": 0, "lapses": 0}
        assert {r: fc.schedule(new, r, t)["interval"] for r in (1, 2, 3, 4)} == {1: 0, 2: 1, 3: 1, 4: 4}
        g1 = fc.schedule(new, 3, t); g2 = fc.schedule(g1, 3, t); g3 = fc.schedule(g2, 3, t)
        assert (g1["interval"], g2["interval"], g3["interval"]) == (1, 3, 8) and g3["due"] == (t + timedelta(days=8)).isoformat(), (g1, g2, g3)
        lapse = fc.schedule(g3, 1, t)
        assert lapse["reps"] == 0 and lapse["lapses"] == 1 and lapse["interval"] == 0 and lapse["ease"] == 2.3 and lapse["due"] == t.isoformat()
        assert fc.schedule(fc.schedule(new, 1, t), 1, t)["ease"] == 2.1 and min(fc.schedule({**new, "ease": 1.3}, 1, t)["ease"], 9) == 1.3          # ease never drops below 1.3
        added = [fc.add("Q%d?" % i, "A%d" % i, "Papers/x.pdf", 3, "quote") for i in range(12)]
        assert fc.counts(fc.load())["new"] == 12 and fc.counts(fc.load())["new_today"] == 10 and len(fc.queue(fc.load())) == 10          # at most ten new cards a day
        fc.rate(added[0]["id"], 3); fc.rate(added[1]["id"], 1)
        doc = fc.load(); c = fc.counts(doc)
        assert c["new_today"] == 8 and c["due"] == 1 and [x["id"] for x in fc.queue(doc)][0] == added[1]["id"] and doc["cards"][0]["introduced"] == date.today().isoformat(), c   # the lapsed card is due again today; two cards used up two of today's new ones
        fc.update(added[2]["id"], suspended=True); assert fc.counts(fc.load())["new"] == 9 and fc.counts(fc.load())["suspended"] == 1
        fc.update(added[3]["id"], q="  Changed   question? ")
        assert fc.load()["cards"][3]["q"] == "Changed question?" and fc.delete([added[4]["id"], "nope"]) == 1 and len(fc.load()["cards"]) == 11
        try:
            fc.add("", "no question")
            raise AssertionError("empty card accepted")
        except ValueError:
            pass
        pt = {"P1": {"file": "a.pdf"}, "P2": {"file": "b.pdf"}}
        nt = {"P1-1": {"page": 4, "quote": "q1"}, "P1-2": {"page": None, "quote": ""}, "P2-1": {"page": 2, "quote": "q2"}}
        d = {"cards": [{"paper": "P1", "q": "Q one?", "a": "A", "ids": ["P1-1"]}, {"paper": "P1", "q": "q ONE", "a": "dup within", "ids": ["P1-1"]}, {"paper": "P2", "q": "Q two?", "a": "A", "ids": ["P2-1", "P1-1"]},
                      {"paper": "P2", "q": "No note", "a": "A", "ids": ["P9-9"]}, {"paper": "P7", "q": "Wrong paper", "a": "A", "ids": ["P1-2"]}, {"paper": "P1", "q": "", "a": "A", "ids": ["P1-1"]},
                      {"paper": "P1", "q": "Third?", "a": "A", "ids": ["P1-2"]}, {"paper": "P1", "q": "Fourth?", "a": "A", "ids": ["P1-2"]}]}
        out, bad, dup = fc.assemble(d, pt, nt, 3, [{"file": "b.pdf", "q": "Q TWO?"}])
        assert [(c["q"], c["file"], c["page"]) for c in out] == [("Q one?", "a.pdf", 4), ("Wrong paper", "a.pdf", None), ("Third?", "a.pdf", None)], out      # per-paper cap 3, a paper named wrongly is read from its note's id
        assert bad == 2 and dup == 2, (bad, dup)                                                                                               # no note / empty question dropped; a repeat of yours and a repeat inside the answer skipped
    check("flashcards: SM-2 scheduling, the daily new-card limit, edits, and cards held to the notes the model was given", t_flashcards)

    def t_board_notes():
        pdf = os.path.join(PAPERS, "board.pdf")
        open(pdf, "wb").write(b"%PDF-1.4\n%%EOF")
        ns.ROOT = TMP
        n0 = ns.load(pdf)
        assert n0["frames"] == [] and n0["links"] == [] and n0["paper_note"] == ""
        out = ns.save(pdf, {"highlights": [], "cards": [{"id": "a", "type": "note"}, {"id": "b", "type": "note"}], "bookmarks": [], "frames": [{"id": "f1", "x": 1, "y": 2, "w": 300, "h": 200, "label": "Group"}, "junk"],
                            "links": [{"id": "l1", "a": "a", "b": "b", "label": "refines"}], "paper_note": "Why this matters."}, 0)
        back = ns.load(pdf)
        assert back["frames"] == [{"id": "f1", "x": 1, "y": 2, "w": 300, "h": 200, "label": "Group"}] and back["links"][0]["label"] == "refines" and back["paper_note"] == "Why this matters." and out["rev"] == 1, back
        empty = ns.save(pdf, {"highlights": [], "cards": [], "bookmarks": [], "frames": [], "links": [], "paper_note": "  "}, 1)
        assert "frames" not in empty and "links" not in empty and "paper_note" not in empty                                  # nothing extra is written when there is nothing to keep
    check("notes: frames, arrows and the note on the paper as a whole are saved with the notes and nothing extra is written when they are empty", t_board_notes)

    def t_workspace_clean():
        doc = {"cards": [{"id": "a", "type": "excerpt", "x": 10, "y": 20, "w": 280, "h": 100, "text": "A finding.", "note": "mine", "file": "Papers/x.pdf", "page": 4},
                         {"id": "b", "type": "note", "x": 400, "y": 30, "w": 280, "h": 80, "text": "My idea"}, {"id": "c", "type": "bogus", "x": 0, "y": 0}, {"id": "a", "type": "note", "x": 0, "y": 0, "text": "duplicate id"},
                         {"id": "d", "type": "paper", "x": 50, "y": 600, "w": 280, "h": 60, "text": "A title", "file": "../outside.pdf"}],
               "frames": [{"id": "f", "x": 0, "y": 0, "w": 380, "h": 300, "label": "Group"}], "links": [{"id": "l", "a": "a", "b": "b", "label": "→"}, {"id": "m", "a": "a", "b": "zzz"}, {"id": "n", "a": "a", "b": "a"}]}
        c = wk.clean(doc)
        assert [x["id"] for x in c["cards"]] == ["a", "b", "d"] and "file" not in c["cards"][2], c["cards"]                            # unknown types, repeated ids and files outside Papers/ are not kept
        assert c["cards"][0]["file"] == "Papers/x.pdf" and c["cards"][0]["page"] == 4 and [l["id"] for l in c["links"]] == ["l"], c       # arrows only between cards that exist
        g = wk.groups({**c, "rev": 1})
        assert [x["title"] for x in g] == ["Group", "My notes"] and [k["id"] for k in g[0]["cards"]] == ["a"] and [k["id"] for k in g[1]["cards"]] == ["b", "d"], g       # the frame first (a is inside it), then the cards that have no paper
        md = wk.markdown({**c, "rev": 1}, "Lab", [{"file": "Papers/x.pdf", "authors": "Tian, Jing; Xue, Kai", "year": "2026", "title": "T"}])
        assert "## Group" in md and "> A finding." in md and "Tian" in md and "p. 4" in md and "My idea" in md, md
    check("workspace: what may be stored is cleaned (types, ids, files, arrows) and the board reads as a document", t_workspace_clean)

    def t_bibimport():
        ris = ("TY  - JOUR\nAU  - Rai, Arun\nAU  - M\u00fcller, Jan\nTI  - Will your AI turn\n       backers away?\nPY  - 2026/04/01\nT2  - Decision Support Systems\nDO  - https://doi.org/10.1016/j.dss.2026.114773\nAB  - An abstract.\nER  -\n"
               "TY  - BOOK\nTI  - A book without doi\nPY  - 2019\nA2  - An Editor\nER  -\nTY  - JOUR\nAU  - Nobody\nER  -\n")
        e = bi.parse(ris, "x.ris")
        assert [x["title"] for x in e] == ["Will your AI turn backers away?", "A book without doi"], e                                    # a wrapped title is joined; an entry without a title is dropped
        assert e[0]["authors"] == ["Arun Rai", "Jan M\u00fcller"] and e[0]["year"] == "2026" and e[0]["doi"] == "10.1016/j.dss.2026.114773" and e[0]["journal"] == "Decision Support Systems" and e[1]["authors"] == [], e
        bib = ("@article{rai2026fair,\n  author = {Rai, Arun and M{\\\"u}ller, Jan and Singhal, S.},\n  title = {Will {AI} turn backers away? {\\'E}vidence from Kickstarter},\n  journal = {Decision Support Systems},\n"
               "  year = 2026, doi = \"10.1016/j.dss.2026.114773\",\n  abstract = {Something with \\& and {nested {braces}}.}\n}\n@comment{ignored}\n@string{x = \"y\"}\n@inproceedings(lee2020, title={Another paper}, author={Lee, H.}, booktitle={ICIS}, year={2020})\n")
        b = bi.parse(bib, "x.bib")
        assert len(b) == 2 and b[0]["title"] == "Will AI turn backers away? \u00c9vidence from Kickstarter" and b[0]["authors"] == ["Arun Rai", "Jan M\u00fcller", "S. Singhal"] and b[0]["abstract"] == "Something with & and nested braces." and b[0]["key"] == "rai2026fair", b[0]
        assert b[1]["journal"] == "ICIS" and b[1]["year"] == "2020" and b[1]["doi"] == "" and bi.parse("nothing here") == []
        m = bi.match(b, [{"file": "Papers/a.pdf", "doi": "10.1016/J.DSS.2026.114773", "title": "x"}, {"file": "Papers/b.pdf", "doi": "", "title": "ANOTHER paper!"}])
        assert [x["file"] for x in m] == ["Papers/a.pdf", "Papers/b.pdf"] and m[0]["has_doi"] is True and m[1]["has_doi"] is False, m                            # matched by DOI, or by title when there is none
    check("bibliography import: RIS and BibTeX are read (accents, nested braces, wrapped lines), and matched to the library by DOI or title", t_bibimport)

    def t_smart():
        sm.PATH = os.path.join(PAPERS, "_smart.json")
        assert sm.load() == []
        items = sm.add("  Platform   work ", {"q": " complementors ", "status": "toread", "star": 1, "nonsense": "x", "journal": ""})
        assert items[0]["name"] == "Platform work" and items[0]["filters"] == {"q": "complementors", "status": "toread", "star": True}, items
        for bad in (lambda: sm.add("platform WORK", {"q": "x"}), lambda: sm.add("", {"q": "x"}), lambda: sm.add("Empty", {})):
            try:
                bad()
                raise AssertionError("accepted")
            except ValueError:
                pass
        sid = items[0]["id"]
        assert sm.rename(sid, "Platforms")[0]["name"] == "Platforms" and sm.delete(sid) == [] and not os.path.exists(sm.PATH)
    check("saved searches: names and filters are cleaned, repeats refused, an empty list leaves no file", t_smart)

    def t_personal():
        pe.CACHE = os.path.join(TMP, "personal_vecs.json"); pe._mem.clear()
        A = ["Platform governance and transparency rules for complementors", "Transparency of platform rules and complementor entry", "Governance of digital platforms: rules and complementors", "Complementors respond to platform governance rules"]
        B = ["Protein folding with deep neural networks", "Neural networks for protein structure prediction"]
        mk = lambda i, t, st=None, **kw: {"file": "Papers/x%d.pdf" % i, "title": t, "matched_topics": "", "state": st or {}, "folders": [], "notes": None, "score": 5, "date_checked": "2020-01-01", **kw}   # noqa: E731
        few = [mk(0, A[0], {"status": "read"})] + [mk(1, A[1]), mk(2, B[0])]
        q = pe.queue(few)
        assert not q["profile"]["ready"] and [i["file"] for i in q["items"]] == ["Papers/x1.pdf", "Papers/x2.pdf"] and all(i["fit"] == "" for i in q["items"]), q            # one read paper is not a profile yet
        rows = [mk(0, A[0], {"status": "read"}), mk(1, A[1], {"star": True}), mk(2, A[2], {"status": "read", "star": True}), mk(3, A[3]), mk(4, B[0]), mk(5, B[1], {"status": "toread"}), mk(6, "Done already", {"status": "read"}), mk(7, "Skipped one", {"skip": True}), mk(8, "Opened one", {"opened": "x"})]
        q = pe.queue(rows, 10)
        files = [i["file"] for i in q["items"]]
        assert q["profile"]["ready"] and "Papers/x6.pdf" not in files and "Papers/x7.pdf" not in files and "Papers/x8.pdf" not in files, files                       # read, skipped and already-opened papers are not offered
        assert files[0] == "Papers/x5.pdf" and q["items"][0]["why"].startswith("you marked it to read"), q["items"][0]                                                      # your own "to read" list comes first
        fit = {i["file"]: i["fit"] for i in q["items"]}
        assert fit["Papers/x3.pdf"] == "high" and fit["Papers/x4.pdf"] != "high", fit                                                                                       # the platform paper is closer to what you read than the protein paper
        f = pe.fits([{"id": "n1", "title": "Rules of platforms and what complementors do"}, {"id": "n2", "title": "Folding proteins with neural nets"}, {"id": "n3", "title": ""}], rows)
        assert set(f) == {"n1", "n2"} and f["n1"]["score"] > f["n2"]["score"] and f["n1"]["band"] == "high", f
        assert pe.fits([{"id": "n1", "title": "Anything"}], few) == {}                                                                                                     # not enough to learn from: no fits, no guesses
    check("reading queue: your list first, read/skipped/opened papers left out, fit learned from what you read, nothing guessed from too little", t_personal)

    def t_mcp_protocol():
        r = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}})
        assert r["result"]["protocolVersion"] == "2025-03-26" and "tools" in r["result"]["capabilities"], r                                   # it echoes the client's protocol version
        assert mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None and mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "ping"})["result"] == {}
        tools = mcp.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})["result"]["tools"]
        assert len(tools) == 8 and all(t["inputSchema"]["type"] == "object" and t["description"] for t in tools), [t["name"] for t in tools]
        assert mcp.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "nope"}})["error"]["code"] == -32602
        assert mcp.handle({"jsonrpc": "2.0", "id": 5, "method": "nope"})["error"]["code"] == -32601 and mcp.handle({"jsonrpc": "2.0", "method": "nope"}) is None
        mcp.rows = lambda: [{"file": "Papers/a.pdf", "title": "Alpha platforms", "authors": "Jane Doe; Jo Roe", "year": "2026", "state": {"status": "read"}, "folders": [], "card": True, "summary": None, "notes": None},
                            {"file": "Papers/b.pdf", "title": "Beta platforms", "authors": "", "year": "2025", "state": {}, "folders": [], "card": False, "summary": None, "notes": None}]
        st = json.loads(mcp.handle({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "library_stats", "arguments": {}}})["result"]["content"][0]["text"])
        assert st["papers"] == 2 and st["read"] == 1 and st["with_card"] == 1, st
        bad = mcp.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "get_paper", "arguments": {"paper": "platforms"}}})["result"]
        assert bad["isError"] and "More than one paper" in bad["content"][0]["text"], bad                                                    # an ambiguous title is an error the model can act on
        assert mcp.cite({"authors": "Jane Doe; Jo Roe", "year": "2026"}) == "Doe et al. (2026)"
    check("MCP server: protocol replies, tool list, errors the model can read, ambiguous titles refused", t_mcp_protocol)

    def t_reviewer():
        text = "Platforms that publish clear rules attract more complementors. We study this with data from 120 firms over five years."
        P = {"P1": {"short": "Tian & Xue (2026)"}}
        N = {"P1-2": {"kind": "finding", "text": "x", "page": 3}}
        d = {"summary": "S.", "recommendation": "Reject-ish", "major": [{"point": "A", "why": "w", "suggestion": "s"}] * 6, "minor": [{"point": "m"}, {"point": ""}],
             "unsupported": [{"quote": "platforms that PUBLISH clear rules attract more complementors", "need": "n"}, {"quote": "invented sentence not in the text", "need": "n"}, {"quote": "short", "need": "n"}],
             "literature": [{"paper": "P1", "point": "P1 says otherwise.", "ids": ["P1-2"]}, {"paper": "P1", "point": "no note", "ids": ["P1-9"]}, {"paper": "P7", "point": "x", "ids": ["P7-1"]}], "questions": ["q1", "", "q2"]}
        r = rv.assemble(d, text, P, N)
        assert r["recommendation"] == "major" and len(r["major"]) == 4 and len(r["minor"]) == 1, r                                          # an unknown recommendation becomes "major"; lists are capped; empty points dropped
        assert [u["quote"] for u in r["unsupported"]] == ["platforms that PUBLISH clear rules attract more complementors"] and r["dropped"] == 4, r   # only quotations that are in the text (ignoring case and punctuation) survive
        assert [(l["paper"], l["ids"], l["point"]) for l in r["literature"]] == [("P1", ["P1-2"], "Tian & Xue (2026) says otherwise.")] and r["questions"] == ["q1", "q2"], r
        md = rv.markdown({**r, "papers": P, "notes": N, "model": "m", "generated": "2026-10-27T00:00:00"}, "Lab")
        assert "## Major concerns" in md and "## Your own papers bear on this" in md and "Tian & Xue (2026), p. 3" in md, md
    check("Reviewer 2: quotations must be in your text, papers and notes must have been given, lists are capped", t_reviewer)

    def t_fulltext():
        mk = lambda t, pg=0, kind="body": {"text": t, "page": pg, "kind": kind}   # noqa: E731
        by = {"a.pdf": ({"title": "Alpha", "authors": "Jane Doe", "year": "2026"}, [mk("Transparent governance rules attract complementors to open platforms.", 0), mk("Unrelated paragraph about cooking and sailing in the sea today.", 1), mk("Governance of platforms is also transparent in some markets and not in others.", 2)]),
              "b.pdf": ({"title": "Beta", "authors": "Jo Roe", "year": "2025"}, [mk("References: transparent governance rules, Journal of Tests, 2020, many many entries here.", 5, "ref"), mk("We study governance.", 1), mk("Rules of governance that are transparent matter; transparent rules are best.", 3)])}
        r = ft.search(by, "transparent governance")
        assert sorted((i["file"], i["page"]) for i in r) == [("a.pdf", 1), ("a.pdf", 3), ("b.pdf", 4)], r; assert r[0]["score"] >= r[-1]["score"]          # every paragraph with all the words, the bibliography's left out; best score first
        assert [(i["file"], i["page"]) for i in ft.search(by, '"governance rules"')] == [("a.pdf", 1)], ft.search(by, '"governance rules"')    # an exact phrase must match as written
        assert ft.search(by, "the of and") == [] and ft.search(by, "zebra governance") == [], "stop words only / a word that is nowhere"
        assert ft.parse('alpha "Exact  Phrase" beta') == (["exact phrase"], ["alpha", "beta"])
        assert "…" in ft.search({"c.pdf": ({"title": "C"}, [mk("word " * 80 + "needle sits here " + "tail " * 80)])}, "needle")[0]["snippet"]
    check("full-text search: all words in one paragraph, exact phrases, no bibliography, stop words ignored", t_fulltext)

    def t_latex():
        P = {"P1": {"key": "tian2026open", "short": "T (2026)"}, "P2": {"key": "zhang2025", "short": "Z (2025)"}}
        doc = {"raw": "## Trust & rules\nRules shape behaviour [P1-2, P2-1, P9-1] and cost 5% of revenue. {P1-3} show a_b effects [P2-4].\n\nA second paragraph with no citation.", "papers": P, "notes": {}}
        t = rw.latex(doc, "Lab")
        assert "\\subsection{Trust \\& rules}" in t and "\\citep{tian2026open,zhang2025}" in t and "\\citet{tian2026open}" in t and "\\citep{zhang2025}" in t, t
        assert "5\\% of revenue" in t and "a\\_b" in t and "P9" not in t and "[P" not in t and "{P1-3}" not in t, t                       # specials are escaped, ids that were not given vanish
    def t_docx_headings():
        import io
        from docx import Document
        P = {"P1": {"key": "k1", "short": "T (2026)", "label": "T", "year": "2026", "ref": ["T. (2026). A. ", "J", "."], "file": "a.pdf", "title": "A"}}
        doc = {"raw": "## Trust\nRules shape behaviour [P1-2].\nStill the same paragraph.\n\n## Gap\nSecond.", "papers": P, "notes": {"P1-2": {"page": 1}}, "style": "author-year", "pages": False}
        d = Document(io.BytesIO(rw.docx(doc, "Lab")))
        heads = [p.text for p in d.paragraphs if p.style.name.startswith("Heading")]
        body = [p.text for p in d.paragraphs if not p.style.name.startswith("Heading")]
        assert heads[:3] == ["Related work — Lab", "Trust", "Gap"] and body[0] == "Rules shape behaviour (T, 2026). Still the same paragraph." and body[1] == "Second.", (heads, body)   # a heading line is only the heading, even with no blank line after it
    check("related work: Word keeps a heading line separate from the paragraph under it", t_docx_headings)
    check("related work: LaTeX with \\citep / \\citet keys, escaped text, headings as \\subsection", t_latex)

    # ---- the updater: the feed, the choices, the download check and the swap of the app, all against a local feed and temporary folders
    import hashlib
    import subprocess
    import time as _time
    UP = os.path.join(TMP, "up"); os.makedirs(UP, exist_ok=True)
    up.SETTINGS, up.STATE_FILE, up.SUPPORT = os.path.join(UP, "update.json"), os.path.join(UP, "state.json"), os.path.join(UP, "support")
    ZIPB = os.urandom(3000)
    FEEDS = {"/appcast.json": {"app": "Journal Watcher", "version": "1.1.0", "released": "2026-10-28", "notes": ["One", " Two  words ", ""], "url": "http://127.0.0.1:%d/j.zip", "sha256": hashlib.sha256(ZIPB).hexdigest(), "size": len(ZIPB),
                               "history": [{"version": "1.0.5", "released": "2026-10-20", "notes": ["Old note"]}, {"version": "0.9.0", "notes": ["Older"]}, {"nope": 1}]}}

    class FeedH(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path == "/j.zip":
                body = ZIPB
            elif self.path in FEEDS:
                body = json.dumps(FEEDS[self.path]).encode() % () if False else json.dumps(FEEDS[self.path]).replace("%d", str(feed_srv.server_address[1])).encode()
            else:
                self.send_response(404); self.end_headers(); return
            self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    feed_srv = ThreadingHTTPServer(("127.0.0.1", 0), FeedH)
    threading.Thread(target=feed_srv.serve_forever, daemon=True).start()
    FURL = "http://127.0.0.1:%d/appcast.json" % feed_srv.server_address[1]
    os.environ["JW_VERSION"], os.environ["JW_UPDATE_FEED"] = "1.0.0", FURL

    def t_up_versions():
        assert up.newer("1.10.0", "1.9.9") and up.newer("2", "1.9.9") and not up.newer("1.0", "1.0.0") and not up.newer("1.0.0", "1.0.1") and up.newer("1.0.0.1", "1.0.0")
        assert up.version() == "1.0.0"
    def t_up_feed():
        f = up.parse_feed(json.dumps(FEEDS["/appcast.json"]).replace("%d", "1"))
        assert f["version"] == "1.1.0" and f["notes"] == ["One", "Two words"] and [h["version"] for h in f["history"]] == ["1.0.5", "0.9.0"], f
        good = dict(FEEDS["/appcast.json"], url="http://127.0.0.1:1/j.zip")
        for bad in ({"version": "abc"}, {"sha256": "xyz"}, {"url": "http://example.com/j.zip"}, {"url": "ftp://x/j.zip"}, {"url": "file:///etc/passwd"}):
            try:
                up.parse_feed(json.dumps({**good, **bad})); raise AssertionError("accepted " + str(bad))
            except ValueError:
                pass
        try:
            up.parse_feed("[1, 2]"); raise AssertionError("accepted a list")
        except ValueError:
            pass
    def t_up_check():
        st = up.check()
        assert st["available"] and st["latest"] == "1.1.0" and st["notes"] == ["One", "Two words"] and [h["version"] for h in st["history"]] == ["1.0.5"] and not st["skipped"] and st["feed"] and st["error"] == "", st
        assert st["can_install"] is False and st["app"] is False                            # a project run never installs by itself
        up.save_settings({"skipped": "1.1.0"})
        assert up.public()["skipped"] is True and up.public()["available"] is True          # still announced, but the pill stays hidden for a skipped version
        FEEDS["/appcast.json"]["version"] = "1.2.0"
        assert up.check()["skipped"] is False and up.public()["latest"] == "1.2.0"           # a newer one than the skipped is news again
        os.environ["JW_VERSION"] = "1.2.0"
        assert up.check()["available"] is False                                              # nothing newer than what you have
        os.environ["JW_VERSION"] = "1.0.0"
        FEEDS["/appcast.json"]["version"] = "1.1.0"
        assert up.check()["latest"] == "1.1.0"
    def t_up_failure():
        os.environ["JW_UPDATE_FEED"] = "http://127.0.0.1:1/appcast.json"
        try:
            st = up.check()
            assert st["error"] and "update feed" in st["error"].lower() and st["latest"] == "1.1.0", st        # the last good answer is kept, the problem is said
        finally:
            os.environ["JW_UPDATE_FEED"] = FURL
        assert up.check()["error"] == ""
    def t_up_settings():
        try:
            up.save_settings({"feed": "http://example.com/appcast.json"}); raise AssertionError("an insecure address was accepted")
        except ValueError:
            pass
        assert up.save_settings({"feed": "https://example.com/appcast.json", "auto": False})["auto"] is False and up.settings()["feed"] == "https://example.com/appcast.json"
        up.save_settings({"feed": "", "auto": True})
        assert up.settings()["auto"] is True and up.settings()["feed"] == ""
        assert up.public()["feed_url"] == FURL                                            # JW_UPDATE_FEED wins over what is saved
    def t_up_download():
        os.makedirs(up.SUPPORT, exist_ok=True)
        dest = os.path.join(up.SUPPORT, "x.zip")
        up._download("http://127.0.0.1:%d/j.zip" % feed_srv.server_address[1], dest, len(ZIPB), hashlib.sha256(ZIPB).hexdigest())
        assert open(dest, "rb").read() == ZIPB and up._progress["phase"] == "downloading"
        try:
            up._download("http://127.0.0.1:%d/j.zip" % feed_srv.server_address[1], dest, len(ZIPB), "0" * 64); raise AssertionError("a wrong checksum was accepted")
        except ValueError as e:
            assert "damaged" in str(e)
        try:
            up._stage(dest, "1.1.0", "local.journalwatcher.app"); raise AssertionError("random bytes were accepted as an app")
        except ValueError as e:
            assert "not a Journal Watcher app" in str(e), e
    def t_up_swap():
        def run(new_exists):
            d = tempfile.mkdtemp(dir=UP)
            tgt, new, stage = os.path.join(d, "Journal Watcher.app"), os.path.join(d, "stage", "Journal Watcher.app"), os.path.join(d, "stage")
            for p, txt in ((tgt, "old"), (new, "new")):
                if p == new and not new_exists:
                    os.makedirs(stage, exist_ok=True); continue
                os.makedirs(p); open(os.path.join(p, "v.txt"), "w").write(txt)
            helper = os.path.join(d, "swap.sh"); open(helper, "w").write(up.HELPER)
            sleeper = subprocess.Popen(["/bin/sleep", "1"])
            h = subprocess.Popen(["/bin/bash", helper, str(sleeper.pid), new, tgt, stage, os.path.join(d, "log")], env={**os.environ, "JW_OPEN": "true"})
            t0 = _time.time(); sleeper.wait(); h.wait(timeout=30)
            return d, tgt, stage, _time.time() - t0
        d, tgt, stage, dt = run(True)
        assert open(os.path.join(tgt, "v.txt")).read() == "new" and not [n for n in os.listdir(d) if ".old-" in n] and not os.path.exists(stage) and dt >= 0.9, os.listdir(d)   # waits for the old app to quit, then swaps
        d, tgt, stage, dt = run(False)
        assert open(os.path.join(tgt, "v.txt")).read() == "old" and not [n for n in os.listdir(d) if ".old-" in n], os.listdir(d)                                         # a failed copy puts the old app back
    def t_up_appcast_script():
        z = os.path.join(UP, "Journal-Watcher-1.3.0.zip"); open(z, "wb").write(ZIPB)
        out1, out2 = os.path.join(UP, "a1.json"), os.path.join(UP, "a2.json")
        mk = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(HERE)))), "app", "make_appcast.py")
        subprocess.run([sys.executable, mk, "--version", "1.2.0", "--zip", z, "--url", "https://example.com/a.zip", "--notes", "First|Second", "--out", out1], check=True, capture_output=True)
        subprocess.run([sys.executable, mk, "--version", "1.3.0", "--zip", z, "--url", "https://example.com/b.zip", "--notes", "Third", "--previous", out1, "--out", out2], check=True, capture_output=True)
        f = up.parse_feed(open(out2, "rb").read())
        assert f["version"] == "1.3.0" and f["sha256"] == hashlib.sha256(ZIPB).hexdigest() and f["size"] == len(ZIPB) and f["history"] == [{"version": "1.2.0", "released": f["history"][0]["released"], "notes": ["First", "Second"]}], f
    def t_frozen_browser_path():
        code = "import sys, os; sys.frozen = True; sys._MEIPASS = '/nowhere'; sys.path.insert(0, %r); import paths; print(os.environ.get('PLAYWRIGHT_BROWSERS_PATH'))" % HERE
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env={k: v for k, v in os.environ.items() if k != "PLAYWRIGHT_BROWSERS_PATH"}).stdout.strip()
        assert out == os.path.expanduser("~/Library/Caches/ms-playwright"), out                      # in the app Playwright must not look for Chromium inside the bundle
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env={**os.environ, "PLAYWRIGHT_BROWSERS_PATH": "/custom"}).stdout.strip()
        assert out == "/custom", out                                                                 # a place you chose is respected
    # ---- library access and sign-in status
    si.PROFILE_FILE, si.STATUS_FILE = os.path.join(UP, "library.json"), os.path.join(UP, "signins.json")

    def t_si_profile():
        assert si.profile()["mode"] == "institutional" and not si.profile()["configured"] and not si.profile()["signs_in"]          # nothing set up: the publishers' own institution pages
        json.dump({"name": "Example University Library", "openathens": "myuni.example.edu", "redirector": "https://go.openathens.net/redirector/myuni.example.edu?url={url}", "ebsco_home": "https://research.ebsco.com/c/x"}, open(si.PROFILE_FILE, "w"))
        p = si.profile()
        assert p["mode"] == "openathens" and p["configured"] and p["signs_in"] and p["home"].endswith("/c/x"), p                      # a library.json from before modes existed still works
        assert si.via("https://a.org/x y") == "https://go.openathens.net/redirector/myuni.example.edu?url=https%3A%2F%2Fa.org%2Fx%20y"
        q = si.save_profile({"name": "Uni of Somewhere", "mode": "ezproxy", "ezproxy": "https://ezproxy.somewhere.edu/login?url=", "ill_email": "ill@somewhere.edu"})
        assert q["mode"] == "ezproxy" and q["signs_in"] and "redirector" not in json.load(open(si.PROFILE_FILE)) and q["ebsco_home"].endswith("/c/x"), q   # the old OpenAthens redirector goes, notes you did not touch stay
        assert si.via("https://a.org/p") == "https://ezproxy.somewhere.edu/login?url=https://a.org/p"
        assert si.save_profile({"mode": "openathens", "openathens": "somewhere.ac.uk"})["configured"] and si.via("https://a.org/").startswith("https://go.openathens.net/redirector/somewhere.ac.uk?url=")
        v = si.save_profile({"mode": "vpn"})
        assert v["configured"] and not v["signs_in"] and si.via("https://a.org/") == "https://a.org/"                              # on the VPN there is no address of its own
        for bad in ({"mode": "wifi"}, {"ezproxy": "http://insecure.edu/login?url="}, {"ezproxy": "https://ezproxy.edu/login"}, {"openathens": "https://x.ac.uk/"}, {"ill_email": "not an email"}, {"home": "ftp://x"}):
            try:
                si.save_profile(bad); raise AssertionError("accepted %s" % bad)
            except ValueError:
                pass
    def t_si_status():
        assert not si.status()["informs"]["signed_in"]
        si.record("informs", True); si.record("library", True); si.record("library", False)
        st = si.status()
        assert st["informs"]["signed_in"] and st["informs"]["age_hours"] < 0.1 and st["library"]["signed_in"] and not st["ais"]["signed_in"], st            # a failed attempt does not undo a good sign-in
        d = json.load(open(si.STATUS_FILE)); d["library"]["at"] = "2020-01-01T00:00:00+00:00"; json.dump(d, open(si.STATUS_FILE, "w"))
        st = si.status()
        assert not st["library"]["signed_in"] and st["library"]["expired"] and st["informs"]["signed_in"], st["library"]                                  # an old one is "expired", not forgotten
        si.record("nonsense", True); assert "nonsense" not in json.load(open(si.STATUS_FILE))
        si.forget("informs"); assert not si.status()["informs"]["signed_in"]
    def t_si_login_pages():
        assert brw.looks_like_login("https://login.openathens.net/auth/x") and brw.looks_like_login("https://login.microsoftonline.com/common") and brw.looks_like_login("https://idp.uni.edu/idp/profile/SAML2/Redirect/SSO")
        assert not brw.looks_like_login("https://research.ebsco.com/c/XXXXXX/search/results?q=DI+10.1000%2Fx") and not brw.looks_like_login("https://www.tandfonline.com/doi/full/10.1080/07421222.2026.1")
        si.save_profile({"name": "Uni", "mode": "openathens", "openathens": "uni.ac.uk", "home": "https://lib.uni.ac.uk"})
        pages = brw.login_pages()
        assert pages[0][0].startswith("Uni") and "go.openathens.net" in pages[0][1] and len(pages) == len(brw.PUBLISHER_LOGINS) + 1 and not any("myuni" in u for _, u in pages)
    check("library access: a mode per way of getting in, old files understood, bad addresses refused, notes kept", t_si_profile)
    check("sign-in status: a sign-in counts for as long as it normally lasts, then says expired", t_si_status)
    check("sign-in pages: your library first, then the publishers' own institution pages (no university built in)", t_si_login_pages)
    def t_celebrate():
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            pg = b.new_page()
            pg.set_content("<html><body><h1>Publisher page</h1></body></html>")
            brw.celebrate(pg, "Signed in to INFORMS")                 # waits a few seconds so the person can read it: the page is still there afterwards
            t = pg.evaluate("[...document.querySelectorAll('div')].map(d => d.textContent).join('|')")
            svg = pg.evaluate("document.querySelectorAll('svg path').length")
            fixed = pg.evaluate("getComputedStyle(document.body.lastElementChild).position")
            b.close()
        assert "Signed in to INFORMS" in t and "closes by itself" in t and svg == 1 and fixed == "fixed", (t, svg, fixed)
    check("a successful sign-in shows a green tick message in the browser window itself, before it closes", t_celebrate)
    check("the app uses the shared Chromium cache for sign-ins and downloads (not a copy inside the bundle)", t_frozen_browser_path)
    check("updates: version order, including 1.10 after 1.9", t_up_versions)
    check("updates: the feed is read strictly (version, checksum, https or loopback only, no file:// or http:// elsewhere)", t_up_feed)
    check("updates: a newer version is announced with notes and the history since yours (older releases are left out), a skipped one is hidden until a newer one comes, an equal one is no news", t_up_check)
    check("updates: when the feed cannot be reached the last answer is kept and the problem is said", t_up_failure)
    check("updates: choices are saved (automatic checks, the address must be https) and a test feed overrides them", t_up_settings)
    check("updates: a download with the wrong checksum is refused, and something that is not an app is not installed", t_up_download)
    check("updates: the helper waits for the old app to quit, swaps the new one in, and puts the old one back if that fails", t_up_swap)
    check("updates: make_appcast.py writes a feed the app accepts, with the last release as history", t_up_appcast_script)
    feed_srv.shutdown()
    os.environ.pop("JW_VERSION", None); os.environ.pop("JW_UPDATE_FEED", None)

    srv.shutdown(); shutil.rmtree(TMP, ignore_errors=True)
    print("\n%d failed" % len(FAILS) if FAILS else "\nall passed")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
