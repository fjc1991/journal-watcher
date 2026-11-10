// Settings page: AI providers and routing, privacy and budget, usage and cost, your prompt library, reader preferences.
// Opened from the ⚙ in the top bar (window.Settings.open("ai"|"usage"|"prompts"|"reader")) and from the reader's Ask tab.
import {errBox, errInline, notifyError, explainUpdate, updateFacts, $, $$, esc, api, toast, ico, fileUrl, COLORS, exactCost} from "./state.js";

let root, D = null, tab = "ai", LOG = [];
const FEATURES = [["ask", "Ask the paper", "Questions answered from the paper's passages"], ["explain", "Explain selection", "The one-click prompts on selected text and figures"],
  ["suggest", "Suggested questions", "Ideas for what to ask next — a cheap model is enough"], ["screen", "Screening new papers", "Judges each new paper from its title and abstract (weekly check and archive search). Needs a model that answers in structured JSON: Test checks that. The monthly budget stops it; what is left unscreened is picked up by Retry screening or the next search"],
  ["summarize", "Paper summaries", "The pipeline's summary for each downloaded paper"],
  ["card", "Paper cards", "The structured, cited card of each paper (needs long, reliable JSON output)"], ["events", "Ideas Lab: current events", "Searches the web: pick Claude, OpenAI or OpenRouter"],
  ["ideas", "Ideas Lab: ideas and outlines", "Writes the research ideas and develops them — your strongest model"], ["reviewer", "Reviewer 2: a critical read of your text", "Needs judgement and care — use your strongest model"], ["audio", "Spoken briefings", "Writes the two-minute script that your browser reads aloud — a cheap model is enough"], ["flashcards", "Flashcards", "Writes question-and-answer cards from your paper cards — a cheap model is enough"], ["synthesis", "Synthesis map and gaps", "Reads all the cards of a folder at once — a strong model finds more"], ["claims", "Claim check", "Judges whether your papers support what you wrote — a strong model checks more carefully"], ["draft", "Related-work drafter", "Writes the section from your paper cards — prose matters, so use a strong model"], ["critique", "Ideas Lab: the editor", "Scores each idea for novelty, feasibility, significance and fit"],
  ["folder_ask", "Ask a folder or the library", "Questions answered from passages or cards of several papers"], ["matrix", "Comparison matrix", "Fills your own columns, one call per paper"],
  ["skim", "Skim layer", "Finds the key sentences of a paper by lens (question, finding, limitation…)"]];
const money = v => exactCost(v, "—");
const TABS = [["ai", "AI"], ["watch", "Journals & interests"], ["reader", "Reader & summaries"], ["files", "AI files & backup"], ["library", "Library access"], ["sources", "Paper sources"], ["look", "Appearance"], ["sync", "Computers & sync"], ["about", "About & updates"]];
const AI_SUBS = [["ai", "AI & privacy"], ["usage", "Usage & cost"], ["prompts", "Prompts"]];      // three pages under the one main tab "AI" (their keys are unchanged: Settings.open("usage") still opens Usage & cost)
const groupOf = t => AI_SUBS.some(([k]) => k === t) ? "ai" : t;
let lastAI = "ai";      // the AI page you were on, for when you come back to the AI tab
let dirty = false;      // something on this page was changed and not saved (pages with a Save button; a control that saves at once carries data-now)
let watchChanged = false;      // journals or topics were changed: the Watch page is drawn from them once, so it is loaded again when Settings closes
const leaveOk = () => !dirty || confirm("You have changes on this page that are not saved.\n\nLeave without saving them?");

function build() {
  if (root) return;
  root = document.createElement("div"); root.id = "settings"; root.className = "st hide";
  root.innerHTML = `<div class="st-head"><button class="btn back" id="st-back">← Back</button><h1>Settings</h1><div class="st-tabs">${TABS.map(([k, t]) => `<button data-t="${k}">${t}</button>`).join("")}</div></div><div class="st-sub hide" id="st-sub" aria-label="AI settings">${AI_SUBS.map(([k, t]) => `<button data-t="${k}">${t}</button>`).join("")}</div><div class="st-body" id="st-body"></div>`;
  document.body.append(root);
  $("#st-back", root).onclick = close;
  $$(".st-tabs button", root).forEach(b => b.onclick = () => { if (!leaveOk()) return; tab = b.dataset.t === "ai" ? lastAI : b.dataset.t; render(); });
  $$(".st-sub button", root).forEach(b => b.onclick = () => { if (!leaveOk()) return; tab = b.dataset.t; render(); });
  document.addEventListener("keydown", e => { if (e.key === "Escape" && !root.classList.contains("hide") && !e.target.closest("input,textarea,select")) close(); });
  const touched = e => { if ($("#st-save", root) && !e.target.closest("[data-now]") && e.target.closest("#st-body")) dirty = true; };      // (a page with a Save button: what you type is not kept until you press it)
  root.addEventListener("input", touched); root.addEventListener("change", touched);
  addEventListener("beforeunload", e => { if (dirty && root && !root.classList.contains("hide")) { e.preventDefault(); e.returnValue = ""; } });
}
export function close() {
  if (!root || root.classList.contains("hide")) return;
  if (!leaveOk()) return;
  dirty = false; root.classList.add("hide"); document.body.classList.remove("settings-open");
  if (watchChanged) { watchChanged = false; location.reload(); }      // (the Watch page's journals and topics are drawn once, at its start)
}
export async function open(t) {
  build(); if (t) tab = t;
  if (window.About) About.close();
  root.classList.remove("hide"); document.body.classList.add("settings-open");
  try { D = await api("/api/settings"); } catch (e) { toast(e.message); return; }
  render();
}
function render() {
  if (!D) return;          // a tab pressed before the settings arrived: open() draws the page when they do
  dirty = false;
  if (groupOf(tab) === "ai") lastAI = tab;
  $$(".st-tabs button", root).forEach(b => b.classList.toggle("on", b.dataset.t === groupOf(tab)));
  $("#st-sub", root).classList.toggle("hide", groupOf(tab) !== "ai");
  $$(".st-sub button", root).forEach(b => b.classList.toggle("on", b.dataset.t === tab));
  const on = $(".st-tabs button.on", root); if (on && on.scrollIntoView) on.scrollIntoView({block: "nearest", inline: "nearest"});      // a narrow window scrolls the row: keep the open tab in view
  const body = $("#st-body", root);
  ({ai: renderAI, watch: renderWatch, usage: renderUsage, prompts: renderPrompts, reader: renderReader, files: renderFiles, library: renderLibraryAccess, sources: renderSources, look: renderLook, sync: renderSync, about: renderAbout})[tab](body);
}
const note = (el, msg, bad) => { el.textContent = msg; el.classList.toggle("bad", !!bad); if (!bad && el.id === "st-msg") dirty = false; };      // (a page's "Saved": nothing is unsaved any more)

// ---------------------------------------------------------------- Journals & interests
// Your topics (what papers are screened against), the methods, and the journals: which are read by the weekly check and which are ticked for a search by year.
// Topics are saved with the button (they are checked first); a journal's switch is saved as you flip it.
const PUBNAME = {aisel: "AIS eLibrary", informs: "INFORMS", wiley: "Wiley", tandfonline: "Taylor & Francis", elsevier: "Elsevier", acm: "ACM", aaas: "AAAS", pnas: "National Academy of Sciences",
  nber: "NBER", hbr: "Harvard Business Publishing", ssrn: "SSRN", sage: "SAGE", oup: "Oxford University Press", aaa: "American Accounting Association", aom: "Academy of Management", springer: "Springer"};
const topicRow = (t, kind) => `<div class="wl-topic" data-kind="${kind}" data-name="${esc(t.name || "")}">
    <div class="row" style="gap:8px"><input type="text" class="wl-label" value="${esc(t.label || "")}" placeholder="${kind === "topic" ? "Name of the topic, e.g. Platform economics" : "Name of the method, e.g. Field experiments"}" aria-label="Name" style="flex:1">
      <button class="btn small" data-wlrm title="Remove this ${kind}">Remove</button></div>
    ${kind === "method" ? `<input type="text" class="wl-desc" value="${esc(t.description || "")}" placeholder="One sentence the model reads to recognise it" aria-label="Description" style="width:100%;margin-top:6px">` : ""}
    <textarea class="wl-terms" rows="3" aria-label="Words and phrases" placeholder="Words and phrases to look for, separated by commas or new lines">${esc((t.terms || []).join(", "))}</textarea></div>`;
function journalRows(W) {
  // each journal once: under the first list it belongs to (MISQ and ISR are in both lists); a list's heading still counts all of its journals
  const seen = new Set(), groups = [...Object.entries(W.sets).map(([k, name]) => [k, name, W.journals.filter(j => j.sets.includes(k))]), ["", "Other journals", W.journals.filter(j => !j.sets.length)]].filter(g => g[2].length);
  return groups.map(([k, name, all]) => { const js = all.filter(j => !seen.has(j.abbr)); js.forEach(j => seen.add(j.abbr)); const above = all.length - js.length;
    return `<tr class="wl-group"><th colspan="2">${esc(name)} <small>${all.filter(j => j.weekly).length} of ${all.length} in the weekly check${above ? ` · ${above} of them listed above` : ""}</small></th>
      <th colspan="2"><button class="linkbtn" data-wlall="${esc(k)}" data-on="1">weekly check: all on</button> · <button class="linkbtn" data-wlall="${esc(k)}" data-on="0">all off</button></th></tr>` +
    js.map(j => `<tr data-j="${esc(j.abbr)}"><td><b>${esc(j.name)}</b> <small>${esc(j.abbr)}${j.sets.length > 1 && k ? " · also in the " + esc(j.sets.filter(s => s !== k).map(s => W.sets[s]).join(", ")) : ""}</small></td>
      <td><small>${esc(PUBNAME[j.publisher] || j.publisher)}</small></td>
      <td><label class="chk"><input type="checkbox" data-now data-wlsw="weekly"${j.weekly ? " checked" : ""}${j.abbr === "SSRN" ? " disabled" : ""}> weekly check</label></td>
      <td><label class="chk"><input type="checkbox" data-now data-wlsw="search"${j.search ? " checked" : ""}${j.can_search ? "" : " disabled"}> ticked for a search</label></td></tr>`).join(""); }).join("");
}
async function renderWatch(body) {
  body.innerHTML = `<div class="card st-card"><p class="pmeta">Loading…</p></div>`;
  let W, I;
  try { [W, I] = await Promise.all([api("/api/journals"), api("/api/interests")]); } catch (e) { body.innerHTML = `<div class="card st-card">${errInline(e)}</div>`; return; }
  if (tab !== "watch") return;
  const draw = () => {
    body.innerHTML = `<div class="card st-card"><h2>Your topics <small>what every new paper is compared with</small></h2>
      <p class="pmeta">A paper is kept when its title, keywords or abstract contain enough of a topic's words (a word in the title counts 3, in the keywords 2, in the abstract 1), and the AI then judges it against the same topics. The three topics the app starts with are only examples: replace them with your own.</p>
      <div id="wl-topics">${I.topics.map(t => topicRow(t, "topic")).join("")}</div>
      <div class="row" style="gap:8px;margin-top:8px"><button class="btn small" id="wl-addt">＋ Add a topic</button>
        <label class="chk">A paper needs a score of <input type="number" id="wl-min" min="1" max="20" value="${esc(I.min_score)}" style="width:4.5em"> <small>lower lets more papers through to the AI (and costs more)</small></label></div>
      <h2 style="margin-top:18px">Methods <small>optional: a paper can also be marked by how it was done</small></h2>
      <div id="wl-methods">${I.methods.map(t => topicRow(t, "method")).join("")}</div>
      <div class="row" style="gap:8px;margin-top:8px"><button class="btn small" id="wl-addm">＋ Add a method</button></div>
      <div class="st-actions"><button class="btn primary" id="st-save">Save topics and methods</button> <span id="st-msg" class="st-msg"></span></div></div>
    <div class="card st-card"><h2>Your journals <small>${W.journals.filter(j => j.weekly).length} are read by the weekly check</small></h2>
      <p class="pmeta">“Weekly check” decides whether <b>Check for new papers</b> reads a journal. “Ticked for a search” decides whether it starts ticked when you search by year (you can still tick any journal there). A switch is saved as you flip it. More journals in the weekly check means more papers to screen each week.</p>
      ${W.missing.length ? `<div id="wl-missing"><b>${W.missing.length} journal${W.missing.length === 1 ? "" : "s"} of the AIS basket and the UTD 24 ${W.missing.length === 1 ? "is" : "are"} not in your list</b> <small>(${esc(W.missing.slice(0, 6).map(j => j.abbr).join(", "))}${W.missing.length > 6 ? ", …" : ""})</small>.
        <button class="btn small primary" id="wl-add">Add ${W.missing.length === 1 ? "it" : "them"}, switched off</button> <small>None of your own entries is changed; you switch on the ones you want below.</small></div>` : ""}
      <table class="st-table" id="wl-journals">${journalRows(W)}</table><span class="st-msg" id="wl-jmsg"></span></div>`;
  };
  draw();
  const collect = (box, kind) => $$(`#${box} .wl-topic`, body).map(d => ({name: d.dataset.name, label: $(".wl-label", d).value, terms: $(".wl-terms", d).value, ...(kind === "method" ? {description: $(".wl-desc", d).value} : {})}));
  const done = r => { W = {journals: r.journals, missing: r.missing, sets: r.sets}; watchChanged = true; const y = $("#st-body", root).scrollTop; draw(); $("#st-body", root).scrollTop = y; };
  body.onclick = async e => {
    if (e.target.closest("#wl-addt")) { $("#wl-topics", body).insertAdjacentHTML("beforeend", topicRow({}, "topic")); dirty = true; $("#wl-topics .wl-topic:last-child .wl-label", body).focus(); return; }
    if (e.target.closest("#wl-addm")) { $("#wl-methods", body).insertAdjacentHTML("beforeend", topicRow({}, "method")); dirty = true; $("#wl-methods .wl-topic:last-child .wl-label", body).focus(); return; }
    const rm = e.target.closest("[data-wlrm]"); if (rm) { rm.closest(".wl-topic").remove(); dirty = true; return; }
    if (e.target.closest("#st-save")) {
      const msg = $("#st-msg", body);
      try { const r = await api("/api/interests", {topics: collect("wl-topics", "topic"), methods: collect("wl-methods", "method"), min_score: $("#wl-min", body).value});
        I = r; dirty = false; watchChanged = true; draw(); note($("#st-msg", body), "Saved. The next check and search use them."); }
      catch (er) { msg.innerHTML = errInline(er); }
      return;
    }
    if (e.target.closest("#wl-add")) { try { const r = await api("/api/journals", {action: "add_missing"}); done(r); toast(`${r.added.length} journal${r.added.length === 1 ? "" : "s"} added, switched off.`); } catch (er) { $("#wl-jmsg", body).innerHTML = errInline(er); } return; }
    const all = e.target.closest("[data-wlall]");
    if (all) { const k = all.dataset.wlall, abbrs = W.journals.filter(j => (k ? j.sets.includes(k) : !j.sets.length) && j.abbr !== "SSRN").map(j => j.abbr);
      try { done(await api("/api/journals", {action: "switch", abbrs, weekly: all.dataset.on === "1"})); } catch (er) { $("#wl-jmsg", body).innerHTML = errInline(er); } }
  };
  body.onchange = async e => {
    const sw = e.target.closest("[data-wlsw]"); if (!sw) return;
    try { done(await api("/api/journals", {action: "switch", abbrs: [sw.closest("tr").dataset.j], [sw.dataset.wlsw]: sw.checked})); } catch (er) { sw.checked = !sw.checked; $("#wl-jmsg", body).innerHTML = errInline(er); }
  };
}

// ---------------------------------------------------------------- AI & privacy
function srcText(au) {               // where an automatic price comes from, in words
  const when = au.checked ? new Date(au.checked + "T12:00:00").toLocaleDateString(undefined, {day: "numeric", month: "short", year: "numeric"}) : "";
  const t = au.source === "openrouter" ? `OpenRouter's list${when ? ", read " + when : ""}` : au.source === "built-in" ? `built into the app${when ? ", checked " + when : ""}` : au.source === "free" ? "free (runs on this computer)" : esc(au.source || "");
  return esc(t) + (au.stale ? ` <span class="warn">· possibly out of date</span>` : "");
}
function renderAI(body) {
  const A = D.ai; if (!A) { body.innerHTML = `<div class="card st-card">AI features aren’t installed. Run <code>pip install -r requirements.txt</code>.</div>`; return; }
  const provs = Object.entries(A.providers), pnames = provs.map(([n]) => n);
  const price = m => (A.prices || {})[m] || {};
  const feats = FEATURES.map(([k]) => A.features[k] || A.features.ask), tally = (key, only) => { const c = {}; feats.filter(f => !only || only(f)).forEach(f => { c[f[key]] = (c[f[key]] || 0) + 1; }); return Object.entries(c).sort((a, b) => b[1] - a[1]); };
  const mainP = (tally("provider")[0] || [])[0], mainM = (tally("model", f => f.provider === mainP)[0] || [])[0];      // what most features use now: the one-for-everything row starts there (the model counted only among the features of that provider: another provider's model is no choice for it)
  const mixed = "Now: " + tally("provider").map(([p, n]) => `${p} ×${n}`).join(", ") + ". Applies the provider and model to every feature below (not the effort, not embeddings), then press Save. Any row can still be changed on its own.";
  const used = [...new Set([...Object.values(A.features).map(f => f.model), A.embeddings.provider !== "local" ? A.embeddings.model : null].filter(Boolean))];
  body.innerHTML = `
  <div class="card st-card"><h2>Providers</h2><p class="hint">Paste a key and it is saved ${A.keychain ? "in this Mac’s Keychain (not in your data folder, so a synced folder never carries it to another computer)" : "in the <code>.env</code> file of your data folder (readable by you only)"}; it is never shown again or sent anywhere but to its provider. No restart is needed. “Models you use” is a comma-separated list that fills the model pickers below; Test tries the first one.</p>
    <div class="st-scroll"><table class="st-table st-prov"><colgroup><col class="c-prov"><col class="c-key"><col class="c-flag"><col class="c-flag"><col class="c-models"><col class="c-test"></colgroup>
    <tr><th>Provider</th><th>Key</th><th class="c">On this computer</th><th class="c">Web search</th><th>Models you use</th><th></th></tr>
    ${provs.map(([n, p]) => `<tr data-p="${esc(n)}"><td><b>${esc(n)}</b></td><td>${!p.key_env ? `<div class="keycell"><span class="ok">no key needed</span></div>` : `<div class="keycell">${p.configured ? `<span class="ok keyok">✓ key found${p.key_where ? ` <small class="kw">· ${esc(p.key_where)}</small>` : ""}</span><button class="btn small" data-keychange="${esc(n)}">Change</button><button class="btn small" data-keyremove="${esc(n)}">Remove</button>` : ""}<span class="keyform${p.configured ? " hide" : ""}"><input type="password" autocomplete="off" spellcheck="false" aria-label="${esc(n)} API key (${esc(p.key_env)})" placeholder="paste your ${esc(p.key_env)}"><button class="btn small primary" data-savekey="${esc(n)}">Save key</button></span></div><div class="tres keymsg"></div>`}</td>
      <td class="c">${p.local ? "✓" : "—"}</td><td class="c">${p.web_search ? "✓" : "—"}</td><td><input type="text" class="pm" aria-label="Models of ${esc(n)}, separated by commas" value="${esc((p.models || []).join(", "))}"></td>
      <td class="c-test"><button class="btn small" data-test="${esc(n)}">Test</button><div class="tres"></div></td></tr>`).join("")}</table></div></div>
  <div class="card st-card"><h2>Which model does what</h2>
    <div class="st-main" id="st-main"><b>Use one for everything</b>
      <select id="mn-p" aria-label="Provider for all features">${pnames.map(n => `<option${mainP === n ? " selected" : ""}>${esc(n)}</option>`).join("")}</select>
      <input type="text" id="mn-m" list="dl-main" aria-label="Model for all features" value="${esc(mainM || "")}"><datalist id="dl-main">${((A.providers[mainP] || {}).models || []).map(m => `<option value="${esc(m)}">`).join("")}</datalist>
      <button class="btn small" id="mn-test">Test</button><span class="tres"></span>
      <button class="btn" id="mn-apply">Apply to all features</button>
      <small id="mn-note" class="st-msg">${mixed}</small></div>
    <div class="st-scroll"><table class="st-table st-feat"><colgroup><col class="c-feat"><col class="c-fp"><col class="c-fm"><col class="c-fe"><col class="c-test"></colgroup>
    <tr><th>Feature</th><th>Provider</th><th>Model</th><th>Effort</th><th></th></tr>
    ${FEATURES.map(([k, t, d]) => { const f = A.features[k] || A.features.ask; return `<tr data-f="${k}"><td><b>${t}</b><br><small>${d}</small></td>
      <td><select class="fp">${pnames.map(n => `<option${f.provider === n ? " selected" : ""}>${esc(n)}</option>`).join("")}</select></td>
      <td><input type="text" class="fm" list="dl-${k}" aria-label="Model for ${esc(k)}" value="${esc(f.model)}"><datalist id="dl-${k}">${((A.providers[f.provider] || {}).models || []).map(m => `<option value="${esc(m)}">`).join("")}</datalist></td>
      <td><select class="fe">${["low", "medium", "high"].map(e => `<option${(f.effort || "low") === e ? " selected" : ""}>${e}</option>`).join("")}</select></td>
      <td class="c-test"><button class="btn small" data-testf="${k}">Test</button><div class="tres"></div></td></tr>`; }).join("")}
    <tr data-f="emb"><td><b>Embeddings</b><br><small>Used to find relevant passages</small></td><td><select class="fp">${["local", ...pnames].map(n => `<option${A.embeddings.provider === n ? " selected" : ""}>${esc(n)}</option>`).join("")}</select></td>
      <td><input type="text" class="fm" aria-label="Embedding model" value="${esc(A.embeddings.model)}"></td><td></td><td><small>“local” runs on this Mac, free</small></td></tr></table></div></div>
  <div class="card st-card"><h2>Privacy and spending</h2>
    <label class="chk"><input type="checkbox" id="st-local"${A.privacy.local_only ? " checked" : ""}> <b>Local-only mode</b> — never send paper text to a provider outside this computer (only Ollama works; Ask and Explain need an Ollama model)</label>
    <label class="chk">Monthly budget (USD) <input type="number" id="st-budget" min="0" step="1" value="${A.budget.monthly_usd || ""}" placeholder="no limit" style="width:110px"> <small>new paid calls stop when the month's spend reaches it (answers already saved and free models on this computer still work); a warning shows at 80%</small></label>
    <label class="chk">Reuse earlier screening verdicts for <input type="number" id="st-reuse" min="0" max="3650" step="1" value="${(A.screening || {}).reuse_days ?? 90}" style="width:80px"> days <small>0 = always ask the AI again. A verdict is reused only for the same paper text and the same question (topics, method and focus phrase), so searching again does not pay for papers it already judged</small></label>
    <label class="chk"><input type="checkbox" id="st-keep"${(A.ask || {}).keep_paper ? " checked" : ""}> <b>Whole-paper questions: keep the paper ready between questions (Claude)</b> <small>The first question on a paper then costs about a quarter more on the paper's text; each further question within 5 minutes costs about a tenth of it. Later than 5 minutes, the quarter is paid again. Off: every question pays for the whole paper, as before.</small></label></div>
  <div class="card st-card"><h2>Prices <small>USD per million tokens — for cost figures and the budget</small></h2>
    <table class="st-table" id="st-prices"><tr><th>Model</th><th>Input</th><th>Output</th><th>Cache write</th><th>Cache read</th><th>Where the price comes from</th></tr>
    ${used.map(m => { const au = (A.price_auto || {})[m] || {}, mine = Object.keys(price(m)).length > 0;
      const src = mine ? "yours" + (au.source ? ` <small>(without it: ${srcText(au)})</small>` : "") : au.source ? srcText(au) : `<span class="warn">not known — type it, or calls count as “price unknown”</span>`;
      return `<tr data-m="${esc(m)}"><td>${esc(au.name && au.name !== m ? au.name + " · " : "")}<code>${esc(m)}</code></td>${["in", "out", "cache_write", "cache_read"].map(k => `<td><input type="number" step="0.01" min="0" data-k="${k}" aria-label="${esc(m)}: ${({in: "input", out: "output", cache_write: "cache write", cache_read: "cache read"})[k]} price per million tokens" value="${price(m)[k] ?? ""}" placeholder="${au.source && au[k] != null ? +(+au[k]).toFixed(4) : ""}" style="width:90px"></td>`).join("")}<td class="psrc">${src}</td></tr>`; }).join("")}</table>
    <p class="hint">A price you type always wins; empty boxes use the price shown in grey. OpenRouter models get OpenRouter's current price by themselves (read once a day from openrouter.ai: nothing about you or your papers is sent); Claude models a price list built into the app; a model on this computer is free. Clear a row to go back to the automatic price. Without any price a model's calls are counted as “price unknown”, and the run's cost says “at least”.</p></div>
  <div class="st-actions"><button class="btn primary" id="st-save">Save</button> <span id="st-msg" class="st-msg"></span></div>`;
  const msg = $("#st-msg", body);
  const WEB_ONLY = ["events"];          // the Ideas Lab's current events searches the web: only some providers can, so "everything" leaves it alone on one that cannot
  $$("tr[data-f] .fp", body).forEach(sel => sel.onchange = () => { const tr = sel.closest("tr"), dl = $("datalist", tr); if (dl) dl.innerHTML = ((A.providers[sel.value] || {}).models || []).map(m => `<option value="${esc(m)}">`).join(""); });
  // feature "screen": the test is a small screening request with the real JSON schema (a model can answer questions and still be unable to screen: a summariser, say).
  // both: the plain test, then the screening one, for "Use one for everything", whose model would also screen.
  const test = async (btn, provider, model, feature, both) => { const out = btn.parentElement.querySelector(".tres"); note(out, "…"); btn.disabled = true;
    const ask = f => api("/api/ai/test", {provider, model, ...(f ? {feature: f} : {})}).catch(e => ({ok: false, error: e.message}));
    const r = await ask(feature), s = both && r.ok ? await ask("screen") : null; btn.disabled = false;
    const ok = x => `✓ ${x.ms} ms${x.cost != null ? " · " + money(x.cost) : ""}`;
    if (!r.ok) return note(out, r.error + (feature === "screen" ? " Choose another model for screening." : ""), true);
    if (s && !s.ok) return note(out, `${ok(r)}, but it ${s.error.replace(/^Can’t/, "can’t")} Set “Screening new papers” to another model.`, true);
    note(out, ok(r) + (feature === "screen" || s ? " · screens papers" : "")); };
  const mp = $("#mn-p", body), mm = $("#mn-m", body), mnote = $("#mn-note", body);
  mp.onchange = () => { const ms = (A.providers[mp.value] || {}).models || []; $("#dl-main", body).innerHTML = ms.map(m => `<option value="${esc(m)}">`).join(""); if (ms.length && !ms.includes(mm.value)) mm.value = ms[0]; };
  $("#mn-test", body).onclick = e => { if (!mm.value.trim()) return note(mnote, "Type a model first.", true); test(e.currentTarget, mp.value, mm.value.trim(), null, true); };
  $("#mn-apply", body).onclick = () => {
    const p = mp.value, m = mm.value.trim(); if (!m) return note(mnote, "Type a model first.", true);
    let n = 0; const left = [];
    $$("tr[data-f]", body).forEach(tr => { const k = tr.dataset.f; if (k === "emb") return;
      if (WEB_ONLY.includes(k) && !(A.providers[p] || {}).web_search) { left.push($("b", tr).textContent); return; }
      const fp = $(".fp", tr); fp.value = p; fp.dispatchEvent(new Event("change")); $(".fm", tr).value = m; n++; });
    note(mnote, `Set ${n} features to ${p} · ${m}. Press Save to keep it.` + (left.length ? ` Left alone: ${left.join(", ")} (it needs a provider that can search the web).` : ""));
  };
  $$("[data-keyremove]", body).forEach(b => b.onclick = async () => {
    const n = b.dataset.keyremove;
    if (!confirm(`Remove the ${n} API key from this computer?\n\nIt is deleted from this Mac's Keychain and from the .env file of your data folder. Features that use ${n} stop working until you paste a key again.`)) return;
    try { D.ai = await api("/api/ai/key", {provider: n, remove: true}); window.dispatchEvent(new Event("jw-settings-changed")); renderAI(body); toast(D.ai.key_note || "Key removed."); }
    catch (e) { notifyError(e); }
  });
  $$("[data-keychange]", body).forEach(b => b.onclick = () => { const f = $(".keyform", b.closest("td")); f.classList.remove("hide"); $("input", f).focus(); b.classList.add("hide"); const ok = $(".keyok", b.closest("td")); if (ok) ok.classList.add("hide"); const rm = $("[data-keyremove]", b.closest("td")); if (rm) rm.classList.add("hide"); });
  $$("[data-savekey]", body).forEach(b => b.onclick = async () => { const td = b.closest("td"), inp = $("input", td), out = $(".keymsg", td); if (!inp.value.trim()) return;
    try { D.ai = await api("/api/ai/key", {provider: b.dataset.savekey, key: inp.value}); inp.value = ""; window.dispatchEvent(new Event("jw-settings-changed")); renderAI(body); toast(D.ai.key_note ? "Key saved. " + D.ai.key_note : "Key saved in the .env file of your data folder."); } catch (e) { note(out, e.message, true); } });
  $$("[data-test]", body).forEach(b => b.onclick = () => { const tr = b.closest("tr"), first = $(".pm", tr).value.split(",")[0].trim(); const m = first || prompt("Model name to test on " + b.dataset.test + ":"); if (m) test(b, b.dataset.test, m); });
  $$("[data-testf]", body).forEach(b => b.onclick = () => { const tr = b.closest("tr"); test(b, $(".fp", tr).value, $(".fm", tr).value.trim(), tr.dataset.f === "screen" ? "screen" : null); });
  $("#st-save", body).onclick = async () => {
    const features = {}; $$("tr[data-f]", body).forEach(tr => { const k = tr.dataset.f; if (k === "emb") return; features[k] = {provider: $(".fp", tr).value, model: $(".fm", tr).value.trim(), effort: $(".fe", tr).value}; });
    const provider_models = {}; $$("tr[data-p]", body).forEach(tr => { provider_models[tr.dataset.p] = $(".pm", tr).value.split(",").map(s => s.trim()).filter(Boolean); });
    const prices = {}; $$("tr[data-m]", body).forEach(tr => { const v = {}; let any = false; $$("input", tr).forEach(i => { v[i.dataset.k] = i.value === "" ? 0 : +i.value; any = any || i.value !== ""; }); prices[tr.dataset.m] = any ? v : null; });      // (an empty row: back to the automatic price)
    const emb = $('tr[data-f="emb"]', body);
    try {
      D.ai = await api("/api/ai/config", {features, provider_models, prices, embeddings: {provider: $(".fp", emb).value, model: $(".fm", emb).value.trim()},
        privacy: {local_only: $("#st-local", body).checked}, budget: {monthly_usd: $("#st-budget", body).value}, screening: {reuse_days: $("#st-reuse", body).value}, ask: {keep_paper: $("#st-keep", body).checked}});
      note(msg, "Saved."); window.dispatchEvent(new Event("jw-settings-changed"));
    } catch (e) { note(msg, e.message, true); }
  };
}

// ---------------------------------------------------------------- usage & cost
async function renderUsage(body) {
  if (!D.usage) { body.innerHTML = `<div class="card st-card">AI features aren’t installed.</div>`; return; }
  const [log, U] = await Promise.all([api("/api/ai/log?limit=40").catch(() => []), api("/api/ai/usage").catch(() => D.usage)]);      // (the figures are read again each time: the budget may have been changed on the AI page a moment ago, and what is left follows it)
  if (tab !== "usage") return;      // (another tab was pressed while these were on their way: do not draw over it)
  LOG = log; D.usage = U;
  const bud = U.budget, pct = bud ? Math.min(100, 100 * U.month.cost / bud) : 0, days = Array.from({length: 30}, (_, i) => { const d = new Date(Date.now() - (29 - i) * 864e5).toISOString().slice(0, 10); return [d, U.by_day[d] || 0]; }), max = Math.max(0.0001, ...days.map(d => d[1])), spent = days.some(d => d[1] > 0);
  body.innerHTML = `
  <div class="st-cards">
    <div class="card st-stat"><span>This month</span><b>${U.month.unpriced ? "at least " : ""}${money(U.month.cost)}</b><small>${U.month.calls} calls${bud ? ` · budget ${money(bud)} · ${money(U.left)} left` : ""}</small>${bud ? `<div class="st-bar"><i class="${U.warn ? "warn" : ""}" style="width:${pct}%"></i></div>` : ""}</div>
    <div class="card st-stat"><span>All time</span><b>${money(U.cost)}</b><small>${U.calls} calls</small></div>
    <div class="card st-stat"><span>Answered from cache</span><b>${U.cached}</b><small>free repeats · ${D.cache ? D.cache.entries + " entries, " + Math.round(D.cache.bytes / 1024) + " KB" : ""}</small></div>
    <div class="card st-stat"><span>Calls without a price</span><b>${U.unpriced}</b><small>add prices in Settings → AI</small></div></div>
  ${U.warn ? `<div class="card st-card warnbox">You have used ${Math.round(pct)}% of this month's budget.</div>` : ""}
  ${U.month.unpriced ? `<div class="card st-card warnbox" id="st-unpriced">${U.month.unpriced} call${U.month.unpriced === 1 ? "" : "s"} this month had no known price, so the real total is higher than ${money(U.month.cost)}${bud ? `, and the budget cannot count ${U.month.unpriced === 1 ? "it" : "them"}` : ""}. Add the model’s price in Settings → AI → Prices, and the calls after that are counted.</div>` : ""}
  <div class="card st-card"><h2>Last 30 days</h2><div class="st-chart">${days.map(([d, v], i) => `<div title="${d}: ${money(v)}"><i class="${v ? "" : "zero"}" style="height:${v ? Math.max(4, 100 * v / max) : 2}%"></i><small>${i % 5 === 4 || i === 0 ? d.slice(5) : ""}</small></div>`).join("")}</div>${spent ? "" : `<div class="hint">No spend in the last 30 days.</div>`}</div>
  <div class="st-two"><div class="card st-card"><h2>By feature</h2><table class="st-table"><tr><th>Feature</th><th>Calls</th><th>Tokens in/out</th><th>Cost</th></tr>${Object.entries(U.by_feature).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${v.calls}</td><td>${v.in.toLocaleString()} / ${v.out.toLocaleString()}</td><td>${money(v.cost)}</td></tr>`).join("")}</table></div>
  <div class="card st-card"><h2>By provider</h2><table class="st-table"><tr><th>Provider</th><th>Calls</th><th>Cost</th></tr>${Object.entries(U.by_provider).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${v.calls}</td><td>${money(v.cost)}</td></tr>`).join("")}</table></div></div>
  <div class="card st-card"><h2>What was sent <small>the most recent calls; images and PDFs are counted, not stored</small></h2>
    <table class="st-table log"><tr><th>When</th><th>Feature</th><th>Model</th><th>Sent</th><th>Cost</th></tr>${LOG.map(r => `<tr><td>${esc((r.ts || "").replace("T", " ").slice(5, 16))}</td><td>${esc(r.feature)}${r.cached ? " <span class='pill'>cached</span>" : ""}${r.cancelled ? " <span class='pill' title='You cancelled this call. The provider reports exact usage only when an answer ends, so this is an estimate.'>cancelled · estimated</span>" : ""}${r.searches ? " 🌐" : ""}</td><td>${esc(r.provider)} · ${esc(r.model)}</td>
      <td title="${esc(r.preview || "")}">${r.chars ? (r.chars / 1000).toFixed(1) + "k chars: " : ""}<i>${esc((r.preview || "").slice(0, 80))}</i></td><td>${money(r.cost)}</td></tr>`).join("") || `<tr><td colspan="5" class="hint">Nothing yet.</td></tr>`}</table></div>
  <div class="st-actions"><button class="btn" id="st-clear">Clear the answer cache</button> <span id="st-msg" class="st-msg"></span></div>`;
  $("#st-clear", body).onclick = async () => { const r = await api("/api/ai/cache/clear", {}); note($("#st-msg", body), `Removed ${r.removed} cached answers.`); D.cache = {entries: 0, bytes: 0}; };
}

// ---------------------------------------------------------------- prompts + summary templates (shared editor)
function editor(items, kind) {
  return items.map((p, i) => `<div class="card st-item" data-i="${i}"><div class="st-row"><input type="text" class="pn" value="${esc(p.name)}" placeholder="Name">
    ${kind === "prompt" ? `<select class="ps">${["selection", "area"].map(s => `<option value="${s}"${p.scope === s ? " selected" : ""}>${s === "selection" ? "on selected text" : "on a figure / table"}</option>`).join("")}</select>
    <label class="chk"><input type="checkbox" class="pp"${p.pinned ? " checked" : ""}> shown in the toolbar</label>` : ""}
    <span style="flex:1"></span><button class="btn small" data-up="${i}" title="Move up">↑</button><button class="btn small" data-down="${i}" title="Move down">↓</button><button class="btn small danger" data-del="${i}">Delete</button></div>
    <textarea class="pt" rows="${kind === "prompt" ? 4 : 8}">${esc(p.prompt)}</textarea></div>`).join("");
}
function collect(body, kind, id) {
  return $$(`${id} .st-item`, body).map(el => ({id: "", name: $(".pn", el).value, prompt: $(".pt", el).value, ...(kind === "prompt" ? {scope: $(".ps", el).value, pinned: $(".pp", el).checked} : {})}))
    .map((x, i) => ({...x, id: ((kind === "prompt" ? D.prompts : D.summaries)[i] || {}).id || x.name}));
}
function wireEditor(body, getList, kind, id, redraw) {
  const sync = () => { const c = collect(body, kind, id), list = getList(); c.forEach((x, i) => Object.assign(list[i], x)); };
  body.addEventListener("click", e => { const b = e.target.closest(`${id} [data-up],${id} [data-down],${id} [data-del]`); if (!b) return;
    sync(); const list = getList(); const i = +(b.dataset.up ?? b.dataset.down ?? b.dataset.del);
    if (b.dataset.del !== undefined) { if (list.length > 1 || kind === "prompt") list.splice(i, 1); else return toast("Keep at least one template."); }
    else { const j = b.dataset.up !== undefined ? i - 1 : i + 1; if (j < 0 || j >= list.length) return; [list[i], list[j]] = [list[j], list[i]]; }
    redraw(); });
}
function renderPrompts(body) {
  const draw = () => {
    body.innerHTML = `<div class="card st-card"><h2>One-click prompts</h2><p class="hint">These appear when you select text (✨) or draw a box around a figure or table in the reader. Placeholders:
      <code>{{text}}</code> the selection · <code>{{context}}</code> the paragraph around it · <code>{{title}}</code> · <code>{{authors}}</code> · <code>{{year}}</code>. Answers are cached, so asking the same thing twice is free.</p>
      <div id="pl">${editor(D.prompts, "prompt")}</div><button class="btn" id="pa">＋ Add a prompt</button></div>
      <div class="st-actions"><button class="btn primary" id="st-save">Save prompts</button> <span id="st-msg" class="st-msg"></span></div>`;
    $("#pa", body).onclick = () => { D.prompts.push({id: "", name: "New prompt", scope: "selection", pinned: true, prompt: "Explain this passage:\n{{text}}"}); draw(); };
    $("#st-save", body).onclick = async () => { try { const r = await api("/api/settings", {prompts: collect(body, "prompt", "#pl")}); D.prompts = r.prompts; note($("#st-msg", body), "Saved."); window.dispatchEvent(new Event("jw-settings-changed")); } catch (e) { note($("#st-msg", body), e.message, true); } };
  };
  if (!body._wired) { body._wired = true; wireEditor(body, () => D.prompts, "prompt", "#pl", () => renderPrompts(body)); }
  draw();
}

// ---------------------------------------------------------------- reader preferences, colour legend, summary templates
// ---------------------------------------------------------------- AI files & backup
const delHtml = D => { const xs = (D && D.kept) || []; return xs.length ? `<table class="st-table"><tr><th>Deleted</th><th>What</th><th>Kept for</th><th></th></tr>${xs.map(x => `<tr><td>${esc(x.when)}</td><td>${esc(x.title)}${x.kind === "book" ? ` <span class="pill">book</span>` : ""}</td><td>${x.days_left} more day${x.days_left === 1 ? "" : "s"}</td><td><button class="btn small" data-undel="${esc(x.token)}">Put back</button></td></tr>`).join("")}</table>` : `<p class="pmeta">Nothing: papers and books you delete are kept here for 30 days, with their notes and highlights, and can be put back.</p>`; };
const snapHtml = S => { const xs = (S && S.snapshots) || []; return xs.length ? `<table class="st-table"><tr><th>Taken</th><th>Size</th><th></th></tr>${xs.map(x => `<tr><td>${esc(x.when)}${x.before_restore ? ` <span class="pill" title="What you had just before a restore">before a restore</span>` : ""}</td><td>${bytes(x.bytes)}</td><td><button class="btn small" data-snap="${esc(x.name)}" data-when="${esc(x.when)}">Restore…</button></td></tr>`).join("")}</table>` : `<p class="pmeta">None yet: the first is taken the next time the app starts (or press the button).</p>`; };
const bytes = n => n < 1024 ? n + " B" : n < 1048576 ? Math.round(n / 1024) + " KB" : n < 1073741824 ? (n / 1048576).toFixed(n < 10485760 ? 1 : 0) + " MB" : (n / 1073741824).toFixed(1) + " GB";
// ---- Check my PDFs: is each PDF the paper it is filed under? (identity.py; the server reads them in the background and keeps the answer until the app closes)
let PC = null;
function pcHtml() {
  const c = PC && PC.counts || {}, run = PC && PC.state === "running", done = PC && PC.state === "done";
  const total = Object.values(c).reduce((a, n) => a + n, 0), bad = (PC && PC.results || []).filter(r => r.verdict === "mismatch"), odd = (PC && PC.results || []).filter(r => r.verdict !== "mismatch");
  const row = (r, rm) => `<div class="pc-row" data-f="${esc(r.file)}"><b>${esc(r.title || r.file)}</b>${r.doi ? ` <small>${esc(r.doi)}</small>` : ""}<div class="pmeta" style="margin:2px 0 6px">${esc(r.reason)}</div>
      <div class="row" style="gap:8px;flex-wrap:wrap">${r.verdict === "missing" ? "" : `<a class="btn small" href="${fileUrl(r.file)}" target="_blank" rel="noopener">Look at the PDF</a>`}${r.verdict === "missing" ? "" : `<button class="btn small" data-pc-keep="${esc(r.file)}">It is the right paper</button>`}${rm ? `<button class="btn small danger" data-pc-rm="${esc(r.file)}">Remove and download again</button>` : ""}</div></div>`;
  return `<div class="row" style="gap:10px;margin-top:8px"><button class="btn primary" id="pc-go"${run ? " disabled" : ""}>${done ? "Check again" : "Check my PDFs"}</button><span class="pmeta" id="pc-prog" role="status" aria-live="polite" style="margin:0">${run ? `Checking ${PC.done} of ${PC.total || "…"}…` : ""}</span></div>
    ${PC && PC.state === "error" ? `<p class="hint bad">${esc(PC.error)}</p>` : ""}
    ${done ? `<p class="${bad.length ? "" : "hint ok"}" style="margin:10px 0 4px">${bad.length ? "" : "✓ "}${total} PDF${total === 1 ? "" : "s"} checked: <b>${bad.length}</b> do${bad.length === 1 ? "es" : ""} not look like the paper ${bad.length === 1 ? "it is" : "they are"} filed under, ${odd.length} could not be checked, ${(c.match || 0) + (c.kept || 0)} match${(c.kept || 0) ? ` (${c.kept} you marked as right)` : ""}.${bad.length ? "" : " (Only what it could read is vouched for: see the ones it could not check.)"}</p>
      ${bad.map(r => row(r, true)).join("")}
      ${odd.length ? `<details style="margin-top:8px"><summary class="hint" style="cursor:pointer">Could not be checked (${odd.length}): scans with no text, files it could not read, no title on record</summary>${odd.map(r => row(r, false)).join("")}</details>` : ""}` : ""}`;
}
async function pcRefresh(body) {
  try { PC = await api("/api/library/check"); } catch (e) { PC = {state: "error", error: e.message, results: [], counts: {}}; }
  const box = $("#pc-res", body); if (box && body.isConnected) box.innerHTML = pcHtml();
  if (PC.state === "running" && body.isConnected && $("#pc-res", body)) setTimeout(() => pcRefresh(body), 700);
}
async function pcClick(body, e) {
  const go = e.target.closest("#pc-go"), keep = e.target.closest("[data-pc-keep]"), rm = e.target.closest("[data-pc-rm]");
  const paint = () => { const box = $("#pc-res", body); if (box) box.innerHTML = pcHtml(); };
  if (go) { try { PC = await api("/api/library/check", {}); } catch (er) { notifyError(er); return; } paint(); pcRefresh(body); }
  else if (keep) { try { PC = await api("/api/library/check/keep", {file: keep.dataset.pcKeep}); paint(); } catch (er) { notifyError(er); } }
  else if (rm) {
    const r = (PC.results || []).find(x => x.file === rm.dataset.pcRm); if (!r) return;
    if (!confirm(`Remove this PDF from your library?\n\n${r.title}\n\n${r.reason}\n\nThe PDF and its notes, highlights and summary are moved to Papers/_removed/ (nothing is deleted), and Journal Watcher downloads the paper again. The new file is checked the same way before it is kept.`)) return;
    try { const x = await api("/api/library/check/remove", {file: rm.dataset.pcRm}); PC = x; paint(); toast(x.download ? "Moved aside. Downloading the paper again… watch it under Watch → Past runs." : "Moved aside. Download the paper again from its page."); window.dispatchEvent(new Event("jw-library-changed")); }
    catch (er) { notifyError(er); }
  }
}
async function renderFiles(body) {
  body.innerHTML = `<div class="card st-card"><span class="ask-status">Counting files…</span></div>`;
  let R; try { R = await api("/api/aifiles"); R.snaps = await api("/api/snapshots").catch(() => null); R.del = await api("/api/library/deleted").catch(() => null); } catch (e) { body.innerHTML = `<div class="card st-card">${errBox(e)}</div>`; return; }
  const draw = () => {
    const total = R.kinds.reduce((a, k) => a + k.bytes, 0), b = R.backup, pdfs = $("#af-pdfs", body) ? $("#af-pdfs", body).checked : false;
    body.innerHTML = `<div class="card st-card" id="pc-card"><h2>Check my PDFs</h2>
      <p class="pmeta">Opens the first pages of every PDF in your library and checks that each one is the paper it is filed under (its DOI, or its title and authors). It changes nothing by itself: for each PDF that is not, you decide.</p><div id="pc-res">${pcHtml()}</div></div>
    <div class="card st-card"><h2>Back up my data</h2>
      <p class="pmeta">A zip of everything that is yours: highlights and notes, paper cards, skims, conversations, folders with their matrices and idea boards, tags, reading status, the library index, your prompts and settings (no API keys). ${bytes(b.data_bytes)} in ${b.files} files.</p>
      <label class="chk"><input type="checkbox" id="af-pdfs"${pdfs ? " checked" : ""}> Include the PDFs too (${bytes(b.pdf_bytes)}; they stay where they are in <code>Papers/</code> either way)</label>
      <div class="row" style="gap:8px;margin-top:8px"><button class="btn primary" id="af-dl">Download the backup (.zip)</button>${R.export_dir ? `<button class="btn" id="af-save" title="${esc(R.export_dir)}">Save it into my export folder</button>` : `<span class="pmeta">Set an export folder in “Reader &amp; summaries” to save it there directly.</span>`}<span class="st-msg" id="af-msg"></span></div></div>
    <div class="card st-card" id="snap-card"><h2>Daily snapshots <small>on this computer</small></h2>
      <p class="pmeta">Once a day, when the app starts, a copy of everything that is yours (not the PDFs) is kept on this computer, outside the synced folder: the last ${(R.snaps || {}).keep || 14}. Restoring one puts your notes, folders, library index and settings back as they were then; what you have now is kept first, so a restore can be undone.</p>
      <div id="snap-list">${snapHtml(R.snaps)}</div>
      <div class="row" style="gap:8px;margin-top:8px"><button class="btn small" id="snap-now">Take one now</button><span class="st-msg" id="snap-msg"></span></div></div>
    <div class="card st-card" id="del-card"><h2>Recently deleted <small>kept for ${(R.del || {}).days || 30} days</small></h2>
      <div id="del-list">${delHtml(R.del)}</div><span class="st-msg" id="del-msg"></span></div>
    <div class="card st-card"><h2>What the AI has written or saved <small>${bytes(total)} in all</small></h2>
      <p class="pmeta">Your highlights and notes are never listed here and are never touched. Anything that is yours or hard to make again is zipped before it is cleared, and can be put back below.</p>
      <table class="st-table af"><tr><th>Kind</th><th>Files</th><th>Size</th><th></th></tr>${R.kinds.map(k => `<tr data-k="${k.id}"><td><b>${esc(k.name)}</b> ${k.human ? `<span class="pill" title="May contain lines you edited, cells you wrote or stars and notes you added">yours</span>` : k.regen ? `<span class="pill" title="Made again by itself, for free or nearly">comes back</span>` : ""}<div class="pmeta">${esc(k.what)}${k.note ? ` <b>${esc(k.note)}.</b>` : ""}</div></td>
        <td>${k.count}</td><td>${bytes(k.bytes)}</td><td><button class="btn small" data-clear="${k.id}"${k.count ? "" : " disabled"}>Clear…</button></td></tr>`).join("")}</table></div>
    ${R.trash.length ? `<div class="card st-card"><h2>Recently cleared</h2><table class="st-table"><tr><th>When</th><th>What</th><th>Files</th><th>Size</th><th></th></tr>${R.trash.map(t => `<tr><td>${esc(t.when)}</td><td>${esc((R.kinds.find(k => k.id === t.kind) || {name: t.kind}).name)}</td><td>${t.files}</td><td>${bytes(t.bytes)}</td><td><button class="btn small" data-restore="${esc(t.name)}">Put back</button></td></tr>`).join("")}</table>
      <p class="pmeta">Kept in <code>.agents/skills/ais-journal-watcher/state/trash/</code> (the twelve newest). “Put back” never overwrites a file that exists again.</p></div>` : ""}
    ${R.mcp ? `<div class="card st-card"><h2>Use your library from Claude <small>MCP, read-only</small></h2>
      <p class="pmeta">Lets Claude Desktop or Claude Code search your papers, read their summaries, cards and text, and see your highlights — nothing is written, no AI is called by it, and nothing leaves this computer except what Claude reads when you ask it a question.</p>
      <div class="pmeta"><b>Claude Code</b> (one line in a terminal):</div><pre class="mcp-pre" id="mcp-cc">claude mcp add${Object.entries(R.mcp.env || {}).map(([k, v]) => ` --env ${k}="${esc(v)}"`).join("")} journal-watcher -- ${[R.mcp.python, ...R.mcp.args].map(a => `"${esc(a)}"`).join(" ")}</pre>
      <div class="pmeta"><b>Claude Desktop</b> (add to <code>claude_desktop_config.json</code>, then restart it):</div><pre class="mcp-pre" id="mcp-cd">${esc(JSON.stringify({mcpServers: {"journal-watcher": {command: R.mcp.python, args: R.mcp.args, ...(Object.keys(R.mcp.env || {}).length ? {env: R.mcp.env} : {})}}}, null, 2))}</pre>
      <div class="row" style="gap:8px"><button class="btn small" data-mcpcopy="mcp-cc">Copy the Claude Code line</button><button class="btn small" data-mcpcopy="mcp-cd">Copy the Claude Desktop config</button></div></div>` : ""}`;
  };
  draw();
  if (PC && PC.state === "running") pcRefresh(body); else api("/api/library/check").then(x => { if (!PC || PC.state !== "running") { PC = x; const box = $("#pc-res", body); if (box && body.isConnected) box.innerHTML = pcHtml(); if (x.state === "running") pcRefresh(body); } }).catch(() => {});
  body.onclick = async e => {
    if (e.target.closest("#pc-card")) return pcClick(body, e);
    const m = $("#af-msg", body), c = e.target.closest("[data-clear]"), rs = e.target.closest("[data-restore]"), mc = e.target.closest("[data-mcpcopy]");
    if (mc) { try { await navigator.clipboard.writeText($("#" + mc.dataset.mcpcopy, body).textContent); toast("Copied."); } catch (er) { notifyError(er); } return; }
    const ub = e.target.closest("[data-undel]");
    if (ub) {
      ub.disabled = true;
      try { const r = await api("/api/library/restore", {tokens: [ub.dataset.undel]}); R.del = {kept: r.kept, days: (R.del || {}).days}; $("#del-list", body).innerHTML = delHtml(R.del);
        $("#del-msg", body).textContent = r.errors.length ? r.errors[0].error : `Put back “${(r.restored[0].title || "").slice(0, 60)}”.`; window.dispatchEvent(new Event("jw-library-changed")); }
      catch (er) { $("#del-msg", body).innerHTML = errInline(er); ub.disabled = false; }
      return;
    }
    const sr = e.target.closest("[data-snap]");
    if (sr) {
      const n = sr.dataset.snap, when = sr.dataset.when;
      if (!confirm(`Put your notes, highlights, folders, tags, reading status, library index and settings back as they were on ${when}?\n\nWhat you have now is kept first in a snapshot of its own, so you can undo this. PDFs are not touched.`)) return;
      sr.disabled = true;
      try { const r = await api("/api/snapshots/restore", {name: n}); toast(`Restored ${r.restored} files as of ${when}. Reloading…`); setTimeout(() => location.reload(), 1200); }
      catch (er) { const sm = $("#snap-msg", body); if (sm) sm.innerHTML = errInline(er); sr.disabled = false; }
      return;
    }
    if (e.target.closest("#snap-now")) { try { const r = await api("/api/snapshots/now", {}); R.snaps = {snapshots: r.snapshots, keep: (R.snaps || {}).keep}; $("#snap-list", body).innerHTML = snapHtml(R.snaps); $("#snap-msg", body).textContent = "Kept."; } catch (er) { $("#snap-msg", body).innerHTML = errInline(er); } return; }
    if (e.target.closest("#af-dl")) { const a = document.createElement("a"); a.href = "/api/backup?pdfs=" + ($("#af-pdfs", body).checked ? 1 : 0); a.download = ""; document.body.append(a); a.click(); a.remove(); if (m) m.textContent = "Building the zip… the download starts in a moment."; }
    else if (e.target.closest("#af-save")) { try { const r = await api("/api/backup/save", {pdfs: $("#af-pdfs", body).checked}); if (m) m.textContent = `Saved ${bytes(r.bytes)} to ${r.path}`; } catch (er) { if (m) m.innerHTML = errInline(er); } }
    else if (c) {
      const k = R.kinds.find(x => x.id === c.dataset.clear);
      if (!confirm(`Clear “${k.name}”?\n\n${k.count} file${k.count === 1 ? "" : "s"}, ${bytes(k.bytes)}.${k.note ? "\n" + k.note + "." : ""}\n\n` + (k.regen ? "It is made again by itself when needed." : "A zip of them is kept so you can put them back from this page."))) return;
      try { const r = await api("/api/aifiles/clear", {kind: k.id}); R.kinds = r.kinds; R.trash = r.trash; draw(); toast(`Cleared ${r.removed} file${r.removed === 1 ? "" : "s"} (${bytes(r.bytes)}).`); window.dispatchEvent(new Event("jw-library-changed")); }
      catch (er) { notifyError(er); }
    } else if (rs) {
      try { const r = await api("/api/aifiles/restore", {name: rs.dataset.restore}); R.kinds = r.kinds; R.trash = r.trash; draw(); toast(`Put back ${r.restored} file${r.restored === 1 ? "" : "s"}${r.skipped ? `; ${r.skipped} already existed again and were left alone` : ""}.`); window.dispatchEvent(new Event("jw-library-changed")); }
      catch (er) { notifyError(er); }
    }
  };
}
// ---------------------------------------------------------------- Computers & sync (one data folder used from more than one computer, e.g. in Dropbox)
const ago = s => s < 90 ? "just now" : s < 5400 ? Math.round(s / 60) + " minutes ago" : s < 129600 ? Math.round(s / 3600) + " hours ago" : Math.round(s / 86400) + " days ago";
async function renderSync(body) {
  body.innerHTML = `<div class="card st-card"><span class="ask-status">Looking…</span></div>`;
  let R, AI = D.ai; try { R = await api("/api/sync"); AI = await api("/api/ai/config"); D.ai = AI; } catch (e) { body.innerHTML = `<div class="card st-card">${errBox(e)}</div>`; return; }
  const inEnvN = A => (A.keys_in_env || Object.values(A.providers).filter(p => p.key_env && p.key_where === ".env file").map(p => p.key_env)).length;     // (AI keys and the paper sources' keys: OpenAlex, Elsevier)
  let inEnv = inEnvN(AI);
  const draw = () => {
    body.innerHTML = `
    <div class="card st-card"><h2>This computer</h2>
      <p class="hint">Use the same data folder on every computer (put it in Dropbox) and each one starts where the other stopped. The name below is what the other computers call this one.</p>
      <div class="row" style="gap:8px;align-items:center"><label for="sy-name">Name</label><input type="text" id="sy-name" maxlength="40" value="${esc(R.me.name)}" style="max-width:240px"><button class="btn small" id="sy-save">Save name</button><span class="st-msg" id="sy-msg"></span></div>
      <p class="hint" style="margin-top:10px">Your data folder, shared between computers: <code>${esc(R.data_folder)}</code></p>
      <p class="hint">${R.separate ? `Kept on this computer only (it does not travel with the folder): the browser used for library sign-ins, the language model, lookup caches and the update state, in <code>${esc(R.local_folder)}</code>.` : "Everything, including the sign-in browser, is inside the data folder (the app was started from a project folder, not as the Mac app)."}</p>
      <p class="hint">Travels with the folder, so every computer shares it: your settings and view choices (theme, list or grid, where you stopped reading), the search index and the saved AI answers${R.shared_cache && R.shared_cache.files ? ` (${R.shared_cache.files} files, ${R.shared_cache.bytes >= 1e8 ? Math.round(R.shared_cache.bytes / 1e6) + " MB" : (R.shared_cache.bytes / 1e6).toFixed(1) + " MB"})` : ""}. A paper is read into the index once, on whichever computer sees it first, and an AI answer paid for once is free on the others. Both can be made again, so nothing is lost if Dropbox keeps two copies.</p></div>
    <div class="card st-card"><h2>Other computers</h2>
      ${R.computers.length ? `<table class="st-table" id="sy-comps"><tr><th>Computer</th><th>Last seen</th><th></th></tr>${R.computers.map(c => `<tr><td><b>${esc(c.name)}</b>${c.version ? ` <small>version ${esc(c.version)}</small>` : ""}</td><td>${ago(c.age_s)}</td><td>${c.open ? `<span class="pill">open now</span>` : ""}</td></tr>`).join("")}</table>
      ${R.open_elsewhere.length ? `<p class="hint warn">The folder is open on ${R.open_elsewhere.map(c => esc(c.name)).join(" and ")} right now. Changes you make on both at once can collide (the same file changed twice): Dropbox then keeps two copies and they show up below. Working on one computer at a time avoids it.</p>` : `<p class="hint">None is open at the moment.</p>`}`
      : `<p class="hint">No other computer has used this folder yet. Install the app on the other computer and choose this same folder when it asks.</p>`}</div>
    <div class="card st-card" id="sy-conf"><h2>Copies Dropbox had to make ${R.conflicts.length ? `<small>${R.conflicts.length}</small>` : ""}</h2>
      ${R.conflicts.length ? `<p class="hint">When two computers change the same file before Dropbox has copied the first change, Dropbox keeps both and renames one “… conflicted copy …”. Nothing is deleted by the buttons here: a copy you set aside goes to <code>Papers/_conflicts/</code>.</p>
      <table class="st-table"><tr><th>File</th><th>Changed</th><th></th></tr>${R.conflicts.map(c => `<tr data-p="${esc(c.path)}"><td><code>${esc(c.path)}</code><div class="pmeta">copy of <code>${esc(c.original)}</code>${c.original_exists ? "" : " (that file is gone)"}</div></td><td>${esc(c.modified.slice(0, 10))}</td>
        <td>${c.join && c.original_exists ? `<button class="btn small primary" data-merge="${esc(c.path)}" title="${c.join === "index" ? "Keeps every paper from both lists" : "Keeps what is in either copy: nothing of yours is dropped"}">${c.join === "index" ? "Join the two lists" : "Join the two copies"}</button> ` : ""}<button class="btn small" data-aside="${esc(c.path)}">Set aside</button></td></tr>`).join("")}</table>`
      : `<p class="hint ok">✓ None. Dropbox has not had to duplicate any of your files.</p>`}</div>
    <div class="card st-card"><h2>API keys</h2>
      ${AI.keychain ? `<p class="hint">API keys can live in this Mac’s Keychain instead of the <code>.env</code> file in your data folder, so a synced folder never carries them to another computer. You paste each key once on each computer. A key you paste in Settings is saved there from now on.</p>
      ${inEnv ? `<p class="hint warn">${inEnv} key${inEnv === 1 ? " is" : "s are"} still in the <code>.env</code> file inside the data folder (readable in Dropbox).</p><div class="row" style="gap:8px"><button class="btn primary" id="sy-keys">Move ${inEnv === 1 ? "it" : "them"} to the Keychain</button><span class="st-msg" id="sy-kmsg"></span></div>
      <p class="hint">Each key is read back from the Keychain before its line is removed from the file. Dropbox keeps earlier versions of files, so after moving, create new keys at your AI providers (and at OpenAlex or Elsevier) and delete the old ones.</p>` : `<p class="hint ok">✓ No key is stored in a file in your data folder.</p>`}`
      : `<p class="hint">Keys are saved in the <code>.env</code> file of your data folder. The Keychain is only available in the Mac app.</p>`}</div>`;
  };
  draw();
  body.onclick = async e => {
    const msg = $("#sy-msg", body), b = e.target.closest("button"); if (!b) return;
    try {
      if (b.id === "sy-save") { R = await api("/api/sync", {action: "rename", name: $("#sy-name", body).value}); draw(); toast("Name saved."); }
      else if (b.dataset.merge) { const r = await api("/api/sync", {action: "merge", path: b.dataset.merge}); R = r; draw(); toast(`${r.said} The copy was set aside.`); window.dispatchEvent(new Event("jw-library-changed")); }
      else if (b.dataset.aside) { if (!confirm("Set this copy aside?\n\n" + b.dataset.aside + "\n\nIt moves to Papers/_conflicts/ and is not deleted.")) return; R = await api("/api/sync", {action: "set_aside", path: b.dataset.aside}); draw(); toast("Set aside."); }
      else if (b.id === "sy-keys") { const r = await api("/api/ai/keys/move", {}); D.ai = r.config; AI = r.config; inEnv = inEnvN(AI); R = await api("/api/sync"); draw(); toast(r.moved.length ? `Moved ${r.moved.length} key${r.moved.length === 1 ? "" : "s"} to the Keychain.` : "Nothing was moved."); if (Object.keys(r.kept).length) { const m = $("#sy-kmsg", body); if (m) m.textContent = "Not moved: " + Object.entries(r.kept).map(([k, v]) => k + " (" + v + ")").join("; "); } }
    } catch (er) { if (msg) note(msg, er.message, true); else notifyError(er); }
  };
}
function renderLook(body) {
  const cur = document.documentElement.dataset.themepref || "light";
  const pick = (val, t, d) => `<label class="lk-opt${cur === val ? " on" : ""}"><input type="radio" name="lt" value="${val}"${cur === val ? " checked" : ""}><b>${t}</b><span>${d}</span></label>`;
  body.innerHTML = `<div class="card st-card"><h2>Theme</h2><p class="pmeta">Light is the default: a warm, paper-like page. Dark is the same design on a soft charcoal.</p>
    <div class="lk-row" id="lk-theme">${pick("light", "Light", "Warm off-white, easy on paper-like reading")}${pick("dark", "Dark", "Soft charcoal for the evening")}${pick("auto", "Match my computer", "Follows the system light/dark setting")}</div></div>`;
  $("#lk-theme", body).onchange = e => { window.setTheme(e.target.value); renderLook(body); };
}
// the "is there an update" part of the About card, drawn from /api/update (and drawn again by itself when a check ends, wherever it was started from)
function updBlock(U) {
  const X = explainUpdate(U), facts = U.problem ? updateFacts(U) : [];
  const status = U.available ? `<p id="ab-status" style="margin:0 0 8px"><b>Version ${esc(U.latest)} is ready</b> (you have ${esc(U.current)}). <span class="hint">${U.problem ? "This is what it found earlier; the latest look did not work." : esc(X.detail)}</span></p>`
    : X.tone === "ok" || X.tone === "neutral" ? `<p id="ab-status" style="margin:0 0 8px">${esc(X.title)}${/[.!?]$/.test(X.title) ? "" : "."} <span class="hint">${esc(X.detail)}</span></p>` : "";
  const box = X.tone === "bad" ? "error" : X.kind === "busy" ? "busy" : X.kind === "not_found" || X.kind === "address" ? "setup" : "net";
  const problem = U.problem && X.tone !== "neutral" ? `<div class="errbox ${box}" id="ab-problem" role="status">${ico("warn", "sm")}<div class="eb-b"><b>${esc(X.title)}</b><div class="eb-d">${esc(X.detail)}</div></div></div>` : "";
  const why = facts.length ? `<details id="ab-facts" style="margin:6px 0"><summary class="hint" style="cursor:pointer">Details</summary>${facts.map(f => `<p class="hint" style="margin:2px 0;overflow-wrap:anywhere">${esc(f)}</p>`).join("")}</details>` : "";
  return status + problem + why + (U.available && U.notes.length ? `<ul class="ab-notes">${U.notes.map(n => `<li>${esc(n)}</li>`).join("")}</ul>` : "");
}
document.addEventListener("jw:update", e => { const b = document.querySelector("#st-body"); if (b && b._abPaint) b._abPaint(e.detail); });      // update.js: a look at the feed has ended
async function renderAbout(body) {
  let U, C = null; try { [U, C] = await Promise.all([api("/api/update"), api("/api/config").catch(() => null)]); } catch (e) { body.innerHTML = `<div class="card st-card">${esc(e.message)}</div>`; return; }
  if (tab !== "about") return;                       // (the person went to another tab while this was loading)
  const canMove = !!(window.pywebview && window.pywebview.api && window.pywebview.api.change_home);          // the Mac app (app/jw_app.py): the same as its Data → Change data folder… menu
  body.innerHTML = `<div class="card st-card"><h2>Journal Watcher <small>version ${esc(U.current)}</small></h2>
      <p class="hint">${C && C.home ? `Your data lives in <code>${esc(C.home)}</code> (papers, notes, folders, settings and your API keys). ` : ""}${U.app ? "This is the Mac app." : "This copy runs from the project folder."}</p>
      ${canMove ? `<div class="st-actions" style="margin:6px 0 0"><button class="btn" id="ab-move">Change data folder…</button><span class="hint" style="margin:0">Use another folder from now on (a Dropbox or iCloud folder, say); you can take your papers and settings along. The old folder is left as it is.</span></div>` : ""}
      <div class="st-actions" style="margin:6px 0 0"><button class="btn" id="ab-welcome">Show the welcome guide</button><span class="hint" style="margin:0">What API keys are, how to get one, and how the publisher sign-ins work.</span></div>
      <div class="st-actions" style="margin:6px 0 0"><button class="btn" id="ab-author">About the author and support</button><span class="hint" style="margin:0">Who makes Journal Watcher, and how to chip in.</span></div></div>
    <div class="card st-card"><h2>Updates</h2>
      <div id="ab-updblock">${updBlock(U)}</div>
      <p class="hint" id="ab-sig">${U.signed ? `Updates are checked against the author’s digital signature before anything is installed (${U.keys} signing key${U.keys === 1 ? "" : "s"} built into this app): a feed without a good signature is ignored.` : `This copy does not check an update’s digital signature (no signing key is built into it); it checks the download against the feed’s checksum only.`}</p>
      <div class="st-actions" style="margin:8px 0"><button class="btn${U.available ? " primary" : ""}" id="ab-go">${U.available ? "Update now…" : "Check for updates"}</button></div>
      <label class="chk"><input type="checkbox" id="ab-auto"${U.auto ? " checked" : ""}> Look for updates by itself (a little after start-up and every six hours); a button appears in the top bar when one is ready</label>
      <details style="margin-top:10px"><summary class="hint" style="cursor:pointer">Update address</summary><label class="chk" style="margin-top:6px">Feed <input type="text" id="ab-feed" value="${esc(U.feed_url || "")}" placeholder="https://… /appcast.json" style="flex:1"> <button class="btn small" id="ab-feedsave">Save</button></label>
      <p class="hint">Where the app asks for news of new versions: a small <code>appcast.json</code> that <code>app/release.sh</code> writes. Leave it as it is unless you moved it.</p></details>
      <div class="st-msg" id="ab-msg"></div></div>`;
  $("#ab-welcome", body).onclick = () => { close(); window.Welcome && window.Welcome.open(0); };
  $("#ab-author", body).onclick = () => { close(); window.About && window.About.open(); };
  if ($("#ab-move", body)) $("#ab-move", body).onclick = async () => { try { const r = await window.pywebview.api.change_home(); if (r && r.error) toast(r.error); } catch (e) { toast(String(e && e.message || e)); } };
  body._abPaint = V => {                             // only this part of the card is drawn again: what you typed under Update address, the open Details and the focus stay
    const blk = $("#ab-updblock", body), go = $("#ab-go", body);
    if (tab !== "about" || !body.isConnected || !blk || !go) return;
    const open = !!$("#ab-facts[open]", blk);
    U = V; blk.innerHTML = updBlock(U);
    if (open && $("#ab-facts", blk)) $("#ab-facts", blk).open = true;
    go.className = "btn" + (U.available ? " primary" : ""); go.textContent = U.available ? "Update now…" : "Check for updates";
  };
  $("#ab-go", body).onclick = () => window.JWUpdate ? JWUpdate.show(!U.available) : toast("Updates are still loading.");
  $("#ab-auto", body).onchange = async e => { try { await api("/api/update/settings", {auto: e.target.checked}); note($("#ab-msg", body), "Saved."); } catch (er) { note($("#ab-msg", body), er.message, true); } };
  $("#ab-feedsave", body).onclick = async () => { try { await api("/api/update/settings", {feed: $("#ab-feed", body).value.trim()}); note($("#ab-msg", body), "Saved."); renderAbout(body); } catch (er) { note($("#ab-msg", body), er.message, true); } };
}
// what "Test my access" found, in words: green when it got through, amber when it needs you, grey when it cannot tell (and never more than the page showed)
function testResult(r, last) {
  if (!r || !r.state) return "";
  const when = last && r.age_hours != null ? `<span class="hint" style="margin:0"> Last test ${esc(r.age_hours < 0.05 ? "just now" : agoWords(r.age_hours))}.</span>` : "";
  if (r.state === "ok") return `<p class="hint ok" style="margin:8px 0 0">✓ ${esc(r.message)}${when}</p>`;
  const box = r.state === "unknown" ? "" : ["login", "setup"].includes(r.state) ? " setup" : " net";
  return box ? `<div class="errbox${box}" style="margin:10px 0 0">${ico("warn", "sm")}<div class="eb-b"><div class="eb-d" style="margin:0;color:var(--ink)">${esc(r.message)}${when}</div></div></div>` : `<p class="hint" style="margin:8px 0 0">${esc(r.message)}${when}</p>`;
}
// what "Find my library's settings" found: the answer, the evidence (which pages, how many links) so it can be checked, and a button that fills the form in
function detectResult(r) {
  if (!r) return "";
  const pages = (r.pages || []).filter(p => p.found).map(p => `<li><a href="${esc(p.url)}" target="_blank" rel="noopener">${esc(p.url.replace(/^https?:\/\//, ""))}</a> <span class="hint" style="margin:0">${esc(p.found)}</span></li>`).join("");
  const looked = (r.pages || []).length;
  const good = r.mode === "openathens" ? r.verified !== false : !!r.mode;
  const head = r.mode ? `<p class="hint${good ? " ok" : ""}" style="margin:10px 0 4px">${good ? "✓ " : ""}${esc(r.message)}</p>` : `<p class="hint" style="margin:10px 0 4px">${esc(r.message)}</p>`;
  const use = (r.mode && good) || (!r.mode && r.ebsco_home) ? `<button class="btn primary" id="la-use" style="margin-top:6px">Use these settings</button>` : "";
  return `${head}${pages ? `<details class="hint" style="margin:4px 0"><summary style="cursor:pointer">Where this was seen (${looked} page${looked === 1 ? "" : "s"} read)</summary><ul class="wl-list" style="margin:6px 0">${pages}</ul></details>` : ""}${use}`;
}
function renderLibraryAccessSigned(body, r) { const s = $("#la-signed", body); if (s) { s.textContent = "✓ Signed in · just now"; } else { const x = document.createElement("p"); x.className = "hint ok"; x.id = "la-signed"; x.textContent = "✓ Signed in · just now"; $(".st-actions", body).after(x); } }
async function renderLibraryAccess(body, flash) {
  let P, S; try { [P, S] = await Promise.all([api("/api/library/access"), api("/api/signins")]); } catch (e) { body.innerHTML = `<div class="card st-card">${esc(e.message)}</div>`; return; }
  const modes = [["openathens", "My library uses OpenAthens", "Works on and off campus: on campus your network address usually gives access already, off campus Journal Watcher goes through your library’s OpenAthens sign-in. Its domain looks like myuni.ac.uk (you can also paste any go.openathens.net link from your library’s site)."],
    ["ezproxy", "My library gives me an EZproxy link", "Works on and off campus, like OpenAthens. A web address that begins your library’s sign-in, for example https://ezproxy.myuni.edu/login?url= (you can paste a whole proxied link: the part after url= is cut off)."],
    ["institutional", "Through each publisher’s “sign in through your institution” page", "No set-up here: Sign-ins → Other publisher sites opens each publisher, where you choose your university and log in once per publisher."],
    ["vpn", "Only on campus or on my university’s VPN", "Access by network address only. Nothing to sign in; off campus without the VPN, paywalled papers will not download."],
    ["none", "I have no institutional access", "Only open-access papers, your INFORMS/AIS memberships and any copy you add yourself."]];
  const e = S.library || {}, st = e.signs_in && S.library.signed_in !== undefined ? S.library : null;
  body.innerHTML = `<div class="card st-card"><h2>Find my library’s settings</h2>
    <p class="hint">Paste your library’s web address (or any link to a database copied from its site). Journal Watcher reads a few of its pages, finds how it lets you in (OpenAthens, EZproxy, an EBSCO start page) and checks an OpenAthens domain with OpenAthens itself. Nothing is sent anywhere else.</p>
    <div class="st-actions" style="margin-top:6px"><input type="text" id="la-find" value="${esc(P.home)}" placeholder="e.g. library.myuni.edu" style="flex:1;min-width:240px" aria-label="Your library's web address"><button class="btn primary" id="la-findgo">Find</button></div>
    <div id="la-found" role="status" aria-live="polite"></div></div>
  <div class="card st-card"><h2>Your university library</h2>
    <p class="hint">Many journals are free to you through your university. Tell Journal Watcher how your library lets you in and it will use that to download papers. This is stored in <code>library.json</code> in your data folder; nothing is sent anywhere.</p>
    <label class="chk">Name of your university or library <input type="text" id="la-name" value="${esc(P.name)}" placeholder="e.g. University of Somewhere Library (optional)" style="flex:1"></label>
    <div class="la-modes" role="radiogroup" aria-label="How your library lets you in">${modes.map(([k, t, d]) => `<label class="lk-opt la-mode${P.mode === k ? " on" : ""}"><input type="radio" name="la-mode" value="${k}"${P.mode === k ? " checked" : ""}><b>${t}</b><span>${d}</span>
      ${k === "openathens" ? `<input type="text" id="la-oa" value="${esc(P.openathens)}" placeholder="myuni.ac.uk" aria-label="OpenAthens domain">` : ""}${k === "ezproxy" ? `<input type="text" id="la-ez" value="${esc(P.ezproxy)}" placeholder="https://ezproxy.myuni.edu/login?url=" aria-label="EZproxy login address">` : ""}</label>`).join("")}</div>
    <details style="margin-top:12px" open><summary class="hint" style="cursor:pointer">More (optional)</summary>
      <label class="chk" style="margin-top:8px">Your library’s web site <input type="text" id="la-home" value="${esc(P.home)}" placeholder="https://library.myuni.edu" style="flex:1"> <small>opened as a tab in the publisher sign-in window</small></label>
      <label class="chk">EBSCO start page <input type="text" id="la-eb" value="${esc(P.ebsco_home)}" placeholder="https://research.ebsco.com/c/abc123" style="flex:1"></label>
      <label class="chk"><input type="checkbox" id="la-useeb"${P.use_ebsco || !P.ebsco_home ? " checked" : ""}> When a publisher does not give a paper, look for it in my library’s EBSCO (Business Source and others hold many business and IS journals)</label>
      <label class="chk">Download pace <select id="la-pace">${[["careful", "Careful (recommended): 45 s between papers from sites with human checks, 20 s for others"], ["normal", "Normal: about 20 s and 8 s"], ["fast", "Fast: almost no pause (more “verify you are human” checks)"]].map(([k, t]) => `<option value="${k}"${(P.pace || "careful") === k ? " selected" : ""}>${t}</option>`).join("")}</select></label>
      <p class="hint" style="margin:-4px 0 8px">Publishers such as ScienceDirect ask “are you human?” when papers are fetched one after another. While Journal Watcher waits between them it writes the summaries of the papers it already has.</p>
      <label class="chk">Interlibrary-loan email <input type="text" id="la-ill" value="${esc(P.ill_email)}" placeholder="ill@library.myuni.edu" style="flex:1"> <small>adds an “Ask my library” button to papers that could not be downloaded</small></label></details>
    <div class="st-actions" style="margin-top:14px"><button class="btn primary" id="la-save">Save</button><button class="btn" id="la-go"${P.signs_in ? "" : " disabled"}>Sign in now</button><button class="btn" id="la-test">Test my access</button><span class="st-msg" id="la-msg" role="status"></span></div>
    ${P.signs_in ? `<p class="hint">“Sign in now” opens ScienceDirect through your library, so that your university’s sign-in page appears; type your password there. “Test my access” does the same in the background, with no window.</p>` : `<p class="hint">“Sign in now” is for OpenAthens and EZproxy. With the other options you sign in on each publisher’s own page (Sign-ins → Other publisher sites).</p>`}
    ${st && st.signed_in ? `<p class="hint ok" id="la-signed">✓ Signed in · ${esc(agoWords(st.age_hours))}</p>` : ""}
    <div id="la-test-res" role="status" aria-live="polite">${testResult(S.library && S.library.test, true)}</div></div>`;
  const msg = $("#la-msg", body);
  if (flash) note(msg, flash);
  const pick = v => { const r = $(`input[name="la-mode"][value="${v}"]`, body); if (r && !r.checked) { r.checked = true; r.dispatchEvent(new Event("change")); } };
  $$('input[name="la-mode"]', body).forEach(r => r.onchange = () => $$(".la-mode", body).forEach(l => l.classList.toggle("on", $("input[type=radio]", l).checked)));
  if ($("#la-oa", body)) $("#la-oa", body).oninput = () => pick("openathens");       // typing the domain chooses its option (it used to be saved, but ignored, under "each publisher’s page")
  if ($("#la-ez", body)) $("#la-ez", body).oninput = () => pick("ezproxy");
  const collect = () => ({name: $("#la-name", body).value, mode: ($('input[name="la-mode"]:checked', body) || {}).value || "institutional", openathens: $("#la-oa", body).value, ezproxy: $("#la-ez", body).value, home: $("#la-home", body).value, ebsco_home: $("#la-eb", body).value, use_ebsco: $("#la-useeb", body).checked, pace: $("#la-pace", body).value, ill_email: $("#la-ill", body).value});
  const missing = c => c.mode === "openathens" && !c.openathens.trim() ? "Enter your OpenAthens domain (it looks like myuni.ac.uk), or use “Find my library’s settings” above." : c.mode === "ezproxy" && !c.ezproxy.trim() ? "Enter your EZproxy login address (https://… ending in url=)." : "";
  $("#la-findgo", body).onclick = async () => {
    const given = $("#la-find", body).value.trim(), out = $("#la-found", body), go = $("#la-findgo", body);
    if (!given) { out.innerHTML = `<p class="hint bad" style="margin:8px 0 0">Paste your library’s web address first.</p>`; return; }
    go.disabled = true; out.innerHTML = `<p class="hint" style="margin:8px 0 0">Reading your library’s pages… this takes a few seconds, up to a minute.</p>`;
    try {
      const r = await api("/api/library/detect", {given});
      if (!body.isConnected) return;
      out.innerHTML = detectResult(r);
      const use = $("#la-use", out);
      if (use) use.onclick = async () => {
        if (r.mode === "openathens") { $("#la-oa", body).value = r.openathens; pick("openathens"); }
        else if (r.mode === "ezproxy") { $("#la-ez", body).value = r.ezproxy; pick("ezproxy"); }
        if (r.ebsco_home) { $("#la-eb", body).value = r.ebsco_home; $("#la-useeb", body).checked = true; }
        if (/^https?:\/\/|^[\w.-]+\.[a-z]{2,}/i.test(given) && !/openathens|login\?q?url=/i.test(given) && !$("#la-home", body).value.trim()) $("#la-home", body).value = given;
        const c = collect(), need = missing(c); if (need) { note(msg, need, true); return; }
        try { await api("/api/library/access", c); if (window.loadSignins) loadSignins(); await renderLibraryAccess(body, "Saved. Now press “Sign in now” (or “Test my access” if you are on campus)."); } catch (er) { note(msg, er.message, true); }
      };
    } catch (er) { if (body.isConnected) out.innerHTML = `<p class="hint bad" style="margin:8px 0 0">${esc(er.message)}</p>`; }
    finally { go.disabled = false; }
  };
  $("#la-find", body).onkeydown = ev => { if (ev.key === "Enter") $("#la-findgo", body).click(); };
  $("#la-save", body).onclick = async () => { const c = collect(), need = missing(c); if (need) { note(msg, need, true); return; } try { await api("/api/library/access", c); if (window.loadSignins) loadSignins(); renderLibraryAccess(body, "Saved."); } catch (er) { note(msg, er.message, true); } };       // (drawn again first, then the message: it used to be wiped at once)
  $("#la-go", body).onclick = async () => { const c = collect(), need = missing(c); if (need) { note(msg, need, true); return; } try { await api("/api/library/access", c); await api("/api/login", {which: "library"}); note(msg, "A sign-in window is opening on ScienceDirect; sign in with your university account there."); if (window.loadSignins) loadSignins(); } catch (er) { note(msg, er.message, true); } };
  $("#la-test", body).onclick = async () => {
    const c = collect(), need = missing(c); if (need) { note(msg, need, true); return; }
    const go = $("#la-test", body), res = $("#la-test-res", body); go.disabled = true; note(msg, "");
    res.innerHTML = `<p class="hint">Opening ScienceDirect through your library in the background… this takes a few seconds, up to half a minute.</p>`;
    try { await api("/api/library/access", c); const r = await api("/api/library/test", {}); if (body.isConnected) res.innerHTML = testResult({...r, age_hours: 0}); if (window.loadSignins) loadSignins(); const proxy = ["openathens", "ezproxy"].includes(r.mode); if (proxy && r.state === "ok") renderLibraryAccessSigned(body, r); else if (proxy && r.state === "login") { const s = $("#la-signed", body); if (s) s.remove(); } }
    catch (er) { if (body.isConnected) { res.innerHTML = ""; note(msg, er.message, true); } }
    finally { go.disabled = false; }
  };
}
// Settings → Paper sources: the free keys and the address that let Journal Watcher find abstracts and open-access copies (saved in .env; keys never shown again)
async function renderSources(body, flash) {
  let S; try { S = await api("/api/sources"); } catch (e) { body.innerHTML = `<div class="card st-card">${esc(e.message)}</div>`; return; }
  const set = on => on ? `<span class="hint ok" style="margin:0">✓ saved</span>` : `<span class="hint" style="margin:0">not set</span>`;
  body.innerHTML = `<div class="card st-card"><h2>OpenAlex</h2>
    <p class="hint">OpenAlex supplies the abstracts that Crossref lacks (JMIS, DSS, NBER), and the references behind Discover. Without a key, everyone on your network shares one small daily allowance; when it is used up, abstracts are looked up one by one and searches take longer. A free key gives you your own allowance ($1 of use a day, plenty for this app). Make an account at <a href="https://openalex.org/" target="_blank" rel="noopener">openalex.org</a> and copy the key from <a href="https://openalex.org/settings/api" target="_blank" rel="noopener">openalex.org/settings/api</a>.</p>
    <label class="chk">OpenAlex API key <input type="password" id="ps-oa" placeholder="${S.openalex_key ? "•••••••• (saved; type a new one to replace it)" : "paste your key"}" autocomplete="off" style="flex:1"> ${set(S.openalex_key)}</label>
    ${S.openalex_spent ? `<p class="hint bad" style="margin:4px 0 0">OpenAlex says today’s ${S.openalex_key ? "allowance for your key" : "shared keyless allowance"} is used up (it resets at midnight UTC).</p>` : ""}</div>
  <div class="card st-card"><h2>Unpaywall (open-access copies)</h2>
    <p class="hint">Before using your sign-ins, Journal Watcher asks Unpaywall whether a free, legal copy of the paper exists. Unpaywall needs an email address with each request so that it knows who is asking. It is sent only to Unpaywall. Without one, this step is skipped.</p>
    <label class="chk">Email for Unpaywall <input type="text" id="ps-up" value="${esc(S.unpaywall_email || "")}" placeholder="you@university.edu" style="flex:1"></label></div>
  <div class="card st-card"><h2>Elsevier (optional)</h2>
    <p class="hint">An Elsevier API key (free at <a href="https://dev.elsevier.com/" target="_blank" rel="noopener">dev.elsevier.com</a>) lets Journal Watcher fetch ScienceDirect PDFs through Elsevier’s official interface, without the “are you human?” check. It only works from your university’s network, or with an institutional token from your library.</p>
    <label class="chk">Elsevier API key <input type="password" id="ps-el" placeholder="${S.elsevier_key ? "•••••••• (saved)" : "optional"}" autocomplete="off" style="flex:1"> ${set(S.elsevier_key)}</label>
    <label class="chk">Institutional token <input type="password" id="ps-et" placeholder="${S.elsevier_insttoken ? "•••••••• (saved)" : "optional, from your library"}" autocomplete="off" style="flex:1"> ${set(S.elsevier_insttoken)}</label></div>
  <div class="card st-card"><div class="st-actions"><button class="btn primary" id="ps-save">Save</button>${S.openalex_key || S.elsevier_key || S.elsevier_insttoken ? `<button class="btn" id="ps-clear">Remove saved keys…</button>` : ""}<span class="st-msg" id="ps-msg" role="status"></span></div>
    <p class="hint">${S.keychain ? "Keys are saved in this Mac’s Keychain, not in your data folder, so a synced folder never carries them to another computer: paste them once on each computer. The Unpaywall email is saved in the <code>.env</code> file of your data folder." : "Saved in the <code>.env</code> file of your data folder (readable only by you)."} Keys are never shown again, and each is sent only to its own service.${S.keychain && Object.values(S.where || {}).includes(".env file") ? ` <span class="warn">A key is still in the <code>.env</code> file: Settings → Computers &amp; sync can move it to the Keychain.</span>` : ""}</p></div>`;
  const msg = $("#ps-msg", body);
  if (flash) note(msg, flash);
  $("#ps-save", body).onclick = async () => {
    const b = {unpaywall_email: $("#ps-up", body).value.trim()};
    [["ps-oa", "openalex_key"], ["ps-el", "elsevier_key"], ["ps-et", "elsevier_insttoken"]].forEach(([id, k]) => { const v = $("#" + id, body).value.trim(); if (v) b[k] = v; });     // an empty key box keeps the saved key
    note(msg, b.openalex_key ? "Checking the key with OpenAlex…" : "Saving…");
    try { await api("/api/sources", b); renderSources(body, "Saved."); window.dispatchEvent(new Event("jw-sources-changed")); } catch (er) { note(msg, er.message, true); }
  };
  const cl = $("#ps-clear", body);
  if (cl) cl.onclick = async () => { if (!confirm("Remove the saved OpenAlex and Elsevier keys from this computer?")) return; try { await api("/api/sources", {openalex_key: "", elsevier_key: "", elsevier_insttoken: ""}); renderSources(body, "Removed."); window.dispatchEvent(new Event("jw-sources-changed")); } catch (er) { note(msg, er.message, true); } };
}
const agoWords = h => h == null ? "" : h < 1 ? Math.round(h * 60) + " min ago" : h < 36 ? Math.round(h) + " h ago" : Math.round(h / 24) + " days ago";
function renderReader(body) {
  const P = D.prefs, draw = () => {
    body.innerHTML = `<div class="card st-card"><h2>What the highlight colours mean</h2><p class="hint">Name the colours the way you read (claim, method, finding…). The names appear in the reader, in filters and in exports.</p>
      <div class="legend">${COLORS.map(c => `<label><span class="sw" style="background:${c.c}"></span><input type="text" data-c="${c.id}" value="${esc((P.legend || {})[c.id] || "")}" placeholder="${c.name}"></label>`).join("")}</div></div>
    <div class="card st-card"><h2>Paper summaries</h2><label class="chk">Template used for new papers <select id="sdef">${D.summaries.map(s => `<option value="${esc(s.id)}"${P.summary_template === s.id ? " selected" : ""}>${esc(s.name)}</option>`).join("")}</select></label>
      <p class="hint">In the reader's Summary tab you can generate a paper with any template without touching the saved summary. Placeholders: <code>{{interests}}</code> <code>{{title}}</code> <code>{{journal}}</code> <code>{{doi}}</code>.</p>
      <div id="sl">${editor(D.summaries, "summary")}</div><button class="btn" id="sa">＋ Add a template</button></div>
    <div class="card st-card"><h2>Other</h2><label class="chk">Reading speed <input type="number" id="wpm" min="80" max="600" value="${P.wpm}" style="width:80px"> words per minute (for “time left”)</label>
      <label class="chk">Export folder <input type="text" id="xdir" value="${esc(P.export_dir || "")}" placeholder="e.g. /Users/you/Dropbox/Research notes" style="flex:1"> <small>where “Save to my export folder” in the Ideas Lab writes (e.g. a Dropbox folder)</small></label></div>
    <div class="st-actions"><button class="btn primary" id="st-save">Save</button> <span id="st-msg" class="st-msg"></span></div>`;
    $("#sa", body).onclick = () => { D.summaries.push({id: "", name: "New template", prompt: "Read this paper and write a markdown note about {{title}}."}); draw(); };
    $("#st-save", body).onclick = async () => {
      const legend = {}; $$(".legend input", body).forEach(i => { if (i.value.trim()) legend[i.dataset.c] = i.value.trim(); });
      try {
        const r = await api("/api/settings", {summaries: collect(body, "summary", "#sl"), prefs: {legend, summary_template: $("#sdef", body).value, wpm: +$("#wpm", body).value, export_dir: $("#xdir", body).value}});
        D.prefs = r.prefs; D.summaries = r.summaries; note($("#st-msg", body), "Saved."); window.dispatchEvent(new Event("jw-settings-changed"));
      } catch (e) { note($("#st-msg", body), e.message, true); }
    };
  };
  if (!body._wiredR) { body._wiredR = true; wireEditor(body, () => D.summaries, "summary", "#sl", () => renderReader(body)); }
  draw();
}

window.Settings = {open, close};
