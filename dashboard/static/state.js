// Shared state and small helpers for the reader modules (reader.js, notes.js, text.js, canvas.js, ai.js).

export const $ = (s, r = document) => r.querySelector(s);
export const $$ = (s, r = document) => [...r.querySelectorAll(s)];
export const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
export const fileUrl = p => "/files/" + p.split("/").map(encodeURIComponent).join("/");
export const uid = () => Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
export const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
export const ico = (n, c = "") => `<svg class="ico ${c}" aria-hidden="true"><use href="/static/icons.svg#i-${n}"/></svg>`;
window.ico = ico;
// the heading of a page or a folder: a tool-coloured rule and icon, a small label above the title, the page's actions on the right
export const pageHead = ({tool, icon, eyebrow, title, sub = "", actions = "", id = "", cls = ""}) =>
  `<div class="card pagehead ${cls}"${id ? ` id="${id}"` : ""} data-tool="${tool}"><span class="ph-ico">${icon.startsWith("<") ? icon : ico(icon)}</span>
    <div class="ph-main"><div class="eyebrow t">${esc(eyebrow)}</div><h1>${esc(title)}</h1>${sub ? `<div class="meta">${sub}</div>` : ""}</div>${actions ? `<div class="ph-actions">${actions}</div>` : ""}</div>`;
window.pageHead = pageHead;
export const toast = m => (window.toast ? window.toast(m) : console.log(m));

// ---------------------------------------------------------------- one pattern for things that go wrong
// What went wrong, in plain words, and what to do about it. Every AI feature reports through this: a box in place of the output (errBox), a few words in a
// status line (errInline) or a toast (notifyError). A cancel you asked for is not an error and is shown quietly.
export function explainError(err) {
  const msg = String((err && err.message) || err || "").replace(/\s+/g, " ").trim(), st = err && err.status;
  const settings = [{act: "settings", label: "Open AI settings"}], retry = {act: "retry", label: "Try again"};
  const mk = (kind, title, detail, actions = []) => ({kind, title, detail, actions});
  if (/^Cancelled/i.test(msg)) return mk("cancelled", "Cancelled", "");
  if ((err instanceof TypeError) || /Failed to fetch|NetworkError|Load failed|network error/i.test(msg)) return mk("offline", "Can’t reach the dashboard", "The dashboard may have been closed. Open Journal Watcher again (or start Journal Watcher.command), then reload this page.", [retry]);
  if (/budget/i.test(msg)) return mk("budget", "The monthly AI budget is used up", msg, [{act: "usage", label: "See usage and cost"}, ...settings, retry]);
  if (/Local-only/i.test(msg)) return mk("setup", "Local-only mode is on", msg, [...settings, retry]);
  if (/isn’t set up|isn't set up|add .*\.env|No provider|no model|unknown provider|aren’t installed|isn’t installed|No model chosen/i.test(msg)) return mk("setup", "The AI isn’t set up for this", msg, [...settings, retry]);
  if (/401|403|invalid.*(api )?key|authentication|permission denied|refused the key/i.test(msg) || st === 401 || st === 403) return mk("auth", "The AI service didn’t accept the key", msg, [...settings, retry]);
  if (/rate.?limit|429|overloaded|529|too many requests|try again in/i.test(msg)) return mk("busy", "The AI service is busy", "Wait a minute and try again. " + msg, [retry]);
  if (/Can’t reach|can't reach|timed out|timeout|Connection (reset|refused)|temporarily/i.test(msg)) return mk("net", "Can’t reach the AI service", msg, [retry]);
  if (/cut off|came back empty|didn’t return|no answer|returned no/i.test(msg)) return mk("empty", "The answer didn’t come out right", msg, [retry]);
  const sw = /^Something went wrong:?\s*/i.exec(msg);
  return mk("error", sw ? "Something went wrong" : "That didn’t work", msg.replace(/^Something went wrong:?\s*/i, "").slice(0, 300), [retry]);
}
// retry = the attribute string of the button the page already handles, e.g. 'data-k="make"'; without it no "Try again" is shown
export function errBox(err, {retry = ""} = {}) {
  const x = explainError(err);
  if (x.kind === "cancelled") return `<div class="errbox cancelled" role="status">${ico("x", "sm")}<div class="eb-b"><b>Cancelled.</b> <span class="eb-d">Nothing was kept.</span>${retry ? ` <button class="rd-btn" ${retry}>Ask again</button>` : ""}</div></div>`;
  const btns = x.actions.map(a => a.act === "retry" ? (retry ? `<button class="rd-btn" ${retry}>${esc(a.label)}</button>` : "") : `<button class="rd-btn" data-eb="${a.act}">${esc(a.label)}</button>`).join("");
  return `<div class="errbox ${x.kind}" role="alert">${ico("warn", "sm")}<div class="eb-b"><b>${esc(x.title)}</b>${x.detail ? `<div class="eb-d">${esc(x.detail)}</div>` : ""}${btns ? `<div class="eb-a">${btns}</div>` : ""}</div></div>`;
}
export const errInline = err => { const x = explainError(err); return `<span class="err-inline ${x.kind}">${esc(x.title)}${x.detail && x.kind !== "cancelled" ? ": " + esc(x.detail.slice(0, 140)) : ""}</span>`; };
export function notifyError(err) {
  const x = explainError(err); if (x.kind !== "cancelled") console.warn(err);
  toast(x.kind === "cancelled" ? "Cancelled." : x.kind === "error" && x.title === "That didn’t work" ? x.detail : x.title + (x.detail ? ". " + x.detail.slice(0, 160) : ""));
}
window.notifyError = notifyError; window.errBox = errBox;
document.addEventListener("click", e => {          // the buttons of an error box that lead somewhere else
  const b = e.target.closest("[data-eb]"); if (!b) return;
  if (b.dataset.eb === "settings") window.Settings && window.Settings.open("ai"); else if (b.dataset.eb === "usage") window.Settings && window.Settings.open("usage");
});
document.addEventListener("click", e => { const r = e.target.closest("[data-retry]"); if (r) { const t = document.querySelector(r.dataset.retry); if (t) t.click(); } });   // "Try again" for a page whose own button starts the work

export async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { const e = new Error(j.error || r.statusText); e.status = r.status; e.body = j; throw e; }
  return j;
}

export function debounce(fn, ms) {
  let t = null;
  const d = (...a) => { clearTimeout(t); t = setTimeout(() => { t = null; fn(...a); }, ms); };
  d.flush = (...a) => { if (t !== null) { clearTimeout(t); t = null; fn(...a); } };
  return d;
}

// markdown from the model or from a summary file: raw HTML is neutralised, as in the dashboard's loadSummary
export const md = s => window.marked ? window.marked.parse(String(s || "").replace(/</g, "&lt;")) : `<pre>${esc(s)}</pre>`;

// 8 highlight colours (LiquidText has 32; these are the ones used most)
export const COLORS = [
  {id: "yellow", name: "Yellow", c: "#ffe066"}, {id: "green", name: "Green", c: "#8ce99a"},
  {id: "blue", name: "Blue", c: "#74c0fc"}, {id: "pink", name: "Pink", c: "#faa2c1"},
  {id: "orange", name: "Orange", c: "#ffc078"}, {id: "purple", name: "Purple", c: "#d0bfff"},
  {id: "red", name: "Red", c: "#ff8787"}, {id: "gray", name: "Gray", c: "#ced4da"},
];
export const colorOf = id => (COLORS.find(c => c.id === id) || COLORS[0]).c;
export const legend = {};           // what you named the colours (Settings -> Reader); filled by the reader
export const colorName = id => legend[id] || (COLORS.find(c => c.id === id) || COLORS[0]).name;

// the one open paper; everything else reads and writes this
export const S = {
  open: false, file: null, meta: null, pdf: null,
  pages: [],           // {n, proxy, w, h, cv, tm, bands:[]}  w/h in PDF points at scale 1
  scale: 1.25,
  notes: null, rev: 0, tags: [], tagsRev: 0,
  save: "saved",       // saved | saving | dirty | conflict | error
  view: {keep: null, source: null, context: 2},   // keep = Map(page -> Set(line index)) when collapsed
  find: {q: "", hits: [], cur: -1},
  lastColor: "yellow",
  fn: {},              // functions other modules call without importing reader.js (jump, ...)
};

// tiny event bus: notes:changed, tags:changed, tab:show ...
export const bus = new EventTarget();
export const emit = (name, detail) => bus.dispatchEvent(new CustomEvent(name, {detail}));
export const on = (name, fn) => bus.addEventListener(name, e => fn(e.detail));
