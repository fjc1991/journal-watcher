#!/bin/bash
# Double-click to open the Journal Watcher dashboard from a checkout of the source (the Mac app needs none of this).
# Closing this Terminal window stops the dashboard.
cd "$(dirname "$0")" || exit 1

# Where your papers and settings live (not in the checkout): JW_HOME, else the folder the Mac app uses, else ~/Documents/Journal Watcher
APPCFG="$HOME/Library/Application Support/Journal Watcher/app.json"
if [ -z "${JW_HOME:-}" ] && [ -f "$APPCFG" ]; then
  JW_HOME="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("home",""))' "$APPCFG" 2>/dev/null)"
fi
export JW_HOME="${JW_HOME:-$HOME/Documents/Journal Watcher}"
mkdir -p "$JW_HOME"
echo "Your data folder: $JW_HOME"

if [ ! -x .venv/bin/python ]; then
  echo "First run: setting up (this takes a minute)…"
  python3 -m venv .venv || { echo "Python 3 is required."; read -r; exit 1; }
  .venv/bin/pip install -q -r requirements.txt
  .venv/bin/python -m playwright install chromium
fi
if ! .venv/bin/python -c "import anthropic, playwright, pymupdf, fastembed, numpy, docx" 2>/dev/null; then
  .venv/bin/pip install -q -r requirements.txt
fi

echo "Starting Journal Watcher…"
exec .venv/bin/python dashboard/server.py
