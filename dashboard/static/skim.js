// Skim layer: the sentences of the open paper that matter, found by lens (research question, hypothesis, theory, sample & data, finding, limitation,
// contribution) and drawn on the page as ghost highlights. A density slider chooses how many (key points · more · everything), a click on a ghost
// highlights it (or dismisses it), "Highlight all shown" accepts what you see. Accepted ones become real highlights, coloured by lens and tagged
// "lens: finding" etc., so they show up in the Highlights tab, the Library's highlights hub and exports like any other.
// Server: GET /api/doc/skim (saved proposals, lenses, cost), POST /api/ai/skim (stream: status | progress | skim | done | error), POST /api/doc/skim (dismissed ids).
import {notifyError, S, $, $$, esc, toast, on, api, colorOf, clamp, debounce, aboutCost, exactCost} from "./state.js";
import * as N from "./notes.js";
import * as T from "./text.js";
import {frame, wire as wireOut} from "./output.js";

const DENS = ["Key points", "More", "Everything"];
const money = aboutCost;
const K = {on: null, loaded: null, data: null, lenses: [], est: null, dens: 2, off: new Set(), dismissed: new Set(), cur: -1, busy: false, status: ""};
let bar, pop, root;
const ls = {get: (k, d) => { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch (e) { return d; } }, set: (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* private window */ } }};

export function init() {
  root = S.ui.root;
  bar = document.createElement("div"); bar.className = "rd-skim hide"; bar.id = "rd-skim";
  const find = $("#rd-find", root); find.insertAdjacentElement("afterend", bar);
  pop = document.createElement("div"); pop.className = "rd-float col hide rd-skimpop"; root.append(pop);
  K.dens = clamp(+ls.get("jw-skim-d", 2) || 2, 1, 3);
  try { K.off = new Set(JSON.parse(ls.get("jw-skim-off", "[]"))); } catch (e) { K.off = new Set(); }
  bar.addEventListener("click", onBar); bar.addEventListener("input", onBar); pop.addEventListener("mousedown", e => e.preventDefault()); pop.addEventListener("click", onPop);
  S.ui.pane.addEventListener("click", onPage, true);
  document.addEventListener("mousedown", e => { if (!e.target.closest(".rd-skimpop")) pop.classList.add("hide"); });
  S.fn.addOverlay(paint); S.fn.toggleSkim = toggle; S.fn.skimState = () => K;
  on("paper:opened", () => { K.data = null; K.loaded = null; K.cur = -1; K.est = null; K.dismissed = new Set(); K.on = false; bar.classList.add("hide"); pop.classList.add("hide"); markBtn();
    if (ls.get("jw-skim-on:" + S.file, "") === "1" && S.meta && S.meta.skim) toggle(true); });
  on("reader:closed", () => { bar.classList.add("hide"); pop.classList.add("hide"); K.on = false; });
  on("notes:changed", () => { if (K.on && K.data && !K.busy) dyn(); });
  markBtn();
}
const markBtn = () => { const b = $("#rd-skimbtn", root); if (b) b.classList.toggle("on", !!K.on); };

async function toggle(force) {
  K.on = typeof force === "boolean" ? force : !K.on; markBtn(); pop.classList.add("hide");
  ls.set("jw-skim-on:" + S.file, K.on ? "1" : "0");
  bar.classList.toggle("hide", !K.on);
  if (!K.on) return S.fn.repaint();
  if (K.loaded !== S.file) await load();
  draw(); S.fn.repaint();
}
async function load() {
  const f = S.file; K.loaded = f;
  try {
    const r = await api("/api/doc/skim?file=" + encodeURIComponent(f)); if (S.file !== f) return;
    K.lenses = r.lenses; K.est = r.estimate || null; K.data = r.skim; K.dismissed = new Set((r.skim || {}).dismissed || []);
    if (K.data) await place();
  } catch (e) { K.loaded = null; toast(e.status === 501 ? "AI features aren’t set up, so the skim layer can’t be made." : e.message); }
}
// where each proposed sentence sits on its page (rects as fractions of the page), found from the same text the search uses
async function place() {
  const items = K.data.items, f = S.file;
  for (const it of items) {
    if (S.file !== f) return;
    try { const tm = await T.model(S.pages[it.page - 1]), loc = T.locate(tm, it.quote);
      if (loc) { it.rects = T.rects(tm, loc.a, loc.b); it.text = tm.str.slice(loc.a, loc.b); it.ctx = T.context(tm, loc.a, loc.b); } else it.rects = null; }
    catch (e) { it.rects = null; }
  }
}

// ---------------------------------------------------------------- what is shown
const accepted = () => new Set(S.notes.highlights.filter(h => h.skim).map(h => h.skim));
const lensOf = id => K.lenses.find(l => l.id === id) || {color: "yellow", name: id};
function visible() {
  if (!K.data) return [];
  const acc = accepted();
  return K.data.items.filter(i => i.rects && i.rank <= K.dens && !K.off.has(i.lens) && !K.dismissed.has(i.id) && !acc.has(i.id));
}
function paint(n, b, f, box) {
  if (!K.on || !K.data) return;
  for (const it of visible()) {
    if (it.page !== n) continue;
    for (const r of it.rects) {
      if (r[1] + r[3] <= b.y0 || r[1] >= b.y1) continue;
      const d = document.createElement("div"); d.className = "skim-g r" + it.rank; d.style.cssText = box(r) + `;--c:${colorOf(lensOf(it.lens).color)}`; d.dataset.sk = it.id; f.append(d);
    }
  }
}

// ---------------------------------------------------------------- the bar
function draw() {
  if (!K.on) return;
  if (K.busy) { bar.innerHTML = `<span class="sk-t">⚡ Skim</span><span class="ask-status">${esc(K.status || "Reading the paper…")}</span>`; return; }
  if (!K.data) {
    bar.innerHTML = `<span class="sk-t">⚡ Skim</span><span class="sk-intro">Marks the sentences that matter for each lens — question, hypothesis, theory, sample, finding, limitation, contribution — so you can skim by reading only those.</span>
      <button class="btn primary small" data-sk="make">Skim this paper${K.est && K.est.cost != null ? " · " + money(K.est.cost) : ""}</button><button class="rd-btn" data-sk="close" title="Close">✕</button>`;
    return;
  }
  bar.innerHTML = `<span class="sk-t">⚡ Skim</span><span class="sk-lenses"></span>
    <label class="sk-dens" title="How many sentences to mark"><input type="range" id="sk-d" min="1" max="3" step="1" value="${K.dens}"><span>${DENS[K.dens - 1]}</span></label>
    <span class="sk-nav"><button class="rd-btn" data-sk="prev" title="Previous marked sentence">◀</button><button class="rd-btn" data-sk="next" title="Next marked sentence">▶</button></span>
    <button class="rd-btn" data-sk="list" title="List the marked sentences by lens, with Highlight and Dismiss on each">☰ List</button><button class="rd-btn" data-sk="all" title="Turn every sentence shown into a real highlight (one undo step)">✓ Highlight all shown</button><span class="sk-sum"></span>
    <button class="rd-btn" data-sk="redo" title="Skim again with the model (${exactCost((K.data.cost || 0))} last time); what you dismissed stays dismissed">⟳</button><button class="rd-btn" data-sk="close" title="Hide the skim layer">✕</button>`;
  dyn();
}
// the parts that change with the slider, the lens chips, accepting and dismissing (the slider itself is never rebuilt, so dragging it is smooth)
function dyn() {
  if (!K.on || !K.data || K.busy || !$(".sk-lenses", bar)) return;
  const vis = visible(), acc = accepted(), items = K.data.items, unplaced = items.filter(i => !i.rects).length;
  const count = id => vis.filter(i => i.lens === id).length;
  $(".sk-lenses", bar).innerHTML = K.lenses.map(l => `<button class="sk-chip${K.off.has(l.id) ? "" : " on"}" data-lens="${l.id}" style="--c:${colorOf(l.color)}" title="${esc(l.hint)}"><i></i>${esc(l.name)} <b>${count(l.id)}</b></button>`).join("");
  $(".sk-sum", bar).textContent = `${vis.length} shown${acc.size ? ` · ${items.filter(i => acc.has(i.id)).length} highlighted` : ""}${K.dismissed.size ? ` · ${K.dismissed.size} dismissed` : ""}${unplaced ? ` · ${unplaced} not found on the page` : ""}`;
  $('[data-sk="all"]', bar).disabled = !vis.length;
}
function onBar(e) {
  const t = e.target;
  if (t.id === "sk-d") { if (e.type !== "input") return; K.dens = +t.value; ls.set("jw-skim-d", K.dens); $(".sk-dens span", bar).textContent = DENS[K.dens - 1]; dyn(); S.fn.repaint(); return; }
  if (e.type !== "click") return;
  const lens = t.closest("[data-lens]"), a = t.closest("[data-sk]")?.dataset.sk;
  if (lens) { const id = lens.dataset.lens; K.off.has(id) ? K.off.delete(id) : K.off.add(id); ls.set("jw-skim-off", JSON.stringify([...K.off])); dyn(); S.fn.repaint(); return; }
  if (a === "close") toggle(false);
  else if (a === "make") make(false);
  else if (a === "redo") { if (confirm("Skim this paper again? It uses the model once more. Highlights you already made are kept.")) make(true); }
  else if (a === "all") acceptAll();
  else if (a === "list") S.fn.showTab("skim");
  else if (a === "next" || a === "prev") step(a === "next" ? 1 : -1);
}
function step(d) {
  const vis = visible(); if (!vis.length) return toast("Nothing is marked at this density. Move the slider or switch on more lenses.");
  const pos = S.fn.currentPos(), after = vis.findIndex(i => i.page > pos.n || (i.page === pos.n && i.rects[0][1] > pos.y + 0.02));
  let k = K.cur >= 0 && vis[K.cur] && K.last === vis[K.cur].id ? K.cur + d : (d > 0 ? (after < 0 ? 0 : after) : (after < 0 ? vis.length - 1 : after - 1));
  k = (k + vis.length) % vis.length; K.cur = k; K.last = vis[k].id;
  S.fn.jump(vis[k].page, Math.max(0, vis[k].rects[0][1] - 0.06), {flash: vis[k].rects});
}

// ---------------------------------------------------------------- making the skim
async function make(fresh) {
  if (K.busy) return; K.busy = true; K.status = "Reading the paper…"; draw();
  try {
    const r = await fetch("/api/ai/skim", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({file: S.file, fresh})});
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
    const rd = r.body.getReader(), dec = new TextDecoder(); let buf = "", done = null;
    for (;;) {
      const {value, done: fin} = await rd.read(); if (fin) break; buf += dec.decode(value, {stream: true}); let i;
      while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1); if (!line) continue; const m = JSON.parse(line);
        if (m.type === "status") K.status = m.msg; else if (m.type === "progress") K.status = `Choosing sentences… ${(m.chars / 1000).toFixed(1)}k characters`;
        else if (m.type === "skim") { K.data = m.skim; K.dismissed = new Set(m.skim.dismissed || []); } else if (m.type === "done") done = m; else if (m.type === "error") throw new Error(m.error);
        if (m.type === "status" || m.type === "progress") draw();
      }
    }
    if (S.meta) S.meta.skim = true;
    K.status = "Placing the sentences on the page…"; draw(); await place();
    if (done && !done.existing) toast("Skimmed" + (done.cost != null ? " · " + exactCost(done.cost) : "") + ".");
  } catch (e) { notifyError(e); }
  K.busy = false; draw(); S.fn.repaint();
}

// ---------------------------------------------------------------- accepting and dismissing
async function ensureTags(items) {
  const out = {};
  for (const id of new Set(items.map(i => i.lens))) out[id] = await N.ensureTag("lens: " + lensOf(id).name);
  return out;
}
async function accept(items) {
  items = items.filter(i => i.rects); if (!items.length) return 0;
  const tags = await ensureTags(items);
  N.batch("skim", () => {
    for (const it of items) {
      const h = N.addHighlight({parts: [{page: it.page, rects: it.rects.map(r => r.map(v => +v.toFixed(4)))}], text: it.text, quote: {exact: it.text, ...it.ctx}}, lensOf(it.lens).color);
      h.skim = it.id;
      if (tags[it.lens]) N.toggleTag("hl", h.id, tags[it.lens].id);
    }
  });
  return items.length;
}
async function acceptAll() {
  const vis = visible(); if (!vis.length) return;
  if (vis.length > 25 && !confirm(`Highlight all ${vis.length} sentences shown?`)) return;
  const n = await accept(vis); toast(`Highlighted ${n} sentence${n === 1 ? "" : "s"}. Undo (Ctrl Z) takes them all back.`);
}
const saveDismissed = debounce(() => fetch("/api/doc/skim", {method: "POST", headers: {"Content-Type": "application/json"}, keepalive: true, body: JSON.stringify({file: S.file, dismissed: [...K.dismissed]})}).catch(() => {}), 500);
function dismiss(it) { K.dismissed.add(it.id); saveDismissed(); dyn(); S.fn.repaint(); }

// ---------------------------------------------------------------- clicking a ghost on the page
function onPage(e) {
  if (!K.on || !K.data || S.tool || !getSelection().isCollapsed) return;
  const b = e.target.closest(".rd-band")?._b; if (!b) return;
  const bb = b.el.getBoundingClientRect(), x = (e.clientX - bb.left) / bb.width, y = b.y0 + (e.clientY - bb.top) / bb.height * (b.y1 - b.y0);
  const hit = visible().find(i => i.page === b.p.n && i.rects.some(r => x >= r[0] - 0.004 && x <= r[0] + r[2] + 0.004 && y >= r[1] - 0.004 && y <= r[1] + r[3] + 0.004));
  if (!hit) return;
  e.stopPropagation(); e.preventDefault();
  const l = lensOf(hit.lens);
  pop.innerHTML = `<div class="sk-ph"><span class="sk-chip on" style="--c:${colorOf(l.color)}"><i></i>${esc(l.name)}</span><small>${["", "key point", "supporting", "detail"][hit.rank]}</small></div>
    <div class="sk-pq">${esc(hit.text.length > 220 ? hit.text.slice(0, 220) + "…" : hit.text)}</div>
    <div class="row" style="gap:6px"><button class="rd-btn" data-p="yes" style="background:var(--accent-soft)">✓ Highlight</button><button class="rd-btn" data-p="no">✕ Not useful</button><button class="rd-btn" data-p="ask">✨ Ask</button></div>`;
  pop._it = hit; pop.classList.remove("hide");
  pop.style.left = clamp(e.clientX - 20, 8, innerWidth - 320) + "px"; pop.style.top = clamp(e.clientY + 14, 70, innerHeight - pop.offsetHeight - 10) + "px";
}
async function onPop(e) {
  const a = e.target.closest("[data-p]")?.dataset.p, it = pop._it; if (!a || !it) return;
  pop.classList.add("hide");
  if (a === "yes") await accept([it]); else if (a === "no") dismiss(it);
  else if (a === "ask" && S.fn.explainMenu) S.fn.explainMenu(e.target.closest("[data-p]"), {text: it.text, page: it.page});
}

// ---------------------------------------------------------------- the Skim tab: the same sentences as a list, grouped by lens
let lst = null;
export function mountList(el) {
  lst = el; el.innerHTML = `<div class="rd-scroll" id="skl-body"></div>`; wireOut($("#skl-body", el));
  el.addEventListener("click", onList);
  on("tab:show", k => { if (k === "skim") drawList(); });
  on("paper:opened", () => { const b = $("#skl-body", el); if (b) b.innerHTML = ""; });
  on("notes:changed", () => { if (S.tab === "skim") drawList(); });
}
async function drawList() {
  const body = $("#skl-body", lst); if (!body) return;
  if (!K.data && S.meta && S.meta.skim && K.loaded !== S.file) { body.innerHTML = `<div class="rd-empty"><span class="ask-status">Loading the skim…</span></div>`; await load(); }
  if (!K.data) {
    body.innerHTML = `<div class="rd-empty cd-start" data-tool="skim"><span class="cd-bigico">${window.ico("skim")}</span><b>Skim this paper</b><br>Marks the sentences that matter for each lens — question, hypothesis, theory, sample, finding, limitation, contribution — and lists them here.<br><br>
      <button class="btn primary" data-skl="make">Skim this paper${K.est && K.est.cost != null ? " · " + money(K.est.cost) : ""}</button></div>`; return;
  }
  const acc = accepted(), items = K.data.items.filter(i => i.rank <= K.dens && !K.off.has(i.lens)), shown = items.filter(i => !K.dismissed.has(i.id));
  const sections = K.lenses.map(l => {
    const xs = shown.filter(i => i.lens === l.id).sort((a, b) => a.page - b.page || (a.rects ? a.rects[0][1] : 0) - (b.rects ? b.rects[0][1] : 0)); if (!xs.length) return null;
    return {id: l.id, title: l.name, icon: "skim", count: xs.length, note: `<i class="sk-dot" style="--c:${colorOf(l.color)}"></i>`, body: xs.map(i => `<div class="skl-it${acc.has(i.id) ? " done" : ""}" data-id="${esc(i.id)}">
      <button class="cd-cite" data-skl="jump" title="Show it on the page">p. ${i.page}</button><span class="skl-q" data-skl="jump">${esc(i.text || i.quote)}</span>
      <span class="skl-b">${acc.has(i.id) ? `<span class="of-badge">✓ highlighted</span>` : i.rects ? `<button class="rd-btn" data-skl="yes" title="Highlight it">✓</button><button class="rd-btn" data-skl="no" title="Not useful: hide it">✕</button>` : `<span class="of-badge inferred" title="The sentence could not be found on the page">not found</span>`}</span></div>`).join("")};
  }).filter(Boolean);
  const nAcc = items.filter(i => acc.has(i.id)).length;
  body.innerHTML = frame({tool: "skim", kind: "Skim list", icon: "skim", key: "skim:" + S.file, title: `${shown.length} sentence${shown.length === 1 ? "" : "s"} worth reading`, sub: `Density: ${DENS[K.dens - 1]} · set it, and the lenses, with the Skim bar`,
    meta: [{t: nAcc + " highlighted"}, K.dismissed.size ? {t: K.dismissed.size + " dismissed"} : null, K.data.model ? {t: K.data.model} : null],
    actions: `<button class="rd-btn" data-skl="all" title="Turn every sentence listed into a real highlight (one undo step)"${shown.some(i => i.rects && !acc.has(i.id)) ? "" : " disabled"}>✓ Highlight all</button><button class="rd-btn" data-skl="bar" title="Show the skim layer on the page">On the page</button>`, sections});
}
async function onList(e) {
  const a = e.target.closest("[data-skl]")?.dataset.skl; if (!a) return;
  if (a === "make") { await toggle(true); if (!K.data) await make(false); return drawList(); }
  if (a === "bar") return toggle(true);
  if (a === "all") { const vis = K.data.items.filter(i => i.rects && i.rank <= K.dens && !K.off.has(i.lens) && !K.dismissed.has(i.id) && !accepted().has(i.id)); if (!vis.length) return; await accept(vis); toast(`Highlighted ${vis.length}. Undo (Ctrl Z) takes them all back.`); return drawList(); }
  const row = e.target.closest(".skl-it"), it = row && K.data.items.find(i => i.id === row.dataset.id); if (!it) return;
  if (a === "jump") { if (it.rects) S.fn.jump(it.page, Math.max(0, it.rects[0][1] - 0.06), {flash: it.rects}); else S.fn.jump(it.page, 0); }
  else if (a === "yes") { await accept([it]); drawList(); }
  else if (a === "no") { dismiss(it); drawList(); }
}
