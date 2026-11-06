"""What a model costs, and where that number comes from (USD per million tokens).

    prices.lookup(provider, model)  -> {"in", "out", "cache_write", "cache_read", "web_search" (per search, optional), "source", "checked"} or None
    prices.refresh(force=False)     read OpenRouter's public price list (at most once a day); returns how many models it has
    prices.refresh_soon()           the same in the background, when an OpenRouter model is in use and the saved list is old
    prices.name(provider, model)    the name a person reads: "Claude Sonnet 5.5", "scaledown/summarize"

Where a price comes from, in this order:
  1. "yours":       a price typed in Settings → AI → Prices (ai.json "prices"), for that model name; it always wins
  2. "openrouter":  for a provider whose address is openrouter.ai, OpenRouter's own list (https://openrouter.ai/api/v1/models, public, no key; nothing about you is sent),
                    kept in this computer's lookup folder and read again when it is a day old
  3. "built-in":    BUILTIN below, for the providers that publish no list a program can read; each has the date it was checked (BUILTIN_CHECKED)
  4. "free":        a model on this computer that needs no key (Ollama, LM Studio) costs nothing; a local address that needs a key may be a proxy to a paid service, so not
  5. None:          unknown; callers say so rather than counting it as $0
"""
import json
import os
import re
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import download as dl  # noqa: E402

# Anthropic's first-party prices (USD per million tokens), from Anthropic's model table dated BUILTIN_CHECKED. Cache writes are 1.25 x the input price (5-minute cache);
# cache reads as published where the table states them (Fable 5.1, Opus 5.5, Sonnet 5.5), else 0.1 x input. Claude Haiku 5.5: the price for prompts up to 100K tokens.
BUILTIN_CHECKED = "2026-10-06"
_A = lambda i, o, cr=None: {"in": i, "out": o, "cache_write": round(i * 1.25, 4), "cache_read": cr if cr is not None else round(i * 0.1, 4)}  # noqa: E731
BUILTIN = {
    "anthropic": {
        "claude-fable-5-1": _A(10.0, 50.0, 0.25), "claude-fable-5": _A(10.0, 50.0),
        "claude-opus-5-5": _A(4.0, 20.0, 0.20), "claude-opus-5": _A(5.0, 25.0), "claude-opus-4-8": _A(5.0, 25.0), "claude-opus-4-7": _A(5.0, 25.0), "claude-opus-4-6": _A(5.0, 25.0),
        "claude-sonnet-5-5": _A(2.0, 10.0, 0.20), "claude-sonnet-5": _A(2.0, 10.0), "claude-sonnet-4-6": _A(3.0, 15.0),
        "claude-haiku-5-5": _A(0.10, 0.50), "claude-haiku-4-5": _A(1.0, 5.0),
    },
}
STALE_DAYS = 120                  # a built-in price older than this is shown as "possibly out of date"
OR_MAX_AGE = 86400                # read OpenRouter's list again after a day
OR_URL = os.environ.get("JW_OPENROUTER_MODELS_URL", "https://openrouter.ai/api/v1/models")
CACHE = os.path.join(dl.LOCAL, "openrouter_prices.json")
_lock = threading.Lock()
_mem = {"at": 0.0, "data": None}
_busy = {"on": False}


def _cfg():
    import ai
    return ai.config_data()


def _provider(cfg, provider):
    return (cfg.get("providers") or {}).get(provider) or {}


def is_openrouter(p):
    return "openrouter.ai" in str((p or {}).get("base_url") or "")


def is_local(p):
    return bool(re.match(r"https?://(localhost|127\.|0\.0\.0\.0|\[::1\])", str((p or {}).get("base_url") or "")))


def _load():
    """The saved OpenRouter list {"fetched", "models": {id: prices}} (read from disk at most every 10 s)."""
    if _mem["data"] is not None and time.time() - _mem["at"] < 10:
        return _mem["data"]
    try:
        with open(CACHE, encoding="utf-8") as f:
            d = json.load(f)
        d = d if isinstance(d, dict) and isinstance(d.get("models"), dict) else {"models": {}}
    except (OSError, ValueError):
        d = {"models": {}}
    _mem.update(at=time.time(), data=d)
    return d


def _per_million(v):
    try:
        return round(float(v) * 1e6, 6)
    except (TypeError, ValueError):
        return None


def parse_openrouter(payload):
    """{model id: {"in", "out", "cache_write", "cache_read", "web_search"}} from the JSON of OpenRouter's /api/v1/models (prices there are USD per token, as text)."""
    out = {}
    for m in (payload or {}).get("data") or []:
        pr, mid = m.get("pricing") or {}, m.get("id")
        if not isinstance(mid, str) or not isinstance(pr, dict):
            continue
        i, o = _per_million(pr.get("prompt")), _per_million(pr.get("completion"))
        if i is None or o is None or i < 0 or o < 0:                # (a negative price marks a router whose price depends on where it sends you)
            continue
        e = {"in": i, "out": o}
        cw, cr = _per_million(pr.get("input_cache_write")), _per_million(pr.get("input_cache_read"))
        e["cache_write"] = cw if cw is not None else i
        e["cache_read"] = cr if cr is not None else i
        try:
            if pr.get("web_search") is not None:
                e["web_search"] = float(pr["web_search"])
        except (TypeError, ValueError):
            pass
        out[mid] = e
    return out


def _ua():
    try:
        import updater
        return updater.user_agent()
    except Exception:  # noqa: BLE001
        return "JournalWatcher/dev"


def refresh(force=False):
    """Read OpenRouter's list if the saved one is older than a day (or force). Returns the number of models known afterwards. Never raises: a failed read keeps the old list."""
    with _lock:
        d = _load()
        age = time.time() - float(d.get("fetched_ts") or 0)
        if not force and d.get("models") and age < OR_MAX_AGE:
            return len(d["models"])
        try:
            req = urllib.request.Request(OR_URL, headers={"User-Agent": _ua(), "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                models = parse_openrouter(json.load(r))
        except Exception:  # noqa: BLE001      (offline, a changed format: keep what we had)
            return len(d.get("models") or {})
        if not models:
            return len(d.get("models") or {})
        new = {"fetched": datetime.now(timezone.utc).isoformat(timespec="seconds"), "fetched_ts": time.time(), "models": models}
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        tmp = dl.tmp_path(CACHE)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(new, f)
        os.replace(tmp, CACHE)
        _mem.update(at=time.time(), data=new)
        return len(models)


def uses_openrouter(cfg=None):
    cfg = cfg or _cfg()
    return any(is_openrouter(_provider(cfg, f.get("provider"))) for f in (cfg.get("features") or {}).values() if isinstance(f, dict))


def refresh_soon(force=False):
    """refresh() in a background thread, only when an OpenRouter model is in use (or force) and no read is already under way."""
    if os.environ.get("JW_OFFLINE") and not os.environ.get("JW_OPENROUTER_MODELS_URL"):
        return False
    try:
        if not force and not uses_openrouter():
            return False
    except Exception:  # noqa: BLE001
        return False
    with _lock:
        if _busy["on"]:
            return False
        _busy["on"] = True

    def go():
        try:
            refresh(force)
        finally:
            _busy["on"] = False
    threading.Thread(target=go, daemon=True).start()
    return True


def lookup(provider, model, cfg=None, yours=True):
    """The price of `model` at `provider` (see the module's docstring), or None. yours=False: what the app would use without a price you typed (Settings shows it)."""
    if not model:
        return None
    cfg = cfg or _cfg()
    typed = (cfg.get("prices") or {}).get(model) if yours else None
    if isinstance(typed, dict) and any(float(typed.get(k) or 0) for k in ("in", "out", "cache_write", "cache_read")):
        return {**{k: float(typed.get(k) or 0) for k in ("in", "out", "cache_write", "cache_read")}, "source": "yours", "checked": ""}
    p = _provider(cfg, provider)
    if is_openrouter(p):
        d = _load()
        e = (d.get("models") or {}).get(model)
        if e:
            return {**e, "source": "openrouter", "checked": str(d.get("fetched") or "")[:10]}
        refresh_soon()                    # (not in the list yet, or no list: the next figures will have it)
        return None
    kind = "anthropic" if (p.get("type") == "anthropic" or provider == "anthropic") else provider
    b = (BUILTIN.get(kind) or {}).get(model)
    if b:
        return {**b, "source": "built-in", "checked": BUILTIN_CHECKED}
    if is_local(p) and not p.get("key_env"):          # (Ollama, LM Studio: on this computer and no key; a local proxy that needs a key may forward to a paid service)
        return {"in": 0.0, "out": 0.0, "cache_write": 0.0, "cache_read": 0.0, "source": "free", "checked": ""}
    return None


def stale(entry, today=None):
    """True for a built-in price older than STALE_DAYS, or an OpenRouter price read more than two days ago."""
    if not entry or not entry.get("checked"):
        return False
    try:
        d = datetime.strptime(entry["checked"][:10], "%Y-%m-%d").date()
    except ValueError:
        return False
    days = ((today or datetime.now(timezone.utc).date()) - d).days
    return days > (STALE_DAYS if entry.get("source") == "built-in" else 2)


def name(provider, model):
    """The name a person reads. Claude models get their product name ("claude-sonnet-5-5" -> "Claude Sonnet 5.5"); any other model keeps its own id."""
    m = re.fullmatch(r"(?:anthropic/)?claude-([a-z]+)-(\d+)(?:[-.](\d+))?", str(model or ""))
    if m:
        return "Claude %s %s%s" % (m.group(1).capitalize(), m.group(2), "." + m.group(3) if m.group(3) else "")
    return str(model or provider or "the AI")
