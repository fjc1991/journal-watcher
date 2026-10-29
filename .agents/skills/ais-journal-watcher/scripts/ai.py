"""Provider-agnostic GenAI layer. Every AI feature calls chat()/stream() with a *feature name* ("ask", "explain", ...); which
provider and model answers is a setting in ai.json (features), not code, so Claude, OpenAI, Gemini, OpenRouter or a local
Ollama model can be swapped per feature.

    import ai
    r = ai.chat("ask", [{"role": "user", "content": "..."}], system="...")        # -> Result(.text .usage .cost .sources ...)
    for kind, v in ai.stream("explain", messages, system="...", cache=True):       # ("text", delta) ... ("done", Result)
        ...
    vecs = ai.embed(["a passage", ...])                 # float32 (n, d), L2-normalised; kind="query" for questions
    ai.config()  ai.save_config({...})  ai.usage_summary()  ai.usage_log()  ai.test("openai", "gpt-...")

A message's content is a string or a list of parts: {"type":"text","text"}, {"type":"image","media_type","data"(base64)},
{"type":"pdf","path"} (sent natively to Claude; for other providers the paper's extracted text is sent instead).
Options: web_search=True (the provider searches the web; sources come back in Result.sources), cache=True (identical
requests are answered from state/ai_cache/ and cost nothing; fresh=True skips the cache), override={"provider","model"}.

Two adapters: `anthropic` (the SDK, same call style as pipeline.py) and `openai_compat` (plain HTTPS with urllib, so no
extra dependency). Every call is appended to state/ai_usage.jsonl. Keys come from .env (never from ai.json). Settings that
guard spending and privacy: ai.json -> budget.monthly_usd, privacy.local_only.
"""
import base64
import hashlib
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import activity as act  # noqa: E402
import download as dl  # noqa: E402

BASE, STATE = dl.BASE, dl.STATE
CONFIG = os.path.join(BASE, "ai.json")
LEDGER = os.path.join(STATE, "ai_usage.jsonl")
MODELS = os.path.join(STATE, "models")       # local embedding models are cached here (survives reboots)
CACHE = os.path.join(STATE, "ai_cache")      # answers to identical requests (regenerable)
FULL_TEXT_LIMIT = 450_000                    # characters of a paper sent as text to providers without native PDF input

_lock = threading.Lock()
_clients = {}
_local = {}


class AIError(Exception):
    """A problem the user can act on (missing key, provider down...): shown as-is in the UI."""


class Cancelled(AIError):
    """The user pressed Cancel in the activity tray: the call stops between two pieces of the answer (what was already generated is still billed)."""


# ---------------------------------------------------------------- config

DEFAULT = {
    "providers": {"anthropic": {"type": "anthropic", "key_env": "ANTHROPIC_API_KEY", "models": ["claude-sonnet-5-5"]}},
    "features": {"ask": {"provider": "anthropic", "model": "claude-sonnet-5-5", "effort": "low"}},
    "embeddings": {"provider": "local", "model": "BAAI/bge-small-en-v1.5"},
    "prices": {},
    "privacy": {"local_only": False},
    "budget": {"monthly_usd": None},
    "screening": {"reuse_days": 90},      # an earlier screening verdict is believed for this many days (0 = always ask again): pipeline.reuse_days
}


def config_data():
    c = dl.load_json(CONFIG, None) or json.loads(json.dumps(DEFAULT))
    c.setdefault("privacy", {"local_only": False})
    c.setdefault("budget", {"monthly_usd": None})
    c.setdefault("screening", {"reuse_days": 90})
    return c


def _key(name):
    """API key from the environment or .env (re-read, so adding a key doesn't need a restart); '' if none."""
    if not name:
        return ""
    return os.environ.get(name) or dl.load_env().get(name, "")


def set_key(provider, key):
    """Save a provider's API key in the .env file of the data home (readable by you only); it is never sent back to the page.
    Returns the fresh config. Raises ValueError for an unknown provider or a key that cannot be one."""
    p = (config_data().get("providers") or {}).get(provider)
    name = (p or {}).get("key_env")
    if not name:
        raise ValueError("%s needs no key" % provider)
    key = str(key or "").strip()
    if not key or len(key) > 400 or re.search(r"[\s\"'\\]", key):
        raise ValueError("That doesn’t look like an API key (no spaces or quotes; paste just the key).")
    path = os.path.join(dl.ROOT, ".env")
    lines = open(path, encoding="utf-8").read().splitlines() if os.path.exists(path) else []
    lines = [l for l in lines if not re.match(r"\s*%s\s*=" % re.escape(name), l)] + ["%s=%s" % (name, key)]
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)
    os.environ[name] = key
    return config()


def is_local(p):
    host = urllib.parse.urlsplit(p.get("base_url", "")).hostname or ""
    return bool(p.get("local")) or host in ("localhost", "127.0.0.1", "::1")


def can_search_web(p):
    return p.get("type") == "anthropic" or bool(p.get("web_body"))


def feature_model(feature):
    """(provider, model) that answers a feature; features without their own setting use the Ask one."""
    c = config_data()
    f = c["features"].get(feature) or c["features"].get("ask") or {}
    return f.get("provider"), f.get("model")


def estimate(feature, in_chars, out_tokens, searches=0):
    """Rough USD for one call before it is made (about 3.3 characters per token, as measured on real papers); None when the model has no price."""
    _, model = feature_model(feature)
    if not model:
        return None
    return cost_of(model, {"in": int(in_chars / 3.3), "out": int(out_tokens)}, searches)


def config():
    """What the UI needs: providers (and whether a key is set), feature routing, embeddings, privacy, budget. Never any key."""
    c = config_data()
    provs = {n: {"type": p.get("type"), "base_url": p.get("base_url", ""), "models": p.get("models", []), "key_env": p.get("key_env", ""),
                 "configured": not p.get("key_env") or bool(_key(p["key_env"])), "local": is_local(p),
                 "web_search": can_search_web(p)} for n, p in c["providers"].items()}
    return {"providers": provs, "features": c["features"], "embeddings": c["embeddings"], "prices": c.get("prices", {}),
            "privacy": c["privacy"], "budget": c["budget"], "screening": c["screening"]}


def save_config(patch):
    """Merge into ai.json after validating: features, embeddings, privacy, budget, provider model lists, prices."""
    c = config_data()
    for name, f in (patch.get("features") or {}).items():
        if not re.fullmatch(r"[a-z_]{1,24}", name) or f.get("provider") not in c["providers"]:
            raise ValueError("unknown provider for %s" % name)
        model = str(f.get("model") or "").strip()
        if not model or len(model) > 120:
            raise ValueError("model name required")
        new = {**c["features"].get(name, {}), "provider": f["provider"], "model": model}
        if f.get("effort") in ("low", "medium", "high"):
            new["effort"] = f["effort"]
        c["features"][name] = new
    e = patch.get("embeddings")
    if e:
        if e.get("provider") != "local" and e.get("provider") not in c["providers"]:
            raise ValueError("unknown embedding provider")
        c["embeddings"] = {"provider": e["provider"], "model": str(e.get("model") or "").strip()[:120]}
    if "privacy" in patch:
        c["privacy"] = {"local_only": bool((patch["privacy"] or {}).get("local_only"))}
    if "budget" in patch:
        v = (patch["budget"] or {}).get("monthly_usd")
        c["budget"] = {"monthly_usd": None if v in (None, "") else max(0.0, float(v))}
    if "screening" in patch:
        try:
            days = int((patch["screening"] or {}).get("reuse_days", 90))
        except (TypeError, ValueError):
            raise ValueError("The number of days must be a whole number (0 = always ask again).")
        if not 0 <= days <= 3650:
            raise ValueError("The number of days must be between 0 and 3650.")
        c["screening"] = {"reuse_days": days}
    for pname, models in (patch.get("provider_models") or {}).items():
        if pname not in c["providers"] or not isinstance(models, list):
            raise ValueError("unknown provider %s" % pname)
        c["providers"][pname]["models"] = [str(m).strip()[:120] for m in models if str(m).strip()][:40]
    for model, pr in (patch.get("prices") or {}).items():
        if pr is None:
            c["prices"].pop(model, None)
        else:
            c["prices"][str(model)[:120]] = {k: max(0.0, float(pr.get(k) or 0)) for k in ("in", "out", "cache_write", "cache_read")}
    tmp = CONFIG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(c, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG)
    return config()


# ---------------------------------------------------------------- result + ledger

class Result:
    def __init__(self, text, usage, provider, model, cost=None, stop=None, sources=None, searches=0, cached=False):
        self.text, self.provider, self.model, self.stop = text, provider, model, stop
        self.usage = {"in": 0, "out": 0, "cache_write": 0, "cache_read": 0, **(usage or {})}
        self.sources, self.searches, self.cached = sources or [], searches, cached
        self.cost = cost if cost is not None else cost_of(model, self.usage, searches)


def cost_of(model, usage, searches=0):
    """USD from the price table in ai.json (per million tokens, plus "_web_search" per search), or None when the model has no price."""
    prices = config_data().get("prices", {})
    p = prices.get(model)
    if not p:
        return None
    return sum(usage.get(k, 0) * p.get(k, 0) for k in ("in", "out", "cache_write", "cache_read")) / 1e6 + searches * (prices.get("_web_search") or {}).get("per_search", 0.01)


def _sent(messages, system):
    """(characters, preview) of what is being sent, for the 'what was sent' log (images and PDFs are counted, not stored)."""
    n, last = len(system or ""), ""
    for m in messages:
        c = m.get("content")
        parts = [{"type": "text", "text": c}] if isinstance(c, str) else (c or [])
        text = " ".join(x.get("text", "") for x in parts if x.get("type") == "text")
        n += len(text) + sum(2000 for x in parts if x.get("type") in ("image", "pdf"))
        if m.get("role") == "user":
            last = text
    return n, re.sub(r"\s+", " ", last)[:180]


def _log(feature, r, sent=None, note=None):
    row = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "feature": feature, "provider": r.provider,
           "model": r.model, **r.usage, "cost": 0.0 if r.cached else r.cost}
    act.add_cost(row["cost"])           # the activity tray shows what the running job has cost so far
    if r.searches:
        row["searches"] = r.searches
    if r.cached:
        row["cached"] = True
    if sent:
        row["chars"], row["preview"] = sent
    with _lock:
        os.makedirs(STATE, exist_ok=True)
        with open(LEDGER, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _log_cancelled(feature, provider, f, sent, text):
    """A call the user cancelled was still billed for what it had read and written by then. The provider reports exact usage only at the end of an answer, so the
    ledger gets an estimate (about 3.3 characters per token) marked as such: better a slight over-count than money that vanishes from the totals."""
    try:
        usage = {"in": int((sent[0] if sent else 0) / 3.3), "out": int(len(text) / 3.3)}
        r = Result(text, usage, provider, f["model"])
        row = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "feature": feature, "provider": r.provider, "model": r.model, **r.usage, "cost": r.cost, "cancelled": True, "estimated": True}
        if sent:
            row["chars"], row["preview"] = sent
        act.add_cost(r.cost)
        with _lock:
            os.makedirs(STATE, exist_ok=True)
            with open(LEDGER, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001      (never let bookkeeping hide the cancel)
        pass


def _rows():
    if not os.path.exists(LEDGER):
        return
    with open(LEDGER, encoding="utf-8") as f:
        for line in f:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def month_spend():
    ym = datetime.now(timezone.utc).strftime("%Y-%m")
    return sum(r.get("cost") or 0 for r in _rows() if r.get("ts", "").startswith(ym))


def usage_summary():
    """{calls, cost, unpriced, cached, month:{cost,calls}, budget, warn, by_feature, by_provider, by_day} from the ledger
    (cost = known prices only)."""
    ym = datetime.now(timezone.utc).strftime("%Y-%m")
    out = {"calls": 0, "cost": 0.0, "unpriced": 0, "cached": 0, "month": {"cost": 0.0, "calls": 0}, "by_feature": {}, "by_provider": {}, "by_day": {}}
    for r in _rows():
        b = out["by_feature"].setdefault(r.get("feature", "?"), {"calls": 0, "cost": 0.0, "in": 0, "out": 0})
        pv = out["by_provider"].setdefault(r.get("provider", "?"), {"calls": 0, "cost": 0.0})
        b["calls"] += 1; b["in"] += r.get("in", 0); b["out"] += r.get("out", 0); pv["calls"] += 1
        out["calls"] += 1
        out["cached"] += 1 if r.get("cached") else 0
        c = r.get("cost")
        if c is None:
            out["unpriced"] += 1
        else:
            b["cost"] += c; pv["cost"] += c; out["cost"] += c
            day = out["by_day"].setdefault(r.get("ts", "")[:10], 0.0)
            out["by_day"][r.get("ts", "")[:10]] = day + c
        if r.get("ts", "").startswith(ym):
            out["month"]["calls"] += 1; out["month"]["cost"] += c or 0
    bud = config_data()["budget"].get("monthly_usd")
    out["budget"] = bud
    out["warn"] = bool(bud) and out["month"]["cost"] >= 0.8 * bud
    out["by_day"] = dict(sorted(out["by_day"].items())[-30:])
    return out


def usage_log(limit=40):
    """The most recent calls, newest first, with a short preview of what was sent."""
    rows = list(_rows())
    return rows[-limit:][::-1]


# ---------------------------------------------------------------- routing, privacy, budget

def _route(feature, override=None, web=False):
    c = config_data()
    f = dict(c["features"].get(feature) or c["features"].get("ask"))
    if override:
        f.update({k: v for k, v in override.items() if k in ("provider", "model", "effort") and v})
    p = c["providers"].get(f["provider"])
    if not p:
        raise AIError("No provider is set for “%s”. Choose one in Settings → AI." % feature)
    if c["privacy"].get("local_only") and not is_local(p):
        raise AIError("Local-only mode is on, so “%s” can’t use %s. Choose Ollama for this feature or turn local-only off in Settings → AI." % (feature, f["provider"]))
    if p.get("key_env") and not _key(p["key_env"]):
        raise AIError("%s isn’t set up: add %s=… to the .env file in the project folder (no restart needed), "
                      "or pick another provider in Settings → AI." % (f["provider"], p["key_env"]))
    bud = c["budget"].get("monthly_usd")
    if bud and month_spend() >= bud:
        raise AIError("This month’s AI budget ($%.2f) is used up. Raise it in Settings → Usage, or wait until next month." % bud)
    if web and not can_search_web(p):
        raise AIError("%s can’t search the web. Pick Claude, OpenRouter or OpenAI for this feature in Settings → AI." % f["provider"])
    return f["provider"], p, f


# ---------------------------------------------------------------- message parts (text / image / pdf)

def _parts(content):
    return [{"type": "text", "text": content}] if isinstance(content, str) else list(content or [])


def _pdf_text(path):
    import blocks as bl
    bs = [b for b in bl.extract(path) if b.get("kind", "body") != "ref"]
    text, n = [], 0
    for b in bs:
        n += len(b["text"])
        if n > FULL_TEXT_LIMIT:
            text.append("[…the rest of the paper is omitted for length…]")
            break
        text.append(b["text"])
    return "Full text of the paper (extracted from the PDF):\n\n" + "\n\n".join(text)


def _to_anthropic(messages):
    out = []
    for m in messages:
        if isinstance(m.get("content"), str):
            out.append(m); continue
        parts = []
        for x in _parts(m.get("content")):
            if x["type"] == "image":
                parts.append({"type": "image", "source": {"type": "base64", "media_type": x.get("media_type", "image/png"), "data": x["data"]}})
            elif x["type"] == "pdf":
                with open(x["path"], "rb") as fh:
                    parts.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": base64.standard_b64encode(fh.read()).decode()}})
            else:
                parts.append({"type": "text", "text": x.get("text", "")})
        out.append({**m, "content": parts})
    return out


def _to_compat(messages):
    out = []
    for m in messages:
        if isinstance(m.get("content"), str):
            out.append(m); continue
        parts = []
        for x in _parts(m.get("content")):
            if x["type"] == "image":
                parts.append({"type": "image_url", "image_url": {"url": "data:%s;base64,%s" % (x.get("media_type", "image/png"), x["data"])}})
            elif x["type"] == "pdf":
                parts.append({"type": "text", "text": _pdf_text(x["path"])})
            else:
                parts.append({"type": "text", "text": x.get("text", "")})
        out.append({**m, "content": parts})
    return out


# ---------------------------------------------------------------- the cache (identical request -> stored answer, no cost)

def _cache_key(name, f, messages, system, schema, max_tokens, web):
    def norm(m):
        parts = []
        for x in _parts(m.get("content")):
            if x["type"] == "image":
                parts.append(["image", hashlib.sha1(x["data"].encode()).hexdigest()])
            elif x["type"] == "pdf":
                try:
                    st = os.stat(x["path"]); parts.append(["pdf", x["path"], st.st_mtime_ns, st.st_size])
                except OSError:
                    parts.append(["pdf", x["path"]])
            else:
                parts.append(["text", x.get("text", "")])
        return [m.get("role"), parts]
    blob = json.dumps([name, f.get("model"), f.get("effort"), system, schema, max_tokens, web, [norm(m) for m in messages]], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def cache_clear():
    n = 0
    if os.path.isdir(CACHE):
        for fn in os.listdir(CACHE):
            os.remove(os.path.join(CACHE, fn)); n += 1
    return n


def cache_stats():
    fs = [os.path.join(CACHE, x) for x in os.listdir(CACHE)] if os.path.isdir(CACHE) else []
    return {"entries": len(fs), "bytes": sum(os.path.getsize(x) for x in fs)}


# ---------------------------------------------------------------- chat

def stream(feature, messages, system=None, schema=None, max_tokens=4000, web_search=False, cache=False, fresh=False, override=None, cache_if=None):
    """Yield ("text", delta) as the answer arrives, then ("done", Result). `schema` asks for JSON matching it.
    cache_if(text) -> bool lets the caller keep a bad answer (cut-off JSON, say) out of the cache, so asking again really asks again."""
    if act.is_cancelled():
        raise Cancelled("Cancelled.")
    name, p, f = _route(feature, override, web_search)
    sent = _sent(messages, system)
    key = _cache_key(name, f, messages, system, schema, max_tokens, web_search) if cache else None
    if key and not fresh:
        hit = dl.load_json(os.path.join(CACHE, key + ".json"), None) if os.path.exists(os.path.join(CACHE, key + ".json")) else None
        if hit:
            r = Result(hit["text"], hit.get("usage"), name, f["model"], cost=0.0, sources=hit.get("sources"), cached=True)
            yield "text", r.text
            _log(feature, r, sent)
            yield "done", r
            return
    run = _anthropic if p.get("type") == "anthropic" else _compat
    got = []
    for kind, v in run(name, p, f, messages, system, schema, max_tokens, web_search):
        if kind == "text":
            got.append(v)
        if act.is_cancelled() and kind != "done":      # stop between two pieces of the answer (a finished answer is kept and logged)
            _log_cancelled(feature, name, f, sent, "".join(got))
            raise Cancelled("Cancelled.")
        if kind == "done":
            _log(feature, v, sent)
            if key and (cache_if is None or cache_if(v.text)):
                os.makedirs(CACHE, exist_ok=True)
                tmp = os.path.join(CACHE, key + ".tmp")
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump({"text": v.text, "usage": v.usage, "sources": v.sources, "model": v.model, "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}, fh, ensure_ascii=False)
                os.replace(tmp, os.path.join(CACHE, key + ".json"))
        yield kind, v


def chat(feature, messages, system=None, schema=None, max_tokens=4000, **kw):
    res = None
    for kind, v in stream(feature, messages, system, schema, max_tokens, **kw):
        if kind == "done":
            res = v
    return res


def test(provider, model):
    """One tiny real call to check a provider, key and model work: {ok, ms, text|error}."""
    import time
    t = time.time()
    try:
        r = chat("ask", [{"role": "user", "content": "Reply with the single word OK."}], max_tokens=20, override={"provider": provider, "model": model})
        return {"ok": True, "ms": int((time.time() - t) * 1000), "text": r.text.strip()[:60], "cost": r.cost}
    except AIError as e:
        return {"ok": False, "error": str(e)}


def _anthropic(name, p, f, messages, system, schema, max_tokens, web=False):
    import anthropic
    if "a" not in _clients:
        _clients["a"] = anthropic.Anthropic(api_key=_key(p["key_env"]))
    oc = {"effort": f.get("effort", "low")}
    if schema:
        oc["format"] = {"type": "json_schema", "schema": schema}
    kw = {"model": f["model"], "max_tokens": max_tokens, "messages": _to_anthropic(messages), "output_config": oc}
    for k in ("betas", "fallbacks", "thinking"):       # Sonnet 5.5 settings, as in pipeline.py (no temperature / forced tool_choice)
        if p.get(k):
            kw[k] = p[k]
    if system:
        kw["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    if web:
        kw["tools"] = [{"type": "web_search_20260318", "name": "web_search", "max_uses": int(p.get("web_max_uses", 5))}]
    try:
        with _clients["a"].beta.messages.stream(**kw) as s:
            for t in s.text_stream:
                yield "text", t
            msg = s.get_final_message()
    except anthropic.APIStatusError as e:
        raise AIError("Anthropic: %s" % str(e)[:300])
    except anthropic.APIConnectionError as e:
        raise AIError("Can’t reach Anthropic (%s)." % str(e)[:120])
    if msg.stop_reason == "refusal":
        raise AIError("The model declined to answer this.")
    u = msg.usage
    sources, seen = [], set()
    for b in msg.content:
        if b.type == "web_search_tool_result" and isinstance(getattr(b, "content", None), list):
            for r in b.content:
                if getattr(r, "url", None) and r.url not in seen:
                    seen.add(r.url); sources.append({"url": r.url, "title": getattr(r, "title", "") or r.url, "date": getattr(r, "page_age", None)})
    searches = getattr(getattr(u, "server_tool_use", None), "web_search_requests", 0) or 0
    yield "done", Result("".join(b.text for b in msg.content if b.type == "text"),
                         {"in": u.input_tokens or 0, "out": u.output_tokens or 0,
                          "cache_write": u.cache_creation_input_tokens or 0, "cache_read": u.cache_read_input_tokens or 0},
                         name, f["model"], stop=msg.stop_reason, sources=sources, searches=searches)


def _headers(p):
    h = {"Content-Type": "application/json"}
    key = _key(p.get("key_env"))
    if key:
        h["Authorization"] = "Bearer " + key
    if "openrouter" in p.get("base_url", ""):
        h["X-Title"] = "Journal Watcher"
    return h


def _http_error(name, e, raw=None):
    raw = raw if raw is not None else e.read().decode("utf-8", "replace")
    try:
        body = json.loads(raw)
        msg = body["error"].get("message") if isinstance(body.get("error"), dict) else body.get("error") or body
    except Exception:
        msg = raw[:200] or e.reason
    return AIError("%s: %s (HTTP %s)" % (name, str(msg)[:300], e.code))


def _compat(name, p, f, messages, system, schema, max_tokens, web=False):
    body = {"model": f["model"], "stream": True, "stream_options": {"include_usage": True},
            "messages": ([{"role": "system", "content": system}] if system else []) + _to_compat(messages),
            p.get("max_tokens_param", "max_tokens"): max_tokens}
    if schema:
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": "result", "schema": schema, "strict": True}}
    if web:
        body.update(p["web_body"])           # e.g. OpenRouter: {"plugins": [{"id": "web"}]}
    url = p["base_url"].rstrip("/") + "/chat/completions"
    resp = None
    for attempt in range(2):
        req = urllib.request.Request(url, json.dumps(body).encode(), _headers(p))
        try:
            resp = urllib.request.urlopen(req, timeout=300)
            break
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            if e.code == 400 and attempt == 0 and "stream_options" in body and "stream_options" in raw:
                body.pop("stream_options")      # a server without usage-in-stream: ask again without it
                continue
            raise _http_error(name, e, raw)
        except urllib.error.URLError as e:
            raise AIError("Can’t reach %s at %s (%s)%s" % (name, p["base_url"], e.reason, " — is Ollama running?" if name == "ollama" else ""))
    text, usage, sources, seen = "", {}, [], set()
    with resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                ev = json.loads(data)
            except ValueError:
                continue
            if isinstance(ev, dict) and ev.get("error"):
                raise AIError("%s: %s" % (name, str(ev["error"])[:300]))
            for ch in ev.get("choices") or []:
                d = ch.get("delta") or {}
                if d.get("content"):
                    text += d["content"]
                    yield "text", d["content"]
                for a in d.get("annotations") or []:        # OpenRouter / OpenAI web citations
                    u = a.get("url_citation") or {}
                    if u.get("url") and u["url"] not in seen:
                        seen.add(u["url"]); sources.append({"url": u["url"], "title": u.get("title") or u["url"], "date": None})
            if ev.get("usage"):
                usage = ev["usage"]
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
    yield "done", Result(text, {"in": max(0, usage.get("prompt_tokens", 0) - cached), "out": usage.get("completion_tokens", 0),
                                "cache_read": cached}, name, f["model"], cost=usage.get("cost"), sources=sources)


# ---------------------------------------------------------------- embeddings

def embed(texts, kind="passage"):
    """float32 array (n, d) of L2-normalised vectors. kind="query" for a question, "passage" for document text.
    Default is a local ONNX model (fastembed): free, offline; first use downloads it into state/models/."""
    import numpy as np
    cfg = config_data()
    c = cfg["embeddings"]
    if c["provider"] == "local":
        from fastembed import TextEmbedding
        if c["model"] not in _local:
            os.makedirs(MODELS, exist_ok=True)
            _local[c["model"]] = TextEmbedding(model_name=c["model"], cache_dir=MODELS)
        m = _local[c["model"]]
        arr = np.array(list(m.query_embed(texts) if kind == "query" else m.passage_embed(texts)), dtype="float32")
    else:
        p = cfg["providers"][c["provider"]]
        if cfg["privacy"].get("local_only") and not is_local(p):
            raise AIError("Local-only mode is on: choose the local embedding model in Settings → AI.")
        if p.get("key_env") and not _key(p["key_env"]):
            raise AIError("%s isn’t set up: add %s=… to .env." % (c["provider"], p["key_env"]))
        rows = []
        for i in range(0, len(texts), 64):
            req = urllib.request.Request(p["base_url"].rstrip("/") + "/embeddings",
                                         json.dumps({"model": c["model"], "input": texts[i:i + 64]}).encode(), _headers(p))
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    rows += [d["embedding"] for d in sorted(json.load(r)["data"], key=lambda d: d["index"])]
            except urllib.error.HTTPError as e:
                raise _http_error(c["provider"], e)
            except urllib.error.URLError as e:
                raise AIError("Can’t reach %s (%s)." % (c["provider"], e.reason))
        arr = np.array(rows, dtype="float32")
    arr /= np.maximum(np.linalg.norm(arr, axis=1, keepdims=True), 1e-9)
    return arr
