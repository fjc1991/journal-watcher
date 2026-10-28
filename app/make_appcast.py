"""Writes appcast.json: what a running app asks for to learn that a new version exists (see scripts/updater.py).

    make_appcast.py --version 1.2.0 --zip "Journal-Watcher-1.2.0.zip" --url https://…/Journal-Watcher-1.2.0.zip --notes "one|two|three" --previous old-appcast.json --out appcast.json
"""
import argparse
import hashlib
import json
import os
from datetime import date


def main():
    a = argparse.ArgumentParser()
    a.add_argument("--version", required=True)
    a.add_argument("--zip", required=True)
    a.add_argument("--url", required=True)
    a.add_argument("--notes", default="", help="what is new, separated by |")
    a.add_argument("--notes-file", default="", help="one note per line")
    a.add_argument("--previous", default="", help="the appcast of the last release, so its notes become history")
    a.add_argument("--min-macos", default="11.0")
    a.add_argument("--out", required=True)
    o = a.parse_args()
    notes = [n.strip() for n in (open(o.notes_file, encoding="utf-8").read().splitlines() if o.notes_file else o.notes.split("|")) if n.strip()]
    h = hashlib.sha256()
    with open(o.zip, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    hist = []
    if o.previous and os.path.exists(o.previous):
        try:
            p = json.load(open(o.previous, encoding="utf-8"))
            if p.get("version") and p["version"] != o.version:
                hist = [{"version": p["version"], "released": p.get("released", ""), "notes": p.get("notes", [])}] + [x for x in p.get("history", []) if x.get("version") != o.version]
        except (OSError, ValueError):
            pass
    d = {"app": "Journal Watcher", "version": o.version, "released": date.today().isoformat(), "notes": notes, "url": o.url, "sha256": h.hexdigest(), "size": os.path.getsize(o.zip),
         "min_macos": o.min_macos, "history": hist[:10]}
    with open(o.out, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1, ensure_ascii=False)
    print("appcast %s: %s (%.0f MB, sha256 %s…)" % (o.version, o.out, d["size"] / 1048576, d["sha256"][:12]))


if __name__ == "__main__":
    main()
