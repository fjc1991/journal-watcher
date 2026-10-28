"""Run every AI feature for real, on a throw-away copy of a folder of 4 papers (no cards/skims/notes yet), with a hard spending cap. COSTS REAL MONEY (about $0.70 for all stages).
Nothing in your real library is touched: the PDFs/summaries of the folder are copied to a temporary project, its server gets the API key from your .env through its environment only,
and the copy's ai.json carries budget.monthly_usd = the cap, so the server itself refuses calls beyond it. Screenshots of every output go to <dir>.
    JW_HOME=<your data folder> .venv/bin/python .agents/skills/ais-journal-watcher/scripts/real_smoke.py <screenshot-dir> [cap-usd=1.9]
Environment: FOLDER (folder id, default demo-genai-and-platforms; paper titles in the stages are the Demo ones), STAGES="explain,ideas" (substring filter), COPY_CARDS=1 (copy existing cards).
"""
import csv, json, os, shutil, socket, subprocess, sys, tempfile, time, traceback, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paths  # noqa: E402
CODE = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))        # this checkout (the dashboard and scripts)
ROOT, SKILL = paths.ROOT, paths.BASE                                       # your data folder (JW_HOME): the papers, settings and the embedding model to copy from
OUT = sys.argv[1]; CAP = float(sys.argv[2]) if len(sys.argv) > 2 else 1.9
os.makedirs(OUT, exist_ok=True)
TMP = tempfile.mkdtemp(prefix="jw-real-")
import download as dl  # noqa: E402

FID = os.environ.get("FOLDER", "demo-genai-and-platforms")
sk = os.path.join(TMP, ".agents", "skills", "ais-journal-watcher")
shutil.copytree(os.path.join(CODE, "dashboard"), os.path.join(TMP, "dashboard"), ignore=shutil.ignore_patterns("__pycache__"))
shutil.copytree(HERE, os.path.join(sk, "scripts"), ignore=shutil.ignore_patterns("__pycache__"))
for f in ("journals.json", "interests.json", "library.json", "prompts.json"):          # (copied from the user's data folder, see below)
    if os.path.exists(os.path.join(SKILL, f)):
        shutil.copy(os.path.join(SKILL, f), sk)
ai = json.load(open(os.path.join(SKILL, "ai.json"))); ai.setdefault("budget", {})["monthly_usd"] = CAP     # the server itself stops at the cap
json.dump(ai, open(os.path.join(sk, "ai.json"), "w"), indent=1)
os.makedirs(os.path.join(sk, "state", "runs"))
os.symlink(os.path.join(SKILL, "state", "models"), os.path.join(sk, "state", "models"))      # the local embedding model, not copied
fol = json.load(open(os.path.join(ROOT, "Papers", "_folders", FID + ".json")))
rows = [r for r in csv.DictReader(open(os.path.join(ROOT, "Papers", "_index.csv"))) if dl.index_key(r) in fol["papers"]]
os.makedirs(os.path.join(TMP, "Papers", "_folders"))
for r in rows:
    d = os.path.dirname(os.path.join(TMP, r["file"])); os.makedirs(d, exist_ok=True)
    for ext in ((".pdf", ".md", ".card.json") if os.environ.get("COPY_CARDS") else (".pdf", ".md")):
        src = os.path.join(ROOT, r["file"][:-4] + ext)
        if os.path.exists(src):
            shutil.copy(src, os.path.join(TMP, r["file"][:-4] + ext))
with open(os.path.join(TMP, "Papers", "_index.csv"), "w", newline="") as f:
    w = csv.DictWriter(f, list(rows[0].keys())); w.writeheader(); w.writerows(rows)
json.dump(fol, open(os.path.join(TMP, "Papers", "_folders", FID + ".json"), "w"))

env = dict(os.environ)
for line in open(os.path.join(ROOT, ".env")):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1); env[k.strip()] = v.strip().strip('"').strip("'")
s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
srv = subprocess.Popen([sys.executable, os.path.join(TMP, "dashboard", "server.py"), "--port", str(port), "--no-open"], cwd=TMP, env=env, stdout=subprocess.DEVNULL, stderr=open(os.path.join(OUT, "server.err"), "w"))
base = "http://127.0.0.1:%d" % port
USAGE = os.path.join(sk, "state", "ai_usage.jsonl")


def spent():
    try:
        return sum(json.loads(l).get("cost") or 0 for l in open(USAGE) if l.strip())
    except OSError:
        return 0.0


def log(msg):
    print("[%5.1fs  $%.3f] %s" % (time.time() - T0, spent(), msg), flush=True)


T0 = time.time()
try:
    for _ in range(80):
        try:
            urllib.request.urlopen(base + "/api/config", timeout=1); break
        except Exception:  # noqa: BLE001
            time.sleep(0.3)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        br = pw.chromium.launch(headless=True)
        ctx = br.new_context(viewport={"width": 1400, "height": 1500}, permissions=["clipboard-read", "clipboard-write"])
        page = ctx.new_page(); errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        answers = []
        page.on("dialog", lambda d: d.accept(answers.pop(0) if answers else (d.default_value or "")))
        shot = lambda n, el=None: (el or page).screenshot(path=os.path.join(OUT, n + ".png"))

        ONLY = [x for x in os.environ.get("STAGES", "").split(",") if x]
        def stage(name, fn):
            if ONLY and not any(o in name for o in ONLY):
                return
            if spent() > CAP - 0.15:
                log("SKIPPED %s (cap)" % name); return
            try:
                fn(); log("done: " + name)
            except Exception as e:  # noqa: BLE001
                log("FAILED %s: %s" % (name, str(e).splitlines()[0][:200])); traceback.print_exc(limit=2)
                try: shot("fail-" + name.replace(" ", "-"))
                except Exception: pass  # noqa: E722

        def open_paper(title):
            page.goto(base + "/#/folder/" + FID); page.wait_for_selector("#fv-list .librow"); page.locator('#fv-list .librow:has-text("%s") [data-read]' % title).click()
            page.wait_for_selector(".rd-band .textLayer span", timeout=60000); page.wait_for_timeout(800)

        def sides(n):   # a screenshot of the reader's right-hand pane, scrolled to the top
            page.evaluate("document.querySelectorAll('#reader .rd-scroll, #reader .ask-log').forEach(e => e.scrollTop = 0)"); shot(n, page.locator("#rd-side"))

        # ---- the activity tray: background indexing by itself (local model, free), and Cancel on a running card (costs a few cents at most)
        def index():
            page.goto(base + "/#/library"); page.wait_for_function("!document.querySelector('#aibtn').disabled"); page.wait_for_selector("#aibtn.busy", timeout=120000)
            page.click("#aibtn"); page.wait_for_selector("#aitray .at-job.live[data-tool=library]"); page.wait_for_timeout(1500); shot("00a-tray-indexing", page.locator("#aitray"))
            page.wait_for_function("document.querySelector('#aitray .at-job[data-tool=library].done')", timeout=300000); page.wait_for_timeout(500); shot("00b-tray-indexed", page.locator("#aitray"))
            r = json.load(urllib.request.urlopen(base + "/api/activity?pending=1")); print("   index state:", r["indexing"])
        stage("index (background, local)", index)

        def cancel():
            open_paper("Will your AI turn backers"); page.click('.rd-tabs button[data-tab="card"]'); page.wait_for_selector('[data-k="make"]'); page.click('[data-k="make"]')
            page.wait_for_selector("#aibtn.busy"); page.wait_for_timeout(2500); page.click("#aibtn"); page.wait_for_selector("#aitray .at-job.live[data-tool=card]"); shot("00c-tray-card-running", page.locator("#aitray"))
            page.click("#aitray .at-job.live[data-tool=card] [data-at=cancel]"); page.wait_for_selector("#aitray .at-job.cancelled", timeout=60000); page.wait_for_timeout(500); shot("00d-tray-cancelled", page.locator("#aitray"))
            j = [x for x in json.load(urllib.request.urlopen(base + "/api/activity"))["jobs"] if x["kind"] == "card"][0]; print("   cancelled card job:", j["status"], "cost so far $%.4f" % j["cost"])
            page.keyboard.press("Escape")
        stage("cancel (a card)", cancel)

        # ---- paper 1: an empirical paper
        def card1():
            open_paper("Will your AI turn backers"); page.click('.rd-tabs button[data-tab="card"]'); page.wait_for_selector('[data-k="make"]')
            log("card estimate: " + page.inner_text(".cd-est")); page.click('[data-k="make"]'); page.wait_for_selector('.of[data-tool="card"]', timeout=180000)
            page.wait_for_timeout(500); sides("01-card-empirical")
            print("   sections:", [t.strip() for t in page.locator("#reader .of-sec h4").all_inner_texts()])
        stage("card (empirical)", card1)

        def summary():
            page.click('.rd-tabs button[data-tab="sum"]'); page.wait_for_timeout(500); sides("02a-summary-saved")
            page.click("#sum-go"); page.wait_for_selector("#sum-act:not(.hide)", timeout=180000); page.wait_for_timeout(400); sides("02b-summary-generated")
            print("   sections:", [t.strip() for t in page.locator("#sum-body .of-sec h4").all_inner_texts()])
        stage("summary", summary)

        def skim():
            page.click("#rd-skimbtn"); page.wait_for_selector('[data-sk="make"]'); page.click('[data-sk="make"]'); page.wait_for_selector("#rd-skim .sk-chip", timeout=180000); page.wait_for_timeout(2500)
            shot("03a-skim-page"); page.click('.rd-tabs button[data-tab="skim"]'); page.wait_for_selector("#skl-body .of-sec", timeout=20000); sides("03b-skim-list")
            print("   lenses:", [t.strip() for t in page.locator("#skl-body .of-sec h4").all_inner_texts()])
        stage("skim", skim)

        def ask():
            page.click('.rd-tabs button[data-tab="ask"]'); page.wait_for_selector("#ask-q")
            for q in ("What is the main finding of this paper and what sample does it rely on?", "Did the authors run a field experiment with crowdfunding backers in Korea, and what was the budget?"):
                n = page.locator(".ask-turn").count(); page.fill("#ask-q", q); page.click('#reader [data-k="send"]')
                page.wait_for_function("document.querySelectorAll('.ask-turn').length === %d && document.querySelectorAll(\".ask-turn [data-t='regen']\").length === %d" % (n + 1, n + 1), timeout=120000)
            page.wait_for_timeout(400); sides("04a-ask")
            page.locator(".ask-turn").last.locator("[data-t='follow']").click(); page.wait_for_selector(".ask-turn .ask-fu .rd-btn", timeout=60000); page.wait_for_timeout(300); sides("04b-ask-followups")
            for i in range(page.locator(".ask-turn").count()):
                print("   answer %d sections:" % i, [t.strip() for t in page.locator(".ask-turn").nth(i).locator(".of-sec h4").all_inner_texts()])
        stage("ask", ask)

        def explain():
            open_paper("Will your AI turn backers"); page.click('.rd-tabs button[data-tab="toc"]'); n = page.evaluate("""() => { const q = [...document.querySelectorAll('.rd-band[data-n="2"] .textLayer span')].find(s => s.textContent.trim().length > 60);
              if (!q) return 0; q.scrollIntoView({block: 'center'}); const r = document.createRange(); r.selectNodeContents(q); const s = getSelection(); s.removeAllRanges(); s.addRange(r);
              document.querySelector('#rd-pdf').dispatchEvent(new MouseEvent('mouseup', {bubbles: true})); return q.textContent.length; }""")
            if not n: raise RuntimeError("no long span on page 2")
            page.wait_for_selector("#rd-sel:not(.hide)"); page.click('#rd-sel [data-s="explain"]'); page.wait_for_selector(".rd-expmenu:not(.hide)")
            page.locator(".rd-expmenu [data-id]:not([data-id^='__'])").first.click(); page.wait_for_selector(".rd-exp .ex-act:not(.hide)", timeout=120000); page.wait_for_timeout(300)
            shot("05-explain", page.locator(".rd-exp"))
        stage("explain", explain)

        # ---- paper 2: an editorial (not empirical)
        def card2():
            page.click('#reader [data-a="back"]'); open_paper("The Janus Effect"); page.click('.rd-tabs button[data-tab="card"]'); page.wait_for_selector('[data-k="make"]')
            page.click('[data-k="make"]'); page.wait_for_selector('.of[data-tool="card"]', timeout=180000); page.wait_for_timeout(500); sides("06-card-editorial")
            print("   sections:", [t.strip() for t in page.locator("#reader .of-sec h4").all_inner_texts()])
        stage("card (editorial)", card2)
        if page.locator('#reader [data-a="back"]').count():
            page.click('#reader [data-a="back"]'); page.wait_for_timeout(500)

        # ---- the folder
        def evidence():
            page.goto(base + "/#/folder/" + FID + "/evidence"); page.wait_for_selector("#ev-make"); log("evidence: " + page.inner_text("#ev-make")); page.click("#ev-make")
            page.wait_for_selector("#fv-body:has-text('4 of 4')", timeout=300000); page.wait_for_timeout(1500); shot("07-evidence", page.locator("#fv-body"))
            print("   sections:", [t.strip() for t in page.locator("#fv-body .of-sec h4").all_inner_texts()])
        stage("evidence + the other cards", evidence)

        def matrix():
            page.goto(base + "/#/folder/" + FID + "/matrix"); page.wait_for_selector("table.mx"); answers.extend(["Sample size", "How many observations or participants are in the sample?"])
            page.click('[data-k="cols"]'); page.click('[data-k="newcol"]'); page.wait_for_selector('[data-k="fill"]', timeout=20000); log("matrix: " + page.inner_text('[data-k="fill"]'))
            page.click('[data-k="fill"]'); page.wait_for_selector("text=every cell is filled", timeout=240000); page.wait_for_timeout(500); shot("08-matrix", page.locator("#fv-body"))
        stage("matrix", matrix)

        def folder_ask():
            page.goto(base + "/#/folder/" + FID + "/ask"); page.wait_for_selector("#am-q")
            page.fill("#am-q", "How do these papers differ in how they treat generative AI, and what do they agree on?"); page.click('[data-k="send"]')
            page.wait_for_function("document.querySelectorAll('.am-log .ask-turn [data-t=regen]').length === 1", timeout=180000); page.wait_for_timeout(400); shot("09-folder-ask", page.locator("#fv-body"))
            print("   sections:", [t.strip() for t in page.locator(".am-log .of-sec h4").all_inner_texts()])
        stage("folder ask", folder_ask)

        def discover():
            page.goto(base + "/#/folder/" + FID + "/discover"); page.wait_for_selector("#dc-out .of", timeout=180000); page.wait_for_timeout(500); shot("10-discover", page.locator("#fv-body"))
        stage("discover (OpenAlex, free)", discover)

        def ideas():
            page.goto(base + "/#/folder/" + FID + "/lab"); page.wait_for_selector('[data-k="events"]'); page.fill("#lb-focus", "generative AI and the governance of digital platforms and labour markets")
            page.select_option("#lb-n", "3"); page.click('[data-k="events"]'); page.wait_for_function("document.querySelector('#ev-st') && !document.querySelector('[data-k=events]').disabled", timeout=240000); page.wait_for_timeout(500)
            log("events found: %d" % page.locator(".lab-event").count())
            page.click('[data-k="gen"]'); page.wait_for_selector(".idea", timeout=300000); page.wait_for_timeout(800); shot("11a-ideas", page.locator("#fv-body"))
            page.locator(".idea >> nth=0 >> [data-itab=design]").click(); page.locator(".idea >> nth=0 >> [data-itab=check]").click(); page.wait_for_timeout(200)
            page.locator('.idea >> nth=0 >> [data-a="develop"]').click(); page.wait_for_selector('.idea >> nth=0 >> [data-a="copydev"]', timeout=240000); page.wait_for_timeout(800)
            shot("11b-idea-outline", page.locator(".idea >> nth=0"))
            print("   ideas:", page.locator(".idea").count(), "outline sections:", [t.strip() for t in page.locator(".idea >> nth=0 >> .idea-dev .of-sec h4").all_inner_texts()])
        stage("ideas lab", ideas)

        def draft():   # needs the cards: run it with COPY_CARDS=1 (about 3 cents)
            page.goto(base + "/#/folder/" + FID + "/draft"); page.wait_for_selector('[data-dr="go"]')
            page.fill("#dr-topic", "We study how disclosing generative-AI use shapes backers' trust and funding decisions on crowdfunding platforms.")
            page.wait_for_function("document.querySelector('#dr-est').textContent.includes('·')", timeout=20000); log("draft estimate: " + page.inner_text("#dr-est").strip())
            page.click('[data-dr="go"]'); page.wait_for_selector('.of[data-tool="draft"]', timeout=240000); page.wait_for_timeout(800); shot("12-draft", page.locator("#fv-body"))
            print("   sections:", [t.strip() for t in page.locator(".of[data-tool=draft] .of-sec h4").all_inner_texts()], "| citations:", page.locator(".dr-c").count(), "| check:", page.inner_text('.of-sec[data-sec="check"]')[:400].replace("\n", " "))
            page.click(".dr-unc summary"); print("   uncited:", [t.strip() for t in page.locator(".dr-unc li").all_inner_texts()])
        stage("related-work draft", draft)

        def claims():   # about 5 cents; the sentences name the Demo papers, one of them wrongly
            page.goto(base + "/#/folder/" + FID + "/claims"); page.wait_for_selector("#cl-text")
            page.fill("#cl-text", "Disclosing AI use on Kickstarter was associated with lower funding success (Singhal et al., 2026). "
                      "Freelancers exposed to ChatGPT more than doubled the number of bids they placed (Yiu et al., 2026). "
                      "AI-generated product ideas were always rated better than crowd-generated ideas (Lee & Maruping, 2026). "
                      "The disclosure penalty was strongest for campaigns in technology categories (Susarla et al., 2023). "
                      "Generative AI will replace most knowledge workers within five years.")
            page.wait_for_function("document.querySelectorAll('.cl-s').length === 5"); page.wait_for_function("document.querySelector('#cl-gob') && /Check 5 claims/.test(document.querySelector('#cl-gob').textContent)")
            log("claims: " + page.inner_text("#cl-gob").strip())
            page.click('[data-cl="go"]'); page.wait_for_selector('.of[data-tool="claims"]', timeout=240000); page.wait_for_timeout(600); shot("13-claims", page.locator("#fv-body"))
            for r in page.locator(".cl-row").all():
                print("   ", r.get_attribute("data-n"), r.get_attribute("class").split()[-1], "|", r.locator(".cl-why").inner_text()[:140] if r.locator(".cl-why").count() else "", "| cited-warning" if "do not clearly say" in r.inner_text() else "")
        stage("claim check", claims)

        def synthesis():   # about 5 cents; needs the cards (COPY_CARDS=1)
            page.goto(base + "/#/folder/" + FID + "/synthesis"); page.wait_for_selector('[data-sy="go"]')
            page.wait_for_function("document.querySelector('#sy-est').textContent.includes('·')", timeout=20000); log("synthesis estimate: " + page.inner_text("#sy-est").strip())
            page.click('[data-sy="go"]'); page.wait_for_selector('.of[data-tool="synthesis"]', timeout=240000); page.wait_for_timeout(800); shot("14-synthesis", page.locator("#fv-body"))
            print("   themes:", [t.strip() for t in page.locator(".sy-map tbody th b").all_inner_texts()], "| cells:", page.locator(".sy-cell").count(), "| gaps:", page.locator(".sy-gap").count(), "| ungrounded:", page.locator(".sy-gap.ung").count(),
                  "| check:", page.inner_text('.of-sec[data-sec="check"]')[:200].replace("\n", " "))
        stage("synthesis", synthesis)

        def figure():   # a mention of "Table n" in the text -> hover -> Explain it: the caption's page goes to the model as a picture (about 2 cents)
            open_paper("Will your AI")
            page.wait_for_function("document.querySelectorAll('.rd-band .textLayer span').length > 50", timeout=30000)
            found = None
            for n in range(1, 9):                                             # the first page of the paper that mentions a table or a figure in running text
                page.evaluate("n => document.querySelector(`#rd-pdf`).scrollTop = document.querySelector(`.rd-band[data-n='${n}']`)?.offsetTop || 0", n); page.wait_for_timeout(500)
                found = page.evaluate("""(n) => { const re = /\\b(Table|Figure) \\d\\b/; for (const s of document.querySelectorAll(`.rd-band[data-n="${n}"] .textLayer span`)) { const m = re.exec(s.textContent); if (m) { const r = document.createRange(); r.setStart(s.firstChild, m.index); r.setEnd(s.firstChild, m.index + m[0].length); const b = r.getBoundingClientRect(); return [b.left + b.width / 2, b.top + b.height / 2, m[0]]; } } return null; }""", n)
                if found: break
            log("mention: %s" % (found and found[2]))
            page.mouse.move(found[0] - 30, found[1] + 60); page.mouse.move(found[0], found[1]); page.wait_for_selector('.rd-hov [data-h="explain"]', timeout=15000)
            page.click('.rd-hov [data-h="explain"]'); page.wait_for_selector(".rd-exp:not(.hide) .ex-act:not(.hide)", timeout=120000); page.wait_for_timeout(500); shot("15-figure-explain")
            print("   answer:", page.inner_text(".rd-exp .ex-body")[:300].replace("\n", " "))
        stage("figure explain", figure)

        def flashcards():   # about 3 cents; needs the cards (COPY_CARDS=1)
            page.goto(base + "/#/library/review"); page.wait_for_selector(".rv-head"); page.click('[data-rv="view"][data-v="make"]'); page.click('[data-rv="selall"]')
            page.wait_for_function("document.querySelector('#rv-est') && document.querySelector('#rv-est').textContent.includes('·')", timeout=20000); log("flashcards estimate: " + page.inner_text("#rv-est").strip())
            page.click('[data-rv="go"]'); page.wait_for_selector(".rv-card", timeout=240000); log("toast: " + page.inner_text("#toast")); page.keyboard.press("Space"); page.wait_for_timeout(500); shot("16-flashcard")
            page.click('[data-rv="view"][data-v="browse"]'); page.wait_for_selector(".rv-item"); shot("17-flashcards-browse", page.locator("#rv-body"))
            for it in page.locator(".rv-item").all()[:6]:
                print("   Q:", it.locator(".rv-iq").inner_text()[:110], "| A:", it.locator(".rv-ia").inner_text()[:110])
            print("   cards:", page.locator(".rv-item").count())
        stage("flashcards", flashcards)

        def workspace():   # free (no model call); needs the cards for the tray's "Card lines"
            page.goto(base + "/#/folder/" + FID + "/workspace"); page.wait_for_selector(".ws-list"); page.click('[data-wt="ln"]'); page.wait_for_selector(".ws-it")
            for i in (0, 9, 20, 30):
                page.locator(".ws-it >> nth=%d" % i).click(); page.wait_for_timeout(150)
            page.click('[data-w="note"]'); page.fill(".ws-card.note textarea.nc-main", "**Compare:** do the effects point the same way?"); page.click("#ws-q")
            page.evaluate("""async () => { const els = [...document.querySelectorAll('.ws-card')]; els.forEach((e, i) => { e.style.left = (30 + (i % 3) * 330) + 'px'; e.style.top = (60 + Math.floor(i / 3) * 190) + 'px'; }); }""")
            page.wait_for_timeout(300); shot("18-workspace")
            print("   cards:", page.locator(".ws-card").count(), "| tray items:", page.locator(".ws-it").count())
        stage("workspace", workspace)

        def audio():   # about 1 cent each; needs the cards (COPY_CARDS=1)
            page.goto(base + "/#/library"); page.wait_for_selector("#libitem")
            txt = page.evaluate("""async () => { const lib = await (await fetch('/api/library')).json(), f = lib.find(r => /backers away/i.test(r.title)).file;
              const r = await fetch('/api/ai/audio', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({file: f})}); const t = await r.text(); return t.split('\\n').filter(Boolean).map(l => JSON.parse(l)).filter(m => m.type === 'text').map(m => m.delta).join(''); }""")
            print("   words:", len(txt.split()), "|", txt[:420].replace("\n", " / "))
            fol = page.evaluate("""async () => { const r = await fetch('/api/ai/audio', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({folder: '%s'})}); const t = await r.text(); return t.split('\\n').filter(Boolean).map(l => JSON.parse(l)).filter(m => m.type === 'text').map(m => m.delta).join(''); }""" % FID)
            print("   folder words:", len(fol.split()), "|", fol[:300].replace("\n", " / "))
        stage("audio script", audio)

        def reviewer():   # about 4 cents; needs the cards (COPY_CARDS=1)
            page.goto(base + "/#/folder/" + FID + "/claims"); page.wait_for_selector("#cl-text")
            page.fill("#cl-text", "Generative AI is transforming digital platforms. We show that disclosing AI use always reduces funding on crowdfunding platforms, because backers distrust machines. "
                      "Our evidence comes from a single platform over one year, and we conclude that regulators should ban AI-generated campaign content. Freelancers, in contrast, benefit from AI at every skill level.")
            page.wait_for_function("document.querySelector('#cl-rvest') && /[$¢]/.test(document.querySelector('#cl-rvest').textContent)", timeout=20000); log("reviewer: " + page.inner_text("#cl-rvb").strip())
            page.click("#cl-rvb"); page.wait_for_selector("#cl-rev .of", timeout=240000); page.wait_for_timeout(600); shot("19-reviewer2", page.locator("#cl-rev"))
            print("   rec:", page.inner_text("#cl-rev .rv2-rec")[:60], "| sections:", [t.strip() for t in page.locator("#cl-rev .of-sec h4").all_inner_texts()], "| left out:", page.locator("#cl-rev .of-notices").count())
            print("   first major:", page.locator("#cl-rev .rv2-i >> nth=0").inner_text()[:300].replace("\n", " "))
        stage("reviewer", reviewer)
        log("console errors: %s" % errors[:5])
        br.close()
finally:
    srv.terminate(); srv.wait(timeout=10)
    log("TOTAL spent $%.3f of a $%.2f cap; screenshots in %s; project copy %s" % (spent(), CAP, OUT, TMP))
    shutil.rmtree(TMP, ignore_errors=True)
