// The AI activity tray: what the AI is doing right now and what it just did, from any page. The server pushes every change of an AI job (a card, an answer, a matrix
// fill, an ideas run, the library indexer…) over the event stream; the button in the top bar shows how many are running and what they have cost, and its panel lists
// them with progress, cost and a Cancel button (the work stops at the next piece of the answer; what was already generated is still billed). Finished jobs stay in the
// list for a while with a link to where the result is. The panel also holds the background-indexing switch ("Index now", on/off): reading the library into the
// index for Ask happens by itself with the local model, so the first question over a folder or the whole library does not wait for it.
import {notifyError, $, $$, esc, api, ico, toast} from "./state.js";

let ctx = null, btn = null, panel = null, jobs = new Map(), idx = {enabled: true, local: true, running: false, pending: null}, usage = null, tick = 0;
const TOOL = {card: "card", cards: "card", skim: "skim", ask: "ask", askmany: "ask", explain: "explain", summary: "sum", matrix: "matrix", ideas: "ideas", draft: "draft", claims: "claims", synthesis: "synthesis", flashcards: "review", audio: "explain", review: "claims", index: "library"};
const ICON = {card: "card", cards: "card", skim: "skim", ask: "ask", askmany: "ask", explain: "sparkle", summary: "sum", matrix: "matrix", ideas: "ideas", draft: "draft", claims: "claims", synthesis: "synthesis", flashcards: "review", audio: "sparkle", review: "claims", index: "library"};
const money = c => c == null ? "" : c < 0.001 ? "$0.00" : c < 0.1 ? "$" + c.toFixed(3) : "$" + c.toFixed(2);
const running = () => [...jobs.values()].filter(j => !j.ended);
const ago = t => { const s = Math.max(0, Date.now() / 1000 - t); return s < 60 ? "just now" : s < 3600 ? Math.round(s / 60) + " min ago" : Math.round(s / 3600) + " h ago"; };
const dur = j => { const s = Math.round((j.ended || Date.now() / 1000) - j.started); return s < 60 ? s + " s" : Math.floor(s / 60) + " min " + (s % 60) + " s"; };

// ---------------------------------------------------------------- the button
function paintButton(noDraw) {
  if (!btn) return; const r = running(), n = r.length, c = r.reduce((a, j) => a + (j.cost || 0), 0);
  btn.classList.toggle("busy", n > 0);
  $(".at-t", btn).textContent = n ? `${n} running${c ? " · " + money(c) : ""}` : "AI";
  btn.title = n ? "AI is working: " + r.map(j => j.label).join("; ") : "AI activity: what ran, what it cost, cancel";
  if (!noDraw && !panel.classList.contains("hide")) draw();
}
function patchRow(j) {       // progress, message and cost of a running job, in place
  const el = $(`.at-job[data-id="${j.id}"]`, panel); if (!el) return false;
  const bar = $(".at-bar i", el); if (bar && j.total) bar.style.width = Math.min(100, 100 * j.done / j.total) + "%";
  $(".at-s", el).innerHTML = stateText(j); $(".at-c", el).textContent = j.cost ? money(j.cost) : ""; return true;
}
function bump(j) {
  const was = jobs.get(j.id); jobs.set(j.id, j);
  if (was && !was.ended && !j.ended && !!was.cancelling === !!j.cancelling && !panel.classList.contains("hide") && patchRow(j)) paintButton(true); else paintButton(); clearInterval(tick); if (running().length) tick = setInterval(() => { if (!panel.classList.contains("hide")) $$(".at-el", panel).forEach(e => { const j2 = jobs.get(e.dataset.id); if (j2 && !j2.ended) e.textContent = dur(j2); }); }, 1000); }

// ---------------------------------------------------------------- the panel
function stateText(j) {
  const live = !j.ended, st = live ? (j.cancelling ? "Cancelling…" : esc(j.msg || "Working…")) : j.status === "done" ? (j.errors ? j.errors + " problem" + (j.errors === 1 ? "" : "s") + ": " + esc(j.error) : "Done") : j.status === "cancelled" ? "Cancelled" : esc(j.error || "Failed");
  return `${live && j.total ? `<b>${j.done} of ${j.total}</b> · ` : ""}${st}${live && j.chars && !j.total ? ` · ${(j.chars / 1000).toFixed(1)}k characters` : ""}`;
}
function row(j) {
  const t = TOOL[j.kind] || "library", live = !j.ended, pct = j.total ? Math.min(100, 100 * j.done / j.total) : null;
  const open = j.ended && j.status !== "cancelled" && j.kind !== "index" && (j.scope.file || j.scope.folder || j.scope.library);
  return `<div class="at-job ${live ? "live" : j.status}" data-tool="${t}" data-id="${j.id}">
    <span class="at-ic">${ico(ICON[j.kind] || "sparkle")}</span>
    <div class="at-main"><div class="at-l">${esc(j.label)}</div>
      ${live ? `<div class="at-bar${pct == null ? " ind" : ""}"><i style="${pct != null ? `width:${pct}%` : ""}"></i></div>` : ""}
      <div class="at-s">${stateText(j)}</div></div>
    <div class="at-r"><span class="at-c">${j.cost ? money(j.cost) : ""}</span><span class="at-el" data-id="${j.id}">${live ? dur(j) : ago(j.ended)}</span>
      ${live ? `<button class="btn small" data-at="cancel" data-id="${j.id}"${j.cancelling ? " disabled" : ""}>${j.cancelling ? "…" : "Cancel"}</button>` : open ? `<button class="btn small" data-at="open" data-id="${j.id}">Open</button>` : ""}</div></div>`;
}
function draw() {
  const all = [...jobs.values()].sort((a, b) => (a.ended ? 1 : 0) - (b.ended ? 1 : 0) || (b.ended || b.started) - (a.ended || a.started)), run = all.filter(j => !j.ended), done = all.filter(j => j.ended).slice(0, 12);
  const left = idx.pending, ixrun = running().some(j => j.kind === "index");
  const spend = usage ? `Today <b>${money(usage.today)}</b> · this month <b>${money(usage.month)}</b>${usage.budget ? " of " + money(usage.budget) : ""} · <a href="#" data-at="usage">Usage &amp; cost</a>` : "";
  panel.innerHTML = `<div class="at-h"><b>AI activity</b><span class="at-sp">${spend}</span></div>
    <div class="at-body">${run.length ? `<div class="eyebrow at-g">Running</div>${run.map(row).join("")}` : `<div class="at-none">Nothing is running.</div>`}
    ${done.length ? `<div class="eyebrow at-g">Recent</div>${done.map(row).join("")}` : ""}</div>
    <div class="at-ix"><div class="at-ixt">${ico("library", "sm")}<span><b>Library index for Ask</b> ${ixrun ? "is being updated…" : left == null ? "" : left ? `has ${left} paper${left === 1 ? "" : "s"} still to read` : "is up to date"}</span>
      ${!ixrun && (left == null || left > 0) ? `<button class="btn small" data-at="index">Index now</button>` : ""}</div>
      <label class="at-sw" title="${idx.local ? "Free: it runs on this computer with the local model." : "Not available by itself: your embedding model is a paid service, so indexing only runs when you press Index now."}"><input type="checkbox" data-at="auto"${idx.enabled && idx.local ? " checked" : ""}${idx.local ? "" : " disabled"}> Read new papers into the index in the background${idx.local ? "" : " (needs the local model)"}</label></div>`;
}

async function refresh(full) {
  try {
    const r = await api("/api/activity" + (full ? "?pending=1" : ""));
    r.jobs.forEach(j => jobs.set(j.id, j)); idx = {...idx, ...r.indexing}; paintButton();
  } catch (e) { /* the AI features are not installed */ }
}
async function usageNow() {
  try { const u = await api("/api/ai/usage"), day = new Date().toISOString().slice(0, 10); usage = {today: (u.by_day || {})[day] || 0, month: (u.month || {}).cost || 0, budget: u.budget}; if (!panel.classList.contains("hide")) draw(); } catch (e) { /* no AI */ }
}
function openJob(j) {
  const s = j.scope || {}; panel.classList.add("hide");
  const lib = ctx.getLib();
  if (s.file) { const r = lib.find(x => x.file === s.file); if (r) return ctx.readPaper(r, s.tab ? {tab: s.tab} : undefined); }
  if (s.folder) return nav({view: "lib", j: "@" + s.folder, tab: "folders", ftab: s.ftab});
  if (s.library) return nav({view: "lib", j: null, tab: s.ltab || "ask"});
}
async function nav(n) { if (window.Settings) window.Settings.close(); if (window.Reader && window.Reader.isOpen()) { const g = new Promise(r => { addEventListener("popstate", r, {once: true}); setTimeout(r, 450); }); await window.Reader.close(); await g; } ctx.go(n); }

async function onClick(e) {
  const b = e.target.closest("[data-at]"); if (!b || (e.type === "click" && b.matches("input"))) return; const a = b.dataset.at, id = b.dataset.id;
  if (a === "cancel") { try { await api("/api/activity/cancel", {id}); const j = jobs.get(id); if (j) { j.cancelling = true; bump(j); } } catch (er) { notifyError(er); } }
  else if (a === "open") { const j = jobs.get(id); if (j) openJob(j); }
  else if (a === "usage") { e.preventDefault(); panel.classList.add("hide"); window.Settings && window.Settings.open("usage"); }
  else if (a === "index") { try { const r = await api("/api/activity/indexing", {now: true}); idx = {...idx, ...r.indexing}; if (!r.started) toast("Everything is already indexed, or an indexing pass is running."); setTimeout(() => refresh(true), 600); } catch (er) { notifyError(er); } }
  else if (a === "auto") { try { const r = await api("/api/activity/indexing", {enabled: b.checked}); idx = {...idx, ...r.indexing}; toast(b.checked ? "New papers will be read into the index in the background." : "Background indexing is off."); } catch (er) { notifyError(er); b.checked = !b.checked; } }
}

function toggle(force) {
  const open = typeof force === "boolean" ? force : panel.classList.contains("hide");
  panel.classList.toggle("hide", !open); btn.setAttribute("aria-expanded", String(open));
  if (open) { draw(); refresh(true).then(draw); usageNow(); }
}

export function init(c) {
  ctx = c; if (btn) return;
  btn = $("#aibtn"); if (!btn) return; btn.removeAttribute("disabled");
  panel = document.createElement("div"); panel.id = "aitray"; panel.className = "at card raised hide"; panel.setAttribute("role", "dialog"); panel.setAttribute("aria-label", "AI activity"); document.body.append(panel);
  btn.addEventListener("click", e => { e.stopPropagation(); toggle(); });
  panel.addEventListener("click", e => { e.stopPropagation(); onClick(e); });
  panel.addEventListener("change", e => { if (e.target.matches("[data-at=auto]")) onClick(e); });
  document.addEventListener("click", e => { if (!e.target.closest("#aitray, #aibtn")) panel.classList.add("hide"); });
  document.addEventListener("keydown", e => { if (e.key === "Escape" && !panel.classList.contains("hide")) { panel.classList.add("hide"); } });
  refresh(false).then(paintButton); usageNow();
}
export function event(j) { if (!btn) return; const was = jobs.get(j.id); bump(j); if (j.ended && (!was || !was.ended)) usageNow(); }

window.Activity = {init, event, toggle};
