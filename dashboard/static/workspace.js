// Folder tab "Workspace": one board for the whole folder. The tray on the left lists what the folder's papers hold (your highlights, the lines of each paper card, the papers themselves);
// click an item (or drag it) to put it on the board, write notes between them, put cards that belong together in a labelled frame and draw arrows. Zoom and pan as in the reader's notes.
// Every excerpt keeps its paper and page: its header opens the paper there. Exports as Markdown or Word with the cites. Saved as Papers/_folders/<id>.workspace.json.
// Server: GET/POST /api/folder/workspace, GET /api/folder/workspace/sources, GET /api/export/workspace.
import {$, $$, esc, ico, api, toast, notifyError, debounce, clamp, md, uid} from "./state.js";
import {Viewport, Links, Frames} from "./board.js";

let ctx = null;
const W = {fid: null, el: null, doc: null, rev: 0, src: null, tab: "hl", q: "", view: "board", undo: [], redo: [], kind: null, t: 0, saving: false, again: false};
const els = new Map();
let scroller, sizer, world, vp, links, frames, hintEl;

export async function mount(el, fid) {
  W.fid = fid; W.el = el; W.view = "board"; els.clear(); W.undo = []; W.redo = []; el.innerHTML = `<div class="card ev-card">Opening the workspace…</div>`;
  try { const [d, s] = await Promise.all([api("/api/folder/workspace?id=" + encodeURIComponent(fid)), api("/api/folder/workspace/sources?id=" + encodeURIComponent(fid))]); W.doc = d.doc; W.rev = d.doc.rev; W.src = s.papers; }
  catch (e) { el.innerHTML = `<div class="card empty"><h2>The workspace isn’t available</h2>${esc(e.message)}</div>`; return; }
  shell(); renderAll();
}
const rowOf = f => ctx.getLib().find(r => r.file === f);
const cite = f => { const r = rowOf(f); if (!r) return "a paper"; const a = Array.isArray(r.authors) ? r.authors : String(r.authors || "").split(/;\s*/); const nm = (a[0] || "").trim(), s = nm.includes(",") ? nm.split(",")[0].trim() : nm.split(/\s+/).pop(); return (s || (r.title || "").slice(0, 20)) + (a.length > 1 ? " et al." : "") + (r.year ? " (" + r.year + ")" : ""); };

// ---------------------------------------------------------------- the page
function shell() {
  W.el.innerHTML = `<div class="ws" data-tool="workspace"><aside class="ws-tray card"><div class="ws-th"><div class="seg" id="ws-tabs">${[["hl", "Highlights"], ["ln", "Card lines"], ["pp", "Papers"]].map(([k, t]) => `<button data-wt="${k}" class="${W.tab === k ? "on" : ""}">${t}</button>`).join("")}</div>
      <input type="search" id="ws-q" placeholder="Search…" value="${esc(W.q)}"></div><div class="ws-list" id="ws-list"></div></aside>
    <section class="ws-main card"><div class="rd-sub"><button class="rd-btn" data-w="note" title="New note (or double-click the board)">+ Note</button><button class="rd-btn" data-w="frame" title="A labelled frame around cards that belong together">▭ Frame</button>
      <button class="rd-btn" data-w="undo" disabled title="Undo (Ctrl Z)">↶</button><button class="rd-btn" data-w="redo" disabled title="Redo">↷</button>
      <span class="nc-zoom"><button class="rd-btn" data-w="zout" aria-label="Zoom out">−</button><button class="rd-btn" data-w="zfit" id="ws-zv" title="Fit everything in view">100%</button><button class="rd-btn" data-w="zin" aria-label="Zoom in">+</button></span>
      <span class="seg" id="ws-views"><button data-w="view" data-v="board" class="on">Board</button><button data-w="view" data-v="outline">Outline</button></span>
      <span class="menuwrap"><button class="rd-btn" data-w="exp">${ico("download", "sm")} Export ${ico("down", "sm")}</button><div class="menu card hide" id="ws-exp"><button data-w="copy">Copy as Markdown</button><button data-w="md">Download .md</button><button data-w="docx">Download Word</button><button data-w="save">Save Word to my export folder</button></div></span></div>
      <div class="nc-connhint hide">Click the card this arrow should point to. Esc cancels.</div>
      <div class="nc-scroll" id="ws-scroll"><div class="nc-sizer" id="ws-sizer"><div class="nc-world" id="ws-world"></div></div><div class="rd-empty nc-hint" id="ws-hint">An empty board. Click things in the tray on the left to put them here,<br>or write a note. Drag cards around, draw arrows, group them in frames.</div></div>
      <div class="nc-outline hide" id="ws-outline"></div></section></div>`;
  scroller = $("#ws-scroll", W.el); sizer = $("#ws-sizer", W.el); world = $("#ws-world", W.el); hintEl = $("#ws-hint", W.el);
  vp = new Viewport(scroller, sizer, world, {onZoom: z => { $("#ws-zv", W.el).textContent = Math.round(z * 100) + "%"; links.draw(); }});
  links = new Links({world, vp, elOf: id => els.get(id), list: () => W.doc.links, onAdd: (a, b) => { snap("link"); if (!W.doc.links.some(l => (l.a === a && l.b === b) || (l.a === b && l.b === a))) W.doc.links.push({id: uid(), a, b, label: ""}); changed(); links.draw(); },
    onLabel: (id, label) => { snap("link"); W.doc.links.find(l => l.id === id).label = label; changed(); links.draw(); }, onRemove: id => { snap("link"); W.doc.links = W.doc.links.filter(l => l.id !== id); changed(); links.draw(); }});
  frames = new Frames({world, vp, list: () => W.doc.frames, cards: () => [...els].map(([id, el]) => ({id, el})), onMove: () => links.draw(),
    onUpdate: (id, patch, quiet, moved) => { snap("frame:" + id); Object.assign(W.doc.frames.find(f => f.id === id), patch); (moved || []).forEach(m => { const c = card(m.id); if (c) { c.x = m.x; c.y = m.y; } }); changed(); if (!quiet) frames.render(); size(); },
    onRemove: id => { snap("frame"); W.doc.frames = W.doc.frames.filter(f => f.id !== id); changed(); frames.render(); size(); }});
  W.el.addEventListener("click", onClick); W.el.addEventListener("input", e => { if (e.target.id === "ws-q") { W.q = e.target.value; list(); } });
  scroller.addEventListener("dblclick", e => { if (e.target === scroller || e.target === sizer || e.target === world) { const p = vp.toWorld(e.clientX, e.clientY); addCard({type: "note"}, p.x, p.y); } });
  scroller.addEventListener("dragover", e => { if ([...e.dataTransfer.types].includes("application/x-jw-ws")) { e.preventDefault(); e.dataTransfer.dropEffect = "copy"; } });
  scroller.addEventListener("drop", e => { const raw = e.dataTransfer.getData("application/x-jw-ws"); if (!raw) return; e.preventDefault(); const p = vp.toWorld(e.clientX, e.clientY); fromItem(JSON.parse(raw), p.x - 20, p.y - 10); });
  $("#ws-list", W.el).addEventListener("dragstart", e => { const it = e.target.closest("[data-it]"); if (it) e.dataTransfer.setData("application/x-jw-ws", JSON.stringify(item(it.dataset.it))); });
  W.el.addEventListener("change", e => { if (e.target.closest("#ws-outline")) return; });
  if (!W.keys) { W.keys = true; document.addEventListener("keydown", e => { if (!W.el || !W.el.isConnected || W.el.closest(".hide") || e.target.closest("input, textarea")) return; if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "z") { e.preventDefault(); e.shiftKey ? redo() : undo(); } }); }
  list();
}

// ---------------------------------------------------------------- the tray
let items = [];
function list() {
  const box = $("#ws-list", W.el); if (!box) return; items = [];
  const ws = W.q.toLowerCase().split(/\s+/).filter(Boolean), ok = t => ws.every(w => t.toLowerCase().includes(w));
  const html = W.src.map(p => {
    let rows = [];
    if (W.tab === "hl") rows = p.highlights.filter(h => ok(h.text + " " + h.comment)).map(h => ({type: "excerpt", text: h.text, note: h.comment || "", file: p.file, page: h.page, quote: h.text.slice(0, 120), kind: "highlight"}));
    else if (W.tab === "ln") rows = p.lines.filter(l => ok(l.text + " " + l.kind)).map(l => ({type: "excerpt", text: l.text, note: "", file: p.file, page: l.page, quote: l.quote || l.text.slice(0, 120), kind: l.kind}));
    else if (ok(p.title + " " + p.cite)) rows = [{type: "paper", text: p.title, note: p.takeaway || "", file: p.file}];
    if (!rows.length) return "";
    return `<div class="ws-pg"><div class="ws-pt">${esc(p.cite)} <small>${rows.length}</small></div>${rows.map(r => { const i = items.push(r) - 1; return `<div class="ws-it" draggable="true" data-it="${i}" title="Click to add it to the board, or drag it there">${r.kind && r.kind !== "highlight" && r.type === "excerpt" ? `<span class="of-badge">${esc(r.kind)}</span> ` : ""}${esc(r.text.length > 170 ? r.text.slice(0, 170) + "…" : r.text)}${r.page ? ` <small>p. ${r.page}</small>` : ""}</div>`; }).join("")}</div>`;
  }).join("");
  box.innerHTML = html || `<div class="pmeta" style="padding:10px">${W.q ? "Nothing matches." : W.tab === "hl" ? "No highlights in this folder’s papers yet. Highlight in the reader, or use Card lines." : W.tab === "ln" ? "No paper card yet: make cards in the Evidence tab." : "No papers."}</div>`;
}
const item = i => items[+i];
function fromItem(it, x, y) {
  const dup = W.doc.cards.find(c => c.file === it.file && c.type === it.type && c.text === it.text && (c.page || 0) === (it.page || 0));
  if (dup) { const e = els.get(dup.id); if (e) { e.scrollIntoView({block: "center", inline: "center"}); e.classList.add("new"); setTimeout(() => e.classList.remove("new"), 1400); } return toast("That is already on the board."); }
  addCard({...it}, x, y);
}

// ---------------------------------------------------------------- state, saving, undo
const card = id => W.doc.cards.find(c => c.id === id);
const copy = () => JSON.stringify({cards: W.doc.cards, frames: W.doc.frames, links: W.doc.links});
function snap(kind = "edit") {
  const now = Date.now();
  if (/^(text|move|frame):/.test(kind) && kind === W.kind && now - W.t < 1500 && W.undo.length) { W.t = now; return; }
  W.undo.push(copy()); if (W.undo.length > 100) W.undo.shift(); W.redo = []; W.kind = kind; W.t = now; tools();
}
function restore(json) { Object.assign(W.doc, JSON.parse(json)); W.kind = null; renderAll(); changed(); tools(); }
function undo() { if (!W.undo.length) return; W.redo.push(copy()); restore(W.undo.pop()); }
function redo() { if (!W.redo.length) return; W.undo.push(copy()); restore(W.redo.pop()); }
function tools() { const u = $('[data-w="undo"]', W.el), r = $('[data-w="redo"]', W.el); if (u) { u.disabled = !W.undo.length; r.disabled = !W.redo.length; } }
const flush = debounce(() => post(), 700);
function changed() { W.dirty = true; flush(); }
async function post() {
  if (W.saving) { W.again = true; return; } W.saving = true; W.dirty = false;
  W.doc.cards.forEach(c => { const e = els.get(c.id); if (e) c.h = e.offsetHeight; });
  try { const r = await api("/api/folder/workspace", {id: W.fid, doc: W.doc, rev: W.rev}); W.rev = r.doc.rev; W.doc.rev = r.doc.rev; }
  catch (e) { if (e.status === 409) { toast("The workspace changed in another window — showing that copy."); W.doc = e.body.current; W.rev = W.doc.rev; renderAll(); } else notifyError(e); }
  W.saving = false; if (W.again) { W.again = false; post(); }
}
addEventListener("pagehide", () => { if (W.dirty && W.doc && W.fid) fetch("/api/folder/workspace", {method: "POST", keepalive: true, headers: {"Content-Type": "application/json"}, body: JSON.stringify({id: W.fid, doc: W.doc, rev: W.rev})}); });

// ---------------------------------------------------------------- the board
function renderAll() {
  els.forEach(e => e.remove()); els.clear(); W.doc.cards.forEach(addEl); frames.render(); size(); hint(); tools();
  if (W.view === "outline") outline();
}
function hint() { hintEl.style.display = W.doc.cards.length || W.doc.frames.length ? "none" : ""; }
function size() {
  let w = 700, h = 500; els.forEach(e => { w = Math.max(w, e.offsetLeft + e.offsetWidth + 80); h = Math.max(h, e.offsetTop + e.offsetHeight + 120); }); W.doc.frames.forEach(f => { w = Math.max(w, f.x + f.w + 80); h = Math.max(h, f.y + f.h + 120); });
  world.style.width = w + "px"; world.style.height = h + "px"; vp.resize(w, h); links.draw();
}
function nextPos() { let y = 20; els.forEach(e => { y = Math.max(y, e.offsetTop + e.offsetHeight + 14); }); return {x: 20, y}; }
function addCard(c, x, y) {
  const p = nextPos(); snap("add");
  const card = {id: uid(), type: "note", x: Math.max(0, Math.round(x ?? p.x)), y: Math.max(0, Math.round(y ?? p.y)), w: 280, h: 0, text: "", note: "", created: new Date().toISOString(), ...c};
  W.doc.cards.push(card); addEl(card); hint(); size(); changed();
  const e = els.get(card.id); e.classList.add("new"); setTimeout(() => e.classList.remove("new"), 1400); e.scrollIntoView({block: "nearest", inline: "nearest"});
  if (card.type === "note") { const t = $("textarea.nc-main", e); t.focus(); }
}
function cardHtml(c) {
  const head = c.type === "excerpt" ? `<button class="wsl" data-w="open" title="Open the paper at this page">✂ ${esc(cite(c.file))}${c.page ? ", p. " + c.page : ""}</button>${c.kind && c.kind !== "highlight" ? ` <span class="of-badge">${esc(c.kind)}</span>` : ""}` : c.type === "paper" ? `<button class="wsl" data-w="open" title="Open the paper">📄 ${esc(cite(c.file))}</button>` : "✎ Note";
  const body = c.type === "note" ? `<div class="nc-md md${c.text ? "" : " hide"}" title="Click to edit (Markdown works)">${md(c.text || "")}</div><textarea class="nc-main${c.text ? " hide" : ""}" rows="3" placeholder="Write a note… (Markdown works)">${esc(c.text)}</textarea>`
    : `<div class="nc-text">${esc(c.text)}</div><textarea class="nc-note" rows="1" placeholder="Add a note…">${esc(c.note || "")}</textarea>`;
  return `<div class="nc-head" data-drag><span>${head}</span><span class="nc-act"><button data-w="conn" title="Draw an arrow from this card to another one">⟶</button><button data-w="del" title="Remove from the board">✕</button></span></div>${body}<div class="nc-grip" title="Drag to resize"></div>`;
}
function addEl(c) {
  const el = document.createElement("div"); el.className = "nc-card ws-card " + (c.type === "note" ? "note" : "excerpt"); el.dataset.id = c.id; el.dataset.card = c.id; el.style.cssText = `left:${c.x}px;top:${c.y}px;width:${c.w}px`;
  el.style.setProperty("--hc", c.type === "note" ? "var(--accent)" : c.type === "paper" ? "var(--t-library)" : "var(--ink-3)");
  el.innerHTML = cardHtml(c); els.set(c.id, el); world.append(el); wire(el, c.id); autosize(el);
}
const autosize = el => $$("textarea", el).forEach(t => { t.style.height = "auto"; t.style.height = t.scrollHeight + 2 + "px"; });
function wire(el, id) {
  const save = debounce((patch) => { snap("text:" + id); Object.assign(card(id), patch); changed(); }, 400);
  $$("textarea", el).forEach(t => t.addEventListener("input", () => { autosize(el); size(); save(t.classList.contains("nc-note") ? {note: t.value} : {text: t.value}); }));
  const mdBox = $(".nc-md", el), main = $("textarea.nc-main", el);
  if (mdBox) {
    mdBox.addEventListener("click", e => { if (e.target.closest("a")) return; mdBox.classList.add("hide"); main.classList.remove("hide"); autosize(el); main.focus(); size(); });
    main.addEventListener("blur", () => { if (!main.value.trim()) return; mdBox.innerHTML = md(main.value); mdBox.classList.remove("hide"); main.classList.add("hide"); size(); });
  }
  $(".nc-head", el).addEventListener("pointerdown", e => {
    if (e.target.closest("button") || e.button !== 0) return;
    const sx = e.clientX, sy = e.clientY, ox = el.offsetLeft, oy = el.offsetTop, z = vp.z; el.setPointerCapture(e.pointerId); el.classList.add("drag");
    const move = ev => { el.style.left = Math.max(0, ox + (ev.clientX - sx) / z) + "px"; el.style.top = Math.max(0, oy + (ev.clientY - sy) / z) + "px"; links.draw(); };
    const up = () => { el.releasePointerCapture(e.pointerId); el.classList.remove("drag"); el.removeEventListener("pointermove", move); el.removeEventListener("pointerup", up);
      if (el.offsetLeft !== ox || el.offsetTop !== oy) { snap("move"); const c = card(id); c.x = el.offsetLeft; c.y = el.offsetTop; changed(); } size(); };
    el.addEventListener("pointermove", move); el.addEventListener("pointerup", up);
  });
  $(".nc-grip", el).addEventListener("pointerdown", e => {
    e.preventDefault(); e.stopPropagation(); const sx = e.clientX, w0 = el.offsetWidth, g = e.currentTarget; g.setPointerCapture(e.pointerId);
    const move = ev => { el.style.width = clamp(w0 + (ev.clientX - sx) / vp.z, 180, 720) + "px"; autosize(el); links.draw(); };
    const up = () => { g.removeEventListener("pointermove", move); g.removeEventListener("pointerup", up); snap("resize"); card(id).w = el.offsetWidth; changed(); size(); };
    g.addEventListener("pointermove", move); g.addEventListener("pointerup", up);
  });
}

// ---------------------------------------------------------------- the outline (the board read as a document)
function groups() {
  const cards = W.doc.cards, used = new Set(), out = [];
  W.doc.frames.slice().sort((a, b) => (a.y - b.y) || (a.x - b.x)).forEach(f => {
    const cs = cards.filter(c => { const e = els.get(c.id), h = e ? e.offsetHeight : 100, cx = c.x + c.w / 2, cy = c.y + h / 2; return !used.has(c.id) && cx >= f.x && cx <= f.x + f.w && cy >= f.y && cy <= f.y + f.h; }).sort((a, b) => (a.y - b.y) || (a.x - b.x));
    cs.forEach(c => used.add(c.id)); out.push({title: f.label || "Untitled group", frame: true, color: f.color, cards: cs});
  });
  const rest = cards.filter(c => !used.has(c.id)), mine = rest.filter(c => !c.file); if (mine.length) out.push({title: "My notes", cards: mine.sort((a, b) => (a.y - b.y) || (a.x - b.x))});
  [...new Set(rest.filter(c => c.file).map(c => c.file))].forEach(f => out.push({title: cite(f), cards: rest.filter(c => c.file === f).sort((a, b) => (a.page || 0) - (b.page || 0) || a.y - b.y)}));
  return out;
}
const snip = c => { const t = (c.text || c.note || "").replace(/[*_#>`]/g, "").replace(/\s+/g, " ").trim(); return t.length > 50 ? t.slice(0, 50) + "…" : t || "a card"; };
function outline() {
  const box = $("#ws-outline", W.el), gs = groups(), L = W.doc.links, byid = Object.fromEntries(W.doc.cards.map(c => [c.id, c]));
  box.innerHTML = `<div class="ol-bar"><span class="pmeta">${W.doc.cards.length} card${W.doc.cards.length === 1 ? "" : "s"}${W.doc.frames.length ? " · " + W.doc.frames.length + " group" + (W.doc.frames.length === 1 ? "" : "s") : ""}${L.length ? " · " + L.length + " arrow" + (L.length === 1 ? "" : "s") : ""}</span></div>` +
    (gs.map(g => `<section class="ol-g ${g.frame ? "frame" : ""}"${g.color ? ` style="--fc:${esc(g.color)}"` : ""}><h4>${esc(g.title)} <small>${g.cards.length}</small></h4>${g.cards.map(c => {
      const to = L.filter(l => l.a === c.id && byid[l.b]).map(l => `<span class="ol-lk">→ ${l.label ? "<i>" + esc(l.label) + "</i> " : ""}${esc(snip(byid[l.b]))}</span>`).join("");
      return `<div class="ol-item ${c.type}" data-id="${c.id}"><div class="ol-h"><span>${c.type === "excerpt" ? "✂ Excerpt" + (c.page ? " · p. " + c.page : "") : c.type === "paper" ? "📄 Paper" : "✎ Note"}</span><span class="ol-act">${c.file ? `<button data-w="open" title="Open the paper">⤴</button>` : ""}</span></div>
        ${c.type === "note" ? `<div class="ol-b md">${md(c.text || "")}</div>` : `<blockquote class="ol-q">${esc(c.text)}</blockquote>${c.note ? `<div class="ol-b">${esc(c.note)}</div>` : ""}`}${to ? `<div class="ol-lks">${to}</div>` : ""}</div>`; }).join("")}</section>`).join("") || `<div class="rd-empty">The board is empty.</div>`);
}

// ---------------------------------------------------------------- actions
async function exportFile(fmt) { const l = document.createElement("a"); l.href = `/api/export/workspace?folder=${encodeURIComponent(W.fid)}&fmt=${fmt}`; l.download = ""; document.body.append(l); l.click(); l.remove(); }
async function onClick(e) {
  const t = e.target.closest("[data-w],[data-wt]"); if (!t) { if (!e.target.closest(".menuwrap")) $("#ws-exp", W.el)?.classList.add("hide"); const it = e.target.closest("[data-it]"); if (it) fromItem(item(it.dataset.it)); return; }
  if (t.dataset.wt) { W.tab = t.dataset.wt; $$("[data-wt]", W.el).forEach(b => b.classList.toggle("on", b === t)); return list(); }
  const a = t.dataset.w, cardEl = t.closest(".nc-card"), id = cardEl && cardEl.dataset.id, ol = t.closest(".ol-item"), oid = ol && ol.dataset.id;
  try {
    if (a === "note") return addCard({type: "note"});
    if (a === "frame") { snap("frame"); const p = nextPos(), r = scroller.getBoundingClientRect(); W.doc.frames.push({id: uid(), x: 20, y: p.y + 6, w: clamp(Math.round(r.width / vp.z - 60), 220, 460), h: 260, label: "", color: "#5b7fa6"}); frames.render(); size(); hint(); changed(); requestAnimationFrame(() => scroller.scrollTo({top: Math.max(0, (p.y - 30) * vp.z)})); return; }
    if (a === "undo") return undo(); if (a === "redo") return redo();
    if (a === "zin") return vp.zoomTo(vp.z * 1.25); if (a === "zout") return vp.zoomTo(vp.z / 1.25); if (a === "zfit") return vp.fit();
    if (a === "view") { W.view = t.dataset.v; $$("#ws-views [data-v]", W.el).forEach(b => b.classList.toggle("on", b.dataset.v === W.view)); scroller.classList.toggle("hide", W.view !== "board"); $("#ws-outline", W.el).classList.toggle("hide", W.view !== "outline"); $$('[data-w="note"],[data-w="frame"],.nc-zoom', W.el).forEach(b => b.classList.toggle("hide", W.view !== "board")); if (W.view === "outline") outline(); else { els.forEach(autosize); size(); } return; }
    if (a === "del" && id) { snap("del"); W.doc.cards = W.doc.cards.filter(c => c.id !== id); W.doc.links = W.doc.links.filter(l => l.a !== id && l.b !== id); els.get(id).remove(); els.delete(id); hint(); size(); changed(); return; }
    if (a === "conn" && id) return links.start(id, on => $(".nc-connhint", W.el).classList.toggle("hide", !on));
    if (a === "open") { const c = card(id || oid); const r = c && c.file && rowOf(c.file); if (!r) return toast("That paper is no longer in your library."); return ctx.readPaper(r, c.page ? {goto: {page: c.page, quote: c.quote || ""}} : {tab: "card"}); }
    if (a === "exp") { $("#ws-exp", W.el).classList.toggle("hide"); return; }
    $("#ws-exp", W.el).classList.add("hide");
    if (a === "copy") { if (W.dirty) await post(); const r = await fetch(`/api/export/workspace?folder=${encodeURIComponent(W.fid)}&fmt=md`); if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || "Nothing to export yet."); await navigator.clipboard.writeText(await r.text()); return toast("Copied as Markdown."); }
    if (a === "md" || a === "docx") { if (W.dirty) await post(); return exportFile(a); }
    if (a === "save") { if (W.dirty) await post(); const r = await api("/api/export/save", {what: "workspace", folder: W.fid, fmt: "docx"}); return toast("Saved: " + r.path); }
  } catch (er) { notifyError(er); }
}

window.WorkspaceUI = {init(c) { ctx = c; }, mount};
