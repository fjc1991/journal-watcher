// Notes canvas: free-floating cards beside the paper. Excerpts keep a ⤴ link back to the exact spot they came from
// (it jumps there and flashes it); notes are plain text; AI answers keep clickable citations.
import {S, $, $$, esc, clamp, debounce, toast, colorOf, on, md} from "./state.js";
import * as N from "./notes.js";
import * as T from "./text.js";
import {answerHtml, wireCites} from "./cite.js";
import {Viewport, Links, Frames} from "./board.js";

let panel, scroller, sizer, world, hint, undoBtn, redoBtn, outlineBox, vp, links, frames, view = "canvas";
const els = new Map();     // card id -> element

export function mount(el) {
  panel = el;
  el.innerHTML = `<div class="rd-sub"><button class="rd-btn" data-k="add" title="New text note (or double-click the canvas)">+ Note</button><button class="rd-btn" data-k="frame" title="Draw a labelled frame around cards that belong together">▭ Frame</button>
    <button class="rd-btn" data-k="tidy" title="Arrange the cards in two columns, in reading order">Tidy</button>
    <button class="rd-btn" data-k="undo" title="Undo (Ctrl Z)" disabled>↶</button><button class="rd-btn" data-k="redo" title="Redo (Ctrl Shift Z)" disabled>↷</button>
    <span class="nc-zoom" title="Zoom (Ctrl + wheel or pinch)"><button class="rd-btn" data-k="zout" aria-label="Zoom out">−</button><button class="rd-btn" data-k="zfit" id="nc-zv" title="Fit everything in view">100%</button><button class="rd-btn" data-k="zin" aria-label="Zoom in">+</button></span>
    <span class="seg" id="nc-views"><button data-k="view" data-v="canvas" class="on">Canvas</button><button data-k="view" data-v="outline">Outline</button></span>
    <button class="rd-btn" data-k="export" title="Download your highlights and notes as Markdown">⬇ Export .md</button></div>
    <div class="nc-pn"><textarea id="nc-pn" rows="1" maxlength="20000" placeholder="A note on the paper as a whole: why it matters, what you will take from it…"></textarea></div>
    <div class="nc-connhint hide">Click the card this arrow should point to. Esc cancels.</div><div class="nc-scroll" id="nc-scroll"><div class="nc-sizer" id="nc-sizer"><div class="nc-world" id="nc-world"></div></div>
    <div class="rd-empty nc-hint" id="nc-hint">Select text and press <b>→ Notes</b>, drag a selection over here, or box a figure with <b>▭ Area</b>.<br>Every excerpt keeps a ⤴ link back to where it came from.</div></div>
    <div class="nc-outline hide" id="nc-outline"></div>`;
  scroller = $("#nc-scroll", el); sizer = $("#nc-sizer", el); world = $("#nc-world", el); hint = $("#nc-hint", el); outlineBox = $("#nc-outline", el); undoBtn = $('[data-k="undo"]', el); redoBtn = $('[data-k="redo"]', el);
  vp = new Viewport(scroller, sizer, world, {onZoom: z => { $("#nc-zv", el).textContent = Math.round(z * 100) + "%"; links.draw(); }});
  links = new Links({world, vp, elOf: id => els.get(id), list: () => S.notes ? S.notes.links || [] : [], onAdd: (a, b) => N.addLink(a, b), onLabel: (id, label) => N.updateLink(id, {label}), onRemove: id => N.deleteLink(id)});
  frames = new Frames({world, vp, list: () => S.notes ? S.notes.frames || [] : [], cards: () => [...els].map(([id, el]) => ({id, el})), onMove: () => links.draw(),
    onUpdate: (id, patch, quiet, moved) => { if (moved) N.batch("moveframe", () => { N.updateFrame(id, patch, true); N.moveCards(moved); }); else N.updateFrame(id, patch, quiet); moved && moved.forEach(m => { const c = N.cardById(m.id); if (c) { c.x = m.x; c.y = m.y; } }); size(); },
    onRemove: id => N.deleteFrame(id)});
  on("undo:state", () => { undoBtn.disabled = !N.canUndo(); redoBtn.disabled = !N.canRedo(); });
  el.addEventListener("click", e => {
    const k = e.target.closest("[data-k]")?.dataset.k;
    if (k === "add") addNote(); else if (k === "frame") addFrame(); else if (k === "tidy") tidy(); else if (k === "export") exportMd();
    else if (k === "undo") N.undo(); else if (k === "redo") N.redo();
    else if (k === "zin") vp.zoomTo(vp.z * 1.25); else if (k === "zout") vp.zoomTo(vp.z / 1.25); else if (k === "zfit") vp.fit();
    else if (k === "view") setView(e.target.closest("[data-v]").dataset.v);
  });
  const pn = $("#nc-pn", el), savePn = debounce(() => N.setPaperNote(pn.value), 500);
  pn.addEventListener("input", () => { pn.style.height = "auto"; pn.style.height = Math.min(pn.scrollHeight + 2, 220) + "px"; savePn(); });
  scroller.addEventListener("dblclick", e => { if (e.target === scroller || e.target === sizer || e.target === world) { const p = vp.toWorld(e.clientX, e.clientY); addNote(p.x, p.y); } });
  scroller.addEventListener("dragover", e => { if ([...e.dataTransfer.types].includes("application/x-jw-sel")) { e.preventDefault(); e.dataTransfer.dropEffect = "copy"; } });
  scroller.addEventListener("drop", e => {
    const raw = e.dataTransfer.getData("application/x-jw-sel"); if (!raw) return;
    e.preventDefault(); const d = JSON.parse(raw), p = vp.toWorld(e.clientX, e.clientY);
    N.addCard({type: "excerpt", text: d.text, src: {hl: null, page: d.parts[0].page, parts: d.parts, quote: {exact: d.text}}, x: Math.max(0, p.x - 20), y: Math.max(0, p.y - 10)});
  });
  wireCites(el, b => N.cardById(b.closest(".nc-card")?.dataset.id)?.cites);
  outlineBox.addEventListener("click", onOutline); outlineBox.addEventListener("input", onOutlineInput);
  on("tab:show", k => { if (k === "notes" && S.open) { els.forEach(autosize); autosize1(pn); size(); } });   // textareas can't be measured while hidden
  on("paper:opened", () => { setView("canvas"); vp.zoomTo(1); renderAll(); }); on("reader:closed", () => { els.clear(); $$(".nc-card", world).forEach(e => e.remove()); frames.render(); links.draw(); });
  on("cards:changed", d => { if (!S.open) return; if (d.add) { addEl(N.cardById(d.add)); S.fn.showTab?.("notes"); focusCard(d.add); } else if (d.del) { drop(d.del); links.draw(); } else if (d.id) refreshEl(d.id); badge(); if (view === "outline") outline(); });
  on("board:changed", () => { if (!S.open) return; frames.render(); links.draw(); size(); if (view === "outline") outline(); });
  on("notes:changed", d => { if (S.open && d.all) renderAll(); });
}
const autosize1 = t => { t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight + 2, 220) + "px"; };
function setView(v) {
  view = v; $$("#nc-views [data-v]", panel).forEach(b => b.classList.toggle("on", b.dataset.v === v));
  scroller.classList.toggle("hide", v !== "canvas"); outlineBox.classList.toggle("hide", v !== "outline");
  $$('[data-k="frame"],[data-k="tidy"],.nc-zoom', panel).forEach(b => b.classList.toggle("hide", v !== "canvas"));
  if (v === "outline") outline(); else { els.forEach(autosize); size(); }
}

function badge() {
  const n = S.notes ? S.notes.cards.length : 0; $("#rd-n-notes")?.replaceChildren(n ? ` ${n}` : "");
  hint.style.display = n || (S.notes && (S.notes.frames || []).length) ? "none" : "";
}
function renderAll() {
  els.clear(); $$(".nc-card", world).forEach(e => e.remove()); S.notes.cards.forEach(addEl); frames.render(); badge(); size();
  const pn = $("#nc-pn", panel); pn.value = S.notes.paper_note || ""; autosize1(pn); if (view === "outline") outline();
}
const size = () => {
  let w = 600, h = 500;
  els.forEach(e => { w = Math.max(w, e.offsetLeft + e.offsetWidth + 80); h = Math.max(h, e.offsetTop + e.offsetHeight + 120); });
  (S.notes ? S.notes.frames || [] : []).forEach(f => { w = Math.max(w, f.x + f.w + 80); h = Math.max(h, f.y + f.h + 120); });
  world.style.width = w + "px"; world.style.height = h + "px"; vp.resize(w, h); links.draw();
};

// where the next card goes: under the lowest card of the first column
export function nextPos() {
  let y = 20; els.forEach(e => { y = Math.max(y, e.offsetTop + e.offsetHeight + 14); });
  return {x: 20, y};
}
export function excerpt(h) { N.excerptFrom(h, nextPos()); }
function addNote(x, y) { const p = nextPos(); N.addCard({type: "note", x: x ?? p.x, y: y ?? p.y}); }
function addFrame() {                           // a new frame goes under the lowest card, sized to fit what you can see; drag cards into it
  const p = nextPos(), r = scroller.getBoundingClientRect();
  N.addFrame({x: 20, y: p.y + 6, w: clamp(Math.round(r.width / vp.z - 60), 220, 420), h: 260});
  requestAnimationFrame(() => scroller.scrollTo({top: Math.max(0, (p.y - 30) * vp.z), behavior: "auto"}));
}

function cardHtml(c) {
  const hl = c.src?.hl && N.hlById(c.src.hl), color = hl ? colorOf(hl.color) : "var(--ink-3)", pg = hl ? N.firstPage(hl) : c.src?.page;
  const tags = (c.tags || []).map(N.tagById).filter(Boolean).map(t => `<span class="tgc" style="--tc:${esc(t.color)}">${esc(N.tagLabel(t))}</span>`).join("");
  const head = c.type === "excerpt" ? `✂ Excerpt${pg ? " · p. " + pg : ""}` : c.type === "image" ? `▭ Figure / table · p. ${c.src?.page}` : c.type === "ai" ? `✨ ${esc(c.model || "AI answer")}` : "✎ Note";
  let body;
  if (c.type === "image") body = `<canvas class="nc-img"></canvas><textarea class="nc-note" rows="1" placeholder="Add a note…">${esc(c.note || "")}</textarea>`;
  else if (c.type === "excerpt") body = `<div class="nc-text">${esc(c.text)}</div><textarea class="nc-note" rows="1" placeholder="Add a note…">${esc(c.note || "")}</textarea>`;
  else if (c.type === "ai") body = `<div class="nc-q">${esc(c.question || "")}</div><div class="nc-text md">${answerHtml(c.text, c.cites)}</div>`;
  else body = `<div class="nc-md md${c.text ? "" : " hide"}" title="Click to edit (Markdown works)">${md(c.text || "")}</div><textarea class="nc-main${c.text ? " hide" : ""}" rows="3" placeholder="Write a note… (Markdown works)">${esc(c.text)}</textarea>`;
  return `<div class="nc-head" data-drag><span>${head}</span><span class="nc-act">${c.src && (c.src.hl || c.src.parts) ? `<button data-c="link" title="Show where this came from">⤴</button>` : ""}<button data-c="conn" title="Draw an arrow from this card to another one">⟶</button><button data-c="tag" title="Tags">🏷</button><button data-c="del" title="Delete">✕</button></span></div>
    ${body}<div class="nc-tags">${tags}</div><div class="nc-tagpick hide"></div><div class="nc-grip" title="Drag to resize"></div>`;
}
function addEl(c) {
  const el = document.createElement("div");
  el.className = "nc-card " + c.type; el.dataset.id = c.id; el.dataset.card = c.id; el.style.cssText = `left:${c.x}px;top:${c.y}px;width:${c.w}px`;
  el.style.setProperty("--hc", c.type === "excerpt" ? (c.src?.hl && N.hlById(c.src.hl) ? colorOf(N.hlById(c.src.hl).color) : "var(--ink-3)") : "var(--accent)");
  el.innerHTML = cardHtml(c); els.set(c.id, el); world.append(el); wireCard(el, c.id); autosize(el); size();
  if (c.type === "image") fillImage(el, c);
}
async function fillImage(el, c) {            // the picture is drawn from the PDF each time; the notes only store the page and the box
  const cv = $(".nc-img", el);
  try { const src = await T.renderCrop(c.src.page, c.src.rect, 680); cv.width = src.width; cv.height = src.height; cv.getContext("2d").drawImage(src, 0, 0); autosize(el); size(); }
  catch (e) { cv.replaceWith(Object.assign(document.createElement("div"), {className: "pmeta", style: "padding:8px 10px", textContent: "Couldn’t draw this area."})); }
}
function refreshEl(id) {                       // tags/links changed: redraw the card, keeping its position
  const c = N.cardById(id), old = els.get(id); if (!c || !old) return;
  const keepScroll = scroller.scrollTop, picking = !$(".nc-tagpick", old).classList.contains("hide");
  old.remove(); addEl(c); scroller.scrollTop = keepScroll;
  if (picking) tagPick(els.get(id), id);
}
function drop(id) { els.get(id)?.remove(); els.delete(id); size(); }
function focusCard(id) { const e = els.get(id); if (!e) return; e.scrollIntoView({block: "nearest"}); e.classList.add("new"); setTimeout(() => e.classList.remove("new"), 1500);
  const t = $("textarea.nc-main:not(.hide)", e); if (t) t.focus(); }
const autosize = el => $$("textarea", el).forEach(t => { t.style.height = "auto"; t.style.height = t.scrollHeight + 2 + "px"; });

// ---------------------------------------------------------------- the note assistant: type // in a note to link it to a card, a highlight or a page of this paper
let refBox = null, refSt = null;
function refItems(q, self) {
  const ql = q.toLowerCase().trim(), items = [], pg = /^(?:p\.?\s*)?(\d{1,4})$/.exec(ql);
  if (pg && S.pages.length && +pg[1] >= 1 && +pg[1] <= S.pages.length) items.push({label: "p. " + pg[1], sub: "go to page " + pg[1], href: "jw:page/" + pg[1], ico: "📄"});
  if (!ql) items.push({label: "p. " + (currentPage()), sub: "the page you are on", href: "jw:page/" + currentPage(), ico: "📄"});
  const has = t => !ql || t.toLowerCase().includes(ql);
  S.notes.cards.filter(c => c.id !== self).forEach(c => { const t = snip(c); if (has(t)) items.push({label: t, sub: c.type === "note" ? "note" : c.type === "excerpt" ? "excerpt" : c.type === "image" ? "figure / table" : "AI answer", href: "jw:card/" + c.id, ico: "▭"}); });
  S.notes.highlights.filter(h => (h.text || h.comment) && has((h.text || "") + " " + (h.comment || ""))).slice(0, 30).forEach(h => items.push({label: (h.text || h.comment).replace(/\s+/g, " ").slice(0, 60), sub: "highlight · p. " + N.firstPage(h), href: "jw:hl/" + h.id, ico: "▬"}));
  return items.slice(0, 8);
}
const currentPage = () => { try { return S.fn.currentPos().n; } catch (e) { return 1; } };
function refClose() { if (refBox) refBox.remove(); refBox = null; refSt = null; }
function refOpen(ta, id) {
  const caret = ta.selectionStart, upto = ta.value.slice(0, caret), at = upto.lastIndexOf("//");
  if (at < 0 || /\s\s/.test(upto.slice(at + 2)) || /\n/.test(upto.slice(at)) || (at > 0 && upto[at - 1] === ":")) return refClose();          // not "https://", and one word or number after the slashes
  const q = upto.slice(at + 2), items = refItems(q, id); if (!items.length) return refClose();
  if (!refBox) { refBox = document.createElement("div"); refBox.className = "nc-ref card"; refBox.addEventListener("mousedown", e => e.preventDefault()); refBox.addEventListener("click", e => { const b = e.target.closest("[data-ri]"); if (b) refPick(+b.dataset.ri); }); document.body.append(refBox); }
  refSt = {ta, id, at, items, sel: Math.min(refSt && refSt.ta === ta ? refSt.sel : 0, items.length - 1), q};
  refBox.innerHTML = `<div class="nc-reft">Link to…${q ? ` <small>“${esc(q)}”</small>` : ""}</div>` + items.map((it, i) => `<button data-ri="${i}" class="${i === refSt.sel ? "on" : ""}"><span>${it.ico}</span><b>${esc(it.label)}</b><small>${esc(it.sub)}</small></button>`).join("");
  const r = ta.getBoundingClientRect(); refBox.style.left = Math.min(r.left, innerWidth - 330) + "px"; refBox.style.top = Math.min(r.bottom + 4, innerHeight - refBox.offsetHeight - 8) + "px";
}
function refPick(i) {
  if (!refSt) return; const {ta, at, items, id} = refSt, it = items[i], caret = ta.selectionStart; if (!it) return;
  const text = `[${it.label.replace(/[\[\]]/g, "")}](${it.href})`;
  ta.value = ta.value.slice(0, at) + text + " " + ta.value.slice(caret); const pos = at + text.length + 1; ta.setSelectionRange(pos, pos); ta.dispatchEvent(new Event("input", {bubbles: true})); refClose(); ta.focus();
}
function refKeys(e) {
  if (!refSt) return;
  if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); refSt.sel = (refSt.sel + (e.key === "ArrowDown" ? 1 : refSt.items.length - 1)) % refSt.items.length; refBox.querySelectorAll("[data-ri]").forEach((b, i) => b.classList.toggle("on", i === refSt.sel)); }
  else if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); e.stopPropagation(); refPick(refSt.sel); }
  else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); refClose(); }
}
// a link made that way: a card flashes, a page opens, a highlight is shown
function followRef(href) {
  const [kind, id] = href.slice(3).split("/");
  if (kind === "page") return S.fn.jump(+id, 0);
  if (kind === "hl") { const h = N.hlById(id); if (!h) return toast("That highlight is gone."); const pt = h.parts[0]; return S.fn.jump(pt.page, Math.max(0, pt.rects[0][1] - 0.04), {flash: pt.rects}); }
  if (kind === "card") { if (view !== "canvas") setView("canvas"); const e = els.get(id); if (!e) return toast("That card is gone."); e.scrollIntoView({block: "center", inline: "center"}); e.classList.add("new"); setTimeout(() => e.classList.remove("new"), 1500); }
}
document.addEventListener("click", e => { const a = e.target.closest && e.target.closest('a[href^="jw:"]'); if (a && (a.closest(".nc-card") || a.closest("#nc-outline"))) { e.preventDefault(); followRef(a.getAttribute("href")); } });
function wireCard(el, id) {
  const save = debounce((patch) => N.updateCard(id, patch, true), 400);
  $$("textarea", el).forEach(t => { t.addEventListener("input", () => { autosize(el); size(); save(t.classList.contains("nc-note") ? {note: t.value} : {text: t.value}); if (t.classList.contains("nc-main")) refOpen(t, id); });
    t.addEventListener("keydown", refKeys, true); t.addEventListener("blur", () => setTimeout(refClose, 120)); });
  el.addEventListener("click", e => {
    const b = e.target.closest("[data-c]"); if (!b) return;
    const c = N.cardById(id);
    if (b.dataset.c === "del") N.deleteCard(id);
    else if (b.dataset.c === "link") showSource(c);
    else if (b.dataset.c === "tag") tagPick(el, id);
    else if (b.dataset.c === "conn") links.start(id, on => $(".nc-connhint", panel).classList.toggle("hide", !on));
  });
  const mdBox = $(".nc-md", el), main = $("textarea.nc-main", el);
  if (mdBox) {                                   // a note shows as formatted Markdown; click to edit, leave the box to see it formatted again
    mdBox.addEventListener("click", e => { if (e.target.closest("a")) return; mdBox.classList.add("hide"); main.classList.remove("hide"); autosize(el); main.focus(); size(); });
    main.addEventListener("blur", () => { if (!main.value.trim()) return; mdBox.innerHTML = md(main.value); mdBox.classList.remove("hide"); main.classList.add("hide"); size(); });
  }
  $(".nc-head", el).addEventListener("pointerdown", e => {            // drag by the header
    if (e.target.closest("button")) return;
    const sx = e.clientX, sy = e.clientY, ox = el.offsetLeft, oy = el.offsetTop, z = vp.z; el.setPointerCapture(e.pointerId); el.classList.add("drag");
    const move = ev => { el.style.left = Math.max(0, ox + (ev.clientX - sx) / z) + "px"; el.style.top = Math.max(0, oy + (ev.clientY - sy) / z) + "px"; links.draw(); };
    const up = () => { el.releasePointerCapture(e.pointerId); el.classList.remove("drag"); el.removeEventListener("pointermove", move); el.removeEventListener("pointerup", up);
      N.updateCard(id, {x: el.offsetLeft, y: el.offsetTop}, true); size(); };
    el.addEventListener("pointermove", move); el.addEventListener("pointerup", up);
  });
  $(".nc-grip", el).addEventListener("pointerdown", e => {            // resize width
    e.preventDefault(); e.stopPropagation(); const sx = e.clientX, w0 = el.offsetWidth; const g = e.currentTarget; g.setPointerCapture(e.pointerId);
    const move = ev => { el.style.width = clamp(w0 + (ev.clientX - sx) / vp.z, 180, 720) + "px"; autosize(el); links.draw(); };
    const up = () => { g.removeEventListener("pointermove", move); g.removeEventListener("pointerup", up); N.updateCard(id, {w: el.offsetWidth}, true); size(); };
    g.addEventListener("pointermove", move); g.addEventListener("pointerup", up);
  });
}
function showSource(c) {
  const hl = c.src?.hl && N.hlById(c.src.hl), parts = hl ? hl.parts : c.src?.parts;
  if (!parts || !parts.length) return toast("This card isn’t linked to a place in the paper.");
  const pt = parts[0]; S.fn.jump(pt.page, Math.max(0, pt.rects[0][1] - 0.04), {flash: pt.rects});
}
function tagPick(el, id) {
  const box = $(".nc-tagpick", el), c = N.cardById(id);
  if (box.classList.toggle("hide")) return;      // it was open: now closed
  const draw = () => { box.innerHTML = S.tags.map(t => `<span class="tgc pick${(N.cardById(id).tags || []).includes(t.id) ? " on" : ""}" data-t="${esc(t.id)}" style="--tc:${esc(t.color)}">${esc(N.tagLabel(t))}</span>`).join("") +
    `<input type="text" placeholder="+ tag (category: name)" style="border:1px solid var(--line);border-radius:99px;padding:1px 8px;background:var(--panel-2);width:150px">`; };
  draw();
  box.onclick = e => { const t = e.target.closest("[data-t]"); if (t) { N.toggleTag("card", id, t.dataset.t); } };
  box.onkeydown = async e => { if (e.key === "Enter" && e.target.value.trim()) { const t = await N.ensureTag(e.target.value); if (t && !(c.tags || []).includes(t.id)) N.toggleTag("card", id, t.id); } };
}

function tidy() {                              // two columns, in the order the passages appear in the paper
  N.snap("tidy");
  const cs = S.notes.cards.slice().sort((a, b) => (srcPage(a) - srcPage(b)) || a.y - b.y);
  const colW = Math.max(...cs.map(c => c.w), 280) + 20, ys = [20, 20];
  cs.forEach(c => { const e = els.get(c.id), i = ys[0] <= ys[1] ? 0 : 1; c.x = 20 + i * colW; c.y = ys[i]; ys[i] += (e ? e.offsetHeight : 120) + 14; if (e) { e.style.left = c.x + "px"; e.style.top = c.y + "px"; } });
  N.updateCard(cs[0]?.id, {}, true); size();
}
const srcPage = c => { const hl = c.src?.hl && N.hlById(c.src.hl); return hl ? N.firstPage(hl) : (c.src?.page ?? 9999); };

// ---------------------------------------------------------------- the outline: the same cards as a document (your frames first, then your own notes, then the rest by page)
const cardBox = c => { const e = els.get(c.id); return {x: c.x, y: c.y, w: c.w || 280, h: e ? e.offsetHeight : 120}; };
const inFrame = (f, c) => { const b = cardBox(c), cx = b.x + b.w / 2, cy = b.y + b.h / 2; return cx >= f.x && cx <= f.x + f.w && cy >= f.y && cy <= f.y + f.h; };
const byPos = (a, b) => (a.y - b.y) || (a.x - b.x);
function groups() {
  const cards = S.notes.cards.slice(), used = new Set(), out = [];
  (S.notes.frames || []).slice().sort((a, b) => (a.y - b.y) || (a.x - b.x)).forEach(f => { const cs = cards.filter(c => !used.has(c.id) && inFrame(f, c)).sort(byPos); cs.forEach(c => used.add(c.id)); out.push({title: f.label || "Untitled group", kind: "frame", color: f.color, cards: cs}); });
  const rest = cards.filter(c => !used.has(c.id)), mine = rest.filter(c => srcPage(c) === 9999).sort(byPos);
  if (mine.length) out.push({title: "My notes", kind: "mine", cards: mine});
  const pages = [...new Set(rest.map(srcPage).filter(p => p !== 9999))].sort((a, b) => a - b);
  pages.forEach(pg => out.push({title: "Page " + pg, kind: "page", cards: rest.filter(c => srcPage(c) === pg).sort(byPos)}));
  return out;
}
const snip = c => { const t = (c.text || c.note || c.question || (c.type === "image" ? "figure / table, p. " + (c.src && c.src.page) : "")).replace(/[*_#>`]/g, "").replace(/^\s*-\s+/gm, "").replace(/\s+/g, " ").trim(); return t.length > 48 ? t.slice(0, 48) + "…" : t || "a card"; };
function outline() {
  const gs = groups(), L = S.notes.links || [], pn = (S.notes.paper_note || "").trim();
  const conn = c => L.filter(l => l.a === c.id || l.b === c.id).map(l => { const o = N.cardById(l.a === c.id ? l.b : l.a); return o ? `<span class="ol-lk">${l.a === c.id ? "→" : "←"} ${l.label ? "<i>" + esc(l.label) + "</i> " : ""}${esc(snip(o))}</span>` : ""; }).join("");
  const item = c => {
    const pg = srcPage(c), head = c.type === "excerpt" ? "✂ Excerpt" + (pg !== 9999 ? " · p. " + pg : "") : c.type === "image" ? "▭ Figure / table · p. " + (c.src && c.src.page) : c.type === "ai" ? "✨ " + esc(c.model || "AI answer") : "✎ Note";
    const body = c.type === "note" ? `<div class="ol-b md" data-ob="text">${md(c.text || "")}</div>` : c.type === "excerpt" ? `<blockquote class="ol-q">${esc(c.text)}</blockquote>${c.note ? `<div class="ol-b" data-ob="note">${esc(c.note)}</div>` : ""}`
      : c.type === "ai" ? `<div class="nc-q">${esc(c.question || "")}</div><div class="ol-b md">${answerHtml(c.text, c.cites)}</div>` : `<canvas class="nc-img ol-img" data-img="${c.id}"></canvas>${c.note ? `<div class="ol-b" data-ob="note">${esc(c.note)}</div>` : ""}`;
    return `<div class="ol-item ${c.type}" data-id="${c.id}"><div class="ol-h"><span>${head}</span><span class="ol-act">${c.src && (c.src.hl || c.src.parts) ? `<button data-o="src" title="Show where this came from">⤴</button>` : ""}${c.type === "note" || c.type === "excerpt" || c.type === "image" ? `<button data-o="edit" title="Edit">✎</button>` : ""}</span></div>${body}${conn(c) ? `<div class="ol-lks">${conn(c)}</div>` : ""}</div>`;
  };
  outlineBox.innerHTML = `<div class="ol-bar"><span class="pmeta">${S.notes.cards.length} card${S.notes.cards.length === 1 ? "" : "s"}${(S.notes.frames || []).length ? " · " + S.notes.frames.length + " group" + (S.notes.frames.length === 1 ? "" : "s") : ""}${L.length ? " · " + L.length + " arrow" + (L.length === 1 ? "" : "s") : ""}</span><button class="rd-btn" data-o="copymd" title="Copy the outline as Markdown">Copy as Markdown</button></div>
    ${pn ? `<div class="ol-pn"><div class="eyebrow">The paper as a whole</div><div class="md">${md(pn)}</div></div>` : ""}
    ${gs.map(g => `<section class="ol-g ${g.kind}"${g.color ? ` style="--fc:${esc(g.color)}"` : ""}><h4>${esc(g.title)} <small>${g.cards.length}</small></h4>${g.cards.length ? g.cards.map(item).join("") : `<div class="pmeta">Nothing in this group yet.</div>`}</section>`).join("") || `<div class="rd-empty">No cards yet. Add notes or excerpts, then come back for the outline.</div>`}`;
  $$("canvas[data-img]", outlineBox).forEach(async cv => { const c = N.cardById(cv.dataset.img); try { const src = await T.renderCrop(c.src.page, c.src.rect, 560); cv.width = src.width; cv.height = src.height; cv.getContext("2d").drawImage(src, 0, 0); } catch (e) { cv.remove(); } });
}
function onOutline(e) {
  const b = e.target.closest("[data-o]"), it = e.target.closest(".ol-item"); if (!b) return;
  if (b.dataset.o === "copymd") return void S.fn.copyText(outlineMd(), "Outline copied as Markdown.");
  const c = it && N.cardById(it.dataset.id); if (!c) return;
  if (b.dataset.o === "src") return showSource(c);
  if (b.dataset.o === "edit") {                  // swap the text for a box; typing saves as you go
    const field = c.type === "note" ? "text" : "note", host = $(`[data-ob="${field}"]`, it) || it.appendChild(Object.assign(document.createElement("div"), {className: "ol-b"}));
    if ($("textarea", it)) return void outline();
    host.innerHTML = `<textarea class="ol-ta" data-f="${field}" rows="3" placeholder="${field === "text" ? "Write a note… (Markdown works)" : "Add a note…"}">${esc(c[field] || "")}</textarea>`; const t = $("textarea", host); autosize1(t); t.focus();
  }
}
const saveOutline = debounce((id, patch) => { N.updateCard(id, patch, true); refreshEl(id); }, 400);
function onOutlineInput(e) { const t = e.target.closest(".ol-ta"); if (!t) return; autosize1(t); saveOutline(t.closest(".ol-item").dataset.id, {[t.dataset.f]: t.value}); }
function outlineMd() {
  const m = S.meta || {}, pn = (S.notes.paper_note || "").trim(), L = S.notes.links || [];
  let o = `# ${m.title || "Notes"}\n\n${pn ? pn + "\n\n" : ""}`;
  groups().forEach(g => {
    o += `## ${g.title}\n\n`;
    g.cards.forEach(c => {
      const pg = srcPage(c), to = L.filter(l => l.a === c.id).map(l => { const x = N.cardById(l.b); return x ? `\n  - ${l.label ? "*" + l.label + "* → " : "→ "}${snip(x)}` : ""; }).join("");
      if (c.type === "excerpt") o += `- > ${c.text.replace(/\n/g, " ")}${pg !== 9999 ? ` (p. ${pg})` : ""}${c.note ? "\n  " + c.note.replace(/\n/g, "\n  ") : ""}${to}\n`;
      else if (c.type === "ai") o += `- **${c.question || "AI answer"}**\n  ${(c.text || "").replace(/\n/g, "\n  ")}${to}\n`;
      else if (c.type === "image") o += `- [figure / table, p. ${c.src && c.src.page}]${c.note ? " " + c.note : ""}${to}\n`;
      else o += `- ${(c.text || "").replace(/\n/g, "\n  ")}${to}\n`;
    });
    o += "\n";
  });
  return o;
}

// Markdown export: the paper's reference, notes in reading order, then every highlight by page
function exportMd() {
  const m = S.meta, n = S.notes, tg = ids => (ids || []).map(N.tagById).filter(Boolean).map(t => "#" + N.tagLabel(t).replace(/[:\s]+/g, "-")).join(" ");
  let o = `# ${m.title}\n\n`;
  const au = Array.isArray(m.authors) ? m.authors.join(", ") : String(m.authors || "").replace(/;\s*/g, ", ");
  o += `${au ? au + " — " : ""}${m.journal || ""} ${m.year || ""}${m.doi ? ` · https://doi.org/${m.doi}` : ""}\n\n`;
  if ((n.paper_note || "").trim()) o += `## The paper as a whole\n\n${n.paper_note.trim()}\n\n`;
  const cards = n.cards.slice().sort((a, b) => srcPage(a) - srcPage(b) || a.y - b.y);
  if (cards.length) {
    o += `## Notes\n\n`;
    cards.forEach(c => {
      if (c.type === "excerpt") o += `> ${c.text.replace(/\n/g, "\n> ")}\n>\n> — p. ${srcPage(c)}${tg(c.tags) ? " " + tg(c.tags) : ""}\n\n${c.note ? c.note + "\n\n" : ""}`;
      else if (c.type === "ai") o += `**Q: ${c.question || ""}**\n\n${c.text}\n\n*(${c.model || "AI"}; citations are passage numbers)*\n\n`;
      else o += `${c.text}\n\n`;
    });
  }
  if (n.highlights.length) {
    o += `## Highlights\n\n`; let pg = 0;
    n.highlights.slice().sort((a, b) => N.firstPage(a) - N.firstPage(b) || a.parts[0].rects[0][1] - b.parts[0].rects[0][1]).forEach(h => {
      if (N.firstPage(h) !== pg) { pg = N.firstPage(h); o += `### Page ${pg}\n\n`; }
      o += `- ${h.text}${h.comment ? `\n  - *${h.comment}*` : ""}${tg(h.tags) ? " " + tg(h.tags) : ""}\n`;
    });
  }
  const a = document.createElement("a"), name = (S.file.split("/").pop() || "notes").replace(/\.pdf$/i, "") + "-notes.md";
  a.href = URL.createObjectURL(new Blob([o], {type: "text/markdown"})); a.download = name; document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
}
