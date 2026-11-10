// The welcome guide for a first start: what the app does, what an API key ("API code") is and how to get and save one, what the publisher sign-ins are and
// how they stay safe, and a short map of the app. It opens by itself the first time (until you finish or skip it), and again from Help → Welcome guide,
// Settings → About & updates or the sidebar. Everything it does goes through the same places as Settings (/api/ai/key, /api/ai/config, /api/login).
import {$, $$, esc, api, toast, ico} from "./state.js";

const KEY = "jw-welcome";
const SITE = {anthropic: ["Anthropic (Claude)", "https://console.anthropic.com/settings/keys", "console.anthropic.com"], openai: ["OpenAI", "https://platform.openai.com/api-keys", "platform.openai.com"],
  gemini: ["Google Gemini", "https://aistudio.google.com/apikey", "aistudio.google.com"], openrouter: ["OpenRouter", "https://openrouter.ai/keys", "openrouter.ai"]};
const STEPS = ["Welcome", "AI keys", "Sign-ins", "Your interests", "Around the app", "Ready"];
let dlg = null, step = 0, AI = null, CFG = null, SIN = null, chosen = "anthropic", saved = {};

const seen = () => { try { return !!localStorage.getItem(KEY); } catch (e) { return true; } };
const mark = () => { try { localStorage.setItem(KEY, "done"); } catch (e) { /* the guide may come again; that is all */ } };

async function load() {
  try { [AI, CFG, SIN] = await Promise.all([api("/api/ai/config").catch(() => null), api("/api/config").catch(() => null), api("/api/signins").catch(() => null)]); } catch (e) { /* the guide still works without it */ }
  const names = AI ? Object.keys(AI.providers).filter(n => AI.providers[n].key_env) : [];
  if (names.length && !names.includes(chosen)) chosen = names[0];
}
const keyed = () => AI ? Object.entries(AI.providers).filter(([, p]) => p.key_env && p.configured).map(([n]) => n) : [];

const pages = [
  () => `<h2>Welcome to Journal Watcher</h2>
    <p class="wl-lead">It follows the journals you care about, picks out the new papers that match your research interests, downloads them, and gives you one place to read, highlight and ask questions about them.</p>
    <div class="wl-cards">
      <div class="wl-c" data-tool="watch"><span class="wl-ci">${ico("watch")}</span><b>Watch</b><span>Checks your journals every week and screens each new paper against your interests.</span></div>
      <div class="wl-c" data-tool="read"><span class="wl-ci">${ico("papers")}</span><b>Read</b><span>Highlights, notes and a short “card” for every paper, in one reader.</span></div>
      <div class="wl-c" data-tool="ask"><span class="wl-ci">${ico("ask")}</span><b>Think</b><span>Ask a folder of papers, compare them, find the gaps and draft related work.</span></div></div>
    <div class="wl-box">${ico("folders", "sm")}<div><b>Your papers stay yours.</b> Everything — PDFs, highlights, notes, folders, settings — lives in one ordinary folder on this Mac${CFG && CFG.home ? `: <code>${esc(CFG.home)}</code>` : ""}. Nothing is uploaded to a Journal Watcher server (there is none). Only the AI features send text to the AI service you choose.</div></div>
    <p class="meta">This guide takes about two minutes and you can skip any step. You can open it again later from <b>Help → Welcome guide</b> or <b>Settings → About &amp; updates</b>.</p>`,

  () => { const names = AI ? Object.keys(AI.providers).filter(n => AI.providers[n].key_env) : [], p = AI && AI.providers[chosen], site = SITE[chosen], have = keyed();
    return `<h2>AI keys: the “API code” the AI features need</h2>
    <p class="wl-lead">Screening new papers, summaries, paper cards and “Ask” are done by an AI service. To use one, the app needs an <b>API key</b>: a long secret code, like a password, that tells the service which account to charge. You pay the service directly for what you use (a few cents for a question, more for screening a whole week of papers); Journal Watcher itself is free.</p>
    <div class="wl-two"><div>
      <h3>Get a key (about 3 minutes)</h3>
      <ol class="wl-steps"><li>Pick a service below. <b>Anthropic (Claude)</b> is the one this app is tuned for.</li>
        <li>Open its keys page${site ? ` (<a href="${esc(site[1])}" target="_blank" rel="noopener">${esc(site[2])}</a>)` : ""}, sign in and add a small credit or payment method: the service asks for this before it issues keys.</li>
        <li>Choose <b>Create key</b>, give it any name (“Journal Watcher”) and <b>copy</b> it. It is shown only once.</li>
        <li>Paste it here and press <b>Save key</b>.</li></ol>
      <p class="meta">No key yet? You can still use the app to read PDFs, highlight, take notes and organise folders. <b>Ollama</b> runs models on your own Mac with no key at all (see Settings).</p></div>
    <div>
      <h3>Paste it here</h3>
      <label class="wl-l">Service</label><select id="wl-prov" aria-label="AI service">${names.map(n => `<option value="${esc(n)}"${n === chosen ? " selected" : ""}>${esc((SITE[n] || [n])[0])}${AI.providers[n].configured ? " ✓" : ""}</option>`).join("")}</select>
      <label class="wl-l">API key ${p ? `<small>(${esc(p.key_env)})</small>` : ""}</label>
      <div class="wl-row"><input type="password" id="wl-key" autocomplete="off" spellcheck="false" placeholder="${p && p.configured ? "A key is saved. Paste a new one to replace it" : "sk-… paste your key"}" aria-label="API key"><button class="btn primary" id="wl-save">Save key</button></div>
      <div class="wl-msg" id="wl-keymsg" role="status">${p && p.configured ? `${ico("check", "sm")} A key for ${esc((SITE[chosen] || [chosen])[0])} is saved.` : ""}</div>
      <label class="wl-l">Monthly spending limit <small>(optional, US dollars)</small></label>
      <div class="wl-row"><input type="number" id="wl-budget" min="0" step="1" placeholder="no limit" value="${AI && AI.budget && AI.budget.monthly_usd != null ? esc(AI.budget.monthly_usd) : ""}" aria-label="Monthly budget in US dollars"><button class="btn" id="wl-bsave">Set limit</button></div>
      <p class="meta">When this month’s spending reaches the limit, AI calls stop. Before every weekly check the app shows an estimate, and <b>Settings → Usage &amp; cost</b> keeps a running total.</p></div></div>
    <div class="wl-box">${ico("key", "sm")}<div><b>Where the key is kept.</b> In a file called <code>.env</code> inside your data folder, readable only by you. It is sent only to the service it belongs to, never shown again on screen and never shared. If it ever leaks, delete it on the service’s keys page and make a new one.${have.length ? ` <span class="ok">Saved so far: ${have.map(n => esc((SITE[n] || [n])[0])).join(", ")}.</span>` : ""}</div></div>`; },

  () => { const L = (SIN && SIN.library) || {}, st = k => (k !== "publishers" && SIN && SIN[k] && SIN[k].signed_in) ? `<span class="wl-ok">${ico("check", "sm")} Signed in</span>` : "";
    const btn = (k, t, d) => `<button class="btn${k !== "publishers" && SIN && SIN[k] && SIN[k].signed_in ? " done" : ""}" data-si="${k}"><b>${esc(t)}</b><span>${esc(d)}</span>${st(k)}</button>`;
    return `<h2>Sign-ins: getting the PDFs behind paywalls</h2>
    <p class="wl-lead">Many journals keep their PDFs for members or university libraries. Journal Watcher can fetch them for you with a browser window where <b>you sign in yourself</b>, exactly as you would in Safari. It works with any university: you tell it which one.</p>
    <div class="wl-two"><div>
      <h3>How it works</h3>
      <ul class="wl-list"><li>You press a sign-in button. A small browser window opens on the publisher’s or your library’s own page.</li>
        <li>You type your username and password <b>into that page</b>. Journal Watcher never sees or stores your password.</li>
        <li>When it worked, the window shows a green tick, closes by itself, and the button here gets a <b>green tick</b> too. The app keeps only the browser’s signed-in session, on this Mac, in your data folder (university sessions usually last about 8–12 hours).</li>
        <li>The first time, the app downloads a small private browser (about 150 MB), used only for this.</li></ul>
      <p class="meta"><b>Which university?</b> Any. Choose it under <b>My university library</b> (OpenAthens, EZproxy, or each publisher’s “sign in through your institution” page, where you pick yours from a list). Not needed for open-access papers; you can do all of this later from <b>Sign-ins</b> at the bottom of the sidebar.</p></div>
    <div>
      <h3>Sign in now (optional)</h3>
      <div class="wl-btns">
        ${btn("informs", "INFORMS member account", "ISR, Management Science, MSOM, Organization Science")}
        ${btn("ais", "AIS member account", "MIS Quarterly, JAIS")}
        ${L.signs_in ? btn("library", L.name || "My university library", "Wiley, Elsevier, Taylor & Francis and what your library holds") : `<button class="btn" data-si="setup"><b>My university library</b><span>${L.configured ? "Library access settings…" : "Not set up yet: choose your university…"}</span></button>`}
        ${btn("publishers", "Other publisher sites", "One tab per publisher; choose your university when asked")}</div>
      <div class="wl-msg" id="wl-simsg" role="status"></div></div></div>`; },

  () => { const ts = (CFG && CFG.topics) || [], weekly = CFG ? CFG.journals.filter(j => j.weekly !== false).length : 0, off = CFG ? CFG.journals.filter(j => j.weekly === false && !j.needs_query).length : 0;
    return `<h2>Your interests: what the app looks for</h2>
    <p class="wl-lead">Every new paper is compared with <b>your topics</b>: a list of words and phrases per topic. Only papers that match are shown to the AI, which then judges them against the same topics. Until you put in your own, the app looks for three example topics, and that would spend AI money on papers you do not care about.</p>
    <div class="wl-two"><div>
      <h3>Your topics now</h3>
      <ul class="wl-list">${ts.length ? ts.map(t => `<li>${esc(t)}</li>`).join("") : "<li>None yet.</li>"}</ul>
      <p class="meta">These three are the examples the app starts with if you have not changed them: GenAI, Platform Economics, Decision Support Systems.</p></div>
    <div>
      <h3>Your journals</h3>
      <p>${weekly} journal${weekly === 1 ? " is" : "s are"} read by the weekly check${off ? `, and ${off} more are in your list, switched off: the rest of the <b>AIS basket of eleven</b> and of the <b>UTD 24</b>` : ""}. You switch each one on or off.</p>
      <div class="wl-btns"><button class="btn primary" data-si="interests"><b>Set my topics and journals</b><span>Opens Settings → Journals &amp; interests; you can come back to this guide from Search</span></button></div></div></div>`; },

  () => `<h2>Finding your way around</h2>
    <div class="wl-map">
      <div><span class="wl-ci" data-tool="watch">${ico("watch")}</span><b>Watch</b><span>Press <i>Check for new papers</i>. The app screens them, you tick the ones you want and press <i>Download &amp; summarize</i>.</span></div>
      <div><span class="wl-ci" data-tool="library">${ico("library")}</span><b>Library</b><span>Everything you have. Search titles and the text of your highlights, filter, and open a paper to read it. Drag PDFs into the window to add your own.</span></div>
      <div><span class="wl-ci" data-tool="folders">${ico("folders")}</span><b>Folders</b><span>Group papers for a project, then Ask them, compare them in a matrix, look for gaps, brainstorm ideas, draft related work.</span></div>
      <div><span class="wl-ci" data-tool="review">${ico("review")}</span><b>Review</b><span>Flashcards made from your papers, a few minutes a day.</span></div>
      <div><span class="wl-ci" data-tool="settings">${ico("settings")}</span><b>Settings</b><span>Keys and models, what each AI feature costs, prompts, backups and updates.</span></div>
      <div><span class="wl-ci" data-tool="ask">${ico("search")}</span><b>Search <kbd>⌘K</kbd></b><span>Jump to any paper, folder, tool or setting from anywhere.</span></div></div>
    <div class="wl-box">${ico("download", "sm")}<div><b>Backups and updates.</b> Your data is plain files in your data folder: copy it like any folder, or use <b>Settings → AI files &amp; backup</b> for a zip. When a new version of the app is ready, an <b>Update</b> button appears in the top bar.</div></div>`,

  () => { const have = keyed(), signed = SIN ? ["informs", "ais", "library"].filter(k => SIN[k] && SIN[k].signed_in).map(k => ({informs: "INFORMS", ais: "AIS", library: SIN.library.name || "your library"})[k]) : []; return `<h2>You’re ready</h2>
    <ul class="wl-check">
      <li class="${have.length ? "ok" : "todo"}">${ico(have.length ? "check" : "key", "sm")}<span><b>AI key:</b> ${have.length ? `saved for ${have.map(n => esc((SITE[n] || [n])[0])).join(", ")}` : `none yet: reading, highlighting and notes work, the AI features will ask for one`}</span>${have.length ? "" : `<button class="btn small" data-go="1">Add a key</button>`}</li>
      <li class="${signed.length || saved.signin ? "ok" : "todo"}">${ico(signed.length || saved.signin ? "check" : "key", "sm")}<span><b>Sign-ins:</b> ${signed.length ? `signed in to ${signed.join(", ")}` : saved.signin ? "started in this guide" : "not set up yet: do it from Sign-ins in the sidebar when a download needs it"}</span>${signed.length || saved.signin ? "" : `<button class="btn small" data-go="2">Set up</button>`}</li>
      <li class="ok">${ico("check", "sm")}<span><b>Your data folder:</b> ${CFG && CFG.home ? `<code>${esc(CFG.home)}</code>` : "chosen"}</span></li></ul>
    <p class="wl-lead">A good first move: open <b>Watch</b> and press <b>Check for new papers</b>, or drag a PDF you already have into the window.</p>`; },
];

function draw() {
  const last = step === pages.length - 1;
  dlg.innerHTML = `<div class="wl-top"><ol class="wl-dots" aria-label="Steps">${STEPS.map((t, i) => `<li class="${i === step ? "on" : i < step ? "done" : ""}"><button data-go="${i}" aria-label="${esc(t)}"${i === step ? ' aria-current="step"' : ""}><span>${i < step ? "✓" : i + 1}</span>${esc(t)}</button></li>`).join("")}</ol>
      <button class="wl-x" data-wl="skip" aria-label="Close the guide">${ico("x")}</button></div>
    <div class="wl-body">${pages[step]()}</div>
    <div class="wl-foot"><button class="btn" data-wl="back"${step ? "" : " disabled"}>Back</button><span class="spacer"></span>${last ? "" : `<button class="btn" data-wl="skip">Skip the guide</button>`}<button class="btn primary" data-wl="${last ? "finish" : "next"}" autofocus>${last ? "Start using Journal Watcher" : "Next"}</button></div>`;
  const body = $(".wl-body", dlg); body.scrollTop = 0;
}

async function onClick(e) {
  const t = e.target.closest("[data-wl],[data-go],[data-si],#wl-save,#wl-bsave"); if (!t) return;
  if (t.dataset.go != null) { step = +t.dataset.go; return draw(); }
  const w = t.dataset.wl;
  if (w === "next") { step = Math.min(pages.length - 1, step + 1); return draw(); }
  if (w === "back") { step = Math.max(0, step - 1); return draw(); }
  if (w === "skip" || w === "finish") { mark(); dlg.close(); return; }
  if (t.dataset.si === "setup") { mark(); dlg.close(); if (window.Settings) Settings.open("library"); return; }
  if (t.dataset.si === "interests") { mark(); dlg.close(); if (window.Settings) Settings.open("watch"); return; }
  if (t.dataset.si) { try { await api("/api/login", {which: t.dataset.si}); saved.signin = true; $("#wl-simsg", dlg).innerHTML = `${ico("check", "sm")} A sign-in window is opening. Sign in there; it closes by itself when you are done.`; } catch (er) { $("#wl-simsg", dlg).textContent = er.message; } return; }
  if (t.id === "wl-save") {
    const inp = $("#wl-key", dlg), msg = $("#wl-keymsg", dlg); if (!inp.value.trim()) { msg.textContent = "Paste the key first."; return; }
    try { AI = await api("/api/ai/key", {provider: chosen, key: inp.value}); inp.value = ""; window.dispatchEvent(new Event("jw-settings-changed")); draw(); $("#wl-keymsg", dlg).innerHTML = `${ico("check", "sm")} Saved. You can change it any time in Settings → AI &amp; privacy.`; } catch (er) { msg.textContent = er.message; msg.classList.add("bad"); }
    return;
  }
  if (t.id === "wl-bsave") {
    const v = $("#wl-budget", dlg).value; try { AI = await api("/api/ai/config", {budget: {monthly_usd: v}}); toast(v === "" ? "No monthly limit." : `Monthly limit set to $${v}.`); } catch (er) { toast(er.message); }
  }
}

export async function open(at = 0) {
  await load();
  if (!dlg) {
    dlg = document.createElement("dialog"); dlg.className = "card wl"; dlg.setAttribute("aria-label", "Welcome guide");
    document.body.append(dlg);
    dlg.addEventListener("click", onClick);
    dlg.addEventListener("change", e => { if (e.target.id === "wl-prov") { chosen = e.target.value; draw(); } });
    dlg.addEventListener("close", mark);
    dlg.addEventListener("keydown", e => { if (e.key === "Enter" && e.target.id === "wl-key") { e.preventDefault(); $("#wl-save", dlg).click(); } });
  }
  step = Math.max(0, Math.min(pages.length - 1, at)); saved = {};
  draw(); if (!dlg.open) dlg.showModal();
}

window.addEventListener("jw-signins", e => { SIN = e.detail; if (dlg && dlg.open && (step === 2 || step === 4)) { const msg = $("#wl-simsg", dlg) && $("#wl-simsg", dlg).innerHTML; draw(); if (msg && $("#wl-simsg", dlg)) $("#wl-simsg", dlg).innerHTML = msg; } });
window.Welcome = {open};
// the first start: after the page has settled, unless it has been seen (or finished) before
window.addEventListener("load", () => { if (!seen()) setTimeout(() => { if (!document.querySelector("dialog[open]")) open(0); }, 900); });
