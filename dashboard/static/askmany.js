// "Ask" over several papers: a folder's Ask tab, and the Library's Ask tab (the whole library, narrowed by folder / journal / year / tag / status).
// Answers cite numbered passages from different papers; each number opens that paper at that place. Two ways to read: the closest passages
// (deeper) or the papers' cards (cheaper, every paper that has one). The conversation is saved (folder: Papers/_folders/<id>.ask.json).
// POST /api/ai/askmany streams {type: status|scope|passages|text|done|error}.
import {$, $$, esc, api, toast, debounce, md, aboutCost} from "./state.js";
import {wire as wireOut} from "./output.js";
import {turnHtml as askTurn, actionButtons} from "./askview.js";

let ctx = null;
const STARTERS = ["What do these papers conclude, and where do they disagree?", "Which theories and methods do they use?", "What limitations do the authors name, and what future research do they suggest?",
  "Which findings seem to contradict each other, and why might that be?"];
const money = aboutCost;

function openAt(file, page, quote) {
  const row = ctx.getLib().find(r => r.file === file);
  if (row) ctx.readPaper(row, {goto: {page: page || 1, quote: quote || ""}}); else toast("That paper is no longer in your library.");
}

export function mount(el, opts = {}) {
  const S = {el, folder: opts.folder || null, scope: {}, mode: "passages", turns: [], busy: false, info: null, models: ""};
  try { S.mode = localStorage.getItem("jw-askmany-mode") || "passages"; } catch (e) { /* private window */ }
  const lib = opts.folder ? "" : `<select data-s="folder"><option value="">Any folder</option></select><select data-s="journal"><option value="">Any journal</option></select>
      <select data-s="year"><option value="">Any year</option></select><select data-s="tag"><option value="">Any tag</option></select>
      <select data-s="status"><option value="">Any status</option><option value="toread">To read</option><option value="reading">Reading</option><option value="read">Read</option></select>`;
  el.innerHTML = `<div class="card am-bar"><div class="row" style="gap:8px;flex-wrap:wrap;align-items:center">${lib}
      <select data-s="mode" title="What the model reads"><option value="passages">Read the closest passages (deeper)</option><option value="cards">Read the cards (every paper, cheaper)</option></select>
      <button class="btn small" data-k="clear" title="Start a new conversation">🗑</button></div><div class="pmeta" id="am-info" style="margin-top:6px"></div></div>
    <div class="am-log" id="am-log"></div>
    <div class="card am-box"><textarea id="am-q" rows="2" placeholder="Ask all of these papers a question…  (Ctrl/⌘+Enter)"></textarea><button class="btn primary" data-k="send">Ask</button></div>`;
  S.log = $("#am-log", el); S.input = $("#am-q", el); wireOut(S.log); $('[data-s="mode"]', el).value = S.mode;
  el.onclick = e => onClick(S, e); el.onchange = e => onChange(S, e);
  S.input.addEventListener("keydown", e => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); submit(S); } });
  S.save = () => fetch("/api/ask/chat", {method: "POST", headers: {"Content-Type": "application/json"}, keepalive: true,
    body: JSON.stringify({folder: S.folder || "", turns: S.turns.filter(t => t.done || t.text).map(({q, text, passages, scope, mode, model, cost, cached, invalid, cut_off, label}) => ({q, text, passages, scope, mode, model, cost, cached, invalid, cut_off, label, done: true}))})}).catch(() => {});
  api("/api/ask/chat" + (S.folder ? "?folder=" + encodeURIComponent(S.folder) : "")).then(r => { S.turns = r.turns || []; draw(S); }).catch(() => draw(S));
  if (!S.folder) fillSelects(S);
  refreshInfo(S);
}

async function fillSelects(S) {
  const L = ctx.getLib(), sel = k => $(`[data-s="${k}"]`, S.el), opt = (v, t) => `<option value="${esc(v)}">${esc(t)}</option>`;
  const js = [...new Map(L.map(r => [r.abbr, r.journal])).entries()].sort((a, b) => a[1].localeCompare(b[1])), ys = [...new Set(L.map(r => r.year).filter(Boolean))].sort().reverse();
  let folders = [], tags = [];
  try { folders = await api("/api/folders"); tags = (await api("/api/tags")).tags; } catch (e) { /* the filters are optional */ }
  sel("folder").insertAdjacentHTML("beforeend", folders.map(f => opt(f.id, "📁 " + f.name + " (" + f.count + ")")).join(""));
  sel("journal").insertAdjacentHTML("beforeend", js.map(([a, n]) => opt(a, n)).join(""));
  sel("year").insertAdjacentHTML("beforeend", ys.map(y => opt(y, y)).join(""));
  sel("tag").insertAdjacentHTML("beforeend", tags.map(t => opt(t.id, (t.category ? t.category + ": " : "") + t.name)).join(""));
}
const scopeQuery = S => Object.entries({folder: S.folder || S.scope.folder || "", ...S.scope}).filter(([, v]) => v).map(([k, v]) => k + "=" + encodeURIComponent(v)).join("&");
async function refreshInfo(S) {
  const box = $("#am-info", S.el); if (!box) return;
  try {
    const i = S.info = await api("/api/ask/scope?" + scopeQuery(S)), est = i.estimate[S.mode];
    const parts = [`<b>${i.papers}</b> paper${i.papers === 1 ? "" : "s"}`, `${i.cards} with a card`];
    if (S.mode === "passages" && i.unindexed) parts.push(`${i.unindexed} to read for the first time (the first question takes about ${Math.max(5, i.seconds)} seconds)`);
    if (S.mode === "cards" && i.cards < i.papers) parts.push(`<span class="cd-tag">${i.papers - i.cards} without a card are left out</span>`);
    box.innerHTML = parts.join(" · ") + (est != null ? ` · each question ${money(est)}` : "") + (!i.papers ? ` <span class="ask-warn" style="display:inline-block;margin:0">No papers match.</span>` : "");
  } catch (e) { box.textContent = e.message; }
}
function onChange(S, e) {
  const s = e.target.closest("[data-s]"); if (!s) return;
  const k = s.dataset.s;
  if (k === "mode") { S.mode = s.value; try { localStorage.setItem("jw-askmany-mode", S.mode); } catch (er) { /* fine */ } } else S.scope[k] = s.value;
  refreshInfo(S);
}
function onClick(S, e) {
  const t = e.target, k = t.closest("[data-k]")?.dataset.k;
  if (k === "send") return submit(S);
  if (k === "clear") { if (S.turns.length && !confirm("Clear this conversation?")) return; S.turns = []; draw(S); S.save(); return; }
  const st = t.closest("[data-starter]"); if (st) return ask(S, st.dataset.starter);
  const c = t.closest(".cite"), turn = t.closest(".ask-turn");
  if (c && turn) { const p = (S.turns[+turn.dataset.i].passages || []).find(x => x.n === +c.dataset.n); if (p) openAt(p.file, p.page, p.quote); return; }
  const a = t.closest("[data-t]")?.dataset.t; if (a && turn) turnAction(S, +turn.dataset.i, a);
}
const submit = S => { const q = S.input.value.trim(); if (q) { S.input.value = ""; ask(S, q); } };

function draw(S) {
  let h = "";
  if (!S.turns.length) h += `<div class="card empty" style="text-align:left"><b>Ask ${S.folder ? "this folder" : "your library"}</b><br>Answers name the papers and cite passages; click a number to open that paper at that place.<br><br>${STARTERS.map(q => `<button class="rd-btn" data-starter="${esc(q)}" style="margin:3px;text-align:left">${esc(q)}</button>`).join("")}</div>`;
  h += S.turns.map((t, i) => turnHtml(S, t, i)).join("");
  S.log.innerHTML = h; S.log.scrollTop = S.log.scrollHeight;
}
function sources(t) {
  const ps = t.passages || []; if (!ps.length) return null;
  const by = new Map(); ps.forEach(p => (by.get(p.file) || by.set(p.file, []).get(p.file)).push(p));
  return {count: ps.length, note: `from ${by.size} paper${by.size === 1 ? "" : "s"}`, html: `<div class="ask-src"><div class="ask-src-h">${ps.length} passages from ${by.size} paper${by.size === 1 ? "" : "s"}</div>${[...by].map(([f, xs]) => `<div class="am-src"><b>${esc(xs[0].cite)}</b> <span class="pmeta">${esc(xs[0].title.slice(0, 80))}</span><ul style="margin:3px 0 6px 18px;padding:0">${xs.map(p =>
    `<li value="${p.n}"><button class="cite" data-n="${p.n}">${p.n}</button> p. ${p.page}${p.section ? " · " + esc(p.section) : ""} — ${esc((p.quote || "").slice(0, 120))}…</li>`).join("")}</ul></div>`).join("")}</div>`};
}
function turnHtml(S, t, i) {
  const miss = t.scope && t.scope.missing_cards && t.scope.missing_cards.length ? `<div class="of-co warn">${window.ico("warn", "sm")}<div>Not included (no card): ${t.scope.missing_cards.map(m => esc(m.title.slice(0, 50))).join(" · ")}</div></div>` : "";
  return askTurn(t, i, {key: "askmany:" + (S.folder || "library"), sources: sources(t), notices: miss, actions: t.done ? actionButtons().replace(/<button[^>]*data-t="save"[^>]*>.*?<\/button>/, "") : ""});
}
const redraw = (S, i) => { const el = S.log.querySelector(`.ask-turn[data-i="${i}"]`); if (el) { el.outerHTML = turnHtml(S, S.turns[i], i); S.log.scrollTop = S.log.scrollHeight; } };
async function ask(S, q, replace, fresh) {
  if (S.busy) return toast("Still answering the last question.");
  S.busy = true;
  const label = S.mode === "cards" ? "cards" : "passages", t = {q, text: "", passages: [], status: "Searching…", done: false, mode: S.mode, label: label + " ·"};
  let i = replace; if (i == null) { S.turns.push(t); i = S.turns.length - 1; } else S.turns[i] = t;
  draw(S);
  const hist = S.turns.slice(0, i).filter(x => x.done && !x.error && x.text).slice(-4).map(x => ({q: x.q, a: x.text}));
  try {
    const r = await fetch("/api/ai/askmany", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({scope: {folder: S.folder || S.scope.folder || "", ...S.scope}, question: q, mode: S.mode, history: hist, fresh: !!fresh})});
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
    const rd = r.body.getReader(), dec = new TextDecoder(); let buf = "";
    for (;;) {
      const {value, done} = await rd.read(); if (done) break; buf += dec.decode(value, {stream: true}); let k;
      while ((k = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, k).trim(); buf = buf.slice(k + 1); if (!line) continue; const m = JSON.parse(line);
        if (m.type === "status") t.status = m.msg; else if (m.type === "scope") t.scope = m; else if (m.type === "passages") t.passages = m.passages;
        else if (m.type === "text") t.text += m.delta; else if (m.type === "error") t.error = m.error;
        else if (m.type === "done") Object.assign(t, {done: true, model: m.model, cost: m.cost, cached: m.cached, invalid: m.invalid, cut_off: m.cut_off || undefined});
        redraw(S, i);
      }
    }
    if (!t.error && !t.text) t.error = "The model returned no answer.";
  } catch (e) { t.error = e.message; }
  t.done = !t.error; S.busy = false; redraw(S, i); if (t.done) { S.save(); refreshInfo(S); }
}
function turnAction(S, i, a) {
  const t = S.turns[i]; if (!t) return;
  if (a === "regen") return ask(S, t.q, i, true);
  if (a === "copy") navigator.clipboard.writeText(t.text).then(() => toast("Answer copied."));
}

window.AskUI = {init(c) { ctx = c; }, mount};
