"""Summaries for the downloaded papers in your library that have none yet: the Library's "Summarise N papers…" button.

    summaries.pending()           [library row] of the papers to summarise: a PDF on disk, no summary beside it, not a book
    summaries.estimate()          {n, files, provider, model, cost, cost_low, cost_high, per_paper, basis, calls, history_min, running}: what the button shows before
                                  anything is spent
    summaries.start(files)        one background job in the activity tray (Cancel works there); False while one is running
    summaries.status()            {running, done, total, made, failed: [{file, title, error}], cost, error, stopped, ended}

Each paper is summarised exactly as the pipeline does after a download (pipeline.summarize: the "Paper summaries" model in Settings → AI, the summary template in
Settings → Reader), so the summary is written beside its PDF as <pdf>.md and the paper gets its "Ready to read" bar. The calls go through the budgeted AI layer: the
monthly budget, local-only mode, the usage ledger and the tray's cost all apply. Books are left out (a book is never summarised by itself: books.py), and so is a
paper whose PDF is missing. The job never opens the download browser: nothing is downloaded.

What stops the job and what does not: a problem that would hit every paper (ai.Blocked: no key, budget used up, local-only; or an answer asking again cannot change
for this key or model: 401, 402, 403, 404) stops it at once and says why. Any other failure is noted against that paper and the job goes on, unless three papers in a
row fail, which almost always means the same problem for all of them; it then stops instead of paying for a long list of failures.
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import activity as act  # noqa: E402
import ai  # noqa: E402
import pipeline as pl  # noqa: E402

ROOT = pl.ROOT
OUT_TOKENS = 1500              # a summary in the default template is about 1,000 to 1,700 tokens long (measured on real summaries)
HISTORY_MIN = 3                # past summaries by the same provider and model that make the cost guess rest on what they cost
STOP_ALL = frozenset({401, 402, 403, 404})       # key, credit, permission, model: the same for every paper
MAX_IN_A_ROW = 3

_lock = threading.Lock()
_state = {"running": False, "done": 0, "total": 0, "made": 0, "failed": [], "cost": 0.0, "error": "", "stopped": "", "ended": None}


def _pdf(r):
    rel = r.get("file") or ""
    path = os.path.join(ROOT, rel)
    return path if rel.lower().endswith(".pdf") and os.path.isfile(path) else None


def pending():
    """The library rows to summarise, in the library's order (newest first). The summary file is looked for on disk, so a summary written on another computer that
    shares the data folder counts."""
    out = []
    for r in pl.library():
        if r.get("journal") == "Books" or r.get("book") is not None:
            continue
        pdf = _pdf(r)
        if pdf and not os.path.exists(pdf[:-4] + ".md"):
            out.append(r)
    return out


def _native(provider):
    """Whether this provider is sent the PDF itself (Claude reads every page as text and as an image) rather than its extracted text."""
    return ((ai.config_data().get("providers") or {}).get(provider) or {}).get("type") == "anthropic"


def _chars(pdf, native):
    """(low, likely, high) characters of a paper as the provider will read it, from the size of the file alone (ai.pdf_chars). The PDF itself is not opened: the
    button asks this for every paper at once, and a file Dropbox keeps online only would be downloaded just to be measured."""
    return ai.pdf_chars(pdf, native)


def _history(provider, model):
    """What past summaries by this provider and model cost, from the usage ledger: (calls, mean cost per paper). Cached answers (free) and calls without a price are
    left out, so the mean is what a new summary is likely to cost."""
    costs = [r["cost"] for r in ai._rows() if r.get("feature") == "summarize" and r.get("provider") == provider and r.get("model") == model
             and not r.get("cached") and not r.get("cancelled") and isinstance(r.get("cost"), (int, float))]
    return len(costs), (sum(costs) / len(costs) if costs else None)


def estimate():
    """What "Summarise N papers…" would do, before anything is spent. basis says what the cost rests on:
    "history"  the mean cost of your earlier summaries with this provider and model (at least HISTORY_MIN of them): the most reliable guess, since it includes how the
               provider counts a PDF (Claude reads its pages as images too) and how long the summaries come out;
    "prices"   the price table in Settings → AI and the size of each PDF file. The size of a file says only roughly how many pages it has, so this is a range:
               cost is the likely figure, cost_low and cost_high its ends (for Claude, which is sent the PDF itself, each page is counted as an image too);
    "unknown"  no price for this model and too few past summaries: cost is None.
    cost_low and cost_high are None unless the basis is "prices" with papers to do. history_min is how many earlier summaries make the basis "history"."""
    rows = pending()
    provider, model = ai.feature_model("summarize")
    calls, mean = _history(provider, model)
    low = high = None
    if calls >= HISTORY_MIN:
        basis, per = "history", mean
        cost = per * len(rows)
    else:
        native = _native(provider)
        sizes = [_chars(_pdf(r), native) for r in rows]
        costs = [ai.estimate("summarize", sum(s[i] + 3000 for s in sizes), OUT_TOKENS * len(rows)) for i in range(3)]      # (low, likely, high: one sum for all the papers, the cost being so much per character)
        if rows and all(c is not None for c in costs):
            basis, (low, cost, high) = "prices", costs
            per = cost / len(rows)
        elif not rows and ai.estimate("summarize", 30_000, OUT_TOKENS) is not None:
            basis, cost, per = "prices", 0.0, None
        else:
            basis, cost, per = "unknown", None, None
    with _lock:
        running = _state["running"]
    return {"n": len(rows), "files": [r["file"] for r in rows], "provider": provider, "model": model, "cost": cost, "cost_low": low, "cost_high": high, "per_paper": per, "basis": basis,
            "calls": calls, "history_min": HISTORY_MIN, "running": running}


def status():
    with _lock:
        s = dict(_state)
        s["failed"] = list(_state["failed"])
    return s


def start(files=None):
    """Summarise the pending papers, or only those of `files` that are still pending (the list the estimate showed: a paper summarised meanwhile, on this computer or
    another, is not paid for twice). False when a job is already running or there is nothing to do."""
    with _lock:
        if _state["running"]:            # (asked first: working out what is pending reads the whole library)
            return False
    rows = pending()
    if files is not None:
        want = set(files)
        rows = [r for r in rows if r["file"] in want]
    with _lock:
        if _state["running"] or not rows:
            return False
        _state.update(running=True, done=0, total=len(rows), made=0, failed=[], cost=0.0, error="", stopped="", ended=None)
    threading.Thread(target=_run, args=(rows,), daemon=True).start()
    return True


def _note(**kw):
    with _lock:
        _state.update(kw)


def _run(rows):
    job = act.start("summary", "Summaries for %d paper%s" % (len(rows), "" if len(rows) == 1 else "s"), scope={"library": True, "ltab": "papers"}, total=len(rows))
    usage, made, failed, in_a_row, stopped = pl.Usage(), 0, [], 0, ""
    try:
        for i, r in enumerate(rows):
            if job.cancelled:
                stopped = "cancelled"
                break
            title = str(r.get("title") or os.path.basename(r["file"]))
            job.update(msg=title[:90], done=i)
            pdf = _pdf(r)
            err = None
            if not pdf:
                err = "the PDF is no longer there"
            elif os.path.exists(pdf[:-4] + ".md"):          # written meanwhile (by another computer that shares the data folder, say): nothing to do
                _note(done=i + 1)
                job.update(done=i + 1)
                continue
            else:
                try:
                    pl.summarize({"title": r.get("title"), "journal": r.get("journal"), "doi": r.get("doi")}, pdf, usage)
                except ai.Cancelled:
                    stopped = "cancelled"
                    break
                except ai.AIError as e:
                    err = str(e) or "the provider gave no answer"
                    if isinstance(e, ai.Blocked) or getattr(e, "status", None) in STOP_ALL:
                        failed.append({"file": r["file"], "title": title[:120], "error": err[:300]})
                        stopped = err
                        break
                except Exception as e:  # noqa: BLE001      (a PDF that cannot be read must not stop the others)
                    err = "%s: %s" % (type(e).__name__, str(e)[:200])
            if err:
                failed.append({"file": r["file"], "title": title[:120], "error": err[:300]})
                in_a_row += 1
                job.update(errors=job.errors + 1, error="%s: %s" % (title[:40], err[:150]))
                if in_a_row >= MAX_IN_A_ROW:
                    stopped = "%d papers in a row failed, the last with: %s" % (in_a_row, err[:200])
                    break
            else:
                made += 1
                in_a_row = 0
            _note(done=i + 1, made=made, failed=list(failed), cost=usage.cost())
            job.update(done=i + 1)
        job.update(msg="")
        if stopped and stopped != "cancelled":
            job.update(error=stopped[:200])
        job.finish("cancelled" if stopped == "cancelled" else "error" if stopped else "done")
    except Exception as e:  # noqa: BLE001
        stopped = stopped or str(e)[:200]
        job.update(error=stopped); job.finish("error")
    finally:
        _note(running=False, made=made, failed=list(failed), cost=usage.cost(), stopped=stopped, error="" if stopped == "cancelled" else stopped, ended=time.time())
