# Journal Watcher

A research desk for Information Systems journals (and any others you add). Once a week it checks the journals you follow, screens every new paper against **your** research interests with an AI model, downloads the PDFs you are entitled to, and gives you one place to **read, highlight and ask questions** about them.

- **Watch** — a weekly check of 14 journals (MISQ, ISR, JMIS, JAIS, ISJ, DSS, Management Science, MSOM, Organization Science, CACM, Science, PNAS, NBER, HBR), a search of the archive by year, topic and method, or a free search by terms (for example difference-in-differences, DiD). You see the AI's verdict and reason for each paper, tick the ones you want, and it downloads and summarises them.
- **Library** — everything you have: search titles *and the text of your own highlights*, filter, star, mark as to-read. Drag PDFs you already have into the window.
- **Reader** — highlights in colours with your own meanings, tags, a notes canvas, in-paper search, text-to-speech, "explain this selection", **Ask** the paper with page-cited answers, a structured *paper card* (question, theory, method, findings, limits).
- **Folders** — group papers for a project, then ask the whole folder, compare papers in a **matrix**, map evidence and gaps, brainstorm in an **Ideas Lab**, draft **related work** from your cards (citations checked against what the model was given), run a claim check, a "Reviewer 2".
- **Review** — flashcards made from your papers.
- **Your data stays yours** — PDFs, highlights, notes and settings live in one ordinary folder on your computer. Only the AI features send text to the AI service *you* choose (Anthropic, OpenAI, Gemini, OpenRouter, or Ollama on your own machine), with *your* API key.

## Install the Mac app

1. Download **Journal Watcher.dmg** from the [Releases](../../releases) page (Apple-silicon Macs).
2. Open it and drag **Journal Watcher** onto **Applications**.
3. Open it. The first time macOS may say it can't verify the developer, because the app is not notarised by Apple: open **System Settings → Privacy & Security** and press **Open Anyway** (older macOS: right-click the app → Open). Copies installed by the app's own updater don't ask again.
4. A **welcome guide** explains the two things you need: an **AI key** (how to get one, what it costs, where it is kept) and **sign-ins** to your university library or publishers, so downloads work (any university: OpenAthens, EZproxy, or each publisher's "sign in through your institution" page).

The app tells you when a new version is ready ("Update to x.y.z" in the top bar) and updates itself; **Help → Check for Updates…** looks at once.

## Run from source

```bash
git clone <this repository> && cd journal-watcher
./"Journal Watcher.command"      # creates .venv, installs requirements, opens the dashboard
```

Or by hand: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/python -m playwright install chromium && JW_HOME=~/Documents/"Journal Watcher" .venv/bin/python dashboard/server.py`.

Python 3.12+ (developed on 3.14). Tested on macOS; the dashboard itself is plain Python + a browser UI, but the sign-in windows use Playwright's Chromium and the app packaging is macOS-only.

### Where your data lives

`JW_HOME` (default `~/Documents/Journal Watcher`, or the folder you chose in the Mac app):

```
Papers/YYYY-MM/<Journal>/…        the PDFs, with sidecar files for notes, highlights, cards …
Papers/_index.csv                 the library index
.env                              your API keys (written by Settings; mode 600; never sent anywhere but to their provider)
.agents/skills/ais-journal-watcher/{ai,journals,interests,library,prompts}.json   your settings (copied from defaults/ on first start)
.agents/skills/ais-journal-watcher/state/                                     caches, indexes, runs, the browser profile (regenerable)
```

## Develop

```bash
.venv/bin/python .agents/skills/ais-journal-watcher/scripts/test_reader.py   # offline checks (no network, no API key)
.venv/bin/python .agents/skills/ais-journal-watcher/scripts/test_ui.py       # headless-browser end-to-end suite against a throw-away copy and a fake AI provider (~7 min)
```

On Linux the two updater checks in `test_reader.py` that call macOS's `ditto` fail, and the Mac app cannot be built; CI on a macOS runner is the judge for those. (A sandbox whose preinstalled Chromium is older than the newest Playwright needs the matching `playwright` version, e.g. `pip install playwright==1.63.0` for Chromium 1243.)

`docs/HISTORY.md` is the long development log (design, decisions, gotchas); `docs/roadmap.md` what is done and what is not.

### Build the Mac app, and release it

```bash
app/build_app.sh            # PyInstaller + pywebview → app/dist/Journal Watcher.app, the update zip, a drag-to-Applications .dmg
app/test_update.sh          # end-to-end proof of "Update now" with the real app (~5 min)
```

**Before the first release, once:** `python app/sign_release.py keygen` makes your signing key (kept in `~/.journal-watcher/`, back it up) and prints the public half; put that line in `app/update_public_key.txt` and commit it. Builds that contain it only believe an update feed signed with your key. **After each release:** the workflow publishes the build, then you sign it on your own computer: `python app/sign_release.py sign --tag vX.Y.Z --upload` (it shows what it signs, recomputes the zip's checksum, and attaches `appcast.json.sig`; `--dir` works on files you downloaded, e.g. while the repository is private). Installed copies see the update once it is signed. **To rehearse a release without publishing anything:** Actions → release → Run workflow (a dry run: it builds, rehearses the signing with a throw-away key and keeps the files for three days; a full macOS build).

**Checklist for a release (the first one included):**

1. `CHANGELOG.md` has a `## X.Y.Z` section, and `app/VERSION` says `X.Y.Z` (the workflow sets it from the tag, but local builds read the file).
2. Once, before the first release: `python app/sign_release.py keygen`, then commit `app/update_public_key.txt`. A build without it accepts an unsigned update feed.
3. `.venv/bin/python .agents/skills/ais-journal-watcher/scripts/test_reader.py` passes (on a Mac, all of it).
4. `git tag vX.Y.Z && git push origin vX.Y.Z`. Wait for the release workflow, then check that the release has `Journal Watcher.dmg`, the zip and `appcast.json`.
5. `python app/sign_release.py sign --tag vX.Y.Z --upload`.

While the repository is private, only people signed in to GitHub with access can download the release, and installed apps cannot read the update feed (`releases/latest/download/appcast.json` answers 404 to them). Make the repository public, or host the feed elsewhere (Settings → About & updates), before giving the app to others.

Pushing a tag `vX.Y.Z` runs `.github/workflows/release.yml`: it builds on a macOS runner, writes the update feed `appcast.json` (the notes come from `CHANGELOG.md`), and attaches the `.dmg`, the `.zip` and the feed to the GitHub release. The app's built-in feed address is `https://github.com/<owner>/<repo>/releases/latest/download/appcast.json`. Signing with an Apple Developer ID and notarising are supported through repository secrets (see the workflow); without them the app is ad-hoc signed.

## Privacy and safety

- No analytics, no accounts, no server of ours. Network calls: journal feeds, Crossref, OpenAlex, Unpaywall, the publishers you sign in to, the AI provider you configured, and (if you keep it on) the update feed.
- The scholarly databases (OpenAlex, Crossref, Unpaywall, Semantic Scholar, Open Library, OpenRouter's public model list) are told plainly who is asking: `JournalWatcher/<version>` and this project's page, never an e-mail address. (Unpaywall asks for an e-mail with each lookup: only the one you type in Settings → Paper sources, and only to Unpaywall.) Publisher pages and feeds are read as a browser reads them.
- A friendly reminder that the app is free and takes tips (the author's tip page is set in `dashboard/static/author.json`) appears at most three times, weeks apart, and can be switched off under **About**. To decide when, the app counts on your computer how often you open it (`support.json` in your data folder); the count is never sent anywhere.
- Passwords are typed into the publisher's or your library's own page in a browser window; the app never sees them. It keeps the browser's signed-in session on your computer.
- API keys (the AI providers', OpenAlex's and Elsevier's) are kept in the Mac's Keychain in the Mac app, and otherwise in your data folder's `.env`; never in the repository. **Do not commit your data folder.**

## License

**GNU AGPL v3 or later** (see `LICENSE`). The app bundles [PyMuPDF](https://pymupdf.readthedocs.io), which is AGPL-licensed, so the whole is distributed under the AGPL. Other components and their licences are listed in `THIRD_PARTY.md`.
