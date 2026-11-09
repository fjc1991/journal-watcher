"""Offline tests for the reader's backend: notes store, AI layer (against a fake OpenAI-compatible server), PDF blocks,
embedding search and the Ask pipeline. No network, no API key, no paid calls, and the real library is never touched.

    .venv/bin/python .agents/skills/ais-journal-watcher/scripts/test_reader.py
"""
import base64
import io
import json
import os
import re
import shutil
import stat
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
os.environ["JW_NO_KEYCHAIN"] = "1"      # the tests never touch the Keychain of whoever runs them; the Keychain tests start their own processes with a fake store or a temporary keychain
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
import support as sp  # noqa: E402
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
        fail = getattr(Fake, "http_error", None)
        hit = fail(body) if fail else None           # (status, message): the provider refuses the request, as OpenRouter does for a model without the parameters asked for
        if hit:
            data = json.dumps({"error": {"message": hit[1], "code": hit[0]}}).encode()
            self.send_response(hit[0]); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
        # chat completions, streamed
        if "stream_options" in body and getattr(Fake, "reject_stream_options", False):
            data = json.dumps({"error": {"message": "unknown field stream_options"}}).encode()
            self.send_response(400); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.send_header("Connection", "close"); self.end_headers()
        pieces = Fake.responder(body) if getattr(Fake, "responder", None) else getattr(Fake, "pieces", ["The sample has ", "120 firms (2)."])
        if getattr(Fake, "refusal", None):       # OpenAI's structured outputs: a refusal arrives in delta.refusal instead of the answer
            self.wfile.write(("data: " + json.dumps({"choices": [{"delta": {"refusal": Fake.refusal}, "finish_reason": "stop"}]}) + "\n\n").encode()); pieces = []
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


ATTACH_CHILD = r'''
import csv, json, os, sys, threading, queue, urllib.parse, urllib.request, urllib.error
sys.path.insert(0, @HERE@)
import paths
paths.seed_home()
import download as dl, pipeline as pl, importer, identity, deletion, folders as fo, authors as au
R = {k: open(v, "rb").read() for k, v in json.loads(@FILES@).items()}
ID_T, ID_D = @T@, @D@
def boom(*a, **k):
    raise AssertionError("a summary was asked for")
calls = []
pl.summarize = boom
pl._summarize_one = lambda run, p, emit: calls.append(p["id"])
def paper(**kw):
    p = {"id": "doi:" + ID_D, "doi": ID_D, "title": ID_T, "authors": ["Jane Doe", "Richard Roe"], "journal": "Management Science", "abbr": "MNSC", "pub_date": "2024-05",
         "publisher": "informs", "verdict": "download", "dl_status": "failed", "dl_note": "no PDF on any route", "manual_url": "https://doi.org/" + ID_D}
    p.update(kw)
    return p
def mkrun(papers, kind="picks", params=None):
    r = pl.Run(kind, params or {"label": "t"})
    for p in papers:
        r.add_paper(p)
    r.save(force=True)
    return r
IDX = os.path.join(dl.PAPERS, "_index.csv")
rows = lambda: list(csv.DictReader(open(IDX, newline="", encoding="utf-8"))) if os.path.exists(IDX) else []
pdfs = lambda: sorted(os.path.join(a, f) for a, _, fs in os.walk(dl.PAPERS) for f in fs if f.endswith(".pdf"))
staged = lambda: sorted(os.listdir(importer.STAGING)) if os.path.isdir(importer.STAGING) else []
def raises(fn, words):
    try:
        fn()
    except ValueError as e:
        assert words in str(e), (words, str(e))
        return
    raise AssertionError("no error: " + words)

# (1) the right PDF: filed like a download (its name, an index row 'ok' "added by you"), the run saved, no summary
P = paper(); run = mkrun([P])
r = pl.attach_pdf(run, P["id"], R["right"])
assert r["filed"] and not r["existing"] and os.path.join(dl.ROOT, r["file"]) == dl.out_path(dict(P)), r
row = [x for x in rows() if x["file"] == r["file"]]
assert row and row[-1]["status"] == "ok" and row[-1]["note"].startswith("added by you"), row
p = run.papers[P["id"]]
assert p["dl_status"] == "ok" and p["file"] == r["file"] and p["summary_by_hand"] is True and r["paper"]["dl_status"] == "ok" and not r["unverified"], p
saved = json.load(open(os.path.join(pl.RUNS, run.id + ".json")))
assert [x for x in saved["papers"] if x["id"] == P["id"]][0]["dl_status"] == "ok", "the run is saved"
assert open(os.path.join(dl.ROOT, r["file"]), "rb").read() == R["right"]
FIRST = r["file"]
# (2) a second time on the same paper
raises(lambda: pl.attach_pdf(run, P["id"], R["right"]), "already in your library")
# (3) the same paper in another run: the copy you have (nothing written twice, nothing checked, nothing kept aside)
n_rows, n_pdfs = len(rows()), len(pdfs())
run2 = mkrun([paper()])
r = pl.attach_pdf(run2, P["id"], R["wrong"])
assert r["filed"] and r["existing"] and r["file"] == FIRST and len(rows()) == n_rows and len(pdfs()) == n_pdfs and not staged(), (r, staged())
assert run2.papers[P["id"]]["file"] == FIRST and run2.papers[P["id"]]["dl_status"] == "ok"
# (4) a PDF of another paper: kept aside, nothing filed; it says what the PDF seems to be and why
Q = paper(id="doi:10.1287/mnsc.2024.0002", doi="10.1287/mnsc.2024.0002", dl_status="wrong_paper")
O = paper(id="doi:10.1/other", doi="10.1/other", title="Another paper entirely, about hotels")
run3 = mkrun([Q, O])
n_rows, n_pdfs = len(rows()), len(pdfs())
r = pl.attach_pdf(run3, Q["id"], R["wrong"])
assert not r["filed"] and os.path.exists(os.path.join(importer.STAGING, r["token"] + ".pdf")) and r["seems"] == "Two-stage differences in differences", r
assert r["why"] and not r["why"].startswith("this PDF is titled") and r["title"] == ID_T, r
assert len(rows()) == n_rows and len(pdfs()) == n_pdfs and run3.papers[Q["id"]]["dl_status"] == "wrong_paper"
tok = r["token"]
# (5) "It is the right paper": for that paper only (not another paper's card, not a Library import's PDF); filed, and Check my PDFs leaves it alone
raises(lambda: pl.attach_keep(run3, O["id"], tok), "not checked against this paper")
lib_tok = importer._stage(R["wrong"])
raises(lambda: pl.attach_keep(run3, Q["id"], lib_tok), "not checked against this paper")
importer.discard([lib_tok])
r = pl.attach_keep(run3, Q["id"], tok)
assert r["filed"] and r["kept"] and "you said it is the right paper" in run3.papers[Q["id"]]["dl_note"], r
assert [x["verdict"] for x in identity.audit(use_kept=True) if x["file"] == r["file"]] == ["kept"]
assert not [f for f in staged() if f.startswith(tok)], staged()
raises(lambda: pl.attach_keep(run3, Q["id"], tok), "already in your library")
# a staged PDF is taken once; when filing it fails, it waits again
t2 = importer._stage(b"x", bind={"a": 1})
assert importer.take_staged(t2, lambda d: d, {"a": 1}) == b"x"
raises(lambda: importer.take_staged(t2, lambda d: d, {"a": 1}), "no longer waiting")
t3 = importer._stage(b"y", bind={"a": 1})
def bad(d):
    raise ValueError("nope")
raises(lambda: importer.take_staged(t3, bad, {"a": 1}), "nope")
assert importer.take_staged(t3, lambda d: d, {"a": 1}) == b"y"
t4 = importer._stage(b"z", bind={"a": 1}); took = []
def slow(d):
    import time as _t; _t.sleep(0.3); took.append(d); return d
def take4():
    try:
        importer.take_staged(t4, slow, {"a": 1})
    except ValueError:
        pass
ths = [threading.Thread(target=take4) for _ in range(2)]
[x.start() for x in ths]; [x.join() for x in ths]
assert took == [b"z"], "two presses at once: the PDF is taken once"
# (6) Cancel: the kept-aside PDF is gone
Q2 = paper(id="doi:10.1287/mnsc.2024.0003", doi="10.1287/mnsc.2024.0003"); run3.add_paper(Q2)
tok = pl.attach_pdf(run3, Q2["id"], R["wrong"])["token"]
importer.discard([tok])
assert not [f for f in staged() if f.startswith(tok)], staged()
raises(lambda: pl.attach_keep(run3, Q2["id"], tok), "no longer waiting")
# (7) a scan cannot be checked: filed at once, and the card says so
Q3 = paper(id="doi:10.1287/mnsc.2024.0004", doi="10.1287/mnsc.2024.0004"); run3.add_paper(Q3)
r = pl.attach_pdf(run3, Q3["id"], R["scan"])
assert r["filed"] and r["unverified"] and "identity not checked" in run3.papers[Q3["id"]]["dl_note"], r
# (8) not a PDF, half a PDF, a paper being downloaded, a paper not in the run; a short PDF is fine
Q4 = paper(id="doi:10.1287/mnsc.2024.0005", doi="10.1287/mnsc.2024.0005"); run3.add_paper(Q4)
raises(lambda: pl.attach_pdf(run3, Q4["id"], b"not a pdf"), "complete PDF")
raises(lambda: pl.attach_pdf(run3, Q4["id"], R["right"][:len(R["right"]) // 2]), "complete PDF")
run3.papers[Q4["id"]]["dl_status"] = "downloading"
raises(lambda: pl.attach_pdf(run3, Q4["id"], R["small"]), "being downloaded")
run3.papers[Q4["id"]]["dl_status"] = "failed"
raises(lambda: pl.attach_pdf(run3, "doi:nope", R["right"]), "not in this run")
assert not dl.is_pdf(R["small"])
r = pl.attach_pdf(run3, Q4["id"], R["small"])
assert r["filed"], r
# (9) a paper you deleted: adding it yourself brings it back
deletion.delete_item(r["file"]); pl.mark_downloaded(run3)
assert run3.papers[Q4["id"]]["dl_status"] == "deleted" and dl.index_key(Q4) in deletion.deleted_keys()
r = pl.attach_pdf(run3, Q4["id"], R["small"])
assert r["filed"] and r["was_deleted"] and dl.index_key(Q4) not in deletion.deleted_keys(), r
# (10) an author run: into the author's folder (and again after you deleted it and add it back: you chose it)
fid, _ = au.folder_for(["A5000000333"], "Ann Lee", "")      # (an author record: what it filed once is not filed again by itself)
A = paper(id="doi:10.7777/au.3", doi="10.7777/au.3", title="Platforms and the ties that bind them", folder=fid)
runA = mkrun([A], kind="author", params={"label": "Ann Lee"})
t = pl.attach_pdf(runA, A["id"], R["wrong"]); assert not t["filed"], t
r = pl.attach_keep(runA, A["id"], t["token"])
assert dl.index_key(A) in fo.get(fid)["papers"] and r["folder"] == "Ann Lee", (r, fo.get(fid))
deletion.delete_item(r["file"]); pl.mark_downloaded(runA)
assert dl.index_key(A) not in fo.get(fid)["papers"]
t = pl.attach_pdf(runA, A["id"], R["wrong"]); r = pl.attach_keep(runA, A["id"], t["token"])
assert dl.index_key(A) in fo.get(fid)["papers"] and r["folder"] == "Ann Lee" and r["was_deleted"], (r, fo.get(fid))
# (11) a followed author's work in the weekly run: dealt with (not fetched again next week), also for a co-author you follow, and in both folders
fidF, _ = au.folder_for(["A5000000111"], "Bo Chen", "MIT"); au.set_follow(fidF, True)
fid2, _ = au.folder_for(["A5000000222"], "Cy Dee", "")
W = paper(id="doi:10.7777/w.42", doi="10.7777/w.42", title="Followed work about ties", folder=fidF, openalex="https://openalex.org/W42", also_folders=[[fid2, "https://openalex.org/W43"]], summary_by_hand=True)
runW = mkrun([W], kind="weekly")
t = pl.attach_pdf(runW, W["id"], R["wrong"]); r = pl.attach_keep(runW, W["id"], t["token"])
recs = {x["folder"]: x for x in au.load()["authors"]}
assert "W42" in recs[fidF].get("follow_known", []) and "W43" in recs[fid2].get("follow_known", []), recs
assert dl.index_key(W) in fo.get(fidF)["papers"] and dl.index_key(W) in fo.get(fid2)["papers"]
# a followed work you already had, filed without its DOI (keyed by its title): both folders get the key your copy has
TT = "Followed work filed by its title"
had = os.path.join(dl.PAPERS, "2024-05", "x_title_only.pdf"); os.makedirs(os.path.dirname(had), exist_ok=True); open(had, "wb").write(R["small"])
dl.save_index({"doi": "", "title": TT, "journal": "J", "abbr": "J"}, os.path.relpath(had, dl.ROOT), "ok", "added by you")
W2 = paper(id="doi:10.7777/w.44", doi="10.7777/w.44", title=TT, folder=fidF, openalex="https://openalex.org/W44", also_folders=[[fid2, "https://openalex.org/W45"]])
runW.add_paper(W2)
r = pl.attach_pdf(runW, W2["id"], R["wrong"])
assert r["existing"] and TT.lower() in fo.get(fidF)["papers"] and TT.lower() in fo.get(fid2)["papers"], (r, fo.get(fid2))

# (12) a download batch with a paper whose PDF you added: not downloaded, not summarised
class _Br:
    def __init__(self, on_challenge=None, ask=False, **k):
        self.ch = on_challenge
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def open_check(self, host, stay=False):
        self.ch(host, "passed"); return True
got_dl = []
def fake_dl(p, env, browser=None, dry=False):
    got_dl.append(p["id"]); return False, None, "no PDF"
dl.download_paper, dl.open_browser = fake_dl, _Br
calls.clear()
pl.download_and_summarize(run, [P["id"]], lambda *a: None, threading.Event(), sleep=lambda d: None, clock=lambda: 0.0)
assert not got_dl and not calls and run.papers[P["id"]]["dl_status"] == "ok", (got_dl, calls)
# (13) a paper set aside for a human check gets its PDF from you while another site's paper downloads; the check is then opened: it stays as it is
SD = "www.sciencedirect.com"; ev_log, hit = [], {}; clk = [0.0]
E1 = paper(id="doi:10.1016/e1", doi="10.1016/e1", title="Elsevier paper one", publisher="elsevier")
E2 = paper(id="doi:10.1287/mnsc.2024.0006", doi="10.1287/mnsc.2024.0006", publisher="elsevier")
W1 = paper(id="doi:10.1002/w1", doi="10.1002/w1", title="Wiley paper one", publisher="wiley")
runC = mkrun([E1, E2, W1]); checks = queue.Queue()
def fake_dl2(p, env, browser=None, dry=False):
    if p["publisher"] == "elsevier":
        browser.ch(SD, "pending"); return False, None, "challenge:%s" % SD
    assert runC.papers[E2["id"]]["dl_status"] == "needs_check"
    hit["attach"] = pl.attach_pdf(runC, E2["id"], R["right"]); ev_log.append(("ATTACH", {}))
    checks.put(SD)                                   # and you press "Open the check"
    f = dl.out_path(dict(p)); open(f, "wb").write(R["small"]); return True, f, "browser"
def tick(d):
    clk[0] += max(d, 1.0)
dl.download_paper = fake_dl2
pl.download_and_summarize(runC, [E1["id"], E2["id"], W1["id"]], lambda e, d: ev_log.append((e, d)), threading.Event(), sleep=tick, clock=lambda: clk[0], checks=checks, summarize=False)
assert hit["attach"]["filed"], hit
assert runC.papers[E2["id"]]["dl_status"] == "ok" and runC.papers[E1["id"]]["dl_status"] == "needs_check" and runC.papers[W1["id"]]["dl_status"] == "ok", {k: v["dl_status"] for k, v in runC.papers.items()}
i = ev_log.index(("ATTACH", {}))
assert not [d for e, d in ev_log[i:] if e == "download" and d.get("id") == E2["id"]], [d for e, d in ev_log[i:] if e == "download"]
prog = [d for e, d in ev_log if e == "progress" and d.get("stage") == "downloading"]
assert prog[-1]["done"] == prog[-1]["total"] == 3, prog[-1]
# ... and when the check is never opened (the batch ends with the paper still set aside): it stays as you left it
ev_log.clear(); hit.clear(); clk[0] = 0.0
E3 = paper(id="doi:10.1016/e3", doi="10.1016/e3", title="Elsevier paper three", publisher="elsevier")
E4 = paper(id="doi:10.1287/mnsc.2024.0013", doi="10.1287/mnsc.2024.0013", publisher="elsevier")
W3 = paper(id="doi:10.1002/w3", doi="10.1002/w3", title="Wiley paper three", publisher="wiley")
runC = mkrun([E3, E4, W3]); E2 = E4
def fake_dl2b(p, env, browser=None, dry=False):
    if p["publisher"] == "elsevier":
        browser.ch(SD, "pending"); return False, None, "challenge:%s" % SD
    hit["attach"] = pl.attach_pdf(runC, E4["id"], R["right"]); ev_log.append(("ATTACH", {}))
    f = dl.out_path(dict(p)); open(f, "wb").write(R["small"]); return True, f, "browser"
dl.download_paper = fake_dl2b
pl.download_and_summarize(runC, [E3["id"], E4["id"], W3["id"]], lambda e, d: ev_log.append((e, d)), threading.Event(), sleep=tick, clock=lambda: clk[0], checks=queue.Queue(), summarize=False)
assert hit["attach"]["filed"] and runC.papers[E4["id"]]["dl_status"] == "ok" and runC.papers[E3["id"]]["dl_status"] == "needs_check", {k: v["dl_status"] for k, v in runC.papers.items()}
i = ev_log.index(("ATTACH", {}))
assert not [d for e, d in ev_log[i:] if e == "download" and d.get("id") == E4["id"]]
assert [m for e, m in ev_log if e == "log" and "1 paper(s) still need a human check" in m.get("msg", "")], "only the paper still waiting is counted"
# (14) the batch is downloading the paper: you are told to wait; the batch reaches a paper whose PDF you added during its wait: it is not downloaded, your file stays
B1 = paper(id="doi:10.1287/mnsc.2024.0007", doi="10.1287/mnsc.2024.0007"); runD = mkrun([B1])
def fake_dl3(p, env, browser=None, dry=False):
    raises(lambda: pl.attach_pdf(runD, B1["id"], R["right"]), "being downloaded"); return False, None, "no PDF"
dl.download_paper, dl.open_browser = fake_dl3, _Br
pl.download_and_summarize(runD, [B1["id"]], lambda *a: None, threading.Event(), sleep=lambda d: None, clock=lambda: 0.0)
assert runD.papers[B1["id"]]["dl_status"] == "failed"
B2 = paper(id="doi:10.1/b2", doi="10.1/b2", title="Informs paper two"); B3 = paper(id="doi:10.1287/mnsc.2024.0008", doi="10.1287/mnsc.2024.0008")
runE = mkrun([B2, B3]); seen_dl, clk[0] = [], 0.0
def slp(d):
    clk[0] += max(d, 1.0)
    if "b3" not in hit:
        hit["b3"] = pl.attach_pdf(runE, B3["id"], R["right"])
def fake_dl4(p, env, browser=None, dry=False):
    seen_dl.append(p["id"]); return False, None, "no PDF"
dl.download_paper = fake_dl4
pl.download_and_summarize(runE, [B2["id"], B3["id"]], lambda *a: None, threading.Event(), sleep=slp, clock=lambda: clk[0], summarize=False)
assert seen_dl == [B2["id"]] and hit["b3"]["filed"] and runE.papers[B3["id"]]["dl_status"] == "ok", (seen_dl, runE.papers[B3["id"]])
assert open(os.path.join(dl.ROOT, runE.papers[B3["id"]]["file"]), "rb").read() == R["right"]
assert not calls
# (14b) the same paper waits in a running download AND sits in another run you opened: you add its PDF on that other run's card; the running download takes your copy
X1 = paper(id="doi:10.1/x1", doi="10.1/x1", title="Informs paper x one"); X = paper(id="doi:10.1287/mnsc.2024.0014", doi="10.1287/mnsc.2024.0014")
runP = mkrun([X1, X]); runR = mkrun([dict(X)]); seen_dl, clk[0] = [], 0.0
def slp2(d):
    clk[0] += max(d, 1.0)
    if "x" not in hit:
        hit["x"] = pl.attach_pdf(runR, X["id"], R["right"])            # (a run read from its file: runP's copy of the paper knows nothing of it)
dl.download_paper = fake_dl4
pl.download_and_summarize(runP, [X1["id"], X["id"]], lambda *a: None, threading.Event(), sleep=slp2, clock=lambda: clk[0])
px = runP.papers[X["id"]]
assert seen_dl == [X1["id"]] and hit["x"]["filed"] and px["dl_status"] == "ok" and px["file"] == hit["x"]["file"] and px["summary_by_hand"] and not calls, (seen_dl, px)
assert open(os.path.join(dl.ROOT, px["file"]), "rb").read() == R["right"]
dl.save_index(dict(X), "", "failed", "a download that failed meanwhile")       # (a failed download never takes a paper out of the library)
assert [x["status"] for x in rows() if x["doi"] == X["doi"]] == ["ok"], [x for x in rows() if x["doi"] == X["doi"]]
# (14c) a paper whose copy in the library is keyed by its title: when the download batch reaches it, it is not filed again under its DOI
fidK, _ = au.folder_for(["A5000000444"], "Di Ek", "")
TK = "A paper you had without its DOI"
hk = os.path.join(dl.PAPERS, "2024-05", "x_tk.pdf"); open(hk, "wb").write(R["small"])
dl.save_index({"doi": "", "title": TK, "journal": "J", "abbr": "J"}, os.path.relpath(hk, dl.ROOT), "ok", "added by you")
K = paper(id="doi:10.7777/k.1", doi="10.7777/k.1", title=TK, folder=fidK); runK = mkrun([K], kind="author", params={"label": "Di Ek"})
assert pl.attach_pdf(runK, K["id"], R["wrong"])["existing"]
dl.download_paper, dl.open_browser = fake_dl, _Br
pl.download_and_summarize(runK, [K["id"]], lambda *a: None, threading.Event(), sleep=lambda d: None, clock=lambda: 0.0)
assert fo.get(fidK)["papers"] == [TK.lower()], fo.get(fidK)["papers"]
# (14d) every paper that waited for a site's check got its PDF from you: pressing "Open the check" opens nothing, and the page is told it no longer waits
opened = []
class _BrB(_Br):
    def open_check(self, host, stay=False):
        opened.append(host); self.ch(host, "passed"); return True
E5 = paper(id="doi:10.1287/mnsc.2024.0015", doi="10.1287/mnsc.2024.0015", publisher="elsevier")
W5 = paper(id="doi:10.1002/w5", doi="10.1002/w5", title="Wiley paper five", publisher="wiley")
runF = mkrun([E5, W5]); checks = queue.Queue(); ev_log.clear(); clk[0] = 0.0
def fake_dl5(p, env, browser=None, dry=False):
    if p["publisher"] == "elsevier":
        browser.ch(SD, "pending"); return False, None, "challenge:%s" % SD
    pl.attach_pdf(runF, E5["id"], R["right"]); checks.put(SD)
    f = dl.out_path(dict(p)); open(f, "wb").write(R["small"]); return True, f, "browser"
dl.download_paper, dl.open_browser = fake_dl5, _BrB
pl.download_and_summarize(runF, [E5["id"], W5["id"]], lambda e, d: ev_log.append((e, d)), threading.Event(), sleep=tick, clock=lambda: clk[0], checks=checks, summarize=False)
assert not opened and runF.papers[E5["id"]]["dl_status"] == "ok" and any(e == "challenge" and d.get("state") == "cleared" and d.get("host") == SD for e, d in ev_log), (opened, [d for e, d in ev_log if e == "challenge"])
# (14f) a paper with the same title as one you have WITHOUT a DOI is another paper: it is downloaded, not pointed at your file
he = os.path.join(dl.PAPERS, "2024-05", "x_editorial.pdf"); open(he, "wb").write(R["small"])
dl.save_index({"doi": "", "title": "Editorial", "journal": "MIS Quarterly", "abbr": "MISQ"}, os.path.relpath(he, dl.ROOT), "ok", "added by you")
ED = paper(id="doi:10.1287/isre.ed", doi="10.1287/isre.ed", title="Editorial"); runG = mkrun([ED]); seen_dl.clear()
dl.download_paper, dl.open_browser = fake_dl4, _Br
pl.download_and_summarize(runG, [ED["id"]], lambda *a: None, threading.Event(), sleep=lambda d: None, clock=lambda: 0.0, summarize=False)
assert seen_dl == [ED["id"]] and runG.papers[ED["id"]].get("file") != os.path.relpath(he, dl.ROOT), (seen_dl, runG.papers[ED["id"]])
# (14g) the download fails, but the paper's PDF came meanwhile on another run's card: the card shows it as in the library, not as failed
Y = paper(id="doi:10.1287/mnsc.2024.0016", doi="10.1287/mnsc.2024.0016"); runY = mkrun([Y]); runY2 = mkrun([dict(Y)])
def fake_dl6(p, env, browser=None, dry=False):
    pl.attach_pdf(runY2, Y["id"], R["right"]); return False, None, "no PDF on any route"
dl.download_paper = fake_dl6
pl.download_and_summarize(runY, [Y["id"]], lambda *a: None, threading.Event(), sleep=lambda d: None, clock=lambda: 0.0, summarize=False)
assert runY.papers[Y["id"]]["dl_status"] == "ok" and runY.papers[Y["id"]]["file"] == runY2.papers[Y["id"]]["file"], runY.papers[Y["id"]]
# (14h) the papers that waited for a site's check got their PDFs on another run's cards: pressing "Open the check" opens nothing
E6 = paper(id="doi:10.1287/mnsc.2024.0017", doi="10.1287/mnsc.2024.0017", publisher="elsevier")
W6 = paper(id="doi:10.1002/w6", doi="10.1002/w6", title="Wiley paper six", publisher="wiley")
runH = mkrun([E6, W6]); runH2 = mkrun([dict(E6)]); checks = queue.Queue(); ev_log.clear(); clk[0] = 0.0; opened.clear()
def fake_dl7(p, env, browser=None, dry=False):
    if p["publisher"] == "elsevier":
        browser.ch(SD, "pending"); return False, None, "challenge:%s" % SD
    pl.attach_pdf(runH2, E6["id"], R["right"]); checks.put(SD)
    f = dl.out_path(dict(p)); open(f, "wb").write(R["small"]); return True, f, "browser"
dl.download_paper, dl.open_browser = fake_dl7, _BrB
pl.download_and_summarize(runH, [E6["id"], W6["id"]], lambda e, d: ev_log.append((e, d)), threading.Event(), sleep=tick, clock=lambda: clk[0], checks=checks, summarize=False)
assert not opened and runH.papers[E6["id"]]["dl_status"] == "ok" and any(e == "download" and d.get("id") == E6["id"] and d.get("dl_status") == "ok" for e, d in ev_log), (opened, runH.papers[E6["id"]]["dl_status"])
# (14i) a followed work's co-author folder gets the key its library copy has (a copy keyed by its title)
runJ = pl.Run("weekly", {}); runJ.add_paper(paper(id="doi:10.7777/j.1", doi="10.7777/j.1", title="Jay", folder=fidF, openalex="https://openalex.org/W50", also_folders=[[fid2, "https://openalex.org/W51"]], dl_status="ok", lib_key="a title key"))
pl.follow_settle(runJ, ["doi:10.7777/j.1"])
assert "a title key" in fo.get(fid2)["papers"], fo.get(fid2)["papers"]
# (14e) Check my PDFs' "Remove" writes the index under the same lock as a download
keep_rm = identity._remove
identity._remove = lambda f: dl.INDEX_LOCK._is_owned()
assert identity.remove("anything") is True
identity._remove = keep_rm

# (14j) the sign-in browser does not start: said once in plain words; one paper's error does not stop the others; a full disk stops the batch and says so
class _NoBr:
    def __init__(self, **k):
        dl.BROWSER_ERROR["why"] = "Chromium is missing"
    def __enter__(self):
        return None
    def __exit__(self, *a):
        return False
N1 = paper(id="doi:10.1/n1", doi="10.1/n1", title="Paper n one"); N2 = paper(id="doi:10.1/n2", doi="10.1/n2", title="Paper n two", publisher="wiley"); N3 = paper(id="doi:10.1/n3", doi="10.1/n3", title="Paper n three", publisher="acm")
runN = mkrun([N1, N2, N3]); ev_log.clear(); tried = []
def fake_dl8(p, env, browser=None, dry=False):
    tried.append(p["id"])
    if p["id"] == N1["id"]:
        raise RuntimeError("the index could not be written")
    return False, None, "no PDF"
dl.download_paper, dl.open_browser = fake_dl8, _NoBr
pl.download_and_summarize(runN, [N1["id"], N2["id"], N3["id"]], lambda e, d: ev_log.append((e, d)), threading.Event(), sleep=lambda d: None, clock=lambda: 0.0, summarize=False)
logs = [d.get("msg", "") for e, d in ev_log if e == "log"]
assert sum("sign-in browser could not start (Chromium is missing)" in m for m in logs) == 1, logs
assert sorted(tried) == sorted([N1["id"], N2["id"], N3["id"]]) and runN.papers[N1["id"]]["dl_status"] == "failed" and "could not be saved" in runN.papers[N1["id"]]["dl_note"], (tried, runN.papers[N1["id"]])
runN2 = mkrun([dict(N1, dl_status=None), dict(N2, dl_status=None), dict(N3, dl_status=None)]); tried.clear(); ev_log.clear()
def fake_dl9(p, env, browser=None, dry=False):
    tried.append(p["id"]); raise OSError(28, "No space left on device")
dl.download_paper, dl.open_browser = fake_dl9, _Br
stop9 = threading.Event()
pl.download_and_summarize(runN2, [N1["id"], N2["id"], N3["id"]], lambda e, d: ev_log.append((e, d)), stop9, sleep=lambda d: None, clock=lambda: 0.0, summarize=False)
assert len(tried) == 1 and stop9.is_set() and any("disk is full" in d.get("msg", "") for e, d in ev_log if e == "log"), (tried, [d for e, d in ev_log if e == "log"])
assert not any(v.get("dl_status") == "downloading" for v in runN2.papers.values()), "no paper is left 'downloading'"
# (14k) what still waits is written down while the batch runs (a quit leaves it in the run's file, and the run offers to go on); a batch that ends leaves no mark
runQ = mkrun([dict(N1, dl_status=None), dict(N2, dl_status=None), dict(N3, dl_status=None)]); on_disk = []
def fake_dl10(p, env, browser=None, dry=False):
    on_disk.append(sorted(x["id"] for x in json.load(open(os.path.join(pl.RUNS, runQ.id + ".json")))["papers"] if x.get("dl_queued")))
    return False, None, "no PDF"
dl.download_paper, dl.open_browser = fake_dl10, _Br
pl.download_and_summarize(runQ, [N1["id"], N2["id"], N3["id"]], lambda *a: None, threading.Event(), sleep=lambda d: None, clock=lambda: 0.0, summarize=False)
assert on_disk[0] == sorted([N1["id"], N2["id"], N3["id"]]) and all(N3["id"] in x for x in on_disk), on_disk        # (the file is saved at most every two seconds: it may also name papers already tried, which a retry does no harm to)
assert not any(x.get("dl_queued") for x in json.load(open(os.path.join(pl.RUNS, runQ.id + ".json")))["papers"]) and "dl_queued" in pl.slim({"dl_queued": True}), "a finished batch leaves no mark"
# (15) the index and a run's file, written by several threads at once: nothing lost, nothing half-written
def many(tag):
    for i in range(150):
        dl.save_index({"doi": "10.9/%s%d" % (tag, i), "title": "t"}, "", "failed")
ths = [threading.Thread(target=many, args=(x,)) for x in "ab"]
[x.start() for x in ths]; [x.join() for x in ths]
ks = {x["doi"] for x in rows()}
assert all("10.9/%s%d" % (x, i) in ks for x in "ab" for i in range(150)), len(ks)
errs = []
def saver():
    try:
        for _ in range(80):
            runC.save(force=True)
    except Exception as e:  # noqa: BLE001
        errs.append(repr(e))
ths = [threading.Thread(target=saver) for _ in range(3)]
[x.start() for x in ths]; [x.join() for x in ths]
assert not errs, errs

# --- the server: /api/run/attach (the raw PDF), /api/run/attach/keep, the guard; a run read from its file; two at once
import importlib.util
from http.server import ThreadingHTTPServer
spec = importlib.util.spec_from_file_location("jw_server", os.path.join(paths.DASHBOARD, "server.py")); sv = importlib.util.module_from_spec(spec); spec.loader.exec_module(sv)
srv = ThreadingHTTPServer(("127.0.0.1", 0), sv.Handler); threading.Thread(target=srv.serve_forever, daemon=True).start()
base = "http://127.0.0.1:%d" % srv.server_address[1]
hq = sv.HUB.subscribe()
def post(path, data, headers):
    try:
        with urllib.request.urlopen(urllib.request.Request(base + path, data, headers)) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")
def drain():
    out = []
    while True:
        try:
            out.append(hq.get_nowait())
        except queue.Empty:
            return out
S1 = paper(id="doi:10.1287/mnsc.2024.0009", doi="10.1287/mnsc.2024.0009", dl_status="wrong_paper")
S2 = paper(id="doi:10.1287/mnsc.2024.0010", doi="10.1287/mnsc.2024.0010", title="A different paper about platforms and hotels")
S3 = paper(id="doi:10.1287/mnsc.2024.0011", doi="10.1287/mnsc.2024.0011"); S4 = paper(id="doi:10.1287/mnsc.2024.0012", doi="10.1287/mnsc.2024.0012")
runS = mkrun([S1, S2, S3, S4]); sv.JOBS.run = None
hdr = lambda pid, rid=None: {"Content-Type": "application/pdf", "X-Run": urllib.parse.quote(rid or runS.id), "X-Paper": urllib.parse.quote(pid), "X-Filename": "x.pdf"}
drain()
c, j = post("/api/run/attach", R["right"], hdr(S1["id"]))
assert c == 200 and j["ok"] and j["filed"] and j["paper"]["dl_status"] == "ok", (c, j)
evs = drain()
assert any(m.startswith("event: download") and '"dl_status": "ok"' in m and S1["id"] in m for m in evs) and any(m.startswith("event: log") and "was added to the library" in m for m in evs), evs
assert pl.Run.load(runS.id).papers[S1["id"]]["dl_status"] == "ok", "a run read from its file is saved"
c, j = post("/api/run/attach", R["wrong"], hdr(S2["id"]))
assert c == 200 and j["filed"] is False and j["token"] and j["seems"] and j["why"], (c, j)
assert not [m for m in drain() if m.startswith("event: download")]
jh = {"Content-Type": "application/json"}
c, j2 = post("/api/run/attach/keep", json.dumps({"run_id": runS.id, "id": S2["id"], "token": j["token"]}).encode(), jh)
assert c == 200 and j2["filed"] and j2["kept"], (c, j2)
c, j3 = post("/api/run/attach/keep", json.dumps({"run_id": runS.id, "id": S2["id"], "token": j["token"]}).encode(), jh)
assert c == 400, (c, j3)
c, j = post("/api/run/attach", b"", hdr(S3["id"])); assert c == 400 and "150 MB" in j["error"], (c, j)
c, j = post("/api/run/attach", R["right"], hdr(S3["id"], "nope")); assert c == 404, (c, j)
c, j = post("/api/run/attach", b"hello", hdr(S3["id"])); assert c == 400 and "complete PDF" in j["error"], (c, j)
c, j = post("/api/run/attach", R["right"], dict(hdr(S3["id"]), Origin="https://evil.example")); assert c == 403, (c, j)
res = {}
def at(pid):
    res[pid] = post("/api/run/attach", R["right"], hdr(pid))
ths = [threading.Thread(target=at, args=(x["id"],)) for x in (S3, S4)]
[x.start() for x in ths]; [x.join() for x in ths]
assert all(v[0] == 200 and v[1]["filed"] for v in res.values()), res
back = pl.Run.load(runS.id).papers
assert back[S3["id"]]["dl_status"] == "ok" and back[S4["id"]]["dl_status"] == "ok", "two at once: both are saved"
# --- after the run: its papers into a folder you choose ("Add to a folder…" on the run's page)
fidR = fo.create("Labor Market Papers")["id"]
A1 = paper(id="doi:10.1287/mnsc.2024.0020", doi="10.1287/mnsc.2024.0020")                                   # (filed below: in the library)
A2 = paper(id="doi:10.1287/mnsc.2024.0021", doi="10.1287/mnsc.2024.0021", dl_status="failed")               # still to download
A3 = paper(id="doi:10.1/skip", doi="10.1/skip", title="Skipped one", verdict="skip", dl_status=None)       # not for download: never waits for the folder
A4 = paper(id="doi:10.1/au.9", doi="10.1/au.9", title="An author's work", folder=fidK, dl_status="failed")  # waits for its author's folder
runT = mkrun([A1, A2, A3, A4], params={"label": "this folder"})
assert pl.attach_pdf(runT, A1["id"], R["right"])["filed"]
r = pl.run_to_folder(runT, fidR)
assert (r["added"], r["already"], r["pending"], r["kept"], r["folder"]) == (1, 0, 1, 1, "Labor Market Papers"), r
assert dl.index_key(A1) in fo.get(fidR)["papers"] and runT.papers[A2["id"]].get("folder") == fidR and not runT.papers[A3["id"]].get("folder") and runT.papers[A4["id"]]["folder"] == fidK
assert runT.title() == "Picks · Labor Market Papers" and runT.params["folder"] == fidR, runT.title()
assert pl.Run.load(runT.id).papers[A2["id"]].get("folder") == fidR and pl.Run.load(runT.id).title() == "Picks · Labor Market Papers", "saved"
assert pl.attach_pdf(runT, A2["id"], R["right"])["folder"] == "Labor Market Papers" and dl.index_key(A2) in fo.get(fidR)["papers"], "a paper still to come goes in when it arrives"
r = pl.run_to_folder(runT, fidR)
assert (r["added"], r["already"], r["pending"]) == (0, 2, 0), r
fo.remove(fidR, [dl.index_key(A1)])
assert pl.run_to_folder(runT, fidR)["added"] == 1 and dl.index_key(A1) in fo.get(fidR)["papers"], "you chose it: a paper taken out goes back in"
runU = mkrun([paper(id="doi:10.1/u1", doi="10.1/u1", title="Imported one", dl_status="failed")], params={"label": "Imported: refs"})
pl.run_to_folder(runU, fidR, pending=False)
assert runU.title() == "Picks · Imported: refs" and not runU.papers["doi:10.1/u1"].get("folder"), "a named run keeps its name; without pending nothing waits"
try:
    pl.run_to_folder(runU, "no-such-folder"); raise AssertionError("no error")
except ValueError as e:
    assert "not there any more" in str(e)
srv = ThreadingHTTPServer(("127.0.0.1", 0), sv.Handler); threading.Thread(target=srv.serve_forever, daemon=True).start()
base = "http://127.0.0.1:%d" % srv.server_address[1]
c, j = post("/api/run/folder", json.dumps({"run_id": runU.id, "new_name": "Picked later", "pending": True}).encode(), jh)
assert c == 200 and j["folder"] == "Picked later" and any(f_["name"] == "Picked later" for f_ in j["folders"]) and pl.Run.load(runU.id).papers["doi:10.1/u1"].get("folder"), (c, j)
c, j = post("/api/run/folder", json.dumps({"run_id": runU.id, "new_name": "Picked later"}).encode(), jh); assert c == 400 and "exists" in j["error"], (c, j)
c, j = post("/api/run/folder", json.dumps({"run_id": "nope", "folder": fidR}).encode(), jh); assert c == 404, (c, j)
# a paper that waits for the folder and comes another way (another run, the Library's Add) is filed when the run is next looked at
V = paper(id="doi:10.1287/mnsc.2024.0022", doi="10.1287/mnsc.2024.0022"); runV = mkrun([V], params={"label": "this folder"})
assert pl.run_to_folder(runV, fidR)["pending"] == 1
hv = dl.out_path(dict(V)); open(hv, "wb").write(R["right"]); dl.save_index(dict(V), os.path.relpath(hv, dl.ROOT), "ok", "added by you")
runV2 = pl.Run.load(runV.id); pl.mark_downloaded(runV2)
assert runV2.papers[V["id"]]["dl_status"] == "ok" and dl.index_key(V) in fo.get(fidR)["papers"], fo.get(fidR)["papers"]
# a paper the running download has just brought in (its file is there, the index was read a moment before) is filed now, not left waiting
W9 = paper(id="doi:10.1/w9", doi="10.1/w9", title="Just arrived", dl_status="ok"); hw = os.path.join(dl.PAPERS, "2024-05", "x_w9.pdf"); open(hw, "wb").write(R["small"])
W9["file"] = os.path.relpath(hw, dl.ROOT); runW9 = mkrun([W9], params={"label": "this folder"})
r = pl.run_to_folder(runW9, fidR)
assert r["added"] == 1 and r["pending"] == 0 and "10.1/w9" in fo.get(fidR)["papers"], r
# without the papers still to come, a Discover run does not take the folder's name (its page would promise what will not happen)
runG2 = mkrun([paper(id="doi:10.1/g2", doi="10.1/g2", title="Waiting one", dl_status="failed")], params={"label": "this folder"})
pl.run_to_folder(runG2, fidR, pending=False)
assert runG2.title() == "Picks · this folder" and not runG2.params.get("folder"), runG2.params
# "Not in any folder" as the scope of the tools that take one (search in the PDFs, ask…)
nf_ = sv.Handler.scope_rows(None, {"folder": "~none"})
assert nf_ is not None and all(not r_.get("folders") for r_ in nf_), "no 'no such folder'"
# a new folder is made only once its run is found
n_f = len(fo.list_all())
c, j = post("/api/run/folder", json.dumps({"run_id": "nope", "new_name": "Never made"}).encode(), jh)
assert c == 404 and len(fo.list_all()) == n_f and not any(f_["name"] == "Never made" for f_ in fo.list_all()), (c, j)
# the Library: papers in no folder, newest first
import library_search as ls_
rows3 = [{"file": "a.pdf", "title": "A", "folders": [], "date_checked": "2026-11-01"}, {"file": "b.pdf", "title": "B", "folders": ["x"], "date_checked": "2026-11-05"},
         {"file": "c.pdf", "title": "C", "folders": [], "date_checked": "2026-11-07"}]
assert [x["file"] for x in ls_.search(rows3, folder="~none", sort="added")] == ["c.pdf", "a.pdf"]
assert [x["file"] for x in ls_.search(rows3, folder="x")] == ["b.pdf"]
srv.shutdown()
print("ALL OK")
'''


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

    def t_notes_unreadable():
        """A notes file that is there but cannot be read (half-synced, damaged) is not taken for 'no notes': the page is told, a save is refused (the next
        highlight used to replace the whole file), and a copy is kept beside it. The same for the tags file."""
        import glob
        np = ns.notes_path(pdf); good = open(np, encoding="utf-8").read()
        tp = ns.TAGS_PATH; good_t = open(tp, encoding="utf-8").read()
        try:
            for bad in ('{"version": 1, "rev": 2, "highlights": [{"id": "h1"', '[1, 2]'):
                open(np, "w", encoding="utf-8").write(bad)
                n = ns.load(pdf, {"id": "10.1/x"})
                assert n.get("unreadable") is True and n["rev"] == 0 and n["highlights"] == [], n
                n["highlights"].append({"id": "new", "page": 1})
                try:
                    ns.save(pdf, n, 0); raise AssertionError("an unreadable notes file was written over")
                except ns.Unreadable as e:
                    assert "could not be read" in str(e) and "nothing is saved over it" in str(e)
                assert open(np, encoding="utf-8").read() == bad, "the file is as it was"
            kept = glob.glob(np[:-5] + ".unreadable-*.json")
            assert kept and open(kept[0], encoding="utf-8").read() in ('{"version": 1, "rev": 2, "highlights": [{"id": "h1"', '[1, 2]'), kept
            open(tp, "w", encoding="utf-8").write('{"version": 1, "tags": [{"id"')
            try:
                ns.save_tags([{"id": "t9", "name": "z"}], 0); raise AssertionError("an unreadable tags file was written over")
            except ns.Unreadable:
                pass
            assert open(tp, encoding="utf-8").read() == '{"version": 1, "tags": [{"id"'
        finally:
            open(np, "w", encoding="utf-8").write(good); open(tp, "w", encoding="utf-8").write(good_t)
            for f_ in glob.glob(np[:-5] + ".unreadable-*.json") + glob.glob(tp[:-5] + ".unreadable-*.json"):
                os.remove(f_)
        assert "unreadable" not in ns.load(pdf) and ns.load(pdf)["rev"] == 2 and ns.save(pdf, ns.load(pdf), 2)["rev"] == 3, "a readable file is saved as before"
    check("notes: a notes or tags file that cannot be read is never taken for empty and written over; a copy is kept and the page is told", t_notes_unreadable)

    def t_snapshots():
        """Your own files (not the PDFs) are kept once a day on this computer; a restore puts them back as they were, keeps what was there first, writes only
        into the data folder and never a PDF; only the newest few are kept."""
        import aifiles as af, zipfile, time as _t
        keep = (af.SNAPSHOTS, af.KEEP_SNAPSHOTS); af.SNAPSHOTS = tempfile.mkdtemp(prefix="jw-snap-")
        np = ns.notes_path(pdf); good = open(np, encoding="utf-8").read()
        try:
            first = af.snapshot()
            assert first and af.snapshot() is None, "once a day"
            names = zipfile.ZipFile(os.path.join(af.SNAPSHOTS, first)).namelist()
            assert any(n.endswith(".notes.json") for n in names) and not any(n.lower().endswith(".pdf") for n in names) and not any(".env" in n for n in names), names[:8]
            open(np, "w", encoding="utf-8").write('{"version": 1, "rev": 9, "highlights": [], "cards": []}')          # a bad save
            pdf_bytes = open(pdf, "rb").read()
            r = af.restore_snapshot(first)
            assert open(np, encoding="utf-8").read() == good and r["restored"] >= 1 and r["before"] and "before-restore" in r["before"], r
            assert '"rev": 9' in zipfile.ZipFile(os.path.join(af.SNAPSHOTS, r["before"])).read(os.path.relpath(np, af.ROOT)).decode(), "what was there is kept first"
            assert open(pdf, "rb").read() == pdf_bytes
            evil = os.path.join(af.SNAPSHOTS, "jw-snapshot-20200101-000000.zip")
            with zipfile.ZipFile(evil, "w") as z:
                z.writestr("../outside.txt", "x"); z.writestr("Papers/evil.pdf", "x"); z.writestr(os.path.relpath(np, af.ROOT), good)
            r = af.restore_snapshot("jw-snapshot-20200101-000000.zip")
            assert r["restored"] == 1 and not os.path.exists(os.path.join(os.path.dirname(af.ROOT), "outside.txt")) and not os.path.exists(os.path.join(af.PAPERS, "evil.pdf")), r
            for bad in ("../x.zip", "nope.zip", ""):
                try:
                    af.restore_snapshot(bad); raise AssertionError("restored " + bad)
                except KeyError:
                    pass
            af.KEEP_SNAPSHOTS = 2
            af.snapshot(force=True); _t.sleep(1.1); last = af.snapshot(force=True)
            assert len(af.snapshots()) == 2 and af.snapshots()[0]["name"] == last, af.snapshots()
        finally:
            shutil.rmtree(af.SNAPSHOTS, ignore_errors=True); af.SNAPSHOTS, af.KEEP_SNAPSHOTS = keep
            open(np, "w", encoding="utf-8").write(good)
    check("snapshots: your own files (no PDFs, no keys) are kept once a day on this computer; a restore puts them back, keeps what was there first, and writes nothing outside the data folder", t_snapshots)

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

    def t_key_paste():
        """A pasted key is tidied (the NAME= of a whole .env line, "Bearer", quotes, invisible characters), what is sent is the tidied key, a key that cannot be one is refused, a changed key replaces the old client,
        and a provider that refuses the key says what to do (OpenRouter's "User not found" 401 was shown bare)."""
        assert ai.clean_key("OPENROUTER_API_KEY=sk-or-v1-abc") == ("sk-or-v1-abc", ["the name before the = sign"])
        assert ai.clean_key("export OPENROUTER_API_KEY = \"sk-or-v1-abc\"") == ("sk-or-v1-abc", ["the name before the = sign", "the quotes"])
        assert ai.clean_key("Bearer sk-or-v1-abc")[0] == "sk-or-v1-abc" and ai.clean_key("\u200bsk-or-v1-abc\ufeff\u00a0")[0] == "sk-or-v1-abc" and ai.clean_key("“sk-or-v1-abc”")[0] == "sk-or-v1-abc"
        assert ai.clean_key("AbC123==") == ("AbC123==", []) and ai.clean_key("sk-or-v1-abc") == ("sk-or-v1-abc", [])                  # a key that ends in = is not a NAME=value line
        envp = os.path.join(dl.ROOT, ".env"); saved_env = open(envp, encoding="utf-8").read() if os.path.exists(envp) else None
        c = json.loads(json.dumps(CFG)); c["providers"]["ortest"] = {"type": "openai_compat", "base_url": URL, "key_env": "OR_TEST_KEY", "models": ["m1"]}; c["providers"]["openrouter"] = {"type": "openai_compat", "base_url": URL, "key_env": "OR_TEST_KEY2", "models": []}
        write_cfg(c)
        try:
            out = ai.set_key("ortest", "OR_TEST_KEY=Bearer 'sk-or-v1-zzz'\u200b")
            assert "OR_TEST_KEY=sk-or-v1-zzz\n" in open(envp, encoding="utf-8").read() and "OR_TEST_KEY=OR_TEST_KEY" not in open(envp, encoding="utf-8").read() and "Tidied" in out["key_note"], out.get("key_note")
            r = ai.chat("ask", [{"role": "user", "content": "x"}], override={"provider": "ortest", "model": "m1"})                   # what is sent is the tidied key
            assert SEEN[-1][2] == "Bearer sk-or-v1-zzz", SEEN[-1][2]
            assert "key_note" not in ai.set_key("ortest", "sk-or-v1-clean"), "a clean key of an unknown kind gets no note"
            note = ai.set_key("openrouter", "sk-ant-api03-wrongprovider").get("key_note", "")
            assert "sk-or-" in note and "Openrouter" in note, note                                                                  # a key of another provider is said, not refused
            for bad in ("has space", "kéy", "a\"b", "", "   ", "x" * 401):
                try:
                    ai.set_key("ortest", bad); raise AssertionError("accepted %r" % bad)
                except ValueError as e:
                    assert "API key" in str(e)
            ai._clients["a"] = object(); ai.set_key("ortest", "sk-or-v1-again"); assert "a" not in ai._clients                          # a client made with the old key is not kept
            # ...and a key saved while a request is being prepared does not break that request (the cache was read twice, the second time after it had been cleared)
            import types as _t
            class _M:
                stop_reason, content = "end_turn", [_t.SimpleNamespace(type="text", text="ok")]
                usage = _t.SimpleNamespace(input_tokens=1, output_tokens=1, cache_creation_input_tokens=0, cache_read_input_tokens=0, server_tool_use=None)
            class _S:
                text_stream = iter(["ok"])
                def __enter__(self): return self
                def __exit__(self, *a): return False
                def get_final_message(self): return _M()
            ai._clients["a"] = _t.SimpleNamespace(beta=_t.SimpleNamespace(messages=_t.SimpleNamespace(stream=lambda **kw: _S())))
            real_to = ai._to_anthropic
            ai._to_anthropic = lambda m: (ai._clients.clear(), real_to(m))[1]                                                           # the cache is cleared while the request is being prepared
            c2 = json.loads(json.dumps(c)); c2["providers"]["anth"] = {"type": "anthropic", "key_env": "OR_TEST_KEY", "models": ["m1"]}; write_cfg(c2)
            try:
                assert ai.chat("ask", [{"role": "user", "content": "x"}], override={"provider": "anth", "model": "m1"}).text == "ok"
            finally:
                ai._to_anthropic = real_to; ai._clients.pop("a", None); write_cfg(c)
            os.environ["BAD_KEY"] = "bad"; c["features"]["y"] = {"provider": "bad", "model": "m"}; write_cfg(c)
            try:
                ai.chat("y", [{"role": "user", "content": "x"}]); raise AssertionError("expected AIError")
            except ai.AIError as e:
                assert "401" in str(e) and "press Change" in str(e) and "Incorrect API key" in str(e), str(e)
            assert "press Change" in str(ai._http_error("p", type("E", (), {"code": 403, "reason": "x"})(), "{}")) and "press Change" not in str(ai._http_error("p", type("E", (), {"code": 500, "reason": "x"})(), "{}"))
        finally:
            write_cfg(CFG); os.environ.pop("OR_TEST_KEY", None); os.environ.pop("OR_TEST_KEY2", None)
            if saved_env is None:
                os.path.exists(envp) and os.remove(envp)
            else:
                open(envp, "w", encoding="utf-8").write(saved_env)
    check("pasting a key: the NAME=, Bearer, quotes and invisible characters are removed (what is sent is the tidied key), keys that cannot be keys are refused, a changed key replaces the old client, and a refused key says what to do", t_key_paste)

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

    def t_chapter_ai():
        """Asking about one chapter reads only its pages; a book's passages say where they are; a chapter summary costs nothing to look up, is asked for after the estimate, goes through the budgeted
        layer, says its pages the way the app says them, and is kept."""
        import books as bk
        # --- retrieval and Ask limited to file pages (the fixture has 4 pages with the same four paragraphs on each)
        top = embed.search(pdf, "How many firms are in the sample?", k=20, pages=(2, 3))
        assert top and {b["page"] for b in top} <= {1, 2} and {b["page"] for b in embed.search(pdf, "How many firms are in the sample?", k=40)} == {0, 1, 2, 3}, "only pages 2 and 3 can be found"
        full = list(ask.stream(pdf, "Summarise.", "full", pages=(2, 3)))
        ps = next(m for m in full if m["type"] == "passages")["passages"]
        sent = SEEN[-1][1]["messages"][-1]["content"]
        assert {p["page"] for p in ps} == {2, 3} and full[-1]["type"] == "done" and "(p. 2)" in sent and "(p. 1)" not in sent and "(p. 4)" not in sent, ({p["page"] for p in ps}, sent[:200])
        rag = next(m for m in ask.stream(pdf, "How many firms are in the sample?", "rag", pages=(4, 4)) if m["type"] == "passages")["passages"]
        assert rag and all(p["page"] == 4 for p in rag), "the closest passages, from page 4 only"
        err = list(ask.stream(pdf, "q", "full", pages=(40, 41)))[-1]
        assert err["type"] == "error" and "this chapter" in err["error"], err
        assert all("where" not in p for p in ps), "a paper's passages have no 'where'"
        # --- a book's passages say where they are
        bpdf = os.path.join(d, "b.pdf"); shutil.copy(pdf, bpdf); brel = rel.replace("x.pdf", "b.pdf")
        json.dump({"source": "pdf", "pages": 4, "offset": 1, "toc": [[1, "Intro", 1], [1, "Methods", 3]]}, open(bpdf[:-4] + ".book.json", "w"))
        bps = next(m for m in ask.stream(bpdf, "Summarise.", "full", pages=(3, 4)) if m["type"] == "passages")["passages"]
        assert {p["where"] for p in bps} == {"Methods · p. 2", "Methods · p. 3"}, {p["where"] for p in bps}
        saved_cfg, had = dict(CFG), hasattr(Fake, "pieces")
        write_cfg(dict(CFG, features={"ask": {"provider": "fake", "model": "m1"}, "summarize": {"provider": "fake", "model": "m1"}}))
        old_pieces = getattr(Fake, "pieces", None)
        try:
            # --- the estimate is free: no request is made
            n0 = len(SEEN)
            est = bk.summary_estimate(brel, 1)
            assert len(SEEN) == n0 and est["pages"] == 2 and est["provider"] == "fake" and est["model"] == "m1" and est["cost"] and est["cost"] > 0 and est["left_out"] is False and est["saved"] is False, est
            for bad in (9, -1, "x", None):
                try:
                    bk.summary_estimate(brel, bad); raise AssertionError("chapter %r accepted" % (bad,))
                except ValueError as ex:
                    assert "does not exist" in str(ex)
            try:
                bk.summary_estimate(rel, 0); raise AssertionError("a paper is not a book")
            except ValueError as ex:
                assert "not a book" in str(ex)
            # --- asking for it: one request through the budgeted layer, the model's file pages said the way the app says them, the result kept
            Fake.pieces = ["**Argument.** It argues something. ", "**Key points.**\n- A claim (p. 3).\n- Another (pp. 3-4).\n"]
            msgs = list(bk.summary_stream(brel, 1, "A Test Book"))
            kinds = [m["type"] for m in msgs]
            assert kinds[0] == "status" and "text" in kinds and kinds[-2:] == ["final", "done"], kinds
            final = next(m for m in msgs if m["type"] == "final")["text"]
            assert "(p. 2)" in final and "(p. 2 – p. 3)" in final and "(p. 3)" not in final, final        # (offset 1: file page 3 is printed page 2)
            req = SEEN[-1][1]
            body = req["messages"][-1]["content"]
            assert len(SEEN) == n0 + 1 and "Book: A Test Book" in body and "Chapter: Methods (pages 3–4 of the file)" in body and "[3] " in body and "[4] " in body and "[1] " not in body, body[:300]
            assert "ONE chapter" in req["messages"][0]["content"] if req["messages"][0]["role"] == "system" else True
            assert msgs[-1]["saved"] is True and msgs[-1]["cached"] is False and msgs[-1]["model"] == "m1"
            kept = bk.info_at(bpdf)["summaries"]["3-4"]
            assert kept["text"] == final and kept["title"] == "Methods" and kept["model"] == "m1" and kept["left_out"] is False, kept
            assert next(c for c in json.loads(json.dumps(bk.chapters(bk.info_at(bpdf)))) if c["i"] == 1)["start"] == 3
            assert bk.summary_estimate(brel, 1)["saved"] is True
            # --- asking again returns the saved one without a request; "fresh" asks again
            n1 = len(SEEN)
            again = list(bk.summary_stream(brel, 1, "A Test Book"))
            assert len(SEEN) == n1 and again[-1]["cached"] is True and again[-1]["cost"] == 0.0 and next(m for m in again if m["type"] == "final")["text"] == final
            ai.cache_clear()                                     # (the saved summary is the book's own: clearing the AI's answer cache in Settings does not make it cost again)
            again2 = list(bk.summary_stream(brel, 1, "A Test Book"))
            assert len(SEEN) == n1 and again2[-1]["cached"] is True and next(m for m in again2 if m["type"] == "final")["text"] == final
            list(bk.summary_stream(brel, 1, "A Test Book", fresh=True))
            assert len(SEEN) == n1 + 1, "a fresh summary really asks"
            # --- the offset can still be changed afterwards, and the summary stays
            bk.set_offset(brel, 2)
            ki = bk.info_at(bpdf)
            assert ki["offset"] == 2 and ki["summaries"]["3-4"]["text"], ki
            # --- a long chapter: the middle is left out, said in the estimate, in the prompt and in the record
            old_max = bk.SUMMARY_MAX_CHARS
            bk.SUMMARY_MAX_CHARS = 300
            try:
                est2 = bk.summary_estimate(brel, 0)
                assert est2["left_out"] is True and est2["chars"] < 300 + len(bk.SUMMARY_SYSTEM) + 400, est2
                list(bk.summary_stream(brel, 0, "A Test Book"))
                assert "[Note: the middle of this chapter" in SEEN[-1][1]["messages"][-1]["content"] and bk.info_at(bpdf)["summaries"]["1-3"]["left_out"] is True
            finally:
                bk.SUMMARY_MAX_CHARS = old_max
            # --- errors are messages, not crashes
            assert list(bk.summary_stream(brel, 9))[-1] == {"type": "error", "error": "That chapter does not exist."}
            assert "not a book" in list(bk.summary_stream(rel, 0))[-1]["error"]
        finally:
            write_cfg(saved_cfg)
            if had:
                Fake.pieces = old_pieces
            elif hasattr(Fake, "pieces"):
                del Fake.pieces
            for ext in (".pdf", ".book.json"):
                try:
                    os.remove(bpdf[:-4] + ext)
                except OSError:
                    pass
    check("books: Ask can be limited to one chapter (only its pages; a book's passages say where they are), and a chapter summary is estimated for free, asked for through the budgeted layer, worded with your pages, kept, and not asked twice", t_chapter_ai)
    def t_whole_book():
        """Summarise a whole book: the estimate is free; every chapter and section without a summary is summarised once (a part with the same pages once, a part
        saved meanwhile skipped), in the tray, then one overview written from those summaries only; nothing twice; Cancel, a refused key, the budget and three
        failures in a row stop it; no overview while a part is missing; an empty answer is never saved or cached."""
        import time as _t
        import books as bk, bookjob as bj, activity as act_
        bpdf = os.path.join(d, "wb.pdf"); shutil.copy(pdf, bpdf); brel = rel.replace("x.pdf", "wb.pdf")
        toc = [[1, "Part I", 1], [2, "Ch 1", 1], [1, "Part II", 3], [2, "Sec 2.1", 4]]           # keys 1-3, 1-3 (the same pages: once), 3-4, 4-4
        pre = {"title": "Part II", "start": 3, "end": 4, "text": "PRESAVED part two (p. 2).", "provider": "fake", "model": "m1", "cost": 0.0}
        def reset(t=toc, sums=None):
            json.dump({"source": "pdf", "pages": 4, "offset": 1, "toc": t, "summaries": {"3-4": dict(pre)} if sums is None else sums}, open(bpdf[:-4] + ".book.json", "w"))
            ai.cache_clear()
        def wait():
            for _ in range(400):
                st = bj.status()
                if not st["running"]:
                    return st
                _t.sleep(0.05)
            raise AssertionError("the book job did not end")
        sysof = lambda body: body["messages"][0]["content"] if body["messages"][0]["role"] == "system" else (body.get("system") or "")      # noqa: E731
        chap = lambda body: re.search(r"Chapter: (.+?) \(pages", body["messages"][-1]["content"]).group(1)                                 # noqa: E731
        def answers(chapter=lambda b: ["S-%s (p. 3)." % chap(b)], overview=lambda b: ["OV (p. 2)."]):
            Fake.responder = lambda body: overview(body) if sysof(body).startswith("You write an OVERVIEW") else chapter(body)
        mine = lambda: [b for (path, b, _) in SEEN if path.endswith("/chat/completions") and (sysof(b).startswith("You write an OVERVIEW") or sysof(b).startswith("You summarise ONE chapter"))]  # noqa: E731
        saved_cfg = dict(CFG)
        write_cfg(dict(CFG, features={"ask": {"provider": "fake", "model": "m1"}, "summarize": {"provider": "fake", "model": "m1"}}))
        try:
            reset()
            # --- the estimate: free, and it counts each part once
            n0 = len(SEEN)
            e = bk.whole_estimate(brel)
            assert len(SEEN) == n0 and e["parts"] == 3 and e["done"] == 1 and e["todo"] == 2 and e["nested"] is True and e["overview"] is True, e
            exp = sum(ai.estimate("summarize", bk.chapter_text(bpdf, c)[1] + len(bk.SUMMARY_SYSTEM) + 300, bk.SUMMARY_OUT_TOKENS) for c in bk.parts(bk.info_at(bpdf)) if bk.chapter_key(c) != "3-4")
            assert abs(e["parts_cost"] - exp) < 1e-12 and e["overview_cost"] > 0 and abs(e["cost"] - exp - e["overview_cost"]) < 1e-12 and e["provider"] == "fake" and e["model"] == "m1", e
            for bad in (rel, ):
                try:
                    bk.whole_estimate(bad); raise AssertionError("a paper is not a book")
                except ValueError as ex:
                    assert "not a book" in str(ex)
            json.dump({"source": "pdf", "pages": 4}, open(bpdf[:-4] + ".book.json", "w"))
            try:
                bj.start(brel); raise AssertionError("a book with no outline")
            except ValueError as ex:
                assert "no outline" in str(ex)
            # --- the job: the two parts without a summary, in the book's order, then one overview from the summaries; the saved one is kept
            reset(); answers(); n0 = len(mine())
            v = bk.overview_view(bk.info_at(bpdf))       # what the book's page and the reader show: no overview yet, 1 of 3 parts summarised
            assert v == {"overview": None, "overview_stale": False, "parts": 3, "parts_done": 1, "parts_summarised": 1}, v
            assert bk.is_book(brel) and not bk.is_book(rel) and not bk.is_book("Papers/nowhere/none.pdf"), "a book is told by its .book.json; a paper is not a book"
            assert bj.start(brel, "A Test Book") is True and bj.start(brel, "A Test Book") is False          # one at a time
            st = wait()
            new = mine()[n0:]
            assert [chap(b) if not sysof(b).startswith("You write an OVERVIEW") else "OVERVIEW" for b in new] == ["Part I", "Sec 2.1", "OVERVIEW"], [sysof(b)[:30] for b in new]
            info = bk.info_at(bpdf); sums = info["summaries"]
            assert sums["1-3"]["text"] == "S-Part I (p. 2)." and sums["4-4"]["text"] == "S-Sec 2.1 (p. 2)." and sums["3-4"]["text"] == pre["text"], sums
            ov_body = new[-1]["messages"][-1]["content"]
            assert "S-Part I (p. 2)." in ov_body and "PRESAVED part two" in ov_body and "S-Sec 2.1" in ov_body and "[3] " not in ov_body and "Book: A Test Book" in ov_body, ov_body[:400]
            assert ov_body.index("S-Part I") < ov_body.index("PRESAVED") < ov_body.index("S-Sec 2.1"), "the summaries in the book's order"
            assert info["overview"]["text"] == "OV (p. 2)." and info["overview"]["parts"] == 3 and info["overview"]["model"] == "m1", info["overview"]     # (its references copied as written: not relabelled)
            v = bk.overview_view(info); assert v["overview"]["text"] == "OV (p. 2)." and not v["overview_stale"] and (v["parts"], v["parts_done"], v["parts_summarised"]) == (3, 3, 3), v
            assert st["made"] == 2 and st["overview"] is True and not st["failed"] and st["stopped"] == "" and st["total"] == 3, st
            js = [j for j in act_.snapshot() if j["kind"] == "summary" and j["label"].startswith("Book summary")]
            assert js and js[0]["status"] == "done" and js[0]["scope"] == {"library": True, "ltab": "books"}, js[:1]
            # --- nothing twice; a chapter summary written again makes the overview out of date, and only the overview is asked for again
            e = bk.whole_estimate(brel); assert e["todo"] == 0 and e["overview"] is False and e["cost"] == 0.0, e
            n1 = len(mine()); assert bj.start(brel) is False and len(mine()) == n1
            answers(chapter=lambda b: ["REWRITTEN (p. 3)."])
            list(bk.summary_stream(brel, 0, "A Test Book", fresh=True))
            assert bk.overview_pending(bk.info_at(bpdf)) and bk.overview_stale(bk.info_at(bpdf))
            v = bk.overview_view(bk.info_at(bpdf)); assert v["overview"]["text"] == "OV (p. 2)." and v["overview_stale"] is True, "an out-of-date overview is still shown, marked as such"
            ie = {"pages": 4, "toc": toc, "summaries": {"1-3": {"empty": True}, "3-4": {"empty": True}, "4-4": {"empty": True}}}
            assert bk.overview_view(ie) == {"overview": None, "overview_stale": False, "parts": 3, "parts_done": 3, "parts_summarised": 0}, "parts with no text are done but not summarised"
            n1 = len(mine()); assert bj.start(brel, "A Test Book") is True; wait()
            assert len(mine()) - n1 == 1 and sysof(mine()[-1]).startswith("You write an OVERVIEW") and "REWRITTEN" in mine()[-1]["messages"][-1]["content"]
            assert not bk.overview_pending(bk.info_at(bpdf)) and not bk.overview_stale(bk.info_at(bpdf))
            # --- a part summarised meanwhile (the per-chapter button, another computer) is skipped, and its text kept
            reset()
            def chapter_inject(b):
                if chap(b) == "Part I":
                    i_ = bk.info_at(bpdf); i_["summaries"]["4-4"] = {"title": "Sec 2.1", "start": 4, "end": 4, "text": "WRITTEN ELSEWHERE (p. 3)."}; bk._write_info(bpdf, i_)
                return ["S-%s (p. 3)." % chap(b)]
            answers(chapter=chapter_inject); n1 = len(mine())
            assert bj.start(brel, "A Test Book") is True; st = wait()
            assert [chap(b) if not sysof(b).startswith("You write") else "OV" for b in mine()[n1:]] == ["Part I", "OV"] and bk.info_at(bpdf)["summaries"]["4-4"]["text"] == "WRITTEN ELSEWHERE (p. 3).", st
            # --- Cancel (from the tray) during the first part: nothing saved, no overview, one request
            reset()
            def cancel_now(b):
                j_ = next(j for j in act_.snapshot() if j["kind"] == "summary" and j["label"].startswith("Book summary") and not j.get("ended"))
                act_.cancel(j_["id"]); return ["Some ", "words (p. 3)."]
            answers(chapter=cancel_now); n1 = len(mine())
            assert bj.start(brel) is True; st = wait()
            assert st["stopped"] == "cancelled" and not st["failed"] and len(mine()) - n1 == 1 and set(bk.info_at(bpdf)["summaries"]) == {"3-4"} and "overview" not in bk.info_at(bpdf), st     # (a cancel is not a failure)
            js = [j for j in act_.snapshot() if j["kind"] == "summary" and j["label"].startswith("Book summary")]
            assert js[0]["status"] == "cancelled", js[:1]
            # --- a refused key stops it at the first part
            reset(); Fake.responder = None; Fake.http_error = lambda body: (401, "User not found."); n1 = len(SEEN)
            assert bj.start(brel) is True; st = wait()
            assert len(SEEN) - n1 == 1 and len(st["failed"]) == 1 and "HTTP 401" in st["stopped"] and "overview" not in bk.info_at(bpdf), st
            Fake.http_error = None
            # --- three parts in a row with an empty answer: it stops, nothing is saved (an empty summary is never saved)
            reset(t=[[1, "A1", 1], [1, "B2", 2], [1, "C3", 3], [1, "D4", 4]], sums={}); answers(chapter=lambda b: [""]); n1 = len(mine())
            assert bj.start(brel) is True; st = wait()
            assert len(mine()) - n1 == 3 and "3 parts in a row failed" in st["stopped"] and st["made"] == 0 and "empty" in st["failed"][0]["error"] and not bk.info_at(bpdf).get("summaries") and "overview" not in bk.info_at(bpdf), st
            # --- one part fails, the others are made: the job goes on, but no overview is written while a part is missing; pressing again does only that part, then the overview
            reset(t=[[1, "A1", 1], [1, "B2", 2], [1, "C3", 3], [1, "D4", 4]], sums={}); answers(chapter=lambda b: [""] if chap(b) == "B2" else ["S-%s (p. 3)." % chap(b)])
            assert bj.start(brel) is True; st = wait()
            assert st["made"] == 3 and len(st["failed"]) == 1 and st["failed"][0]["title"] == "B2" and st["overview"] is False and st["stopped"] == "" and "overview" not in bk.info_at(bpdf), st      # (no overview is even tried)
            answers(); n1 = len(mine())
            assert bj.start(brel) is True; st = wait()
            assert [chap(b) if not sysof(b).startswith("You write") else "OV" for b in mine()[n1:]] == ["B2", "OV"] and st["overview"] is True, [sysof(b)[:20] for b in mine()[n1:]]
            # --- the budget: nothing may be asked, so it stops at once and says why
            reset(); answers(); ai.save_config({"budget": {"monthly_usd": 0.000001}}); n1 = len(SEEN)
            assert bj.start(brel) is True; st = wait()
            assert len(SEEN) == n1 and "budget" in st["stopped"] and "overview" not in bk.info_at(bpdf), st
            write_cfg(dict(CFG, features={"ask": {"provider": "fake", "model": "m1"}, "summarize": {"provider": "fake", "model": "m1"}}))
            # --- a part with no body text (a bibliography, an index): left out, with nothing asked; the overview is still written
            real_extract = bk.bl.extract
            bk.bl.extract = lambda p_, force=False: [dict(b_, kind="ref") if b_["page"] == 3 else b_ for b_ in real_extract(p_)]       # (page 4 is all references)
            try:
                reset(t=[[1, "A1", 1], [1, "B2", 2], [1, "C3", 3], [1, "References", 4]], sums={}); answers(); n1 = len(mine())
                e = bk.whole_estimate(brel)
                assert e["todo"] == 3 and e["empty"] == 1 and e["overview"] is True, e
                assert bj.start(brel, "A Test Book") is True; st = wait()
                info = bk.info_at(bpdf)
                assert st["made"] == 3 and st["skipped"] == 1 and not st["failed"] and st["overview"] is True and info["summaries"]["4-4"].get("empty") and len(mine()) - n1 == 4, st
                assert "References" not in mine()[-1]["messages"][-1]["content"], "the overview leaves out a part with no text"
                assert bj.start(brel) is False and bk.whole_estimate(brel)["todo"] == 0, "nothing left to do"
            finally:
                bk.bl.extract = real_extract
            # --- a long book: the overview drops the deepest level first (sections, then chapters), never the parts, and says what it left out
            info_ = {"source": "pdf", "pages": 9, "offset": 0, "toc": [[1, "Part One", 1], [2, "Chapter A", 2], [3, "Section A.1", 3], [1, "Part Two", 5], [2, "Chapter B", 6], [3, "Section B.1", 7]]}
            info_["summaries"] = {bk.chapter_key(c): {"text": ("%s says something. " % c["title"]) * 30} for c in bk.parts(info_)}
            old_max = bk.OVERVIEW_MAX_CHARS
            try:
                bk.OVERVIEW_MAX_CHARS = 5000
                body_, cut_ = bk._overview_input(info_, bk.parts(info_))
                assert "Chapter A says" in body_ and "Part One says" in body_ and "Section A.1 says" not in body_ and cut_ == "sections" and "below outline level 2" in body_, body_[:300]
                bk.OVERVIEW_MAX_CHARS = 2500
                body_, cut_ = bk._overview_input(info_, bk.parts(info_))
                assert "Part One says" in body_ and "Chapter A says" not in body_ and cut_ == "sections", body_[:300]
            finally:
                bk.OVERVIEW_MAX_CHARS = old_max
            # --- the per-chapter button: an empty answer is an error, nothing is saved, and asking again really asks (it was not cached)
            reset(); answers(chapter=lambda b: [""]); n1 = len(mine())
            out = list(bk.summary_stream(brel, 0, "A Test Book"))
            assert out[-1]["type"] == "error" and "empty" in out[-1]["error"] and "1-3" not in bk.info_at(bpdf)["summaries"], out[-1]
            answers(chapter=lambda b: ["Fine now (p. 3)."])
            out = list(bk.summary_stream(brel, 0, "A Test Book"))
            assert len(mine()) - n1 == 2 and out[-1]["type"] == "done" and bk.info_at(bpdf)["summaries"]["1-3"]["text"] == "Fine now (p. 2).", (out[-1], len(mine()) - n1)
        finally:
            Fake.responder = None; Fake.http_error = None
            write_cfg(saved_cfg)
            for ext in (".pdf", ".book.json"):
                try:
                    os.remove(bpdf[:-4] + ext)
                except OSError:
                    pass
    check("books: summarise the whole book: a free estimate, every chapter and section once (one already saved, or saved meanwhile, is skipped), in the tray, then one overview from the summaries; Cancel, a refused key, the budget and three failures in a row stop it; no overview while a part is missing; an empty answer is never saved", t_whole_book)
    def t_parallel_embeddings():
        """The local model embeds several small pieces of text at once (about one per core): the vectors come out in the same order and equal to one-at-a-time, progress and Cancel work across pieces,
        and a model that cannot be used from several threads falls back to one piece at a time."""
        import random, threading, time
        import numpy as np

        class M:            # a stand-in for the fastembed model: slow by a random amount, and it remembers how many calls ran at the same moment
            active = peak = calls = 0
            lock = threading.Lock()
            unsafe = False

            def passage_embed(self, texts):
                with M.lock:
                    M.active += 1; M.peak = max(M.peak, M.active); M.calls += 1
                    clash = M.unsafe and M.active > 1
                try:
                    time.sleep(random.random() * 0.02)
                    if clash:
                        raise RuntimeError("Already borrowed")
                    return [np.array([float(len(t)), float(sum(map(ord, t)) % 97), 1.0]) for t in texts]
                finally:
                    with M.lock:
                        M.active -= 1
            query_embed = passage_embed

            @classmethod
            def reset(cls, unsafe=False):
                cls.active = cls.peak = cls.calls = 0; cls.unsafe = unsafe

        texts = ["x" * (i + 1) + str(i) for i in range(100)]
        saved_env, had = os.environ.get("JW_EMBED_WORKERS"), "stand-in" in ai._local
        ai._local["stand-in"] = M()
        try:
            os.environ["JW_EMBED_WORKERS"] = "1"; M.reset()
            one_by_one = ai._embed_local(texts, "passage", "stand-in")
            assert M.peak == 1 and ai.embed_workers() == 1, M.peak
            os.environ["JW_EMBED_WORKERS"] = "4"; M.reset()
            seen = []
            several = ai._embed_local(texts, "passage", "stand-in", lambda a, b: seen.append((a, b)))
            assert M.peak > 1 and ai.embed_workers() == 4, "pieces really ran at the same time"
            assert np.array_equal(one_by_one, several) and [int(v[0]) for v in several] == [len(t) for t in texts], "same vectors, same order"
            assert seen[-1] == (100, 100) and [a for a, _ in seen] == sorted(a for a, _ in seen) and len(seen) == 7, seen        # (7 pieces of 16)
            # Cancel: the pieces not started are dropped
            many = ["y" * (i + 1) for i in range(400)]
            os.environ["JW_EMBED_WORKERS"] = "2"; M.reset()

            def stop(a, b):
                raise ai.Cancelled("Cancelled.")
            try:
                ai._embed_local(many, "passage", "stand-in", stop); raise AssertionError("Cancel was ignored")
            except ai.Cancelled:
                pass
            assert M.calls < 25, M.calls
            # a model that fails when used from several threads: the answer is still right
            os.environ["JW_EMBED_WORKERS"] = "4"; M.reset(unsafe=True)
            safe = ai._embed_local(texts, "passage", "stand-in")
            assert np.array_equal(safe, one_by_one), "fell back to one piece at a time"
            # through ai.embed(): normalised, and the worker count is automatic without the override
            M.reset(); del os.environ["JW_EMBED_WORKERS"]
            saved_cfg = dict(CFG)
            write_cfg(dict(CFG, embeddings={"provider": "local", "model": "stand-in"}))
            try:
                arr = ai.embed(texts[:20])
            finally:
                write_cfg(saved_cfg)
            assert arr.shape == (20, 3) and np.allclose(np.linalg.norm(arr, axis=1), 1.0, atol=1e-5)
            assert 1 <= ai.embed_workers() <= 12 and ai.embed_workers() == max(1, min(os.cpu_count() or 1, 12))
        finally:
            if saved_env is None:
                os.environ.pop("JW_EMBED_WORKERS", None)
            else:
                os.environ["JW_EMBED_WORKERS"] = saved_env
            if not had:
                ai._local.pop("stand-in", None)
    check("indexing: the local model embeds pieces of text several at a time (one per core), in order and with the same vectors, Cancel stops the rest, and a model that is not thread-safe falls back to one at a time", t_parallel_embeddings)
    def t_index_validity():
        """Vectors that arrive from another computer through the shared folder are used only if they fit this paper and this model: a wrong shape, a cut-off file or another model's vectors are made again.
        Work that was done reports how long it took."""
        import numpy as np
        saved_cfg = dict(CFG)
        write_cfg(dict(CFG, embeddings={"provider": "fake", "model": "emb"}))
        try:
            vec, meta = embed._paths(pdf)
            for f in (vec, meta):
                if os.path.exists(f):
                    os.remove(f)
            r1 = embed.index_paper(pdf)
            assert r1["cached"] is False and r1["n"] > 0 and r1["embed_s"] >= 0 and r1["extract_s"] >= 0 and r1["workers"] == 1, r1       # (a remote service: no local workers)
            assert embed.index_paper(pdf)["cached"] is True
            assert not [x for x in os.listdir(bl.INDEX) if x.endswith(".tmp")], "no temporary file is left behind"
            n = r1["n"]
            good = np.load(vec)
            for label, damage in (("more rows than passages (another version of the paper)", lambda: np.save(open(vec, "wb"), np.zeros((n + 3, good.shape[1]), dtype="float32"))),
                                  ("another width (another model)", lambda: np.save(open(vec, "wb"), np.zeros((n, good.shape[1] + 5), dtype="float32"))),
                                  ("cut off in the middle (still arriving)", lambda: open(vec, "wb").write(open(vec, "rb").read()[:20])),
                                  ("not an array at all", lambda: open(vec, "wb").write(b"junk"))):
                np.save(open(vec, "wb"), good)
                damage()
                assert embed._valid(vec, meta, n) is False, label
                again = embed.index_paper(pdf)
                assert again["cached"] is False and np.load(vec).shape == good.shape, label
                assert embed.index_paper(pdf)["cached"] is True, label
            m = json.load(open(meta)); m["model"] = "fake:other"; json.dump(m, open(meta, "w"))
            assert embed.index_paper(pdf)["cached"] is False, "vectors made by another model are made again"
            # the search still works on what was rebuilt
            assert embed.search(pdf, "How many firms are in the sample?", k=3)
        finally:
            write_cfg(saved_cfg)
    check("indexing: shared vectors are used only if they fit this paper and model (wrong shape, cut-off or other-model files are made again), nothing temporary is left, and the work reports its timing", t_index_validity)
    def t_shared_caches():
        """The search index and the saved AI answers live in the shared state/ folder: copies Dropbox makes there are removed (never shown as conflicts), old temporary files too; a real conflict is still reported."""
        import subprocess
        home, loc = tempfile.mkdtemp(prefix="jw-home-"), tempfile.mkdtemp(prefix="jw-local-")
        try:
            probe = r'''
import json, os, sys, time
sys.path.insert(0, %r)
import paths
paths.seed_home()
import ai, blocks, syncinfo
st = paths.STATE
def put(rel, body="x", age=0):
    p = os.path.join(st, rel) if not rel.startswith("Papers") else os.path.join(paths.ROOT, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "w").write(body)
    if age: os.utime(p, (time.time() - age, time.time() - age))
put("index/x.vec", "a" * 100); put("index/x (Work Mac's conflicted copy 2026-11-03).vec"); put("index/x.blocks.json", "{}")
put("ai_cache/k.json", "{}"); put("ai_cache/k (Home laptop's conflicted copy).json")
put("index/old.vec.1.2.tmp", age=3 * 86400); put("index/new.vec.1.2.tmp")
put("Papers/_index (Work Mac's conflicted copy 2026-11-03).csv", "a,b\n")
before = sorted(c["path"] for c in syncinfo.conflicts(fresh=True))
removed = syncinfo.tidy_caches()
left = sorted(os.listdir(os.path.join(st, "index"))) + sorted(os.listdir(os.path.join(st, "ai_cache")))
after = sorted(c["path"] for c in syncinfo.conflicts(fresh=True))
print("JSON" + json.dumps({"index": blocks.INDEX, "cache": ai.CACHE, "state": st, "before": before, "removed": removed, "left": left, "after": after, "size": syncinfo.cache_size(), "info": syncinfo.info()["shared_cache"]}))
''' % HERE
            env = {k: v for k, v in os.environ.items() if k not in ("JW_HOME", "JW_LOCAL")}
            r = subprocess.run([sys.executable, "-c", probe], env=dict(env, JW_HOME=home, JW_LOCAL=loc), capture_output=True, text=True, timeout=120)
            assert r.returncode == 0, r.stderr[-800:]
            o = json.loads(r.stdout.split("JSON", 1)[1])
            assert o["index"] == os.path.join(o["state"], "index") and o["cache"] == os.path.join(o["state"], "ai_cache"), "both are in the shared state/"
            assert o["before"] == ["Papers/_index (Work Mac's conflicted copy 2026-11-03).csv"], o["before"]        # (the cache copies are not shown as conflicts)
            assert o["removed"] == 3, o["removed"]                                                                    # (two conflicted copies and the old temporary file)
            assert o["left"] == ["new.vec.1.2.tmp", "x.blocks.json", "x.vec", "k.json"] or sorted(o["left"]) == sorted(["new.vec.1.2.tmp", "x.blocks.json", "x.vec", "k.json"]), o["left"]
            assert o["after"] == o["before"], "a real conflict (the library list) is untouched"
            assert o["size"] == o["info"] and o["size"]["files"] == 4 and o["size"]["bytes"] >= 100, o["size"]
        finally:
            shutil.rmtree(home, ignore_errors=True); shutil.rmtree(loc, ignore_errors=True)
    check("sync: the search index and saved AI answers are shared through state/; Dropbox copies and stale temporary files there are removed (not shown as conflicts), a real conflict is still reported, and their size is known", t_shared_caches)
    def t_ui_prefs():
        """Page choices kept in the data folder: only the named keys travel, changes merge a key at a time, an unchanged value writes nothing, an unreadable file is kept aside, and old reading positions are pruned."""
        import time
        import uiprefs as up_
        saved = (up_.FILE, up_.MAX_POS)
        up_.FILE = os.path.join(TMP, "ui_prefs.json")
        try:
            if os.path.exists(up_.FILE):
                os.remove(up_.FILE)
            g = up_.get()
            assert g["prefs"] == {} and "jw-theme" in g["rules"]["exact"] and "jw-pos:" in g["rules"]["prefix"], g
            pos = "jw-pos:Papers/Management Science/Jørgensen – Équilibre 2020.pdf"
            assert up_.update({"jw-theme": "dark", "jw-sb": "folded", "jw-rd-side": "300", "other": "x", pos: '{"n":3,"y":0.2}', "jw-bad\x01": "x"}, []) == 2
            assert up_.get()["prefs"] == {"jw-theme": "dark", pos: '{"n":3,"y":0.2}'}, "the sidebar and the panel width stay on one computer"
            assert up_.update({"jw-theme": "light"}, []) == 1 and up_.get()["prefs"][pos] == '{"n":3,"y":0.2}', "another key is not undone"
            t0 = os.stat(up_.FILE).st_mtime_ns
            time.sleep(0.02)
            assert up_.update({"jw-theme": "light"}, []) == 0 and os.stat(up_.FILE).st_mtime_ns == t0, "an unchanged value writes nothing"
            assert up_.update({"jw-theme": "x" * 5000, "jw-fp-view": 3}, []) == 0, "too long, or not text: refused"
            assert up_.update({}, ["jw-theme", "jw-nothing"]) == 1 and "jw-theme" not in up_.get()["prefs"]
            assert up_.update("junk", "junk") == 0
            # old reading positions are pruned, the newest kept
            up_.MAX_POS = 3
            up_.update({"jw-pos:p%d" % i: "1" for i in range(6)}, [])
            keys = sorted(k for k in up_.get()["prefs"] if k.startswith("jw-pos:p"))
            assert keys == ["jw-pos:p3", "jw-pos:p4", "jw-pos:p5"], keys
            # a file that cannot be read is kept, not overwritten
            for junk in ("{not json", "[1, 2]"):
                open(up_.FILE, "w").write(junk)
                assert up_.get()["prefs"] == {}
                assert up_.update({"jw-theme": "dark"}, []) == 1
                assert open(up_.FILE + ".unreadable").read() == junk and up_.get()["prefs"] == {"jw-theme": "dark"}
            # the server side of the rules
            assert up_.is_shared("jw-skim-on:Papers/A/b.pdf") and up_.is_shared("jw-lsn-voice") and not up_.is_shared("jw-sb") and not up_.is_shared("theme") and not up_.is_shared(None)
        finally:
            up_.FILE, up_.MAX_POS = saved
            for suffix in ("", ".unreadable"):
                try:
                    os.remove(os.path.join(TMP, "ui_prefs.json") + suffix)
                except OSError:
                    pass
    check("settings follow you: the page's choices (theme, view modes, reading positions) are kept in the data folder, only the named keys travel, a key at a time, nothing is rewritten when nothing changed, an unreadable file is kept aside, old positions are pruned", t_ui_prefs)
    def t_prices():
        """What a model costs and where the number comes from: a price you typed wins, OpenRouter models get OpenRouter's list (read at most once a day, kept when a read
        fails), Claude models the built-in table, a keyless model on this computer is free, anything else is unknown (never a silent $0)."""
        import time
        import prices as pr_
        from datetime import date
        from http.server import BaseHTTPRequestHandler as BH
        payload = {"data": [
            {"id": "scaledown/summarize", "pricing": {"prompt": "0.00000004", "completion": "0"}},
            {"id": "anthropic/claude-sonnet-5.5", "pricing": {"prompt": "0.000002", "completion": "0.00001", "web_search": "0.004", "input_cache_read": "0.0000001", "input_cache_write": "0.0000025"}},
            {"id": "openrouter/auto", "pricing": {"prompt": "-1", "completion": "-1"}},
            {"id": "broken", "pricing": {"prompt": "x"}}, {"pricing": {}},
            # (models that can answer in a JSON schema, and some that cannot or may not be picked: for the backup screening model)
            {"id": "cheap/json", "pricing": {"prompt": "0.00000005", "completion": "0.00000005"}, "supported_parameters": ["response_format", "structured_outputs"], "context_length": 131072, "top_provider": {"max_completion_tokens": 16384}},
            {"id": "so/json", "pricing": {"prompt": "0.000001", "completion": "0.000001"}, "supported_parameters": ["structured_outputs"], "context_length": 200000},
            {"id": "mid/json", "pricing": {"prompt": "0.000003", "completion": "0.000003"}, "supported_parameters": ["tools", "structured_outputs"]},
            {"id": "rf/only", "pricing": {"prompt": "0.00000001", "completion": "0.00000001"}, "supported_parameters": ["response_format"]},           # JSON mode only, no schema
            {"id": "x/json:free", "pricing": {"prompt": "0", "completion": "0"}, "supported_parameters": ["structured_outputs"]},
            {"id": "tiny/json", "pricing": {"prompt": "0.000000001", "completion": "0.000000001"}, "supported_parameters": ["structured_outputs"], "context_length": 8192},
            {"id": "short/json", "pricing": {"prompt": "0.000000001", "completion": "0.000000001"}, "supported_parameters": ["structured_outputs"], "top_provider": {"max_completion_tokens": 2048}},
            {"id": "fee/json", "pricing": {"prompt": "0", "completion": "0", "request": "0.01"}, "supported_parameters": ["structured_outputs"]},
            {"id": "half/x", "pricing": {"prompt": "0.00000002", "completion": "0.00000003"}},
            {"id": "openrouter/stealth-json", "pricing": {"prompt": "0", "completion": "0"}, "supported_parameters": ["structured_outputs"], "context_length": 200000}]}     # $0 without ":free"
        hits = []

        class H(BH):
            fail = False

            def log_message(self, *a):
                pass

            def do_GET(self):
                hits.append(self.path)
                if H.fail:
                    self.send_response(500); self.end_headers(); return
                b = json.dumps(payload).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        saved = (pr_.OR_URL, pr_.CACHE)
        pr_.OR_URL, pr_.CACHE = "http://127.0.0.1:%d/api/v1/models" % srv.server_address[1], os.path.join(TMP, "openrouter_prices.json")
        pr_._mem["data"] = None
        try:
            if os.path.exists(pr_.CACHE):
                os.remove(pr_.CACHE)
            parsed = pr_.parse_openrouter(payload)
            assert parsed["scaledown/summarize"] == {"in": 0.04, "out": 0.0, "cache_write": 0.04, "cache_read": 0.04}, parsed
            assert parsed["anthropic/claude-sonnet-5.5"]["cache_read"] == 0.1 and parsed["anthropic/claude-sonnet-5.5"]["web_search"] == 0.004, parsed
            assert "openrouter/auto" not in parsed and "broken" not in parsed and len(parsed) == 12, "a router's -1 price and a broken entry are left out"
            assert parsed["cheap/json"]["json"] is True and parsed["cheap/json"]["ctx"] == 131072 and parsed["cheap/json"]["max_out"] == 16384 and parsed["so/json"]["json"] is True, parsed
            assert not any("json" in parsed[m] for m in ("rf/only", "half/x", "fee/json", "anthropic/claude-sonnet-5.5")), "JSON mode alone, or a charge per request: not a schema model"
            open(pr_.CACHE, "w").write(json.dumps({"fetched": "2026-11-06T00:00:00+00:00", "fetched_ts": time.time(), "models": {"scaledown/summarize": parsed["scaledown/summarize"]}}))
            pr_._mem["data"] = None                                                                           # a list from before the JSON flags, read an hour ago: read again now
            assert pr_.refresh() == 12 and len(hits) == 1 and json.load(open(pr_.CACHE))["format"] == pr_.FORMAT and json.load(open(pr_.CACHE))["models"]["cheap/json"]["json"]
            assert pr_.refresh() == 12 and len(hits) == 1, "a list read less than a day ago is not read again"
            H.fail = True
            assert pr_.refresh(force=True) == 12 and len(hits) == 2, "a failed read keeps the list it had"
            H.fail = False
            cfg = {"providers": {"anthropic": {"type": "anthropic"}, "openrouter": {"type": "openai_compat", "base_url": "https://openrouter.ai/api/v1", "key_env": "K"},
                                 "ollama": {"type": "openai_compat", "base_url": "http://localhost:11434/v1"}, "proxy": {"type": "openai_compat", "base_url": "http://127.0.0.1:4000/v1", "key_env": "P"},
                                 "openai": {"type": "openai_compat", "base_url": "https://api.openai.com/v1", "key_env": "O"}},
                   "prices": {"gpt-mine": {"in": 1, "out": 3}, "scaledown/summarize": {"in": 0, "out": 0}}, "features": {}}
            src = lambda p, m: (pr_.lookup(p, m, cfg) or {}).get("source")  # noqa: E731
            assert src("openrouter", "scaledown/summarize") == "openrouter" and pr_.lookup("openrouter", "scaledown/summarize", cfg)["in"] == 0.04, "an all-empty typed row does not hide the automatic price"
            assert src("anthropic", "claude-sonnet-5-5") == "built-in" and pr_.lookup("anthropic", "claude-sonnet-5-5", cfg)["out"] == 10.0
            assert src("ollama", "llama3") == "free" and src("proxy", "llama3") is None, "free only on this computer and without a key (a local proxy may forward to a paid service)"
            assert src("openai", "gpt-mine") == "yours" and src("openai", "gpt-x") is None and src("openrouter", "no/such") is None
            cfg["prices"]["claude-sonnet-5-5"] = {"in": 9, "out": 9}
            assert src("anthropic", "claude-sonnet-5-5") == "yours" and pr_.lookup("anthropic", "claude-sonnet-5-5", cfg, yours=False)["source"] == "built-in", "your price wins; Settings still shows the automatic one"
            assert pr_.stale({"source": "built-in", "checked": "2026-01-01"}, date(2026, 11, 5)) and not pr_.stale({"source": "built-in", "checked": "2026-10-06"}, date(2026, 11, 5))
            assert pr_.stale({"source": "openrouter", "checked": "2026-11-01"}, date(2026, 11, 5)) and not pr_.stale({"source": "yours", "checked": ""})
            assert pr_.name("anthropic", "claude-sonnet-5-5") == "Claude Sonnet 5.5" and pr_.name("openrouter", "anthropic/claude-sonnet-5.5") == "Claude Sonnet 5.5"
            assert pr_.name("openrouter", "scaledown/summarize") == "scaledown/summarize" and pr_.name("anthropic", "claude-opus-5") == "Claude Opus 5"
            # the backup screening model: same provider, can answer in a JSON schema, the cheapest at no more than twice the price (else the cheapest, said); never itself
            assert set(pr_.structured_models("openrouter", cfg)) == {"cheap/json", "so/json", "mid/json"}, pr_.structured_models("openrouter", cfg)   # (no free model (":free" or $0), no small or per-request model)
            b = pr_.backup_for("openrouter", "scaledown/summarize", cfg)
            assert b == {"provider": "openrouter", "model": "cheap/json", "name": "cheap/json", "price": 0.1, "current_price": 0.04, "within": False}, b       # 0.1 > 2 x 0.04
            b = pr_.backup_for("openrouter", "anthropic/claude-sonnet-5.5", cfg)
            assert b["model"] == "cheap/json" and b["within"] is True and b["current_price"] == 12.0, b
            assert pr_.backup_for("openrouter", "half/x", cfg)["within"] is True, "exactly twice the price is within"
            b = pr_.backup_for("openrouter", "cheap/json", cfg)
            assert b["model"] == "so/json" and b["within"] is False, b                                         # never the model itself
            b = pr_.backup_for("openrouter", "no/such", cfg)
            assert b["model"] == "cheap/json" and b["current_price"] is None and b["within"] is False, b      # its price unknown: the cheapest, and said
            b = pr_.backup_for("anthropic", "claude-sonnet-5-5", dict(cfg, prices={}))
            assert b["model"] == "claude-haiku-5-5" and b["name"] == "Claude Haiku 5.5" and b["within"] is True, b
            assert pr_.backup_for("openai", "gpt-x", cfg) is None and pr_.structured_models("ollama", cfg) is None, "no list for other providers: no backup"
            n_hits, off0 = len(hits), os.environ.get("JW_OFFLINE")
            open(pr_.CACHE, "w").write(json.dumps({"fetched_ts": time.time(), "models": {"cheap/json": {"in": 0.05, "out": 0.05}}})); pr_._mem["data"] = None
            os.environ["JW_OFFLINE"] = "1"
            try:
                assert pr_.structured_models("openrouter", cfg) == {} and len(hits) == n_hits, "offline (JW_OFFLINE): an old list is not read again, and gives no backup"
                os.environ.pop("JW_OFFLINE")
                assert set(pr_.structured_models("openrouter", cfg)) == {"cheap/json", "so/json", "mid/json"} and len(hits) == n_hits + 1, "an old list is read again when a backup is needed"
            finally:
                os.environ.pop("JW_OFFLINE", None) if off0 is None else os.environ.__setitem__("JW_OFFLINE", off0)
            # through ai.cost_of: the provider decides where the price comes from
            saved_cfg = dict(CFG)
            write_cfg(dict(CFG, providers=dict(CFG["providers"], openrouter=cfg["providers"]["openrouter"], anthropic=cfg["providers"]["anthropic"]), prices={}))
            try:
                assert abs(ai.cost_of("scaledown/summarize", {"in": 1_000_000, "out": 500_000}, provider="openrouter") - 0.04) < 1e-9
                assert abs(ai.cost_of("claude-sonnet-5-5", {"in": 1_000_000, "out": 100_000}, provider="anthropic") - 3.0) < 1e-9
                assert ai.cost_of("m1", {"in": 1000}, provider="fake") is None, "a model with no price is unknown, not $0"
                assert abs(ai.cost_of("anthropic/claude-sonnet-5.5", {"in": 0, "out": 0}, searches=3, provider="openrouter") - 0.012) < 1e-9, "OpenRouter's own price per search (not the $0.01 default)"
                pa = ai.price_auto()
                assert pa["m1"]["source"] is None and pa["m1"]["provider"] == "fake", pa
            finally:
                write_cfg(saved_cfg)
        finally:
            srv.shutdown()
            pr_.OR_URL, pr_.CACHE = saved
            pr_._mem["data"] = None
    check("prices: a typed price wins; OpenRouter models get OpenRouter's list (once a day, kept when a read fails), Claude models the built-in table, a keyless local model is free, anything else is unknown", t_prices)
    def t_run_cost_detail():
        """A run's cost names every model that ran, at that model's price; a call without a price is counted as such (the total is 'at least'), an answer from the saved
        answers costs nothing, an old run still shows its Claude Sonnet 5.5 tokens, and the screening estimate follows the screening model's price."""
        class R_:
            def __init__(s, prov, model, cost, i=1000, o=100, cached=False):
                s.provider, s.model, s.cost, s.usage, s.cached = prov, model, cost, {"in": i, "out": o}, cached
        import pipeline as pl
        u = pl.Usage()
        u.add_result(R_("anthropic", "claude-sonnet-5-5", 0.012)); u.add_result(R_("anthropic", "claude-sonnet-5-5", 0.008))
        u.add_result(R_("openrouter", "scaledown/summarize", 0.0002)); u.add_result(R_("openai", "gpt-x", None)); u.add_result(R_("openai", "gpt-x", 5.0, cached=True))
        d = u.detail()
        names = [m["name"] for m in d["models"]]
        assert names[0] == "Claude Sonnet 5.5" and set(names) == {"Claude Sonnet 5.5", "scaledown/summarize", "gpt-x"}, names
        assert abs(d["cost"] - 0.0202) < 1e-9 and d["unpriced"] == 1, d
        son = d["models"][0]
        assert son["calls"] == 2 and son["in"] == 2000 and son["out"] == 200, son
        assert [m for m in d["models"] if m["name"] == "gpt-x"][0]["calls"] == 1, "the saved answer was not counted"
        json.dumps(u.d)
        old = pl.Usage({"in": 100000, "out": 5000, "cache_write": 0, "cache_read": 0}).detail()
        assert old["cost"] == 0.25 and old["models"][0]["name"] == "Claude Sonnet 5.5" and old["unpriced"] == 0, old
        assert pl.Usage().detail() == {"cost": 0.0, "unpriced": 0, "models": []}
        saved_cfg = dict(CFG)
        try:
            write_cfg(dict(CFG, features=dict(CFG["features"], screen={"provider": "fake", "model": "m1"}), prices={"m1": {"in": 2.0, "out": 10.0}}))
            assert abs(pl.est_screen_cost_per_paper() - pl.EST_SCREEN_COST_PER_PAPER) < 0.0001, "the old constant was Sonnet's price for one paper"
            write_cfg(dict(CFG, features=dict(CFG["features"], screen={"provider": "fake", "model": "m1"}), prices={"m1": {"in": 0.2, "out": 1.0}}))
            assert abs(pl.est_screen_cost_per_paper() - pl.EST_SCREEN_COST_PER_PAPER / 10) < 0.00002, pl.est_screen_cost_per_paper()
            write_cfg(dict(CFG, features=dict(CFG["features"], screen={"provider": "fake", "model": "m1"}), prices={}))
            assert pl.est_screen_cost_per_paper() is None, "no price: unknown, not $0"
        finally:
            write_cfg(saved_cfg)
    check("a run's cost names every model that ran at its own price, counts calls without a price as such, skips saved answers, keeps old runs right, and the screening estimate follows the screening model", t_run_cost_detail)
    def t_delete_items():
        """What you added can be deleted: a paper or a book is deleted for good (files, notes, summary, card, search index, folders, reading status) and stays out of
        later runs, adding it yourself brings it back, a past run's record can be deleted, and an API key can be removed from this computer."""
        import subprocess
        home, loc = tempfile.mkdtemp(prefix="jw-home-"), tempfile.mkdtemp(prefix="jw-local-")
        try:
            child = r"""
import csv, json, os, sys, threading
sys.path.insert(0, %r)
import paths
paths.seed_home()
import download as dl, deletion, pipeline as pl, folders as fo, readstate as rs, uiprefs, notes_store as ns, blocks as bl, ai
ROOT = paths.ROOT
out = {}
def put(rel, data=b"x"):
    p = os.path.join(ROOT, rel); os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "wb").write(data); return p
A, B, C = "Papers/2025-01/MISQ/Alpha.pdf", "Papers/Books/Beta.pdf", "Papers/2025-01/MISQ/Gamma.pdf"
pa = put(A, b"%%PDF-alpha"); put(A[:-4] + ".md", b"summary"); open(ns.notes_path(pa), "w").write("{}"); open(ns.card_path(pa), "w").write("{}")
pb = put(B, b"%%PDF-beta"); put(B[:-4] + ".epub", b"epub"); put(B[:-4] + ".book.json", b'{"source": "epub"}')
put(C, b"%%PDF-gamma")
for p, f in (({"doi": "10.1/a", "title": "Alpha", "journal": "MISQ", "abbr": "MISQ"}, A), ({"doi": "", "title": "Beta, a book", "journal": "Books", "abbr": "BOOK"}, B), ({"doi": "10.1/c", "title": "Gamma", "journal": "MISQ", "abbr": "MISQ"}, C)):
    dl.save_index(p, f, "ok")
kA = dl.index_key({"doi": "10.1/a"})
fid = fo.create("Lab")["id"]; fo.add(fid, [kA, dl.index_key({"doi": "10.1/c"})])
rs.update(kA, {"status": "reading"})
uiprefs.update({"jw-pos:" + A: '{"n": 3}', "jw-pos:" + C: '{"n": 1}'}, [])
sha = ns.sha1_of(pa)
os.makedirs(bl.INDEX, exist_ok=True)
for ext in (".blocks.json", ".vec", ".vec.json", ".refs.json"):
    open(os.path.join(bl.INDEX, sha + ext), "w").write("x")
r1 = deletion.delete_item(A)
out["a"] = {"res": r1, "left": [p for p in (A, A[:-4] + ".md", os.path.relpath(ns.notes_path(pa), ROOT), os.path.relpath(ns.card_path(pa), ROOT)) if os.path.exists(os.path.join(ROOT, p))],
            "index_left": [f for f in os.listdir(bl.INDEX) if f.startswith(sha)], "folder": fo.get(fid)["papers"], "status": kA in rs.load(),
            "pos": sorted(k for k in uiprefs.get()["prefs"] if k.startswith("jw-pos:")), "library": sorted(r["file"] for r in pl.library()),
            "row": [r for r in csv.DictReader(open(os.path.join(paths.PAPERS, "_index.csv"))) if r["doi"] == "10.1/a"][0]}
r2 = deletion.delete_item(B)
out["b"] = {"res": r2, "left": [p for p in (B, B[:-4] + ".epub", B[:-4] + ".book.json") if os.path.exists(os.path.join(ROOT, p))], "gamma": os.path.exists(os.path.join(ROOT, C))}
put("Papers/_removed/2025/x.pdf", b"%%PDF"); put("Papers/2025-01/MISQ/notindex.pdf", b"%%PDF")
dl.save_index({"doi": "10.1/r", "title": "Set aside"}, "Papers/_removed/2025/x.pdf", "ok")      # (a damaged index pointing into Papers/_removed: still not a library paper)
errs = {}
for bad in ("../outside.pdf", "Papers/_removed/2025/x.pdf", A, "Papers/2025-01/MISQ/Alpha.md", "Papers/2025-01/MISQ/notindex.pdf", "", None):
    try:
        deletion.delete_item(bad); errs[str(bad)] = "accepted"
    except ValueError as e:
        errs[str(bad)] = "refused"
out["errs"] = errs
out["gone"] = sorted(deletion.deleted_keys())
# a later run: the deleted paper is marked and never downloaded
run = pl.Run("picks", {"label": "after"})
run.add_paper({"id": "p1", "doi": "10.1/a", "title": "Alpha", "verdict": "download", "publisher": "x", "abbr": "MISQ", "journal": "MISQ"})
run.add_paper({"id": "p2", "doi": "10.1/new", "title": "New one", "verdict": "download", "publisher": "x", "abbr": "MISQ", "journal": "MISQ"})
pl.mark_downloaded(run)
out["marked"] = {i: run.papers[i].get("dl_status") for i in ("p1", "p2")}
asked = []
class _Br:
    def __init__(self, **k): pass
    def __enter__(self): return None
    def __exit__(self, *a): return False
def fake_dl(p, env, browser=None, dry=False):
    asked.append(p["id"]); f = put("Papers/2025-01/MISQ/%%s.pdf" %% p["id"], b"%%PDF"); return True, f, "browser"
pl.dl.download_paper, pl.dl.open_browser, pl.summarize = fake_dl, _Br, (lambda p, path, usage: "summary")
run.papers["p1"]["dl_status"] = None                       # (as if the run had been made before the paper was deleted)
pl.download_and_summarize(run, ["p1", "p2"], lambda *a, **k: None, threading.Event(), sleep=lambda d: None, clock=lambda: 0.0)
out["downloaded"] = asked; out["after_dl"] = run.papers["p1"].get("dl_status")
# you add it yourself again: it is back, and runs see it as downloaded
put(A, b"%%PDF-alpha-again"); dl.save_index({"doi": "10.1/a", "title": "Alpha", "journal": "MISQ", "abbr": "MISQ"}, A, "ok")
pl.mark_downloaded(run)
out["readded"] = {"gone": kA in deletion.deleted_keys(), "status": run.papers["p1"].get("dl_status"), "library": A in [r["file"] for r in pl.library()]}
# a past run's record
run.save(force=True)
rp = os.path.join(pl.RUNS, run.id + ".json")
t = deletion.delete_run(run.id)
runerr = []
for bad in (run.id, "../x", ""):
    try:
        deletion.delete_run(bad); runerr.append("accepted")
    except ValueError:
        runerr.append("refused")
out["run"] = {"title": t, "file_left": os.path.exists(rp), "again": runerr, "library_kept": A in [r["file"] for r in pl.library()]}
# an API key
open(os.path.join(ROOT, ".env"), "a").write("OPENAI_API_KEY=sk-test-123\n")
c1 = ai.config()["providers"]["openai"]["configured"]
cfg = ai.remove_key("openai")
out["key"] = {"before": c1, "after": cfg["providers"]["openai"]["configured"], "note": cfg.get("key_note"), "env_file": "OPENAI_API_KEY" in open(os.path.join(ROOT, ".env")).read(),
              "again": ai.remove_key("openai").get("key_note")}
try:
    ai.remove_key("ollama"); out["key"]["ollama"] = "accepted"
except ValueError:
    out["key"]["ollama"] = "refused"
# undo: a deleted paper is kept for 30 days and can be put back with its notes, folders and reading status; after that it is gone
import time as _t
C2 = "Papers/2025-01/MISQ/Delta.pdf"; pd2 = put(C2, b"%%PDF-delta"); open(ns.notes_path(pd2), "w").write('{"version": 1, "rev": 3, "highlights": [{"id": "h"}]}')
dl.save_index({"doi": "10.1/d", "title": "Delta", "journal": "MISQ", "abbr": "MISQ"}, C2, "ok")
fo.add(fid, ["10.1/d"]); rs.update("10.1/d", {"status": "read"})
rd = deletion.delete_item(C2)
u = {"token": bool(rd.get("undo")), "gone": not os.path.exists(pd2) and not os.path.exists(ns.notes_path(pd2)), "listed": [x["title"] for x in deletion.recently_deleted() if x["token"] == rd["undo"]],
     "not_in_library": C2 not in [r["file"] for r in pl.library()], "out_of_folder": "10.1/d" not in fo.get(fid)["papers"]}
back = deletion.restore(rd["undo"])
row = [r for r in pl.library() if r["file"] == C2]
u.update(back=back["file"] == C2 and os.path.exists(pd2) and '"rev": 3' in open(ns.notes_path(pd2)).read(), row=bool(row), folder="10.1/d" in fo.get(fid)["papers"],
         status=(row[0]["state"] or {}).get("status") if row else None, not_deleted="10.1/d" not in deletion.deleted_keys(), list_empty=not [x for x in deletion.recently_deleted() if x["token"] == rd["undo"]])
try:
    deletion.restore(rd["undo"]); u["twice"] = "accepted"
except ValueError:
    u["twice"] = "refused"
for bad in ("../x", "deleted-20200101-000000-zzzzzz", ""):
    try:
        deletion.restore(bad); u["bad"] = "accepted"
    except ValueError:
        u.setdefault("bad", "refused")
rd2 = deletion.delete_item(C2)
mf = os.path.join(dl.PAPERS, "_removed", rd2["undo"], "_undo.json"); m_ = json.load(open(mf)); m_["when"] = _t.time() - 31 * 86400; json.dump(m_, open(mf, "w"))
u["old_not_listed"] = not [x for x in deletion.recently_deleted() if x["token"] == rd2["undo"]]
u["purged"] = deletion.purge_old() == 1 and not os.path.exists(os.path.join(dl.PAPERS, "_removed", rd2["undo"]))
out["undo"] = u
print("JSON" + json.dumps(out))
""" % HERE
            env = {k: v for k, v in os.environ.items() if k not in ("JW_HOME", "JW_LOCAL", "OPENAI_API_KEY")}
            r = subprocess.run([sys.executable, "-c", child], env=dict(env, JW_HOME=home, JW_LOCAL=loc, JW_NO_KEYCHAIN="1"), capture_output=True, text=True, timeout=180)
            assert r.returncode == 0, r.stderr[-1500:]
            o = json.loads(r.stdout.split("JSON", 1)[1])
            assert o["undo"] == {"token": True, "gone": True, "listed": ["Delta"], "not_in_library": True, "out_of_folder": True, "back": True, "row": True, "folder": True, "status": "read",
                                 "not_deleted": True, "list_empty": True, "twice": "refused", "bad": "refused", "old_not_listed": True, "purged": True}, o["undo"]
            a, b = o["a"], o["b"]
            assert a["res"]["kind"] == "paper" and a["res"]["title"] == "Alpha" and len(a["res"]["deleted"]) == 4 and a["left"] == [], a
            assert a["index_left"] == [], "its passages in the search index are deleted too"
            assert a["folder"] == ["10.1/c"] and a["status"] is False, "out of its folder, no reading status"
            assert a["pos"] == ["jw-pos:Papers/2025-01/MISQ/Gamma.pdf"], "where you stopped reading it is forgotten (the other paper's stays)"
            assert a["library"] == ["Papers/2025-01/MISQ/Gamma.pdf", "Papers/Books/Beta.pdf"], a["library"]
            assert a["row"]["status"] == "deleted" and a["row"]["file"] == "" and a["row"]["note"].startswith("deleted by you"), a["row"]
            assert b["res"]["kind"] == "book" and b["left"] == [] and b["gamma"] is True, "a book: its PDF, EPUB and details go; the other papers stay"
            assert all(v == "refused" for v in o["errs"].values()), o["errs"]
            assert o["gone"] == sorted(["10.1/a", "beta, a book"]), o["gone"]
            assert o["marked"] == {"p1": "deleted", "p2": None}, o["marked"]
            assert o["downloaded"] == ["p2"] and o["after_dl"] == "deleted", "a run never downloads a paper you deleted"
            assert o["readded"] == {"gone": False, "status": "ok", "library": True}, o["readded"]
            assert o["run"]["file_left"] is False and o["run"]["again"] == ["refused"] * 3 and o["run"]["library_kept"] is True, o["run"]
            k = o["key"]
            assert k["before"] is True and k["after"] is False and k["env_file"] is False and "Removed from the .env file" in k["note"], k
            assert k["again"].startswith("There was no saved key") and k["ollama"] == "refused", k
        finally:
            shutil.rmtree(home, ignore_errors=True); shutil.rmtree(loc, ignore_errors=True)
    check("deleting what you added: a paper or a book is deleted for good (files, notes, summary, card, search index, folders, reading status, reading position) and never downloaded again by a run, adding it yourself brings it back, bad paths are refused, a past run's record can be deleted, and an API key removed", t_delete_items)


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

    def t_import_review():
        """A dropped PDF is analysed before anything is filed: the records for the DOIs it prints and for its title, each checked against the PDF (a DOI of a CITED paper, or a
        record of another paper, does not win); the PDF waits under a token; confirming files it with the details you gave; cancelling forgets it."""
        import csv, pymupdf
        right = {"title": "Governing Open Platforms Through Transparent Rules", "authors": ["Jane Doe", "Richard Roe"], "container": "Journal of Open Platforms", "issn": [], "doi": "10.5555/right",
                 "pub_date": "2024-05", "url": "https://doi.org/10.5555/right", "volume": "", "issue": "", "pages": "", "publisher": "P", "type": "journal-article", "abstract": None, "keywords": []}
        cited = dict(right, title="A Totally Different Study of Hotel Reviews", authors=["Zed Quinn"], doi="10.5555/cited", url="https://doi.org/10.5555/cited")
        searched = []
        saved = (importer.crossref_meta, importer.crossref_search)
        importer.crossref_meta = lambda d: {"10.5555/right": right, "10.5555/cited": cited}.get(d.lower())
        importer.crossref_search = lambda t, rows=4: searched.append(t) or ([cited, right] if "governing" in t.lower() else [])
        def pdf(title, extra="", authors="Jane Doe and Richard Roe"):
            d = pymupdf.open(); pg = d.new_page()
            if title:
                pg.insert_textbox(pymupdf.Rect(50, 50, 545, 120), title, fontsize=20); pg.insert_textbox(pymupdf.Rect(50, 125, 545, 150), authors, fontsize=11)
            pg.insert_textbox(pymupdf.Rect(50, 160, 545, 780), (extra + " ") + "We study how platform owners govern complementors with transparent rules. " * 25, fontsize=10)
            for _ in range(60):                                          # (a PDF under 40 kB is not taken for a complete one)
                d.new_page().insert_textbox(pymupdf.Rect(60, 60, 540, 780), "\n".join(os.urandom(20).hex() for _ in range(60)), fontsize=8)
            data = d.tobytes(); d.close(); return data
        staged = lambda t: os.path.join(importer.STAGING, t + ".pdf")
        try:
            # a PDF that prints its own DOI and title: one record, found by the DOI, high confidence, nothing filed yet
            n_idx = len(list(csv.DictReader(open(os.path.join(PAPERS, "_index.csv")))))
            a = importer.analyse_pdf(pdf(right["title"], "doi: 10.5555/right"), "paper (1).pdf")
            c0 = a["candidates"][0]
            assert a["confidence"] == "high" and a["pick"] == 0 and c0["source"] == "doi" and c0["doi_printed"] and c0["verdict"] == "match" and c0["doi"] == "10.5555/right" and not a["existing"], a
            assert a["pdf"]["dois"] == ["10.5555/right"] and a["pdf"]["title"].startswith("Governing Open Platforms") and os.path.exists(staged(a["token"])) and not searched
            assert len(list(csv.DictReader(open(os.path.join(PAPERS, "_index.csv"))))) == n_idx, "analysing files nothing"
            # the right title and no DOI: found by the title; the other paper Crossref also returns first is ranked below it
            b = importer.analyse_pdf(pdf(right["title"]), "x.pdf")
            assert searched and b["candidates"][0]["doi"] == "10.5555/right" and b["candidates"][0]["source"] == "title" and b["confidence"] == "high" and len(b["candidates"]) == 2, b["candidates"]
            assert b["candidates"][1]["doi"] == "10.5555/cited" and b["candidates"][1]["verdict"] != "match"
            # a PDF that prints the DOI of a paper it only cites: that record does not win, the title search finds the real one
            c = importer.analyse_pdf(pdf(right["title"], "as shown by Quinn, doi 10.5555/cited"), "y.pdf")
            assert c["pdf"]["dois"] == ["10.5555/cited"] and c["candidates"][0]["doi"] == "10.5555/right" and c["confidence"] == "high", c["candidates"]
            # nothing readable and nothing found: no candidate, nothing preselected, the file name is the title to start from
            blank = pymupdf.open(); [blank.new_page().draw_rect(pymupdf.Rect(50, 50, 200, 200)) for _ in range(3)]; blank.embfile_add("scan", os.urandom(60000)); blank_data = blank.tobytes(); blank.close()      # pages with no text at all
            e = importer.analyse_pdf(blank_data, "my scan (3).pdf")
            assert e["confidence"] == "none" and e["pick"] == -1 and e["candidates"] == [] and e["pdf"]["title"] == "my scan", e
            # a DOI Crossref does not know and a title it has nothing for: still staged, to be typed by hand
            # confirming files it with the details you gave (here corrected), the way the pipeline files its papers, and forgets the staged copy
            r = importer.confirm(a["token"], {"title": "Governing Open Platforms through Transparent Rules (corrected)", "authors": "Jane Doe; Richard Roe", "journal": "Journal of Open Platforms",
                                               "pub_date": "2024-05", "doi": "https://doi.org/10.5555/right", "source": "edited"})
            row = list(csv.DictReader(open(os.path.join(PAPERS, "_index.csv"))))[-1]
            assert r["file"].startswith("Papers/2024-05/Journal of Open Platforms/2024-05_") and os.path.exists(os.path.join(TMP, r["file"])) and not r["existing"], r
            assert row["doi"] == "10.5555/right" and row["authors"] == "Jane Doe; Richard Roe" and "edited by you" in row["note"] and row["title"].endswith("(corrected)") and not os.path.exists(staged(a["token"]))
            try:
                importer.confirm(a["token"], {"title": "again"}); raise AssertionError("a token works once")
            except ValueError as ex:
                assert "no longer waiting" in str(ex)
            # the same paper dropped again is recognised
            again = importer.analyse_pdf(pdf(right["title"], "doi: 10.5555/right"), "copy.pdf")
            assert again["existing"] == r["file"], again["existing"]
            # typed by hand (no record), no date: filed under today's month; a title is required; a token must be a token
            h = importer.confirm(e["token"], {"title": "My own working paper", "authors": ["Me"], "journal": "", "pub_date": "", "doi": "", "source": "hand"})
            row = list(csv.DictReader(open(os.path.join(PAPERS, "_index.csv"))))[-1]
            assert h["file"].startswith("Papers/") and "typed by you" in row["note"] and row["doi"] == "" and row["authors"] == "Me", (h, row)
            for bad in ({"title": "  "}, {}, None):
                try:
                    importer.confirm(b["token"], bad); raise AssertionError("accepted %r" % (bad,))
                except ValueError as ex:
                    assert "title" in str(ex)
            for bad_t in ("../../etc/passwd", "", None, "A" * 32, b["token"][:-1], 12):
                try:
                    importer.confirm(bad_t, {"title": "x"}); raise AssertionError("accepted token %r" % (bad_t,))
                except ValueError:
                    pass
            assert os.path.exists(staged(b["token"])), "a refused confirm keeps the staged PDF"
            # cancelling forgets staged PDFs (and only those); old ones are forgotten by the next analysis
            assert importer.discard([b["token"], c["token"], "../x", None, "z" * 32]) == 2 and not os.path.exists(staged(b["token"])) and not os.path.exists(staged(c["token"]))
            old = staged("0" * 32); open(old, "wb").write(b"%PDF-old"); os.utime(old, (1, 1))
            importer.analyse_pdf(pdf(right["title"]), "z.pdf"); assert not os.path.exists(old)
            try:
                importer.analyse_pdf(b"not a pdf", "x.pdf"); raise AssertionError("expected ValueError")
            except ValueError:
                pass
        finally:
            importer.crossref_meta, importer.crossref_search = saved
            shutil.rmtree(importer.STAGING, ignore_errors=True)
    check("drop a PDF: it is analysed before anything is filed (records by its printed DOI and by its title, each checked against the PDF; a cited paper's DOI does not win), confirm files it with your details, a token works once, cancel forgets it", t_import_review)
    def t_kinds():
        """A paper you add can be a journal article, a preprint or working paper, a conference paper, or a report, thesis or other (kinds.py): its type comes from the
        Crossref record, or from what the PDF prints (an arXiv stamp beats a published version found only by its title); it is filed under its own venue with a short
        abbreviation, and kept in <stem>.paper.json (never a column of the index, which older versions refuse); the library lists it with its type; Change type writes
        only that file; a stale file from another paper is ignored; the index writers keep columns they do not know."""
        import csv, io, pymupdf, kinds as kd, deletion as dele_, identity as idy, smart as sm_, library_search as ls_, pipeline as pl_
        pre = {"title": "Platform Rules and Entry: Evidence from App Stores", "authors": ["Jane Doe"], "container": "", "issn": [], "doi": "10.2139/ssrn.4400001", "pub_date": "2024-05",
               "url": "https://doi.org/10.2139/ssrn.4400001", "volume": "", "issue": "", "pages": "", "publisher": "Elsevier BV", "type": "posted-content", "group_title": "SSRN", "abstract": None, "keywords": []}
        pub = dict(pre, title="Transparent Governance of Digital Platforms", doi="10.9999/pub.1", url="https://doi.org/10.9999/pub.1", type="journal-article", container="Journal of Platform Studies", group_title="")
        saved = (importer.crossref_meta, importer.crossref_search, dl.PAPERS, dl.ROOT)
        importer.crossref_meta = lambda d: {pre["doi"]: pre}.get(d.lower())
        importer.crossref_search = lambda t, rows=4: [pub] if "transparent governance" in t.lower() else []
        dl.PAPERS, dl.ROOT = pl_.PAPERS, pl_.ROOT
        idx_ = os.path.join(dl.PAPERS, "_index.csv"); saved_idx = open(idx_, encoding="utf-8").read() if os.path.exists(idx_) else None
        def pdf(title, first="", authors="Jane Doe"):
            d = pymupdf.open(); pg = d.new_page()
            pg.insert_textbox(pymupdf.Rect(50, 20, 545, 45), first, fontsize=8)
            pg.insert_textbox(pymupdf.Rect(50, 50, 545, 120), title, fontsize=20); pg.insert_textbox(pymupdf.Rect(50, 125, 545, 150), authors, fontsize=11)
            pg.insert_textbox(pymupdf.Rect(50, 160, 545, 780), "We study how platform owners govern complementors with transparent rules. " * 25, fontsize=10)
            for _ in range(60):
                d.new_page().insert_textbox(pymupdf.Rect(60, 60, 540, 780), "\n".join(os.urandom(20).hex() for _ in range(60)), fontsize=8)
            data = d.tobytes(); d.close(); return data
        try:
            # the record says preprint (and names its series): the dialog gets the type and the venue
            a = importer.analyse_pdf(pdf(pre["title"], "doi: 10.2139/ssrn.4400001"), "pre.pdf")
            assert a["kind"] == "preprint" and a["candidates"][0]["kind"] == "preprint" and a["candidates"][0]["journal"] == "SSRN" and "preprint" in a["kind_hint"], (a["kind"], a["candidates"][:1], a["kind_hint"])
            # confirming it files it under SSRN, abbreviation SSRN, with its type in <stem>.paper.json; the index keeps its columns exactly
            r = importer.confirm(a["token"], {"title": pre["title"], "authors": "Jane Doe", "journal": "SSRN", "pub_date": "2024-05", "doi": pre["doi"], "source": "crossref", "kind": "preprint", "type": "posted-content"})
            assert r["file"].startswith("Papers/2024-05/SSRN/"), r
            side = json.load(open(os.path.join(dl.ROOT, r["file"])[:-4] + ".paper.json"))
            assert side["kind"] == "preprint" and side["for"] == "10.2139/ssrn.4400001" and side["crossref_type"] == "posted-content", side
            rd = csv.DictReader(open(idx_, newline="", encoding="utf-8")); rows = list(rd)
            assert rd.fieldnames == dl.INDEX_COLS and [x for x in rows if x["doi"] == pre["doi"]][0]["abbr"] == "SSRN", (rd.fieldnames, rows[-1])
            lib_ = {x["file"]: x for x in pl_.library()}
            assert lib_[r["file"]]["kind"] == "preprint", lib_[r["file"]].get("kind")
            # an arXiv-stamped PDF whose title finds only the published version: the PDF wins (preprint, arXiv, the journal's DOI is not taken)
            b = importer.analyse_pdf(pdf(pub["title"], "arXiv:2401.01234v2 [cs.CY] 3 Jan 2024"), "arx.pdf")
            assert b["pick"] == -1 and b["kind"] == "preprint" and b["pdf"]["venue"] == "arXiv" and "published version" in b["kind_hint"] and b["candidates"][0]["doi"] == "10.9999/pub.1", (b["pick"], b["kind"], b["kind_hint"])
            importer.discard([b["token"]])
            # a conference paper with no record: the PDF's ICIS footer gives the type and the venue; filed under it with abbreviation ICIS
            c = importer.analyse_pdf(pdf("Algorithmic Pricing on Marketplaces", "Forty-Fourth International Conference on Information Systems, Hyderabad 2023"), "icis.pdf")
            assert c["kind"] == "conference" and c["pdf"]["venue"] == "ICIS 2023" and "conference" in c["kind_hint"], (c["kind"], c["pdf"])
            rc = importer.confirm(c["token"], {"title": "Algorithmic Pricing on Marketplaces", "authors": "Jane Doe", "journal": "ICIS 2023", "pub_date": "2023-12", "doi": "", "source": "hand", "kind": "conference"})
            rowc = [x for x in csv.DictReader(open(idx_, newline="", encoding="utf-8")) if x["file"] == rc["file"]][0]
            assert rc["file"].startswith("Papers/2023-12/ICIS 2023/") and rowc["abbr"] == "ICIS", (rc, rowc)
            # the same venue typed again in another case: the same folder and abbreviation
            d2 = importer.analyse_pdf(pdf("Second ICIS Paper on Fees"), "icis2.pdf")
            rd2 = importer.confirm(d2["token"], {"title": "Second ICIS Paper on Fees", "authors": "Bo Chen", "journal": "icis 2023", "pub_date": "2023-12", "doi": "", "source": "hand", "kind": "conference"})
            assert rd2["file"].startswith("Papers/2023-12/ICIS 2023/"), rd2
            # a journal article's footnote about a talk is not a conference
            e = importer.analyse_pdf(pdf("A Journal Paper About Fees", "An earlier version was presented at the International Conference on Information Systems 2022."), "j.pdf")
            assert e["kind"] == "journal", e["kind"]; importer.discard([e["token"]])
            # the kind sent is checked: missing, "paper" or anything else files a journal article (as before)
            for bad in (None, "", "paper", "zzz", 42):
                g = importer.analyse_pdf(pdf("Kind Check Paper %s" % bad), "k.pdf")
                rg = importer.confirm(g["token"], {"title": "Kind Check Paper %s" % bad, "authors": "X Y", "journal": "Journal of Kind Checks", "pub_date": "2022", "doi": "", "source": "hand", "kind": bad})
                assert json.load(open(os.path.join(dl.ROOT, rg["file"])[:-4] + ".paper.json"))["kind"] == "journal"
            # journal_for: a conference with an AIS DOI and no venue is not filed as JAIS; a watched journal by its exact name is that journal for any kind
            jb = tempfile.mkdtemp(prefix="jw-journals-"); shutil.copy(os.path.join(HERE, "..", "defaults", "journals.json"), jb); base0 = dl.BASE; dl.BASE = jb
            assert importer.journal_for({"container": "", "doi": "10.17705/9conf.1", "issn": []}, "conference") == ("Conference papers", "CONF")
            assert importer.journal_for({"container": "", "doi": "10.17705/1jais.1", "issn": []}, "journal")[1] == "JAIS"
            assert importer.journal_for({"container": "", "doi": "10.3386/w31234", "issn": []}, "preprint")[1] == "NBER"
            assert importer.journal_for({"container": "MS", "doi": "", "issn": []}, "conference")[1] != "MS" and importer.journal_for({"container": "Management Science", "doi": "", "issn": []}, "conference")[1] == "MS"
            assert importer.journal_for({"container": "Some Journal", "doi": "", "issn": ["1536-9323"]}, "journal")[1] == "JAIS", "the ISSN decides"
            dl.BASE = base0
            # Change type writes only the sidecar: the index is byte-identical
            before = open(idx_, "rb").read()
            kd.write_sidecar(os.path.join(dl.ROOT, rd2["file"]), "other", "ICIS 2023", "Report", dl.index_key([x for x in csv.DictReader(open(idx_, newline="", encoding="utf-8")) if x["file"] == rd2["file"]][0]))
            assert open(idx_, "rb").read() == before and {x["file"]: x for x in pl_.library()}[rd2["file"]]["kind_label"] == "Report"
            # a stale sidecar (written for another paper) is ignored; with none, a DOI of a preprint series still reads as a preprint
            json.dump({"kind": "conference", "for": "10.1/other"}, open(os.path.join(dl.ROOT, r["file"])[:-4] + ".paper.json", "w"))
            assert {x["file"]: x for x in pl_.library()}[r["file"]]["kind"] == "preprint", "the stale file is ignored; SSRN's DOI says preprint"
            assert kd.of_row({"journal": "Books", "abbr": "BOOK"}) == ("book", "") and kd.of_row({"journal": "X", "doi": "10.1/x"}) == ("journal", "")
            # the type filter, and a saved search keeps it
            rows_ = pl_.library()
            hits_ = ls_.search(rows_, kind="conference"); hits_ = hits_.get("results", hits_) if isinstance(hits_, dict) else hits_
            assert {x["file"] for x in hits_} == {rc["file"]}, [x["file"] for x in hits_]
            assert sm_.clean_filters({"kind": "preprint", "q": "x"}).get("kind") == "preprint"
            # ordinary journal articles are not theses or conference papers because a footnote says so (a wrapped one too); a preprint server's subject is not its venue
            body_ = "A study of platforms\nJane Doe\n" + "text of the paper\n" * 30
            assert kd.from_text(body_ + "This paper is based on the first author's doctoral dissertation.\nJournal of X 2020") == (None, "", "")
            assert kd.from_text(body_ + "An earlier version was presented at the Fortieth\nInternational Conference on Information Systems, Munich, 2019.") == (None, "", "")
            assert kd.from_text("A thesis submitted for the degree of Doctor of Philosophy\nJane Doe") == ("other", "", "Thesis")
            assert kd.from_text(body_ + "the next section of this paper, which was accepted\nForty-Fourth International Conference on Information Systems, Hyderabad, India 2023\n1") == ("conference", "ICIS 2023", ""), "a real footer stays a conference whatever the body line above it says"
            assert kd.from_text("A study\n* This article is based on the first author's doctoral dissertation, submitted in partial fulfillment of the requirements for the PhD at X University.\n" + "t\n" * 30) == (None, "", "")
            assert kd.from_text("Aalto University\nDOCTORAL DISSERTATIONS 12/2020\nJane Doe") == ("other", "", "Thesis")
            assert kd.from_crossref({"type": "posted-content", "doi": "10.1101/2020.01.01.1", "group_title": "Neuroscience", "publisher": "Cold Spring Harbor Laboratory"})[1] != "Neuroscience"
            # a paper added by its DOI (or a dropped PDF filed at once) is filed under its series, as in the review dialog: SSRN, not "SSRN Electronic Journal"
            old_rec = dict(pre, doi="10.2139/ssrn.4400002", url="https://doi.org/10.2139/ssrn.4400002", type="journal-article", container="SSRN Electronic Journal", group_title="")
            import open_copies as oc_
            keep_ = (importer.crossref_meta, importer._file, dl.try_unpaywall, oc_.find_open_copy)
            try:
                importer.crossref_meta = lambda d: old_rec if d.lower() == old_rec["doi"] else None
                importer._file = lambda p, data, note, after=None: {"p": p}
                dl.try_unpaywall = lambda *a, **k: None
                oc_.find_open_copy = lambda *a, **k: (b"%PDF", "an open copy")
                got_ = importer.add_doi(old_rec["doi"])["p"]
                assert (got_["journal"], got_["abbr"]) == ("SSRN", "SSRN"), (got_["journal"], got_["abbr"])
            finally:
                importer.crossref_meta, importer._file, dl.try_unpaywall, oc_.find_open_copy = keep_
            keep_ = (ls_.notes_of, ls_.summary_of, ls_.card_text)       # words found in a paper's highlights do not hide the papers after it (a loop once reused the name of the type filter)
            try:
                ls_.notes_of = lambda r: {"highlights": [{"text": "a zebra crossing"}], "cards": []}; ls_.summary_of = ls_.card_text = lambda r: ""
                three_ = [{"file": "Papers/a%d.pdf" % i, "title": "T%d" % i, "authors": "", "journal": "J", "abbr": "J"} for i in range(3)]
                assert [x["file"] for x in ls_.search(three_, "zebra")] == [r_["file"] for r_ in three_], "every paper whose highlight has the words is found"
                assert [x["file"] for x in ls_.search(three_, "zebra", kind="journal")] == [r_["file"] for r_ in three_]
            finally:
                ls_.notes_of, ls_.summary_of, ls_.card_text = keep_
            # the sidecar goes with the paper when it is deleted
            sp = os.path.join(dl.ROOT, rc["file"])[:-4] + ".paper.json"
            assert os.path.exists(sp) and sp in __import__("notes_store").sidecars(os.path.join(dl.ROOT, rc["file"]))
            # the index writers keep a column a later version adds (an older writer refused it: two computers on different versions)
            hdr = open(idx_, encoding="utf-8").read().split("\n", 1)
            open(idx_, "w", encoding="utf-8").write(hdr[0] + ",future\n" + "\n".join(l + ",kept" if l.strip() else l for l in hdr[1].split("\n")))
            dl.save_index({"doi": "10.9/fut", "title": "Future Column Paper", "journal": "J", "abbr": "J"}, "", "deleted")
            rd = csv.DictReader(open(idx_, newline="", encoding="utf-8")); rows = list(rd)
            assert rd.fieldnames[-1] == "future" and all(x["future"] == "kept" for x in rows if x["doi"] != "10.9/fut"), rd.fieldnames
            old_paths = (idy.paths.ROOT, idy.paths.PAPERS); idy.paths.ROOT, idy.paths.PAPERS = dl.ROOT, dl.PAPERS
            try:
                gone = idy.remove(rd2["file"])
            finally:
                idy.paths.ROOT, idy.paths.PAPERS = old_paths
            assert gone["kind"] == "other" and gone["label"] == "Report", gone        # (its type goes with the download that replaces it)
            rd = csv.DictReader(open(idx_, newline="", encoding="utf-8")); rows = list(rd)
            assert "future" in rd.fieldnames and all(x["future"] == "kept" for x in rows if x["doi"] != "10.9/fut"), "identity.remove keeps it too"
        finally:
            importer.crossref_meta, importer.crossref_search, dl.PAPERS, dl.ROOT = saved
            if saved_idx is None:
                os.path.exists(idx_) and os.remove(idx_)
            else:
                open(idx_, "w", encoding="utf-8").write(saved_idx)
    check("paper kinds: a paper you add is a journal article, a preprint, a conference paper or other (from Crossref, or from the PDF's stamp or footer; an arXiv stamp beats a published version found by title), filed under its venue with a short abbreviation, kept next to the PDF (not in the index), listed with its type, changed without touching the index; the index writers keep unknown columns", t_kinds)
    def t_refextract():
        """References in a document you have (Word, PowerPoint, PDF, text): found by their shape (no AI, nothing sent), each looked up on Crossref and CHECKED against
        what the document prints; a record that only resembles it is 'unsure', a book is not downloaded, a sentence that is not a reference is dropped, an answer
        Crossref did not give is asked for again and then said plainly; the AI is a backup whose cost is known first and whose list is checked the same way."""
        import io, zipfile, pymupdf, refextract as rx, ai as ai_
        REC = {
            "koning": {"title": "Experimentation and Start-up Performance: Evidence from A/B Testing", "authors": ["Rembrand Koning", "Sharique Hasan"], "container": "Management Science", "doi": "10.1287/mnsc.2021.4209", "pub_date": "2022-09", "type": "journal-article"},
            "koning_wp": {"title": "Experimentation and Start-up Performance: Evidence from A/B Testing", "authors": ["Rembrand Koning"], "container": "", "doi": "10.2139/ssrn.3440291", "pub_date": "2019-08", "type": "posted-content"},
            "wiki": {"title": "Wikipedia matters", "authors": ["Marit Hinnosaar"], "container": "Journal of Economics & Management Strategy", "doi": "10.1111/jems.12421", "pub_date": "2021-07", "type": "journal-article"},
            "cui": {"title": "The Effects of Generative AI on High-Skilled Work: Evidence from Three Field Experiments with Software Developers", "authors": ["Kevin Zheyuan Cui"], "container": "Management Science", "doi": "10.1287/mnsc.2025.00535", "pub_date": "2026-01", "type": "journal-article"},
            "lookalike": {"title": "Design and analysis of switchback experiments in ride sharing", "authors": ["Someone Else"], "container": "Other Journal", "doi": "10.9999/other", "pub_date": "2011-01", "type": "journal-article"},
            "book": {"title": "Trustworthy Online Controlled Experiments", "authors": ["Ron Kohavi"], "container": "", "doi": "10.1017/9781108653985", "pub_date": "2020-03", "type": "book"},
            "ieee": {"title": "Deep residual learning for image recognition", "authors": ["Kaiming He"], "container": "CVPR", "doi": "10.1109/cvpr.2016.90", "pub_date": "2016-06", "type": "proceedings-article"},
            "doi": {"title": "A paper known by its DOI", "authors": ["Ann Lee"], "container": "ISR", "doi": "10.1287/isre.2024.0001", "pub_date": "2024-01", "type": "journal-article"},
        }
        asked, fail = [], {"n": 0}
        def fake_search(q, rows=4, strict=False):
            asked.append(q)
            if fail["n"]:
                fail["n"] -= 1
                if strict:
                    raise OSError("Crossref did not answer")
                return []
            ql = q.lower()
            hits = [REC[k] for k in ("koning_wp", "koning") if "start-up performance" in ql] + [REC["wiki"]] * ("wikipedia matters" in ql) + [REC["cui"]] * ("high-skilled work" in ql) \
                + [REC["lookalike"]] * ("switchback" in ql) + [REC["book"]] * ("trustworthy online" in ql) + [REC["ieee"]] * ("residual learning" in ql)
            return [dict(h) for h in hits]
        keep = (importer.crossref_search, importer.crossref_meta, rx.RETRY_WAIT, ai_.chat)
        importer.crossref_search, importer.crossref_meta, rx.RETRY_WAIT = fake_search, (lambda d: dict(REC["doi"]) if d.lower() == REC["doi"]["doi"] else None), 0
        try:
            APA = "Koning, R., Hasan, S., and Chatterji, A. (2022). Experimentation and start-up performance: Evidence from A/B testing. Management Science, 68, 6434-6453. Start-ups that adopted A/B testing tools grew 30% to 100% more after a year. " + "It is short and motivating, and it is also observational. " * 4 + "Zzfarword ends the note."
            paras_xml = "".join("<w:p><w:r><w:t>%s</w:t></w:r></w:p>" % t for t in [
                "Week 1. Why experiment? Causality in 2026",
                APA.replace("&", "&amp;"),
                "Hinnosaar, M., Hinnosaar, T., Kummer, M., and Slivko, O. (2023). Wikipedia matters. Journal of Economics &amp; Management Strategy, 32(3), 657-669. Kummer&apos;s own field experiment.",
                "Cui, K. Z., Demirer, M., Jaffe, S., Musolff, L., Peng, S., and Salz, T. The effects of generative AI on high-skilled work: Evidence from three field experiments with software developers. Management Science, forthcoming.",
                "Bojinov, I., Simchi-Levi, D., and Zhao, J. (2023). Design and analysis of switchback experiments. Management Science, 69, 3759-3777.",
                "Kohavi, R., Tang, D., and Xu, Y. (2020). Trustworthy online controlled experiments. Cambridge University Press.",
                "Peeking gets a remedy, not only a warning. Fixed horizons, or always-valid sequential tests (Johari et al. 2022).",
                "Frick, Belo and Telang showed in 2023 that incentives were misaligned.",
                "Nobody, A., and Unknown, B. (2021). A working paper nobody has indexed anywhere. Unpublished manuscript.",
            ]) + '<w:p><w:hyperlink r:id="rId9"><w:r><w:t>Lee (2024), the linked paper</w:t></w:r></w:hyperlink></w:p>'
            zb = io.BytesIO()
            with zipfile.ZipFile(zb, "w") as z:
                z.writestr("word/document.xml", '<w:document xmlns:w="w" xmlns:r="r"><w:body>%s</w:body></w:document>' % paras_xml)
                z.writestr("word/_rels/document.xml.rels", '<Relationships><Relationship Id="rId9" Type="hyperlink" Target="https://doi.org/10.1287/isre.2024.0001" TargetMode="External"/></Relationships>')
                z.writestr("word/footnotes.xml", '<w:footnotes xmlns:w="w"><w:p><w:r><w:t>A footnote, no reference.</w:t></w:r></w:p></w:footnotes>')
            docx = zb.getvalue()
            ps = rx.paragraphs(docx, "syllabus.docx")
            assert len(ps) == 11 and "Kummer's own" in ps[2] and ps[9].endswith("https://doi.org/10.1287/isre.2024.0001") and ps[10].startswith("A footnote"), ps
            cs = rx.candidates(ps)
            assert not any(c.startswith(("Week 1", "Peeking", "A footnote")) for c in cs) and any(c.startswith("Cui, K. Z.") for c in cs) and any("Lee (2024)" in c for c in cs), cs
            pr = rx.parse_ref(APA)
            assert pr["year"] == "2022" and pr["title"] == "Experimentation and start-up performance: Evidence from A/B testing" and pr["venue"] == "Management Science" and pr["cut"] <= len(APA), pr
            assert rx.parse_ref(cs[2])["title"].startswith("The effects of generative AI on high-skilled work") and rx.parse_ref(cs[2])["year"] == "", rx.parse_ref(cs[2])
            msgs = list(rx.extract_stream(docx, "syllabus.docx"))
            done = msgs[-1]
            assert done["type"] == "done" and [m for m in msgs if m["type"] == "status" and m.get("total") == len(cs) and m.get("step") == 2], msgs[:3]
            E = {e["doi"] or e["title"]: e for e in done["entries"]}
            k = E["10.1287/mnsc.2021.4209"]
            assert k["status"] == "found" and k["journal"] == "Management Science" and "10.2139/ssrn.3440291" not in E, "the published article, not the working paper of the same title"
            assert E["10.1111/jems.12421"]["status"] == "found", "a two-word title, online 2021 and printed 2023: the author and the year (two either way) make it found"
            assert E["10.1287/mnsc.2025.00535"]["status"] == "found", "a forthcoming paper with no year is found by its title and author"
            assert E["10.9999/other"]["status"] == "unsure", "a record whose title only resembles it (other authors, another year) is not taken as found"
            assert E["10.1017/9781108653985"]["status"] == "book"
            assert E["10.1287/isre.2024.0001"]["status"] == "found" and "DOI" in E["10.1287/isre.2024.0001"]["why"], "a link to doi.org in the document is asked for directly"
            nb = E["A working paper nobody has indexed anywhere"]
            assert nb["status"] == "none" and nb["year"] == "2021" and not nb["doi"], nb
            assert not any("Frick" in (e["ref"] or "") for e in done["entries"]), "a sentence that only begins like a reference is dropped"
            assert all(len(q) <= 400 for q in asked) and not any("zzfarword" in q.lower() for q in asked), "the reference's own words go to Crossref, not the notes after it"
            assert done["ai"]["cost"] is not None and done["ai"]["model"] and done["candidates"] == len(cs), done["ai"]
            # the check itself
            assert rx.check(REC["wiki"], "Smith, J. (2022). Wikipedia matters. Journal X.")[0] != "found", "a two-word title with another author is not found, though the year fits"
            assert rx.check(REC["wiki"], "Hinnosaar, M. (2021). Wikipedia matters. JEMS.")[0] == "found", "a two-word title needs both the author and the year"
            assert rx.check(REC["wiki"], "Completely different words about platforms (1999).")[0] == ""
            assert rx.check(REC["koning"], "Koning, R. (2022). Experimentation and start-up performance. Management Science.")[0] == "found", "the subtitle left out"
            assert rx.check(REC["koning"], "Experimentation and start-up performance: Evidence from A/B testing")[0] == "unsure", "the title alone, no author and no year: not sure"
            # another paper is not "found": a record whose title is only part of the reference's title, the journal's name, a "Reply"; the author is looked for among the authors
            KIM = "Kim, S. (2020). Trust in online markets for used goods. Journal of Management Information Systems, 37(1), 1-20."
            assert rx.check({"title": "Trust in Online Markets", "authors": ["Zed Quill"], "pub_date": "2019"}, KIM)[0] == "unsure"
            assert rx.check({"title": "Management Information Systems", "authors": ["Zed Quill"], "pub_date": "2021"}, KIM)[0] == "", "the journal's name is not the title"
            assert rx.check({"title": "Trust in online markets for used goods: Reply", "authors": ["Other One"], "pub_date": "2021"}, KIM)[0] != "found"
            assert rx.check({"title": "Trust in online markets when he sells", "authors": ["Li He"], "pub_date": "2012"}, "Kim, S. (2020). Trust in online markets when he sells. JMIS.")[0] == "unsure", "'He' is a word of the title, not one of the authors"
            assert rx.check({"title": "What’s trending in difference-in-differences? A synthesis of the recent econometrics literature", "authors": ["Jonathan Roth"], "pub_date": "2023-08"},
                            "Roth, J., and Poe, J. (2023). What's trending in difference-in-differences? A synthesis of the recent econometrics literature. Journal of Econometrics, 235.")[0] == "found", "a title with a question mark"
            assert rx.check({"title": "Long-term effects of socio-economic status on health", "authors": ["Ann Kim"], "pub_date": "2020"}, "Kim, A. (2020). Longterm effects of socio-economic status on health. J.")[0] == "found", "a word joined at a line's end"
            # styles and names: Chicago with a middle initial, Vancouver, capitals of other alphabets, more particles, hyphenated initials
            for good in ("Orlikowski, Wanda J. 1992. The duality of technology: Rethinking the concept of technology. Organization Science 3(3).",
                         "Smith JA, Jones KB. Effects of aspirin on outcomes in adults. Lancet. 2020;395:497-506.", "Østerlund, C., & Carlile, P. (2005). Relations in practice. The Information Society, 21(2).",
                         "Müller, Jörg. 2018. A paper about things that matter. Journal X.", "vom Brocke, J., & Rosemann, M. (2015). Handbook on business process management.",
                         'J.-P. Benoit and V. Krishna, "Finitely repeated games," Econometrica, 1985.'):
                assert rx.looks_like_ref(good) and rx.parse_ref(good)["title"], good
            assert rx.parse_ref("Smith JA, Jones KB. Effects of aspirin on outcomes in adults. Lancet. 2020;395:497-506.")["title"] == "Effects of aspirin on outcomes in adults"
            # a long list of authors does not push the title out of what Crossref is asked
            LONG = ", ".join("Surname%s, A. B." % chr(65 + i) for i in range(14)) + " (2020). The title that must reach Crossref whatever happens. Nature, 1, 2-3. A note."
            q_ = rx.question(rx.parse_ref(LONG), LONG)
            assert "title that must reach Crossref" in q_ and "A note" not in q_ and len(q_) <= 400, q_
            # a DOI the document prints is kept when Crossref does not know it (arXiv, Zenodo)
            e = rx.resolve("Smith, J. (2020). A preprint nobody at Crossref has heard of. https://doi.org/10.48550/arXiv.2001.00001")
            assert e["status"] == "none" and e["doi"].lower() == "10.48550/arxiv.2001.00001" and not e["journal"], e
            assert len(rx._dedupe([rx._entry("x", parsed={"title": "Заголовок один", "year": "", "venue": "", "doi": ""}), rx._entry("y", parsed={"title": "Другой заголовок", "year": "", "venue": "", "doi": ""})])) == 2
            # a damaged or crafted file is read in a moment (it took minutes: every paragraph looked through the rest of the file)
            import time as _t
            t0 = _t.time(); rx._xml_paras("<w:p >" * 40000, "w:p", "w:t"); rx._rels("<Relationship " * 40000); rx._lines_to_entries(["Smith, John, 12 Some Street, Springfield, student"] * 8000)
            assert _t.time() - t0 < 5, _t.time() - t0
            assert rx._xml_paras("<w:p><w:r><w:t>one</w:t></w:r></w:p><w:p ><w:r><w:t xml:space=\"preserve\">two </w:t><w:tab/><w:t>three</w:t></w:r></w:p>", "w:p", "w:t") == ["one", "two three"]
            # more references than are looked up at once: said, not dropped in silence; Crossref silent three times: not asked again for this document
            keep_max = rx.MAX_CANDIDATES; rx.MAX_CANDIDATES = 2
            try:
                d3 = list(rx.extract_stream(("\n\n".join(c for c in cs[:4])).encode(), "four.txt"))[-1]
                assert d3["candidates"] == 2 and d3["more"] == 2, (d3["candidates"], d3["more"])
            finally:
                rx.MAX_CANDIDATES = keep_max
            fail["n"] = 99; asked.clear()
            d4 = list(rx.extract_stream(("\n\n".join(cs[:6])).encode(), "six.txt"))[-1]
            fail["n"] = 0
            assert len(asked) == 6 and sum(1 for e in d4["entries"] if e.get("unasked")) == len(d4["entries"]) >= 5, (len(asked), d4["entries"])
            # Crossref gives no answer once: asked again; twice: said plainly, not "not on Crossref"
            fail["n"] = 1
            assert rx.resolve(APA)["status"] == "found"
            fail["n"] = 2
            e = rx.resolve(APA)
            assert e["status"] == "none" and e.get("unasked") and "did not answer" in e["why"], e
            # other kinds of file
            IEEE = '[1] K. He, X. Zhang, S. Ren, and J. Sun, "Deep residual learning for image recognition," in Proc. CVPR, 2016, pp. 770-778.'
            ents = rx.paragraphs(("References\n\n" + IEEE + "\n[2] A. Nobody, \"Nothing anybody indexed at all,\" 2001.\n\nKoning, R., Hasan, S., and Chatterji, A. (2022). Experimentation and\nstart-up performance: Evidence from A/B testing. Management\nScience, 68, 6434-6453.\n").encode(), "list.txt")
            assert len(ents) == 4 and ents[1] == IEEE and ents[3].endswith("Science, 68, 6434-6453."), ents
            assert rx.resolve(IEEE)["doi"] == "10.1109/cvpr.2016.90" and rx.parse_ref(IEEE)["title"] == "Deep residual learning for image recognition"
            d_ = pymupdf.open(); pg = d_.new_page(); pg.insert_textbox(pymupdf.Rect(60, 60, 400, 400), "References\n" + APA[:118] + "\nHinnosaar, M., Hinnosaar, T., Kummer, M., and Slivko, O. (2023). Wikipedia matters. Journal of Economics and Management Strategy, 32(3).", fontsize=9)
            pdf_refs = rx.candidates(rx.paragraphs(d_.tobytes(), "refs.pdf")); d_.close()
            assert len(pdf_refs) == 2 and pdf_refs[0].startswith("Koning, R.") and "Evidence from A/B testing" in pdf_refs[0] and pdf_refs[1].startswith("Hinnosaar"), pdf_refs
            zp = io.BytesIO()
            with zipfile.ZipFile(zp, "w") as z:
                z.writestr("ppt/slides/slide1.xml", '<p:sld xmlns:a="a"><a:p><a:r><a:t>Readings</a:t></a:r></a:p><a:p><a:r><a:t>%s</a:t></a:r></a:p></p:sld>' % APA[:118].replace("&", "&amp;"))
            assert rx.candidates(rx.paragraphs(zp.getvalue(), "deck.pptx")) == [APA[:118].strip()]
            for bad, name, words in ((b"\xd0\xcf\x11\xe0old word file", "old.doc", "save it as .docx"), (b"PK\x03\x04broken", "x.docx", "not a document"), (b"", "e.txt", "empty")):
                try:
                    rx.paragraphs(bad, name); raise AssertionError("no error for " + name)
                except ValueError as e:
                    assert words in str(e), (name, str(e))
            assert list(rx.extract_stream(b"\xd0\xcf\x11\xe0old", "old.doc"))[-1]["type"] == "error"
            # the AI as a backup: its list is checked on Crossref like the rest; a reference it made up is not "found"; an AI error is said, nothing else happens
            class R:
                text = json.dumps({"references": [{"authors": "Koning, R., Hasan, S.", "year": "2022", "title": "Experimentation and start-up performance: Evidence from A/B testing", "venue": "Management Science", "doi": ""},
                                                  {"authors": "Ghost, A.", "year": "2020", "title": "A paper the model invented from thin air", "venue": "Nature", "doi": "10.1/invented"},
                                                  {"authors": "", "year": "", "title": "", "venue": "", "doi": ""}]})
                cost, provider, model = 0.012, "fake", "m1"
            sent = []
            ai_.chat = lambda feature, messages, **kw: sent.append((feature, messages, kw)) or R()
            am = list(rx.ai_stream(docx, "syllabus.docx"))
            ae = am[-1]["entries"]
            assert am[-1]["type"] == "done" and am[-1]["cost"] == 0.012 and len(sent) == 1 and sent[0][0] == "refs" and sent[0][2].get("schema") and "Wikipedia matters" in sent[0][1][0]["content"], am[-1]
            assert [x["status"] for x in ae] == ["found", "none"] and ae[0]["doi"] == "10.1287/mnsc.2021.4209" and not ae[1]["doi"], ae
            # the AI's own fields are used as they are; a DOI it gives that the document does not print is its guess; a cut-off answer keeps its complete references and says so
            assert ae[1]["title"] == "A paper the model invented from thin air" and ae[1]["year"] == "2020", ae[1]
            class R2:
                text = '{"references": [{"authors": "Koning, R.", "year": "2022", "title": "Experimentation and start-up performance: Evidence from A/B testing", "venue": "Management Science", "doi": "10.1287/isre.2024.0001"}, {"authors": "Cut, O.", "year": "2020", "title": "A reference the answer sto'
                cost, provider, model, stop = 0.02, "fake", "m1", "max_tokens"
            ai_.chat = lambda feature, messages, **kw: R2()
            a2 = list(rx.ai_stream(("Reading list.\n\n" + APA).encode(), "list.txt"))[-1]
            assert a2["short"] is True and len(a2["entries"]) == 1 and a2["entries"][0]["doi"] == "10.1287/mnsc.2021.4209" and "printed" not in a2["entries"][0]["why"], a2
            def boom(*a, **k):
                raise ai_.AIError("This month's AI budget is used up.")
            ai_.chat = boom
            assert list(rx.ai_stream(docx, "syllabus.docx"))[-1] == {"type": "error", "error": "This month's AI budget is used up."}
        finally:
            importer.crossref_search, importer.crossref_meta, rx.RETRY_WAIT, ai_.chat = keep
    check("references in a document (Word, PowerPoint, PDF, text): found by their shape, each checked on Crossref against what the document prints (a look-alike is 'unsure', a book is not downloaded, prose is dropped, an unanswered question is asked again); the AI is a backup whose list is checked the same way", t_refextract)
    def t_import_review_rules():
        """The rules behind the review dialog that a reviewer found missing: a token is claimed atomically and expires, a name clash never overwrites, dates are real dates, a paper
        is recognised with or without a DOI, the confirmed journal is kept, and 'confirmed' needs more than a title."""
        import csv, threading, time, pymupdf
        from datetime import datetime
        right = {"title": "Governing Open Platforms Through Transparent Rules", "authors": ["Jane Doe", "Richard Roe"], "container": "Journal of Open Platforms", "issn": [], "doi": "10.5555/right",
                 "pub_date": "2024-05", "url": "https://doi.org/10.5555/right", "volume": "", "issue": "", "pages": "", "publisher": "P", "type": "journal-article", "abstract": None, "keywords": []}
        searched = []
        saved = (importer.crossref_meta, importer.crossref_search)
        importer.crossref_meta = lambda d: {"10.5555/right": right}.get(d.lower())
        importer.crossref_search = lambda t, rows=4: searched.append(t) or ([right] if "governing" in t.lower() else [])

        def pdf(title, extra="", authors="Jane Doe and Richard Roe", salt=""):
            d = pymupdf.open(); pg = d.new_page()
            if title:
                pg.insert_textbox(pymupdf.Rect(50, 50, 545, 120), title, fontsize=20); pg.insert_textbox(pymupdf.Rect(50, 125, 545, 150), authors, fontsize=11)
            pg.insert_textbox(pymupdf.Rect(50, 160, 545, 780), (extra + " " + salt + " ") + "We study how platform owners govern complementors with transparent rules. " * 25, fontsize=10)
            for _ in range(60):
                d.new_page().insert_textbox(pymupdf.Rect(60, 60, 540, 780), "\n".join(os.urandom(20).hex() for _ in range(60)), fontsize=8)
            data = d.tobytes(); d.close(); return data
        staged = lambda t: os.path.join(importer.STAGING, t + ".pdf")
        orig_file = importer._file
        rows = lambda: list(csv.DictReader(open(os.path.join(PAPERS, "_index.csv"))))
        try:
            # --- a token is claimed atomically: two requests at once file the PDF once (the filing is slowed down so both would be inside it with the old code)
            tok = importer.analyse_pdf(pdf("Concurrent Confirmation Of A Staged Paper"), "c.pdf")["token"]
            calls, out = [], []

            def slow(p, data, note, after=None):
                calls.append(1); time.sleep(0.3); return orig_file(p, data, note, after=after)
            importer._file = slow
            go = threading.Barrier(2)

            def worker():
                go.wait()
                try:
                    out.append(("ok", importer.confirm(tok, {"title": "Concurrent Confirmation Of A Staged Paper", "source": "hand"})["file"]))
                except ValueError as ex:
                    out.append(("refused", str(ex)))
            ts = [threading.Thread(target=worker) for _ in range(2)]
            [t.start() for t in ts]; [t.join() for t in ts]
            importer._file = orig_file
            assert len(calls) == 1 and sorted(o[0] for o in out) == ["ok", "refused"], (calls, out)
            assert "no longer waiting" in [o for o in out if o[0] == "refused"][0][1] and not os.path.exists(staged(tok)) and not os.path.exists(staged(tok) + ".claimed")
            # a filing that fails puts the PDF back, so it can be tried again
            tok2 = importer.analyse_pdf(pdf("A Paper Whose Filing Fails Once"), "f.pdf")["token"]

            def boom(p, data, note, after=None):
                raise OSError("disk full")
            importer._file = boom
            try:
                importer.confirm(tok2, {"title": "A Paper Whose Filing Fails Once"}); raise AssertionError("expected OSError")
            except OSError:
                pass
            importer._file = orig_file
            assert os.path.exists(staged(tok2)) and not os.path.exists(staged(tok2) + ".claimed")
            assert importer.confirm(tok2, {"title": "A Paper Whose Filing Fails Once", "source": "hand"})["existing"] is False

            # --- two different papers that would get the same file name (same month, journal and first six title words, no DOI) are both kept
            a1, a2 = pdf("Platform Alpha One", salt="first"), pdf("Platform Alpha Two", salt="second")
            same = dict(journal="Journal of Collisions", pub_date="2023-04", source="hand")
            r1 = importer.confirm(importer.analyse_pdf(a1, "1.pdf")["token"], dict(same, title="Platform governance rules for open ecosystems and more alpha"))
            r2 = importer.confirm(importer.analyse_pdf(a2, "2.pdf")["token"], dict(same, title="Platform governance rules for open ecosystems and more beta"))
            assert r1["file"] != r2["file"] and not r2["existing"], (r1, r2)
            assert open(os.path.join(TMP, r1["file"]), "rb").read() == a1 and open(os.path.join(TMP, r2["file"]), "rb").read() == a2, "the first PDF was overwritten"

            # --- a staged PDF older than a day is not accepted, and sweep() forgets it without waiting for another analysis
            tok3 = importer.analyse_pdf(pdf("Left Waiting For Two Days"), "w.pdf")["token"]
            os.utime(staged(tok3), (time.time() - 48 * 3600,) * 2)
            try:
                importer.confirm(tok3, {"title": "Left Waiting For Two Days"}); raise AssertionError("an expired token was accepted")
            except ValueError as ex:
                assert "no longer waiting" in str(ex)
            assert importer.sweep() == 1 and not os.path.exists(staged(tok3)) and importer.sweep() == 0

            # --- dates are real calendar dates; a bad one is refused (not silently dropped) and the PDF stays staged
            for bad in ("2024-13", "2024-02-31", "2023-02-29", "2024-05-01garbage", "2024-5", "0000", "May 2024", "2024-00"):
                assert not importer.date_ok(bad), bad
            for good in ("2024", "2024-05", "2024-02-29", "1999-12-31"):
                assert importer.date_ok(good), good
            tok4 = importer.analyse_pdf(pdf("Dated Wrongly On Purpose"), "d.pdf")["token"]
            for bad in ("2024-13", "2024-05-01garbage", "sometime"):
                try:
                    importer.confirm(tok4, {"title": "Dated Wrongly On Purpose", "pub_date": bad}); raise AssertionError("accepted date %r" % bad)
                except ValueError as ex:
                    assert "date must be" in str(ex)
                assert os.path.exists(staged(tok4)), "a refused date keeps the staged PDF"
            ok4 = importer.confirm(tok4, {"title": "Dated Wrongly On Purpose", "pub_date": "", "source": "hand"})
            assert ok4["file"].startswith("Papers/%s/" % datetime.now().strftime("%Y-%m")), ok4          # (no date: today's month, as before)

            # --- recognised with or without a DOI, but two different DOIs stay two papers
            t_doi = "Recognising The Same Paper Twice"
            importer.confirm(importer.analyse_pdf(pdf(t_doi, salt="d1"), "a.pdf")["token"], {"title": t_doi, "doi": "10.5555/dd1", "source": "hand"})
            t_nodoi = "Recognising A Paper Filed Without A DOI"
            importer.confirm(importer.analyse_pdf(pdf(t_nodoi, salt="d2"), "b.pdf")["token"], {"title": t_nodoi, "source": "hand"})
            assert importer.check_existing("", t_doi), "a paper with a DOI is found by its title when the new record has none"
            assert importer.check_existing("10.5555/dd1", "something else entirely") != "" and importer.check_existing("10.5555/DD1 ", t_doi) != ""
            assert importer.check_existing("10.5555/new-doi", t_nodoi), "a paper filed without a DOI is found once Crossref supplies one"
            assert importer.check_existing("10.5555/other", t_doi) == "", "a different DOI is a different paper"
            assert importer.check_existing("", "No Such Paper In The Library At All") == ""

            # --- the journal you confirmed is kept; a publisher's shared DOI prefix does not change it
            jf = lambda doi, cont, issn=(): importer.journal_for({"doi": doi, "container": cont, "issn": list(issn)})
            assert jf("10.1287/mnsc.2024.1", "Management Science")[0] == "Management Science"
            assert jf("10.1287/isre.2024.1", "Information Systems Research")[0] == "Information Systems Research"
            assert jf("10.1287/zzz.1", "Some Other Journal") == ("Some Other Journal", "SOJ")
            assert jf("10.1287/zzz.1", "")[0] == "Other", "a shared prefix alone says nothing about which journal"
            assert jf("10.25300/misq.2024.1", "")[0] == "MIS Quarterly", "a prefix that belongs to one watched journal is still used when no journal is known"
            assert jf("10.9999/x", "Odd Name", ["0025-1909"])[0] == "Management Science", "the ISSN decides first"
            rm = importer.confirm(importer.analyse_pdf(pdf("Journal Kept As Confirmed", salt="jj"), "j.pdf")["token"], {"title": "Journal Kept As Confirmed", "doi": "10.1287/mnsc.2024.77", "journal": "Management Science", "pub_date": "2024-02", "source": "hand"})
            assert "/Management Science/" in rm["file"] and [r for r in rows() if r["title"] == "Journal Kept As Confirmed"][0]["journal"] == "Management Science", rm

            # --- 'confirmed' needs more than a title: the title alone on the PDF is a record to check, the printed DOI or the authors make it a record the PDF backs up
            searched.clear()
            weak = importer.analyse_pdf(pdf(right["title"], authors="Quentin Zzz"), "weak.pdf")
            assert weak["candidates"][0]["verdict"] == "match" and weak["candidates"][0]["authors_found"] is False and weak["confidence"] == "check", weak["candidates"][0]
            strong = importer.analyse_pdf(pdf(right["title"], salt="s1"), "strong.pdf")
            assert strong["candidates"][0]["authors_found"] is True and strong["confidence"] == "high"
            printed = importer.analyse_pdf(pdf(right["title"], "doi: 10.5555/right", authors="Quentin Zzz", salt="s2"), "printed.pdf")
            assert printed["candidates"][0]["doi_printed"] and printed["confidence"] == "high", printed["candidates"][0]
            assert searched, "no record the PDF backs up: the title was searched"
        finally:
            importer.crossref_meta, importer.crossref_search = saved
            importer._file = orig_file
            shutil.rmtree(importer.STAGING, ignore_errors=True)
    check("review rules: a token is claimed once (and restored if filing fails), a name clash never overwrites a PDF, expired staged PDFs are refused and swept, dates must be real, a paper is recognised with or without a DOI, the confirmed journal is kept, and a title alone is not 'confirmed'", t_import_review_rules)
    def t_local_state():
        """The data folder can be synced between computers, so what belongs to one computer lives outside it (paths.LOCAL) and what you want on both stays inside (state/)."""
        import subprocess
        home, loc = tempfile.mkdtemp(prefix="jw-home-"), tempfile.mkdtemp(prefix="jw-local-")
        try:
            probe = r'''
import json, os, sys
sys.path.insert(0, %r)
import paths
st = os.path.join(paths.BASE, "state")
for n, body in (("browser-profile/Default/x", "p"), ("index/a.json", "{}"), ("ai_cache/c.json", "1"), ("models/m.bin", "m"), ("verdicts.json", "{}"), ("ai_usage.jsonl", ""), ("signins.json", "{}"), ("runs/r.json", "{}")):
    os.makedirs(os.path.dirname(os.path.join(st, n)), exist_ok=True); open(os.path.join(st, n), "w").write(body)
for n, body in (("index/b.json", "{}"), ("ai_cache/d.json", "2"), ("ai_cache/c.json", "1")):          # (an earlier development version kept the index and the saved answers here: they belong in state/, shared)
    os.makedirs(os.path.dirname(os.path.join(paths.LOCAL, n)), exist_ok=True); open(os.path.join(paths.LOCAL, n), "w").write(body)
moved = paths.migrate_local(); again = paths.migrate_local()
import ai, blocks, browser, signins, updater, importer, exports, authorcache, related, personal, pipeline, identity, papercard
out = {"local": paths.LOCAL, "state": paths.STATE, "moved": moved, "again": again,
       "where": {"models": ai.MODELS, "profile": browser.PROFILE, "cookies": browser.SESSION_COOKIES, "signins": signins.STATUS_FILE, "update": updater.STATE_FILE,
                 "staging": importer.STAGING, "crossref": exports.CACHE, "authors": authorcache.CACHE, "openalex": related.CACHE, "personal": personal.CACHE, "fetched": pipeline.FETCHED},
       "shared": {"ai_cache": ai.CACHE, "index": blocks.INDEX, "ledger": ai.LEDGER, "verdicts": pipeline.VERDICTS, "runs": pipeline.RUNS, "kept": identity.KEPT, "cards": papercard.BACKUPS},
       "left_in_state": sorted(os.listdir(st)), "in_local": sorted(os.listdir(paths.LOCAL)),
       "index_files": sorted(os.listdir(os.path.join(st, "index"))), "cache_files": sorted(os.listdir(os.path.join(st, "ai_cache")))}
print("JSON" + json.dumps(out))
''' % HERE
            env = {k: v for k, v in os.environ.items() if k not in ("JW_HOME", "JW_LOCAL")}
            r = subprocess.run([sys.executable, "-c", probe], env=dict(env, JW_HOME=home, JW_LOCAL=loc), capture_output=True, text=True, timeout=120)
            assert r.returncode == 0, r.stderr[-800:]
            o = json.loads(r.stdout.split("JSON", 1)[1])
            under = lambda p, d: os.path.realpath(p).startswith(os.path.realpath(d) + os.sep)
            for k, p in o["where"].items():
                assert under(p, o["local"]) and not under(p, o["state"]), (k, p)
            for k, p in o["shared"].items():
                assert under(p, o["state"]) and not under(p, o["local"]), (k, p)
            assert sorted(o["moved"]) == ["ai_cache", "browser-profile", "index", "models", "signins.json"] and o["again"] == [], o["moved"]       # (the index and saved answers came BACK into state/)
            assert "verdicts.json" in o["left_in_state"] and "ai_usage.jsonl" in o["left_in_state"] and "runs" in o["left_in_state"] and "index" in o["left_in_state"] and "ai_cache" in o["left_in_state"], o["left_in_state"]
            assert "browser-profile" in o["in_local"] and "models" in o["in_local"] and "verdicts.json" not in o["in_local"] and "index" not in o["in_local"] and "ai_cache" not in o["in_local"], o["in_local"]
            assert o["index_files"] == ["a.json", "b.json"] and o["cache_files"] == ["c.json", "d.json"], (o["index_files"], o["cache_files"])        # (both copies kept; the duplicate c.json is not doubled)
            # without JW_LOCAL, from a checkout, nothing changes: everything stays in state/
            r2 = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, %r); import paths; print(paths.LOCAL == paths.STATE, paths.migrate_local())" % HERE], env=dict(env, JW_HOME=home), capture_output=True, text=True, timeout=60)
            assert r2.stdout.strip() == "True []", r2.stdout + r2.stderr
        finally:
            shutil.rmtree(home, ignore_errors=True); shutil.rmtree(loc, ignore_errors=True)
    check("sync: per-computer files (sign-in browser profile, cookies, models, lookup caches, update state, staged PDFs) live outside the data folder; the shared ones (runs, ledger, search index, saved AI answers) stay in it, a per-computer copy of the shared caches is merged back once, a checkout is unchanged", t_local_state)
    def t_sync_info():
        """Two computers share one data folder: each says when it is here, Dropbox's conflicted copies are found, the library index copies can be joined without losing a paper, and any copy can be set aside (never deleted)."""
        import subprocess
        real_home, loc = tempfile.mkdtemp(prefix="jw-home-"), tempfile.mkdtemp(prefix="jw-local-")
        home = real_home + "-link"
        os.symlink(real_home, home)           # the data folder is reached through a link (~/Dropbox, or /var on a Mac): the paths shown and used must still be relative to the folder itself
        try:
            child = r'''
import csv, json, os, sys, time
from datetime import datetime, timedelta, timezone
sys.path.insert(0, %r)
import paths
paths.seed_home()
import download as dl, syncinfo as sy
P = paths.PAPERS
iso = lambda dt: dt.isoformat(timespec="seconds")
now = datetime.now(timezone.utc)

# -- who is here
a = sy.me(); assert a == sy.me() and len(a["id"]) == 32 and a["name"], a                        # (made once, kept in LOCAL)
assert os.path.exists(os.path.join(paths.LOCAL, "device.json")) and not os.path.exists(os.path.join(paths.BASE, "devices", "device.json"))
assert sy.rename("  Home   laptop ")["name"] == "Home laptop" and sy.me()["name"] == "Home laptop"
for bad in ("", "   ", None):
    try:
        sy.rename(bad); raise AssertionError("empty name accepted")
    except ValueError:
        pass
sy.beat(); mine = os.path.join(paths.BASE, "devices", a["id"] + ".json")
assert json.load(open(mine))["name"] == "Home laptop" and sy.computers() == [] and sy.others() == [], "this computer is not listed among the others"
D = os.path.join(paths.BASE, "devices"); put = lambda i, **k: json.dump({"id": i, "name": "Work Mac", "seen": iso(now), "pid": 1, "version": "1.2.2", **k}, open(os.path.join(D, i + ".json"), "w"))
put("1" * 32); put("2" * 32, name="Old desk", seen=iso(now - timedelta(minutes=10))); put("3" * 32, name="Ancient", seen=iso(now - timedelta(days=90))); put("4" * 32, seen="yesterday"); put("zz")
open(os.path.join(D, "junk.json"), "w").write("{not json")
c = sy.computers()
assert [x["name"] for x in c] == ["Work Mac", "Old desk"] and [x["open"] for x in c] == [True, False], c      # (open first; 10 minutes ago is not 'open'; ancient and broken files are not shown)
assert [x["name"] for x in sy.others()] == ["Work Mac"] and not os.path.exists(os.path.join(D, "3" * 32 + ".json")), "a computer not seen for 60 days is forgotten"
assert sy.others(now + timedelta(minutes=5)) == [], "three minutes later it is no longer open"
sy.leave(); assert not os.path.exists(mine)

# -- Dropbox conflicts: found, described, left alone when they only look similar
os.makedirs(os.path.join(P, "2024-05", "Journal"), exist_ok=True); os.makedirs(os.path.join(P, "_conflicts", "x"), exist_ok=True); os.makedirs(os.path.join(P, "_removed", "y"), exist_ok=True)
cols = dl.INDEX_COLS
def write_csv(p, rows):
    with open(p, "w", newline="") as f:
        w = csv.DictWriter(f, cols); w.writeheader(); w.writerows(rows)
def row(title, doi, status="ok", file="", date="2026-11-01"):
    return dict(zip(cols, [date, "J", "J", title, "A B", doi, "", "", "", file, status, ""]))
open(os.path.join(P, "2024-05", "Journal", "kept.pdf"), "wb").write(b"%%PDF")
write_csv(os.path.join(P, "_index.csv"), [row("Alpha", "10.1/a", file="Papers/2024-05/Journal/kept.pdf", date="2026-10-01"), row("Beta", "10.1/b", status="failed", date="2026-10-02")])
cp = os.path.join(P, "_index (Joe's MacBook's conflicted copy 2026-11-03).csv")
write_csv(cp, [row("Beta", "10.1/b", file="Papers/2024-05/Journal/kept.pdf", date="2026-11-02"), row("Gamma", "10.1/c", file="Papers/2024-05/Journal/kept.pdf", date="2026-11-02"),
              row("Alpha", "10.1/a", status="failed", date="2026-11-02")])         # (the other computer failed to download Alpha later: the copy that HAS the PDF still wins)
open(os.path.join(paths.BASE, "ai (conflicted copy).json"), "w").write("{}")
open(os.path.join(paths.ROOT, ".env (Work Mac's conflicted copy 2026-11-03)"), "w").write("X=1")
for decoy in (os.path.join(P, "paper (1).pdf"), os.path.join(P, "_conflicts", "x", "old (conflicted copy).csv"), os.path.join(P, "_removed", "y", "z (conflicted copy).pdf"), os.path.join(paths.LOCAL, "q (conflicted copy).json")):
    open(decoy, "wb").write(b"x")
found = {x["path"]: x for x in sy.conflicts(fresh=True)}
assert sorted(found) == sorted(["Papers/_index (Joe's MacBook's conflicted copy 2026-11-03).csv", ".agents/skills/ais-journal-watcher/ai (conflicted copy).json", ".env (Work Mac's conflicted copy 2026-11-03)"]), sorted(found)
ix = found["Papers/_index (Joe's MacBook's conflicted copy 2026-11-03).csv"]
assert ix["kind"] == "index" and ix["original"] == "Papers/_index.csv" and ix["original_exists"] and found[".agents/skills/ais-journal-watcher/ai (conflicted copy).json"]["kind"] == "json"

# -- joining the two library indexes loses no paper; the same paper keeps its better row
r = sy.merge_index(ix["path"])
rows = {x["doi"]: x for x in csv.DictReader(open(os.path.join(P, "_index.csv"), newline=""))}
assert sorted(rows) == ["10.1/a", "10.1/b", "10.1/c"] and r["papers"] == 3 and r["added"] == 1, (sorted(rows), r)
assert rows["10.1/b"]["status"] == "ok" and rows["10.1/b"]["file"], "the downloaded row beats the failed one (when it is in the copy)"
assert rows["10.1/a"]["status"] == "ok" and rows["10.1/a"]["file"], "…and when it is in the original, even though the failed row is newer"
assert rows["10.1/a"]["file"] and not os.path.exists(cp) and os.path.exists(os.path.join(paths.ROOT, r["set_aside"])) and r["set_aside"].startswith("Papers/_conflicts/"), r
assert len([x for x in sy.conflicts(fresh=True) if x["kind"] == "index"]) == 0
# only the library index can be joined; anything else, or a path that is not a conflicted copy, is refused
for bad, fns in ((".agents/skills/ais-journal-watcher/ai (conflicted copy).json", (sy.merge_index,)), ("Papers/_index.csv", (sy.merge_index, sy.set_aside)), ("../outside", (sy.merge_index, sy.set_aside)),
                 ("", (sy.merge_index, sy.set_aside)), (None, (sy.merge_index, sy.set_aside)), ("Papers/2024-05", (sy.merge_index, sy.set_aside)), ("Papers/paper (1).pdf", (sy.merge_index, sy.set_aside)),
                 ("/etc/passwd", (sy.merge_index, sy.set_aside))):
    for fn in fns:
        try:
            fn(bad); raise AssertionError("%%s accepted %%r" %% (fn.__name__, bad))
        except ValueError:
            pass
assert os.path.exists(os.path.join(paths.BASE, "ai (conflicted copy).json")), "a refused request moves nothing"
# setting aside moves (never deletes) and keeps the place it came from
w = sy.set_aside(".env (Work Mac's conflicted copy 2026-11-03)")
assert os.path.exists(os.path.join(paths.ROOT, w)) and w.startswith("Papers/_conflicts/") and not os.path.exists(os.path.join(paths.ROOT, ".env (Work Mac's conflicted copy 2026-11-03)")) and open(os.path.join(paths.ROOT, w)).read() == "X=1"
i = sy.info(); assert i["separate"] and i["me"]["name"] == "Home laptop" and len(i["conflicts"]) == 1 and i["data_folder"] == paths.ROOT
print("OK")
''' % HERE
            env = {k: v for k, v in os.environ.items() if k not in ("JW_HOME", "JW_LOCAL")}
            r = subprocess.run([sys.executable, "-c", child], env=dict(env, JW_HOME=home, JW_LOCAL=loc), capture_output=True, text=True, timeout=120)
            assert r.returncode == 0 and r.stdout.strip().endswith("OK"), (r.stdout + r.stderr)[-1500:]
        finally:
            os.remove(home); shutil.rmtree(real_home, ignore_errors=True); shutil.rmtree(loc, ignore_errors=True)
    check("sync: each computer says it is here (and is forgotten when it stops), Dropbox's conflicted copies are found, the library index copies join without losing a paper, copies are set aside and never deleted, bad paths are refused", t_sync_info)
    def t_books():
        """Books (PDF and EPUB): recognised, read, checked, filed under the 'Books' journal with the EPUB kept beside a PDF made from it, found again by ISBN, DRM refused."""
        import csv, zipfile
        import books as bk, importer, pymupdf
        import pipeline as pl_, identity as idn
        from datetime import datetime

        def epub(title="Platforms and Their Governance", authors=("Lovelace, Ada",), isbn="978-0-201-83595-3", publisher="Test University Press", date="2019-05-01", chapters=3, drm=False, font=False):
            b = io.BytesIO()
            with zipfile.ZipFile(b, "w") as z:
                z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
                z.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
                if drm or font:
                    z.writestr("META-INF/encryption.xml", '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><EncryptedData><EncryptionMethod Algorithm="%s"/></EncryptedData></encryption>'
                               % ("http://www.idpf.org/2008/embedding" if font else "http://www.w3.org/2001/04/xmlenc#aes128-cbc"))
                z.writestr("OEBPS/content.opf", '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">urn:isbn:%s</dc:identifier>'
                           '<dc:title>%s</dc:title>%s<dc:publisher>%s</dc:publisher><dc:date>%s</dc:date><dc:language>en</dc:language></metadata><manifest>%s<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/></manifest><spine>%s</spine></package>'
                           % (isbn, title, "".join("<dc:creator>%s</dc:creator>" % a for a in authors), publisher, date, "".join('<item id="c%d" href="c%d.xhtml" media-type="application/xhtml+xml"/>' % (i, i) for i in range(chapters)),
                              "".join('<itemref idref="c%d"/>' % i for i in range(chapters))))
                z.writestr("OEBPS/nav.xhtml", '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc"><ol>%s</ol></nav></body></html>' % "".join('<li><a href="c%d.xhtml">Chapter %d</a></li>' % (i, i + 1) for i in range(chapters)))
                for i in range(chapters):
                    z.writestr("OEBPS/c%d.xhtml" % i, '<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>Chapter %d</h1>%s</body></html>' % (i + 1, "".join("<p>%s</p>" % ("Platform governance and complementors, paragraph %d of chapter %d. " % (j, i + 1) + "Open ecosystems need rules. " * 20) for j in range(25))))
            return b.getvalue()

        def pdf_book(title, isbn_line="", pages=30):
            d = pymupdf.open()
            for i in range(pages):
                pg = d.new_page()
                pg.insert_textbox(pymupdf.Rect(50, 50, 545, 120), title if i == 0 else "Chapter %d" % i, fontsize=20)
                pg.insert_textbox(pymupdf.Rect(50, 140, 545, 780), (isbn_line + " " if i == 1 else "") + "Words about platforms and ecosystems. " * 60 + "".join(os.urandom(8).hex() for _ in range(40)), fontsize=10)
            d.set_toc([[1, "Introduction", 3], [1, "Chapter One", 10], [2, "A section", 12]])
            d.embfile_add("padding", os.urandom(45000))
            out = d.tobytes(); d.close(); return out

        # ---- ISBNs
        assert bk.isbn13("0201835959") == "9780201835953" and bk.isbn13("978-0-201-83595-3") == "9780201835953" and bk.isbn_ok("9780201835954") == "" and bk.isbn_ok("12345") == "" and bk.isbn13("") == ""
        assert bk.find_isbn("Copyright 2019. ISBN 978-0-201-83595-3 (paperback)") == "9780201835953" and bk.find_isbn("ISBN-10: 0-201-83595-9") == "9780201835953" and bk.find_isbn("ISBN 978-0-201-83595-4") == "" and bk.find_isbn("no number here") == ""
        # ---- recognising and reading an EPUB
        good = epub()
        assert bk.is_epub(good) and not bk.is_epub(b"%PDF-1.4 x") and not bk.is_epub(b"PK\x03\x04junk") and not bk.is_epub(None)
        z = io.BytesIO()
        with zipfile.ZipFile(z, "w") as zz:
            zz.writestr("hello.txt", "a plain zip")
        assert not bk.is_epub(z.getvalue())
        own = bk.read_epub(good)
        assert own["title"] == "Platforms and Their Governance" and own["authors"] == ["Ada Lovelace"] and own["isbn"] == "9780201835953" and own["publisher"] == "Test University Press" and own["date"] == "2019-05-01", own
        assert bk.read_epub(epub(authors=("Smith, J., Doe, A.",)))["authors"] == ["Smith, J., Doe, A."], "a list of names is not 'flipped'"
        try:
            bk.read_epub(epub(drm=True)); raise AssertionError("a DRM EPUB was read")
        except ValueError as ex:
            assert "copy-protected" in str(ex)
        assert bk.read_epub(epub(font=True))["title"], "font obfuscation is not DRM"
        old_max = bk.MAX_UNPACKED
        bk.MAX_UNPACKED = 500
        try:
            bk.read_epub(good); raise AssertionError("a huge unpacked size was accepted")
        except ValueError as ex:
            assert "damaged" in str(ex)
        finally:
            bk.MAX_UNPACKED = old_max
        try:
            bk.read_epub(b"not a zip at all"); raise AssertionError("garbage accepted")
        except ValueError:
            pass
        # ---- turning it into pages keeps the chapters as the outline
        pdf, toc, pages = bk.to_pdf(epub(chapters=4))
        with pymupdf.open(stream=pdf, filetype="pdf") as d:
            assert len(d) == pages and pages >= 4 and [t for _, t, _ in d.get_toc()] == ["Chapter %d" % i for i in range(1, 5)] and "Platform governance" in d[0].get_text() + d[1].get_text()
        # ---- lookups: what is sent and how the answers are read (no network)
        saved = (dl.http_get, os.environ.get("JW_OFFLINE"))
        asked = []
        ol = {"docs": [{"key": "/works/OL1W", "title": "Platforms and Their Governance", "subtitle": "A study", "author_name": ["Ada Lovelace"], "first_publish_year": 2019, "publisher": ["Test University Press", "Other"], "isbn": ["0201835959", "bad", "9780201835953"], "number_of_pages_median": 312}]}
        cr = {"message": {"items": [{"title": ["Platforms and Their Governance (2nd ed.)"], "author": [{"given": "Ada", "family": "Lovelace"}], "DOI": "10.5555/book.1", "publisher": "Crossref Press", "ISBN": ["9780201835953"], "published": {"date-parts": [[2021]]}, "type": "book", "URL": "https://doi.org/10.5555/book.1"},
                           {"title": ["A Quite Different Book About Platform Strategy"], "author": [{"given": "Zed", "family": "Quinn"}], "DOI": "10.5555/book.2", "publisher": "Other Press", "published": {"date-parts": [[2012]]}, "type": "book"}]}}

        sent = []

        def fake_get(url, timeout=25, headers=None):
            asked.append(url); sent.append((url, dict(headers or {})))
            return json.dumps(ol if "openlibrary" in url else cr).encode(), {}
        dl.http_get = fake_get
        os.environ.pop("JW_OFFLINE", None)
        try:
            r = bk.openlibrary_search(isbn="9780201835953")
            assert asked[-1].startswith("https://openlibrary.org/search.json?") and "isbn%3A9780201835953" in asked[-1] and len(r) == 1
            assert r[0]["title"] == "Platforms and Their Governance: A study" and r[0]["authors"] == ["Ada Lovelace"] and r[0]["year"] == "2019" and r[0]["publisher"] == "Test University Press" and r[0]["isbn"] == "9780201835953" and r[0]["pages"] == 312, r
            r2 = bk.openlibrary_search(title="Platforms and Their Governance", authors=["Ada Lovelace"])
            assert "title=Platforms" in asked[-1] and "author=Ada" in asked[-1] and r2[0]["isbn"] == "9780201835953", (asked[-1], r2)
            assert bk.openlibrary_search(title="abc") == [] and bk.openlibrary_search() == [], "nothing is asked for a title that short"
            ua = next(h["User-Agent"] for u, h in sent if "openlibrary" in u)
            assert ua.startswith("JournalWatcher/") and "Mozilla" not in ua and "github.com/fjc1991/journal-watcher" in ua, ua          # (Open Library asks programs to say who they are: this one does)
            c = bk.crossref_books("Platforms and Their Governance")
            assert "filter=type:book" in asked[-1] and c[0]["doi"] == "10.5555/book.1" and c[0]["publisher"] == "Crossref Press" and c[0]["year"] == "2021" and c[0]["isbn"] == "9780201835953", c
            cr_main = cr
            cr = {"message": {"items": [{"title": ["An Edited Volume on Platform Research"], "editor": [{"given": "E.", "family": "Editor"}], "DOI": "10.5555/ed.1", "publisher": "P", "type": "edited-book"}]}}
            assert bk.crossref_books("An Edited Volume on Platform Research")[0]["authors"] == ["E. Editor"], "an edited volume has editors, not authors"
            cr = cr_main
            dl.http_get = lambda *a, **k: (_ for _ in ()).throw(OSError("offline"))
            assert bk.openlibrary_search(isbn="9780201835953") == [] and bk.crossref_books("Platforms and Their Governance") == [], "a lookup that fails gives nothing, not an error"
            dl.http_get = fake_get
            # ---- looking at an EPUB: the file's own details come first and are backed by the file; the lookup's publisher is added to the same book; nothing is filed
            n_rows = len(importer.library_rows())
            a = bk.analyse(good, "my book.epub")
            c0 = a["candidates"][0]
            assert a["type"] == "book" and a["format"] == "epub" and c0["own"] and c0["verdict"] == "match" and c0["isbn"] == "9780201835953" and a["confidence"] == "high" and a["pick"] == 0 and not a["existing"], a
            assert c0["pages"] == a["pdf"]["pages"] > 3 and c0["year"] == "2019" and c0["url"].startswith("https://openlibrary.org/") and a["pdf"]["chapters"] == 3 and a["pdf"]["isbn"] == "9780201835953", (c0, a["pdf"])
            assert c0["doi"] == "10.5555/book.1", "the same ISBN in Crossref: its DOI goes into the same record, which is not listed twice"
            assert [x["doi"] for x in a["candidates"] if x["source"] == "crossref"] == ["10.5555/book.2"] and len(a["candidates"]) == 2 and len(importer.library_rows()) == n_rows, [(x["source"], x["title"]) for x in a["candidates"]]
            stg = lambda t, e: os.path.join(importer.STAGING, t + e)
            assert os.path.exists(stg(a["token"], ".pdf")) and os.path.exists(stg(a["token"], ".conv"))
            importer.discard([a["token"]])
            assert not os.path.exists(stg(a["token"], ".pdf")) and not os.path.exists(stg(a["token"], ".conv")), "cancelling forgets the converted copy too"
            # ---- a PDF you say is a book: its printed ISBN is looked up
            pb = pdf_book("Platforms and Their Governance", "ISBN 978-0-201-83595-3")
            asked.clear()
            ab = bk.analyse(pb, "book.pdf", hint_pdf=True)
            assert ab["format"] == "pdf" and ab["pdf"]["isbn"] == "9780201835953" and "isbn%3A9780201835953" in asked[0] and ab["pdf"]["pages"] == 30 and ab["pdf"]["chapters"] == 3, (ab["pdf"], asked)
            importer.discard([ab["token"]])
            for junk in (b"plain text", b"PK\x03\x04junk"):
                try:
                    bk.analyse(junk, "x.txt"); raise AssertionError("junk accepted")
                except ValueError:
                    pass
            # ---- filing an EPUB: through importer.confirm (kind 'book'), like the dialog does
            a = bk.analyse(good, "my book.epub")
            r = importer.confirm(a["token"], {"kind": "book", "title": "Platforms and Their Governance", "authors": "Ada Lovelace", "publisher": "Test University Press", "pub_date": "2019", "isbn": "0-201-83595-9", "source": "lookup"})
            assert r["file"].startswith("Papers/2019-01/Books/2019-01_BOOK_") and r["file"].endswith(".pdf") and not r["existing"], r
            full = os.path.join(TMP, r["file"])
            assert open(full[:-4] + ".epub", "rb").read() == good, "the EPUB you gave is kept as it was"
            info = json.load(open(full[:-4] + ".book.json"))
            assert info["isbn"] == "9780201835953" and info["publisher"] == "Test University Press" and info["source"] == "epub" and info["offset"] == 0 and [t for _, t, _ in info["toc"]] == ["Chapter 1", "Chapter 2", "Chapter 3"], info
            with pymupdf.open(full) as d:
                assert len(d) == info["pages"] and [t for _, t, _ in d.get_toc()] == ["Chapter 1", "Chapter 2", "Chapter 3"]
            row = next((x for x in importer.library_rows() if x["file"] == r["file"]), None)
            assert row and row["journal"] == "Books" and row["abbr"] == "BOOK" and row["authors"] == "Ada Lovelace" and "details from a book database" in row["note"], row
            assert os.path.exists(full[:-4] + ".epub") and not os.listdir(importer.STAGING), os.listdir(importer.STAGING)
            assert all(p in ns.sidecars(full) for p in (full[:-4] + ".epub", full[:-4] + ".book.json")), "they move and go with the book"
            sp_ = (paths.PAPERS, paths.ROOT)
            paths.PAPERS, paths.ROOT = dl.PAPERS, dl.ROOT          # (this file keeps the pipeline module on another scratch folder: the audit reads paths.*)
            try:
                audited = [x["title"] for x in idn.audit()]
            finally:
                paths.PAPERS, paths.ROOT = sp_
            assert "Platforms and Their Governance" not in audited, "books are not audited like papers (their title is not a DOI to look for)"
            # the same book again (same ISBN, a different title typed) is recognised, not filed twice
            a2 = bk.analyse(epub(title="Platforms Governed"), "again.epub")
            assert a2["existing"] == r["file"], a2["existing"]
            r2 = importer.confirm(a2["token"], {"kind": "book", "title": "Platforms Governed", "isbn": "9780201835953", "source": "hand"})
            assert r2["existing"] and r2["file"] == r["file"] and len([x for x in importer.library_rows() if x["journal"] == "Books"]) == 1
            # refused details keep the book staged; a bad ISBN says so
            a3 = bk.analyse(epub(title="Another Book Entirely", isbn="978-0-306-40615-7"), "x.epub")
            for bad, msg in (({"title": ""}, "title"), ({"title": "T", "isbn": "9780201835954"}, "ISBN"), ({"title": "T", "pub_date": "2019-13"}, "date")):
                try:
                    importer.confirm(a3["token"], dict(bad, kind="book")); raise AssertionError("accepted %r" % (bad,))
                except ValueError as ex:
                    assert msg in str(ex), (bad, str(ex))
                assert os.path.exists(stg(a3["token"], ".pdf")) and os.path.exists(stg(a3["token"], ".conv"))
            r3 = importer.confirm(a3["token"], {"kind": "book", "title": "Another Book Entirely", "authors": ["B. Author"], "pub_date": "", "source": "hand"})
            assert r3["file"].startswith("Papers/%s/Books/" % datetime.now().strftime("%Y-%m")), r3
            # a PDF book is filed as it is, with its own outline
            apdf = bk.analyse(pb, "book.pdf", hint_pdf=True)
            r4 = importer.confirm(apdf["token"], {"kind": "book", "title": "A PDF Book About Platforms", "authors": "C. Writer", "pub_date": "2015", "isbn": "9780306406157", "source": "hand"})
            f4 = os.path.join(TMP, r4["file"])
            assert open(f4, "rb").read() == pb and not os.path.exists(f4[:-4] + ".epub") and json.load(open(f4[:-4] + ".book.json"))["source"] == "pdf" and len(json.load(open(f4[:-4] + ".book.json"))["toc"]) == 3
            assert not [x for x in os.listdir(importer.STAGING)], "nothing is left staged"
        finally:
            dl.http_get = saved[0]
            if saved[1] is not None:
                os.environ["JW_OFFLINE"] = saved[1]
            shutil.rmtree(importer.STAGING, ignore_errors=True)
    check("books: PDF and EPUB are recognised, DRM and damaged files refused with plain words, the file's own details and Open Library/Crossref records are offered, an EPUB becomes pages with its chapters as the outline and is kept beside them, a book is found again by ISBN", t_books)
    def t_book_chapters():
        """A book's outline as page ranges, where a page is in words you could cite (chapter + printed page, or location for an EPUB), the printed-page offset, and search inside one chapter."""
        import books as bk
        info = {"source": "pdf", "pages": 30, "offset": 0, "toc": [[1, "Introduction", 1], [1, "Chapter One", 10], [2, "Section 1.1", 12], [2, "Section 1.2", 15], [1, "Chapter Two", 20]]}
        ch = {c["title"]: (c["start"], c["end"], c["level"]) for c in bk.chapters(info)}
        assert ch == {"Introduction": (1, 10, 1), "Chapter One": (10, 20, 1), "Section 1.1": (12, 15, 2), "Section 1.2": (15, 20, 2), "Chapter Two": (20, 30, 1)}, ch          # (a chapter runs to the page where the next one starts: that page is in both)
        assert [c["i"] for c in bk.chapters(info)] == [0, 1, 2, 3, 4]
        assert bk.chapters({"pages": 10, "toc": []}) == [] and bk.chapters({}) == [] and bk.chapters({"pages": 10, "toc": [["x"], [1, "", 3], [1, "Ok", "bad"], [1, "Fine", 4]]}) == [{"i": 0, "level": 1, "title": "Fine", "start": 4, "end": 10}]
        assert [c["end"] for c in bk.chapters({"pages": 20, "toc": [[1, "A", 5], [1, "B", 5], [1, "C", 9]]})] == [5, 9, 20], "two chapters on one page: the first is that page only"
        assert bk.chapters({"pages": 5, "toc": [[1, "Late", 9]]})[0]["start"] == 5, "an entry beyond the last page is put on it"
        # where a page is
        assert bk.label(info, 13) == "Section 1.1 · file page 13" and bk.label(info, 5) == "Introduction · file page 5" and bk.label(info, 10) == "Chapter One · file page 10", "the innermost entry holding the page, the later one on a shared page"
        assert bk.label(dict(info, offset=9), 13) == "Section 1.1 · p. 4" and bk.label(dict(info, offset=9), 5) == "Introduction · front matter (file page 5)" and bk.label(dict(info, offset=9), 10) == "Chapter One · p. 1"
        assert bk.label(dict(info, source="epub", offset=9), 13) == "Section 1.1 · location 13", "an EPUB has no printed pages: chapter and location"
        assert bk.label({"source": "pdf", "pages": 9, "toc": []}, 3) == "file page 3"
        # the offset and a search inside one chapter, in a book that is really in a library
        import subprocess
        home, loc = tempfile.mkdtemp(prefix="jw-home-"), tempfile.mkdtemp(prefix="jw-local-")
        try:
            child = r'''
import json, os, sys
os.environ["JW_OFFLINE"] = "1"
sys.path.insert(0, %r)
import paths
paths.seed_home()
import books as bk, importer, fulltext as ft, pipeline as pl, pymupdf
d = pymupdf.open()
for i in range(1, 31):
    word = "alpha" if i < 10 else "bravo" if i < 20 else "charlie"
    pg = d.new_page(); pg.insert_textbox(pymupdf.Rect(50, 50, 545, 780), "Page %%d. The platform %%s%%d governs the ecosystem with clear rules. " %% (i, word, i) * 6 + "".join(os.urandom(8).hex() for _ in range(40)), fontsize=11)
d.set_toc([[1, "Introduction", 1], [1, "Chapter One", 10], [1, "Chapter Two", 20]]); d.embfile_add("padding", os.urandom(45000))
data = d.tobytes(); d.close()
a = bk.analyse(data, "book.pdf", hint_pdf=True)
rel = importer.confirm(a["token"], {"kind": "book", "title": "A Test Book About Platforms", "authors": "T. Author", "pub_date": "2020", "source": "hand"})["file"]
info = bk.info_of(rel)
assert [(c["title"], c["start"], c["end"]) for c in bk.chapters(info)] == [("Introduction", 1, 10), ("Chapter One", 10, 20), ("Chapter Two", 20, 30)], bk.chapters(info)
# the offset
assert info["offset"] == 0
for bad in (-1, 30, 99, "abc", None, "", 1.5):
    try:
        bk.set_offset(rel, bad); raise AssertionError("offset %%r accepted" %% (bad,))
    except ValueError:
        pass
assert bk.info_of(rel)["offset"] == 0, "a refused offset changes nothing"
assert bk.set_offset(rel, " 9 ")["offset"] == 9 and bk.info_of(rel)["offset"] == 9 and bk.label(bk.info_of(rel), 13) == "Chapter One · p. 4"
for bad in ("Papers/nothing.pdf", "", None, "../x.pdf"):
    try:
        bk.set_offset(bad, 1); raise AssertionError("offset for %%r accepted" %% (bad,))
    except ValueError:
        pass
full = os.path.join(paths.ROOT, rel[:-4] + ".book.json"); raw = open(full).read()
open(full, "w").write(json.dumps(dict(json.loads(raw), source="epub")))
try:
    bk.set_offset(rel, 3); raise AssertionError("an EPUB took an offset")
except ValueError as ex:
    assert "no printed pages" in str(ex)
open(full, "w").write(raw)
# a search inside one chapter: only its pages, with the words that say where
rows = pl.library()
run = lambda q, pages=None: next(m for m in ft.stream(rows, q, pages) if m["type"] == "results")
whole = run("platform")
assert {i["page"] for i in whole["items"]} and len(whole["items"]) <= 3, "the whole book: at most three passages per paper"
ch1 = run("platform", {"file": rel, "from": 10, "to": 19})
pages = sorted(i["page"] for i in ch1["items"])
assert ch1["searched"] == 1 and pages and all(10 <= p <= 19 for p in pages) and len(pages) > 3, pages                       # (more than three: inside one chapter the paper limit is lifted)
assert all(i["where"].startswith("Chapter One · p. ") for i in ch1["items"]) and ch1["items"][0]["where"] == "Chapter One · p. %%d" %% (ch1["items"][0]["page"] - 9), ch1["items"][0]
assert run("charlie", {"file": rel, "from": 10, "to": 19})["n"] == 0 and run("charlie")["n"] >= 1, "words of another chapter are not found inside this one"
assert run("platform", {"file": "Papers/other.pdf", "from": 1, "to": 5})["searched"] == 0
assert all("where" not in i or i["where"] for i in whole["items"])
print("OK")
''' % HERE
            env = {k: v for k, v in os.environ.items() if k not in ("JW_HOME", "JW_LOCAL")}
            r = subprocess.run([sys.executable, "-c", child], env=dict(env, JW_HOME=home, JW_LOCAL=loc), capture_output=True, text=True, timeout=180)
            assert r.returncode == 0 and r.stdout.strip().endswith("OK"), (r.stdout + r.stderr)[-1800:]
        finally:
            shutil.rmtree(home, ignore_errors=True); shutil.rmtree(loc, ignore_errors=True)
    check("books: the outline as chapter page ranges, where a page is in words you could cite (chapter and printed page; location for an EPUB), the printed-page offset (checked, kept), and a search inside one chapter", t_book_chapters)
    def t_plain_names():
        """HTML entities in titles and journal names (Crossref and some feeds send "&amp;", sometimes twice) are turned back into characters when a paper is saved, when
        the library is listed (rows saved before), and when a venue name comes from OpenAlex or Crossref; ordinary text is left alone."""
        import pipeline as pl_, fetch_new as fx_
        assert dl.plain("Journal of Economic Behavior &amp; Organization") == "Journal of Economic Behavior & Organization"
        assert dl.plain("A &amp;amp; B") == "A & B" and dl.plain("R&D and M&A; prices") == "R&D and M&A; prices" and dl.plain("") == "" and dl.plain(None) == ""
        assert dl.plain("&#8220;Generate&#8221; the Future") == "\u201cGenerate\u201d the Future"
        name, _, _ = fx_.venue_route({"container": "Journal of Economic Behavior &amp;amp; Organization", "issn": []}, "", "10.1016/x", [])
        assert name == "Journal of Economic Behavior & Organization", name
        n1_, a1_, _ = fx_.venue_route({"container": "Journal of Economic Behavior &amp; Organization", "issn": []}, "", "10.1016/x", [])
        assert n1_ == "Journal of Economic Behavior & Organization" and a1_ == fx_.venue_abbr("Journal of Economic Behavior &amp; Organization"), a1_      # (the abbreviation as before: no second group for papers saved earlier)
        saved_dl = (dl.PAPERS, dl.ROOT); dl.PAPERS, dl.ROOT = pl_.PAPERS, pl_.ROOT          # (the library the pipeline lists)
        idx_ = os.path.join(dl.PAPERS, "_index.csv"); saved_ = open(idx_, encoding="utf-8").read() if os.path.exists(idx_) else None
        pdf_ = os.path.join(dl.PAPERS, "2025-01", "JEBO", "amp.pdf"); os.makedirs(os.path.dirname(pdf_), exist_ok=True); open(pdf_, "wb").write(b"%PDF")
        try:
            import csv as _csv
            dl.save_index({"doi": "10.9/amp1", "title": "Winners &amp; losers", "journal": "Journal of Economic Behavior &amp; Organization", "abbr": "JEBO"}, os.path.relpath(pdf_, dl.ROOT), "ok")
            row = [r for r in _csv.DictReader(open(idx_, newline="", encoding="utf-8")) if r["doi"] == "10.9/amp1"][0]
            assert row["journal"] == "Journal of Economic Behavior & Organization" and row["title"] == "Winners &amp; losers", row       # (the title as it came: a paper without a DOI is keyed by it)
            with open(idx_, "a", newline="", encoding="utf-8") as fh:            # a row written by an older version, still escaped
                w = _csv.DictWriter(fh, dl.INDEX_COLS); w.writerow(dict(row, doi="10.9/amp2", title="Old &amp; escaped", journal="Old &amp;amp; Journal"))
                w.writerow(dict(row, doi="", title="No DOI &amp;amp; an old title", journal="J"))      # (keyed by its title as stored)
            import folders as fo_, readstate as rs_
            fid_ = fo_.create("Amp test")["id"]; fo_.add(fid_, ["no doi &amp;amp; an old title"]); rs_.update("no doi &amp;amp; an old title", {"status": "reading"})
            try:
                lib_ = {r["doi"] or r["title"]: r for r in pl_.library()}
                assert lib_["10.9/amp2"]["journal"] == "Old & Journal" and lib_["10.9/amp2"]["title"] == "Old & escaped", lib_["10.9/amp2"]
                assert lib_["10.9/amp1"]["title"] == "Winners & losers"
                nd_ = lib_["No DOI & an old title"]
                assert nd_["key"] == "no doi &amp;amp; an old title" and fid_ in nd_["folders"] and nd_["state"].get("status") == "reading", (nd_["key"], nd_["folders"], nd_["state"])
                import importer as im_
                assert im_._same({"doi": "", "title": "No DOI &amp;amp; an old title"}, {"doi": "", "title": "No DOI & an old title"}), "'&amp;' and '&' are the same title"
            finally:
                fo_.delete(fid_); rs_.update("no doi &amp;amp; an old title", {"status": ""})
        finally:
            if saved_ is None:
                os.path.exists(idx_) and os.remove(idx_)
            else:
                open(idx_, "w", encoding="utf-8").write(saved_)
            os.path.exists(pdf_) and os.remove(pdf_)
            dl.PAPERS, dl.ROOT = saved_dl
    check("names: '&amp;' in titles and journal names is shown as '&' (saved, listed, and from OpenAlex/Crossref), and ordinary '&' is left alone", t_plain_names)
    def t_api_identity():
        """The scholarly databases (OpenAlex, Crossref, Unpaywall, Semantic Scholar, Open Library, the price list) are told plainly who is asking: the app's name, version
        and project page, never an e-mail address. Publisher pages and feeds are asked as before (a browser's User-Agent): only the listed API hosts change."""
        import urllib.request as ur_
        import fetch_new as fx_, download as dl_, updater as up_
        seen = []
        class R:
            headers = {}
            status = 200
            def __init__(s, req): s.req = req
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def read(s): return b'{"message": {"items": []}, "results": [], "data": []}'
            def geturl(s): return s.req.full_url
        real, old_url, old_v = ur_.urlopen, os.environ.get("JW_OPENALEX_URL"), os.environ.get("JW_VERSION")
        ur_.urlopen = lambda req, timeout=None: (seen.append((req.full_url, req.get_header("User-agent"))), R(req))[1]
        os.environ["JW_OPENALEX_URL"] = "http://127.0.0.1:9/"; os.environ["JW_VERSION"] = "9.8.7"
        try:
            fx_.http_get("https://api.crossref.org/works?query.bibliographic=x")
            fx_.http_get("https://api.openalex.org/works?search=x")
            fx_._openalex("works?search=y")                          # through the mirror/test address: still named
            dl_.http_get("https://api.crossref.org/works/10.1/x")
            dl_.try_unpaywall("10.1/x", "me@example.org")
            dl_.http_get("https://api.semanticscholar.org/graph/v1/paper/DOI:10.1/x")
            dl_.http_get("https://api.elsevier.com/content/article/doi/10.1/x")               # Elsevier's keyed API (not its web pages)
            fx_.http_get("https://www.example-journal.org/rss.xml")      # a feed
            dl_.http_get("https://www.publisher.example/doi/pdf/10.1/x")    # a publisher's PDF
            dl_.http_get("https://api.crossref.org.evil.example/x")          # (not a listed host, only a look-alike)
        finally:
            ur_.urlopen = real
            for k, v in (("JW_OPENALEX_URL", old_url), ("JW_VERSION", old_v)):
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        ua = dict(seen)
        api = [u for u, _ in seen if "example" not in urllib.parse.urlsplit(u).hostname]
        assert len(api) == 7, seen
        for u in api:
            assert ua[u].startswith("JournalWatcher/9.8.7 (") and "github.com/fjc1991/journal-watcher" in ua[u] and "@" not in ua[u] and "Mozilla" not in ua[u], (u, ua[u])
        for u in ("https://www.example-journal.org/rss.xml", "https://www.publisher.example/doi/pdf/10.1/x", "https://api.crossref.org.evil.example/x"):
            assert ua[u].startswith("Mozilla/5.0"), (u, ua[u])
        assert "@" not in up_.user_agent() and up_.user_agent().startswith("JournalWatcher/")
    check("identity: the scholarly databases (OpenAlex, Crossref, Unpaywall, Semantic Scholar) are told the app's name, version and project page (never an e-mail address); feeds and publisher pages are asked as before", t_api_identity)
    def t_keychain_keys():
        """On a Mac a pasted API key goes to that Mac's Keychain (not into a file in a folder that is synced to other computers); a key already in the .env file can be moved there; a Keychain
        key wins over an old line; what cannot be held there stays in the file."""
        import subprocess
        home, loc, fake = tempfile.mkdtemp(prefix="jw-home-"), tempfile.mkdtemp(prefix="jw-local-"), os.path.join(tempfile.mkdtemp(prefix="jw-kc-"), "kc.json")
        try:
            child = r'''
import json, os, sys
sys.path.insert(0, %r)
import paths
paths.seed_home()
import download as dl, ai, keychain
env_path = os.path.join(paths.ROOT, ".env")
open(env_path, "w").write("# my keys\nANTHROPIC_API_KEY=sk-ant-old-in-file\nOPENAI_API_KEY=sk-in-file-too\nUNPAYWALL_EMAIL=me@example.org\nOPENROUTER_API_KEY=sk-or-has$dollar\n")
assert keychain.available() and keychain.get("ANTHROPIC_API_KEY") == ""
# a pasted key goes to the Keychain and its old line leaves the file; the other lines stay
ai.set_key("anthropic", "  ANTHROPIC_API_KEY=sk-ant-new-key-123  ")
assert keychain.get("ANTHROPIC_API_KEY") == "sk-ant-new-key-123"
txt = open(env_path).read()
assert "sk-ant" not in txt and "UNPAYWALL_EMAIL=me@example.org" in txt and "# my keys" in txt and "OPENAI_API_KEY=sk-in-file-too" in txt, txt
assert dl.load_env()["ANTHROPIC_API_KEY"] == "sk-ant-new-key-123" and ai._key("ANTHROPIC_API_KEY")
c = ai.config()
assert c["keychain"] is True and c["providers"]["anthropic"]["key_where"] == "Keychain" and c["providers"]["anthropic"]["configured"], c["providers"]["anthropic"]
assert "Keychain" in ai.set_key("anthropic", "sk-ant-second-key-456").get("key_note", "") and keychain.get("ANTHROPIC_API_KEY") == "sk-ant-second-key-456"       # (replaced, not added)
# a Keychain key wins over a line left in the file
open(env_path, "a").write("ANTHROPIC_API_KEY=sk-ant-stale-line\n")
assert dl.load_env()["ANTHROPIC_API_KEY"] == "sk-ant-second-key-456"
# a key with a character the helper cannot take safely stays in the file, as before
ai.set_key("openrouter", "sk-or-abc$def")
assert keychain.get("OPENROUTER_API_KEY") == "" and "OPENROUTER_API_KEY=sk-or-abc$def" in open(env_path).read() and ai.config()["providers"]["openrouter"]["key_where"] == ".env file"
# moving the keys in the file: copied, read back, then their lines are removed; the one that cannot be held stays and says why
r = ai.move_keys_to_keychain()
assert r["moved"] == ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"] and list(r["kept"]) == ["OPENROUTER_API_KEY"], r
txt = open(env_path).read()
assert "OPENAI_API_KEY" not in txt and "ANTHROPIC_API_KEY" not in txt and "OPENROUTER_API_KEY=sk-or-abc$def" in txt and "UNPAYWALL_EMAIL=me@example.org" in txt and "# my keys" in txt, txt
assert keychain.get("OPENAI_API_KEY") == "sk-in-file-too" and ai.config()["providers"]["openai"]["key_where"] == "Keychain"
assert ai.move_keys_to_keychain() == {"moved": [], "kept": {"OPENROUTER_API_KEY": r["kept"]["OPENROUTER_API_KEY"]}}
# names and values the helper refuses
for bad in ("lower", "", "A B", None, "A" * 80):
    try:
        keychain.get(bad); raise AssertionError("name %%r accepted" %% (bad,))
    except ValueError:
        pass
for bad in ("a b", "", "x" * 500, 'q"uote', "back\\slash"):
    try:
        keychain.set("ANTHROPIC_API_KEY", bad); raise AssertionError("value %%r accepted" %% (bad,))
    except ValueError:
        pass
keychain.delete("OPENAI_API_KEY"); assert keychain.get("OPENAI_API_KEY") == ""
print("OK")
''' % HERE
            env = {k: v for k, v in os.environ.items() if k not in ("JW_HOME", "JW_LOCAL", "JW_NO_KEYCHAIN", "JW_KEYCHAIN")}
            r = subprocess.run([sys.executable, "-c", child], env=dict(env, JW_HOME=home, JW_LOCAL=loc, JW_FAKE_KEYCHAIN=fake), capture_output=True, text=True, timeout=120)
            assert r.returncode == 0 and r.stdout.strip().endswith("OK"), (r.stdout + r.stderr)[-1500:]
            # without a Keychain (JW_NO_KEYCHAIN) nothing changes: the key is written to the .env file
            child2 = r'''
import os, sys
sys.path.insert(0, %r)
import paths
paths.seed_home()
import ai, keychain
assert not keychain.available()
ai.set_key("anthropic", "sk-ant-plain-file-key")
assert "ANTHROPIC_API_KEY=sk-ant-plain-file-key" in open(os.path.join(paths.ROOT, ".env")).read() and ai.config()["providers"]["anthropic"]["key_where"] == ".env file" and ai.config()["keychain"] is False
try:
    ai.move_keys_to_keychain(); raise AssertionError("moved without a Keychain")
except ValueError:
    pass
print("OK")
''' % HERE
            home2 = tempfile.mkdtemp(prefix="jw-home-")
            r = subprocess.run([sys.executable, "-c", child2], env=dict(env, JW_HOME=home2, JW_LOCAL=loc, JW_NO_KEYCHAIN="1"), capture_output=True, text=True, timeout=120)
            shutil.rmtree(home2, ignore_errors=True)
            assert r.returncode == 0 and r.stdout.strip().endswith("OK"), (r.stdout + r.stderr)[-1500:]
        finally:
            shutil.rmtree(home, ignore_errors=True); shutil.rmtree(loc, ignore_errors=True); shutil.rmtree(os.path.dirname(fake), ignore_errors=True)
    check("keys: a pasted key goes to this Mac's Keychain and leaves the file, keys in the file can be moved there (read back first), a Keychain key wins over an old line, a key it cannot hold stays in the file, no Keychain = as before", t_keychain_keys)
    def t_source_keys_keychain():
        """The paper sources' keys (OpenAlex, Elsevier key and token) are kept like the AI keys: a key saved in Settings → Paper sources goes to this Mac's Keychain
        and not into the synced .env file; one already in the file is moved with the same button as the AI keys; OpenAlex reads it from there. The Unpaywall email,
        not a secret, stays in the file. Without a Keychain nothing changes."""
        import subprocess
        home, loc, fake = tempfile.mkdtemp(prefix="jw-home-"), tempfile.mkdtemp(prefix="jw-local-"), os.path.join(tempfile.mkdtemp(prefix="jw-kc-"), "kc.json")
        child = r'''
import importlib.util, json, os, sys
sys.path.insert(0, %r)
import paths; paths.seed_home()
import keychain, ai, fetch_new as fx, download as dl
spec = importlib.util.spec_from_file_location("jw_server", os.path.join(paths.DASHBOARD, "server.py")); sv = importlib.util.module_from_spec(spec); spec.loader.exec_module(sv)
sv._check_openalex = lambda k: True            # (no network)
env_path = os.path.join(paths.ROOT, ".env")
open(env_path, "w").write("UNPAYWALL_EMAIL=old@example.org\nELSEVIER_INSTTOKEN=tok-in-file-1\n")
keyc = keychain.available()
assert keyc == bool(os.environ.get("JW_EXPECT_KC")), keyc
st = sv.sources_save({"openalex_key": "oa-key-123", "elsevier_key": "els-key-456", "unpaywall_email": "me@example.org"})
txt = open(env_path).read()
if keyc:
    assert keychain.get("OPENALEX_API_KEY") == "oa-key-123" and keychain.get("ELSEVIER_API_KEY") == "els-key-456", "the keys go to the Keychain"
    assert "oa-key-123" not in txt and "els-key-456" not in txt and "UNPAYWALL_EMAIL=me@example.org" in txt and "old@example.org" not in txt, txt
    assert st["keychain"] is True and st["where"] == {"openalex_key": "Keychain", "elsevier_key": "Keychain", "elsevier_insttoken": ".env file"}, st
    assert st["openalex_key"] and st["elsevier_key"] and st["elsevier_insttoken"] and st["unpaywall_email"] == "me@example.org", st
    # OpenAlex reads its key from the Keychain (nothing in the environment, nothing in the file)
    os.environ.pop("OPENALEX_API_KEY", None); keychain._cache.clear()
    assert fx.openalex_key() == "oa-key-123"
    # the token still in the file is counted with the AI keys and moved by the same button; the Elsevier download reads it from there
    assert "ELSEVIER_INSTTOKEN" in ai.config()["keys_in_env"], ai.config()["keys_in_env"]
    # an old line of the OpenAlex key in the older layout's file (the settings folder, also in the data folder): counted as in a file, and removed by the move
    old_env = os.path.join(dl.BASE, ".env"); assert os.path.abspath(old_env) != os.path.abspath(env_path)
    open(old_env, "a").write("OPENALEX_API_KEY=oa-old-line\n")
    assert "OPENALEX_API_KEY" in ai.config()["keys_in_env"] and sv.sources_state()["where"]["openalex_key"] == ".env file"
    r = ai.move_keys_to_keychain()
    assert r["moved"] == ["ELSEVIER_INSTTOKEN", "OPENALEX_API_KEY"], r
    assert "ELSEVIER_INSTTOKEN" not in open(env_path).read() and keychain.get("ELSEVIER_INSTTOKEN") == "tok-in-file-1" and ai.config()["keys_in_env"] == []
    assert "OPENALEX_API_KEY" not in open(old_env).read() and keychain.get("OPENALEX_API_KEY") == "oa-key-123", "the old line goes; the newer key in the Keychain is kept"
    sv.sources_save({"openalex_key": "oa-key-124"}); open(old_env, "a").write("OPENALEX_API_KEY=oa-old-again\n")
    sv.sources_save({"openalex_key": "oa-key-125"}); assert "OPENALEX_API_KEY" not in open(old_env).read() and keychain.get("OPENALEX_API_KEY") == "oa-key-125"
    os.environ.pop("ELSEVIER_API_KEY", None)
    assert dl.load_env()["ELSEVIER_INSTTOKEN"] == "tok-in-file-1" and dl.load_env()["ELSEVIER_API_KEY"] == "els-key-456"
    # removing a key removes it from the Keychain too
    sv.sources_save({"openalex_key": ""}); assert keychain.get("OPENALEX_API_KEY") == "" and not sv.sources_state()["openalex_key"]
    # a key the Keychain helper cannot take safely goes into the file, as before
    sv.sources_save({"elsevier_key": "els$dollar"}); assert "ELSEVIER_API_KEY=els$dollar" in open(env_path).read()
else:
    assert "OPENALEX_API_KEY=oa-key-123" in txt and "ELSEVIER_API_KEY=els-key-456" in txt and st["keychain"] is False, txt
print("OK")
''' % HERE
        try:
            env = {k: v for k, v in os.environ.items() if k not in ("JW_HOME", "JW_LOCAL", "JW_NO_KEYCHAIN", "JW_KEYCHAIN", "OPENALEX_API_KEY", "ELSEVIER_API_KEY", "ELSEVIER_INSTTOKEN")}
            r = subprocess.run([sys.executable, "-c", child], env=dict(env, JW_HOME=home, JW_LOCAL=loc, JW_FAKE_KEYCHAIN=fake, JW_EXPECT_KC="1"), capture_output=True, text=True, timeout=120)
            assert r.returncode == 0 and r.stdout.strip().endswith("OK"), (r.stdout + r.stderr)[-1500:]
            home2 = tempfile.mkdtemp(prefix="jw-home-")
            r = subprocess.run([sys.executable, "-c", child], env=dict(env, JW_HOME=home2, JW_LOCAL=loc, JW_NO_KEYCHAIN="1"), capture_output=True, text=True, timeout=120)
            shutil.rmtree(home2, ignore_errors=True)
            assert r.returncode == 0 and r.stdout.strip().endswith("OK"), (r.stdout + r.stderr)[-1500:]
        finally:
            shutil.rmtree(home, ignore_errors=True); shutil.rmtree(loc, ignore_errors=True); shutil.rmtree(os.path.dirname(fake), ignore_errors=True)
    check("keys: the OpenAlex and Elsevier keys go to this Mac's Keychain (not the synced .env file), one in the file is moved with the AI keys, OpenAlex reads it from there, the Unpaywall email stays in the file; no Keychain = as before", t_source_keys_keychain)

    def t_real_keychain():
        """The real macOS `security` tool, in a temporary keychain: the key goes in on standard input, comes back, is replaced, and is deleted."""
        import subprocess
        d = tempfile.mkdtemp(prefix="jw-kc-")
        kc = os.path.join(d, "test.keychain-db")
        try:
            subprocess.run(["security", "create-keychain", "-p", "pw", kc], check=True, capture_output=True, timeout=30)
            subprocess.run(["security", "unlock-keychain", "-p", "pw", kc], check=True, capture_output=True, timeout=30)
            child = r'''
import os, sys
sys.path.insert(0, %r)
import keychain
assert keychain.available() and keychain.get("JW_TEST_KEY") == ""
keychain.set("JW_TEST_KEY", "sk-test-abc123_XYZ-789")
assert keychain.get("JW_TEST_KEY") == "sk-test-abc123_XYZ-789"
keychain.set("JW_TEST_KEY", "sk-test-second-key")
assert keychain.get("JW_TEST_KEY") == "sk-test-second-key" and keychain.values(["JW_TEST_KEY", "NOPE"]) == {"JW_TEST_KEY": "sk-test-second-key"}
keychain.delete("JW_TEST_KEY")
assert keychain.get("JW_TEST_KEY") == "" and keychain.values(["JW_TEST_KEY"]) == {}
print("OK")
''' % HERE
            env = {k: v for k, v in os.environ.items() if k not in ("JW_FAKE_KEYCHAIN", "JW_NO_KEYCHAIN")}
            r = subprocess.run([sys.executable, "-c", child], env=dict(env, JW_KEYCHAIN=kc), capture_output=True, text=True, timeout=120)
            assert r.returncode == 0 and r.stdout.strip().endswith("OK"), (r.stdout + r.stderr)[-1500:]
        finally:
            subprocess.run(["security", "delete-keychain", kc], capture_output=True, timeout=30)
            shutil.rmtree(d, ignore_errors=True)
    if sys.platform == "darwin":
        check("keys: the real macOS Keychain (a temporary keychain): set, read back, replace, delete", t_real_keychain)

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
        assert usage.cost() > 0 and sum(m["cost"] for m in usage.d.get("models", {}).values()) > 0 and usage.detail()["models"][0]["provider"] == "fake"     # a non-Claude model is counted at its own price, under its own name
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
        st_ = [m for m in msgs if m["type"] == "status"]           # (the page's progress bar: four steps, and the papers counted while their reference lists are read)
        assert [m.get("step") for m in st_][:1] == [1] and st_[-1].get("step") == 4 and all(m.get("steps") == 4 for m in st_), [(m.get("step"), m.get("msg")) for m in st_]
        loop_ = [m for m in st_ if m.get("step") == 2 and "total" in m]
        assert loop_ and all(m["total"] == len(LIBRARY) for m in loop_) and [m["done"] for m in loop_ if "PDF" not in m["msg"]] == list(range(len(LIBRARY))), [(m.get("done"), m.get("msg")) for m in loop_]
        assert st_[-1]["msg"].startswith("Comparing") and msgs[-2] is st_[-1]
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

    SIGS = {}          # path -> the signature text served there (a signed feed has /appcast.json.sig)

    def feed_body(path):
        return json.dumps(FEEDS[path]).replace("%d", str(feed_srv.server_address[1])).encode()

    class FeedH(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path == "/j.zip":
                body = ZIPB
            elif self.path in FEEDS:
                body = feed_body(self.path)
            elif self.path in SIGS:
                body = SIGS[self.path].encode()
            elif self.path.endswith("/redir.json"):                       # a redirect to somewhere with nothing, carrying a token in its query
                self.send_response(302); self.send_header("Location", "/nothing?token=secret123"); self.end_headers(); return
            elif self.path.startswith("/status/"):                       # the feed address answers with this HTTP status
                self.send_response(int(self.path.split("/")[2])); self.end_headers(); return
            elif self.path == "/html":                                    # a Wi-Fi sign-in page where the feed should be
                body = b"<html><body>Sign in to the Wi-Fi</body></html>"
            elif self.path == "/empty":
                body = b""
            elif self.path == "/shape":                                   # JSON, but not a feed
                body = b"[1, 2]"
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
    def t_up_problems():
        import http.client as _hc, socket as _sk, ssl as _ssl, urllib.error as _ue
        base = "http://127.0.0.1:%d" % feed_srv.server_address[1]
        saved = (up.load_state(), os.environ["JW_UPDATE_FEED"])
        try:
            def look(url):
                os.environ["JW_UPDATE_FEED"] = url
                return up.check()
            # what went wrong is told by the kind of failure, with the HTTP status; the words are the same whoever asked
            for path, kind, code in [("/nothing", "not_found", 404), ("/status/410", "not_found", 410), ("/status/403", "denied", 403), ("/status/401", "denied", 401), ("/status/429", "busy", 429),
                                     ("/status/503", "busy", 503), ("/status/418", "http", 418), ("/html", "garbled", 0), ("/empty", "garbled", 0), ("/shape", "garbled", 0)]:
                st = look(base + path)
                assert st["problem"] and st["problem"]["kind"] == kind and st["problem"]["code"] == code, (path, st["problem"])
                assert st["checked_ok"] is False and st["error"], (path, st)
                assert not st["error"].startswith("Could not reach"), (path, st["error"])                                              # a server that answered was reached
            assert look(base + "/nothing")["problem"]["url"] == base + "/nothing"                                                  # the address that answered is kept for the Details
            red = look(base + "/o/r/releases/latest/download/redir.json")["problem"]                                              # ... without a token in its query, and as the final address after a redirect
            assert red["kind"] == "not_found" and red["url"] == base + "/nothing" and "secret" not in json.dumps(up.public()) and "secret" not in open(up.STATE_FILE).read(), red     # (neither on the page's data nor on disk)
            assert up._bare("https://user:pw@example.org:8443/a/b?token=x#f") == "https://example.org:8443/a/b" and up._bare("file:///etc/passwd") == "" and up._bare("") == ""
            st = look("http://127.0.0.1:1/appcast.json")                                                                          # nobody listens
            assert st["problem"]["kind"] == "offline" and st["error"].startswith("Could not reach"), st
            # a server that does not speak HTTP at all must not break the check (it once raised out of check())
            srv = _sk.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(2)
            def junk():
                for _ in range(2):
                    try:
                        c, _a = srv.accept(); c.recv(2048); c.sendall(b"garbage line\r\n\r\n"); c.close()
                    except OSError:
                        return
            threading.Thread(target=junk, daemon=True).start()
            st = look("http://127.0.0.1:%d/appcast.json" % srv.getsockname()[1])
            assert st["problem"]["kind"] == "garbled", st["problem"]
            srv.close()
            # by the type of the exception: a certificate, a timeout, a reset, a surprise
            for exc, kind in [(_ssl.SSLError(1, "[SSL: DECRYPTION_FAILED_OR_BAD_RECORD_MAC] bad mac"), "tls"), (_ue.URLError(_ssl.SSLCertVerificationError("certificate verify failed")), "tls"), (_ue.URLError(_sk.timeout("timed out")), "timeout"), (TimeoutError("timed out"), "timeout"),
                              (ConnectionResetError(54, "Connection reset by peer"), "offline"), (_hc.RemoteDisconnected("closed"), "offline"), (_hc.IncompleteRead(b"x"), "garbled"),
                              (UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"), "garbled"), (up.BadAddress("must start with https://"), "address"), (RuntimeError("boom"), "other")]:
                assert up.classify(exc)[0] == kind, (exc, up.classify(exc))
            real = up.urllib.request.urlopen
            up.urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("something nobody planned for"))
            try:
                st = look(base + "/appcast.json")
            finally:
                up.urllib.request.urlopen = real
            assert st["problem"]["kind"] == "other" and st["error"], st                                                           # check() never raises, whatever happens
            for bad in ("https://exämple.org/appcast.json", "https://example.org/app cast.json", "https://example.org/a\nb"):      # no server is involved: not 'a page that cannot be read'
                assert look(bad)["problem"]["kind"] == "address", bad
            # nothing to ask is not a check: no 'last checked' stamp, no problem, the page says there is no address
            up._write(up.STATE_FILE, {"checked": "2026-10-01T00:00:00+00:00"})
            before = "2026-10-01T00:00:00+00:00"
            os.environ["JW_UPDATE_FEED"] = ""
            real_bi = up.built_in_feed; up.built_in_feed = lambda: ""
            try:
                st = up.check()
            finally:
                up.built_in_feed = real_bi
            assert st["feed"] is False and st["problem"] is None and st["error"] == "" and up.load_state().get("checked") == before, st
            # a look at one address says nothing about another: after the address is changed (or cleared) the old failure and the old 'checked' are not shown for the new one
            look(base + "/status/403")
            assert up.public()["problem"]["kind"] == "denied"
            os.environ["JW_UPDATE_FEED"] = base + "/appcast.json"
            st = up.public()
            assert st["problem"] is None and st["checked"] == "" and st["checked_ok"] is False and st["error"] == "", st
            # a state file written before 1.2: an unknown failure is 'something unexpected', a bare HTTP error keeps its meaning, a good look stays a good look
            up._write(up.STATE_FILE, {"checked": "2026-10-01T00:00:00+00:00", "error": "old message"})
            st = up.public()
            assert st["problem"] == {"kind": "other", "code": 0, "url": ""} and st["checked_ok"] is False and st["error"] == "old message", st
            up._write(up.STATE_FILE, {"checked": "2026-10-01T00:00:00+00:00", "error": "HTTP Error 404: Not Found"})
            assert up.public()["problem"] == {"kind": "not_found", "code": 404, "url": ""}
            up._write(up.STATE_FILE, {"checked": "2026-10-01T00:00:00+00:00", "error": "", "latest": "1.1.0"})
            st = up.public()
            assert st["checked_ok"] is True and st["ok_at"] == "2026-10-01T00:00:00+00:00" and st["problem"] is None, st
            up._write(up.STATE_FILE, {"checked": "x", "error": "boom", "error_kind": "busy", "error_code": "abc"})                  # a damaged number must not break the page's data
            assert up.public()["problem"]["code"] == 0
            # a feed that names your version is no problem and is 'checked_ok'; a failure after a success keeps the last answer and says there is a problem
            up._write(up.STATE_FILE, {})
            FEEDS["/appcast.json"]["version"] = "1.0.0"
            st = look(base + "/appcast.json")
            assert st["problem"] is None and st["checked_ok"] is True and st["available"] is False and st["ok_at"] and st["error"] == "", st
            FEEDS["/appcast.json"]["version"] = "1.1.0"
            assert look(base + "/appcast.json")["available"] is True
            st = look(base + "/status/503")
            assert st["available"] is True and st["latest"] == "1.1.0" and st["problem"]["kind"] == "busy" and st["checked_ok"] is False, st
            assert look(base + "/appcast.json")["problem"] is None
        finally:
            FEEDS["/appcast.json"]["version"] = "1.1.0"
            os.environ["JW_UPDATE_FEED"] = saved[1]
            up._write(up.STATE_FILE, saved[0])

    def t_up_no_release():
        """A 404 from the address built into the app, before it has ever answered, is 'nothing published yet'; every other 404 is a problem."""
        base = "http://127.0.0.1:%d" % feed_srv.server_address[1]
        saved = (up.load_state(), os.environ["JW_UPDATE_FEED"], up.settings())
        rel = base + "/o/r/releases/latest/download/appcast.json"                  # shaped like the built-in address; the server has nothing there
        try:
            up.save_settings({"feed": ""})
            up._write(up.STATE_FILE, {})
            os.environ["JW_UPDATE_FEED"] = rel
            st = up.check()
            assert st["no_release"] is True and st["problem"]["kind"] == "not_found" and st["source"] == "test", st
            os.environ["JW_UPDATE_FEED"] = base + "/o/r/elsewhere/appcast.json"      # another kind of address
            assert up.check()["no_release"] is False
            os.environ["JW_UPDATE_FEED"] = base + "/o/r/releases/latest/download/redir.json"      # the right kind of address, but the 404 came after a redirect
            st = up.check()
            assert st["no_release"] is False and st["problem"]["kind"] == "not_found", st
            os.environ["JW_UPDATE_FEED"] = rel
            up._write(up.STATE_FILE, {"ok_at": "2026-10-01T00:00:00+00:00"})        # it answered once: now a 404 is something that went wrong
            st = up.check()
            assert st["no_release"] is False and st["problem"]["kind"] == "not_found", st
            up._write(up.STATE_FILE, {})
            os.environ.pop("JW_UPDATE_FEED"); up.save_settings({"feed": rel})        # an address you typed yourself is never 'nothing published yet'
            st = up.check()
            assert st["no_release"] is False and st["source"] == "yours" and st["problem"]["kind"] == "not_found", st
            up.save_settings({"feed": ""})
            os.environ["JW_UPDATE_FEED"] = base + "/status/403"                      # only a 404 is read as 'nothing yet'
            assert up.check()["no_release"] is False
        finally:
            os.environ["JW_UPDATE_FEED"] = saved[1]
            up.save_settings({"feed": saved[2]["feed"]})
            up._write(up.STATE_FILE, saved[0])
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
        assert p["mode"] == "openathens" and p["configured"] and p["signs_in"] and p["ebsco_home"].endswith("/c/x") and p["home"] == "", p   # a library.json from before modes existed still works (its EBSCO address is not the library's web site)
        assert si.via("https://a.org/x y") == "https://go.openathens.net/redirector/myuni.example.edu?url=https%3A%2F%2Fa.org%2Fx%20y"
        q = si.save_profile({"name": "Uni of Somewhere", "mode": "ezproxy", "ezproxy": "https://ezproxy.somewhere.edu/login?url=", "ill_email": "ill@somewhere.edu"})
        assert q["mode"] == "ezproxy" and q["signs_in"] and "redirector" not in json.load(open(si.PROFILE_FILE)) and q["ebsco_home"].endswith("/c/x"), q   # the old OpenAthens redirector goes, notes you did not touch stay
        assert si.via("https://a.org/p") == "https://ezproxy.somewhere.edu/login?url=https://a.org/p"
        assert si.save_profile({"mode": "openathens", "openathens": "somewhere.ac.uk"})["configured"] and si.via("https://a.org/").startswith("https://go.openathens.net/redirector/somewhere.ac.uk?url=")
        v = si.save_profile({"mode": "vpn", "name": ""})
        assert v["configured"] and not v["signs_in"] and si.via("https://a.org/") == "https://a.org/"                              # on the VPN there is no address of its own (and no name is needed)
        # what people paste is tidied, not refused (1.2.0 refused these, and a working set-up ended up saved as something else)
        assert si.save_profile({"mode": "openathens", "openathens": "https://go.openathens.net/redirector/myuni.example.edu?url=https%3A%2F%2Fwww.jstor.org%2F"})["openathens"] == "myuni.example.edu"
        assert si.save_profile({"openathens": " Uni.ac.uk "})["openathens"] == "uni.ac.uk"
        assert si.save_profile({"openathens": "uniname"})["openathens"] == "uniname"                                               # (a domain without a dot, accepted before, still is)
        assert si.save_profile({"home": "library.uni.ac.uk"})["home"] == "https://library.uni.ac.uk"
        assert si.save_profile({"mode": "ezproxy", "ezproxy": "https://ezproxy.uni.edu/login?url=https://www.jstor.org/stable/1"})["ezproxy"] == "https://ezproxy.uni.edu/login?url="
        e = si.save_profile({"ebsco_home": "https://research.ebsco.com/c/XXXXXX/"})
        assert e["ebsco_home"] == "https://research.ebsco.com/c/XXXXXX" and e["use_ebsco"], e
        assert si.save_profile({"use_ebsco": False})["use_ebsco"] is False and si.save_profile({"use_ebsco": True})["use_ebsco"] is True
        si.save_profile({"mode": "vpn"})
        for bad in ({"mode": "wifi"}, {"ezproxy": "http://insecure.edu/login?url="}, {"ezproxy": "https://ezproxy.edu/login"}, {"openathens": "Example University"}, {"openathens": "https://library.uni.ac.uk/"}, {"openathens": "go.openathens.net"}, {"openathens": "https://login.openathens.net/auth/x"},
                    {"ill_email": "not an email"}, {"home": "ftp://x"}, {"ebsco_home": "https://search.example.com/c/x"}):
            try:
                si.save_profile(bad); raise AssertionError("accepted %s" % bad)
            except ValueError:
                pass
        assert not si.save_profile({"ebsco_home": ""})["use_ebsco"]                                                                 # no start page: no EBSCO route (the tests after this one assume none)
    def t_si_status():
        assert not si.status()["informs"]["signed_in"]
        si.record("informs", True); si.record("library", True); si.record("library", False)
        st = si.status()
        assert st["informs"]["signed_in"] and st["informs"]["age_hours"] < 0.1 and st["library"]["signed_in"] and not st["ais"]["signed_in"], st            # a failed attempt does not undo a good sign-in
        d = json.load(open(si.STATUS_FILE)); d["library"]["at"] = "2020-01-01T00:00:00+00:00"; json.dump(d, open(si.STATUS_FILE, "w"))
        st = si.status()
        assert not st["library"]["signed_in"] and st["library"]["expired"] and st["informs"]["signed_in"], st["library"]                                  # an old one is "expired", not forgotten
        si.record("nonsense", True); assert "nonsense" not in json.load(open(si.STATUS_FILE))
        si.record("ais", False); st = si.status()
        assert not st["ais"]["signed_in"] and st["ais"]["failed_at"], st["ais"]                                                                            # a first attempt that fails is noticed too (the page says so)
        si.forget("informs"); assert not si.status()["informs"]["signed_in"]
    def t_si_login_pages():
        assert brw.looks_like_login("https://login.openathens.net/auth/x") and brw.looks_like_login("https://login.microsoftonline.com/common") and brw.looks_like_login("https://idp.uni.edu/idp/profile/SAML2/Redirect/SSO")
        assert not brw.looks_like_login("https://research.ebsco.com/c/XXXXXX/search/results?q=DI+10.1000%2Fx") and not brw.looks_like_login("https://www.tandfonline.com/doi/full/10.1080/07421222.2026.1")
        si.save_profile({"name": "Uni", "mode": "openathens", "openathens": "uni.ac.uk", "home": "https://lib.uni.ac.uk"})
        pages = brw.login_pages()
        assert pages[0][0].startswith("Uni") and pages[0][1].startswith("https://go.openathens.net/redirector/uni.ac.uk?url=https%3A%2F%2Fwww.sciencedirect.com"), pages[0]   # the sign-in goes through a protected publisher page
        assert pages[1][1] == "https://lib.uni.ac.uk" and len(pages) == len(brw.PUBLISHER_LOGINS) + 2 and not any("myuni" in u for _, u in pages), pages[:2]         # the library's own site is opened as it is (it signs nobody in)
        # the steps of a sign-in, as the test and the sign-in window recognise them: the redirector itself is where every attempt starts, so it is not one
        assert not brw.signin_step("https://go.openathens.net/redirector/uni.ac.uk?url=https%3A%2F%2Fwww.sciencedirect.com%2F")
        assert brw.signin_step("https://auth.elsevier.com/ShibAuth/institutionLogin?entityID=https%3A%2F%2Fidp.uni.ac.uk%2Fopenathens&appReturnURL=x")
        assert brw.signin_step("https://login.openathens.net/auth/uni.ac.uk/o/123") and brw.signin_step("https://idp.uni.edu/idp/profile/SAML2/Redirect/SSO?x=1")
        assert brw.signin_step("https://ezproxy.uni.edu/login?url=https://www.jstor.org/") and not brw.signin_step("https://www.sciencedirect.com/science/article/pii/S1")
        start = "https://ezproxy.uni.edu/login?url=https%3A%2F%2Fwww.sciencedirect.com%2F"
        assert brw._steps([start, "https://www.sciencedirect.com/"], start, "https://www.sciencedirect.com/") == []                # EZproxy sent the browser straight on: no sign-in happened
        assert brw._steps(["https://pub.org/ssostart", "https://pub.org/ssostart"], "https://go.openathens.net/redirector/u.ac.uk?url=x", "https://pub.org/ssostart") == []   # the final page is not a step before itself
        assert brw._steps(["https://go.openathens.net/redirector/u.ac.uk?url=x", "https://auth.elsevier.com/ShibAuth/institutionLogin?e=1", "https://www.sciencedirect.com/"],
                          "https://go.openathens.net/redirector/u.ac.uk?url=x", "https://www.sciencedirect.com/") == ["https://auth.elsevier.com/ShibAuth/institutionLogin?e=1"]
        assert brw._site_key("https://www.sciencedirect.com/") == "sciencedirect" and brw._site_key("https://onlinelibrary.wiley.com/x") == "wiley" and brw._site_key("https://www.bbc.co.uk/") == "bbc"
    def t_si_detect():
        site = {
            "https://lib.uni.ac.uk": '<a href="/about/">About</a><a href="/a-to-z/">A-Z databases</a><a href="/off-campus/">Off-campus access</a><a href="/news/">News</a><a href="/wp-content/x.pdf">databases guide (pdf)</a>',
            "https://lib.uni.ac.uk/a-to-z/": '<a href="/databases/acs/">ACS</a><a href="/databases/sciencedirect/">ScienceDirect</a><a href="/databases/jstor/">JSTOR</a><a href="/databases/business-source-ultimate/">Business Source Ultimate</a><a href="https://other.org/databases/x/">elsewhere</a>',
            "https://lib.uni.ac.uk/databases/sciencedirect/": '<a href="https://go.openathens.net/redirector/uni.ac.uk?url=https%3A%2F%2Fwww.sciencedirect.com%2F">Access</a>',
            "https://lib.uni.ac.uk/databases/jstor/": '<a href="https://go.openathens.net/redirector/uni.ac.uk?url=http%3A%2F%2Fwww.jstor.org%2F">Access</a><a href="https://go.openathens.net/redirector/uni.ac.uk?url=x">again</a>',
            "https://lib.uni.ac.uk/databases/business-source-ultimate/": '<a href="https://research.ebsco.com/c/abc123">EBSCO</a>',
        }
        opened = []
        def fetch(u):
            opened.append(u)
            if u not in site:
                raise OSError("404")
            return u, site[u]
        r = si.detect("lib.uni.ac.uk", fetch=fetch, verify=lambda d: (True, "auth.elsevier.com"))
        assert r["mode"] == "openathens" and r["openathens"] == "uni.ac.uk" and r["verified"] is True and r["ebsco_home"] == "https://research.ebsco.com/c/abc123", r
        assert "seen in 3 links" in r["message"] and "OpenAthens confirms" in r["message"] and "EBSCO" in r["message"], r["message"]
        assert opened.index("https://lib.uni.ac.uk/databases/sciencedirect/") < opened.index("https://lib.uni.ac.uk/databases/acs/") if "https://lib.uni.ac.uk/databases/acs/" in opened else True   # a big publisher's page first
        assert not any("other.org" in u or u.endswith(".pdf") for u in opened), opened                                  # never another site, never a file
        # a pasted link that already shows the answer: nothing is opened
        opened.clear()
        r = si.detect("https://go.openathens.net/redirector/uni.ac.uk?url=https%3A%2F%2Fwww.jstor.org%2F", fetch=fetch, verify=lambda d: (True, "x"))
        assert r["mode"] == "openathens" and r["openathens"] == "uni.ac.uk" and opened == [], (r, opened)
        # OpenAthens says the domain is not one of its customers: the answer says so plainly
        r = si.detect("https://go.openathens.net/redirector/wrong.pt?url=x", fetch=fetch, verify=lambda d: (False, "OpenAthens says its redirector is not enabled for “wrong.pt”: that is not your library's OpenAthens domain"))
        assert r["verified"] is False and "not your library's OpenAthens domain" in r["message"], r
        # EZproxy, as a prefix and as a rewritten host name
        r = si.detect("https://ezproxy.uni.edu/login?url=https://www.jstor.org/stable/1", fetch=fetch)
        assert r["mode"] == "ezproxy" and r["ezproxy"] == "https://ezproxy.uni.edu/login?url=", r
        r = si.detect("https://www-sciencedirect-com.ezproxy.uni.edu/science/article/pii/S1", fetch=fetch)
        assert r["mode"] == "ezproxy" and r["ezproxy"] == "https://ezproxy.uni.edu/login?url=", r
        # a malformed link does not end the search; a start page that redirects to the university's main site is followed there; LibGuides counts as the library's
        site3 = {"https://library.uni.edu": '<a href="http://[broken/x">bad</a><a href="https://uni.libguides.com/az.php">A-Z databases</a>',
                 "https://uni.libguides.com/az.php": '<a href="https://go.openathens.net/redirector/uni.edu?url=a">1</a><a href="https://go.openathens.net/redirector/uni.edu?url=b">2</a><a href="https://go.openathens.net/redirector/uni.edu?url=c">3</a>'}
        r = si.detect("https://library.uni.edu", fetch=lambda u: (u, site3[u]), verify=lambda d: (True, "x"))
        assert r["openathens"] == "uni.edu" and len(r["pages"]) == 2, r
        site4 = {"https://lib.uni.edu": "", "https://www.uni.edu/library/": '<a href="/library/databases/">Databases</a>', "https://www.uni.edu/library/databases/": '<a href="https://go.openathens.net/redirector/uni.edu?url=a">x</a>'}
        r = si.detect("https://lib.uni.edu", fetch=lambda u: ("https://www.uni.edu/library/", site4["https://www.uni.edu/library/"]) if u == "https://lib.uni.edu" else (u, site4[u]), verify=lambda d: (True, "x"))
        assert r["openathens"] == "uni.edu", r
        # OCLC-hosted EZproxy, as a prefix and a rewritten host; a prefix and its proxied hosts are one proxy; an unrelated host with "proxy" in its name is not one
        c = si.scan('<a href="https://login.ezproxy.lib.uni.edu/login?qurl=https%3A%2F%2Fx">a</a> https://www-jstor-org.ezproxy.lib.uni.edu/stable/1 https://www.proxy-checker.com/ https://uni.idm.oclc.org/login?url=https://y https://www-jstor-org.uni2.idm.oclc.org/x')
        assert c["ezproxy"] == {"https://login.ezproxy.lib.uni.edu/login?qurl=": 1, "https://uni.idm.oclc.org/login?url=": 1, "https://uni2.idm.oclc.org/login?url=": 1}, c["ezproxy"]
        # nothing to be found: said, and nothing invented
        site2 = {"https://plain.edu": '<a href="/databases/">Databases</a>', "https://plain.edu/databases/": "<p>Use the campus network.</p>"}
        r = si.detect("https://plain.edu", fetch=lambda u: (u, site2[u]), verify=lambda d: (_ for _ in ()).throw(AssertionError("no domain to verify")))
        assert r["mode"] == "" and not r["openathens"] and "No OpenAthens or EZproxy link was found on 2 pages" in r["message"], r
        # OpenAthens itself is asked the way the app does it (the redirect it answers with, or its 403)
        assert si.verify_openathens("uni.ac.uk", fetch_status=lambda u: (302, "https://auth.elsevier.com/ShibAuth/institutionLogin?x"))[0] is True
        ok_, why_ = si.verify_openathens("typo.example", fetch_status=lambda u: (403, ""))
        assert ok_ is False and "typo.example" in why_
        assert si.verify_openathens("uni.ac.uk", fetch_status=lambda u: (_ for _ in ()).throw(OSError("offline")))[0] is None
    check("library access: a mode per way of getting in, old files understood, bad addresses refused, notes kept", t_si_profile)
    check("find my library's settings: OpenAthens, EZproxy and EBSCO read from the library's own pages (big publishers' pages first, never another site or a file), a pasted link read without opening anything, the domain confirmed with OpenAthens, nothing invented when nothing is there", t_si_detect)
    check("sign-in status: a sign-in counts for as long as it normally lasts, then says expired", t_si_status)
    check("sign-in pages: your library first, then the publishers' own institution pages (no university built in)", t_si_login_pages)
    def t_celebrate():
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            try:
                b = pw.chromium.launch()
            except Exception as e:  # noqa: BLE001
                if "Executable doesn't exist" in str(e):
                    print("  (skipped: Chromium is not installed here: python -m playwright install chromium)")
                    return
                raise
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
    check("updates: a failed look says what kind of problem it was (not found, refused, busy, offline, not a feed, TLS, timeout), by the failure and not its wording; the same words whoever asked; a server that does not speak HTTP does not break it; no address is not a check", t_up_problems)
    check("updates: a 404 from the built-in release address that never answered is 'nothing published yet'; a typed address, a feed that worked before, another path or another status is a problem", t_up_no_release)
    check("updates: choices are saved (automatic checks, the address must be https) and a test feed overrides them", t_up_settings)
    check("updates: a download with the wrong checksum is refused, and something that is not an app is not installed", t_up_download)
    check("updates: the helper waits for the old app to quit, swaps the new one in, and puts the old one back if that fails", t_up_swap)
    check("updates: make_appcast.py writes a feed the app accepts, with the last release as history", t_up_appcast_script)
    # ---- signed feeds: the app believes a feed only when the author's key signed its exact bytes (scripts/updater.py, app/sign_release.py)
    import importlib.util
    import stat as _stat
    _spec = importlib.util.spec_from_file_location("sign_release", os.path.abspath(os.path.join(HERE, "..", "..", "..", "..", "app", "sign_release.py")))
    srel = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(srel)

    def _must_not_verify(data, sig, keys, why):
        try:
            up.verify_feed(data, sig, keys)
        except up.FeedNotTrusted:
            return
        raise AssertionError("accepted: " + why)

    def t_sig_rules():
        assert up.SIG_PREFIX == srel.SIG_PREFIX                                           # the script and the app sign and check the same thing
        sk, other = srel.nacl_signing().SigningKey.generate(), srel.nacl_signing().SigningKey.generate()
        pub, data = srel.public_b64(sk), b'{"version": "1.2.0"}'
        sig = srel.sign_bytes(sk, data)
        keys = [bytes(sk.verify_key)]
        assert up.verify_feed(data, sig, keys) is True
        _must_not_verify(data + b" ", sig, keys, "changed bytes")
        _must_not_verify(data, srel.sign_bytes(other, data), keys, "another key's signature")
        _must_not_verify(data, base64.b64encode(sk.sign(data).signature).decode(), keys, "a signature made without the app's prefix")
        _must_not_verify(data, "not base64!", keys, "garbage")
        _must_not_verify(data, base64.b64encode(b"short").decode(), keys, "a short signature")
        _must_not_verify(data, sig, [], "no usable key")
        assert up.verify_feed(data, sig, [bytes(other.verify_key), bytes(sk.verify_key)]) is True      # a second key may be listed (a key change: apps that trust both)
        env = os.environ.get("JW_UPDATE_KEYS")
        try:
            os.environ["JW_UPDATE_KEYS"] = ""
            assert up.trusted_keys() is None                                              # no key: nothing is checked, as in a project run
            os.environ["JW_UPDATE_KEYS"] = "# only a comment\n"
            assert up.trusted_keys() is None
            os.environ["JW_UPDATE_KEYS"] = pub + "  # the author\n" + srel.public_b64(other)
            assert up.trusted_keys() == [bytes(sk.verify_key), bytes(other.verify_key)], up.trusted_keys()
            os.environ["JW_UPDATE_KEYS"] = "garbage\n" + base64.b64encode(b"too short").decode()
            assert up.trusted_keys() == []                                                # a damaged key file must not switch the check off
        finally:
            if env is None:
                os.environ.pop("JW_UPDATE_KEYS", None)
            else:
                os.environ["JW_UPDATE_KEYS"] = env

    def t_sig_check():
        sk, other = srel.nacl_signing().SigningKey.generate(), srel.nacl_signing().SigningKey.generate()
        os.environ["JW_UPDATE_KEYS"] = srel.public_b64(sk)
        try:
            FEEDS["/appcast.json"]["version"] = "1.1.0"
            assert up.public()["available"] is False and up.public()["signed"] is True and up.public()["keys"] == 1      # what an unchecked feed said earlier is not offered once a key is set
            up._progress.update({"phase": "idle", "pct": 0, "error": ""})
            up._run_install()
            assert "not checked against the signing key" in up._progress["error"], up._progress                          # …and cannot be installed either
            up._progress.update({"phase": "idle", "pct": 0, "error": ""})
            assert "not signed" in up.check()["error"] and up.public()["problem"]["kind"] == "unsigned"                  # no .sig at all
            os.environ["JW_UPDATE_FEED"] = FURL.replace("/appcast.json", "/html")                                        # a Wi-Fi sign-in page where the feed is, and no .sig: not 'a release is out'
            assert up.check()["problem"]["kind"] == "garbled"
            SIGS["/html.sig"] = "<html>Sign in to the Wi-Fi</html>"                                                      # ... and the same page where the signature is: not a signature at all, not a bad one
            assert up.check()["problem"]["kind"] == "garbled"
            os.environ["JW_UPDATE_FEED"] = FURL
            SIGS["/appcast.json.sig"] = srel.sign_bytes(sk, feed_body("/appcast.json")) + "\n"
            st = up.check()
            assert st["error"] == "" and st["available"] is True and st["latest"] == "1.1.0" and st["signed"] is True, st
            cached = up.load_state()
            assert base64.b64decode(cached["feed_data"], validate=True) == feed_body("/appcast.json") and cached["feed_signature"].strip() == SIGS["/appcast.json.sig"].strip()
            cached.update({"latest": "9.9.9", "url": "http://127.0.0.1:1/unsigned.zip", "sha256": "0" * 64, "size": 1})
            up._write(up.STATE_FILE, cached)
            st = up.public()
            assert st["available"] is True and st["latest"] == "1.1.0", st
            downloads = []
            old_download = up._download
            def record_download(*args):
                downloads.append(args)
                raise ValueError("stop after verifying cached metadata")
            up._download = record_download
            try:
                up._progress.update({"phase": "idle", "pct": 0, "error": ""})
                up._run_install()
            finally:
                up._download = old_download
            assert len(downloads) == 1 and downloads[0][0] == FEEDS["/appcast.json"]["url"] % feed_srv.server_address[1] and downloads[0][2:] == (len(ZIPB), hashlib.sha256(ZIPB).hexdigest()), downloads
            up._write(up.STATE_FILE, dict(cached, feed_data=base64.b64encode(feed_body("/appcast.json").replace(b"1.1.0", b"9.9.9")).decode("ascii")))
            assert up.public()["available"] is False and up.public()["latest"] == ""                                       # saved bytes that no longer match their signature are not believed
            up._write(up.STATE_FILE, cached)
            assert up.public()["available"] is True
            os.environ["JW_UPDATE_KEYS"] = srel.public_b64(other)
            assert up.public()["available"] is False
            downloads.clear()
            up._progress.update({"phase": "idle", "pct": 0, "error": ""})
            up._run_install()
            assert not downloads and "not checked against the signing key" in up._progress["error"], up._progress
            os.environ["JW_UPDATE_KEYS"] = srel.public_b64(sk)
            FEEDS["/appcast.json"]["version"] = "9.9.9"                                                                  # the feed changes after it was signed
            st = up.check()
            assert "not signed with this app" in st["error"] and st["latest"] == "1.1.0" and st["problem"]["kind"] == "bad_signature", st      # refused; the last checked answer is kept
            SIGS["/appcast.json.sig"] = srel.sign_bytes(other, feed_body("/appcast.json"))
            assert "not signed with this app" in up.check()["error"]                                                      # signed, but by someone else
            os.environ["JW_UPDATE_KEYS"] = srel.public_b64(other) + "\n" + srel.public_b64(sk)
            st = up.check()
            assert st["error"] == "" and st["latest"] == "9.9.9" and st["keys"] == 2, st                                  # either of two listed keys will do
            os.environ["JW_UPDATE_KEYS"] = "not a key\n"                                                                  # a key file that has lines but no usable key: nothing is believed
            assert up.check()["problem"]["kind"] == "no_verifier"
            os.environ["JW_UPDATE_KEYS"] = ""
            SIGS.clear()
            FEEDS["/appcast.json"]["version"] = "1.1.0"
            st = up.check()
            assert st["error"] == "" and st["latest"] == "1.1.0" and st["signed"] is False and st["problem"] is None, st   # no key built in: unsigned feeds work as before
        finally:
            os.environ.pop("JW_UPDATE_KEYS", None)
            SIGS.clear()
            FEEDS["/appcast.json"]["version"] = "1.1.0"
            up.check()

    def t_sign_script():
        import contextlib
        d = tempfile.mkdtemp(prefix="jw-sign-")
        cwd0 = os.getcwd()
        try:
            keyfile = os.path.join(d, "keys", "release-signing-key")
            out_ = io.StringIO()
            with contextlib.redirect_stdout(out_):
                srel.main(["keygen", "--key", keyfile])
            pub = re.search(r"^([A-Za-z0-9+/=]{44})$", out_.getvalue(), re.M).group(1)
            assert stat.S_IMODE(os.stat(keyfile).st_mode) == 0o600 and "BACK IT UP" in out_.getvalue()                     # readable by you only, and it says to keep a copy
            try:
                srel.main(["keygen", "--key", keyfile]); raise AssertionError("a key was overwritten")
            except SystemExit:
                pass
            os.chdir(d)
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    srel.main(["keygen", "--key", "bare-key"])                                                            # a bare file name (the current folder) works too
            finally:
                os.chdir(cwd0)
            assert stat.S_IMODE(os.stat(os.path.join(d, "bare-key")).st_mode) == 0o600
            rel_dir = os.path.join(d, "release"); os.makedirs(rel_dir)
            zip_bytes = os.urandom(2000)
            name = "Journal-Watcher-1.2.0.zip"
            feed = {"app": "Journal Watcher", "version": "1.2.0", "released": "2026-10-28", "notes": ["One"], "url": "https://github.com/x/y/releases/download/v1.2.0/" + name,
                    "sha256": hashlib.sha256(zip_bytes).hexdigest(), "size": len(zip_bytes), "history": []}
            outdir = os.path.join(d, "out"); sig_path = os.path.join(outdir, "appcast.json.sig")

            def put(zb=zip_bytes, fd=feed):
                open(os.path.join(rel_dir, name), "wb").write(zb)
                open(os.path.join(rel_dir, "appcast.json"), "w").write(json.dumps(fd, indent=1))

            def run(tag="v1.2.0", repo="x/y"):
                a = ["sign", "--dir", rel_dir, "--repo", repo, "--key", keyfile, "--out", outdir, "--yes"] + (["--tag", tag] if tag else [])
                with contextlib.redirect_stdout(io.StringIO()) as o:
                    srel.main(a)
                return o.getvalue()

            def refused(why, phrase, **kw):
                put(*kw.pop("files", ()))
                try:
                    run(**kw)
                    raise AssertionError("signed " + why)
                except SystemExit as e:
                    assert isinstance(e.code, str) and phrase in e.code, (why, e.code)          # our own refusal and its reason, not argparse's exit status
                assert not os.path.exists(sig_path), why
            put()
            shown = run()
            sig = open(sig_path).read()
            assert "checksum" in shown and "One" in shown and feed["url"] in shown                                         # it shows what is being signed, and where the feed sends people
            assert up.verify_feed(open(os.path.join(rel_dir, "appcast.json"), "rb").read(), sig, [base64.b64decode(pub)]) is True      # and the app accepts it
            with contextlib.redirect_stdout(io.StringIO()):
                srel.main(["verify", "--appcast", os.path.join(rel_dir, "appcast.json"), "--sig", sig_path, "--pub", pub])
            os.remove(sig_path)
            run(tag=None)                                                                                                  # no --tag: it is the feed's version, and the address is still checked
            assert os.path.exists(sig_path); os.remove(sig_path)
            refused("a zip that is not the one in the feed", "checksum", files=(zip_bytes[::-1],))
            refused("a feed whose zip size is not the zip's size", "bytes", files=(zip_bytes, {**feed, "size": 5}))
            evil = {**feed, "url": "https://evil.example/" + name}
            refused("a feed pointing somewhere else", "release's own zip", files=(zip_bytes, evil))
            refused("a feed pointing somewhere else, with no --tag either", "release's own zip", files=(zip_bytes, evil), tag=None)
            refused("a feed that belongs to another repository", "release's own zip", repo="someone/else")
            refused("a tag that is not the feed's version", "do not match", tag="v1.3.0")
        finally:
            os.chdir(cwd0)
            shutil.rmtree(d, ignore_errors=True)

    check("signed feeds: only the author's key counts (changed bytes, another key, no prefix, garbage and short signatures are refused; two keys may be listed; a damaged key file does not switch the check off)", t_sig_rules)
    check("signed feeds: with a key set, an unsigned or wrongly signed feed is ignored (and what an unchecked feed said earlier is not offered or installed), a signed one is believed; with no key nothing changes", t_sig_check)
    def t_sig_selfcheck():
        r = subprocess.run([sys.executable, os.path.join(HERE, "updater.py")], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0 and "update signature check: ok" in r.stdout, (r.returncode, r.stdout, r.stderr[-300:])      # what build_app.sh runs on the finished app
    check("signed feeds: the build's own self-check (python updater.py) passes", t_sig_selfcheck)
    check("signing script: makes a private key readable only by you (and will not overwrite it), shows what it signs, refuses a zip, size, address, tag or repository that does not match the feed, and its signature is accepted by the app", t_sign_script)
    feed_srv.shutdown()
    os.environ.pop("JW_VERSION", None); os.environ.pop("JW_UPDATE_FEED", None)


    # ---- the author's page and the support reminder (support.py)
    def _with_support_files(fn):
        d = tempfile.mkdtemp(prefix="jw-support-")
        old = (sp.AUTHOR, sp.FILE, paths.STATIC)
        try:
            os.makedirs(os.path.join(d, "static"))
            open(os.path.join(d, "static", "me.jpg"), "wb").write(b"x")
            sp.AUTHOR, sp.FILE, paths.STATIC = os.path.join(d, "author.json"), os.path.join(d, "support.json"), os.path.join(d, "static")
            fn(d)
        finally:
            sp.AUTHOR, sp.FILE, paths.STATIC = old
            shutil.rmtree(d, ignore_errors=True)

    def _author(**kw):
        base = {"name": "Ada Lovelace", "support": {"service": "Ko-fi", "url": "https://ko-fi.com/ada"}}
        base.update(kw)
        json.dump(base, open(sp.AUTHOR, "w"))

    def t_support_author():
        def body(d):
            a = sp.author()          # no file yet: everything empty, no tip page, the service name defaults
            assert a["name"] == "" and a["bio"] == [] and a["links"] == [] and a["photo"] == "" and a["support"] == {"service": "Ko-fi", "url": ""}, a
            open(sp.AUTHOR, "w").write("{not json")
            assert sp.author()["name"] == "" and sp.public()["due"] is False          # damaged: treated like empty
            _author(name="  Ada   Lovelace ", role="R" * 500, university="U", university_url="http://insecure.example", photo="../../etc/passwd", bio=["one", "", "two"],
                    links=[{"label": "Site", "url": "https://example.org"}, {"label": "Bad", "url": "javascript:alert(1)"}, {"label": "Mail", "url": "mailto:a@b.org"}, {"label": "", "url": "https://x.org"}, "nope"],
                    support={"service": "Ko-fi", "url": "http://tips.example"})
            a = sp.author()
            assert a["name"] == "Ada Lovelace" and len(a["role"]) == 120 and a["university_url"] == "" and a["photo"] == "" and a["bio"] == ["one", "two"], a
            assert [x["label"] for x in a["links"]] == ["Site", "Mail"], a["links"]          # only https and mailto links, each with a label
            assert a["support"]["url"] == "", a["support"]                                    # a tip page must be https
            _author(photo="sub/me.jpg", support={"service": "Buy Me a Coffee", "url": "https://buymeacoffee.com/ada"})
            a = sp.author()
            assert a["photo"] == "me.jpg" and a["support"] == {"service": "Buy Me a Coffee", "url": "https://buymeacoffee.com/ada"}, a      # only a file name inside static/ is used
        _with_support_files(body)

    def t_support_rules():
        DAY, t0 = 86400, 1_800_000_000

        def body(d):
            _author()
            sp.visit(t0)
            s = sp._state()
            assert s["first"] == t0 and s["visits"] == 1, s
            sp.visit(t0 + 60)                                  # still the same visit
            assert sp._state()["visits"] == 1
            sp.visit(t0 + 60 + 31 * 60)                        # 31 minutes after the last look: a new visit
            assert sp._state()["visits"] == 2
            for k in range(1, 5):
                sp.visit(t0 + k * DAY)
            assert sp._state()["visits"] == 6
            assert sp.public(t0 + 14 * DAY - 1)["due"] is False and sp.public(t0 + 14 * DAY)["due"] is True      # 14 days since the first start, with 5 visits or more
            sp.act("shown", now=t0 + 14 * DAY)
            assert sp.public(t0 + 14 * DAY + 1)["due"] is False and sp.public(t0 + 74 * DAY - 1)["due"] is False and sp.public(t0 + 74 * DAY)["due"] is True      # then 60 days of quiet
            sp.act("shown", now=t0 + 74 * DAY)
            sp.act("shown", now=t0 + 134 * DAY)
            assert sp._state()["shown"] == 3 and sp.public(t0 + 1000 * DAY)["due"] is False          # never more than three times
        _with_support_files(body)

        def body2(d):
            _author()
            for k in range(4):
                sp.visit(t0 + k * DAY)
            assert sp.public(t0 + 30 * DAY)["due"] is False                                          # four visits are too few, however long ago the first was
            sp.visit(t0 + 20 * DAY)
            assert sp.public(t0 + 30 * DAY)["due"] is True
            sp.act("never", now=t0 + 31 * DAY)
            assert sp.public(t0 + 400 * DAY)["due"] is False and sp.public()["optout"] is True       # "Don't ask again"
            assert sp.act("reminders", True, now=t0 + 32 * DAY)["due"] is True and sp.act("reminders", False)["optout"] is True      # the switch in About
            sp.act("reminders", True)
            sp.act("supported", now=t0 + 33 * DAY)
            assert sp.public(t0 + 400 * DAY)["due"] is False and sp.public()["supported"] is True    # a click on the tip link ends it
            assert sp.act("reminders", True, now=t0 + 400 * DAY)["due"] is True                      # switching reminders on again lifts that too
            try:
                sp.act("bogus")
                raise AssertionError("an unknown action was accepted")
            except ValueError:
                pass
            open(sp.FILE, "w").write("[1, 2")                                                        # a damaged count file starts again instead of failing
            assert sp.public()["visits"] == 0 and sp.public()["due"] is False
        _with_support_files(body2)

        def body3(d):
            json.dump({"name": "Ada"}, open(sp.AUTHOR, "w"))                                         # no tip page: never due
            for k in range(8):
                sp.visit(t0 + k * DAY)
            assert sp.public(t0 + 60 * DAY)["due"] is False
        _with_support_files(body3)

    check("support: the author's page keeps only safe fields (https and mailto links, a photo inside static/, trimmed text); a missing or damaged file is simply empty", t_support_author)
    def t_support_shipped():
        raw = json.load(open(os.path.join(paths.STATIC, "author.json"), encoding="utf-8"))
        a = sp.author()                                                                           # the real file, cleaned the way the page sees it
        assert len(a["links"]) == len(raw.get("links") or []), (raw.get("links"), a["links"])      # a link that is not https or mailto, or has no label, would vanish without a word
        for k in ("university_url", "photo"):
            assert bool(raw.get(k)) == bool(a[k]), (k, raw.get(k), a[k])
        assert bool((raw.get("support") or {}).get("url")) == bool(a["support"]["url"]), raw.get("support")      # the tip page must be https
        assert bool(raw.get("repo")) == bool(a["repo"]) and len(a["bio"]) == len(raw.get("bio") or [])
    check("support: nothing in the shipped dashboard/static/author.json is silently dropped when the page cleans it (https links, a photo that exists, an https tip page)", t_support_shipped)
    check("support: the reminder is due after 14 days and 5 visits, then 60 days apart, at most three times, never after 'Don't ask again' or a click on the tip link, never without a tip page", t_support_rules)

    print("downloads: a PDF is only a paper if it is that paper")
    import identity as idt
    import pymupdf as _pm
    ID_T = "The Pitfalls of Review Solicitation: Evidence from a Natural Experiment on TripAdvisor"
    ID_D = "10.1287/mnsc.2024.0001"
    ID_E = {"title": ID_T, "doi": ID_D, "authors": ["Jane Doe", "Richard Roe"]}
    FILL = ("We estimate how asking customers for reviews changes the reviews that platforms receive, using a natural experiment in which hotels were treated at different times. " * 6)

    def mkpdf(pages, pad=True, scan=False, meta_title=None, logo=False):
        """pages: list of (big_title, body) pairs; a pdf with real text (or, with scan, only a picture per page); padded past is_pdf's 40 kB when pad."""
        d = _pm.open()
        for big, body in pages:
            pg = d.new_page()
            if scan:
                pm = _pm.Pixmap(_pm.csRGB, _pm.IRect(0, 0, 200, 200), False); pm.set_rect(pm.irect, (200, 200, 200)); pg.insert_image(pg.rect, pixmap=pm)
                if body:
                    pg.insert_textbox(_pm.Rect(60, 20, 540, 60), body, fontsize=8)          # a stamp on top of the picture
                continue
            if logo:
                pm = _pm.Pixmap(_pm.csRGB, _pm.IRect(0, 0, 60, 20), False); pm.set_rect(pm.irect, (10, 10, 120)); pg.insert_image(_pm.Rect(60, 30, 120, 50), pixmap=pm)
            if big:
                pg.insert_textbox(_pm.Rect(60, 60, 540, 130), big, fontsize=18)
            pg.insert_textbox(_pm.Rect(60, 150 if big else 60, 540, 790), body, fontsize=9)
        if meta_title:
            d.set_metadata({"title": meta_title})
        if pad:
            d.embfile_add("pad.bin", os.urandom(60000))
        out = d.tobytes(garbage=0); d.close()
        return out

    RIGHT = mkpdf([(ID_T, "Jane Doe and Richard Roe. https://doi.org/%s " % ID_D + FILL)] + [("", FILL)] * 3)
    WRONG = mkpdf([("Two-stage differences in differences", "John Gardner. This version: April 2021. " + FILL)] + [("", FILL)] * 3)

    def t_identity_check():
        v = lambda entry, data: idt.check(entry, data)
        r = v(ID_E, RIGHT); assert r[0] == "match" and "DOI" in r[1], r                                                         # the DOI is printed
        r = v(dict(ID_E, doi="10.1287/mnsc.9999.9999"), mkpdf([(ID_T, "Jane Doe and Richard Roe. " + FILL)] + [("", FILL)] * 3)); assert r[0] == "match" and "title" in r[1], r      # no DOI printed: the title
        cover = [("", "Downloaded from pubsonline.informs.org by IP 1.2.3.4. For personal use only.")] + [("", "Copyright notice.")] * 3 + [(ID_T, "Jane Doe and Richard Roe. " + FILL)] + [("", FILL)] * 3
        assert v(dict(ID_E, doi=""), mkpdf(cover))[0] == "match"                                                                 # a cover sheet and thin legal pages before the title page
        r = v(ID_E, WRONG)
        assert r[0] == "mismatch" and "Two-stage differences in differences" in r[1] and r[2]["pdf_says"].startswith("Two-stage"), r      # the reported case: it says what the PDF actually is
        assert v(ID_E, mkpdf([("", "Request access. Purchase this article for $45. Sign in through your institution.")]))[0] == "mismatch"      # a paywall stub
        assert v(ID_E, mkpdf([("", "Request access. Purchase this article for $45.")], logo=True))[0] == "mismatch"               # ... with the publisher's logo on it: still a stub, not a scan
        assert v(ID_E, mkpdf([("", "")] * 2, scan=True))[0] == "unverified"                                                       # a scan: nothing to read, so nothing is claimed
        assert v(ID_E, mkpdf([("", "Downloaded from pubsonline.informs.org by IP 1.2.3.4. For personal use only. " * 3)] * 4, scan=True))[0] == "unverified"      # ... also with a stamp on each page
        assert v(dict(ID_E, doi=""), b"%PDF-1.4 this is not really a pdf" + b"x" * 50)[0] == "unverified"                         # unreadable: not proof of anything
        assert v({"title": "", "doi": ""}, RIGHT)[0] == "unverified"                                                                # nothing on record to compare with
        # the title is too short to be unique: it needs the DOI, the authors, or (when no author is on record) to be the page's own headline
        ed = "Editorial"
        assert v({"title": ed, "doi": "", "authors": ["Ann Lee"]}, mkpdf([("A different editorial", "Ann Lee. Editorial. " + FILL)]))[0] == "match"
        assert v({"title": ed, "doi": "", "authors": ["Bob Ray"]}, mkpdf([("A different editorial", "Ann Lee. Editorial. " + FILL)]))[0] == "mismatch"
        assert v({"title": ed, "doi": "", "authors": []}, mkpdf([("A different editorial", "Editorial. " + FILL)]))[0] == "mismatch"   # too short even when it is on the page
        hbr = mkpdf([("Managing Oneself", "By the author. " + FILL)])                                                              # an HBR article page: no DOI, often no authors on record
        r = v({"title": "Managing Oneself", "doi": "", "authors": []}, hbr); assert r[0] == "match" and "headline" in r[1], r
        assert v({"title": "Managing Oneself", "doi": "", "authors": []}, mkpdf([("Something Else Entirely", "Managing Oneself is mentioned here. " + FILL)]))[0] == "mismatch"
        # a paper that only CITES the expected one does not match through its reference list, whatever the heading is called
        for head in ("References", "REFERENCES", "References and Notes", "Notes and References", "Reference", "References:", "VII. REFERENCES", "R EFERENCES", "7. References"):
            citing = mkpdf([("Another study of hotels", "Some Author. " + FILL + "\n" + head + "\nDoe J, Roe R. %s. Management Science. doi:%s" % (ID_T, ID_D))])
            assert v(ID_E, citing)[0] == "mismatch", (head, v(ID_E, citing))
        # ... but a "References" line near the top (a sidebar of headings) does not cut the title off
        side = mkpdf([(ID_T, "Contents Introduction Methods Results References\nJane Doe and Richard Roe. " + FILL)])
        assert v(dict(ID_E, doi=""), side)[0] == "match", v(dict(ID_E, doi=""), side)
        nav = mkpdf([("", "Contents\nIntroduction\nMethods\nReferences\n" + ID_T + "\nJane Doe and Richard Roe. " + FILL)])      # the same sidebar as lines of its own, read BEFORE the title
        assert v(dict(ID_E, doi=""), nav)[0] == "match", v(dict(ID_E, doi=""), nav)
        assert v(ID_E, mkpdf([("Another paper on hotels", "Jane Doe and Richard Roe. " + FILL)]))[0] == "mismatch"          # same authors, other paper
        # an appendix, a correction or a comment repeats the title and the DOI without being the paper
        for head in ("Online Appendix to " + ID_T, "Correction to: " + ID_T, "Comment on " + ID_T, "Supplementary Material for the paper", "e-Companion to " + ID_T):
            r = v(ID_E, mkpdf([(head, "https://doi.org/%s " % ID_D + FILL)])); assert r[0] == "mismatch" and "appendix" in r[1], (head, r)
        assert v({"title": "Correction to: " + ID_T, "doi": ID_D, "authors": []}, mkpdf([("Correction to: " + ID_T, "https://doi.org/%s " % ID_D + FILL)]))[0] == "match"   # unless that IS the item
        # the PDF's embedded title counts when the page text does not carry it (a print of a web page whose headline is outside the printed part)
        r = v(dict(ID_E, doi=""), mkpdf([("", "Jane Doe. " + FILL)], meta_title=ID_T)); assert r[0] == "match" and "embedded" in r[1], r
        # the title without its subtitle, from a title long enough to stand alone, matches without authors
        assert v({"title": "Platform Governance and the Financial Outcomes of Complementors: Evidence from App Stores", "doi": "", "authors": []},
                 mkpdf([("Platform Governance and the Financial Outcomes of Complementors", FILL)]))[0] == "match"
        # most of a title (about 87 %) is not enough alone: it needs the authors too (a different paper with a similar title is not this one)
        pt = "Platform Governance and the Financial Outcomes of Complementors in Mobile App Stores"
        sister = mkpdf([("Platform Governance and the Financial Outcomes of Complementors in Mobile Markets", "Jane Doe and Richard Roe. " + FILL)])
        r = v({"title": pt, "doi": "", "authors": ["Jane Doe", "Richard Roe"]}, sister); assert r[0] == "match" and "most of its title" in r[1], r
        assert v({"title": pt, "doi": "", "authors": ["Bob Ray"]}, sister)[0] == "mismatch" and v({"title": pt, "doi": "", "authors": []}, sister)[0] == "mismatch"
        # the title without its subtitle needs the authors when that part is itself short ("Unintended Consequences" is on many papers)
        uc = "Unintended Consequences: Evidence from Natural Experiments in Retail"
        other_uc = mkpdf([("Unintended Consequences of Algorithmic Pricing", "Jane Doe and Richard Roe. " + FILL)])
        assert v({"title": uc, "doi": "", "authors": []}, other_uc)[0] == "mismatch" and v({"title": uc, "doi": "", "authors": ["Bob Ray"]}, other_uc)[0] == "mismatch"
        r = v({"title": uc, "doi": "", "authors": ["Jane Doe"]}, other_uc); assert r[0] == "match" and "main title and authors" in r[1], r
        # DOIs: a whole token only, also split by a line break, with trailing punctuation
        assert not idt.doi_found("10.1287/mnsc.2024.000", "https://doi.org/10.1287/mnsc.2024.0001")                              # a prefix of another DOI
        assert idt.doi_found("10.1287/mnsc.2024.0001", "doi:10.1287/mnsc.2024.0001.") and idt.doi_found("10.1287/MNSC.2024.0001", "https://doi.org/10.1287/mnsc.2024.0001")
        assert idt.doi_found("10.1287/mnsc.2024.0001", "https://doi.org/10.1287/mnsc.\n2024.0001") and idt.doi_found("10.1287/mnsc.2024.0001", "https://doi.org/10.1287/\nmnsc.2024.0001") and not idt.doi_found("", "10.1/x")
        # titles: ligatures (kept or lost), hyphenation and line breaks do not matter; a shared subtitle does not make a match
        assert idt.title_score("Platform Governance and Financial Outcomes", "Plat-\nform Governance and Finan-\ncial Outcomes") == 1.0
        assert idt.title_score("The financial flow of firms", "The ﬁnancial ﬂow of ﬁrms") == 1.0
        assert idt.title_score("Efficient Office Workflow Benefits of Affiliate Staff in Offices", "Ecient Oce Workow Benets of Aliate Sta in Oces") == 1.0     # the glyphs were lost
        assert idt.title_score("Peer Effects in Online Ratings: Evidence from a Natural Experiment", ID_T) < 0.6
        # authors: accents, apostrophes, special letters, old LaTeX accents, double surnames, family name first
        for name, printed in [("Søren Møller", "Soren Moller"), ("Mehmet Yıldız", "Mehmet Yildiz"), ("Dennis O'Brien", "Dennis OBrien"), ("Anna D’Amico", "A. D'Amico"),
                              ("Hans Müller", "Hans M¨uller"), ("Ana García", "Ana Gar´cia"), ("María García López", "María García"), ("Tian, Jing", "Jing Tian"), ("Guðmundsson, Jon", "Gudmundsson")]:
            assert idt.authors_score([name], printed) == 1.0, (name, printed, idt.author_names([name]))
        assert idt.authors_score(["Tian, Jing", "Ling Xue", "Maria Garcia-Lopez Jr."], "Tian and Xue, with Garcia") == 1.0 and idt.authors_score([], "x") is None and idt.authors_score(["Wei Li"], "Li") is None
        # a Crossref title with markup and entities still finds the paper that prints it as plain text
        marked = dict(ID_E, doi="", title="Platforms &amp; <i>Bayesian</i> Governance of CO<sub>2</sub> Markets: Evidence from an Experiment")
        plain = mkpdf([("Platforms and Bayesian Governance of CO2 Markets: Evidence from an Experiment", "Jane Doe and Richard Roe. " + FILL)] + [("", FILL)] * 3)
        assert v(marked, plain)[0] == "match", v(marked, plain)
    check("identity: a PDF is matched to its paper by DOI, title and authors; a different work, a stub, an appendix and a citing paper are refused; scans and unreadable files are only 'not checked'", t_identity_check)

    def _with_downloads(body):
        """download_paper against fakes: no network, a scratch library. body(pdfs): pdfs["oa"] is what Unpaywall's link serves."""
        saved = (dl.try_unpaywall, dl.http_get, dl.PAPERS)
        pdfs = {}
        dl.try_unpaywall = lambda doi, email: "http://fake.test/oa.pdf" if pdfs.get("oa") else None
        def fake_get(url, headers=None, timeout=30):
            if url == "http://fake.test/oa.pdf":
                return pdfs["oa"], {}
            if url in pdfs:
                return pdfs[url], {}
            raise OSError("no network in tests: " + url[:50])
        dl.http_get = fake_get
        dl.PAPERS = os.path.join(TMP, "Papers_gate"); os.makedirs(dl.PAPERS, exist_ok=True)
        try:
            body(pdfs)
        finally:
            dl.try_unpaywall, dl.http_get, dl.PAPERS = saved
            shutil.rmtree(os.path.join(TMP, "Papers_gate"), ignore_errors=True)

    class _FakeBrowser:
        """fetch_pdf, the reader, the full-text page and EBSCO each answer from a dict of (bytes, note); the calls are recorded."""
        def __init__(self, data=None, note="browser", reader=None, page=None, ebsco=None, renew=False, via=None):
            self.data, self.note, self.reader, self.page, self.ebsco, self.renew, self.calls, self.via, self.urls = data, note, reader, page, ebsco, renew, [], via, []
        def fetch_pdf(self, url, wait_s=25, doi=None, own=False):
            self.calls.append("fetch_pdf"); self.urls.append(url)
            if self.via and url.startswith(self.via[0]):            # asked for through the library's sign-in
                return self.via[1]
            return self.data, self.note
        def renew_member(self, m): self.calls.append("renew_member"); return self.renew
        def fetch_from_reader(self, url): self.calls.append("reader"); return self.reader or (None, "no reader")
        def page_to_pdf(self, url, min_chars=5000): self.calls.append("page_to_pdf"); return self.page or (None, "no page")
        def fetch_via_ebsco(self, doi, title): self.calls.append("ebsco"); return self.ebsco or (None, "not found in library EBSCO")

    def t_download_gate():
        import csv
        def body(pdfs):
            p = {"journal": "Management Science", "abbr": "MS", "publisher": "informs", "doi": ID_D, "title": ID_T, "url": "https://doi.org/" + ID_D,
                 "pub_date": "2025-03-01", "authors": ["Jane Doe", "Richard Roe"], "topics": []}
            env = {"UNPAYWALL_EMAIL": "me@example.org"}
            rows = lambda: list(csv.DictReader(open(os.path.join(dl.PAPERS, "_index.csv"), newline="")))
            files = lambda: sorted(os.path.join(d, f) for d, _, fs in os.walk(dl.PAPERS) for f in fs if f.endswith(".pdf"))
            def clear():
                for f in files(): os.remove(f)
            # a) the only route gives a different work: refused, nothing stored, said plainly, the paper stays unavailable
            pdfs["oa"] = WRONG
            ok, path, note = dl.download_paper(dict(p), env)
            assert not ok and path is None and "wrong paper via OA (Unpaywall)" in note and "Two-stage differences in differences" in note, note
            assert files() == [] and rows()[-1]["status"] == "wrong_paper" and rows()[-1]["file"] == "" and rows()[-1]["note"].startswith("wrong paper via OA"), rows()
            # b) a different work on the first route, the right one on the next: the right one is stored (the wrong one is not)
            br = _FakeBrowser(RIGHT)
            ok, path, note = dl.download_paper(dict(p), env, browser=br)
            assert ok and note == "browser" and open(path, "rb").read() == RIGHT and len(files()) == 1 and rows()[-1]["status"] == "ok", note
            clear()
            # c) every route gives the different work: not stored, status wrong_paper, the reason is kept
            ok, path, note = dl.download_paper(dict(p), env, browser=_FakeBrowser(WRONG))
            assert not ok and "wrong paper via browser" in note and files() == [] and rows()[-1]["status"] == "wrong_paper", note
            # d) a scan cannot be compared: accepted, and the note says it was not checked
            pdfs["oa"] = mkpdf([("", "")] * 2, scan=True)
            ok, path, note = dl.download_paper(dict(p), env)
            assert ok and "identity not checked" in note and rows()[-1]["status"] == "ok", note
            # e) a dry run checks too, and writes nothing
            clear(); pdfs["oa"] = WRONG
            ok, path, note = dl.download_paper(dict(p), env, dry=True)
            assert not ok and files() == []
            # f) inside the browser route a refused PDF does not end it: the publisher's reader (PNAS) and the full-text page (ACM) still get their turn
            pdfs.pop("oa")
            pn = dict(p, publisher="pnas")
            br = _FakeBrowser(WRONG, reader=(RIGHT, "browser (reader)"))
            ok, path, note = dl.download_paper(pn, env, browser=br)
            assert ok and note == "browser (reader)" and br.calls[:2] == ["fetch_pdf", "reader"] and open(path, "rb").read() == RIGHT, (note, br.calls)
            clear()
            ac = dict(p, publisher="acm")
            br = _FakeBrowser(WRONG, page=(RIGHT, "x"))
            ok, path, note = dl.download_paper(ac, env, browser=br)
            assert ok and note == "full-text page saved as PDF" and "page_to_pdf" in br.calls, (note, br.calls)
            clear()
            # g) a sign-in that is needed stays the reason, when the reader then gives a different work: the card must offer the sign-in, not say "wrong PDF"
            br = _FakeBrowser(None, note="needs_login:www.pnas.org", reader=(WRONG, "browser (reader)"))
            ok, path, note = dl.download_paper(pn, env, browser=br)
            assert not ok and "needs_login" in note and "wrong paper via browser (reader)" in note and rows()[-1]["status"] == "wrong_paper", note
            # h) every other route goes through the same gate (a refused PDF must not crash the batch): Elsevier, a direct link, the library's EBSCO copy, a free copy, an HBR page
            el = dict(p, publisher="elsevier", abbr="DSS", journal="Decision Support Systems")
            saved_el = dl.try_elsevier_api
            dl.try_elsevier_api = lambda doi, key, tok=None: WRONG
            ok, path, note = dl.download_paper(el, {"ELSEVIER_API_KEY": "k"})
            dl.try_elsevier_api = saved_el
            assert not ok and "wrong paper via elsevier-api" in note and files() == [], note
            ai = dict(p, publisher="aisel", abbr="MISQ", journal="MIS Quarterly")
            saved_ai = dl.try_aisel
            dl.try_aisel = lambda q: "http://fake.test/viewcontent.pdf"; pdfs["http://fake.test/viewcontent.pdf"] = WRONG
            ok, path, note = dl.download_paper(ai, {})
            dl.try_aisel = saved_ai
            assert not ok and "wrong paper via aisel-content" in note and files() == [], note
            lib = brw.si.PROFILE_FILE; saved_lib = open(lib).read() if os.path.exists(lib) else None
            json.dump({"mode": "institutional", "ebsco_home": "https://research.ebsco.com/c/x", "via_ebsco": {"journals": ["MS"]}}, open(lib, "w"))
            try:
                ok, path, note = dl.download_paper(dict(p), {}, browser=_FakeBrowser(None, note="needs_login:x", ebsco=(WRONG, "library EBSCO")))
            finally:
                open(lib, "w").write(saved_lib) if saved_lib is not None else os.remove(lib)
            assert not ok and "wrong paper via library EBSCO" in note and files() == [], note
            import open_copies as oc
            saved_c = oc.candidates
            oc.candidates = lambda doi, title, get: iter([("http://fake.test/free.pdf", "preprint (arXiv)")]); pdfs["http://fake.test/free.pdf"] = WRONG
            try:
                ok, path, note = dl.download_paper(dict(p), {})
                assert not ok and "wrong paper via preprint (arXiv)" in note and rows()[-1]["status"] == "wrong_paper" and files() == [], note
            finally:
                oc.candidates = saved_c
            hb = {"journal": "Harvard Business Review", "abbr": "HBR", "publisher": "hbr", "doi": "", "title": "Managing Oneself", "url": "https://hbr.org/x", "pub_date": "2025-03-01", "authors": []}
            ok, path, note = dl.download_paper(hb, {}, browser=_FakeBrowser(page=(WRONG, "page saved as PDF")))
            assert not ok and "wrong paper via page saved as PDF" in note and files() == [], note
            ok, path, note = dl.download_paper(hb, {}, browser=_FakeBrowser(page=(hbr_pdf, "page saved as PDF")))
            assert ok and files() and "identity not checked" not in note, note
        hbr_pdf = mkpdf([("Managing Oneself", "By the author. " + FILL)])
        _with_downloads(body)
    check("downloads: a different work's PDF is refused on every route (nothing stored, status 'wrong_paper', the PDF named), the next route is tried (also inside the browser route), a needed sign-in stays the reason, a scan is kept as 'not checked'", t_download_gate)
    def t_attach_pdf():
        """A PDF you downloaded yourself, added to a paper of a run (its card): checked like a download, filed under the download's name, never summarised by
        itself; a PDF that does not look like the paper waits until you say it is the right one (for that paper only); the folders of an author run and of a
        followed author get it; a download running at the same time never undoes it; the server's endpoints, and two at once."""
        import subprocess
        home, loc, src = tempfile.mkdtemp(prefix="jw-home-"), tempfile.mkdtemp(prefix="jw-local-"), tempfile.mkdtemp(prefix="jw-src-")
        try:
            files = {}
            for name, data in (("right", RIGHT), ("wrong", WRONG), ("scan", mkpdf([("", "")] * 2, scan=True)), ("small", mkpdf([(ID_T, "Jane Doe and Richard Roe. " + FILL[:400])], pad=False))):
                files[name] = os.path.join(src, name + ".pdf")
                with open(files[name], "wb") as f:
                    f.write(data)
            child = ATTACH_CHILD.replace("@HERE@", repr(HERE)).replace("@FILES@", repr(json.dumps(files))).replace("@T@", repr(ID_T)).replace("@D@", repr(ID_D))
            env = {k: v for k, v in os.environ.items() if k not in ("JW_HOME", "JW_LOCAL", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")}
            r = subprocess.run([sys.executable, "-c", child], env=dict(env, JW_HOME=home, JW_LOCAL=loc, JW_NO_KEYCHAIN="1", JW_OFFLINE="1"), capture_output=True, text=True, timeout=300)
            assert r.returncode == 0 and "ALL OK" in r.stdout, (r.stdout[-2000:], r.stderr[-4000:])
        finally:
            for d_ in (home, loc, src):
                shutil.rmtree(d_, ignore_errors=True)
    check("a PDF you downloaded yourself, for a paper whose download failed: checked like a download and filed under its name (never summarised by itself); a PDF that does not look like it waits until you say it is the right paper; folders and followed authors get it; a download running meanwhile never undoes it; the endpoints", t_attach_pdf)

    def t_library_route():
        """The publisher wants a login and your library (OpenAthens or EZproxy) is set up and signed in: the same page is asked for through the library's sign-in."""
        import csv
        sg = brw.si
        OA = "https://go.openathens.net/redirector/testuni.ac.uk?url="
        def body(pdfs):
            p = {"journal": "Wiley Journal", "abbr": "WJ", "publisher": "wiley", "doi": ID_D, "title": ID_T, "url": "https://doi.org/" + ID_D, "pub_date": "2025-03-01", "authors": ["Jane Doe", "Richard Roe"], "topics": []}
            env = {}
            rows = lambda: list(csv.DictReader(open(os.path.join(dl.PAPERS, "_index.csv"), newline="")))
            def clear():
                for d, _, fs in os.walk(dl.PAPERS):
                    for f in fs:
                        if f.endswith(".pdf"): os.remove(os.path.join(d, f))
            sg.save_profile({"mode": "openathens", "openathens": "testuni.ac.uk", "name": "Test University"})
            sg.record("library", True)
            # a) signed in: after the plain attempt wants a login, the library's address gives the PDF
            br = _FakeBrowser(None, note="needs_login:onlinelibrary.wiley.com", via=(OA, (RIGHT, "browser")))
            ok, path, note = dl.download_paper(dict(p), env, browser=br)
            assert ok and note == "browser (via your library)" and len(br.urls) == 2 and br.urls[1].startswith(OA) and open(path, "rb").read() == RIGHT, (note, br.urls)
            assert "doi%2F" in br.urls[1] or "%2Fdoi%2F" in br.urls[1], br.urls[1]                                  # the publisher's own address is what the redirector is given
            clear()
            # b) the plain attempt worked: the library is not asked at all
            br = _FakeBrowser(RIGHT, via=(OA, (WRONG, "browser")))
            ok, path, note = dl.download_paper(dict(p), env, browser=br)
            assert ok and note == "browser" and len(br.urls) == 1, (note, br.urls)
            clear()
            # c) the library gives another work: refused like any other route (the status says so)
            br = _FakeBrowser(None, note="needs_login:onlinelibrary.wiley.com", via=(OA, (WRONG, "browser")))
            ok, path, note = dl.download_paper(dict(p), env, browser=br)
            assert not ok and "wrong paper via browser (via your library)" in note and rows()[-1]["status"] == "wrong_paper", note
            # d) the library's session has ended (it sent the browser to its own sign-in page): the tick goes, and the card offers the library sign-in
            sg.record("library", True)
            br = _FakeBrowser(None, note="needs_login:onlinelibrary.wiley.com", via=(OA, (None, "needs_login:login.openathens.net")))
            ok, path, note = dl.download_paper(dict(p), env, browser=br)
            assert not ok and "needs_library_login" in note and "login.openathens.net" in note and sg.status()["library"]["signed_in"] is False, note
            # e) the tick says "not signed in" (it is only a guess from the clock): the library is tried anyway, because the university's single sign-on often lets a
            #    saved session through; when it works the tick comes back
            assert sg.status()["library"]["signed_in"] is False
            br = _FakeBrowser(None, note="needs_login:onlinelibrary.wiley.com", via=(OA, (RIGHT, "browser")))
            ok, path, note = dl.download_paper(dict(p), env, browser=br)
            assert ok and len(br.urls) == 2 and note == "browser (via your library)" and sg.status()["library"]["signed_in"] is True, (note, br.urls)
            clear()
            # e2) once the library has asked for a password in a batch, the next papers of that batch do not ask it again (each would only wait for a password)
            br = _FakeBrowser(None, note="needs_login:onlinelibrary.wiley.com", via=(OA, (None, "needs_login:login.microsoftonline.com")))
            ok, path, note = dl.download_paper(dict(p), env, browser=br)
            assert not ok and len(br.urls) == 2 and "needs_library_login" in note and br.library_signed_out is True, (note, br.urls)
            ok, path, note = dl.download_paper(dict(p), env, browser=br)
            assert not ok and len(br.urls) == 3 and "needs_library_login" in note, (note, br.urls)                 # one more plain attempt, no second library attempt
            # e3) off campus a publisher may answer the PDF address with an ordinary page (no "log in" words): that also sends the paper through the library
            br = _FakeBrowser(None, note="browser: no PDF at https://onlinelibrary.wiley.com/doi/abs/x", via=(OA, (RIGHT, "browser")))
            ok, path, note = dl.download_paper(dict(p), env, browser=br)
            assert ok and note == "browser (via your library)" and len(br.urls) == 2, (note, br.urls)
            clear()
            # e4) a human check is not a paywall: the library is not asked (the check is the user's to pass)
            br = _FakeBrowser(None, note="challenge:onlinelibrary.wiley.com", via=(OA, (RIGHT, "browser")))
            ok, path, note = dl.download_paper(dict(p), env, browser=br)
            assert not ok and len(br.urls) == 1, (note, br.urls)
            # f) a paper that is about your INFORMS membership keeps asking for that sign-in (the library is still tried when it is signed in)
            ms = dict(p, publisher="informs", abbr="MS", journal="Management Science")
            ok, path, note = dl.download_paper(ms, env, browser=_FakeBrowser(None, note="needs_login:pubsonline.informs.org"))
            assert not ok and "needs_library_login" not in note and "needs_login:pubsonline.informs.org" in note, note
            # f2) the PDF route starts at doi.org (MIS Quarterly and JAIS on the AIS eLibrary; a DSS paper found by an archive search, without its ScienceDirect address).
            #     Through the library it ends on the publisher's own sign-in page again: that is the publisher saying no, not your library asking for a password. The
            #     sign-in tick stays and the next papers of the batch are still tried through the library. (Before, "doi" was compared with the host it ended on, so the
            #     first MISQ paper of a batch cleared the library sign-in and every later paper said "Needs library sign-in" without being tried.)
            sg.record("library", True)
            misq = dict(p, publisher="aisel", abbr="MISQ", journal="MIS Quarterly")
            assert dl.browser_pdf_url(misq).startswith("https://doi.org/")
            br = _FakeBrowser(None, note="needs_login:aisel.aisnet.org", via=(OA, (None, "needs_login:aisel.aisnet.org")))
            ok, path, note = dl.download_paper(dict(misq), env, browser=br)
            assert not ok and len(br.urls) == 2 and br.urls[1].startswith(OA) and "needs_library_login" not in note, (note, br.urls)
            assert not getattr(br, "library_signed_out", False) and sg.status()["library"]["signed_in"] is True, note
            ok, path, note = dl.download_paper(dict(misq), env, browser=br)
            assert not ok and len(br.urls) == 4 and br.urls[3].startswith(OA) and "needs_library_login" not in note, (note, br.urls)   # the next paper still goes through the library
            dss = dict(p, publisher="elsevier", abbr="DSS", journal="Decision Support Systems")
            assert dl.browser_pdf_url(dss).startswith("https://doi.org/")
            br = _FakeBrowser(None, note="browser: no PDF at https://www.sciencedirect.com/science/article/abs/pii/S0167923625000001",
                              via=(OA, (None, "needs_login:www.sciencedirect.com")))
            ok, path, note = dl.download_paper(dict(dss), env, browser=br)
            assert not ok and "needs_library_login" not in note and sg.status()["library"]["signed_in"] is True, note
            # f3) the same doi.org papers, when the library really asks for a password (the university's single sign-on, OpenAthens): still said, still offered
            br = _FakeBrowser(None, note="needs_login:aisel.aisnet.org", via=(OA, (None, "needs_login:sso.testuni.ac.uk")))
            ok, path, note = dl.download_paper(dict(misq), env, browser=br)
            assert not ok and "needs_library_login" in note and "sso.testuni.ac.uk" in note and br.library_signed_out and sg.status()["library"]["signed_in"] is False, note
            sg.record("library", True)
            br = _FakeBrowser(None, note="needs_login:login.openathens.net", via=(OA, (None, "needs_login:login.openathens.net")))    # the plain attempt already met a sign-in page
            ok, path, note = dl.download_paper(dict(misq), env, browser=br)
            assert not ok and "needs_library_login" in note and br.library_signed_out, note
            sg.record("library", True)
            # g) EZproxy: the library's prefix goes in front of the address
            sg.save_profile({"mode": "ezproxy", "ezproxy": "https://ezproxy.testuni.edu/login?url=", "name": "Test University"}); sg.record("library", True)
            br = _FakeBrowser(None, note="needs_login:onlinelibrary.wiley.com", via=("https://ezproxy.testuni.edu/login?url=", (RIGHT, "browser")))
            ok, path, note = dl.download_paper(dict(p), env, browser=br)
            assert ok and note == "browser (via your library)" and br.urls[1].startswith("https://ezproxy.testuni.edu/login?url=http"), (note, br.urls)
            clear()
            # g2) through EZproxy the publisher's paywall comes under the proxy's host name: that is the publisher saying no, not the library asking for a password
            br = _FakeBrowser(None, note="needs_login:onlinelibrary.wiley.com", via=("https://ezproxy.testuni.edu/login?url=", (None, "needs_login:onlinelibrary-wiley-com.ezproxy.testuni.edu")))
            ok, path, note = dl.download_paper(dict(p), env, browser=br)
            assert not ok and "needs_library_login" not in note and not getattr(br, "library_signed_out", False) and sg.status()["library"]["signed_in"], note
            br = _FakeBrowser(None, note="needs_login:onlinelibrary.wiley.com", via=("https://ezproxy.testuni.edu/login?url=", (None, "needs_login:ezproxy.testuni.edu")))
            ok, path, note = dl.download_paper(dict(p), env, browser=br)
            assert not ok and "needs_library_login" in note and br.library_signed_out, note                         # the proxy's own login page: the session has ended
            # g3) a wrong OpenAthens domain: said as such (not "asked for a password"), and not asked again in the batch
            sg.save_profile({"mode": "openathens", "openathens": "testuni.ac.uk"})
            br = _FakeBrowser(None, note="needs_login:onlinelibrary.wiley.com", via=(OA, (None, "library_setup: OpenAthens says the library's domain is not enabled (check Settings → Library access)")))
            ok, path, note = dl.download_paper(dict(p), env, browser=br)
            assert not ok and "library_setup:" in note and "password" not in note and br.library_signed_out, note
            # h) no sign-in of its own (the publishers' own institution pages, VPN, none): nothing changes, nothing is asked for
            for mode in ("institutional", "vpn", "none"):
                sg.save_profile({"mode": mode, "name": "Test University"})
                br = _FakeBrowser(None, note="needs_login:onlinelibrary.wiley.com", via=(OA, (RIGHT, "browser")))
                ok, path, note = dl.download_paper(dict(p), env, browser=br)
                assert not ok and len(br.urls) == 1 and "needs_library_login" not in note, (mode, note, br.urls)
        try:
            _with_downloads(body)
        finally:
            sg.save_profile({"mode": "institutional"}); sg.forget("library")
    check("downloads: when the publisher wants a login and your library (OpenAthens or EZproxy) is signed in, the page is asked for through it (the PDF is checked like any other); a library session that has ended or was never opened says so and offers the library sign-in; no sign-in of its own changes nothing", t_library_route)

    def t_open_copy_skips_wrong():
        import open_copies as oc
        saved = oc.candidates
        oc.candidates = lambda doi, title, http_get: iter([("http://x.test/first.pdf", "preprint (arXiv)"), ("http://x.test/second.pdf", "open copy (Semantic Scholar)")])
        try:
            get = lambda url, headers=None, timeout=30: ({"http://x.test/first.pdf": WRONG, "http://x.test/second.pdf": RIGHT}[url], {})
            data, label = oc.find_open_copy(ID_D, ID_T, get, authors=ID_E["authors"])
            assert data == RIGHT and label == "open copy (Semantic Scholar)"                              # the first candidate was another work: skipped, not accepted
            data, label = oc.find_open_copy(ID_D, ID_T, lambda u, headers=None, timeout=30: (WRONG, {}), authors=ID_E["authors"])
            assert data is None and label.startswith("wrong paper via preprint (arXiv)") and "Two-stage" in label       # nothing usable, and it says why
            oc.candidates = lambda doi, title, http_get: iter([])
            assert oc.find_open_copy(ID_D, ID_T, get)[1] == "no free copy found"
        finally:
            oc.candidates = saved
    check("free copies: a candidate that is another work is skipped and the next one tried; when none is usable the note says a different work came back", t_open_copy_skips_wrong)

    def t_add_doi_gate():
        saved = (importer.crossref_meta, dl.try_unpaywall, dl.http_get, importer._existing)
        importer.crossref_meta = lambda doi: {"title": ID_T, "authors": ["Jane Doe", "Richard Roe"], "container": "Management Science", "issn": ["0025-1909"], "doi": doi,
            "pub_date": "2025-03", "url": "https://doi.org/" + doi, "volume": "1", "issue": "1", "pages": "1-2", "publisher": "INFORMS", "type": "journal-article", "abstract": None, "keywords": []}
        importer._existing = lambda p: None
        dl.try_unpaywall = lambda doi, email: "http://fake.test/oa.pdf"
        served = {}
        def fake_get(url, headers=None, timeout=30):
            if url == "http://fake.test/oa.pdf":
                return served["pdf"], {}
            raise OSError("no network in tests")
        dl.http_get = fake_get
        try:
            served["pdf"] = WRONG
            try:
                importer.add_doi(ID_D); raise AssertionError("a different work's PDF was filed")
            except ValueError as e:
                assert "No free copy" in str(e) and "not this paper" in str(e) and "Two-stage" in str(e), str(e)
            served["pdf"] = RIGHT
            r = importer.add_doi(ID_D)
            assert r["title"] == ID_T and not r["existing"] and open(os.path.join(TMP, r["file"]), "rb").read() == RIGHT, r
        finally:
            importer.crossref_meta, dl.try_unpaywall, dl.http_get, importer._existing = saved
    check("add by DOI: Unpaywall's PDF must be that paper too (a different work is not filed, and the message says so)", t_add_doi_gate)

    def t_wrong_paper_status():
        import contextlib, pipeline as pl_
        saved = (pl_.dl.download_paper, pl_.dl.open_browser, pl_.summarize)
        called = []
        pl_.dl.open_browser = lambda **k: contextlib.nullcontext(None)
        pl_.summarize = lambda *a, **k: called.append(a) or (_ for _ in ()).throw(AssertionError("a summary was paid for a refused PDF"))
        def status_for(note):
            pl_.dl.download_paper = lambda p, env, browser=None, dry=False: (False, None, note)
            run = pl_.Run("picks", {"label": "t"}); run.add_paper({"id": "doi:" + ID_D, "doi": ID_D, "title": ID_T, "verdict": "download", "publisher": "informs", "abbr": "MS", "journal": "Management Science"})
            pl_.download_and_summarize(run, ["doi:" + ID_D], lambda *a, **k: None, threading.Event())
            return run.papers["doi:" + ID_D]
        try:
            q = status_for("wrong paper via browser: this PDF is titled “Two-stage differences in differences”: its title is not on the first pages (0% found)")
            assert q["dl_status"] == "wrong_paper" and q["verdict"] == "download" and "Two-stage" in q["dl_note"] and q.get("manual_url") and not called, q
            # the PDF's own words are not a status: a headline that happens to contain a marker must not change it, a real sign-in need must stay the reason
            assert status_for("wrong paper via browser: this PDF is titled “The governance challenge: a study of platforms”: its title is not on the first pages (3% found)")["dl_status"] == "wrong_paper"
            assert status_for("needs_login:www.pnas.org | wrong paper via browser (reader): this PDF is titled “x”: its title is not on the first pages (0% found)")["dl_status"] == "needs_login"
            assert status_for("browser: no PDF at https://x | no free copy found")["dl_status"] == "failed"
        finally:
            pl_.dl.download_paper, pl_.dl.open_browser, pl_.summarize = saved
    def t_pace():
        """A publisher's papers are spaced out (the pace in Settings), the waits are used to write summaries, a site that asked for a human check gets longer gaps, Stop stops."""
        import contextlib, pipeline as pl_
        saved = (pl_.dl.download_paper, pl_.dl.open_browser, pl_.summarize)
        t, log, hooks = [0.0], [], {}
        clock, sleep = (lambda: t[0]), (lambda d: t.__setitem__(0, t[0] + d))
        class _Br:
            def __init__(self, on_challenge=None, **k): hooks["ch"] = on_challenge
            def __enter__(self): return None
            def __exit__(self, *a): return False
        def fake_dl(p, env, browser=None, dry=False):
            log.append(("dl", p["id"], t[0]))
            if p["id"] == "E2" and hooks.get("ch"):
                hooks["ch"]("www.sciencedirect.com", "waiting")            # ScienceDirect asks for a human check during E2
            t[0] += 10
            f = os.path.join(TMP, "Papers", "pace_%s.pdf" % p["id"]); os.makedirs(os.path.dirname(f), exist_ok=True); open(f, "wb").write(b"%PDF")
            return True, f, "browser"
        def fake_sum(p, path, usage):
            log.append(("sum", p["id"], t[0])); t[0] += 30
            return "summary"
        pl_.dl.download_paper, pl_.dl.open_browser, pl_.summarize = fake_dl, _Br, fake_sum
        def go(papers, pace="careful", stop=None):
            si.save_profile({"pace": pace}); log.clear(); t[0] = 0.0
            run = pl_.Run("picks", {"label": "pace"})
            for pid, pub in papers:
                run.add_paper({"id": pid, "doi": "10.1/%s" % pid, "title": pid, "verdict": "download", "publisher": pub, "abbr": "X", "journal": "J"})
            pl_.download_and_summarize(run, [pid for pid, _ in papers], lambda *a, **k: None, stop or threading.Event(), sleep=sleep, clock=clock)
            return run
        try:
            run = go([("E1", "elsevier"), ("E2", "elsevier"), ("E3", "elsevier"), ("W1", "wiley")])
            at = {(k, i): x for k, i, x in log}
            assert at[("dl", "E2")] >= 10 + 45 * 0.75 and at[("dl", "E2")] <= 10 + 30 + 45 * 1.35 + 1, log                           # careful: about 45 s after E1 ended
            assert at[("sum", "E1")] < at[("dl", "E2")], log                                                                              # the wait was used to summarise E1
            assert at[("dl", "E3")] - (at[("dl", "E2")] + 10) >= 2 * 45 * 0.75, log                                                       # after the human check: twice the gap
            assert at[("dl", "W1")] - (at[("dl", "E3")] + 10) <= 31, log                                                                  # another site does not wait for ScienceDirect's gap
            assert all(run.papers[i].get("summary") for i in ("E1", "E2", "E3", "W1")) and sum(1 for k, *_ in log if k == "sum") == 4, log   # every paper summarised once
            run = go([("E1", "elsevier"), ("E2", "elsevier")], pace="fast")
            at = {(k, i): x for k, i, x in log}
            assert at[("dl", "E2")] - 10 <= 2 * 1.35 + 0.01 and at[("sum", "E1")] > at[("dl", "E2")], log                                 # fast: no real pause, summaries after
            ev = threading.Event()
            pl_.summarize = lambda p, path, usage: ev.set() or log.append(("sum", p["id"], t[0])) or "s"                                    # Stop pressed while waiting
            run = go([("E1", "elsevier"), ("E2", "elsevier")], stop=ev)
            assert [i for k, i, _ in log if k == "dl"] == ["E1"], log
            assert pl_.pace_gap("aisel", "careful", rnd=lambda a, b: 1.0) == 20 and pl_.pace_gap("elsevier", "normal", True, rnd=lambda a, b: 1.0) == 40
            try:
                si.save_profile({"pace": "turbo"}); raise AssertionError("accepted a pace that does not exist")
            except ValueError:
                pass
        finally:
            pl_.dl.download_paper, pl_.dl.open_browser, pl_.summarize = saved
            si.save_profile({"pace": "careful"})
    check("download pace: a publisher's papers are spaced out (careful by default, about 45 s for sites with human checks), the wait is used to write summaries, a site that asked for a human check gets twice the gap, another site does not wait, Stop stops, 'fast' barely pauses", t_pace)
    def t_check_ask():
        """The app's downloads: a "verify you are human" check opens nothing by itself. That site's papers are set aside, the other sites go on, the user is told
        once; a press opens the check between two papers and the site's papers come back; a site that asks again gets one more nudge (its window then stays);
        papers still set aside at the end need a check, unless nothing else was downloading (then the check opens at once); Stop stops."""
        import queue as queue_mod, pipeline as pl_
        saved = (pl_.dl.download_paper, pl_.dl.open_browser, pl_.summarize, pl_.NOTIFY)
        t, log, notes, events = [0.0], [], [], []
        S = {"again": 0, "fail_open": set(), "press_at": {}, "q": None, "press_t": None, "gated": {"elsevier"}}
        def tick(d):
            t[0] += d
            if S["press_t"] is not None and t[0] >= S["press_t"]:
                S["press_t"] = None; S["q"].put(SD)                    # the user presses "Open the check" at this moment (during a pace wait)
        clock, sleep = (lambda: t[0]), tick
        SD = "www.sciencedirect.com"
        class _Br:
            last = None
            def __init__(self, on_challenge=None, ask=False, **k):
                self.ch, self.ask, self.cleared, self.opened, self.again_left = on_challenge, ask, set(), [], S["again"]
                _Br.last = self
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def open_check(self, host, stay=False):
                self.opened.append((host, stay, [i for k, i, _ in log if k == "dl"]))
                mode = S.get("open_mode")
                if mode == "raise":
                    raise RuntimeError("the window was closed")
                if mode == "stop":                                         # the user presses Stop while the check window is open
                    S["ev"].set(); self.ch(host, "failed"); return False
                if mode == "stop_after_pass":
                    S["ev"].set()
                if len(self.opened) in S["fail_open"]:
                    self.ch(host, "failed"); return False
                self.cleared.add(host); self.ch(host, "passed"); return True
        def fake_dl(p, env, browser=None, dry=False):
            log.append(("dl", p["id"], t[0])); t[0] += 10
            if S.get("stop_in") == p["id"]:
                S["ev"].set()                                              # Stop pressed while this paper downloads
            if S.get("raise_in") == p["id"]:
                raise RuntimeError("something broke")
            if p["id"] in S["press_at"]:
                S["q"].put(S["press_at"].pop(p["id"]))                 # the user presses "Open the check" while this paper downloads
            if p["publisher"] in S["gated"]:
                again = SD in browser.cleared and browser.again_left > 0
                if SD not in browser.cleared or again:
                    if again:
                        browser.again_left -= 1
                    browser.ch(SD, "again" if again else "pending")
                    return False, None, "challenge:%s | not found in library EBSCO" % SD     # (a later route's word wins the final note: the set-aside must not depend on it)
            f = os.path.join(TMP, "Papers", "ask_%s.pdf" % p["id"]); os.makedirs(os.path.dirname(f), exist_ok=True); open(f, "wb").write(b"%PDF")
            return True, f, "browser"
        def fake_sum(p, path, usage):
            log.append(("sum", p["id"], t[0])); tick(1)
            return "summary"
        pl_.dl.download_paper, pl_.dl.open_browser, pl_.summarize, pl_.NOTIFY = fake_dl, _Br, fake_sum, (lambda title, text: notes.append(text))
        def go(papers, press_at=None, again=0, fail_open=(), stop=None, checks=True, press_t=None, gated=("elsevier",), open_mode=None, stop_in=None, raise_in=None, **kw):
            si.save_profile({"pace": "careful"}); log.clear(); notes.clear(); events.clear(); t[0] = 0.0
            S.update(again=again, fail_open=set(fail_open), press_at=dict(press_at or {}), q=queue_mod.Queue() if checks else None, press_t=press_t, gated=set(gated),
                     open_mode=open_mode, stop_in=stop_in, raise_in=raise_in, ev=stop if stop is not None else threading.Event())
            stop = S["ev"]
            run = pl_.Run("picks", {"label": "ask"})
            for pid, pub in papers:
                run.add_paper({"id": pid, "doi": "10.1/ask%s" % pid, "title": pid, "verdict": "download", "publisher": pub, "abbr": "X", "journal": "J"})
            S["run"] = run
            pl_.download_and_summarize(run, [pid for pid, _ in papers], lambda ev, d: events.append((ev, d)), stop or threading.Event(), sleep=sleep, clock=clock, checks=S["q"], **kw)
            return run
        st = lambda run, i: run.papers[i].get("dl_status")
        chs = lambda: [d["state"] for ev, d in events if ev == "challenge"]
        dls = lambda: [i for k, i, _ in log if k == "dl"]
        try:
            # 1) a press while another site downloads: the check opens before that site's next paper, and ScienceDirect's papers come back and download
            run = go([("E1", "elsevier"), ("E2", "elsevier"), ("W1", "wiley"), ("W2", "wiley")], press_at={"W1": SD})
            br = _Br.last
            assert br.ask is True and dls()[:2] == ["E1", "W1"], log                               # ask mode; E2 was not fetched while its site's check waited; Wiley went on
            assert len(br.opened) == 1 and br.opened[0][:2] == (SD, False) and br.opened[0][2] == ["E1", "W1"], br.opened   # opened once, after the press, before W2
            assert dls() == ["E1", "W1", "E1", "E2", "W2"], log
            assert all(st(run, i) == "ok" for i in ("E1", "E2", "W1", "W2")) and sorted(i for k, i, _ in log if k == "sum") == ["E1", "E2", "W1", "W2"], (log, {i: st(run, i) for i in run.papers})
            at = {}
            for k, i, x in log:
                if k == "dl": at[i] = x
            assert at["E2"] - (at["E1"] + 10) >= 2 * 45 * 0.75, log                                 # a site that asked for a check gets twice the gap
            assert chs()[:1] == ["pending"] and chs().count("pending") == 1 and "passed" in chs(), chs()
            pend = [d for ev, d in events if ev == "challenge" and d["state"] == "pending"][0]
            assert pend["host"] == SD and "Open the check" in pend["msg"] and pend.get("site") == "ScienceDirect", pend
            assert len(notes) == 1 and "ScienceDirect" in notes[0], notes                             # one Mac notification for the site, not one per paper
            wait_ev = [d for ev, d in events if ev == "download" and d["id"] == "E2" and d["dl_status"] == "needs_check"]
            assert wait_ev and wait_ev[0]["dl_note"].startswith("waiting for you"), events          # the paper says it waits for the user
            # 1b) a press during a pace wait is served there, before the paper that was waiting
            run = go([("E1", "elsevier"), ("W1", "wiley"), ("W2", "wiley")], press_t=10 + 10 + 20)
            assert len(_Br.last.opened) == 1 and _Br.last.opened[0][2] == ["E1", "W1"] and st(run, "E1") == "ok", (_Br.last.opened, log)
            # 1c) two publishers behind the same check page (a library's proxy, say): one nudge, both set aside
            run = go([("E1", "elsevier"), ("I1", "informs"), ("W1", "wiley")], gated=("elsevier", "informs"))
            assert len(notes) == 1 and chs().count("pending") == 1 and st(run, "E1") == st(run, "I1") == "needs_check", (notes, chs())
            # 2) no press, other sites downloading: nothing opens, nothing waits; the set-aside papers end as needing a check (never left 'downloading')
            run = go([("E1", "elsevier"), ("E2", "elsevier"), ("W1", "wiley")])
            assert _Br.last.opened == [] and dls() == ["E1", "W1"], (log, _Br.last.opened)
            assert st(run, "W1") == "ok" and st(run, "E1") == st(run, "E2") == "needs_check" and "press Download again" in run.papers["E2"]["dl_note"], {i: run.papers[i].get("dl_note") for i in run.papers}
            k_pend = next(n for n, (ev, d) in enumerate(events) if ev == "challenge" and d["state"] == "pending")
            k_w1 = next(n for n, (ev, d) in enumerate(events) if ev == "download" and d["id"] == "W1" and d["dl_status"] == "ok")
            assert k_w1 < k_pend and len(notes) == 1, (k_w1, k_pend, notes)        # told once another paper got its answer (before that, the end of the batch might have opened it by itself)
            # 3) nothing else to download (a retry of papers that need a check): the check opens at once, its window staying for the site's papers
            run = go([("E1", "elsevier"), ("E2", "elsevier")])
            assert len(_Br.last.opened) == 1 and _Br.last.opened[0][:2] == (SD, True) and st(run, "E1") == st(run, "E2") == "ok", (_Br.last.opened, log)
            assert notes == [] and "pending" not in chs(), (notes, chs())             # ...and the user is not first told to press "Open the check" (no banner, no notification)
            # 3b) the same batch where checks must not open by themselves (the weekly check's followed authors): told at once, nothing opens, they need a check
            run = go([("E1", "elsevier"), ("E2", "elsevier")], auto_open=False)
            assert _Br.last.opened == [] and notes == [] and "pending" not in chs() and st(run, "E1") == st(run, "E2") == "needs_check", (_Br.last.opened, notes, chs())   # (nothing left that could wait for a press: nobody is told to press a button)
            run = go([("E1", "elsevier"), ("W1", "wiley")], auto_open=False)
            k_pend = next((n for n, (ev, d) in enumerate(events) if ev == "challenge" and d["state"] == "pending"), None)
            k_w1 = next(n for n, (ev, d) in enumerate(events) if ev == "download" and d["id"] == "W1" and d["dl_status"] == "downloading")
            assert _Br.last.opened == [] and len(notes) == 1 and k_pend is not None and k_pend < k_w1, (notes, k_pend, k_w1)     # ...but told as soon as another site's paper starts
            # 3c) download only: no summary is asked for (neither during a pace wait nor at the end)
            run = go([("W1", "wiley"), ("W2", "wiley"), ("W3", "wiley")], summarize=False)
            assert st(run, "W1") == st(run, "W3") == "ok" and not [x for x in log if x[0] == "sum"], log
            # 4) the site asks again after a pass: one more nudge (nothing opens by itself), the second press keeps its window for the rest; then no third nudge
            run = go([("E1", "elsevier"), ("E2", "elsevier"), ("W1", "wiley"), ("W2", "wiley"), ("W3", "wiley")], press_at={"W1": SD, "W2": SD}, again=1)
            br = _Br.last
            assert [o[1] for o in br.opened] == [False, True] and "W2" in br.opened[1][2], br.opened   # the second opening only after the second press, and it stays
            assert chs().count("pending") == 1 and chs().count("again") == 1 and len(notes) == 2, (chs(), notes)
            assert st(run, "E1") == st(run, "E2") == "ok", {i: st(run, i) for i in run.papers}
            run = go([("E1", "elsevier"), ("E2", "elsevier"), ("W1", "wiley"), ("W2", "wiley"), ("W3", "wiley")], press_at={"W1": SD, "W2": SD}, again=1, fail_open={2})
            assert st(run, "E1") == st(run, "E2") == "needs_check" and len(notes) == 2 and "again" not in chs()[chs().index("failed"):], (chs(), notes)
            run = go([("E1", "elsevier"), ("E2", "elsevier"), ("W1", "wiley"), ("W2", "wiley"), ("W3", "wiley")], press_at={"W1": SD, "W2": SD}, again=2)
            assert len(notes) == 2 and chs().count("again") == 1 and st(run, "E1") == st(run, "E2") == "needs_check", (chs(), notes, log)   # a third ask: no third nudge
            # 5) Stop while a check waits: nothing opens, nothing is left 'downloading'
            ev = threading.Event()
            pl_.summarize = lambda p, path, usage: ev.set() or "s"
            run = go([("E1", "elsevier"), ("W1", "wiley"), ("W2", "wiley")], press_at={"W2": SD}, stop=ev)
            assert _Br.last.opened == [] and all(st(run, i) != "downloading" for i in run.papers), {i: st(run, i) for i in run.papers}
            assert _Br.last.stop is ev                                                                 # Stop also reaches the browser (it ends a wait for the user's tick)
            pl_.summarize = fake_sum
            # 7) a press during the batch's last paper is still served (the page said the window would open)
            run = go([("E1", "elsevier"), ("W1", "wiley")], press_at={"W1": SD})
            assert len(_Br.last.opened) == 1 and st(run, "E1") == "ok", (_Br.last.opened, {i: st(run, i) for i in run.papers})
            # 8) Stop while the check window is open: the next paper (a new site, no pace wait) is not downloaded
            run = go([("E1", "elsevier"), ("I1", "informs"), ("W1", "wiley")], press_at={"I1": SD}, open_mode="stop")
            assert "W1" not in dls() and st(run, "W1") is None and st(run, "E1") == "needs_check", (log, {i: st(run, i) for i in run.papers})
            # 9) Stop just after a passed check: the papers that came back are not left 'waiting for you'
            run = go([("E1", "elsevier"), ("E2", "elsevier"), ("W1", "wiley")], press_at={"W1": SD}, open_mode="stop_after_pass")
            assert all(run.papers[i].get("dl_note") != pl_.WAITING_NOTE for i in run.papers), {i: (st(run, i), run.papers[i].get("dl_note")) for i in run.papers}
            # 10) Stop in a batch of checked papers only: nothing opens at the end, they need a check
            run = go([("E1", "elsevier"), ("E2", "elsevier")], stop_in="E1")
            assert _Br.last.opened == [] and st(run, "E1") == "needs_check" and st(run, "E2") is None, (_Br.last.opened, {i: st(run, i) for i in run.papers})   # (E2: never tried)
            assert notes == [] and "pending" not in chs(), (notes, chs())                              # Stop was pressed: no nudge, no notification
            # 11) a check window that fails (closed, the browser gone): that site's papers need a check, the batch goes on
            run = go([("E1", "elsevier"), ("W1", "wiley"), ("W2", "wiley")], press_at={"W1": SD}, open_mode="raise")
            assert st(run, "E1") == "needs_check" and st(run, "W2") == "ok", {i: st(run, i) for i in run.papers}
            # 12) another publisher meets the site's check while the second nudge waits: it waits too, and the second press still opens
            run = go([("E1", "elsevier"), ("E2", "elsevier"), ("I1", "informs"), ("W1", "wiley"), ("W2", "wiley")], press_at={"W1": SD, "W2": SD}, again=2,
                     gated=("elsevier", "informs"))
            assert [o[1] for o in _Br.last.opened] == [False, True] and all(st(run, i) == "ok" for i in run.papers) and len(notes) == 2, (_Br.last.opened, {i: st(run, i) for i in run.papers}, notes)
            # 14) an error during the batch: the papers set aside do not stay 'waiting for you'
            try:
                go([("E1", "elsevier"), ("W1", "wiley")], raise_in="W1"); raise AssertionError("the error was swallowed")
            except RuntimeError:
                pass
            assert S["run"].papers["E1"]["dl_status"] == "needs_check" and "press Download again" in S["run"].papers["E1"]["dl_note"], S["run"].papers["E1"]
            # 13) the app quit while papers waited: a run opened later says they need a check (not "press Open the check")
            run = pl_.Run("picks", {"label": "quit"})
            run.add_paper({"id": "Q1", "doi": "10.1/q1", "title": "Q1", "verdict": "download", "publisher": "elsevier", "abbr": "X", "journal": "J", "dl_status": "needs_check", "dl_note": pl_.WAITING_NOTE})
            run.add_paper({"id": "Q2", "doi": "10.1/q2", "title": "Q2", "verdict": "download", "publisher": "elsevier", "abbr": "X", "journal": "J", "dl_status": "needs_check",
                           "dl_note": "waiting for you: press “Open the check” at the top of the page"})                 # (the note of version 1.2.6)
            run.save(force=True)
            assert "press Download again" in pl_.Run.load(run.id).papers["Q1"]["dl_note"] and "press Download again" in pl_.Run.load(run.id).papers["Q2"]["dl_note"]
            # 6) without a checks queue (the command line), ask mode is off and nothing is set aside
            go([("W1", "wiley")], checks=False)
            assert _Br.last.ask is False
        finally:
            pl_.dl.download_paper, pl_.dl.open_browser, pl_.summarize, pl_.NOTIFY = saved
            si.save_profile({"pace": "careful"})
    check("a human check in the app's downloads opens nothing by itself: that site's papers wait, the others go on, you are told once; 'Open the check' brings them back; a site that asks again gets one more nudge; at the end they need a check (or open at once when nothing else downloads); Stop stops", t_check_ask)

    def t_check_ask_browser():
        """The browser's ask mode against a local check page in headless Chromium: no window until open_check; then a window on that page, the PDF it gives is
        kept (the site is not asked twice), back to the background; Stop ends the wait; the box is never clicked by the app."""
        import time
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            try:
                pw.chromium.launch().close()
            except Exception as e:  # noqa: BLE001
                if "Executable doesn't exist" in str(e):
                    print("  (skipped: Chromium is not installed here: python -m playwright install chromium)")
                    return
                raise
        PDF = b"%PDF-1.4\n" + b"0" * 50_000 + b"\n%%EOF\n"
        state = {"passed": False, "hits": {}}
        class Gate(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                path = self.path.split("?")[0]
                state["hits"][path] = state["hits"].get(path, 0) + 1
                if path == "/gate" and state["passed"]:
                    self.send_response(200); self.send_header("Content-Type", "application/pdf"); self.send_header("Content-Length", str(len(PDF))); self.end_headers(); self.wfile.write(PDF); return
                if path == "/slow":                                         # an ordinary page that goes on to its PDF after a moment
                    body = b"<html><head><meta http-equiv='refresh' content='2;url=/pdf2'></head><body>Loading the PDF</body></html>"
                    self.send_response(200); self.send_header("Content-Type", "text/html"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
                if path == "/pdf2":
                    self.send_response(200); self.send_header("Content-Type", "application/pdf"); self.send_header("Content-Length", str(len(PDF))); self.end_headers(); self.wfile.write(PDF); return
                if path == "/article":                                      # a web page saved as a PDF (HBR, ACM's full text), behind a check
                    if state["passed"]:
                        body = ("<html><body><article>" + "".join("<p>Paragraph %d on platform pricing, entry and governance in two-sided markets.</p>" % i for i in range(1500)) + "</article></body></html>").encode()
                    else:                                                   # (polls slowly enough for the page to go quiet, and reloads itself once passed)
                        body = (b"<html><head><title>Just a moment...</title></head><body><p>Checking your browser</p><input type=checkbox onclick=\"fetch('/clicked')\">"
                                b"<script>setInterval(() => fetch('/passed').then(r => r.text()).then(t => { if (t === '1') location.reload(); }), 1500)</script></body></html>")
                    self.send_response(200); self.send_header("Content-Type", "text/html"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
                if path == "/passed":
                    body = b"1" if state["passed"] else b"0"
                    self.send_response(200); self.send_header("Content-Type", "text/plain"); self.send_header("Content-Length", "1"); self.end_headers(); self.wfile.write(body); return
                body = (b"<html><head><title>Just a moment...</title></head><body><p>Checking your browser</p><input type=checkbox onclick=\"fetch('/clicked')\">"   # (like a real check page:
                        b"<script>setInterval(() => fetch('/passed').then(r => r.text()).then(t => { if (t === '1') location.reload(); }), 500)</script></body></html>")  # it reloads itself once passed)
                self.send_response(200); self.send_header("Content-Type", "text/html"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Gate)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % srv.server_address[1]
        host = "127.0.0.1:%d" % srv.server_address[1]
        orig, prof = brw.Browser._launch, (brw.PROFILE, brw.SESSION_COOKIES)
        rec, said = [], []
        brw.Browser._launch = lambda self, visible: (rec.append(visible), orig(self, False))[1]        # (no screen here: always headless, but the asked-for visibility is recorded)
        brw.PROFILE, brw.SESSION_COOKIES = os.path.join(TMP, "ask-profile"), os.path.join(TMP, "ask-session.json")      # a browser profile of its own
        try:
            with brw.Browser(ask=True, interactive=False, on_challenge=lambda h, s: said.append((h, s))) as b:     # AIS_BROWSER_INTERACTIVE=0: checks are skipped, never offered
                d, n = b.fetch_pdf(base + "/gate", wait_s=4, own=True)
                assert d is None and n.startswith("challenge:") and not b.pending and not said and b.open_check(host) is False and True not in rec, (n, said, rec)
            with brw.Browser(ask=True, on_challenge=lambda h, s: said.append((h, s))) as b:
                d, n = b.fetch_pdf(base + "/gate", wait_s=4)                                            # a check on another route (a free copy elsewhere): its note only
                assert d is None and n.startswith("challenge:") and not b.pending and not said, (n, said)
                d, n = b.fetch_pdf(base + "/gate", wait_s=4, own=True)
                assert d is None and n.startswith("challenge:") and (host, "pending") in said and True not in rec and host in b.pending, (n, said, rec)
                threading.Timer(3, lambda: state.update(passed=True)).start()                         # the user ticks the box (the server stands in for the user)
                assert b.open_check(host) is True, said
                assert rec[-2:] == [True, False] and said[-2:] == [(host, "open"), (host, "passed")], (rec, said)    # a window, then back to the background
                hits = state["hits"]["/gate"]
                try:
                    d, n = b.fetch_pdf(base + "/gate", wait_s=4)
                except Exception as e_:  # noqa: BLE001     (asking the site again can crash this sandbox's Chromium on a PDF address: the hit count below says what happened)
                    d, n = None, "error: %s" % str(e_)[:80]
                assert d and d.startswith(b"%PDF") and state["hits"]["/gate"] == hits, (n, state["hits"])          # the PDF the window got: the site is not asked twice
                state["passed"] = False
                d, n = b.fetch_pdf(base + "/gate", wait_s=4, own=True)                                 # it asks again: "again", still no window
                assert d is None and said[-1] == (host, "again") and rec[-1] is False, (said, rec)
                real_fetch = b._fetch
                def closed(*a, **k):
                    raise brw.PWError("Target page, context or browser has been closed")
                b._fetch = closed                                                                          # the user closes the check window
                assert b.open_check(host) is False and said[-1] == (host, "failed") and rec[-1] is False, (said, rec)   # a check not passed, back to the background
                b._fetch = real_fetch
                b.fetch_pdf(base + "/gate", wait_s=4, own=True)
                b.stop = threading.Event()
                threading.Timer(2, b.stop.set).start()
                t0 = time.time()
                assert b.open_check(host) is False and time.time() - t0 < 20, time.time() - t0            # Stop ends the wait for the user (not 3 minutes)
                assert b.open_check("nowhere:1") is False                                              # a site with no check waiting: nothing opens
            brw.PROFILE = os.path.join(TMP, "ask-profile-2")                                           # (a fresh profile for each further PDF: this sandbox's Chromium mishandles a second one)
            state["passed"] = False
            with brw.Browser(ask=True, on_challenge=lambda h, s: said.append((h, s))) as b:
                b.fetch_pdf(base + "/gate", wait_s=4, own=True)
                u_, d_, _ = b.pending[host]; b.pending[host] = (u_, d_, "doi.org")                     # (the paper had started at a resolver)
                threading.Timer(3, lambda: state.update(passed=True)).start()
                assert b.open_check(host, stay=True) is True and host in b.checked_hosts and rec[-1] is True, (rec, b.checked_hosts)   # the second time the window stays
                assert "doi.org" not in b.checked_hosts, b.checked_hosts                                 # ...for the check's own site, never the address the paper started from
            brw.PROFILE = os.path.join(TMP, "ask-profile-3")
            with brw.Browser(ask=True) as b:
                b.stop = threading.Event(); b.stop.set()
                d, n = b.fetch_pdf(base + "/slow", wait_s=8)                                            # Stop does not cut short an ordinary page load (it would read as a paywall)
                assert d and d.startswith(b"%PDF"), n
            # a web page saved as a PDF (HBR): its check is noted too, and "Open the check" waits for the tick, then saves that page (it has no PDF link)
            brw.PROFILE = os.path.join(TMP, "ask-profile-4"); state["passed"] = False
            with brw.Browser(ask=True, on_challenge=lambda h, s: said.append((h, s))) as b:
                n_rec = len(rec)
                d, n = b.page_to_pdf(base + "/article")
                assert d is None and n.startswith("challenge:") and said[-1] == (host, "pending") and b.pending.get(host, ("",))[0] == base + "/article" and True not in rec[n_rec:], (n, said[-2:], b.pending, rec[n_rec:])
                threading.Timer(3, lambda: state.update(passed=True)).start()
                assert b.open_check(host) is True and said[-1] == (host, "passed") and True in rec[n_rec:] and rec[-1] is False, (said[-3:], rec[n_rec:])     # a window, then the page saved in the background
                hits = state["hits"]["/article"]
                d, n = b.page_to_pdf(base + "/article")
                assert d and d.startswith(b"%PDF") and n == "page saved as PDF" and state["hits"]["/article"] == hits, (n, state["hits"])     # the copy the check got: not asked twice
            # ACM: its PDF address is asked first; a check met there stays the one to open (the full-text page met next does not replace it or ask a second time)
            brw.PROFILE = os.path.join(TMP, "ask-profile-5"); state["passed"] = False
            with brw.Browser(ask=True, on_challenge=lambda h, s: said.append((h, s))) as b:
                b.fetch_pdf(base + "/gate", wait_s=4, own=True); n_said = len(said)
                d, n = b.page_to_pdf(base + "/article", min_chars=12000)
                assert d is None and n.startswith("challenge:") and b.pending[host][0] == base + "/gate" and len(said) == n_said and not b._page_checks, (b.pending, said[n_said:])
            # the second "Open the check" keeps the window for the site: a web page saved as a PDF waits there for the tick too (it is not noted again, nor given up)
            brw.PROFILE = os.path.join(TMP, "ask-profile-7"); state["passed"] = False
            with brw.Browser(ask=True, on_challenge=lambda h, s: said.append((h, s))) as b:
                b.page_to_pdf(base + "/article")
                threading.Timer(3, lambda: state.update(passed=True)).start()
                assert b.open_check(host, stay=True) is True and host in b.checked_hosts
                b.page_to_pdf(base + "/article")                                                      # (the copy the check got)
                state["passed"] = False; n_said = len(said); n_rec = len(rec)
                threading.Timer(3, lambda: state.update(passed=True)).start()                         # the site asks again: the user ticks it in the window
                d, n = b.page_to_pdf(base + "/article")
                assert d and d.startswith(b"%PDF") and host not in b.pending and True in rec[n_rec:] and (host, "pending") not in said[n_said:], (n, said[n_said:], rec[n_rec:])
            # without ask mode (or with checks switched off) nothing is noted, as before
            brw.PROFILE = os.path.join(TMP, "ask-profile-6"); state["passed"] = False
            with brw.Browser(ask=True, interactive=False) as b:
                d, n = b.page_to_pdf(base + "/article")
                assert d is None and n.startswith("challenge:") and not b.pending, b.pending
            assert "/clicked" not in state["hits"], state["hits"]                                      # the app never ticks the box
        finally:
            brw.Browser._launch = orig
            brw.PROFILE, brw.SESSION_COOKIES = prof
            srv.shutdown()
    check("the browser's ask mode: a check page opens no window; 'Open the check' shows it, keeps the PDF it gives and goes back to the background; it never ticks the box; Stop ends the wait", t_check_ask_browser)

    check("a refused PDF keeps its paper in the download queue as 'wrong_paper' with the reason (a needed sign-in stays the reason; the PDF's own headline never decides the status), offers the publisher's page, and no summary is paid for", t_wrong_paper_status)

    def t_identity_audit():
        import csv, hashlib
        root = os.path.join(TMP, "audit_home"); os.makedirs(os.path.join(root, "Papers", "2025-03", "Management Science"))
        saved = (paths.ROOT, paths.PAPERS)
        paths.ROOT, paths.PAPERS = root, os.path.join(root, "Papers")
        try:
            def put(name, data):
                rel = "Papers/2025-03/Management Science/" + name
                open(os.path.join(root, rel), "wb").write(data); return rel
            items = [("ok.pdf", RIGHT, ID_T, ID_D), ("wrong.pdf", WRONG, "A Study of Hotel Reviews and Their Effects on Demand", "10.1287/mnsc.2024.0002"),
                     ("scan.pdf", mkpdf([("", "")] * 2, scan=True), "An old scanned paper", "10.1287/isre.1.1")]
            with open(os.path.join(root, "Papers", "_index.csv"), "w", newline="") as f:
                w = csv.writer(f); w.writerow("date_checked journal abbr title authors doi url matched_topics score file status note".split())
                for name, data, title, doi in items:
                    w.writerow(["2026-10-29", "Management Science", "MS", title, "Jane Doe; Richard Roe", doi, "", "", "", put(name, data), "ok", "browser"])
                w.writerow(["2026-10-29", "Management Science", "MS", "Not downloaded", "", "10.1/none", "", "", "", "", "failed", "x"])
            snap = lambda: sorted((os.path.relpath(os.path.join(d, f), root), hashlib.sha1(open(os.path.join(d, f), "rb").read()).hexdigest(), os.stat(os.path.join(d, f)).st_mtime_ns)
                                  for d, _, fs in os.walk(root) for f in fs)
            before = snap()
            res = idt.audit()
            assert [(r["file"].split("/")[-1], r["verdict"]) for r in res] == [("wrong.pdf", "mismatch"), ("scan.pdf", "unverified"), ("ok.pdf", "match")], res        # mismatches first; rows that were never downloaded are not looked at
            assert "Two-stage differences in differences" in res[0]["reason"] and res[0]["pdf_says"].startswith("Two-stage")
            assert snap() == before                                                                                                                                       # read-only: no file changed, none added
        finally:
            paths.ROOT, paths.PAPERS = saved
            shutil.rmtree(root, ignore_errors=True)
    def t_identity_remove():
        """Check my PDFs: a PDF you said is right is not read again (until it changes); a wrong one is moved aside with what belongs to it, never deleted, and the library forgets it."""
        import csv
        real = os.path.join(TMP, "remove_home_real"); root = os.path.join(TMP, "remove_home"); folder = os.path.join(real, "Papers", "2025-03", "Management Science"); os.makedirs(folder)
        os.symlink(real, root)                                    # the library is reached through a link, as /var is /private/var on a Mac (this made the first version move files to ../../private/…)
        saved = (paths.ROOT, paths.PAPERS, idt.KEPT)
        paths.ROOT, paths.PAPERS, idt.KEPT = root, os.path.join(root, "Papers"), os.path.join(root, "state", "identity_ok.json")
        try:
            rel = lambda n: "Papers/2025-03/Management Science/" + n
            open(os.path.join(root, rel("right.pdf")), "wb").write(RIGHT); open(os.path.join(root, rel("wrong.pdf")), "wb").write(WRONG)
            for ext in (".md", ".notes.json", ".card.json"):
                open(os.path.join(root, rel("wrong") + ext), "w").write("sidecar")
            hdr = "date_checked journal abbr title authors doi url matched_topics score file status note".split()
            with open(os.path.join(root, "Papers", "_index.csv"), "w", newline="") as f:
                w = csv.writer(f); w.writerow(hdr)
                w.writerow(["2026-10-29", "Management Science", "MS", ID_T, "Jane Doe; Richard Roe", ID_D, "", "", "", rel("right.pdf"), "ok", "browser"])
                w.writerow(["2026-10-29", "Management Science", "MS", "A Study of Hotel Reviews and Their Effects on Demand", "Ann Lee; Bob Ray", "10.1287/mnsc.2024.0002", "", "", "", rel("wrong.pdf"), "ok", "browser"])
            res = idt.audit(use_kept=True); assert [(r["file"].split("/")[-1], r["verdict"]) for r in res] == [("wrong.pdf", "mismatch"), ("right.pdf", "match")], res
            idt.keep(rel("wrong.pdf"))                                                                   # "it is the right paper": not read again while the file is the same
            res = idt.audit(use_kept=True); assert {r["file"].split("/")[-1]: r["verdict"] for r in res} == {"wrong.pdf": "kept", "right.pdf": "match"}, res
            assert {r["verdict"] for r in idt.audit()} == {"mismatch", "match"}                         # (the plain audit ignores that list)
            open(os.path.join(root, rel("wrong.pdf")), "ab").write(b"\n% changed")                      # a changed file is looked at again
            assert {r["file"].split("/")[-1]: r["verdict"] for r in idt.audit(use_kept=True)}["wrong.pdf"] == "mismatch"
            for bad in ("../x.pdf", "Papers/2025-03/Management Science/missing.pdf", "Papers/_index.csv", "Papers/2025-03/Management Science/wrong.md", "/etc/passwd"):
                try:
                    idt.remove(bad); raise AssertionError("removed " + bad)
                except ValueError:
                    pass
            assert os.path.exists(os.path.join(root, rel("wrong.pdf")))
            open(os.path.join(root, "outside.pdf"), "wb").write(RIGHT)                                      # an index row that points outside Papers/ (a damaged or edited index) is not followed
            with open(os.path.join(root, "Papers", "_index.csv"), "a", newline="") as f:
                csv.writer(f).writerow(["2026-10-29", "J", "J", "Outside", "", "10.1/out", "", "", "", "outside.pdf", "ok", ""])
            for bad in ("outside.pdf", "Papers/../outside.pdf"):
                try:
                    idt.remove(bad); raise AssertionError("removed " + bad)
                except ValueError:
                    pass
            assert os.path.exists(os.path.join(root, "outside.pdf"))
            gone = idt.remove(rel("wrong.pdf"))
            assert gone["title"].startswith("A Study of Hotel") and gone["doi"] == "10.1287/mnsc.2024.0002" and gone["authors"] == ["Ann Lee", "Bob Ray"] and gone["date"] == "2025-03", gone
            assert not os.path.exists(os.path.join(root, rel("wrong.pdf"))) and not any(os.path.exists(os.path.join(root, rel("wrong") + e)) for e in (".md", ".notes.json", ".card.json"))
            moved = [os.path.join(root, m) for m in gone["files"]]
            assert len(moved) == 4 and all(os.path.exists(m) and "/Papers/_removed/" in m for m in moved) and any(m.endswith("wrong.pdf") for m in moved), gone     # the PDF and its three sidecars, kept
            rows = list(csv.DictReader(open(os.path.join(root, "Papers", "_index.csv"))))
            r = [x for x in rows if x["doi"] == "10.1287/mnsc.2024.0002"][0]
            assert r["status"] == "removed" and r["file"] == "" and "kept in Papers/_removed/" in r["note"] and [x for x in rows if x["doi"] == ID_D][0]["status"] == "ok", rows
            assert sorted(x["file"].split("/")[-1] for x in idt.audit()) == ["outside.pdf", "right.pdf"]        # the library has forgotten it (the planted row is still there, untouched)
        finally:
            paths.ROOT, paths.PAPERS, idt.KEPT = saved
            os.unlink(root); shutil.rmtree(real, ignore_errors=True)

    check("identity audit: lists the PDFs in a library that are not the paper they are filed under (worst first), changes nothing", t_identity_audit)
    check("check my PDFs: a PDF you marked as right is not read again until it changes; a wrong one is moved aside with its notes, summary and card (never deleted) and the library forgets it; anything outside the library is refused", t_identity_remove)

    def t_link_follow():
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            try:
                pw.chromium.launch().close()
            except Exception as e:  # noqa: BLE001
                if "Executable doesn't exist" in str(e):
                    print("  (skipped: Chromium is not installed here: python -m playwright install chromium)")
                    return
                raise
        port = {}
        def link(doi, host=None):
            return "%s/doi/pdfdirect/10.1287/t.%s" % ("http://%s:%d" % (host, port["p"]) if host else "", doi)
        # the abstract page of each case: what is on it decides what the browser may follow (the request is always /doi/pdf/10.1287/t.<case>, which bounces here)
        PAGES = {
            "cross":     lambda c: "<h2>References</h2><ol><li>Gardner (2021) <a href='http://localhost:%d/other.pdf'>working paper</a></li></ol>" % port["p"],      # the reported bug: another site
            "refs":      lambda c: "<div class='article-references'><a href='/files/cited.pdf'>cited</a></div>",
            "reflist":   lambda c: "<ol class='ref-list'><li><a href='/files/cited.pdf'>cited</a></li></ol>",
            "refown":    lambda c: "<ol class='ref-list'><li><a href='%s'>PDF</a></li></ol>" % link(c),                                         # a link in a reference list is not the article's own, even if it names the DOI (needs-login is the safe answer)
            "footer":    lambda c: "<footer><a href='/files/terms.pdf'>terms</a></footer>",
            "otherdoi":  lambda c: "<p>Related: <a href='/doi/pdfdirect/10.1287/other.2020.9999'>another article</a></p>",
            "ad":        lambda c: "<div class='content'><a href='/files/call-for-papers.pdf'>Call for papers</a></div>",                      # a same-site PDF that is not the article: with a DOI known it is not an article-style link
            "prefix":    lambda c: "<p><a href='/doi/pdfdirect/10.1287/t.prefix0'>the article whose DOI starts with ours</a></p>",
            "own":       lambda c: "<p><a href='%s'>Download PDF</a></p>" % link(c),
            "aside":     lambda c: "<aside><a href='%s'>PDF</a></aside>" % link(c),                                                              # the article's own link in a side rail,
            "nav":       lambda c: "<nav><a href='%s'>PDF</a></nav>" % link(c),                                                                  # in a tab strip,
            "sticky":    lambda c: "<div class='has-sticky-footer'><a href='%s'>PDF</a></div>" % link(c),                                         # under a wrapper whose class says footer,
            "prefs":     lambda c: "<div class='user-preferences'><a href='%s'>PDF</a></div>" % link(c),                                          # or 'preferences' (not a reference list)
            "cdn":       lambda c: "<p><a href='%s'>PDF</a></p>" % link(c, "cdn.localhost"),                                                     # on another host of the publisher, naming our DOI
            "meta":      lambda c: "<p>no links</p>",
            "nodoi":     lambda c: "<div class='aside download-button'><a href='/viewcontent.cgi?article=1&context=misq'>Download</a></div>",     # a landing page without DOI in its links (AIS)
            "nodoirel":  lambda c: "<div class='related-articles'><a href='/viewcontent.cgi?article=2&context=misq'>Related paper</a></div>",    # ... but another article's link in a related list,
            "nodoiref":  lambda c: "<ol class='references'><li><a href='/viewcontent.cgi?article=3&context=misq'>cited</a></li></ol>",         # in a reference list,
            "nodoifoot": lambda c: "<div class='site-footer'><a href='/viewcontent.cgi?article=4&context=misq'>Archive</a></div>",              # or in the footer is not this article's
            "nodoicross": lambda c: "<div class='content'><a href='http://localhost:%d/viewcontent.cgi?article=5&context=misq'>Download</a></div>" % port["p"],      # or on another site
            "metaoff":   lambda c: "<p>no links</p>",                                                                                         # the page's own PDF tag points to another site: not followed
        }
        MUST_FOLLOW = {"own", "aside", "nav", "sticky", "prefs", "cdn", "meta", "nodoi"}
        class Pub(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def _send(self, code, body, ctype, extra=None):
                self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body)))
                for k, v in (extra or {}).items(): self.send_header(k, v)
                self.end_headers(); self.wfile.write(body)
            def do_GET(self):
                path = self.path.split("?")[0]
                m = re.match(r"/doi/(pdf|abs|pdfdirect)/10\.1287/t\.(\w+)$", path)
                if path.startswith("/files/") or path == "/other.pdf":
                    return self._send(200, WRONG, "application/pdf")
                if path == "/viewcontent.cgi" or path == "/doi/pdfdirect/10.1287/other.2020.9999":
                    return self._send(200, RIGHT if path == "/viewcontent.cgi" else WRONG, "application/pdf")
                lm = re.match(r"/landing/(nodoi\w*)$", path)
                if lm:
                    return self._send(200, ("<html><body><h1>Article</h1><p>Request access.</p>%s</body></html>" % PAGES[lm.group(1)](lm.group(1))).encode(), "text/html")
                if not m:
                    return self._send(404, b"no", "text/plain")
                kind, case = m.groups()
                if kind == "pdf":                                              # not entitled: bounced to the abstract page
                    return self._send(302, b"", "text/plain", {"Location": "/doi/abs/10.1287/t." + case})
                if kind == "pdfdirect":
                    return self._send(200, RIGHT if case == "own" or case in MUST_FOLLOW else WRONG, "application/pdf")
                head = "<meta name='citation_pdf_url' content='%s'>" % link("meta") if case == "meta" else ""
                if case == "metaoff":
                    head = "<meta name='citation_pdf_url' content='http://localhost:%d/other.pdf'>" % port["p"]
                bodycls = " class='pb-ui has-sticky-footer'" if case == "sticky" else ""
                self._send(200, ("<html><head>%s</head><body%s><h1>Article</h1><p>Request access to the full text. Purchase this article.</p>%s</body></html>"
                                 % (head, bodycls, PAGES[case](case))).encode(), "text/html")
        srv2 = ThreadingHTTPServer(("127.0.0.1", 0), Pub); port["p"] = srv2.server_address[1]
        threading.Thread(target=srv2.serve_forever, daemon=True).start()
        try:
            got = {}
            with dl.open_browser() as br:
                for case in PAGES:
                    if case.startswith("nodoi"):                                           # no DOI anywhere in the request: the page's own article-style link
                        got[case] = br.fetch_pdf("http://127.0.0.1:%d/landing/%s" % (port["p"], case), wait_s=10)
                    else:
                        got[case] = br.fetch_pdf("http://127.0.0.1:%d/doi/pdf/10.1287/t.%s" % (port["p"], case), wait_s=10, doi="10.1287/t.%s" % case)
            for case in PAGES:
                data, note = got[case]
                if case in MUST_FOLLOW:                                                    # this article's own link is followed, wherever the page puts it
                    assert data == RIGHT and note == "browser", (case, note)
                else:                                                                      # a link that is not this article's is never followed
                    assert data is None and note.startswith("needs_login"), (case, note)
        finally:
            srv2.shutdown()
    def t_library_test():
        """'Test my access' against pages on a local server in headless Chromium: where each kind of answer leads, and what it says (and does not say) about it."""
        from playwright.sync_api import sync_playwright
        import contextlib
        with sync_playwright() as pw:
            try:
                pw.chromium.launch().close()
            except Exception as e:  # noqa: BLE001
                if "Executable doesn't exist" in str(e):
                    print("  (skipped: Chromium is not installed here: python -m playwright install chromium)")
                    return
                raise
        PAGES = {"/open": "<h1>ScienceDirect</h1><p>Access provided by Test University</p>", "/plain": "<h1>Welcome</h1><p>Search articles</p>",
                 "/idp/login": "<h1>Sign in</h1><form><input type=text name=u><input type=password name=p></form>", "/pwd": "<h1>Library</h1><form><input type=password name=p></form>",
                 "/hidden": "<h1>Library</h1><div style='display:none'><input type=password></div><p>Access provided by Test University</p>",
                 "/challenge": "<title>Just a moment...</title><p>Checking your browser before accessing</p>",
                 "/ssrn-own": "<h1>A paper</h1><a href='/sol3/Delivery.cfm/SSRN_ID4723385_code99.pdf?abstractid=4723385&mirid=1'>Download This Paper</a>",
                 "/ssrn-other": "<h1>A paper</h1><a href='/sol3/Delivery.cfm/SSRN_ID9999999_code1.pdf?abstractid=9999999&mirid=1'>Download another</a><a href='/sol3/Delivery.cfm/SSRN_ID47233851_code1.pdf?abstractid=47233851'>a longer number</a>",
                 "/ssrn-stray": "<h1>A paper</h1><a href='/sol3/Delivery.cfm/SSRN_ID9999999_code4723385.pdf?abstractid=9999999'>another paper, whose code field equals our number</a><a href='/sol3/Delivery.cfm/SSRN_ID4723385_code7.pdf?abstractid=4723385&mirid=1'>This paper</a>",
                 "/ssrn-bare": "<h1>A paper</h1><a href='/sol3/Delivery.cfm/file_4723385.pdf'>a link with the number but no identifier</a><a href='/sol3/Delivery.cfm/SSRN_ID4723385_code7.pdf?abstractid=1111111'>names two papers</a>"}
        PAGES["/ssrn-real"] = ("<h1>A paper</h1><div class='box-container'><a href='/sol3/Delivery.cfm?abstractid=4723385&mirid=1' class='button-link primary'>Download This Paper</a>"   # SSRN's real link (from a paper page, Nov 2026)
                               "<a href='/sol3/Delivery.cfm?abstractid=4723385&mirid=1&type=2'>Open PDF in Browser</a></div>"
                               "<div class='related-papers'><a href='/sol3/Delivery.cfm?abstractid=9999999&mirid=1'>Download</a></div>")
        PAGES["/ssrn-real-other"] = "<h1>A paper</h1><div><a href='/sol3/Delivery.cfm?abstractid=9999999&mirid=1'>Download This Paper</a></div>"
        PAGES["/sol3/papers.cfm"] = ("<meta name='citation_pdf_url' content='/sol3/Delivery.cfm?abstractid=4723385'>"                        # SSRN's paper page as it is (Nov 2026): a citation_pdf_url
                                     "<h1>A paper</h1><div class='abstract-buttons'><a href='Delivery.cfm?abstractid=4723385&mirid=1' class='button-link primary'>Download This Paper</a>"   # without &mirid=1,
                                     "<a href='Delivery.cfm?abstractid=4723385&mirid=1&type=2'>Open PDF in Browser</a></div>")                                                    # and the buttons
        PAGES["/ssrn-banner"] = "<h1>A paper</h1><div class='top-banner'><a href='/sol3/Delivery.cfm?abstractid=4723385&mirid=1'>Download This Paper</a></div>"
        SSRN_PDF = b"%PDF-1.4\n" + b"0" * 50_000 + b"\n%%EOF\n"
        delivered = []
        PAGES["/oa-off"] = "<title>OpenAthens: 403 Forbidden</title><p>The redirector is not enabled for the specified customer domain. Please contact your OpenAthens administrator.</p>"
        REDIRECTS = {"/hop1": "/hop2", "/hop2": "/open", "/to-login": "/idp/login?x=1",
                     "/through": "/ShibAuth/institutionLogin?entityID=x", "/ShibAuth/institutionLogin": "/plain"}     # a silent pass through an institution-login step, back to the publisher
        class Pub(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                path = self.path.split("?")[0]
                if path == "/sol3/Delivery.cfm":                                   # SSRN sends the PDF as a download: from its button (&mirid=1), clicked on a page of the site;
                    delivered.append((self.path, self.headers.get("Referer") or ""))  # anything else is sent back to the paper page
                    if "mirid=1" not in self.path or not (self.headers.get("Referer") or "").startswith(base3):
                        self.send_response(302); self.send_header("Location", "/sol3/papers.cfm?abstract_id=4723385"); self.end_headers(); return
                    self.send_response(200); self.send_header("Content-Type", "application/pdf"); self.send_header("Content-Disposition", 'attachment; filename="SSRN-id4723385.pdf"')
                    self.send_header("Content-Length", str(len(SSRN_PDF))); self.end_headers(); self.wfile.write(SSRN_PDF); return
                if path in REDIRECTS:
                    self.send_response(302); self.send_header("Location", REDIRECTS[path]); self.end_headers(); return
                body = ("<html><head><meta charset=utf-8></head><body>%s</body></html>" % PAGES[path]).encode() if path in PAGES else None
                if body is None:
                    self.send_response(404); self.end_headers(); return
                self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        srv3 = ThreadingHTTPServer(("127.0.0.1", 0), Pub)
        threading.Thread(target=srv3.serve_forever, daemon=True).start()
        base3 = "http://127.0.0.1:%d" % srv3.server_address[1]
        saved = (brw.library, brw.via_library, brw.si.probe_url)
        def run(path, signs_in, mode="openathens"):
            brw.library = lambda: {"signs_in": signs_in, "home": base3 + "/never-opened", "mode": mode, "name": "Test University", "openathens": "testuni.ac.uk"}
            brw.si.probe_url = lambda: base3 + path                                  # the publisher page the test opens (ScienceDirect in real life), not the library's web site
            brw.via_library = lambda u: u
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                out = brw.library_test()
            assert json.loads(buf.getvalue().strip().splitlines()[-1]) == out                      # what the app reads from the helper is what it returned
            return out
        try:
            r = run("/through", True)                                                                     # through an institution-login step and back, no password: signed in
            assert r["state"] == "ok" and r["host"].startswith("127.0.0.1") and "without asking for a password" in r["message"] and "institutionLogin" not in r["host"], r
            r = run("/hop1", True)                                                                        # no sign-in step on the way, but the page says whose access it is
            assert r["state"] == "ok" and r["saw"].startswith("Access provided by") and "says" in r["message"] and "Signed in:" not in r["message"], r
            r = run("/plain", True)                                                                       # straight to a page that says nothing: no sign-in was involved, so none is claimed
            assert r["state"] == "unknown" and "was not part of the way" in r["message"], r               #   (1.2.0 said "Signed in" here: OpenAthens passing through to an unprotected page)
            r = run("/oa-off", True); assert r["state"] == "setup" and "testuni.ac.uk" in r["message"] and "not your library's OpenAthens domain" in r["message"], r
            r = run("/to-login", True); assert r["state"] == "login" and "Sign in now" in r["message"] and "/idp/login" not in r["host"], r       # sent to the identity provider's login
            r = run("/pwd", True); assert r["state"] == "login", r                                        # a visible password box on an ordinary address
            r = run("/hidden", True); assert r["state"] == "ok", r                                        # a hidden one (a pop-up for later) is not a sign-in request
            r = run("/challenge", True); assert r["state"] == "challenge" and "human check" in r["message"], r
            r = run("/to-login", False, "institutional"); assert r["state"] == "login" and "Other publisher sites" in r["message"] and "Sign in now" not in r["message"], r
            r = run("/open", False, "vpn"); assert r["state"] == "ok" and "says" in r["message"] and "Access provided by Test University" in r["message"] and "Signed in:" not in r["message"], r   # no sign-in of its own: only what the page says
            r = run("/plain", False, "vpn"); assert r["state"] == "unknown" and "does not prove" in r["message"], r                    # ... and when it says nothing, nothing is claimed
            r = run("/gone", False, "vpn"); assert r["state"] == "error" and "did not open" in r["message"], r            # an address that answers with an empty error does not open: said so, with the reason
            # the sign-in window with a session that still works: it says so and closes, instead of waiting half an hour for a password nobody needs to type
            real_B = brw.Browser
            brw.Browser = lambda visible=None, **kw: real_B(visible=False, **kw)                                                           # (no screen here: the same browser, without a window)
            try:
                brw.library = lambda: {"signs_in": True, "home": base3 + "/never-opened", "mode": "openathens", "name": "Test University", "chooser_text": "", "openathens": "testuni.ac.uk"}
                brw.si.probe_url = lambda: base3 + "/through"
                buf = io.StringIO(); t0 = _time.time()
                with contextlib.redirect_stdout(buf):
                    done = brw.library_login()
                assert done is True and "Signed in to Test University." in buf.getvalue() and _time.time() - t0 < 40, (done, buf.getvalue())
                # a wrong OpenAthens domain: said at once (it used to wait half an hour for a password that no page would ask for)
                brw.si.probe_url = lambda: base3 + "/oa-off"
                buf = io.StringIO(); t0 = _time.time()
                with contextlib.redirect_stdout(buf):
                    done = brw.library_login()
                assert done is False and "not your library's OpenAthens domain" in buf.getvalue() and _time.time() - t0 < 40, (done, buf.getvalue())
            finally:
                brw.Browser = real_B
            # a download attempt that ends on an identity provider's sign-in page names that host (download.py tells an ended library session from a publisher's paywall by it)
            with brw.Browser(interactive=False) as b_:
                d_, n_ = b_.fetch_pdf(base3 + "/to-login", wait_s=8)
                assert d_ is None and n_.startswith("needs_login:127.0.0.1"), n_
                d_, n_ = b_.fetch_pdf(base3 + "/pwd", wait_s=8)
                assert d_ is None and n_.startswith("needs_login:"), n_
            # SSRN's own download link carries the paper's number and no DOI: it is followed when it is this paper's, never another paper's (or a longer number that starts the same)
            with brw.Browser(interactive=False) as b_:
                pg_ = b_.ctx.new_page()
                pg_.goto(base3 + "/ssrn-own"); own_ = brw.Browser._pdf_link(pg_, base3 + "/ssrn-own", "10.2139/ssrn.4723385")
                assert own_ and "Delivery.cfm" in own_ and "ID4723385" in own_, own_
                pg_.goto(base3 + "/ssrn-other"); assert brw.Browser._pdf_link(pg_, base3 + "/ssrn-other", "10.2139/ssrn.4723385") is None
                # a stray number elsewhere in a link (a "code" field) is not the paper's identifier: skipped, and the real link after it is still found
                pg_.goto(base3 + "/ssrn-stray"); st_ = brw.Browser._pdf_link(pg_, base3 + "/ssrn-stray", "10.2139/ssrn.4723385")
                assert st_ and "SSRN_ID4723385_code7" in st_, st_
                # the number alone is not an identifier, and a link naming two different papers is refused
                pg_.goto(base3 + "/ssrn-bare"); assert brw.Browser._pdf_link(pg_, base3 + "/ssrn-bare", "10.2139/ssrn.4723385") is None
                pg_.goto(base3 + "/ssrn-own"); assert brw.Browser._pdf_link(pg_, base3 + "/ssrn-own", "10.1287/mnsc.2024.0001") is None         # another paper's DOI: the rule does not apply
                # SSRN's real button: Delivery.cfm?abstractid=<n>&mirid=1, no ".pdf" in it (1.2.5 and before never looked at it: every SSRN download failed)
                pg_.goto(base3 + "/ssrn-real"); real_ = brw.Browser._pdf_link(pg_, base3 + "/ssrn-real", "10.2139/ssrn.4723385")
                assert real_ and "Delivery.cfm?abstractid=4723385" in real_, real_
                pg_.goto(base3 + "/ssrn-real-other"); assert brw.Browser._pdf_link(pg_, base3 + "/ssrn-real-other", "10.2139/ssrn.4723385") is None   # another paper's button: never
                pg_.goto(base3 + "/ssrn-banner"); bn_ = brw.Browser._pdf_link(pg_, base3 + "/ssrn-banner", "10.2139/ssrn.4723385")
                assert bn_ and "abstractid=4723385" in bn_, bn_                                                                                   # this paper's number decides, whatever box holds the button
            prof_ = brw.PROFILE
            brw.PROFILE = os.path.join(TMP, "ssrn-profile")                                                                                       # (a profile of its own: this sandbox's Chromium mishandles a second PDF in one profile)
            try:
                with brw.Browser(interactive=False) as b_:
                    d_, n_ = b_.fetch_pdf(base3 + "/ssrn-real", wait_s=10, doi="10.2139/ssrn.4723385")
                assert d_ == SSRN_PDF and n_ == "browser" and delivered and "abstractid=4723385" in delivered[0][0], (n_, delivered)               # the paper's page, its button, the PDF
                brw.PROFILE = os.path.join(TMP, "ssrn-profile-2"); delivered.clear()
                with brw.Browser(interactive=False) as b_:
                    pg_ = b_.ctx.new_page(); pg_.goto(base3 + "/sol3/papers.cfm?abstract_id=4723385")
                    btn_ = brw.Browser._pdf_link(pg_, base3 + "/sol3/papers.cfm?abstract_id=4723385", "10.2139/ssrn.4723385"); pg_.close()
                    d_, n_ = b_.fetch_pdf(base3 + "/sol3/papers.cfm?abstract_id=4723385", wait_s=10, doi="10.2139/ssrn.4723385")
                assert btn_ and btn_.endswith("/sol3/Delivery.cfm?abstractid=4723385&mirid=1"), btn_                                                # the button, not the citation_pdf_url
                assert d_ == SSRN_PDF and delivered and "mirid=1" in delivered[0][0] and "/sol3/papers.cfm" in delivered[0][1], (n_, delivered)   # followed from the paper page: the PDF
            finally:
                brw.PROFILE = prof_
            brw.library = lambda: {"signs_in": True, "home": "http://127.0.0.1:1/x", "mode": "openathens", "name": "T"}
            brw.si.probe_url = lambda: "http://127.0.0.1:1/x"                                                                          # nobody listens
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                r = brw.library_test()
            assert r["state"] == "error" and "did not open" in r["message"], r
        finally:
            brw.library, brw.via_library, brw.si.probe_url = saved
            srv3.shutdown()

    def t_terms():
        """Search by terms: what a term matches, and what it does not."""
        import terms as T
        assert T.parse("difference-in-differences + DiD, “staggered” ; did ; x ; DID") == ["difference-in-differences", "DiD", "staggered", "did", "DID"], T.parse("difference-in-differences + DiD, “staggered” ; did ; x ; DID")
        assert T.parse(["Event study", "event STUDY"]) == ["Event study"] and len(T.parse(",".join("t%d" % i for i in range(30)))) == T.MAX_TERMS
        assert T.is_acronym("DiD") and T.is_acronym("DID") and T.is_acronym("RDD") and not T.is_acronym("did") and not T.is_acronym("Did") and not T.is_acronym("LLMsXY")
        m = T.matcher(["difference-in-differences", "DiD"])
        yes = ["We use a difference in differences design", "A Difference\u2010in\u2010Differences approach", "differences-in-differences estimates", "a difference-in-difference estimator",
               "Using DiD, we", "Using DID, we", "an event study (DiD-style) design", "differenceindifferences"]
        no = ["We did not find effects", "Did platforms matter? We ask.", "a candidate test", "the difference in prices", "DIDO lines"]
        for t in yes:
            assert m({"title": "", "abstract": t}), t
        for t in no:
            assert not m({"title": "", "abstract": t}), t
        h = m({"title": "DiD evidence", "abstract": "<jats:p>A difference-in-differences design</jats:p>", "keywords": ["x"]})
        assert [(x["term"], x["field"], x["text"]) for x in h] == [("difference-in-differences", "abstract", "difference-in-differences"), ("DiD", "title", "DiD")], h
        assert T.describe(h) == "Matched: difference-in-differences (abstract), DiD (title)"
        assert T.matcher(["did"])({"abstract": "we did not"}) and T.matcher(["platform"])({"keywords": ["Digital platforms"]})       # lower case: any case; keywords count; plurals
        assert not T.matcher(["platform"])({"abstract": "platformization"})                                                            # whole words only
        # found by the review: "p < 0.01 … > 5%" is text, not a tag (it used to delete what lay between); -es plurals both ways; acronyms in capital-letter titles
        assert m({"abstract": "(p < 0.01). Using a difference-in-differences design, we find gains > 5%."}) and m({"abstract": "(p<0.01) from a DiD with n>500"})
        for a_, b_ in (("focus", "focuses"), ("focuses", "focus"), ("approaches", "approach"), ("status", "statuses"), ("matches", "match")):
            assert T.matcher([a_])({"abstract": b_}), (a_, b_)
        assert not m({"title": "HOW DID FIRMS RESPOND TO TARIFFS AND PRICES"}) and m({"abstract": "two DiDs agree"})
        assert T.parse("difference-in-differences + DiD, C++ ; A+B testing ; (DiD) ; C#") == ["difference-in-differences", "DiD", "C++", "A+B testing", "C#"]
        assert T.matcher(["C++"])({"abstract": "written in C++."}) and not T.matcher(["C++"])({"abstract": "written in C."})
    check("search by terms: any of the terms in the title, abstract or keywords; case, hyphens or spaces, and singular/plural ignored; whole words; an acronym typed with capitals (DiD) does not match the ordinary word (did)", t_terms)

    def t_openalex_budget():
        """OpenAlex without a key shares a small daily budget per network: when it is used up the app does not wait (it used to sleep 30 s three times per call), free
        lookups by DOI still work, many DOIs are asked in one call, and a key goes in the Authorization header, never in the address."""
        import fetch_new as fx_, pipeline as pl_, time as _t
        seen = []
        class OA(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                seen.append((self.path, self.headers.get("Authorization")))
                path = urllib.parse.unquote(self.path)
                if path.startswith("/works?") and "spent" in STATE:
                    body = b'{"error":"Rate limit exceeded","message":"Insufficient budget"}'
                    self.send_response(429); self.send_header("Retry-After", "10200"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
                if path.startswith("/works?filter=doi:"):
                    ds = path.split("filter=doi:", 1)[1].split("&")[0].split("|")
                    res = [{"doi": "https://doi.org/" + d, "abstract_inverted_index": {"batch": [0], d: [1]}} for d in ds if not d.endswith("9")]
                    body = json.dumps({"results": res}).encode()
                elif path.startswith("/works/doi:"):
                    d = path[len("/works/doi:"):]
                    body = json.dumps({"doi": "https://doi.org/" + d, "abstract_inverted_index": {"single": [0], d: [1]}}).encode()
                else:
                    self.send_response(404); self.end_headers(); return
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        STATE = {}
        srv_ = ThreadingHTTPServer(("127.0.0.1", 0), OA); threading.Thread(target=srv_.serve_forever, daemon=True).start()
        old_url, old_key = os.environ.get("JW_OPENALEX_URL"), os.environ.get("OPENALEX_API_KEY")
        os.environ["JW_OPENALEX_URL"] = "http://127.0.0.1:%d/" % srv_.server_address[1]
        os.environ.pop("OPENALEX_API_KEY", None)
        saved_key = fx_.openalex_key
        fx_.openalex_key = lambda: os.environ.get("OPENALEX_API_KEY")
        try:
            fx_.OPENALEX_SPENT.update(until=0.0, keyed=None)
            items = [{"doi": "10.3386/w%d" % n, "title": "t"} for n in range(100, 160)]
            pl_.fill_abstracts(items, lambda *a, **k: None, threading.Event(), "NBER")
            calls = [p_ for p_, _ in seen if p_.startswith("/works?")]
            assert len(calls) == 2 and all(i.get("abstract") for i in items), (len(calls), [i for i in items if not i.get("abstract")][:2])        # 60 DOIs: two calls of 50 and 10; the six OpenAlex lacks asked one by one
            assert sum(1 for i in items if i["abstract"].startswith("single")) == 6
            # the day's budget is used up: no waiting, the free lookups still work, the next list call is not even made
            STATE["spent"] = True; seen.clear(); t0 = _t.time()
            assert fx_.openalex_abstracts_dois(["10.1/a", "10.1/b"]) == {} and _t.time() - t0 < 3 and fx_.openalex_spent()
            assert fx_.openalex_abstract_doi("10.1/a").startswith("single")
            n0 = len(seen); assert fx_.openalex_abstracts_dois(["10.1/c"]) == {} and len(seen) == n0                                   # not asked again until it resets
            items = [{"doi": "10.3386/w%d" % n, "title": "t"} for n in range(200, 210)]
            logs = []; pl_.fill_abstracts(items, lambda e, d=None: logs.append((d or {}).get("msg", "")), threading.Event(), "NBER")
            assert all(i.get("abstract") for i in items) and any("allowance" in l and "Paper sources" in l for l in logs), logs
            # with a key: its own allowance (the keyless one being spent does not stop it), sent in the header
            os.environ["OPENALEX_API_KEY"] = "k123"; STATE.clear(); seen.clear()
            assert not fx_.openalex_spent() and fx_.openalex_abstracts_dois(["10.1/a"])
            assert seen and all(a == "Bearer k123" and "api_key" not in p_ and "k123" not in p_ for p_, a in seen), seen
        finally:
            fx_.openalex_key = saved_key
            fx_.OPENALEX_SPENT.update(until=0.0, keyed=None)
            for k, v in (("JW_OPENALEX_URL", old_url), ("OPENALEX_API_KEY", old_key)):
                if v is None: os.environ.pop(k, None)
                else: os.environ[k] = v
            srv_.shutdown()
    def t_authors():
        """Author search (authors.py) against a local fake OpenAlex: the name is sent as OpenAlex reads it, the candidates say who they are, every page of works
        is fetched (or the list is marked incomplete), the right kinds are kept and a working paper only when no published version is listed, each paper gets its
        publisher's route without a Crossref call, papers already in the library or deleted are not downloaded, and the author's folder is found again."""
        import authors as au, fetch_new as fx_, folders as fo_, csv as _csv, importer as im_
        seen, STATE = [], {}
        def W(i, title, typ="article", doi=None, src=None, year=2023):
            return {"id": "https://openalex.org/W%d" % i, "doi": ("https://doi.org/" + doi) if doi else None, "display_name": title, "publication_date": "%d-05-01" % year,
                    "publication_year": year, "type": typ, "primary_location": {"source": src, "landing_page_url": "https://example.org/w%d" % i},
                    "best_oa_location": {"pdf_url": "https://repo.example.org/w%d.pdf" % i}, "authorships": [{"author": {"display_name": "Ann Lee"}}]}
        WORKS = {"big": [W(1000 + k, "Bulk paper %d" % k, doi="10.9999/bulk.%d" % k, src={"display_name": "Bulk Journal", "type": "journal"}) for k in range(450)]}
        class OA(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                seen.append(self.path)
                q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                mode = STATE.get("mode")
                if mode == "429":
                    body = b'{"error":"Insufficient budget"}'
                    self.send_response(429); self.send_header("Retry-After", "10200"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
                if mode == "500" or (mode == "page2" and q.get("cursor") == ["c1"]):
                    self.send_response(500); self.end_headers(); return
                if self.path.startswith("/authors?"):
                    res = [{"id": "https://openalex.org/A5000000111", "display_name": "Ann Lee", "orcid": "https://orcid.org/0000-0002-1825-0097", "works_count": 42, "cited_by_count": 900,
                            "last_known_institutions": [{"display_name": "MIT"}], "topics": [{"display_name": t} for t in ("Platforms", "Pricing", "Labor", "Extra")]},
                           {"id": "https://openalex.org/A5000000222", "display_name": "Ann Lee", "works_count": 3, "last_known_institutions": [], "topics": []}]
                    body = json.dumps({"results": res if "orcid" not in self.path else res[:1]}).encode()
                elif self.path.startswith("/works?"):
                    data = WORKS[STATE.get("set", "big")]
                    per, cur = int(q["per-page"][0]), q.get("cursor", ["*"])[0]
                    start = 0 if cur == "*" else int(cur[1:]) * per
                    page = data[start:start + per]
                    nxt = "c%d" % (start // per + 1) if start + per < len(data) else None
                    body = json.dumps({"meta": {"count": len(data), "next_cursor": nxt}, "results": page}).encode()
                else:
                    self.send_response(404); self.end_headers(); return
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        srv_ = ThreadingHTTPServer(("127.0.0.1", 0), OA); threading.Thread(target=srv_.serve_forever, daemon=True).start()
        old_url = os.environ.get("JW_OPENALEX_URL")
        os.environ["JW_OPENALEX_URL"] = "http://127.0.0.1:%d/" % srv_.server_address[1]
        saved = (im_.crossref_meta, fx_.openalex_key)
        im_.crossref_meta = lambda *a, **k: (_ for _ in ()).throw(AssertionError("no Crossref call per paper"))
        fx_.openalex_key = lambda: None
        try:
            fx_.OPENALEX_SPENT.update(until=0.0, keyed=None)
            # the name as OpenAlex's search reads it, encoded (a space or a ':' in the address used to make every name 'not answered')
            got = au.search("O'Neil: J?")
            q_ = urllib.parse.parse_qs(urllib.parse.urlsplit(seen[-1]).query)
            assert q_["search"] == ["O Neil J"] and " " not in seen[-1] and "orcid" in q_["select"][0] and "last_known_institutions" in q_["select"][0], seen[-1]
            assert got[0] == {"id": "A5000000111", "name": "Ann Lee", "institutions": ["MIT"], "works": 42, "cited": 900, "topics": ["Platforms", "Pricing", "Labor"], "orcid": "0000-0002-1825-0097"}, got[0]
            au.search("Jürgen Müller"); assert urllib.parse.parse_qs(urllib.parse.urlsplit(seen[-1]).query)["search"] == ["Jürgen Müller"], seen[-1]
            assert len(au.search("0000-0002-1825-0097")) == 1 and "filter=orcid:0000-0002-1825-0097" in seen[-1]
            au.search("A5000000111"); assert "filter=openalex:A5000000111" in seen[-1], seen[-1]                 # an id: the list endpoint (an unknown id is "none found", not "no answer")
            STATE["mode"] = "500"
            try:
                au.search("Ann Lee"); raise AssertionError("no error")
            except au.AuthorError as e:
                assert "didn't answer" in str(e), e
            STATE["mode"] = "429"
            try:
                au.search("Ann Lee"); raise AssertionError("no error")
            except au.AuthorError as e:
                assert "daily allowance" in str(e) and "Settings → Paper sources" in str(e), e
            STATE.clear(); fx_.OPENALEX_SPENT.update(until=0.0, keyed=None)
            # every page of works, for both ids of the person, of the kinds asked
            seen.clear(); calls = []
            ws, complete, total = au.works(["A5000000111", "https://openalex.org/A5000000222"], progress=lambda n, t: calls.append((n, t)))
            assert len(ws) == 450 and complete and total == 450 and calls[-1] == (450, 450), (len(ws), complete, total, calls[-1:])
            qs = [urllib.parse.parse_qs(urllib.parse.urlsplit(x).query) for x in seen]
            assert [x["cursor"][0] for x in qs] == ["*", "c1", "c2"] and qs[0]["filter"][0] == "author.id:A5000000111|A5000000222,type:article|preprint|report|review|book-chapter" and qs[0]["per-page"] == ["200"], qs[0]
            STATE["mode"] = "page2"
            ws, complete, _ = au.works(["A5000000111"])
            assert len(ws) == 200 and complete is False, (len(ws), complete)                                   # what came is kept, and the list says it is incomplete
            STATE.clear()
            stop_ = threading.Event(); seen.clear()
            ws, complete, _ = au.works(["A5000000111"], progress=lambda n, t: stop_.set(), stop=stop_)          # Stop pressed after the first page
            assert len(ws) == 200 and complete is False and len(seen) == 1, (len(ws), complete, seen)
            assert not any("mailto" in x for x in seen), seen                                                   # no e-mail address goes to OpenAlex
            # which works are kept
            J = {"display_name": "Management Science", "type": "journal", "issn": ["0025-1909"]}
            ws = [W(1, "Platform Pricing and Entry", doi="10.1287/mnsc.2023.1", src=J),
                  W(2, "Platform pricing and entry", "preprint", doi="10.2139/ssrn.4000001", src={"display_name": "SSRN Electronic Journal", "type": "repository"}),   # its working paper: left out
                  W(3, "A Working Paper Only", "preprint", doi="10.2139/ssrn.4000002", src={"display_name": "SSRN Electronic Journal", "type": "repository"}),        # no published version: kept
                  W(4, "Editorial", doi="10.1111/isj.1", src={"display_name": "Information Systems Journal", "type": "journal"}),
                  W(5, "Editorial", doi="10.1111/isj.2", src={"display_name": "Information Systems Journal", "type": "journal"}),        # another work of the same title: kept
                  W(6, "Deep Nets at Scale", "book-chapter", doi="10.1007/978-3-1", src={"display_name": "Lecture Notes in Computer Science", "type": "book series"}),   # a conference paper
                  W(7, "A Chapter in a Handbook", "book-chapter", doi="10.4324/hb.1", src={"display_name": "Handbook of Platforms", "type": "book"}),                      # left out
                  W(8, "Conference Talk", doi="10.5465/AMPROC.2023.1", src={"display_name": "Academy of Management Proceedings", "type": "conference"}),
                  W(1, "Platform Pricing and Entry", doi="10.1287/mnsc.2023.1", src=J),                                                  # listed under the person's second id too
                  W(9, "No DOI Here", src=None)]
            kept, excl = au.select(ws)
            ids_ = [au._short(w["id"]) for w in kept]
            assert ids_ == ["W1", "W3", "W4", "W5", "W6", "W8", "W9"], ids_
            assert excl == {"book-chapter": 1, "working paper (published version listed)": 1}, excl
            assert [w["_kind"] for w in kept] == ["journal", "working", "journal", "journal", "conference", "conference", "journal"], [w["_kind"] for w in kept]
            assert [au._short(w["id"]) for w in au.select([ws[1], ws[0]])[0]] == ["W1"]                         # the working paper listed first: still left out
            cjk = [W(20, "中国经济增长", doi="10.9999/cjk.1", src=J), W(21, "数字平台研究", "preprint", doi="10.2139/ssrn.4000021", src={"display_name": "SSRN", "type": "repository"})]
            assert [au._short(w["id"]) for w in au.select(cjk)[0]] == ["W20", "W21"]                              # titles in another script are not all "the same title"
            twin = [W(30, "Introduction", src=None), W(31, "Introduction", src=None)]
            assert len(au.to_papers(au.select(twin)[0], "x", "Ann Lee")) == 1                                    # two records without DOI, title or venue apart: one paper, one id
            jl = dl.load_json(os.path.join(dl.BASE, "journals.json"), {"journals": []})["journals"]
            ms = next((j for j in jl if j.get("issn") == "0025-1909"), None)
            ps = au.to_papers(kept, "ann-lee", "Ann Lee")
            by = {p_["openalex"]: p_ for p_ in ps}
            assert all(p_["verdict"] == "download" and p_["folder"] == "ann-lee" and p_["source"] == "author" for p_ in ps)
            assert by["W1"]["publisher"] == "informs" and by["W1"]["id"] == fx_.key_for("10.1287/mnsc.2023.1", by["W1"]["journal"], by["W1"]["title"]), by["W1"]
            if ms:
                assert by["W1"]["abbr"] == ms["abbr"], by["W1"]                                                           # a watched journal is recognised by its ISSN
            assert by["W3"]["publisher"] == "ssrn" and by["W3"]["url"] == "https://doi.org/10.2139/ssrn.4000002"
            assert by["W9"]["doi"] == "" and by["W9"]["url"] == "https://repo.example.org/w9.pdf", by["W9"]                   # no DOI: the free copy OpenAlex knows
            # already in the library / deleted
            pdf_ = os.path.join(dl.PAPERS, "2023-05", "AU", "have.pdf"); os.makedirs(os.path.dirname(pdf_), exist_ok=True); open(pdf_, "wb").write(b"%PDF")
            idx_ = os.path.join(dl.PAPERS, "_index.csv")
            saved_idx = open(idx_, encoding="utf-8").read() if os.path.exists(idx_) else None
            try:
                dl.save_index({"doi": "10.1111/isj.1", "title": "Editorial"}, os.path.relpath(pdf_, dl.ROOT), "ok")
                dl.save_index({"doi": "10.9999/published-version", "title": "A Working Paper Only"}, "", "deleted")       # its published version was deleted: its preprint stays away too
                dl.save_index({"doi": "", "title": "Conference Talk"}, "", "deleted")
                dl.save_index({"doi": "10.2139/ssrn.5555", "title": "Platform Pricing and Entry"}, os.path.relpath(pdf_, dl.ROOT), "ok")   # its preprint is in the library: the published version still comes
                new, have, gone = au.split(ps)
                assert sorted(p_["openalex"] for p_ in new) == ["W1", "W5", "W6", "W9"] and [(p_["openalex"], k) for p_, k in have] == [("W4", "10.1111/isj.1")] \
                    and sorted(p_["openalex"] for p_ in gone) == ["W3", "W8"], ([p_["openalex"] for p_ in new], have, [p_["openalex"] for p_ in gone])
            finally:
                if saved_idx is None:
                    os.remove(idx_)
                else:
                    open(idx_, "w", encoding="utf-8").write(saved_idx)
            # the author's folder: made, found again, never someone else's
            store_ = os.path.join(dl.PAPERS, "_authors.json")
            if os.path.exists(store_):
                os.remove(store_)
            f1, _ = au.folder_for(["A5000000111"], "Ann Lee", "MIT")
            f2, rec2 = au.folder_for(["A5000000111", "A5000000222"], "Ann Lee", "MIT")
            assert f1 == f2 and rec2["ids"] == ["A5000000111", "A5000000222"] and [f["name"] for f in fo_.list_all()].count("Ann Lee") == 1, (f1, f2, rec2)
            f3, _ = au.folder_for(["A5000000999"], "Ann Lee", "Stanford")                                                            # another Ann Lee
            assert fo_.get(f3)["name"] == "Ann Lee · Stanford", fo_.get(f3)
            fo_.create("Bo Chen")                                                                                               # a folder you made yourself
            f4, _ = au.folder_for(["A5000000444"], "Bo Chen", "")
            assert fo_.get(f4)["name"] == "Bo Chen (2)" and f4 != "bo-chen", fo_.get(f4)
            au.file_have(f1, [(None, "10.1/x")])                                                                # filed into the first folder
            fo_.delete(f1)
            f5, _ = au.folder_for(["A5000000111"], "Ann Lee", "MIT")                                                                   # you deleted the folder: a new one
            assert fo_.get(f5) and json.load(open(store_))["authors"][0]["folder"] == f5
            assert au.file_have(f5, [(None, "10.1/x"), (None, "10.1/y")]) == ["10.1/x", "10.1/y"] and fo_.get(f5)["papers"] == ["10.1/x", "10.1/y"]
            fo_.remove(f5, ["10.1/y"])                                                                                          # you took one out
            assert au.file_have(f5, [(None, "10.1/x"), (None, "10.1/y")]) == [] and fo_.get(f5)["papers"] == ["10.1/x"]        # it stays out
            assert au.file_new(f5, "10.1/y") is False and au.file_new(f5, "10.1/z") is True and fo_.get(f5)["papers"] == ["10.1/x", "10.1/z"]   # a download too
            long_ = "Maria Fernanda Rodriguez-Gonzalez de la Vega y Montes Altos"[:50]
            fl1, _ = au.folder_for(["A5000000771"], long_, "Universidad Autonoma de Barcelona")
            fl2, _ = au.folder_for(["A5000000772"], long_, "Universidad Autonoma de Barcelona y Madrid")      # the same name and institution, cut to 60 characters
            fl3, _ = au.folder_for(["A5000000773"], long_, "Universidad Autonoma de Barcelona y Madrid")
            assert len({fl1, fl2, fl3}) == 3 and all(len(fo_.get(x)["name"]) <= 60 for x in (fl1, fl2, fl3)), [fo_.get(x)["name"] for x in (fl1, fl2, fl3)]
            for f in fo_.list_all():
                if f["name"].startswith(("Ann Lee", "Bo Chen", "Maria Fernanda")):
                    fo_.delete(f["id"])
            os.remove(store_)
        finally:
            im_.crossref_meta, fx_.openalex_key = saved
            fx_.OPENALEX_SPENT.update(until=0.0, keyed=None)
            if old_url is None:
                os.environ.pop("JW_OPENALEX_URL", None)
            else:
                os.environ["JW_OPENALEX_URL"] = old_url
            srv_.shutdown()
    check("author search: the name is sent as OpenAlex reads it, the candidates say who they are, every page of works (or 'incomplete'), journal/conference/working papers kept and a working paper only without its published version, each paper's route without a Crossref call, papers in the library or deleted not downloaded, the author's folder found again and never another's", t_authors)

    def t_follow():
        """Following an author (authors.py, pipeline.follow_list / follow_settle) against a local fake OpenAlex: the record is a union of works dealt with; the weekly
        check adds only works not dealt with, marks a feed paper of the same work for download in place (no second copy) and keeps the feed from adding it again;
        works that are not a kind that is downloaded, already in the library or deleted are dealt with at once; only works that came are dealt with after the
        downloads (the others are tried next week); a merged OpenAlex id is followed to the new one; a follow with no record yet is set up silently; a deleted
        folder pauses it; nothing about following is kept on the run."""
        import copy, authors as au, fetch_new as fx_, folders as fo_, pipeline as pl_
        asked = []
        WORKS, MERGED = {}, {}
        def W(i, title, doi=None, src=None, typ="article"):
            return {"id": "https://openalex.org/W%d" % i, "doi": ("https://doi.org/" + doi) if doi else None, "display_name": title, "publication_date": "2026-10-01",
                    "publication_year": 2026, "type": typ, "primary_location": {"source": src, "landing_page_url": "https://example.org/w%d" % i},
                    "best_oa_location": {}, "authorships": [{"author": {"display_name": "Ann Lee"}}]}
        class OA(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                asked.append(self.path)
                q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                m = re.match(r"/authors/(A\d+)\?select=id", self.path)
                if m:
                    to = MERGED.get(m.group(1))
                    if to in (404, 500):
                        self.send_response(to); self.end_headers(); return
                    body = json.dumps({"id": "https://openalex.org/" + (to or m.group(1))}).encode()
                elif self.path.startswith("/works?"):
                    ids = re.match(r"author\.id:([A0-9|]+)", q["filter"][0]).group(1).split("|")
                    data = [w for i in ids for w in WORKS.get(i, [])]
                    body = json.dumps({"meta": {"count": len(data), "next_cursor": None}, "results": data}).encode()
                else:
                    self.send_response(404); self.end_headers(); return
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        srv_ = ThreadingHTTPServer(("127.0.0.1", 0), OA); threading.Thread(target=srv_.serve_forever, daemon=True).start()
        old_url = os.environ.get("JW_OPENALEX_URL"); os.environ["JW_OPENALEX_URL"] = "http://127.0.0.1:%d/" % srv_.server_address[1]
        saved_key = fx_.openalex_key; fx_.openalex_key = lambda: None
        store_ = os.path.join(dl.PAPERS, "_authors.json"); seen_p = os.path.join(pl_.STATE, "seen.json"); idx_ = os.path.join(dl.PAPERS, "_index.csv")
        saved_files = {p_: (open(p_, encoding="utf-8").read() if os.path.exists(p_) else None) for p_ in (store_, seen_p, idx_)}
        made = []
        events = []
        emit = lambda ev, d=None: events.append((ev, d))     # noqa: E731
        try:
            fx_.OPENALEX_SPENT.update(until=0.0, keyed=None)
            if os.path.exists(store_):
                os.remove(store_)
            j = next(x for x in pl_.journals() if x.get("issn"))
            J = {"display_name": j["name"], "type": "journal", "issn": [j["issn"]]}
            O = {"display_name": "Other Journal", "type": "journal", "issn": ["9999-0001"]}
            fid, _ = au.folder_for(["A5000000111"], "Ann Lee", "MIT"); made.append(fid)
            # --- the record: follow on, a union of works dealt with, read fresh every time
            assert au.set_follow(fid, True)["follow"] is True and [r["folder"] for r in au.followed()] == [fid]
            au.follow_mark(fid, ["W1", "https://openalex.org/W2"]); au.follow_mark(fid, ["W3"])
            assert au.followed()[0]["follow_known"] == ["W1", "W2", "W3"], au.followed()[0]
            d_ = json.load(open(store_)); d_["authors"][0]["follow_known"] = ["W0"]; json.dump(d_, open(store_, "w"))      # (changed on another computer)
            assert au.followed()[0]["follow_known"] == ["W0"]
            # --- the weekly check: a feed paper of a followed work is the same paper; new ones are added; the rest are dealt with
            pdf_ = os.path.join(dl.PAPERS, "2026-10", "FW", "have.pdf"); os.makedirs(os.path.dirname(pdf_), exist_ok=True); open(pdf_, "wb").write(b"%PDF")
            dl.save_index({"doi": "10.7/have", "title": "Already Have It"}, os.path.relpath(pdf_, dl.ROOT), "ok")
            dl.save_index({"doi": "10.7/gone", "title": "You Deleted It"}, "", "deleted")
            WORKS["A5000000111"] = [W(0, "Old Known Work", "10.7/old", O), W(1, "First Followed Paper", "10.7/a", J), W(2, "Second Followed Paper", "10.7/b", O),
                                    W(3, "Third Followed Paper", None, J), W(5, "A Whole Book", "10.7/book", O, typ="book"),
                                    W(6, "First followed paper", "10.2139/ssrn.777", {"display_name": "SSRN", "type": "repository"}, typ="preprint"),
                                    W(7, "Already Have It", "10.7/have", O), W(8, "You Deleted It", "10.7/gone", O)]
            run = pl_.Run("weekly", {"topics": [], "query": ""})
            run.add_paper({"id": "doi:10.7/a", "doi": "10.7/a", "title": "First Followed Paper", "abbr": j["abbr"], "journal": j["name"], "pub_date": "2026-10-01"})
            run.add_paper({"id": "doi:10.7/c", "doi": "10.7/c", "title": "Third Followed Paper", "abbr": j["abbr"], "journal": j["name"], "pub_date": "2026-10-01"})   # (OpenAlex has no DOI for it)
            params0 = copy.deepcopy(run.params)
            todo = pl_.follow_list(run, emit, threading.Event())
            assert sorted(todo) == ["doi:10.7/a", "doi:10.7/b", "doi:10.7/c"], todo
            assert len(run.papers) == 3 and run.papers["doi:10.7/a"]["verdict"] == "download" and run.papers["doi:10.7/a"]["folder"] == fid and run.papers["doi:10.7/a"]["openalex"] == "W1"
            assert run.papers["doi:10.7/c"]["verdict"] == "download" and run.papers["doi:10.7/c"]["openalex"] == "W3", run.papers["doi:10.7/c"]                # the same work by its title
            assert run.papers["doi:10.7/b"]["reason"].startswith("followed author · ") and run.papers["doi:10.7/b"]["folder"] == fid
            assert any(ev == "verdict" and d["id"] == "doi:10.7/a" for ev, d in events) and any(ev == "paper" and d["id"] == "doi:10.7/b" for ev, d in events)
            known = set(au.followed()[0]["follow_known"])
            assert {"W0", "W5", "W6", "W7", "W8"} <= known and not known & {"W1", "W2", "W3"}, known       # the book, the preprint, the one you have, the one you deleted: dealt with
            assert dl.index_key({"doi": "10.7/have"}) in fo_.get(fid)["papers"], fo_.get(fid)
            seen = json.load(open(seen_p))
            for k_ in ("doi:10.7/a", "doi:10.7/b", fx_.key_for(None, j["name"], "Third Followed Paper"), fx_.key_for(None, "Other Journal", "Second Followed Paper")):
                assert k_ in seen and seen[k_].get("follow"), (k_, list(seen)[:8])                         # the feeds will not add them again
            # --- after the downloads: only what came is dealt with; the rest comes back next week
            run.papers["doi:10.7/a"]["dl_status"] = "ok"; run.papers["doi:10.7/b"]["dl_status"] = "needs_check"; run.papers["doi:10.7/c"]["dl_status"] = "deleted"
            assert pl_.follow_settle(run, todo) == (2, 1)
            known = set(au.followed()[0]["follow_known"])
            assert {"W1", "W3"} <= known and "W2" not in known, known
            WORKS["A5000000111"].append(W(60, "First Followed Paper", "10.2139/ssrn.888", {"display_name": "SSRN", "type": "repository"}, typ="preprint"))   # a new preprint of a paper you have
            run2 = pl_.Run("weekly", {"topics": [], "query": ""})
            assert pl_.follow_list(run2, emit, threading.Event()) == ["doi:10.7/b"], list(run2.papers)
            assert "W60" in au.followed()[0]["follow_known"], "a new working paper whose published version is listed is not downloaded"
            # --- nothing about following is kept on the run
            assert run.params == params0 and not any(k_.startswith("follow") for k_ in run.to_dict()) and not any(k_.startswith("follow") for k_ in vars(run))
            # --- an id that OpenAlex merged into another: followed to the new one
            fid2, _ = au.folder_for(["A5000000333"], "Bo Chen", ""); made.append(fid2); au.set_follow(fid2, True); au.follow_mark(fid2, ["W90"])
            MERGED["A5000000333"] = "A5000000444"; WORKS["A5000000444"] = [W(91, "Bo New One", "10.8/1", O), W(92, "Bo New Two", "10.8/2", O)]
            au.set_follow(fid, False)
            run3 = pl_.Run("weekly", {"topics": [], "query": ""}); n_ask = len(asked)
            t3 = pl_.follow_list(run3, emit, threading.Event())
            rec2 = next(r for r in au.followed() if r["folder"] == fid2)
            assert rec2["ids"] == ["A5000000333", "A5000000444"] and sorted(t3) == ["doi:10.8/1", "doi:10.8/2"], (rec2, t3)
            assert not any("A5000000111" in a_ for a_ in asked[n_ask:]), "an author not followed any more is not asked about"
            # ...and when OpenAlex does not know the old id at all: a note on the Authors tab, nothing marked; when it does not answer: no note
            fid3, _ = au.folder_for(["A5000000555"], "Cy Doe", ""); made.append(fid3); au.set_follow(fid3, True); au.follow_mark(fid3, ["W95"]); au.set_follow(fid2, False)
            MERGED["A5000000555"] = 500; events.clear()
            pl_.follow_list(pl_.Run("weekly", {"topics": [], "query": ""}), emit, threading.Event())
            rec3 = next(r for r in au.followed() if r["folder"] == fid3)
            assert not rec3.get("follow_warn") and rec3["follow_known"] == ["W95"] and any("did not answer" in d["msg"] for ev, d in events if ev == "log"), rec3
            MERGED["A5000000555"] = 404; events.clear()
            pl_.follow_list(pl_.Run("weekly", {"topics": [], "query": ""}), emit, threading.Event())
            rec3 = next(r for r in au.followed() if r["folder"] == fid3)
            assert "no works" in rec3["follow_warn"] and rec3["follow_known"] == ["W95"] and any(d.get("level") == "warn" for ev, d in events if ev == "log"), rec3
            WORKS["A5000000555"] = [W(96, "Cy Back Again", "10.9/1", O)]
            pl_.follow_list(pl_.Run("weekly", {"topics": [], "query": ""}), emit, threading.Event())
            assert not next(r for r in au.followed() if r["folder"] == fid3)["follow_warn"], "a listing that works clears the note"
            # --- followed with no record of what was dealt with (another computer, an older record): set up silently, nothing downloaded
            fid4, _ = au.folder_for(["A5000000666"], "Di Ng", ""); made.append(fid4); au.set_follow(fid4, True); au.set_follow(fid3, False)
            WORKS["A5000000666"] = [W(97, "Di One", "10.10/1", O), W(98, "Di Two", "10.10/2", O)]
            r5 = pl_.Run("weekly", {"topics": [], "query": ""})
            assert pl_.follow_list(r5, emit, threading.Event()) == [] and not r5.papers and next(r for r in au.followed() if r["folder"] == fid4)["follow_known"] == ["W97", "W98"]
            # --- the folder was deleted: following pauses, OpenAlex is not asked
            fo_.delete(fid4); n_ask = len(asked)
            assert pl_.follow_list(pl_.Run("weekly", {"topics": [], "query": ""}), emit, threading.Event()) == [] and len(asked) == n_ask
            rec4 = json.load(open(store_))["authors"][-1]
            assert "folder was deleted" in rec4["follow_warn"] and rec4["follow"] is False, rec4                        # paused for real
            assert [a_["name"] for a_ in au.saved()] == ["Ann Lee", "Bo Chen", "Cy Doe", "Di Ng"] and au.saved()[0]["follow"] is False and au.saved()[0]["known"] >= 7
            assert au.saved()[3]["folder_name"] == "" and "folder was deleted" in au.saved()[3]["follow_warn"], au.saved()[3]   # (listed with its note, so it can be followed again)
            # --- a work of two authors you follow: one paper, downloaded once, filed into both folders
            fid5, _ = au.folder_for(["A5000000777"], "Ed Co", ""); fid6, _ = au.folder_for(["A5000000888"], "Flo Co", ""); made.extend([fid5, fid6])
            for x_ in (fid5, fid6):
                au.set_follow(x_, True); au.follow_mark(x_, ["W1"])
            for x_ in (fid2, fid3):
                au.set_follow(x_, False)
            WORKS["A5000000777"] = [W(70, "A Joint Paper", "10.11/joint", O)]; WORKS["A5000000888"] = [W(70, "A Joint Paper", "10.11/joint", O)]
            r6 = pl_.Run("weekly", {"topics": [], "query": ""})
            t6 = pl_.follow_list(r6, emit, threading.Event())
            assert t6 == ["doi:10.11/joint"] and r6.papers["doi:10.11/joint"]["summary_by_hand"] is True and pl_.slim(r6.papers["doi:10.11/joint"])["summary_by_hand"] is True, t6
            r6.papers["doi:10.11/joint"]["dl_status"] = "ok"
            pl_.follow_settle(r6, t6)
            assert all("W70" in next(r for r in au.followed() if r["folder"] == x_)["follow_known"] for x_ in (fid5, fid6))
            assert r6.papers["doi:10.11/joint"]["folder"] == fid5 and dl.index_key({"doi": "10.11/joint"}) in (fo_.get(fid6)["papers"] or []), [fo_.get(x_)["papers"] for x_ in (fid5, fid6)]   # (the first author's folder gets it when it is downloaded; the second's here)
        finally:
            fx_.openalex_key = saved_key; fx_.OPENALEX_SPENT.update(until=0.0, keyed=None)
            os.environ.pop("JW_OPENALEX_URL", None) if old_url is None else os.environ.__setitem__("JW_OPENALEX_URL", old_url)
            srv_.shutdown()
            for x in made:
                if fo_.get(x):
                    fo_.delete(x)
            for p_, txt in saved_files.items():
                if txt is None:
                    os.path.exists(p_) and os.remove(p_)
                else:
                    open(p_, "w", encoding="utf-8").write(txt)
    check("following an author: a union of works dealt with; the weekly check adds only works not dealt with (a feed paper of the same work is marked for download, not copied; the feeds do not add it again), deals at once with what is not downloaded, in the library or deleted; only works that came are dealt with afterwards; a merged id is followed; set up silently with no record; a deleted folder pauses it; nothing on the run", t_follow)

    def t_author_downloads():
        """An author run: the run's name and file; a downloaded paper, or one already on disk, goes into the author's folder; and papers from publishers the app has
        no route for are spaced out and set aside per site (their DOI prefix), so one site's human check does not hold up another's papers."""
        import queue as queue_mod, pipeline as pl_, folders as fo_
        r_ = pl_.Run("author", {"label": "Ann Lee", "author_ids": ["A5000000111"], "folder": "x"})
        assert r_.title() == "Author · Ann Lee" and r_.slug().startswith("author_annlee_") and pl_.Run("author", {"label": "Ann Lee", "author_ids": ["A5000000999"]}).slug() != r_.slug()
        assert pl_.site_key({"publisher": "wiley", "doi": "10.1002/x"}) == "wiley" and pl_.site_key({"doi": "10.1007/abc"}) == "prefix:10.1007" \
            and pl_.site_key({"url": "https://repo.example.org/a.pdf"}) == "host:repo.example.org" and pl_.site_key({}) == "other"
        saved = (pl_.dl.download_paper, pl_.dl.open_browser, pl_.summarize)
        f = fo_.create("Author Test Folder")
        hooks, log = {}, []
        class _Br:
            def __init__(self, on_challenge=None, ask=False, **k): hooks["ch"] = on_challenge
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def open_check(self, host, stay=False): return False
        def fake_dl(p, env, browser=None, dry=False):
            log.append(p["id"])
            if p["doi"].startswith("10.1007/"):
                hooks["ch"]("link.springer.com", "pending")
                return False, None, "challenge:link.springer.com"
            out = os.path.join(TMP, "Papers", "au_%s.pdf" % p["id"].replace(":", "_").replace("/", "_")); os.makedirs(os.path.dirname(out), exist_ok=True); open(out, "wb").write(b"%PDF")
            return True, out, "browser"
        pl_.dl.download_paper, pl_.dl.open_browser, pl_.summarize = fake_dl, _Br, (lambda p, path, usage: "summary")
        try:
            run = pl_.Run("author", {"label": "Ann Lee", "author_ids": ["A5000000111"], "folder": f["id"]})
            have_ = os.path.join(pl_.ROOT, "Papers", "au_have.pdf"); os.makedirs(os.path.dirname(have_), exist_ok=True); open(have_, "wb").write(b"%PDF")
            papers = [{"id": "doi:10.1007/s1", "doi": "10.1007/s1", "title": "Springer One", "publisher": "", "verdict": "download", "folder": f["id"], "journal": "J", "abbr": "J"},
                      {"id": "doi:10.1007/s2", "doi": "10.1007/s2", "title": "Springer Two", "publisher": "", "verdict": "download", "folder": f["id"], "journal": "J", "abbr": "J"},
                      {"id": "doi:10.1177/g1", "doi": "10.1177/g1", "title": "Sage One", "publisher": "", "verdict": "download", "folder": f["id"], "journal": "J", "abbr": "J"},
                      {"id": "doi:10.9/have", "doi": "10.9/have", "title": "On Disk", "publisher": "", "verdict": "download", "folder": f["id"], "journal": "J", "abbr": "J",
                       "file": os.path.relpath(have_, pl_.ROOT)}]
            for p_ in papers:
                run.add_paper(p_)
            si.save_profile({"pace": "fast"})
            pl_.download_and_summarize(run, [p_["id"] for p_ in papers], lambda *a, **k: None, threading.Event(), sleep=lambda d: None, checks=queue_mod.Queue())
            st_ = {i: run.papers[i].get("dl_status") for i in run.papers}
            assert st_["doi:10.1177/g1"] == "ok" and "doi:10.1177/g1" in log, (st_, log)                      # another publisher's site: not held up by Springer's check
            assert log.count("doi:10.1007/s2") == 0 and st_["doi:10.1007/s2"] == "needs_check", (st_, log)      # the same site's next paper waits for the check
            assert sorted(fo_.get(f["id"])["papers"]) == ["10.1177/g1", "10.9/have"] and "doi:10.9/have" not in log, (fo_.get(f["id"]), log)    # downloaded, and already on disk (not fetched again): into the author's folder
        finally:
            pl_.dl.download_paper, pl_.dl.open_browser, pl_.summarize = saved
            si.save_profile({"pace": "careful"})
            fo_.delete(f["id"])
    check("an author run: its name and file; a paper downloaded, or already on disk, goes into the author's folder; publishers without a route are spaced out and set aside per site, so one site's check does not hold up another's papers", t_author_downloads)

    check("OpenAlex: many DOIs in one call (an NBER year: 26 calls instead of 1,300), the rest one by one; a used-up daily budget answers at once instead of waiting and stops only the paid calls; a key is sent in the header, never in the address", t_openalex_budget)

    def t_term_run():
        """A search by terms keeps only the articles that contain a term, says what matched, keeps the full saved copy for later searches, and is its own saved search."""
        import pipeline as pl_
        abbr = pl_.journals()[0]["abbr"]
        arts = [{"doi": "10.1/a", "title": "Platform entry", "abstract": "A difference-in-differences design.", "pub_date": "2024-02"},
                {"doi": "10.1/b", "title": "Why firms did not grow", "abstract": "We did interviews.", "pub_date": "2024-03"},
                {"doi": "10.1/c", "title": "A DiD study of apps", "abstract": "", "pub_date": "2024-04"}]
        saved = (pl_.fx.crossref_journal_range, pl_.fill_abstracts, pl_.saved_fetch, pl_.save_fetch)
        stored = {}
        pl_.fx.crossref_journal_range = lambda *a, **k: [dict(x) for x in arts]
        pl_.fill_abstracts = lambda *a, **k: None
        pl_.saved_fetch = lambda *a, **k: (None, None)
        pl_.save_fetch = lambda ab, y0, y1, items, words="": stored.setdefault("items", items)
        try:
            params = {"year_from": 2024, "year_to": 2024, "journals": [abbr], "topics": pl_.topic_labels(), "methods": [], "query": "", "terms": ["difference-in-differences", "DiD"]}
            run = pl_.Run("search", params)
            pl_.fetch_search(run, lambda *a, **k: None, threading.Event())
            got = {p_["doi"]: p_ for p_ in run.papers.values()}
            assert set(got) == {"10.1/a", "10.1/c"}, set(got)                                                                          # "did" (the verb) is not DiD
            assert got["10.1/a"]["reason"] == "Matched: difference-in-differences (abstract)" and got["10.1/c"]["term_hits"][0]["field"] == "title"
            assert len(stored["items"]) == 3 and not any("term_hits" in x for x in stored["items"])                                    # the saved copy is every article, unmarked
            arch = dict(params, terms=[])
            assert pl_.search_key(params) != pl_.search_key(arch) and pl_.search_key(arch) == pl_.search_key({k: v for k, v in arch.items() if k != "terms"})   # archive keys are unchanged
            assert pl_.search_key(dict(params, terms=["did", "difference-in-differences"])) != pl_.search_key(params)          # did ≠ DiD
            assert run.title() == "2024 · terms: difference-in-differences, DiD"
            assert pl_.search_key(dict(params, topics=["something else"])) == pl_.search_key(params)                              # editing your interests does not make it another search
        finally:
            pl_.fx.crossref_journal_range, pl_.fill_abstracts, pl_.saved_fetch, pl_.save_fetch = saved
    check("a search by terms keeps only the articles with a term (and says which, where), keeps the whole saved copy for other searches, and is a saved search of its own", t_term_run)

    def t_hbr_archive():
        """HBR by year: its print issues' contents pages (stream-item tags), monthly codes before 2017 and six a year since, an empty page is no issue, each article once
        (its copies merged), nothing from another year, an issue that cannot be read stops the search with its code. An old journals.json gets the year search;
        a search by terms keeps the HBR articles whose title or summary has a term, with the same paper id as the weekly feed gives them."""
        import fetch_new as fx_, pipeline as pl_
        assert fx_.hbr_issue_codes(2016) == list(range(1, 13)) and fx_.hbr_issue_codes(2017) == list(range(1, 7))
        def item(url, title="", authors="", summary="", topic=""):
            return '<stream-item class="stream-item" data-url="%s" data-title="%s" data-authors="%s" data-summary="%s" data-topic="%s"><h3>x</h3></stream-item>' % (url, title, authors, summary, topic)
        page = ("<html><body><ul>" + item("/2024/03/a-staggered-did-on-pricing", "A Staggered DiD on Pricing", "", "Short.")
                + item("/2024/03/plain-strategy-note", "Plain &amp; Simple Strategy", "Ann Lee;Bo Chen;", "We use difference-in-differences to study it.", "Strategy")
                + item("/2024/03/a-staggered-did-on-pricing", "A Staggered DiD on Pricing", "Cy Doe;", "Short.")       # its long copy: the authors come from here
                + item("/2019/05/an-old-classic", "An Old Classic", "Dee Old", "From another year.")                # not this issue's year: left out
                + item("/2024/03/no-title-here", "", "X", "y") + item("/resources/whatever", "Not an article") + "</ul></body></html>")
        its = fx_.hbr_issue_items(page, 2024)
        assert [i["url"] for i in its] == ["https://hbr.org/2024/03/a-staggered-did-on-pricing", "https://hbr.org/2024/03/plain-strategy-note"], its
        assert its[0]["authors"] == ["Cy Doe"] and its[1]["authors"] == ["Ann Lee", "Bo Chen"] and its[1]["title"] == "Plain & Simple Strategy" and its[1]["topic"] == "Strategy" and its[0]["pub_date"] == "2024-03"
        assert fx_.hbr_issue_items("<html><body>The Magazine</body></html>", 2024) == []
        asked, prog = [], []
        def get(url, timeout=40):
            asked.append(url)
            return (page.encode() if url.endswith("BR2402") else b"<html>no issue</html>"), url, {}
        got = fx_.hbr_archive(2024, 2024, progress=lambda a, b: prog.append((a, b)), pause=0, get=get)
        assert asked == [fx_.HBR_TOC % (24, n) for n in range(1, 7)] and prog[-1] == (6, 6) and len(got) == 2, (asked, prog, got)
        assert got[1] == {"doi": None, "title": "Plain & Simple Strategy", "url": "https://hbr.org/2024/03/plain-strategy-note", "abstract": "We use difference-in-differences to study it.",
                          "authors": ["Ann Lee", "Bo Chen"], "keywords": ["Strategy"], "pub_date": "2024-03"}, got[1]
        assert len(fx_.hbr_archive(2015, 2016, pause=0, get=lambda u, timeout=40: (b"", u, {}))) == 0
        tries = []
        def flaky(url, timeout=40):                        # one failure is asked again; two stop the search, naming the issue
            tries.append(url)
            if url.endswith("BR2403"):
                raise OSError("timed out")
            if url.endswith("BR2401") and tries.count(url) == 1:
                raise OSError("reset")
            return b"<html></html>", url, {}
        try:
            fx_.hbr_archive(2024, 2024, pause=0, get=flaky, retry_wait=0); raise AssertionError("a missing issue went unnoticed")
        except RuntimeError as e:
            assert "BR2403" in str(e) and tries.count(fx_.HBR_TOC % (24, 1)) == 2 and tries.count(fx_.HBR_TOC % (24, 3)) == 2, (str(e), tries)
        # a journals.json from before: HBR "weekly checks only" is read with the year search; one that says how it is searched is left as it is
        jpath = os.path.join(pl_.BASE, "journals.json"); keep = open(jpath, encoding="utf-8").read()
        try:
            json.dump({"journals": [{"name": "Harvard Business Review", "abbr": "HBR", "publisher": "hbr", "source": "rss", "feed": "http://feeds.hbr.org/harvardbusiness", "archive": False, "per_year": None},
                                    {"name": "Other HBR", "abbr": "OH", "publisher": "hbr", "archive": False, "archive_source": "none"}]}, open(jpath, "w"))
            js = {j["abbr"]: j for j in pl_.journals()}
            assert js["HBR"]["archive"] is True and js["HBR"]["archive_source"] == "hbr_issues" and js["HBR"]["default_off"] and js["HBR"]["feed"] and js["OH"]["archive"] is False, js
        finally:
            open(jpath, "w", encoding="utf-8").write(keep)
        hbr = next(j for j in pl_.journals() if j["abbr"] == "HBR")
        assert hbr["archive"] and hbr["archive_source"] == "hbr_issues"                                                   # the starting settings have it too
        saved = (pl_.fx.hbr_archive, pl_.saved_fetch, pl_.save_fetch, pl_.fx.crossref_journal_range)
        pl_.fx.hbr_archive = lambda y0, y1, progress=None: [dict(x) for x in got]
        pl_.fx.crossref_journal_range = lambda *a, **k: (_ for _ in ()).throw(AssertionError("HBR is not in Crossref"))
        pl_.saved_fetch = lambda *a, **k: (None, None)
        pl_.save_fetch = lambda *a, **k: None
        try:
            run = pl_.Run("search", {"year_from": 2024, "year_to": 2024, "journals": ["HBR"], "topics": [], "methods": [], "query": "", "terms": ["difference-in-differences", "DiD"]})
            pl_.fetch_search(run, lambda *a, **k: None, threading.Event())
            ps = list(run.papers.values())
            assert len(ps) == 2 and {p_["publisher"] for p_ in ps} == {"hbr"} and {p_["source"] for p_ in ps} == {"hbr-issues"}, ps
            byt = {p_["title"]: p_ for p_ in ps}
            assert byt["A Staggered DiD on Pricing"]["term_hits"][0]["field"] == "title" and byt["Plain & Simple Strategy"]["term_hits"][0]["field"] == "abstract"
            assert byt["Plain & Simple Strategy"]["id"] == fx_.key_for(None, "Harvard Business Review", "Plain & Simple Strategy")     # the weekly feed's id for it too
        finally:
            pl_.fx.hbr_archive, pl_.saved_fetch, pl_.save_fetch, pl_.fx.crossref_journal_range = saved
    check("HBR by year: its print issues' contents pages (monthly codes before 2017, six since; an empty page is no issue), each article once with its authors, nothing from another year, an unreadable issue stops the search with its code; an old journals.json gets it; a terms search keeps the matches with the weekly feed's ids", t_hbr_archive)

    def t_term_screen():
        """A search by terms: every match is a download. "Screen with AI" adds topics, the method, a relevance and a reason; its own borderline or skip is kept as
        model_verdict (also in the verdict cache, so another search with the same question gets the model's word, not "download"), never as the verdict. A verdict you
        set stays yours. A run saved before this rule is read with it applied. A search without terms is unchanged."""
        import pipeline as pl_
        abbr = pl_.journals()[0]["abbr"]
        hit = [{"term": "DiD", "field": "abstract", "text": "DiD"}]
        def fake_batch(system, batch, usage, override=None):
            return {n: {"n": n, "verdict": ["download", "borderline", "skip"][n % 3], "relevance": 10 * n, "topics": [], "reason": "r%d" % n, "method": "Econometrics (DiD)"}
                    for n, _ in enumerate(batch, 1)}
        def arts(with_hits):
            return [dict({"id": "t%d" % k, "title": "Paper %d" % k, "abbr": abbr, "pub_date": "2024-03-01", "abstract": "A DiD design %d." % k, "keywords": None},
                         **({"term_hits": hit} if with_hits else {})) for k in range(6)]
        real = pl_._screen_batch
        pl_._screen_batch = fake_batch
        try:
            if os.path.exists(pl_.VERDICTS): os.remove(pl_.VERDICTS)
            params = {"year_from": 2024, "year_to": 2024, "journals": [abbr], "topics": pl_.topic_labels(), "methods": [], "query": "", "terms": ["DiD"]}
            run = pl_.Run("search", params)
            for a in arts(True): run.add_paper(a)
            events = []
            pl_.screen(run, lambda ev, d=None: events.append((ev, d)), threading.Event())
            ps = [run.papers["t%d" % k] for k in range(6)]
            assert [p_["verdict"] for p_ in ps] == ["download"] * 6, [p_["verdict"] for p_ in ps]
            assert [p_["model_verdict"] for p_ in ps] == ["borderline", "skip", "download"] * 2 and ps[0]["relevance"] == 10 and ps[0]["method"] == "Econometrics (DiD)" and ps[1]["reason"] == "r2"
            sent = {d["id"]: d for ev, d in events if ev == "verdict"}
            assert sent["t1"]["verdict"] == "download" and sent["t1"]["model_verdict"] == "skip" and pl_.slim(ps[1])["model_verdict"] == "skip"
            cache = json.load(open(pl_.VERDICTS))
            assert sorted(e["v"] for q in cache.values() for e in q.values()) == sorted(["borderline", "skip", "download"] * 2), cache   # the model's word is what is kept
            # the same question without terms (same topics, no focus): the cached verdicts are reused as the model gave them
            plain = pl_.Run("search", dict(params, terms=[]))
            for a in arts(False): plain.add_paper(a)
            pl_.screen(plain, lambda *a, **k: None, threading.Event())
            assert [plain.papers["t%d" % k]["verdict"] for k in range(6)] == ["borderline", "skip", "download"] * 2 and not any("model_verdict" in p_ for p_ in plain.papers.values())
            # the same terms search again: the cached verdicts go through the rule too
            again = pl_.Run("search", params)
            for a in arts(True): again.add_paper(a)
            pl_.screen(again, lambda *a, **k: None, threading.Event())
            assert [again.papers["t%d" % k]["verdict"] for k in range(6)] == ["download"] * 6 and again.papers["t1"]["model_verdict"] == "skip"
            # a run saved before the rule: skipped and borderline matches become downloads; yours, errors and unscreened ones stay as they are; loading twice changes nothing more
            old = pl_.Run("search", params)
            olds = arts(True)
            for a, v in zip(olds, ["skip", "borderline", "download", "error", None, "skip"]):
                if v: a["verdict"] = v
            olds[5].update(user_set=True, ai_verdict="download")
            for a in olds: old.add_paper(a)
            old.save(force=True)
            for _ in range(2):
                back = pl_.Run.load(old.id)
                got = [(back.papers["t%d" % k].get("verdict"), back.papers["t%d" % k].get("model_verdict")) for k in range(6)]
                assert got == [("download", "skip"), ("download", "borderline"), ("download", "download"), ("error", None), (None, None), ("skip", None)], got
                back.save(force=True)
            # a search without terms is never touched by the rule
            weekly = pl_.Run("search", dict(params, terms=[]))
            q = dict(arts(True)[0], verdict="skip"); pl_.terms_rule(weekly, q)
            assert q["verdict"] == "skip" and "model_verdict" not in q
        finally:
            pl_._screen_batch = real
            if os.path.exists(pl_.VERDICTS): os.remove(pl_.VERDICTS)
    check("a search by terms: every match is a download; Screen with AI only adds topics, method, relevance and a reason (its own skip is shown, and cached as it was said); your verdicts stay; older saved runs are read with the rule", t_term_screen)

    def t_search_memory():
        """A search is not paid for twice: verdicts are reused for the same question and the same text (and expire), a journal's year range is kept for a while, and the same search is the same saved run."""
        import contextlib, pipeline as pl_
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        topic = pl_.topic_labels()[0]; abbr = pl_.journals()[0]["abbr"]
        asked, fetched, events = [], [], []
        def fake_batch(system, batch, usage, override=None):
            asked.extend(p["title"] for p in batch)
            return {n: {"n": n, "verdict": "download" if "Platform" in p["title"] else "skip", "relevance": 80, "topics": [topic], "reason": "because", "method": None} for n, p in enumerate(batch, 1)}
        real = (pl_._screen_batch, pl_.fx.crossref_journal_range, pl_.fill_abstracts)
        pl_._screen_batch = fake_batch
        emit = lambda ev, d=None: events.append((ev, d))
        stop = threading.Event()
        def papers(abs_="About platforms."):
            return [{"id": "p%d" % k, "title": ttl, "abbr": abbr, "pub_date": "2024-03-01", "abstract": abs_, "keywords": ["x"]} for k, ttl in enumerate(["Platform A", "Platform B", "Other C"])]
        def run_with(query="", ps=None):
            r = pl_.Run("search", {"year_from": 2024, "year_to": 2024, "journals": [abbr], "topics": [topic], "methods": [], "query": query})
            for q in (ps or papers()):
                r.add_paper(dict(q))
            return r
        saved_state = ai.config_data()
        try:
            for f in (pl_.VERDICTS,):
                if os.path.exists(f): os.remove(f)
            ai.save_config({"screening": {"reuse_days": 90}})
            r1 = run_with(); pl_.screen(r1, emit, stop)
            assert asked == ["Platform A", "Platform B", "Other C"] and [r1.papers[i]["verdict"] for i in ("p0", "p1", "p2")] == ["download", "download", "skip"] and r1.reused == 0
            asked.clear(); events.clear()
            r2 = run_with(); pl_.screen(r2, emit, stop)                                        # the same question, the same text: nothing is asked
            assert not asked and r2.reused == 3 and [r2.papers[i]["verdict"] for i in ("p0", "p1", "p2")] == ["download", "download", "skip"], (asked, r2.reused)
            assert r2.papers["p0"]["reason"] == "because" and r2.papers["p0"]["topics"] == [topic] and any(e == "log" and "already had a verdict" in d["msg"] for e, d in events), events[-3:]
            r2.save(force=True); assert pl_.Run.load(r2.id).reused == 3 and [d for d in pl_.list_runs() if d["id"] == r2.id][0]["reused"] == 3
            asked.clear(); r3 = run_with(query="a different focus"); pl_.screen(r3, emit, stop)    # another question has its own verdicts
            assert len(asked) == 3 and r3.reused == 0
            asked.clear(); ps = papers(); ps[0]["abstract"] = "An abstract that arrived later."; r4 = run_with(ps=ps); pl_.screen(r4, emit, stop)      # another text: that one paper is asked again
            assert asked == ["Platform A"] and r4.reused == 2, (asked, r4.reused)
            asked.clear(); ai.save_config({"screening": {"reuse_days": 0}}); r5 = run_with(); pl_.screen(r5, emit, stop)                        # 0 days: always ask
            assert len(asked) == 3 and r5.reused == 0
            ai.save_config({"screening": {"reuse_days": 30}})
            d = json.load(open(pl_.VERDICTS)); old = (_dt.now(_tz.utc) - _td(days=31)).isoformat(timespec="seconds")
            for fp in d:
                for e in d[fp].values(): e["at"] = old
            json.dump(d, open(pl_.VERDICTS, "w"))
            asked.clear(); r6 = run_with(); pl_.screen(r6, emit, stop)                           # older than the number of days: asked again, and remembered afresh
            assert len(asked) == 3 and r6.reused == 0
            asked.clear(); r7 = run_with(); pl_.screen(r7, emit, stop); assert not asked and r7.reused == 3
            pl_._screen_batch = lambda system, batch, usage, override=None: {}                                  # a failure is an error verdict and is never remembered
            asked.clear(); r8 = run_with(query="fails"); pl_.screen(r8, emit, stop)
            assert {r8.papers[i]["verdict"] for i in r8.papers} == {"error"}
            pl_._screen_batch = fake_batch
            asked.clear(); r9 = run_with(query="fails"); pl_.screen(r9, emit, stop); assert len(asked) == 3 and r9.reused == 0
            for bad in ({"reuse_days": -1}, {"reuse_days": "x"}, {"reuse_days": 99999}):
                try:
                    ai.save_config({"screening": bad}); raise AssertionError("accepted %r" % bad)
                except ValueError:
                    pass
            assert ai.config()["screening"] == {"reuse_days": 30}
            # the same search is the same saved run: the order of the choices and the case of the focus phrase do not matter
            a = {"year_from": 2024, "year_to": 2024, "journals": ["B", "A"], "topics": ["t2", "t1"], "methods": [], "query": "  LLMs "}
            b = {"year_from": 2024, "year_to": 2024, "journals": ["A", "B"], "topics": ["t1", "t2"], "methods": [], "query": "llms"}
            assert pl_.search_key(a) == pl_.search_key(b) and pl_.search_key(a) != pl_.search_key(dict(b, year_to=2025)) and pl_.search_key(a) != pl_.search_key(dict(b, journals=["A"]))
            assert pl_.Run("search", a).id.split("_")[-1] != pl_.Run("search", dict(a, journals=["A"])).id.split("_")[-1] and pl_.Run("search", a).id.split("_")[-1] == pl_.Run("search", b).id.split("_")[-1]     # two searches started in the same second do not share a file
            rs_ = pl_.Run("search", a); rs_.add_paper(dict(papers()[0])); rs_.save(force=True)
            assert pl_.find_search(b)["id"] == rs_.id and pl_.find_search(dict(b, query="other")) is None and pl_.find_search(a)["search_key"] == pl_.search_key(a)
            # a journal's year range is kept for a few days (a day when it reaches the current year), asked for again after that
            calls = []
            def fake_cr(issn, y0, y1, progress=None, doi_prefix=None, work_type=None):
                calls.append((y0, y1))
                return [{"title": "Platform %d" % k, "doi": "10.9999/t.%d.%d" % (y0, k), "pub_date": "%d-02-01" % y0, "abstract": "Abstract %d" % k, "keywords": ["k"]} for k in range(3)]
            pl_.fx.crossref_journal_range = fake_cr; pl_.fill_abstracts = lambda *a, **k: None
            def search(y0, y1):
                r = pl_.Run("search", {"year_from": y0, "year_to": y1, "journals": [abbr], "topics": [topic], "methods": [], "query": ""}); events.clear()
                pl_.fetch_search(r, emit, stop); return r
            shutil.rmtree(pl_.FETCHED, ignore_errors=True)
            r = search(2023, 2023); assert calls == [(2023, 2023)] and len(r.order) == 3
            r = search(2023, 2023); assert calls == [(2023, 2023)] and len(r.order) == 3 and any(e == "log" and "saved copy" in d["msg"] for e, d in events) and r.jstate[abbr]["label"] == "Saved copy"
            path = pl_._fetched_path(abbr, 2023, 2023); dd = json.load(open(path)); dd["at"] = (_dt.now(_tz.utc) - _td(days=8)).isoformat(timespec="seconds"); json.dump(dd, open(path, "w"))
            r = search(2023, 2023); assert calls == [(2023, 2023), (2023, 2023)]                   # older than a week: asked again
            this_year = _dt.now(_tz.utc).year
            search(this_year - 1, this_year); n = len(calls)
            path = pl_._fetched_path(abbr, this_year - 1, this_year); dd = json.load(open(path)); dd["at"] = (_dt.now(_tz.utc) - _td(days=2)).isoformat(timespec="seconds"); json.dump(dd, open(path, "w"))
            search(this_year - 1, this_year); assert len(calls) == n + 1                              # a range up to this year is kept for a day only (new articles appear)
            pl_.fx.crossref_journal_range = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
            r = search(2022, 2022); assert not r.order and not os.path.exists(pl_._fetched_path(abbr, 2022, 2022))          # a failure is not saved
            stop2 = threading.Event()
            def cr_stopped(*a, **k):                                                                                        # Stop pressed while the journal was being asked: what came is not complete
                stop2.set(); return fake_cr(None, 2021, 2021)
            pl_.fx.crossref_journal_range = cr_stopped
            r = pl_.Run("search", {"year_from": 2021, "year_to": 2021, "journals": [abbr], "topics": [topic], "methods": [], "query": ""}); pl_.fetch_search(r, emit, stop2)
            assert not os.path.exists(pl_._fetched_path(abbr, 2021, 2021)), "a stopped fetch was saved"
            # an error verdict is never remembered (screen filters them out, and this is the second guard)
            pl_.remember_verdicts("fpx", [{"id": "e1", "verdict": "error", "title": "T"}, {"id": "e2", "verdict": "skip", "title": "T2", "reason": "r"}])
            kept = json.load(open(pl_.VERDICTS))["fpx"]; assert "e1" not in kept and kept["e2"]["v"] == "skip", kept
        finally:
            pl_._screen_batch, pl_.fx.crossref_journal_range, pl_.fill_abstracts = real
            ai.save_config({"screening": saved_state["screening"]})

    def t_screen_ai():
        """Screening goes through the AI layer: the "screen" setting (not Ask) picks the model, the monthly budget stops it (papers left unscreened, not marked as failed), local-only mode applies,
        every call (a billed refusal too) is in the usage ledger and the run's cost, a refusal is not asked again, a busy provider is, several batches at once work, and saved verdicts still count."""
        import pipeline as pl_, types
        topic = pl_.topic_labels()[0]; abbr = pl_.journals()[0]["abbr"]
        saved_cfg = open(ai.CONFIG).read() if os.path.exists(ai.CONFIG) else None
        real_compat, real_sleep = ai._compat, pl_.time.sleep
        calls, events, stop = [], [], threading.Event()
        emit = lambda ev, d=None: events.append((ev, d))
        mode = {"fail": None}
        def fake_compat(name, p, f, messages, system, schema, max_tokens, web=False):
            calls.append((name, f["model"], schema is not None, "Topics" in (system or "") or "Topic" in (system or "")))
            if mode["fail"] == "declined":
                raise ai.Declined("The model declined to answer this.")
            if mode["fail"] == "busy-once":
                mode["fail"] = None
                raise ai.AIError("fake: overloaded")
            items = json.loads(messages[-1]["content"])
            yield "done", ai.Result(json.dumps({"results": [{"n": it["n"], "verdict": "download" if "Platform" in it["title"] else "skip", "relevance": 80, "topics": [topic], "reason": "because", "method": None} for it in items]}),
                                    {"in": 1_000_000, "out": 0}, name, f["model"])         # $1.00 a call at the test price
        def new_run(query="", titles=("Platform A", "Platform B", "Other C")):
            r = pl_.Run("search", {"year_from": 2024, "year_to": 2024, "journals": [abbr], "topics": [topic], "methods": [], "query": query})
            for k, ttl in enumerate(titles):
                r.add_paper({"id": "s%d" % k, "title": ttl, "abbr": abbr, "pub_date": "2024-03-01", "abstract": "About platforms.", "keywords": ["x"]})
            return r
        def config(screen=None, local_only=False, budget=None):
            c = {**CFG, "providers": {**CFG["providers"], "anthropic": {"type": "anthropic", "key_env": "FAKE_KEY", "models": [pl_.MODEL]}, "remote": {"type": "openai_compat", "base_url": "http://example.invalid/v1", "key_env": ""}},
                 "features": {"ask": {"provider": "fake", "model": "m1"}, **({"screen": screen} if screen else {})}, "privacy": {"local_only": local_only}, "budget": {"monthly_usd": budget}}
            write_cfg(c)
        try:
            ai._compat = fake_compat; pl_.time.sleep = lambda s: None
            if os.path.exists(pl_.VERDICTS): os.remove(pl_.VERDICTS)
            # 1. a settings file from before screening had its own setting: screening keeps Claude Sonnet 5.5, it does not follow the Ask model, and saved verdicts stay valid
            config()
            assert ai.feature_model("screen") == ("anthropic", pl_.MODEL) and ai.feature_model("ask") == ("fake", "m1") and pl_.screen_model() == pl_.MODEL
            assert pl_.screen_fingerprint("sys") == pl_.hashlib.sha256((pl_.MODEL + "\nsys").encode()).hexdigest()[:16]            # the same fingerprint as before: nothing saved is lost
            c0 = json.loads(open(ai.CONFIG).read()); del c0["providers"]["anthropic"]; write_cfg(c0)                                          # no Claude provider in the file at all: screening follows Ask rather than pointing at nothing
            assert ai.feature_model("screen") == ("fake", "m1") and ai.config()["features"]["screen"]["provider"] in ai.config()["providers"]
            # 2. the budget: two calls fit, the third is refused; the paper it would have judged stays unscreened (no "error" verdict) and is judged on the next search
            config(screen={"provider": "fake", "model": "m1"})
            assert pl_.screen_model() == "fake/m1"
            base = ai.month_spend(); ai.save_config({"budget": {"monthly_usd": base + 1.5}})
            r = new_run(); calls.clear()
            pl_.screen(r, emit, stop, batch_size=1, workers=1)
            judged = [i for i in r.order if r.papers[i].get("verdict")]
            assert len(calls) == 2 and len(judged) == 2 and not any(r.papers[i].get("verdict") == "error" for i in r.order), (calls, judged)
            assert any(e == "log" and d.get("level") == "warn" and "budget" in d["msg"] and "1 papers not screened" in d["msg"] for e, d in events), events[-3:]
            assert abs(r.usage.cost() - 2.0) < 1e-6, r.usage.d                                                               # the run's cost counts what was paid
            rows = [x for x in ai.usage_log(10) if x["feature"] == "screen"]
            u = ai.usage_summary()
            assert len(rows) == 2 and u["by_feature"]["screen"]["calls"] == 2 and abs(u["by_feature"]["screen"]["cost"] - 2.0) < 1e-6 and u["warn"], u["by_feature"]   # Settings → Usage shows it
            assert calls[0][2] is True                                                                                                     # the answer is asked as JSON matching the schema
            # 3. after the budget is raised, the same search screens only the paper that is left; the other two come from the saved verdicts at no cost
            ai.save_config({"budget": {"monthly_usd": None}}); calls.clear(); events.clear()
            r2 = new_run(); pl_.screen(r2, emit, stop, batch_size=1, workers=1)
            assert len(calls) == 1 and r2.reused == 2 and all(r2.papers[i].get("verdict") for i in r2.order), (calls, r2.reused)
            # 4. local-only mode applies to screening: a remote provider is refused before anything is sent
            config(screen={"provider": "remote", "model": "m"}, local_only=True); calls.clear(); events.clear()
            r3 = new_run(query="local only"); pl_.screen(r3, emit, stop, batch_size=1, workers=1)
            assert not calls and not any(r3.papers[i].get("verdict") for i in r3.order) and any(e == "log" and "Local-only" in d["msg"] for e, d in events), (calls, events[-2:])
            # 5. a refusal is final (asked once), a busy provider is asked again
            config(screen={"provider": "fake", "model": "m1"}); mode["fail"] = "declined"; calls.clear()
            r4 = new_run(query="refused"); pl_.screen(r4, emit, stop, batch_size=3, workers=1)
            assert len(calls) == 1 and {r4.papers[i]["verdict"] for i in r4.order} == {"error"}, calls
            mode["fail"] = "busy-once"; calls.clear()
            r5 = new_run(query="busy"); pl_.screen(r5, emit, stop, batch_size=3, workers=1)
            assert len(calls) == 2 and all(r5.papers[i]["verdict"] in ("download", "skip") for i in r5.order), calls
            # 6. four batches at once (the default) all work, and every one is counted
            config(screen={"provider": "fake", "model": "m1"}); calls.clear()
            r6 = new_run(query="parallel", titles=["Platform %d" % k for k in range(12)]); base_extra = r6.usage.cost()
            pl_.screen(r6, emit, stop, batch_size=1, workers=4)
            assert len(calls) == 12 and all(r6.papers[i].get("verdict") == "download" for i in r6.order) and abs(r6.usage.cost() - base_extra - 12.0) < 1e-6, (len(calls), r6.usage.d)
            # 6b. a refusal is billed: Claude's (stop_reason "refusal" still reports its tokens) and an OpenAI-style one (delta.refusal) both reach the ledger and the run's cost, and neither is asked again
            ai._compat = real_compat
            class _Msg:                                              # what the Anthropic SDK hands back for a refused answer
                stop_reason, content = "refusal", []
                usage = types.SimpleNamespace(input_tokens=1_000_000, output_tokens=0, cache_creation_input_tokens=0, cache_read_input_tokens=0, server_tool_use=None)
            class _Stream:
                text_stream = iter(())
                def __enter__(self): return self
                def __exit__(self, *a): return False
                def get_final_message(self): return _Msg()
            asked_a = []
            ai._clients["a"] = types.SimpleNamespace(beta=types.SimpleNamespace(messages=types.SimpleNamespace(stream=lambda **kw: asked_a.append(kw) or _Stream())))
            try:
                config(screen={"provider": "anthropic", "model": "m1"}); n0 = len(ai.usage_log(100000))
                r7 = new_run(query="claude refuses"); pl_.screen(r7, emit, stop, batch_size=3, workers=1)
                rows = ai.usage_log(100000)[:len(ai.usage_log(100000)) - n0]
                assert len(asked_a) == 1 and {r7.papers[i]["verdict"] for i in r7.order} == {"error"}, len(asked_a)
                assert len(rows) == 1 and rows[0]["feature"] == "screen" and abs(rows[0]["cost"] - 1.0) < 1e-6 and abs(r7.usage.cost() - 1.0) < 1e-6, (rows, r7.usage.d)
            finally:
                ai._clients.pop("a", None)
            config(screen={"provider": "fake", "model": "m1"}); n0 = len(ai.usage_log(100000)); n_seen = len(SEEN)
            Fake.refusal = "I can't help with that."
            try:
                r8 = new_run(query="openai refuses"); pl_.screen(r8, emit, stop, batch_size=3, workers=1)
            finally:
                Fake.refusal = None
            rows = ai.usage_log(100000)[:len(ai.usage_log(100000)) - n0]
            assert len(SEEN) == n_seen + 1 and {r8.papers[i]["verdict"] for i in r8.order} == {"error"}, (len(SEEN) - n_seen)
            assert len(rows) == 1 and rows[0]["feature"] == "screen" and rows[0]["cost"] > 0 and r8.usage.cost() > 0, (rows, r8.usage.d)
            ai._compat = fake_compat
            # 7. once the budget is used up, the batches still waiting are dropped instead of each being refused in turn (each refusal reads the whole ledger)
            config(screen={"provider": "fake", "model": "m1"}); ai.save_config({"budget": {"monthly_usd": max(ai.month_spend(), 0.000001)}})
            reads, real_spend = [], ai.month_spend
            ai.month_spend = lambda: (reads.append(1), real_sleep(0.03), real_spend())[2]          # slow reads: the main thread cancels the waiting batches long before the worker could get through 40 of them
            try:
                r7 = new_run(query="many", titles=["Platform %d" % k for k in range(40)]); calls.clear()
                pl_.screen(r7, emit, stop, batch_size=1, workers=1)
            finally:
                ai.month_spend = real_spend
            assert not calls and len(reads) < 12 and not any(r7.papers[i].get("verdict") for i in r7.order), (len(reads), calls)
            ai.save_config({"budget": {"monthly_usd": None}})
        finally:
            ai._compat, pl_.time.sleep = real_compat, real_sleep
            if saved_cfg is None:
                os.path.exists(ai.CONFIG) and os.remove(ai.CONFIG)
            else:
                open(ai.CONFIG, "w").write(saved_cfg)

    def t_ssrn():
        """SSRN is searched by words through Crossref (DOI prefix 10.2139): the request, the answer, the pseudo-journal, the saved copy, the weekly check leaving it alone, the paper's own page."""
        import pipeline as pl_
        import fetch_new as fx_
        asked = []
        answer = {"status": "ok", "message-type": "work-list", "message": {"total-results": 2815, "items": [
            {"DOI": "10.2139/ssrn.4723385", "title": ["Hold-Up, Innovation, and Platform Governance"], "abstract": "<jats:p>Motivated by large digital platforms, I study a model.</jats:p>",
             "author": [{"given": "Georgy", "family": "Bueno de Mesquita"}], "published": {"date-parts": [[2024]]}, "type": "posted-content"},
            {"DOI": "10.2139/ssrn.4483039", "title": ["Two-Faced Governance in Platform Ecosystems"], "author": [{"given": "A.", "family": "Harraca"}, {"family": "Gawer"}], "posted": {"date-parts": [[2023, 6]]}},
            {"DOI": "10.1111/not-ssrn.1", "title": ["A paper of another publisher"], "published": {"date-parts": [[2024]]}},
            {"DOI": "10.2139/ssrn.1", "title": [""], "published": {"date-parts": [[2024]]}}]}}
        real = fx_.http_get
        def fake_get(url, timeout=40, headers=None):
            asked.append(url); return json.dumps(answer).encode(), {}, 200
        fx_.http_get = fake_get
        try:
            got = fx_.crossref_ssrn("platform governance", 2023, 2024)
            u = asked[-1]
            assert "filter=prefix:10.2139,type:posted-content,from-pub-date:2023-01-01,until-pub-date:2024-12-31" in u and "query.bibliographic=platform%20governance" in u and "rows=200" in u, u
            assert [g["doi"] for g in got] == ["10.2139/ssrn.4723385", "10.2139/ssrn.4483039"], got                       # another publisher's DOI and an empty title are left out
            assert got[0]["pub_date"] == "2024-01" and got[1]["pub_date"] == "2023-06" and got[0]["abstract"].startswith("Motivated by large") and "<" not in got[0]["abstract"], got
            assert got[1]["authors"] == ["A. Harraca", "Gawer"] and got[0]["url"] == "https://doi.org/10.2139/ssrn.4723385", got
            fx_.crossref_ssrn("x" * 500, 2024, 2024, limit=5000); assert "rows=1000" in asked[-1] and len(asked[-1]) < 700                # words and rows are capped
            # the pseudo-journal exists for any journals.json, once, and is never read by the weekly check
            js = pl_.journals(); assert [j["abbr"] for j in js].count("SSRN") == 1 and [j for j in js if j["abbr"] == "SSRN"][0]["needs_query"]
            jpath = os.path.join(pl_.BASE, "journals.json"); saved = open(jpath).read() if os.path.exists(jpath) else None
            try:
                d = json.loads(saved) if saved else {"journals": []}
                d["journals"] = [j for j in d["journals"] if j["abbr"] != "SSRN"] + [dict(pl_.SSRN)]
                json.dump(d, open(jpath, "w"))
                assert [j["abbr"] for j in pl_.journals()].count("SSRN") == 1                                                       # (already there: not added twice)
                seen_ = []
                real_fj = fx_.fetch_journal
                fx_.fetch_journal = lambda j: (seen_.append(j["abbr"]) or [])
                try:
                    pl_.fetch_weekly(pl_.Run("weekly", {}), lambda ev, d=None: None, threading.Event())
                finally:
                    fx_.fetch_journal = real_fj
                assert seen_ and "SSRN" not in seen_, seen_
            finally:
                open(jpath, "w").write(saved) if saved is not None else os.remove(jpath)
            # an archive search with SSRN: the focus phrase is what is asked, the papers are SSRN's, and a saved copy is kept per phrase
            calls = []
            real_cr = fx_.crossref_ssrn
            fx_.crossref_ssrn = lambda q, y0, y1, limit=200, progress=None: calls.append((q, y0, y1, limit)) or [dict(g) for g in got]
            shutil.rmtree(pl_.FETCHED, ignore_errors=True)
            try:
                def search(q):
                    r = pl_.Run("search", {"year_from": 2023, "year_to": 2024, "journals": ["SSRN"], "topics": [], "methods": [], "query": q})
                    pl_.fetch_search(r, lambda ev, d=None: None, threading.Event()); return r
                r = search("platform governance")
                assert calls == [("platform governance", 2023, 2024, 200)] and len(r.order) == 2, calls
                p0 = r.papers[r.order[0]]; assert p0["publisher"] == "ssrn" and p0["journal"] == "SSRN" and p0["abbr"] == "SSRN" and p0["source_mode"] == "search", p0
                search("platform governance"); assert len(calls) == 1                                                                 # the same words, lately: the saved copy
                search("Platform Governance "); assert len(calls) == 1                                                               # (case and spaces do not matter)
                search("two-sided markets"); assert len(calls) == 2                                                                  # other words: asked again
            finally:
                fx_.crossref_ssrn = real_cr
        finally:
            fx_.http_get = real
        # the paper's page, from its DOI (whatever publisher the card says)
        assert dl.browser_pdf_url({"doi": "10.2139/ssrn.4723385", "publisher": "ssrn"}) == "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4723385"
        assert dl.browser_pdf_url({"doi": "10.2139/SSRN.4723385", "publisher": ""}) == "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4723385"
        assert dl.browser_pdf_url({"doi": "10.1287/mnsc.2024.0001", "publisher": "informs"}).startswith("https://pubsonline.informs.org/doi/pdf/")
        assert any("papers.ssrn.com" in u for _, u in brw.PUBLISHER_LOGINS)                                                                # a tab for it in "Other publisher sites"

    def t_library_test_record():
        """What the test finds changes the tick only when it is clear: a library with a sign-in of its own is ticked when the test got through and un-ticked when it was asked for a password."""
        sg = brw.si
        sg.save_profile({"mode": "openathens", "openathens": "testuni.ac.uk", "name": "Test University"})
        sg.forget("library")
        assert sg.status()["library"]["signed_in"] is False and sg.status()["library"]["test"] is None
        sg.record_test({"state": "ok", "message": "Signed in: through", "host": "x.org", "mode": "openathens"})
        s = sg.status()["library"]; assert s["signed_in"] is True and s["test"]["state"] == "ok" and s["test"]["host"] == "x.org" and s["test"]["age_hours"] is not None, s
        for state in ("unknown", "challenge", "error"):                                                    # unclear answers change nothing
            sg.record_test({"state": state, "message": "m", "mode": "openathens"}); assert sg.status()["library"]["signed_in"] is True and sg.status()["library"]["test"]["state"] == state
        sg.record_test({"state": "login", "message": "asked for a password", "mode": "openathens"})
        assert sg.status()["library"]["signed_in"] is False and sg.status()["library"]["test"]["state"] == "login"
        sg.save_profile({"mode": "vpn", "name": "Test University"})                                        # no sign-in of its own: a test never ticks (there is nothing to be signed in to)
        sg.record_test({"state": "ok", "message": "says: Access provided by", "mode": "vpn"})
        assert sg.status()["library"]["signed_in"] is False and sg.status()["library"]["test"]["state"] == "ok"
        sg.save_profile({"mode": "institutional"})

    check("browser: on a paywalled landing page only THIS article's PDF link is followed (its own link wherever the page puts it: aside, tab strip, 'footer'-named wrapper, other host, citation_pdf_url; not another site, a reference list, the footer, a banner PDF, another article or one whose DOI merely starts with ours)", t_link_follow)
    check("search memory: a verdict is reused for the same question and the same paper text (never another question, another text, an error, or one older than the number of days you set), a journal's year range is kept for a while, and the same search is the same saved run", t_search_memory)
    check("screening goes through the AI layer: its own model setting (not Ask), stopped by the monthly budget with the papers left unscreened, local-only mode applies, in the usage ledger and the run's cost (a billed refusal too), a refusal is not asked again, four batches at once work", t_screen_ai)
    check("library test: where each kind of answer leads (through, asked for a password, a visible versus a hidden password box, a human check, a page that says nothing, a page that does not open) and what the message claims; a library without a sign-in of its own is never said to be 'signed in'", t_library_test)
    check("library test: the tick changes only when the answer is clear (ticked when it got through, taken away when asked for a password), never for a library without a sign-in of its own", t_library_test_record)
    check("SSRN: searched by words through Crossref (prefix 10.2139: only SSRN's own, capped), a pseudo-journal that is never read weekly and not added twice, the papers marked as SSRN's, a saved copy per phrase, the paper's own page, and its download link followed only when it is that paper's", t_ssrn)

    def t_screen_errors():
        """A screening that fails says why, in the provider's words; an answer that asking again cannot change (a model without structured output, a refused key) stops the
        screening at the first batch with the papers left unscreened (not marked as failed); a busy provider or an answer that is not the JSON asked for marks only that
        batch, with the reason; failed papers are screened again by Retry screening; Settings' Test for screening finds a model that cannot screen."""
        import pipeline as pl_
        topic = pl_.topic_labels()[0]; abbr = pl_.journals()[0]["abbr"]
        saved_cfg = open(ai.CONFIG).read() if os.path.exists(ai.CONFIG) else None
        real_sleep = pl_.time.sleep
        events, stop = [], threading.Event()
        emit = lambda ev, d=None: events.append((ev, d))
        def verdicts(body):           # a model that screens: one result per paper it was shown
            items = json.loads(body["messages"][-1]["content"])
            return [json.dumps({"results": [{"n": it["n"], "verdict": "download", "relevance": 70, "topics": [topic], "method": "Field experiment", "reason": "fits"} for it in items]})]
        def new_run(query, n=5):
            r = pl_.Run("weekly", {"topics": [topic], "query": query})
            for k in range(n):
                r.add_paper({"id": "%s-%d" % (query, k), "title": "Paper %d about platforms" % k, "abbr": abbr, "pub_date": "2026-09-01", "abstract": "About platforms.", "keywords": ["x"]})
            return r
        def chat_calls():
            return [b for (path, b, _) in SEEN if path.endswith("/chat/completions") and b.get("response_format")]
        try:
            pl_.time.sleep = lambda s: None
            write_cfg({**CFG, "features": {"ask": {"provider": "fake", "model": "m1"}, "screen": {"provider": "fake", "model": "m1"}}})
            if os.path.exists(pl_.VERDICTS): os.remove(pl_.VERDICTS)
            # the HTTP status travels with the error, and decides what is worth asking again
            assert [ai.retryable(ai.AIError("x", status=c)) for c in (400, 401, 402, 403, 404, 422, 429, 500, 503, None)] == [False] * 6 + [True] * 4
            assert not ai.retryable(ai.Blocked("budget")) and not ai.retryable(ai.Declined("no")) and ai.AIError("plain").status is None
            # 1. the model cannot do what screening asks (OpenRouter's answer for a model without structured output): one request, then the screening stops;
            #    nothing is marked as failed, the reason is kept on the run (and survives saving it) and said in the log with what to check
            Fake.http_error = lambda body: (404, "No endpoints found that can handle the requested parameters.") if body.get("response_format") else None
            n0, rows0 = len(chat_calls()), len(ai.usage_log(100000))
            r = new_run("hard"); pl_.screen(r, emit, stop, batch_size=2, workers=1)
            assert len(chat_calls()) - n0 == 1, len(chat_calls()) - n0                                                        # not 3 batches x 3 tries
            assert not any(r.papers[i].get("verdict") for i in r.order), [r.papers[i].get("verdict") for i in r.order]
            assert "No endpoints found" in r.screen_error and "HTTP 404" in r.screen_error and "structured JSON" in r.screen_error, r.screen_error
            assert any(e == "log" and d.get("level") == "warn" and "5 papers not screened" in d["msg"] and "Retry screening" in d["msg"] for e, d in events), events[-2:]
            assert len(ai.usage_log(100000)) == rows0                                                                       # a refused request costs nothing and is not in the ledger
            r.save(force=True); assert pl_.Run.load(r.id).screen_error == r.screen_error and pl_.Run.load(r.id).to_dict(full=False)["screen_error"] == r.screen_error
            assert r.screen_backup == {} and {c["model"] for c in chat_calls()[n0:]} == {"m1"}, "a provider with no model list: no backup is tried"
            # 2. a key the provider refuses: the same, at once
            Fake.http_error = lambda body: (401, "User not found.") if body.get("response_format") else None
            n0 = len(chat_calls()); r2 = new_run("key"); pl_.screen(r2, emit, stop, batch_size=2, workers=1)
            assert len(chat_calls()) - n0 == 1 and not any(r2.papers[i].get("verdict") for i in r2.order) and "HTTP 401" in r2.screen_error and "structured JSON" not in r2.screen_error, r2.screen_error
            # 2b. a backup model (OpenRouter, a model that cannot answer in a JSON schema): the rest of the run is screened once more with the cheapest model of the same
            #     provider that can (from OpenRouter's list), for this run only (the setting is not changed); the run says which, and its verdicts are that model's
            assert ai.cannot_structure(ai.AIError("scaledown/summarize: response_format is not supported by this model (HTTP 400)", status=400))
            assert ai.cannot_structure(ai.AIError("No endpoints found that can handle the requested parameters.", status=404))
            assert not ai.cannot_structure(ai.AIError("response_format is not supported", status=401)) and not ai.cannot_structure(ai.AIError("maximum context length exceeded", status=400))
            assert not ai.cannot_structure(ai.AIError("output_config.effort: unknown value", status=400)) and not ai.cannot_structure(ai.Blocked("response_format"))
            import prices as pr_
            saved_pr = (pr_.CACHE, pr_.OR_URL, os.environ.get("JW_OFFLINE"))
            pr_.CACHE, pr_.OR_URL = os.path.join(TMP, "or_backup_prices.json"), "http://127.0.0.1:9/api/v1/models"     # (never the real list)
            os.environ["JW_OFFLINE"] = "1"
            sd = {"in": 0.04, "out": 0.0, "cache_write": 0.04, "cache_read": 0.04}
            json.dump({"format": pr_.FORMAT, "fetched": "2026-11-06T00:00:00+00:00", "fetched_ts": pl_.time.time(),
                       "models": {"scaledown/summarize": sd, "cheap/json": {"in": 0.05, "out": 0.05, "cache_write": 0.05, "cache_read": 0.05, "json": True}}}, open(pr_.CACHE, "w"))
            pr_._mem["data"] = None
            orfake = dict(CFG["providers"]["fake"], base_url=CFG["providers"]["fake"]["base_url"].replace("/v1", "/openrouter.ai/v1"))     # (the fake server, at an address that reads as OpenRouter)
            write_cfg({**CFG, "providers": dict(CFG["providers"], orfake=orfake), "features": {"ask": {"provider": "fake", "model": "m1"}, "screen": {"provider": "orfake", "model": "scaledown/summarize"}}})
            cfg_text = open(ai.CONFIG).read()
            no_schema = lambda models: (lambda body: (400, "%s: response_format is not supported by this model" % body["model"]) if body.get("response_format") and body.get("model") in models else None)
            try:
                Fake.http_error, Fake.responder = no_schema({"scaledown/summarize"}), verdicts
                n0, events[:] = len(chat_calls()), []
                r6 = new_run("backup"); pl_.screen(r6, emit, stop, batch_size=2, workers=1)
                assert [c["model"] for c in chat_calls()[n0:]] == ["scaledown/summarize", "cheap/json", "cheap/json", "cheap/json"], [c["model"] for c in chat_calls()[n0:]]
                assert all(r6.papers[i]["verdict"] == "download" for i in r6.order) and r6.screen_error == "", (r6.screen_error, [r6.papers[i].get("verdict") for i in r6.order])
                b6 = r6.screen_backup
                assert b6["model"] == "cheap/json" and b6["provider"] == "orfake" and b6["instead_of"] == "scaledown/summarize" and b6["within"] is False and "response_format" in b6["why"], b6
                assert open(ai.CONFIG).read() == cfg_text, "the screening setting is not changed"
                logs = [d["msg"] for e, d in events if e == "log" and "backup" in d["msg"]]
                assert len(logs) == 1 and "cheap/json" in logs[0] and "unchanged" in logs[0] and "twice its price" in logs[0], logs
                r6.save(force=True); assert pl_.Run.load(r6.id).screen_backup == b6 and pl_.Run.load(r6.id).to_dict(full=False)["screen_backup"] == b6
                system6 = pl_.screening_system(r6.params.get("topics"), r6.params.get("query"), r6.params.get("methods"))
                vd = json.load(open(pl_.VERDICTS))
                fp_b, fp_c = pl_.screen_fingerprint(system6, {"provider": "orfake", "model": "cheap/json"}), pl_.screen_fingerprint(system6)
                assert set(r6.order) <= set(vd.get(fp_b) or {}) and not set(r6.order) & set(vd.get(fp_c) or {}), "the verdicts are the backup's, not the chosen model's"
                # the chosen model works again: new papers are screened by it, and the run no longer names a backup
                Fake.http_error = None
                for k in (7, 8):
                    r6.add_paper({"id": "backup-%d" % k, "title": "Paper %d about platforms" % k, "abbr": abbr, "pub_date": "2026-09-01", "abstract": "About platforms.", "keywords": ["x"]})
                n0 = len(chat_calls()); pl_.screen(r6, emit, stop, batch_size=2, workers=1)
                assert [c["model"] for c in chat_calls()[n0:]] == ["scaledown/summarize"] and r6.screen_backup == {}, ([c["model"] for c in chat_calls()[n0:]], r6.screen_backup)
                # four batches on their way at once: those already sent finish, then the rest goes to the backup; said once
                Fake.http_error = no_schema({"scaledown/summarize"})
                n0, events[:] = len(chat_calls()), []
                r7 = new_run("backup4", n=6); pl_.screen(r7, emit, stop, batch_size=1, workers=4)
                ms = [c["model"] for c in chat_calls()[n0:]]
                assert 1 <= ms.count("scaledown/summarize") <= 4 and ms.count("cheap/json") == 6 and all(r7.papers[i]["verdict"] == "download" for i in r7.order), ms
                assert len([1 for e, d in events if e == "log" and "backup" in d["msg"]]) == 1
                # not for a refused key, nor a request that is too long: those stop as before, no backup
                for err in ((401, "User not found."), (400, "This model's maximum context length is 8192 tokens.")):
                    Fake.http_error = (lambda e_: (lambda body: e_ if body.get("response_format") else None))(err)
                    n0 = len(chat_calls()); r8 = new_run("nobackup%d" % err[0]); pl_.screen(r8, emit, stop, batch_size=2, workers=1)
                    assert [c["model"] for c in chat_calls()[n0:]] == ["scaledown/summarize"] and r8.screen_backup == {} and r8.screen_error and not any(r8.papers[i].get("verdict") for i in r8.order), (err, r8.screen_error)
                # the backup cannot either: one request to each, never a third model; the papers wait unscreened and the reason names both
                Fake.http_error = no_schema({"scaledown/summarize", "cheap/json"})
                n0 = len(chat_calls()); r9 = new_run("both"); pl_.screen(r9, emit, stop, batch_size=2, workers=1)
                assert [c["model"] for c in chat_calls()[n0:]] == ["scaledown/summarize", "cheap/json"], [c["model"] for c in chat_calls()[n0:]]
                assert not any(r9.papers[i].get("verdict") for i in r9.order) and r9.screen_backup == {} and "scaledown/summarize" in r9.screen_error and "cheap/json" in r9.screen_error, r9.screen_error
                # a backup that answers but never with verdicts (prose): its papers fail, and the run does not say it was screened by it
                Fake.http_error = no_schema({"scaledown/summarize"}); Fake.responder = lambda body: ["Just a summary of the papers."]
                r11 = new_run("backupfails", n=2); pl_.screen(r11, emit, stop, batch_size=2, workers=1)
                assert r11.screen_backup == {} and {r11.papers[i]["verdict"] for i in r11.order} == {"error"}, (r11.screen_backup, [r11.papers[i].get("verdict") for i in r11.order])
                Fake.responder = verdicts
                # no list to choose from (offline, nothing saved): it stops as before and says that no backup could be chosen
                os.remove(pr_.CACHE); pr_._mem["data"] = None
                Fake.http_error = no_schema({"scaledown/summarize"})
                n0 = len(chat_calls()); r10 = new_run("nolist"); pl_.screen(r10, emit, stop, batch_size=2, workers=1)
                assert len(chat_calls()) - n0 == 1 and "No backup model could be chosen" in r10.screen_error and r10.screen_backup == {}, r10.screen_error
            finally:
                pr_.CACHE, pr_.OR_URL = saved_pr[:2]; pr_._mem["data"] = None
                os.environ.pop("JW_OFFLINE", None) if saved_pr[2] is None else os.environ.__setitem__("JW_OFFLINE", saved_pr[2])
                Fake.http_error = None; Fake.responder = None
                write_cfg({**CFG, "features": {"ask": {"provider": "fake", "model": "m1"}, "screen": {"provider": "fake", "model": "m1"}}})
            # 3. a busy provider every time: each batch is asked three times, then its papers are marked as failed with the provider's words
            Fake.http_error = lambda body: (503, "upstream overloaded") if body.get("response_format") else None
            n0, events[:] = len(chat_calls()), []
            r3 = new_run("busy", n=3); pl_.screen(r3, emit, stop, batch_size=2, workers=1)
            assert len(chat_calls()) - n0 == 6, len(chat_calls()) - n0                                                        # 2 batches x 3 tries
            assert {r3.papers[i]["verdict"] for i in r3.order} == {"error"} and all("upstream overloaded" in r3.papers[i]["reason"] and "HTTP 503" in r3.papers[i]["reason"] for i in r3.order), [r3.papers[i]["reason"] for i in r3.order]
            assert sum(1 for e, d in events if e == "log" and "Screening failed for" in d["msg"]) == 2 and r3.screen_error == ""
            # 4. a summariser: it answers, in prose: every answer is paid for and in the ledger, and the reason says it was not the JSON (with how it began)
            Fake.http_error = None; Fake.responder = lambda body: ["This paper studies how ", "platforms nudge users."]
            n0, rows0 = len(chat_calls()), len(ai.usage_log(100000))
            r4 = new_run("prose", n=2); pl_.screen(r4, emit, stop, batch_size=2, workers=1)
            assert len(chat_calls()) - n0 == 3 and len(ai.usage_log(100000)) - rows0 == 3
            assert {r4.papers[i]["verdict"] for i in r4.order} == {"error"} and all("not the JSON" in r4.papers[i]["reason"] and "This paper studies" in r4.papers[i]["reason"] for i in r4.order), r4.papers[r4.order[0]]["reason"]
            # 5. an answer that leaves a paper out: only that paper fails, and says so
            Fake.responder = lambda body: [json.dumps({"results": [{"n": 1, "verdict": "skip", "relevance": 5, "topics": [], "method": "Survey", "reason": "no"}]})]
            r5 = new_run("partial", n=2); pl_.screen(r5, emit, stop, batch_size=2, workers=1)
            assert r5.papers["partial-0"]["verdict"] == "skip" and r5.papers["partial-1"]["verdict"] == "error" and "no verdict for this paper" in r5.papers["partial-1"]["reason"]
            # 6. Retry screening (what /api/rescreen does): the failed ones lose their "error" and are asked again with the papers never screened; a clean run clears the reason
            Fake.responder = verdicts
            for run in (r, r3, r4):
                for p in run.papers.values():
                    if p.get("verdict") == "error":
                        p.pop("verdict")
                pl_.screen(run, emit, stop, batch_size=2, workers=1)
                assert all(run.papers[i]["verdict"] == "download" and run.papers[i]["reason"] == "fits" for i in run.order), [run.papers[i].get("verdict") for i in run.order]
            assert r.screen_error == ""
            # 7. Settings → AI → Screening new papers → Test: a small request with the real schema
            ok = pl_.screen_test("fake", "m1"); assert ok["ok"] and "screens: download" in ok["text"], ok
            Fake.responder = lambda body: ["A summary of the paper."]
            bad = pl_.screen_test("fake", "m1"); assert not bad["ok"] and "not with the structured verdicts" in bad["error"] and "A summary of the paper" in bad["error"], bad
            Fake.responder = None; Fake.http_error = lambda body: (404, "No endpoints found that can handle the requested parameters.") if body.get("response_format") else None
            no = pl_.screen_test("fake", "m1"); assert not no["ok"] and no["error"].startswith("Can’t screen papers: the provider refused") and "HTTP 404" in no["error"], no
            assert ai.test("fake", "m1")["ok"]                                                                             # …while the plain test says the model works: why the screening test exists
        finally:
            Fake.http_error = None; Fake.responder = None; pl_.time.sleep = real_sleep
            if saved_cfg is None:
                os.path.exists(ai.CONFIG) and os.remove(ai.CONFIG)
            else:
                open(ai.CONFIG, "w").write(saved_cfg)

    def t_summaries_job():
        """Library → "Summarise N papers…": only downloaded papers without a summary (never a book, a missing PDF or a failed download); the cost guess rests on earlier
        summaries with the same model when there are enough, else on the prices, else it is unknown; the job writes each summary beside its PDF, skips one written
        meanwhile, stops at once on a refused key, stops after three failures in a row, never writes an empty summary, and runs one at a time."""
        import csv, time as _t, pipeline as pl_, summaries as su
        import activity as act_
        root = os.path.join(TMP, "sum_home"); folder = os.path.join(root, "Papers", "2026-09", "MIS Quarterly"); os.makedirs(folder)
        os.makedirs(os.path.join(root, "Papers", "2026-09", "Books"))
        src = os.path.join(TMP, "sum_src.pdf"); make_pdf(src)
        rel = lambda n, j="MIS Quarterly": "Papers/2026-09/%s/%s.pdf" % (j, n)
        for n in ("a", "b", "f1", "f2", "f3", "f4"):
            shutil.copy(src, os.path.join(root, rel(n)))
        shutil.copy(src, os.path.join(root, rel("book", "Books")))
        open(os.path.join(root, rel("b")[:-4] + ".md"), "w").write("# B\n\nAlready summarised.\n")
        hdr = "date_checked journal abbr title authors doi url matched_topics score file status note".split()
        def index(names):
            with open(os.path.join(root, "Papers", "_index.csv"), "w", newline="") as f:
                w = csv.writer(f); w.writerow(hdr)
                for n in names:
                    if n == "book":
                        w.writerow(["2026-09-01", "Books", "BOOK", "A Book", "Ann Lee", "", "", "", "", rel("book", "Books"), "ok", ""])
                    elif n == "missing":
                        w.writerow(["2026-09-01", "MIS Quarterly", "MISQ", "Gone", "", "10.1/gone", "", "", "", rel("gone"), "ok", ""])
                    elif n == "failed":
                        w.writerow(["2026-09-01", "MIS Quarterly", "MISQ", "Never came", "", "10.1/fail", "", "", "", "", "failed", ""])
                    else:
                        w.writerow(["2026-09-01", "MIS Quarterly", "MISQ", "Paper " + n.upper(), "Jane Doe", "10.1/" + n, "", "", "", rel(n), "ok", ""])
        saved = (pl_.PAPERS, pl_.ROOT, su.ROOT, ai.LEDGER, open(ai.CONFIG).read() if os.path.exists(ai.CONFIG) else None)
        pl_.PAPERS, pl_.ROOT, su.ROOT, ai.LEDGER = os.path.join(root, "Papers"), root, root, os.path.join(root, "ledger.jsonl")
        def wait():
            t0 = _t.time()
            while su.status()["running"] and _t.time() - t0 < 60:
                _t.sleep(0.05)
            assert not su.status()["running"], "the job did not end"
            return su.status()
        try:
            write_cfg({**CFG, "features": {"ask": {"provider": "fake", "model": "m1"}, "summarize": {"provider": "fake", "model": "m1"}}})
            index(["a", "b", "book", "missing", "failed"])
            assert [r["file"] for r in su.pending()] == [rel("a")], [r["file"] for r in su.pending()]                  # not b (summarised), not the book, not a missing PDF, not a failed download
            e = su.estimate(); assert e["n"] == 1 and e["files"] == [rel("a")] and e["basis"] == "prices" and e["cost"] > 0 and e["provider"] == "fake" and e["model"] == "m1", e
            for c in (0.002, 0.004, 0.003):                                                                                # three earlier summaries with this model: the guess rests on them
                open(ai.LEDGER, "a").write(json.dumps({"ts": "2026-11-01T10:00:00+00:00", "feature": "summarize", "provider": "fake", "model": "m1", "in": 1, "out": 1, "cost": c}) + "\n")
            open(ai.LEDGER, "a").write(json.dumps({"ts": "2026-11-01T10:00:00+00:00", "feature": "summarize", "provider": "fake", "model": "m1", "cost": 0.0, "cached": True}) + "\n")      # (free, from the cache: not counted)
            open(ai.LEDGER, "a").write(json.dumps({"ts": "2026-11-01T10:00:00+00:00", "feature": "summarize", "provider": "other", "model": "m1", "cost": 9.0}) + "\n")                    # (another provider: not counted)
            e = su.estimate(); assert e["basis"] == "history" and e["calls"] == 3 and abs(e["per_paper"] - 0.003) < 1e-9 and abs(e["cost"] - 0.003) < 1e-9, e
            write_cfg({**CFG, "prices": {}, "features": {"ask": {"provider": "fake", "model": "m2"}, "summarize": {"provider": "fake", "model": "m2"}}})
            e = su.estimate(); assert e["basis"] == "unknown" and e["cost"] is None and e["model"] == "m2", e                # no price, no history: said, not invented
            write_cfg({**CFG, "features": {"ask": {"provider": "fake", "model": "m1"}, "summarize": {"provider": "fake", "model": "m1"}}})
            # the job: one summary written beside its PDF, through the AI layer (the ledger), in the tray as one job
            Fake.responder = lambda body: ["# Paper A\n\n", "**Authors**: Jane Doe\n\n## TL;DR\nA test summary."]
            rows0 = len(ai.usage_log(100000))
            assert su.start([rel("a")]) is True and su.start([rel("a")]) is False                                          # one job at a time
            st = wait(); assert st["made"] == 1 and not st["failed"] and not st["stopped"] and st["done"] == st["total"] == 1, st
            assert open(os.path.join(root, rel("a")[:-4] + ".md")).read().startswith("# Paper A") and len(ai.usage_log(100000)) - rows0 == 1
            js = [j for j in act_.snapshot() if j["kind"] == "summary"]; assert js and js[0]["status"] == "done" and js[0]["scope"] == {"library": True, "ltab": "papers"}, js[:1]
            assert su.pending() == [] and su.start() is False and su.estimate()["n"] == 0                                 # nothing left to do
            # three failures in a row stop it (here: the model answers nothing), and an empty answer is never written as a summary
            index(["f1", "f2", "f3", "f4"]); Fake.responder = lambda body: [""]
            assert su.start() is True; st = wait()
            assert st["made"] == 0 and len(st["failed"]) == 3 and "3 papers in a row failed" in st["stopped"] and "empty summary" in st["failed"][0]["error"], st
            assert not any(os.path.exists(os.path.join(root, rel(n)[:-4] + ".md")) for n in ("f1", "f2", "f3", "f4"))
            # a key the provider refuses stops it at the first paper
            Fake.responder = None; Fake.http_error = lambda body: (401, "User not found.")
            assert su.start() is True; st = wait()
            assert st["made"] == 0 and len(st["failed"]) == 1 and "HTTP 401" in st["stopped"], st
            # only the papers asked for, and one summarised meanwhile (by another computer, say) is skipped, not paid for twice
            Fake.http_error = None; Fake.responder = lambda body: ["# F\n\nSummary."]
            open(os.path.join(root, rel("f1")[:-4] + ".md"), "w").write("# F1\n\nWritten elsewhere.\n")
            rows0 = len(ai.usage_log(100000))
            assert su.start([rel("f1"), rel("f2")]) is True; st = wait()
            assert st["made"] == 1 and st["total"] == 1 and len(ai.usage_log(100000)) - rows0 == 1 and open(os.path.join(root, rel("f1")[:-4] + ".md")).read().startswith("# F1\n\nWritten")
            assert sorted(r["file"] for r in su.pending()) == sorted([rel("f3"), rel("f4")])
            # the budget: nothing may be asked, so it stops at once and says why
            ai.save_config({"budget": {"monthly_usd": 0.000001}})
            assert su.start() is True; st = wait()
            assert st["made"] == 0 and "budget" in st["stopped"] and len(st["failed"]) == 1, st
        finally:
            Fake.http_error = None; Fake.responder = None
            pl_.PAPERS, pl_.ROOT, su.ROOT, ai.LEDGER = saved[:4]
            if saved[4] is None:
                os.path.exists(ai.CONFIG) and os.remove(ai.CONFIG)
            else:
                open(ai.CONFIG, "w").write(saved[4])
            shutil.rmtree(root, ignore_errors=True)

    check("screening errors: the provider's own words on every failed paper; a refused request (a model without structured output, a refused key) stops at the first batch and leaves the papers unscreened with the reason on the run; a busy provider or prose marks only that batch; Retry screening asks again; the screening Test finds a model that cannot screen", t_screen_errors)
    check("summarise the papers without a summary: only downloaded papers (no book, missing PDF or failed download), a cost guess that says what it rests on, one background job in the tray, a summary beside each PDF, skipped if written meanwhile, stopped by a refused key, the budget or three failures in a row, never an empty summary", t_summaries_job)

    srv.shutdown(); shutil.rmtree(TMP, ignore_errors=True)
    print("\n%d failed" % len(FAILS) if FAILS else "\nall passed")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
