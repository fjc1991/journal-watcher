// Settings page: AI providers and routing, privacy and budget, usage and cost, your prompt library, reader preferences.
// Opened from the ⚙ in the top bar (window.Settings.open("ai"|"usage"|"prompts"|"reader")) and from the reader's Ask tab.
import {errBox, errInline, notifyError, explainUpdate, updateFacts, $, $$, esc, api, toast, ico, COLORS} from "./state.js";

let root, D = null, tab = "ai", LOG = [];
const FEATURES = [["ask", "Ask the paper", "Questions answered from the paper's passages"], ["explain", "Explain selection", "The one-click prompts on selected text and figures"],
  ["suggest", "Suggested questions", "Ideas for what to ask next"], ["summarize", "Paper summaries", "The pipeline's summary for each downloaded paper"],
  ["card", "Paper cards", "The structured, cited card of each paper (needs long, reliable JSON output)"], ["events", "Ideas Lab: current events", "Searches the web: pick Claude, OpenAI or OpenRouter"],
  ["ideas", "Ideas Lab: ideas and outlines", "Writes the research ideas and develops them — your strongest model"], ["reviewer", "Reviewer 2: a critical read of your text", "Needs judgement and care — use your strongest model"], ["audio", "Spoken briefings", "Writes the two-minute script that your browser reads aloud — a cheap model is enough"], ["flashcards", "Flashcards", "Writes question-and-answer cards from your paper cards — a cheap model is enough"], ["synthesis", "Synthesis map and gaps", "Reads all the cards of a folder at once — a strong model finds more"], ["claims", "Claim check", "Judges whether your papers support what you wrote — a strong model checks more carefully"], ["draft", "Related-work drafter", "Writes the section from your paper cards — prose matters, so use a strong model"], ["critique", "Ideas Lab: the editor", "Scores each idea for novelty, feasibility, significance and fit"],
  ["folder_ask", "Ask a folder or the library", "Questions answered from passages or cards of several papers"], ["matrix", "Comparison matrix", "Fills your own columns, one call per paper"],
  ["skim", "Skim layer", "Finds the key sentences of a paper by lens (question, finding, limitation…)"]];
const money = v => v == null ? "—" : v < 0.01 && v > 0 ? "$" + v.toFixed(4) : "$" + v.toFixed(2);
const TABS = [["ai", "AI & privacy"], ["usage", "Usage & cost"], ["prompts", "Prompts"], ["reader", "Reader & summaries"], ["files", "AI files & backup"], ["library", "Library access"], ["look", "Appearance"], ["about", "About & updates"]];

function build() {
  if (root) return;
  root = document.createElement("div"); root.id = "settings"; root.className = "st hide";
  root.innerHTML = `<div class="st-head"><button class="btn back" id="st-back">← Back</button><h1>Settings</h1><div class="st-tabs">${TABS.map(([k, t]) => `<button data-t="${k}">${t}</button>`).join("")}</div></div><div class="st-body" id="st-body"></div>`;
  document.body.append(root);
  $("#st-back", root).onclick = close;
  $$(".st-tabs button", root).forEach(b => b.onclick = () => { tab = b.dataset.t; render(); });
  document.addEventListener("keydown", e => { if (e.key === "Escape" && !root.classList.contains("hide") && !e.target.closest("input,textarea,select")) close(); });
}
export function close() { root?.classList.add("hide"); document.body.classList.remove("settings-open"); }
export async function open(t) {
  build(); if (t) tab = t;
  if (window.About) About.close();
  root.classList.remove("hide"); document.body.classList.add("settings-open");
  try { D = await api("/api/settings"); } catch (e) { toast(e.message); return; }
  render();
}
function render() {
  if (!D) return;          // a tab pressed before the settings arrived: open() draws the page when they do
  $$(".st-tabs button", root).forEach(b => b.classList.toggle("on", b.dataset.t === tab));
  const body = $("#st-body", root);
  ({ai: renderAI, usage: renderUsage, prompts: renderPrompts, reader: renderReader, files: renderFiles, library: renderLibraryAccess, look: renderLook, about: renderAbout})[tab](body);
}
const note = (el, msg, bad) => { el.textContent = msg; el.classList.toggle("bad", !!bad); };

// ---------------------------------------------------------------- AI & privacy
function renderAI(body) {
  const A = D.ai; if (!A) { body.innerHTML = `<div class="card st-card">AI features aren’t installed. Run <code>pip install -r requirements.txt</code>.</div>`; return; }
  const provs = Object.entries(A.providers), pnames = provs.map(([n]) => n);
  const price = m => (A.prices || {})[m] || {};
  const used = [...new Set([...Object.values(A.features).map(f => f.model), A.embeddings.provider !== "local" ? A.embeddings.model : null].filter(Boolean))];
  body.innerHTML = `
  <div class="card st-card"><h2>Providers</h2><p class="hint">Paste a key and it is saved in the <code>.env</code> file of your data folder (readable by you only); it is never shown again or sent anywhere but to its provider. No restart is needed.</p>
    <table class="st-table"><tr><th>Provider</th><th>Key</th><th>On this computer</th><th>Web search</th><th>Models you use <small>(comma-separated; fills the pickers)</small></th><th></th></tr>
    ${provs.map(([n, p]) => `<tr data-p="${esc(n)}"><td><b>${esc(n)}</b></td><td>${!p.key_env ? `<span class="ok">no key needed</span>` : `${p.configured ? `<span class="ok">✓ key found</span> <button class="btn small" data-keychange="${esc(n)}">Change</button>` : ""}<span class="keyform${p.configured ? " hide" : ""}"><input type="password" autocomplete="off" spellcheck="false" aria-label="${esc(n)} API key (${esc(p.key_env)})" placeholder="paste your ${esc(p.key_env)}"> <button class="btn small primary" data-savekey="${esc(n)}">Save key</button></span> <span class="tres keymsg"></span>`}</td>
      <td>${p.local ? "✓" : "—"}</td><td>${p.web_search ? "✓" : "—"}</td><td><input type="text" class="pm" aria-label="Models of this provider, separated by commas" value="${esc((p.models || []).join(", "))}"></td>
      <td><button class="btn small" data-test="${esc(n)}">Test</button> <span class="tres"></span></td></tr>`).join("")}</table></div>
  <div class="card st-card"><h2>Which model does what</h2>
    <table class="st-table"><tr><th>Feature</th><th>Provider</th><th>Model</th><th>Effort</th><th></th></tr>
    ${FEATURES.map(([k, t, d]) => { const f = A.features[k] || A.features.ask; return `<tr data-f="${k}"><td><b>${t}</b><br><small>${d}</small></td>
      <td><select class="fp">${pnames.map(n => `<option${f.provider === n ? " selected" : ""}>${esc(n)}</option>`).join("")}</select></td>
      <td><input type="text" class="fm" list="dl-${k}" aria-label="Model for ${esc(k)}" value="${esc(f.model)}"><datalist id="dl-${k}">${(A.providers[f.provider].models || []).map(m => `<option value="${esc(m)}">`).join("")}</datalist></td>
      <td><select class="fe">${["low", "medium", "high"].map(e => `<option${(f.effort || "low") === e ? " selected" : ""}>${e}</option>`).join("")}</select></td>
      <td><button class="btn small" data-testf="${k}">Test</button> <span class="tres"></span></td></tr>`; }).join("")}
    <tr data-f="emb"><td><b>Embeddings</b><br><small>Used to find relevant passages</small></td><td><select class="fp">${["local", ...pnames].map(n => `<option${A.embeddings.provider === n ? " selected" : ""}>${esc(n)}</option>`).join("")}</select></td>
      <td><input type="text" class="fm" aria-label="Embedding model" value="${esc(A.embeddings.model)}"></td><td></td><td><small>“local” runs on this Mac, free</small></td></tr></table></div>
  <div class="card st-card"><h2>Privacy and spending</h2>
    <label class="chk"><input type="checkbox" id="st-local"${A.privacy.local_only ? " checked" : ""}> <b>Local-only mode</b> — never send paper text to a provider outside this computer (only Ollama works; Ask and Explain need an Ollama model)</label>
    <label class="chk">Monthly budget (USD) <input type="number" id="st-budget" min="0" step="1" value="${A.budget.monthly_usd ?? ""}" placeholder="no limit" style="width:110px"> <small>calls stop when the month's spend reaches it; a warning shows at 80%</small></label>
    <label class="chk">Reuse earlier screening verdicts for <input type="number" id="st-reuse" min="0" max="3650" step="1" value="${(A.screening || {}).reuse_days ?? 90}" style="width:80px"> days <small>0 = always ask the AI again. A verdict is reused only for the same paper text and the same question (topics, method and focus phrase), so searching again does not pay for papers it already judged</small></label></div>
  <div class="card st-card"><h2>Prices <small>USD per million tokens — only for cost figures</small></h2>
    <table class="st-table"><tr><th>Model</th><th>Input</th><th>Output</th><th>Cache write</th><th>Cache read</th></tr>
    ${used.map(m => `<tr data-m="${esc(m)}"><td>${esc(m)}</td>${["in", "out", "cache_write", "cache_read"].map(k => `<td><input type="number" step="0.01" min="0" data-k="${k}" aria-label="${esc(m)}: ${({in: "input", out: "output", cache_write: "cache write", cache_read: "cache read"})[k]} price per million tokens" value="${price(m)[k] ?? ""}" style="width:90px"></td>`).join("")}</tr>`).join("")}</table>
    <p class="hint">Without a price a model's calls show “—” and don't count towards the budget. Look the numbers up on the provider's pricing page.</p></div>
  <div class="st-actions"><button class="btn primary" id="st-save">Save</button> <span id="st-msg" class="st-msg"></span></div>`;
  const msg = $("#st-msg", body);
  $$("tr[data-f] .fp", body).forEach(sel => sel.onchange = () => { const tr = sel.closest("tr"), dl = $("datalist", tr); if (dl) dl.innerHTML = ((A.providers[sel.value] || {}).models || []).map(m => `<option value="${esc(m)}">`).join(""); });
  const test = async (btn, provider, model) => { const out = btn.parentElement.querySelector(".tres"); note(out, "…"); btn.disabled = true;
    const r = await api("/api/ai/test", {provider, model}).catch(e => ({ok: false, error: e.message})); btn.disabled = false;
    note(out, r.ok ? `✓ ${r.ms} ms${r.cost != null ? " · " + money(r.cost) : ""}` : r.error, !r.ok); };
  $$("[data-keychange]", body).forEach(b => b.onclick = () => { const f = $(".keyform", b.closest("td")); f.classList.remove("hide"); $("input", f).focus(); b.classList.add("hide"); });
  $$("[data-savekey]", body).forEach(b => b.onclick = async () => { const td = b.closest("td"), inp = $("input", td), out = $(".keymsg", td); if (!inp.value.trim()) return;
    try { D.ai = await api("/api/ai/key", {provider: b.dataset.savekey, key: inp.value}); inp.value = ""; window.dispatchEvent(new Event("jw-settings-changed")); renderAI(body); toast("Key saved in the .env file of your data folder."); } catch (e) { note(out, e.message, true); } });
  $$("[data-test]", body).forEach(b => b.onclick = () => { const tr = b.closest("tr"), first = $(".pm", tr).value.split(",")[0].trim(); const m = first || prompt("Model name to test on " + b.dataset.test + ":"); if (m) test(b, b.dataset.test, m); });
  $$("[data-testf]", body).forEach(b => b.onclick = () => { const tr = b.closest("tr"); test(b, $(".fp", tr).value, $(".fm", tr).value.trim()); });
  $("#st-save", body).onclick = async () => {
    const features = {}; $$("tr[data-f]", body).forEach(tr => { const k = tr.dataset.f; if (k === "emb") return; features[k] = {provider: $(".fp", tr).value, model: $(".fm", tr).value.trim(), effort: $(".fe", tr).value}; });
    const provider_models = {}; $$("tr[data-p]", body).forEach(tr => { provider_models[tr.dataset.p] = $(".pm", tr).value.split(",").map(s => s.trim()).filter(Boolean); });
    const prices = {}; $$("tr[data-m]", body).forEach(tr => { const v = {}; let any = false; $$("input", tr).forEach(i => { v[i.dataset.k] = i.value === "" ? 0 : +i.value; any = any || i.value !== ""; }); if (any) prices[tr.dataset.m] = v; });
    const emb = $('tr[data-f="emb"]', body);
    try {
      D.ai = await api("/api/ai/config", {features, provider_models, prices, embeddings: {provider: $(".fp", emb).value, model: $(".fm", emb).value.trim()},
        privacy: {local_only: $("#st-local", body).checked}, budget: {monthly_usd: $("#st-budget", body).value}, screening: {reuse_days: $("#st-reuse", body).value}});
      note(msg, "Saved."); window.dispatchEvent(new Event("jw-settings-changed"));
    } catch (e) { note(msg, e.message, true); }
  };
}

// ---------------------------------------------------------------- usage & cost
async function renderUsage(body) {
  const U = D.usage; if (!U) { body.innerHTML = `<div class="card st-card">AI features aren’t installed.</div>`; return; }
  LOG = await api("/api/ai/log?limit=40").catch(() => []);
  const bud = U.budget, pct = bud ? Math.min(100, 100 * U.month.cost / bud) : 0, days = Array.from({length: 30}, (_, i) => { const d = new Date(Date.now() - (29 - i) * 864e5).toISOString().slice(0, 10); return [d, U.by_day[d] || 0]; }), max = Math.max(0.0001, ...days.map(d => d[1])), spent = days.some(d => d[1] > 0);
  body.innerHTML = `
  <div class="st-cards">
    <div class="card st-stat"><span>This month</span><b>${money(U.month.cost)}</b><small>${U.month.calls} calls${bud ? ` · budget ${money(bud)}` : ""}</small>${bud ? `<div class="st-bar"><i class="${U.warn ? "warn" : ""}" style="width:${pct}%"></i></div>` : ""}</div>
    <div class="card st-stat"><span>All time</span><b>${money(U.cost)}</b><small>${U.calls} calls</small></div>
    <div class="card st-stat"><span>Answered from cache</span><b>${U.cached}</b><small>free repeats · ${D.cache ? D.cache.entries + " entries, " + Math.round(D.cache.bytes / 1024) + " KB" : ""}</small></div>
    <div class="card st-stat"><span>Calls without a price</span><b>${U.unpriced}</b><small>add prices in Settings → AI</small></div></div>
  ${U.warn ? `<div class="card st-card warnbox">You have used ${Math.round(pct)}% of this month's budget.</div>` : ""}
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
const bytes = n => n < 1024 ? n + " B" : n < 1048576 ? Math.round(n / 1024) + " KB" : n < 1073741824 ? (n / 1048576).toFixed(n < 10485760 ? 1 : 0) + " MB" : (n / 1073741824).toFixed(1) + " GB";
async function renderFiles(body) {
  body.innerHTML = `<div class="card st-card"><span class="ask-status">Counting files…</span></div>`;
  let R; try { R = await api("/api/aifiles"); } catch (e) { body.innerHTML = `<div class="card st-card">${errBox(e)}</div>`; return; }
  const draw = () => {
    const total = R.kinds.reduce((a, k) => a + k.bytes, 0), b = R.backup, pdfs = $("#af-pdfs", body) ? $("#af-pdfs", body).checked : false;
    body.innerHTML = `<div class="card st-card"><h2>Back up my data</h2>
      <p class="pmeta">A zip of everything that is yours: highlights and notes, paper cards, skims, conversations, folders with their matrices and idea boards, tags, reading status, the library index, your prompts and settings (no API keys). ${bytes(b.data_bytes)} in ${b.files} files.</p>
      <label class="chk"><input type="checkbox" id="af-pdfs"${pdfs ? " checked" : ""}> Include the PDFs too (${bytes(b.pdf_bytes)}; they stay where they are in <code>Papers/</code> either way)</label>
      <div class="row" style="gap:8px;margin-top:8px"><button class="btn primary" id="af-dl">Download the backup (.zip)</button>${R.export_dir ? `<button class="btn" id="af-save" title="${esc(R.export_dir)}">Save it into my export folder</button>` : `<span class="pmeta">Set an export folder in “Reader &amp; summaries” to save it there directly.</span>`}<span class="st-msg" id="af-msg"></span></div></div>
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
  body.onclick = async e => {
    const m = $("#af-msg", body), c = e.target.closest("[data-clear]"), rs = e.target.closest("[data-restore]"), mc = e.target.closest("[data-mcpcopy]");
    if (mc) { try { await navigator.clipboard.writeText($("#" + mc.dataset.mcpcopy, body).textContent); toast("Copied."); } catch (er) { notifyError(er); } return; }
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
  body.innerHTML = `<div class="card st-card"><h2>Journal Watcher <small>version ${esc(U.current)}</small></h2>
      <p class="hint">${C && C.home ? `Your data lives in <code>${esc(C.home)}</code> (papers, notes, folders, settings and your API keys). ` : ""}${U.app ? "This is the Mac app." : "This copy runs from the project folder."}</p>
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
  const box = r.state === "unknown" ? "" : r.state === "login" ? " setup" : " net";
  return box ? `<div class="errbox${box}" style="margin:10px 0 0">${ico("warn", "sm")}<div class="eb-b"><div class="eb-d" style="margin:0;color:var(--ink)">${esc(r.message)}${when}</div></div></div>` : `<p class="hint" style="margin:8px 0 0">${esc(r.message)}${when}</p>`;
}
function renderLibraryAccessSigned(body, r) { const s = $("#la-signed", body); if (s) { s.textContent = "✓ Signed in · just now"; } else { const x = document.createElement("p"); x.className = "hint ok"; x.id = "la-signed"; x.textContent = "✓ Signed in · just now"; $(".st-actions", body).after(x); } }
async function renderLibraryAccess(body, flash) {
  let P, S; try { [P, S] = await Promise.all([api("/api/library/access"), api("/api/signins")]); } catch (e) { body.innerHTML = `<div class="card st-card">${esc(e.message)}</div>`; return; }
  const modes = [["institutional", "Through each publisher’s “sign in through your institution” page", "Works for most universities and needs no set-up: when you press Sign-ins → Other publisher sites, every publisher asks you to choose your university and you log in there once."],
    ["openathens", "My library uses OpenAthens", "Ask your library, or look for “OpenAthens” on its e-resources page. You need its domain, which looks like myuni.ac.uk."],
    ["ezproxy", "My library gives me an EZproxy link", "A web address that begins your library’s sign-in, for example https://ezproxy.myuni.edu/login?url= (the paper’s address is added after it)."],
    ["vpn", "I am on campus or use my university’s VPN", "Access is by network address: connect the VPN and downloads just work. Nothing to sign in."],
    ["none", "I have no institutional access", "Only open-access papers, your INFORMS/AIS memberships and any copy you add yourself."]];
  const e = S.library || {}, st = e.signs_in && S.library.signed_in !== undefined ? S.library : null;
  body.innerHTML = `<div class="card st-card"><h2>Your university library</h2>
    <p class="hint">Many journals are free to you through your university. Tell Journal Watcher how your library lets you in and it will use that to download papers. This is stored in <code>library.json</code> in your data folder; nothing is sent anywhere.</p>
    <label class="chk">Name of your university or library <input type="text" id="la-name" value="${esc(P.name)}" placeholder="e.g. University of Somewhere Library" style="flex:1"></label>
    <div class="la-modes" role="radiogroup" aria-label="How your library lets you in">${modes.map(([k, t, d]) => `<label class="lk-opt la-mode${P.mode === k ? " on" : ""}"><input type="radio" name="la-mode" value="${k}"${P.mode === k ? " checked" : ""}><b>${t}</b><span>${d}</span>
      ${k === "openathens" ? `<input type="text" id="la-oa" value="${esc(P.openathens)}" placeholder="myuni.ac.uk" aria-label="OpenAthens domain">` : ""}${k === "ezproxy" ? `<input type="text" id="la-ez" value="${esc(P.ezproxy)}" placeholder="https://ezproxy.myuni.edu/login?url=" aria-label="EZproxy login address">` : ""}</label>`).join("")}</div>
    <details style="margin-top:12px"${P.home || P.ill_email ? " open" : ""}><summary class="hint" style="cursor:pointer">More (optional)</summary>
      <label class="chk" style="margin-top:8px">Library page to open when signing in <input type="text" id="la-home" value="${esc(P.home)}" placeholder="https://library.myuni.edu" style="flex:1"></label>
      <label class="chk">Interlibrary-loan email <input type="text" id="la-ill" value="${esc(P.ill_email)}" placeholder="ill@library.myuni.edu" style="flex:1"> <small>adds an “Ask my library” button to papers that could not be downloaded</small></label></details>
    <div class="st-actions" style="margin-top:14px"><button class="btn primary" id="la-save">Save</button><button class="btn" id="la-go"${P.signs_in ? "" : " disabled"}>Sign in now</button><button class="btn" id="la-test">Test my access</button><span class="st-msg" id="la-msg" role="status"></span></div>
    ${P.signs_in ? "" : `<p class="hint">“Sign in now” is for OpenAthens and EZproxy. With the other options you sign in on each publisher’s own page (Sign-ins → Other publisher sites).</p>`}
    ${st && st.signed_in ? `<p class="hint ok" id="la-signed">✓ Signed in · ${esc(agoWords(st.age_hours))}</p>` : ""}
    <div id="la-test-res" role="status" aria-live="polite">${testResult(S.library && S.library.test, true)}</div></div>
  <div class="card st-card"><h2>How to find out</h2><ul class="wl-list">
    <li>Search the web for your <b>university name + “OpenAthens”</b> or <b>“EZproxy”</b>, or open your library’s <b>e-resources / off-campus access</b> page.</li>
    <li>If a link on that page starts with <code>go.openathens.net</code>, it is OpenAthens; if it contains <code>ezproxy</code>, it is EZproxy.</li>
    <li>Not sure? Leave the first option selected and use <b>Sign-ins → Other publisher sites</b>: each publisher asks which university you belong to.</li></ul></div>`;
  const msg = $("#la-msg", body);
  if (flash) note(msg, flash);
  const pick = v => { const r = $(`input[name="la-mode"][value="${v}"]`, body); if (r && !r.checked) { r.checked = true; r.dispatchEvent(new Event("change")); } };
  $$('input[name="la-mode"]', body).forEach(r => r.onchange = () => $$(".la-mode", body).forEach(l => l.classList.toggle("on", $("input[type=radio]", l).checked)));
  if ($("#la-oa", body)) $("#la-oa", body).oninput = () => pick("openathens");       // typing the domain chooses its option (it used to be saved, but ignored, under "each publisher’s page")
  if ($("#la-ez", body)) $("#la-ez", body).oninput = () => pick("ezproxy");
  const collect = () => ({name: $("#la-name", body).value, mode: ($('input[name="la-mode"]:checked', body) || {}).value || "institutional", openathens: $("#la-oa", body).value, ezproxy: $("#la-ez", body).value, home: $("#la-home", body).value, ill_email: $("#la-ill", body).value});
  const missing = c => c.mode === "openathens" && !c.openathens.trim() ? "Enter your OpenAthens domain (it looks like myuni.ac.uk)." : c.mode === "ezproxy" && !c.ezproxy.trim() ? "Enter your EZproxy login address (https://… ending in url=)." : "";
  $("#la-save", body).onclick = async () => { const c = collect(), need = missing(c); if (need) { note(msg, need, true); return; } try { await api("/api/library/access", c); if (window.loadSignins) loadSignins(); renderLibraryAccess(body, "Saved."); } catch (er) { note(msg, er.message, true); } };       // (drawn again first, then the message: it used to be wiped at once)
  $("#la-go", body).onclick = async () => { const c = collect(), need = missing(c); if (need) { note(msg, need, true); return; } try { await api("/api/library/access", c); await api("/api/login", {which: "library"}); note(msg, "A sign-in window is opening; sign in there."); if (window.loadSignins) loadSignins(); } catch (er) { note(msg, er.message, true); } };
  $("#la-test", body).onclick = async () => {
    const c = collect(), need = missing(c); if (need) { note(msg, need, true); return; }
    const go = $("#la-test", body), res = $("#la-test-res", body); go.disabled = true; note(msg, "");
    res.innerHTML = `<p class="hint">Looking at your library’s page in the background… this takes a few seconds, up to half a minute.</p>`;
    try { await api("/api/library/access", c); const r = await api("/api/library/test", {}); if (body.isConnected) res.innerHTML = testResult({...r, age_hours: 0}); if (window.loadSignins) loadSignins(); const proxy = ["openathens", "ezproxy"].includes(r.mode); if (proxy && r.state === "ok") renderLibraryAccessSigned(body, r); else if (proxy && r.state === "login") { const s = $("#la-signed", body); if (s) s.remove(); } }
    catch (er) { if (body.isConnected) { res.innerHTML = ""; note(msg, er.message, true); } }
    finally { go.disabled = false; }
  };
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
