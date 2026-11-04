"""movehome.copy_into: destination wins, the library list is joined, per-computer items and the lock stay behind, nothing in the source changes.
    python .agents/skills/ais-journal-watcher/scripts/test_movehome.py
"""
import csv
import hashlib
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
T = tempfile.mkdtemp(prefix="jw-movehome-")
os.environ["JW_HOME"] = os.path.join(T, "unused-home")        # paths.py reads it when first imported; the copy must not depend on it
os.environ["JW_LOCAL"] = os.path.join(T, "unused-local")
import movehome  # noqa: E402
import paths  # noqa: E402

fails = []
def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (("  " + str(detail)) if detail and not ok else ""))
    if not ok:
        fails.append(name)

def put(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)

def index(root, rows):
    put(root, "Papers/_index.csv", "")
    with open(os.path.join(root, "Papers/_index.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, ["date_checked", "journal", "abbr", "title", "authors", "doi", "url", "matched_topics", "score", "file", "status", "note"])
        w.writeheader(); w.writerows(rows)

def tree(root):
    out = {}
    for d, _, fs in os.walk(root):
        for f in fs:
            p = os.path.join(d, f)
            out[os.path.relpath(p, root)] = ("link:" + os.readlink(p)) if os.path.islink(p) else hashlib.sha1(open(p, "rb").read()).hexdigest()
    return out

SRC, DST = os.path.join(T, "old"), os.path.join(T, "Dropbox", "JW")
skill = ".agents/skills/ais-journal-watcher"
# the old folder: two papers, settings, keys, shared state, and the per-computer items an older version kept inside state/
put(SRC, "Papers/2025-01/MISQ/a.pdf", "PDF A")
put(SRC, "Papers/2025-01/MISQ/a.md", "summary of A")
put(SRC, "Papers/2025-02/ISR/b.pdf", "PDF B")
put(SRC, "Papers/notes.json", '{"from": "old"}')
put(SRC, ".env", "ANTHROPIC_API_KEY=old")
put(SRC, skill + "/ai.json", '{"from": "old"}')
put(SRC, skill + "/journals.json", '{"from": "old"}')
put(SRC, skill + "/state/seen.json", '{"shared": true}')
put(SRC, skill + "/state/runs/2026-W40_weekly.json", "{}")
put(SRC, skill + "/state/server.lock", '{"pid": 1}')
put(SRC, skill + "/state/.DS_Store", "x")
put(SRC, skill + "/devices/" + "a" * 32 + ".json", '{"id": "%s", "name": "This Mac"}' % ("a" * 32))      # this computer's "I am here" file, written under its old id
for name in paths.LOCAL_ITEMS:
    if name.endswith(".json"):
        put(SRC, skill + "/state/" + name, "{}")
    else:
        put(SRC, skill + "/state/" + name + "/x", "x")
index(SRC, [{"date_checked": "2025-01-10", "title": "Paper A", "doi": "10.1/a", "file": "Papers/2025-01/MISQ/a.pdf", "status": "ok"},
            {"date_checked": "2025-02-10", "title": "Paper B", "doi": "10.1/b", "file": "Papers/2025-02/ISR/b.pdf", "status": "ok"},
            {"date_checked": "2025-03-01", "title": "Paper C failed here", "doi": "10.1/c", "file": "", "status": "failed"}])
# the destination: synced from another computer, already has paper C (downloaded there), its own settings, and a newer B row without the file
put(DST, "Papers/2025-03/JAIS/c.pdf", "PDF C")
put(DST, ".env", "ANTHROPIC_API_KEY=dropbox")
put(DST, skill + "/ai.json", '{"from": "dropbox"}')
index(DST, [{"date_checked": "2025-03-05", "title": "Paper C", "doi": "10.1/c", "file": "Papers/2025-03/JAIS/c.pdf", "status": "ok"},
            {"date_checked": "2025-04-01", "title": "Paper B", "doi": "10.1/b", "file": "", "status": "failed"}])

before_src = tree(SRC)
check("looks_like_home: data folder", movehome.looks_like_home(SRC))
check("looks_like_home: empty folder", not movehome.looks_like_home(os.path.join(T, "Dropbox")))
check("check: same folder refused", movehome.check(SRC, SRC) is not None)
check("check: destination inside source refused", movehome.check(SRC, os.path.join(SRC, "Papers")) is not None)
check("check: source inside destination refused", movehome.check(SRC, T) is not None)
check("check: fine pair", movehome.check(SRC, DST) is None)
p = movehome.plan(SRC)
check("plan counts pdfs", p["pdfs"] == 2, p)
check("plan leaves out local items and the lock", set(os.path.basename(s) for s in p["skipped"]) == set(paths.LOCAL_ITEMS) | {"server.lock", "devices"}, p["skipped"])

seen = []
res = movehome.copy_into(SRC, DST, say=lambda d, t, rel: seen.append((d, t)))
print(res)
print(movehome.describe(res, SRC))
check("no errors", not res["errors"], res["errors"])
check("source untouched", tree(SRC) == before_src)
check("papers copied", open(os.path.join(DST, "Papers/2025-01/MISQ/a.pdf")).read() == "PDF A" and os.path.exists(os.path.join(DST, "Papers/2025-02/ISR/b.pdf")))
check("summary copied", os.path.exists(os.path.join(DST, "Papers/2025-01/MISQ/a.md")))
check("destination's .env kept", open(os.path.join(DST, ".env")).read() == "ANTHROPIC_API_KEY=dropbox")
check("destination's ai.json kept", "dropbox" in open(os.path.join(DST, skill + "/ai.json")).read())
check("missing settings file copied", os.path.exists(os.path.join(DST, skill + "/journals.json")))
check("shared state copied", os.path.exists(os.path.join(DST, skill + "/state/seen.json")) and os.path.exists(os.path.join(DST, skill + "/state/runs/2026-W40_weekly.json")))
check("lock not copied", not os.path.exists(os.path.join(DST, skill + "/state/server.lock")))
check(".DS_Store not copied", not os.path.exists(os.path.join(DST, skill + "/state/.DS_Store")))
check("the computers' 'I am here' files not copied", not os.path.lexists(os.path.join(DST, skill, "devices")))
check("local items not copied", not any(os.path.lexists(os.path.join(DST, skill, "state", n)) for n in paths.LOCAL_ITEMS))
check("kept count (.env and ai.json were already there)", res["kept"] == 2, res["kept"])
check("copied count", res["copied"] == 3 + 1 + 1 + 2, res["copied"])        # a.pdf a.md b.pdf notes.json | journals.json | seen.json runs
with open(os.path.join(DST, "Papers/_index.csv"), newline="") as f:
    rows = {r["doi"]: r for r in csv.DictReader(f)}
check("index: every paper of either list", set(rows) == {"10.1/a", "10.1/b", "10.1/c"}, set(rows))
check("index: C keeps the destination's downloaded row", rows["10.1/c"]["status"] == "ok" and rows["10.1/c"]["file"].endswith("c.pdf"))
check("index: B keeps the row whose PDF is now in the destination", rows["10.1/b"]["status"] == "ok" and rows["10.1/b"]["file"].endswith("b.pdf"), rows["10.1/b"])
check("index: order starts with the destination's rows", list(rows)[0] == "10.1/c")
check("index result", res["index"] == {"papers": 3, "added": 1}, res["index"])
check("progress reported to the end", seen and seen[-1][0] == seen[-1][1])
# a second run changes nothing more (everything is already there)
again = movehome.copy_into(SRC, DST)
check("second copy copies nothing", again["copied"] == 0 and not again["errors"], again)
check("second copy keeps the index", again["index"] == {"papers": 3, "added": 0}, again["index"])
# a first-start copy into an empty folder: the list is simply copied
EMPTY = os.path.join(T, "fresh")
r2 = movehome.copy_into(SRC, EMPTY)
check("empty destination: index copied whole", r2["index"] == {"papers": 3, "added": 3} and os.path.exists(os.path.join(EMPTY, ".env")), r2)

# ---- this computer's sign-in browser and model come along to the new folder's per-computer folder (carry_local)
LIB = os.path.join(T, "Library", "local")
local_of = lambda root: os.path.join(LIB, hashlib.sha1(os.path.realpath(root).encode()).hexdigest()[:10])     # what the Mac app does (paths.local_dir_for)
A = local_of(SRC)
put(A, "browser-profile/Default/Cookies", "cookie jar")
put(A, "browser-profile/Default/Network/Cookies", "new cookie jar")
put(A, "browser-profile/Default/Cache/data_0", "x" * 1000)
put(A, "browser-profile/Default/Code Cache/js/1", "x")
put(A, "browser-profile/Local State", "{}")
os.symlink("/tmp/some-host-123", os.path.join(A, "browser-profile", "SingletonLock"))       # what a running Chromium leaves: must not be copied
put(A, "session-cookies.json", "[1]")
put(A, "signins.json", '{"ok": true}')
put(A, "models/bge/model.onnx", "MODEL")
put(A, "device.json", '{"id": "old"}')
put(A, "crossref_cache.json", "{}")
before_a = tree(A)
cr = movehome.carry_local(SRC, DST, local_of)
B = local_of(DST)
check("carry: the sign-in browser, its cookies and state, and the model come along", sorted(cr["carried"]) == sorted(movehome.CARRY) and not cr["errors"], cr)
check("carry: the cookies are there", open(os.path.join(B, "browser-profile/Default/Cookies")).read() == "cookie jar" and os.path.exists(os.path.join(B, "browser-profile/Default/Network/Cookies")))
check("carry: the model is there", open(os.path.join(B, "models/bge/model.onnx")).read() == "MODEL")
check("carry: a running browser's lock is not copied", not os.path.lexists(os.path.join(B, "browser-profile", "SingletonLock")))
check("carry: the browser's caches are not copied", not os.path.exists(os.path.join(B, "browser-profile/Default/Cache")) and not os.path.exists(os.path.join(B, "browser-profile/Default/Code Cache")))
check("carry: nothing else (the computer's id, folder-specific caches)", not os.path.exists(os.path.join(B, "device.json")) and not os.path.exists(os.path.join(B, "crossref_cache.json")))
check("carry: the old per-computer folder is unchanged", tree(A) == before_a)
check("carry: nothing went into the data folder", not os.path.exists(os.path.join(DST, skill, "state", "browser-profile")) and not os.path.exists(os.path.join(DST, skill, "state", "models")))
put(A, "signins.json", '{"ok": "newer"}')
again = movehome.carry_local(SRC, DST, local_of)
check("carry: what the new folder already has is kept", sorted(again["kept"]) == sorted(movehome.CARRY) and not again["carried"] and open(os.path.join(B, "signins.json")).read() == '{"ok": true}', again)
check("carry: the same per-computer folder does nothing", movehome.carry_local(SRC, DST, lambda r: A)["skipped"])
check("carry: never into a data folder (a run from a project folder keeps them in state/)", movehome.carry_local(SRC, DST, lambda r: os.path.join(r, skill, "state"))["skipped"] and not os.path.exists(os.path.join(DST, skill, "state", "models")))
OLDV = os.path.join(T, "older-version")                                         # a folder an older version used: the items are in its own state/
put(OLDV, "Papers/x.pdf", "x"); put(OLDV, skill + "/state/browser-profile/Default/Cookies", "old cookies"); put(OLDV, skill + "/state/models/m.onnx", "M")
NEWV = os.path.join(T, "newer")
r3 = movehome.carry_local(OLDV, NEWV, local_of)
check("carry: from where an older version kept them (its state/)", "browser-profile" in r3["carried"] and "models" in r3["carried"] and open(os.path.join(local_of(NEWV), "browser-profile/Default/Cookies")).read() == "old cookies", r3)
print("\n%s" % ("ALL OK" if not fails else "FAILED: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
