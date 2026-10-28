"""What the AI is doing right now: a small registry of running and recent jobs, for the dashboard's activity tray.

    job = activity.start("card", "Card · Some paper", scope={"file": "Papers/…pdf", "tab": "card"})   # registers it, makes it the current job of this thread
    job.observe(message)       # feed it the dicts a streaming endpoint yields (status / progress / start / paper / done / error): progress and messages follow
    activity.is_cancelled()    # asked from any thread the job's work runs in: has the user pressed Cancel?
    activity.add_cost(c)       # called by ai.py for every real call, so a job knows what it has cost
    run = activity.wrap(fn)    # fn that runs in the job of whoever wrapped it (use it for work handed to threads / executors)
    activity.cancel(job_id); activity.snapshot()

Jobs live in memory only (the last 25 finished ones are kept for the tray). Cancelling is cooperative: ai.stream checks it between the pieces of an answer
and raises ai.Cancelled, an indexer checks between batches; whatever was already paid for stays in the ledger (state/ai_usage.jsonl).
"""
import contextvars
import threading
import time
import uuid
from collections import OrderedDict

_cur = contextvars.ContextVar("jw_activity_job", default=None)
_lock = threading.RLock()
_jobs = OrderedDict()
_emit = lambda event, data: None  # noqa: E731  (the server sets this to its event hub)
KEEP = 25


class Job:
    def __init__(self, kind, label, scope=None, total=None):
        self.id = time.strftime("%H%M%S") + "-" + uuid.uuid4().hex[:5]
        self.kind, self.label, self.scope = kind, label, scope or {}
        self.started, self.ended = time.time(), None
        self.status = "running"             # running | done | error | cancelled
        self.msg, self.done, self.total = "", 0, total
        self.chars, self.cost, self.error, self.errors, self.completed = 0, 0.0, "", 0, False
        self.cancel_ev = threading.Event()
        self._last = 0.0

    @property
    def cancelled(self):
        return self.cancel_ev.is_set()

    def to_dict(self):
        return {"id": self.id, "kind": self.kind, "label": self.label, "scope": self.scope, "status": self.status, "msg": self.msg, "done": self.done, "total": self.total,
                "chars": self.chars, "cost": round(self.cost, 5), "error": self.error, "errors": self.errors, "started": self.started, "ended": self.ended,
                "cancelling": self.cancelled and self.status == "running"}

    def push(self, force=False):
        now = time.time()
        if force or now - self._last > 0.25:
            self._last = now
            _emit("activity", self.to_dict())

    def update(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        self.push()

    def add_cost(self, c):
        self.cost += c or 0
        self.push()

    def observe(self, m):
        """Follow a message of a streaming endpoint (the same dicts the browser reads)."""
        t = m.get("type")
        if t == "status":
            self.msg = str(m.get("msg") or "")[:140]
        elif t == "progress":
            self.chars = m.get("chars") or self.chars
        elif t == "start":
            self.total, self.done = m.get("total"), 0
            self.msg = "%s to do" % m.get("total") if m.get("total") is not None else self.msg
        elif t == "paper":
            self.done, self.total = m.get("done", self.done + 1), m.get("total", self.total)
            self.msg = str(m.get("title") or "")[:90]
        elif t == "done":
            self.completed = True
        elif t == "error":
            self.errors += 1
            self.error = str(m.get("error") or "")[:200]
        self.push()

    def finish(self, status=None):
        if self.ended:
            return
        self.ended = time.time()
        self.status = status or ("done" if self.completed else "cancelled" if self.cancelled else "error" if (self.error and not self.done and self.errors) else "done")
        if self.status == "done":
            self.msg = self.msg if self.kind == "index" else ""
        if _cur.get() is self:
            _cur.set(None)
        self.push(force=True)
        with _lock:
            old = [k for k, j in _jobs.items() if j.ended]
            for k in old[:-KEEP]:
                _jobs.pop(k, None)


def start(kind, label, scope=None, total=None):
    j = Job(kind, label, scope, total)
    with _lock:
        _jobs[j.id] = j
    _cur.set(j)
    j.push(force=True)
    return j


def current():
    return _cur.get()


def is_cancelled():
    j = _cur.get()
    return bool(j and j.cancelled)


def add_cost(c):
    j = _cur.get()
    if j:
        j.add_cost(c)


def wrap(fn):
    """fn, run later in the job that is current now: a thread or an executor does not inherit it on its own."""
    ctx = contextvars.copy_context()
    return lambda *a, **k: ctx.run(fn, *a, **k)


def bind(job):
    """Make `job` the current job of this thread (a background thread that started its own job)."""
    _cur.set(job)


def cancel(job_id):
    with _lock:
        j = _jobs.get(job_id)
    if not j or j.ended:
        return False
    j.cancel_ev.set()
    j.push(force=True)
    return True


def snapshot():
    with _lock:
        js = list(_jobs.values())
    running = sorted((j for j in js if not j.ended), key=lambda j: j.started)
    recent = sorted((j for j in js if j.ended), key=lambda j: -j.ended)
    return [j.to_dict() for j in running + recent]


def set_emitter(fn):
    global _emit
    _emit = fn
