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
// ---------------------------------------------------------------- "is there an update?" in plain words
// The server (updater.py) says what kind of problem the last look at the update feed had (U.problem.kind, from the type of the failure, never from its wording);
// this turns that into a sentence for people. tone: ok | neutral (nothing wrong) | info (an amber notice: a setting, the network, a server) | bad (red: only a signature that did not check out, or something unexpected).
export function explainUpdate(U) {
  const mk = (kind, tone, title, detail = "") => ({kind, tone, title, detail});
  const p = U.problem, code = p && p.code ? ` (${p.code})` : "";
  if (!U.feed) return mk("no_feed", "neutral", "No update address is set up, so this copy doesn’t look for updates.");
  if (p) {
    if (U.no_release) return mk("no_release", "neutral", "No update found yet", `The built-in update address has nothing at it so far (the server said it could not find it). That is what you see before the first update is published, and there is nothing for you to do. ${U.auto ? "A button appears in the top bar when one is found." : "Press Check for updates now and then."} If this stays after an update was announced, tell the author.`);
    switch (p.kind) {
      case "not_found": return mk("not_found", "info", "The update address doesn’t lead to an update file", `The server could not find the file${code}. ${U.source === "yours" ? "Check the address under Update address, or clear it to use the built-in one." : "The file may have been moved or taken down."}`);
      case "denied": return mk("denied", "info", "The update server wouldn’t hand over the file", `The server refused the request${code}. A VPN, a company network or a private download page can cause this; the app can only read public downloads.`);
      case "busy": return mk("busy", "info", "The update server is busy", `It answered with an error${code} that usually clears by itself. ${U.auto ? "Journal Watcher tries again in a few hours; or press Check for updates later." : "Press Check for updates later."}`);
      case "http": return mk("http", "info", "The update server answered with an error", `It answered with error ${p.code || "?"}. Try again later.`);
      case "offline": return mk("offline", "info", "Can’t reach the update server", `Check that this computer is online (and your VPN, if you use one), then press Check for updates.${U.auto ? " Journal Watcher also tries again by itself every six hours." : ""}`);
      case "timeout": return mk("timeout", "info", "The update server didn’t answer in time", "The connection may be slow or blocked. Try again in a moment.");
      case "tls": return mk("tls", "info", "The secure connection to the update server failed", "A VPN, a company firewall or a wrong date or time on this computer can cause this. Nothing was changed.");
      case "garbled": return mk("garbled", "info", "The update server sent something the app can’t read", "This can happen on Wi-Fi that wants you to sign in first, or when the address points to a web page instead of the update file. Nothing was changed.");
      case "unsigned": return mk("unsigned", "neutral", "A release is out, but it isn’t signed yet", `This copy only accepts updates signed by the author, so it ignores it for now. ${U.auto ? "It will be offered once the author has signed it." : "Press Check for updates again later."}`);
      case "bad_signature": return mk("bad_signature", "bad", "The update’s signature didn’t check out", "Journal Watcher ignored it and changed nothing. If this keeps happening, download the newest app from the author’s page instead of updating from inside the app.");
      case "no_verifier": return mk("no_verifier", "bad", "This copy can’t check update signatures", "So it ignores updates to be safe. Download the newest Journal Watcher from the author’s page.");
      case "address": return mk("address", "info", "The update address isn’t usable", "It must start with https:// and have no spaces or unusual characters. Fix it under Update address.");
      default: return mk("other", "bad", "Looking for updates didn’t work", "Something unexpected happened. Try again later; the details are for the author.");
    }
  }
  const when = U.checked ? new Date(U.checked).toLocaleString() : "";
  if (U.available) return mk("available", "ok", `Version ${U.latest} is ready`, when ? `Checked ${when}.` : "");
  if (U.checked_ok) return mk("current", "ok", `You have the newest version (${U.current}).`, when ? `Checked ${when}.` : "");
  return mk("never", "neutral", "Haven’t looked for updates yet", U.auto ? "Journal Watcher looks by itself a little after it starts and every six hours. Press Check for updates to look now." : "Automatic checks are switched off. Press Check for updates to look.");
}
// the facts behind a problem, for the "Details" under it (and for the author, if you write to them)
export function updateFacts(U) {
  const p = U.problem, when = U.checked ? new Date(U.checked).toLocaleString() : "";
  return [p && p.code ? `The server answered: HTTP ${p.code}` : "", U.error ? `Message: ${U.error}` : "", (p && p.url) || U.feed_url ? `Address asked: ${(p && p.url) || U.feed_url}` : "",
    when ? `Last attempt: ${when}` : "", `This copy: version ${U.current}`].filter(Boolean);
}
// a few words for the toast after Check for updates
export function updateToast(U) {
  const x = explainUpdate(U), s = t => (/[.!?]$/.test(t) ? t : t + ".");
  if (x.tone === "ok" || x.kind === "no_feed" || x.kind === "no_release") return s(x.title);
  return s(x.title) + " More under Settings → About & updates.";
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

// A progress bar for something that takes a while (Discover's "Find papers", a synthesis, a claim check…): progress(el, m) with a "status" message from the server
// ({msg, done?, total?, step?, steps?}). With counts the bar fills ("Reading reference lists · 12 of 40"); without, it moves, and the seconds that have passed are
// shown, so it is plain that the work goes on. It goes away by itself when the tool writes something else into el (or progressDone(el)).
const PROG = new WeakMap();
export function progress(el, m) {
  if (!el) return;
  m = typeof m === "string" ? {msg: m} : (m || {});
  let p = PROG.get(el);
  if (!p || !el.contains(p.node)) {
    if (p) clearInterval(p.timer);
    el.innerHTML = `<span class="jw-prog" role="progressbar" aria-label="Working" aria-valuemin="0" aria-valuemax="100"><span class="jw-bar"><i></i></span><span class="jw-pmsg" aria-live="polite"></span></span>`;
    p = {node: el.firstElementChild, t0: Date.now(), m: {}};
    p.timer = setInterval(() => { if (!el.isConnected || !el.contains(p.node)) { clearInterval(p.timer); PROG.delete(el); return; } paint(p); }, 1000);     // (written over, or taken off the page)
    PROG.set(el, p);
  }
  p.m = m;
  paint(p);
}
function paint(p) {
  const m = p.m, n = p.node, bar = n.querySelector("i"), msg = n.querySelector(".jw-pmsg");
  const part = m.total ? Math.min(1, (m.done || 0) / m.total) : null;
  const frac = m.steps ? Math.min(1, ((m.step || 1) - 1 + (part ?? 0)) / m.steps) : part;
  n.classList.toggle("ind", frac == null);
  if (frac != null) { bar.style.width = Math.round(frac * 100) + "%"; n.setAttribute("aria-valuenow", String(Math.round(frac * 100))); } else { bar.style.width = ""; n.removeAttribute("aria-valuenow"); }
  const secs = Math.round((Date.now() - p.t0) / 1000);
  msg.textContent = (m.msg || "Working…") + (m.total ? ` · ${m.done || 0} of ${m.total}` : "") + (secs >= 2 ? ` · ${secs} s` : "");
}
export function progressDone(el) {
  const p = el && PROG.get(el);
  if (p) { clearInterval(p.timer); PROG.delete(el); }
  if (el) el.textContent = "";
}

// a book's summary, said the same way on its page, in the reader and in the Chapters panel (books.overview_view gives R)
export const BOOK_HOW = "A book is summarised chapter by chapter, then as a whole: press Chapters (Library → Books), then “Summarise the whole book…”. You see what it costs before anything is sent.";
export const BOOK_STALE = "A chapter’s summary has changed since this overview was written. To bring it up to date, open Chapters (Library → Books) and press “Summarise the whole book…”: you see what it costs first.";
export function bookEmpty(R) {          // what to say when a book has no overview yet: [the sentence, whether the how-to line belongs after it]
  if (!R) return ["No summary yet.", false];
  if (!R.parts) return ["No summary yet. This book has no outline (the file carries no chapter list), so it cannot be summarised chapter by chapter.", false];
  if (R.parts_done === R.parts && !R.parts_summarised) return ["No text could be read from this book’s chapters (a scanned book?), so there is nothing to summarise.", false];
  if (R.parts_done === R.parts) return ["Every chapter and section has its summary; the overview of the whole book is not written yet. Press Chapters, then “Summarise the whole book…”: it writes only the overview, and you see what it costs first.", false];
  if (R.parts_summarised) return [`${R.parts_summarised} of ${R.parts} chapters and sections have their own summary. “Summarise the whole book…” (in Chapters) summarises the other ${R.parts - R.parts_done}, then writes the overview from all of them; you see what it costs first.`, false];
  return ["No summary yet.", true];
}
// What something costs, said one way everywhere. aboutCost: an estimate before you start ("about $0.12", "under 1 cent"); exactCost: an amount that was
// charged or is on record ("$0.12", and up to four decimals below a cent: "$0.0015"). `none` is what to say when the price is not known.
export const aboutCost = (c, none = "") => c == null ? none : c < 0.005 ? "under 1 cent" : "about $" + c.toFixed(2);
export const exactCost = (c, none = "") => c == null ? none : c > 0 && c < 0.00005 ? "under $0.0001" : c > 0 && c < 0.01 ? "$" + c.toFixed(4).replace(/0+$/, "") : "$" + c.toFixed(2);
export const usd = aboutCost;

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
