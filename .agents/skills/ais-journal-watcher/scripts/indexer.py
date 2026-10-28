"""Reads every paper of the library into the semantic index in the background, so the first Ask over a folder or the whole library does not wait for it.

    indexer.kick(delay=0, manual=False)    # start a pass over the papers that are not indexed yet (does nothing if one is running)
    indexer.status()                       # {"enabled", "local", "running"}; indexer.pending() lists what is left

It runs when the server starts, after papers are downloaded or added, and when you press "Index now" in the activity tray. It happens by itself only with the
local embedding model (free, on this computer); with an embedding service that costs money it waits until you ask. Indexing is one job in the tray and can be cancelled;
a paper that is half done is simply done next time.
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import activity as act  # noqa: E402
import ai  # noqa: E402
import embed  # noqa: E402
import notes_store as ns  # noqa: E402
import pipeline as pl  # noqa: E402
import prefs  # noqa: E402

_lock = threading.Lock()
_running = False


def local():
    try:
        return ai.config_data()["embeddings"]["provider"] == "local"
    except Exception:  # noqa: BLE001
        return False


def enabled():
    return bool(prefs.get_settings().get("background_index", True))


def pending():
    out = []
    for r in pl.library():
        pdf = ns.pdf_path(r.get("file"))
        if pdf and not embed.is_indexed(pdf):
            out.append((r, pdf))
    return out


def status():
    return {"enabled": enabled(), "local": local(), "running": _running}


def kick(delay=0, manual=False):
    """Start a pass in the background. Automatic passes need the setting on and the local model; a manual one (the tray's Index now) only needs a working embedding model."""
    global _running
    with _lock:
        if _running or (not manual and not (enabled() and local())):
            return False
        _running = True
    threading.Thread(target=_run, args=(delay,), daemon=True).start()
    return True


def _run(delay):
    global _running
    try:
        if delay:
            time.sleep(delay)
        todo = pending()
        if not todo:
            return
        job = act.start("index", "Indexing %d paper%s for Ask" % (len(todo), "" if len(todo) == 1 else "s"), total=len(todo))
        try:
            for i, (r, pdf) in enumerate(todo):
                if job.cancelled:
                    break
                job.update(msg=str(r.get("title") or "")[:90], done=i)

                def stop(a, b):
                    if job.cancelled:
                        raise ai.Cancelled("Cancelled.")
                try:
                    embed.index_paper(pdf, stop)
                except ai.Cancelled:
                    break
                except ai.AIError as e:        # a problem with the embedding model itself: stop, and say why
                    job.update(error=str(e)[:200], errors=job.errors + 1)
                    break
                except Exception as e:  # noqa: BLE001      (one unreadable PDF must not stop the others)
                    job.update(errors=job.errors + 1, error="%s: %s" % ((r.get("title") or "a paper")[:40], str(e)[:100]))
                job.update(done=i + 1)
                time.sleep(0.05)
            job.update(msg="")
            job.finish("cancelled" if job.cancelled else "error" if job.error and job.done < job.total else "done")
        except Exception as e:  # noqa: BLE001
            job.update(error=str(e)[:200]); job.finish("error")
    finally:
        _running = False
