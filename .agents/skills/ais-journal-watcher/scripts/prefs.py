"""Your prompt library and reader preferences, each a small JSON file next to ai.json (config, not library data):

    prompts.json   one-click prompts for selected text / figures, and the summary templates
    settings.json  colour legend (what each highlight colour means), default summary template, export folder

    prefs.get_prompts() / save_prompts({"prompts": [...], "summaries": [...]})
    prefs.fill("Explain {{text}}", text="...")        # {{placeholders}}; unknown ones are left empty
    prefs.get_settings() / save_settings({"legend": {...}})
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import download as dl  # noqa: E402

PROMPTS = os.path.join(dl.BASE, "prompts.json")
SETTINGS = os.path.join(dl.BASE, "settings.json")
COLORS = ("yellow", "green", "blue", "pink", "orange", "purple", "red", "gray")
SCOPES = ("selection", "area", "paper")
DEFAULT_SETTINGS = {"legend": {}, "summary_template": "is_brief", "export_dir": "", "wpm": 230, "background_index": True}


def _write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def get_prompts():
    d = dl.load_json(PROMPTS, {}) if os.path.exists(PROMPTS) else {}
    return {"prompts": d.get("prompts", []), "summaries": d.get("summaries", [])}


def _clean(items, kind):
    out, seen = [], set()
    for x in items:
        if not isinstance(x, dict):
            raise ValueError("bad %s entry" % kind)
        i = re.sub(r"[^a-z0-9_-]+", "-", str(x.get("id") or x.get("name") or "").lower()).strip("-")[:30]
        name, prompt = str(x.get("name") or "").strip()[:60], str(x.get("prompt") or "").strip()
        if not i or not name or not prompt:
            raise ValueError("every %s needs a name and a prompt" % kind)
        if len(prompt) > 8000:
            raise ValueError("“%s” is too long (8000 characters at most)" % name)
        n, base = 2, i
        while i in seen:
            i = "%s-%d" % (base, n); n += 1
        seen.add(i)
        e = {"id": i, "name": name, "prompt": prompt}
        if kind == "prompt":
            e["scope"] = x.get("scope") if x.get("scope") in SCOPES else "selection"
            e["pinned"] = bool(x.get("pinned"))
        out.append(e)
    return out


def save_prompts(d):
    cur = dl.load_json(PROMPTS, {}) if os.path.exists(PROMPTS) else {}
    if "prompts" in d:
        cur["prompts"] = _clean(d["prompts"], "prompt")
    if "summaries" in d:
        cur["summaries"] = _clean(d["summaries"], "summary template")
        if not cur["summaries"]:
            raise ValueError("keep at least one summary template")
    _write(PROMPTS, cur)
    return get_prompts()


def fill(template, **vars):
    return re.sub(r"\{\{(\w+)\}\}", lambda m: str(vars.get(m.group(1), "") or ""), template)


def summary_prompt(template_id, **vars):
    ts = get_prompts()["summaries"]
    t = next((x for x in ts if x["id"] == template_id), None) or (ts[0] if ts else None)
    return fill(t["prompt"], **vars) if t else None


def get_settings():
    d = dl.load_json(SETTINGS, {}) if os.path.exists(SETTINGS) else {}
    return {**DEFAULT_SETTINGS, **{k: v for k, v in d.items() if k in DEFAULT_SETTINGS}}


def save_settings(patch):
    cur = get_settings()
    if "legend" in patch:
        cur["legend"] = {k: str(v).strip()[:40] for k, v in (patch["legend"] or {}).items() if k in COLORS and str(v).strip()}
    if "summary_template" in patch:
        if patch["summary_template"] not in [s["id"] for s in get_prompts()["summaries"]]:
            raise ValueError("unknown summary template")
        cur["summary_template"] = patch["summary_template"]
    if "export_dir" in patch:
        cur["export_dir"] = str(patch["export_dir"] or "").strip()[:300]
    if "wpm" in patch:
        cur["wpm"] = max(80, min(600, int(patch["wpm"])))
    if "background_index" in patch:
        cur["background_index"] = bool(patch["background_index"])
    _write(SETTINGS, cur)
    return cur
