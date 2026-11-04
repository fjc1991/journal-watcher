// "Ask" tab: a saved conversation about the open paper. Answers come from numbered passages and cite them; each citation number jumps to
// the passage. Follow-ups remember the conversation; ⟳ asks again; ⇄ asks a second model the same question side by side; 💡 suggests what
// to ask next. The server (POST /api/ai/ask) streams newline-delimited JSON:
//   {type:"status", msg} {type:"passages", passages:[{n,page,quote}]} {type:"text", delta} {type:"done", model, provider, cost, cached, invalid} {type:"error", error}
import {notifyError, S, $, esc, toast, on, debounce, api} from "./state.js";
import * as N from "./notes.js";
import * as C from "./canvas.js";
import {wireCites} from "./cite.js";
import {wire as wireOut} from "./output.js";
import {turnHtml as askTurn, actionButtons, compareButtons} from "./askview.js";

let panel, log, input, busy = false, ctxText = "", CFG = null;
let turns = [];            // [{q, text, passages, model, cost, cached, invalid, done, error, alt?, follow?, chap?, pages?}]
let chapters = [], chap = -1;          // a book's chapters, and the one the questions are limited to (-1: the whole book)
const curPages = () => chap >= 0 && chapters[chap] ? {from: chapters[chap].start, to: chapters[chap].end, title: chapters[chap].title} : null;
const STARTERS = ["What is the main contribution of this paper?", "What theory, method and sample does it use?",
  "List the hypotheses or propositions and whether they were supported.", "What are the limitations and the suggested future research?"];
const saveChat = debounce(() => fetch("/api/doc/chat", {method: "POST", headers: {"Content-Type": "application/json"}, keepalive: true,
  body: JSON.stringify({file: S.file, turns: turns.filter(t => t.done || t.text).map(({q, text, passages, model, cost, cached, invalid, follow, alt, pages}) => ({q, text, passages, model, cost, cached, invalid, follow, alt, pages, done: true}))})}).catch(() => {}), 600);

export function mount(el) {
  panel = el; S.fn.askAbout = askAbout;
  el.innerHTML = `<div class="rd-sub"><span id="ask-model" style="flex:1;color:var(--ink-3)"></span><span id="ask-chap"></span>
      <select id="ask-mode" title="What the model reads"><option value="rag">Relevant passages</option><option value="full">Whole paper</option></select>
      <button class="rd-btn" data-k="clear" title="Start a new conversation">🗑</button><button class="rd-btn" data-k="cfg" title="AI provider and model (Settings)">⚙</button></div>
    <div class="ask-log" id="ask-log"></div>
    <div class="ask-box"><textarea id="ask-q" rows="1" placeholder="Ask about this paper…  (Ctrl/⌘+Enter)"></textarea><button class="btn primary small" data-k="send">Ask</button></div>`;
  log = $("#ask-log", el); input = $("#ask-q", el); wireOut(log);
  try { $("#ask-mode", el).value = localStorage.getItem("jw-ask-mode") || "rag"; } catch (e) {}
  $("#ask-mode", el).onchange = e => { try { localStorage.setItem("jw-ask-mode", e.target.value); } catch (er) {} };
  el.addEventListener("click", e => {
    const k = e.target.closest("[data-k]")?.dataset.k, q = e.target.closest("[data-starter]"), t = e.target.closest("[data-t]");
    if (k === "send") submit(); else if (k === "cfg") settings(); else if (k === "clear") clearChat(); else if (k === "suggest") suggest();
    else if (q) ask(q.dataset.starter);
    else if (t) turnAction(+t.closest(".ask-turn").dataset.i, t.dataset.t);
  });
  input.addEventListener("keydown", e => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); submit(); } });
  input.addEventListener("input", () => { input.style.height = "auto"; input.style.height = Math.min(140, input.scrollHeight) + "px"; });
  wireCites(el, b => { const t = turns[+b.closest(".ask-turn")?.dataset.i]; return t && (b.closest(".ask-alt") && t.alt ? t.alt.passages : t.passages); });
  on("paper:opened", async () => { loadBook(); turns = []; ctxText = ""; draw(); status(); try { turns = (await api("/api/doc/chat?file=" + encodeURIComponent(S.file))).turns || []; draw(); } catch (e) { /* no saved chat */ } });
  on("reader:closed", () => { turns = []; });
  window.addEventListener("jw-settings-changed", () => status());
  draw();
}

// a book (not a paper) has chapters: the questions can be limited to one, so the model reads only that part
async function loadBook() {
  chapters = []; chap = -1; const slot = $("#ask-chap", panel); if (slot) slot.innerHTML = "";
  let b; try { b = await api("/api/book?file=" + encodeURIComponent(S.file)); } catch (e) { return; }              // 404: it is a paper
  chapters = b.chapters || []; if (!chapters.length || !slot) return;
  slot.innerHTML = `<select id="ask-chapsel" aria-label="What the model reads" title="Read the whole book, or only one chapter of it"><option value="-1">Whole book</option>${chapters.map(c => `<option value="${c.i}">${"– ".repeat(c.level - 1)}${esc(c.title)}</option>`).join("")}</select>`;
  const sel = $("#ask-chapsel", slot);
  sel.onchange = () => { chap = +sel.value; };
  const want = window.__askChapter && window.__askChapter.file === S.file ? window.__askChapter.i : -1; window.__askChapter = null;      // "Ask about it" in the Library's chapter list
  if (want >= 0 && chapters[want]) { chap = want; sel.value = String(want); }
}
const submit = () => { const q = input.value.trim(); if (q) { input.value = ""; input.style.height = "auto"; ask(q); } };
export function askAbout(text) { ctxText = text; draw(); input.value = ""; input.focus(); }
function clearChat() {
  if (turns.length && !confirm("Clear this conversation? (It is only stored next to the paper.)")) return;
  turns = []; ctxText = ""; draw();
  fetch("/api/doc/chat", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({file: S.file, turns: []})}).catch(() => {});
}

function draw() {
  if (!log) return;
  let h = "";
  if (ctxText) h += `<div class="ask-turn"><div class="ask-q">About this passage</div><div class="ask-a"><i>“${esc(ctxText.slice(0, 400))}${ctxText.length > 400 ? "…" : ""}”</i></div>
    <div class="ask-meta">${["Explain this in plain words", "What evidence supports this?", "How does this connect to the rest of the paper?", "What is a counter-argument?"].map(s => `<button class="rd-btn" data-starter="${esc(s)}">${esc(s)}</button>`).join(" ")}</div></div>`;
  if (!turns.length && !ctxText) h += `<div class="rd-empty">Ask anything about this paper. Answers use only passages from the paper and cite them — click a number to jump there.<br><br>
    <button class="rd-btn" data-k="suggest" style="margin:3px">💡 Suggest questions for this paper</button><br>${STARTERS.map(s => `<button class="rd-btn" data-starter="${esc(s)}" style="margin:3px">${esc(s)}</button>`).join("")}</div>`;
  turns.forEach((t, i) => { h += turnHtml(t, i); });
  log.innerHTML = h; log.scrollTop = log.scrollHeight;
}
function turnHtml(t, i) {
  const sources = t.passages && t.passages.length ? {count: t.passages.length, note: "", html: `<ol class="ask-src" style="margin:0;padding-left:20px">${t.passages.map(p =>
    `<li value="${p.n}"><button class="cite" data-n="${p.n}">${p.n}</button> ${esc(p.where || "p. " + p.page)} — ${esc(p.quote.slice(0, 140))}…</li>`).join("")}</ol>`, ...(t.pages ? {note: ` Only “${t.pages.title}” was read for this answer.`} : {})} : null;
  return askTurn(t, i, {key: "ask:" + S.file, sources, actions: t.done ? actionButtons(compareButtons()) : ""});
}

// ---------------------------------------------------------------- asking
const histBefore = i => turns.slice(0, i).filter(x => x.done && !x.error && x.text).slice(-4).map(x => ({q: x.q, a: x.text}));
async function run(q, extra, onMsg) {
  const r = await fetch("/api/ai/ask", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({file: S.file, question: q, mode: $("#ask-mode", panel).value, history: extra.history, override: extra.override, fresh: extra.fresh, pages: extra.pages ? {from: extra.pages.from, to: extra.pages.to} : undefined})});
  if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(r.status === 404 || r.status === 501 ? "The AI backend isn’t installed on this server yet." : (j.error || r.statusText)); }
  const rd = r.body.getReader(), dec = new TextDecoder(); let buf = "";
  for (;;) {
    const {value, done} = await rd.read(); if (done) break;
    buf += dec.decode(value, {stream: true}); let i;
    while ((i = buf.indexOf("\n")) >= 0) { const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1); if (line) onMsg(JSON.parse(line)); }
  }
}
function apply(t, m) {
  if (m.type === "status") t.status = m.msg; else if (m.type === "passages") t.passages = m.passages;
  else if (m.type === "text") t.text += m.delta; else if (m.type === "error") t.error = m.error;
  else if (m.type === "done") Object.assign(t, {done: true, model: (m.provider ? m.provider + " · " : "") + (m.model || ""), cost: m.cost, cached: m.cached, invalid: m.invalid});
}
const redraw = i => { const el = log.querySelector(`.ask-turn[data-i="${i}"]`); if (el) { el.outerHTML = turnHtml(turns[i], i); log.scrollTop = log.scrollHeight; } };

export async function ask(q, opts = {}) {
  if (busy) return toast("Still answering the last question.");
  busy = true; const full = ctxText ? `${q}\n\nPassage from the paper: “${ctxText}”` : q; ctxText = "";
  const prev = opts.replace != null ? turns[opts.replace] : null, pages = prev ? prev.pages : curPages();         // asking again keeps the chapter the first answer was limited to
  const t = {q, text: "", passages: [], status: "Reading the paper…", done: false, pages};
  let i = opts.replace;
  if (i == null) { turns.push(t); i = turns.length - 1; } else turns[i] = t;
  draw();
  try { await run(full, {fresh: opts.fresh, history: histBefore(i), pages}, m => { apply(t, m); redraw(i); }); if (!t.error && !t.text) t.error = "The model returned no answer."; }
  catch (e) { t.error = e.message; }
  t.done = !t.error; busy = false; redraw(i); status(); if (t.done) saveChat();
}
async function compare(i, over) {
  const t = turns[i]; if (busy || !t) return; busy = true;
  t.alt = {text: "", passages: t.passages, status: "Asking " + over.model + "…", done: false, want: over.provider + " · " + over.model}; redraw(i);
  try { await run(t.q, {override: over, history: histBefore(i), pages: t.pages}, m => { apply(t.alt, m); redraw(i); }); if (!t.alt.error && !t.alt.text) t.alt.error = "No answer."; } catch (e) { t.alt.error = e.message; }
  t.alt.done = !t.alt.error; busy = false; redraw(i); status(); saveChat();
}
async function turnAction(i, a) {
  const t = turns[i]; if (!t) return;
  if (a === "regen") return ask(t.q, {replace: i, fresh: true});
  if (a === "copy") return S.fn.copyText(t.text, "Answer copied.");
  if (a === "save") { const p = C.nextPos(); N.addCard({type: "ai", question: t.q, text: t.text, cites: t.passages, model: t.model, x: p.x, y: p.y, w: 340}); S.fn.showTab("notes"); return toast("Saved to your notes."); }
  if (a === "usealt" && t.alt) { Object.assign(t, {text: t.alt.text, model: t.alt.model, cost: t.alt.cost, cached: t.alt.cached, invalid: t.alt.invalid}); delete t.alt; redraw(i); return saveChat(); }
  if (a === "dropalt") { delete t.alt; redraw(i); return saveChat(); }
  if (a === "compare") return comparePick(i);
  if (a === "follow") { try { const r = await api("/api/ai/suggest", {file: S.file, q: t.q, a: t.text}); t.follow = r.questions; redraw(i); saveChat(); } catch (e) { notifyError(e); } }
}
async function suggest() {
  try {
    const r = await api("/api/ai/suggest", {file: S.file}), el = log.querySelector(".rd-empty");
    if (el) el.innerHTML = `<b>Questions worth asking</b><br><br>` + r.questions.map(q => `<button class="rd-btn" data-starter="${esc(q)}" style="margin:3px;text-align:left">${esc(q)}</button>`).join("") + `<br><br><button class="rd-btn" data-k="suggest">💡 More</button>`;
  } catch (e) { notifyError(e); }
}
// choose which other model should answer the same question
async function comparePick(i) {
  if (!CFG) await status();
  const opts = []; Object.entries((CFG && CFG.providers) || {}).forEach(([p, v]) => { if (v.configured) (v.models || []).forEach(m => opts.push({provider: p, model: m})); });
  const cur = (CFG && CFG.features && CFG.features.ask) || {}, cand = opts.filter(o => !(o.provider === cur.provider && o.model === cur.model));
  let pick = cand.length === 1 ? cand[0] : null;
  if (!pick) {
    const ans = prompt("Compare with which model?\n" + (cand.length ? cand.map((o, n) => `${n + 1}. ${o.provider} · ${o.model}`).join("\n") + "\n\nType a number, or provider/model:"
      : "No other models are listed yet (add them in Settings → AI).\nType provider/model, e.g. openai/gpt-…"), cand.length ? "1" : "");
    if (!ans) return;
    pick = /^\d+$/.test(ans.trim()) && cand[+ans.trim() - 1] ? cand[+ans.trim() - 1] : (() => { const [p, ...m] = ans.trim().split("/"); return {provider: p.trim(), model: m.join("/").trim()}; })();
  }
  if (!pick.provider || !pick.model) return toast("Use provider/model, e.g. openai/gpt-…");
  compare(i, pick);
}

async function status() {
  try {
    CFG = await api("/api/ai/config"); const f = (CFG.features || {}).ask || {};
    $("#ask-model", panel).textContent = (CFG.privacy && CFG.privacy.local_only ? "🔒 local only · " : "") + (f.model ? `${f.provider} · ${f.model}` : "No model chosen — click ⚙");
  } catch (e) { $("#ask-model", panel).textContent = "AI backend not available"; }
}
function settings() { if (window.Settings) window.Settings.open("ai"); else toast("Settings are still loading."); }
