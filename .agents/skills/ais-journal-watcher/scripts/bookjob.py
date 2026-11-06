"""Summarise a whole book in one go: the Chapters panel's "Summarise the whole book…" button.

    bookjob.start(rel, title)     one background job in the activity tray (Cancel works there); False while one is running or when there is nothing to do
    bookjob.status()              {running, file, title, done, total, made, skipped (parts with no text), failed: [{key, title, error}], cost, stopped, overview, ended}

Every entry of the book's outline (chapters and their sections: books.parts) that has no summary yet is summarised, one after another, exactly as the per-chapter
"Summarise…" button does (books._summarise: the "Paper summaries" model in Settings → AI, the budgeted AI layer, the summary saved in the book's .book.json). A part
summarised meanwhile (by that button, or on another computer that shares the data folder) is skipped, not paid for twice: the book's file is read again before each
part. A part with no body text (a bibliography, an index) is noted as such and left out, with nothing asked. When every part has its summary, one overview of
the whole book is written from those summaries (books.write_overview), never from the book's text.

What stops the job: Cancel; a problem that would hit every part (ai.Blocked: no key, budget used up, local-only; or 401, 402, 403, 404) at once; three parts in a row
that fail. Any other failure is noted against that part and the job goes on, but then no overview is written (it would leave a part out): pressing the button again
does only the parts still missing, then the overview.
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import activity as act  # noqa: E402
import ai  # noqa: E402
import books  # noqa: E402

STOP_ALL = frozenset({401, 402, 403, 404})       # key, credit, permission, model: the same for every part
MAX_IN_A_ROW = 3

_lock = threading.Lock()
_state = {"running": False, "file": "", "title": "", "done": 0, "total": 0, "made": 0, "skipped": 0, "failed": [], "cost": 0.0, "stopped": "", "overview": False, "ended": None}


def status():
    with _lock:
        s = dict(_state)
        s["failed"] = list(_state["failed"])
    return s


def _note(**kw):
    with _lock:
        _state.update(kw)


def start(rel, title=""):
    """Start summarising the book `rel` (a path under Papers/). False when a book job is already running, or when every part has its summary and the overview is
    up to date. Raises ValueError for a file that is not a book, or a book with no outline."""
    with _lock:
        if _state["running"]:
            return False
    pdf, info, todo, ps = books.whole_plan(rel)
    if not todo and not books.overview_pending(info):
        return False
    with _lock:
        if _state["running"]:
            return False
        _state.update(running=True, file=rel, title=title, done=0, total=len(todo) + 1, made=0, skipped=0, failed=[], cost=0.0, stopped="", overview=False, ended=None)
    threading.Thread(target=_run, args=(rel, title, pdf, todo), daemon=True).start()
    return True


def _run(rel, title, pdf, todo):
    job = act.start("summary", "Book summary · %s" % (title or os.path.basename(rel))[:80], scope={"library": True, "ltab": "books"}, total=len(todo) + 1)
    made, failed, in_a_row, stopped, cost, wrote, skipped = 0, [], 0, "", 0.0, False, 0
    try:
        blocks = books.bl.extract(pdf) if todo else None
        for i, c in enumerate(todo):
            if job.cancelled:
                stopped = "cancelled"
                break
            key = books.chapter_key(c)
            job.update(msg=c["title"][:90], done=i)
            info = books.info_at(pdf)                               # (read again: a part summarised meanwhile is not paid for twice)
            if ((info.get("summaries") or {}).get(key) or {}).get("text"):
                _note(done=i + 1)
                job.update(done=i + 1)
                continue
            err = None
            try:
                gen = books._summarise(pdf, info, c, title, blocks=blocks)
                while True:
                    try:
                        next(gen)
                    except StopIteration as fin:
                        rec = fin.value
                        break
                cost += rec.get("cost") or 0.0
            except books.NoText:                                    # a bibliography, an index: nothing to summarise (noted, so it is not tried again)
                books.mark_empty(pdf, c)
                skipped += 1
                _note(done=i + 1, skipped=skipped)
                job.update(done=i + 1)
                continue
            except ai.Cancelled:
                stopped = "cancelled"
                break
            except ai.AIError as e:
                err = str(e) or "the provider gave no answer"
                if isinstance(e, ai.Blocked) or getattr(e, "status", None) in STOP_ALL:
                    failed.append({"key": key, "title": c["title"][:120], "error": err[:300]})
                    stopped = err
                    break
            except Exception as e:  # noqa: BLE001      (one part that cannot be read must not stop the others)
                err = "%s: %s" % (type(e).__name__, str(e)[:200])
            if err:
                failed.append({"key": key, "title": c["title"][:120], "error": err[:300]})
                in_a_row += 1
                job.update(errors=job.errors + 1, error="%s: %s" % (c["title"][:40], err[:150]))
                if in_a_row >= MAX_IN_A_ROW:
                    stopped = "%d parts in a row failed, the last with: %s" % (in_a_row, err[:200])
                    break
            else:
                made += 1
                in_a_row = 0
            _note(done=i + 1, made=made, failed=list(failed), cost=cost)
            job.update(done=i + 1)
        if not stopped and not failed and not job.cancelled:
            info = books.info_at(pdf)
            if books.overview_pending(info):
                job.update(msg="Overview of the whole book", done=len(todo))
                try:
                    rec = books.write_overview(rel, title)
                    cost += rec.get("cost") or 0.0
                    wrote = True
                except ai.Cancelled:
                    stopped = "cancelled"
                except (ai.AIError, ValueError) as e:
                    stopped = "The overview could not be written: %s" % (str(e) or "no answer")[:200]
        elif job.cancelled and not stopped:
            stopped = "cancelled"
        job.update(msg="", done=len(todo) + (1 if wrote else 0))
        if stopped and stopped != "cancelled":
            job.update(error=stopped[:200])
        job.finish("cancelled" if stopped == "cancelled" else "error" if stopped or failed else "done")
    except Exception as e:  # noqa: BLE001
        stopped = stopped or str(e)[:200]
        job.update(error=stopped); job.finish("error")
    finally:
        _note(running=False, done=len(todo) + (1 if wrote else 0), made=made, failed=list(failed), cost=cost, stopped=stopped, overview=wrote, ended=time.time())
