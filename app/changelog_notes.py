"""Prints the bullet points under `## <version>` in CHANGELOG.md, one per line (for make_appcast.py --notes-file).   changelog_notes.py 1.2.0"""
import os
import re
import sys

version = sys.argv[1].lstrip("v")
text = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "CHANGELOG.md"), encoding="utf-8").read()
m = re.search(r"^##\s+v?%s\b.*?\n(.*?)(?=^##\s|\Z)" % re.escape(version), text, re.S | re.M)
notes = [re.sub(r"^[-*]\s+", "", l).strip() for l in (m.group(1).splitlines() if m else []) if l.strip().startswith(("-", "*"))]
print("\n".join(notes or ["Version %s." % version]))
