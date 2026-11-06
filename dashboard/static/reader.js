// PDF reader: page rendering (PDF.js), text selection -> highlights, search, collapse, side panels.
// Loaded by index.html as a module; exposes window.Reader = {open, close, isOpen}.
// Pages are drawn as "bands": normally one band per page, in collapse mode several strips of a page with
// "⋯ N lines hidden" gaps between them. Highlights are fractions of the page, so every band can draw them.
import * as pdfjs from "/static/vendor/pdfjs/pdf.mjs";
import {errBox, notifyError, ico, S, $, $$, esc, fileUrl, clamp, debounce, toast, md, COLORS, colorOf, colorName, legend, emit, on, BOOK_HOW, BOOK_STALE, bookEmpty, usd} from "./state.js";
import {frame, mdParts, chipList, wire as wireOut} from "./output.js";
import * as N from "./notes.js";
import * as T from "./text.js";

pdfjs.GlobalWorkerOptions.workerSrc = "/static/vendor/pdfjs/pdf.worker.mjs";
const V = "/static/vendor/pdfjs/";
const TABS = [["notes", "Notes"], ["hl", "Highlights"], ["toc", "Contents"], ["sum", "Summary"], ["card", "Card"], ["skim", "Skim"], ["related", "Related"], ["ask", "Ask"]];
let root, pane, flow, io, C = null, A = null;
const panels = {};
const optional = p => import(p).catch(e => { console.warn("reader: optional module", p, e.message); return null; });

// ---------------------------------------------------------------- build the page once
async function build() {
  if (root) return;
  root = document.createElement("div");
  root.id = "reader"; root.className = "rd hide"; root.setAttribute("role", "region"); root.setAttribute("aria-label", "Paper reader");
  root.innerHTML = `
  <div class="rd-bar">
    <button class="btn back" data-a="back">← Back</button>
    <div class="rd-title" id="rd-title"></div>
    <div class="rd-tools">
      <button class="rd-btn" data-a="prev" title="Previous page">‹</button>
      <input class="rd-num" id="rd-num" value="1" aria-label="Page number"><span id="rd-of"></span><span id="rd-left" class="rd-left" title="Estimated from the words left and your reading speed (Settings)"></span>
      <button class="rd-btn" data-a="next" title="Next page">›</button><span class="sep"></span>
      <button class="rd-btn" data-a="zout" title="Zoom out (Ctrl −)">−</button><span id="rd-zoom" style="min-width:42px;text-align:center"></span>
      <button class="rd-btn" data-a="zin" title="Zoom in (Ctrl +)">+</button><button class="rd-btn" data-a="fit" id="rd-fit" title="Fit to width — the page keeps filling the space when you resize the notes (Ctrl 0)">Fit</button><button class="rd-btn" data-a="fitpage" id="rd-fitpage" title="Fit the whole page on screen (Ctrl Shift 0)">Page</button><span class="sep"></span>
      <button class="rd-btn" data-a="find" title="Search this paper (Ctrl F)">🔍 Search</button>
      <button class="rd-btn" data-a="collapse" id="rd-collapse" title="Collapse the paper around search results or highlights">⇕ Collapse</button>
      <button class="rd-btn" data-a="skim" id="rd-skimbtn" title="Skim: mark the sentences that matter by lens (question, finding, limitation…), then highlight the ones you want">⚡ Skim</button>
      <span class="sep"></span><button class="rd-btn" data-a="undo" id="rd-undo" title="Undo (Ctrl Z)" disabled>↶</button><button class="rd-btn" data-a="redo" id="rd-redo" title="Redo (Ctrl Shift Z)" disabled>↷</button>
      <button class="rd-btn" data-a="tool-note" id="rd-t-note" title="Sticky note: then click on the page to drop a note">📝</button>
      <button class="rd-btn" data-a="tool-area" id="rd-t-area" title="Box a figure or table: copy it to your notes, highlight it, or ask AI about it">▭ Area</button><span class="sep"></span>
      <button class="rd-btn" data-a="dark" title="Dark pages">◐</button>
      <button class="rd-btn" data-a="reflow" id="rd-reflowbtn" title="Text view: the paper as flowing text you can enlarge, with your highlights marked">¶ Text</button>
      <button class="rd-btn" data-a="twin" title="Read two papers side by side: open another paper in a second window beside this one">⧉ Beside</button>
      <button class="rd-btn" data-a="side" title="Show or hide the notes pane">▥</button>
      <span class="rd-state" id="rd-state" data-a="retry"></span>
    </div>
  </div>
  <div class="rd-progress"><i id="rd-progi"></i></div>
  <div class="rd-find hide" id="rd-find">
    <input type="search" id="rd-q" placeholder="Search this paper…" autocomplete="off">
    <span class="rd-count" id="rd-cnt"></span>
    <button class="rd-btn" data-a="fprev" title="Previous result">↑</button><button class="rd-btn" data-a="fnext" title="Next result">↓</button>
    <button class="rd-btn" data-a="hlall" title="Highlight every result in the last colour">Highlight all</button>
    <span style="flex:1"></span>
    <label>Collapse to <select id="rd-src"></select></label>
    <label>Context <input type="range" id="rd-ctx" min="0" max="8" value="2"><span id="rd-ctxn">2</span> lines</label>
    <button class="rd-btn" data-a="findclose" title="Close search (Esc)">✕</button>
  </div>
  <div class="rd-body">
    <div class="rd-pdfwrap"><div class="rd-pdf" id="rd-pdf"><div class="rd-flow" id="rd-flow"></div></div><div class="rd-ticks" id="rd-ticks" title="Results and highlights in the whole paper — click to jump"></div>
      <div class="rd-load hide" id="rd-load"></div></div>
    <div class="rd-split" id="rd-split"></div>
    <aside class="rd-side" id="rd-side">
      <div class="rd-tabs" id="rd-tabs">${TABS.map(([k, t]) => `${k === "sum" ? `<span class="sepv" aria-hidden="true"></span>` : ""}<button data-tab="${k}" data-tool="${k}" title="${t}" aria-label="${t}">${ico(k)}<span class="tl">${t}</span><span class="n" id="rd-n-${k}"></span></button>`).join("")}</div>
      ${TABS.map(([k]) => `<div class="rd-panel" data-p="${k}"></div>`).join("")}
    </aside>
  </div>
  <div class="rd-float hide" id="rd-sel"></div>
  <div class="rd-float col hide" id="rd-pop"></div>
  <div class="rd-qn hide" id="rd-qn"><input id="rd-qni" placeholder="Go to a page, section or highlight — or search" autocomplete="off"><div id="rd-qnl"></div><div class="hint">↑ ↓ choose · Enter go · Esc close</div></div>`;
  document.body.append(root);
  pane = $("#rd-pdf", root); flow = $("#rd-flow", root); S.ui = {root, pane, flow};
  TABS.forEach(([k]) => { panels[k] = $(`.rd-panel[data-p="${k}"]`, root); });
  const mods = await Promise.all(["./canvas.js", "./ai.js", "./links.js", "./area.js", "./explain.js", "./card.js", "./discover.js", "./skim.js", "./reflow.js"].map(optional));
  [C, A] = mods;
  io = new IntersectionObserver(es => es.forEach(e => { if (e.isIntersecting) want(e.target._b.p); }), {root: pane, rootMargin: "1400px 0px"});
  wire();
  buildHl(); buildToc(); buildSum();
  mods.slice(2).forEach(m => m && m.init && m.init());      // links, area box, explain: optional extras that hook into the reader
  if (C) C.mount(panels.notes); else panels.notes.innerHTML = `<div class="rd-empty">The notes canvas didn’t load.</div>`;
  if (A) A.mount(panels.ask); else panels.ask.innerHTML = `<div class="rd-empty">AI features aren’t set up yet.</div>`;
  if (mods[5]) mods[5].mount(panels.card); else panels.card.innerHTML = `<div class="rd-empty">Paper cards didn’t load.</div>`;
  if (mods[7] && mods[7].mountList) mods[7].mountList(panels.skim); else panels.skim.innerHTML = `<div class="rd-empty">The skim list didn’t load.</div>`;
  if (mods[6]) mods[6].mountPaper(panels.related); else panels.related.innerHTML = `<div class="rd-empty">Related papers didn’t load.</div>`;
  loadPrefs(); window.addEventListener("jw-settings-changed", loadPrefs);
  try { S.mode = localStorage.getItem("jw-rd-mode") || "highlight"; } catch (e) {}
  try { if (localStorage.getItem("jw-rd-dark") === "1") root.classList.add("dark"); const w = +localStorage.getItem("jw-rd-side"); if (w) $("#rd-side", root).style.width = w + "px"; } catch (e) {}
}

async function loadPrefs() {      // colour names, one-click prompts, reading speed (Settings)
  try { const d = await (await fetch("/api/settings")).json(); Object.keys(legend).forEach(k => delete legend[k]); Object.assign(legend, d.prefs.legend || {});
    S.prompts = d.prompts || []; S.summaries = d.summaries || []; S.prefs = d.prefs; fillTemplates(); if (S.open) { renderHl(); updateProgress(); } } catch (e) {}
}
S.mode = "highlight"; S.tool = null;

// ---------------------------------------------------------------- open / close
let pushed = false;
export async function open(meta, push = true, opts = {}) {
  await build();
  if (S.open) await teardown();
  S.open = true; S.file = meta.file; S.meta = {...meta};
  S.pages = []; S.bands = []; S.find = {q: "", hits: [], cur: -1}; S.view = {keep: null, source: "results", context: 2, extra: new Set()}; S.fit = true;
  document.body.classList.remove("sb-open"); document.body.classList.add("reading"); root.classList.remove("hide");
  $("#rd-title", root).innerHTML = `${esc(meta.title)}<small>${esc(meta.journal || "")} ${esc(meta.year || "")}</small>`;
  loading("Opening…");
  if (push) { history.pushState({view: "read", meta}, ""); pushed = true; } else pushed = false;
  try {
    const [, pdf] = await Promise.all([N.load(meta.file), pdfjs.getDocument({
      url: fileUrl(meta.file), cMapUrl: V + "cmaps/", cMapPacked: true, standardFontDataUrl: V + "standard_fonts/",
      wasmUrl: V + "wasm/", iccUrl: V + "iccs/", isEvalSupported: false}).promise]);
    S.pdf = pdf;
    S.pages = await Promise.all([...Array(pdf.numPages)].map(async (_, i) => {
      const proxy = await pdf.getPage(i + 1), vp = proxy.getViewport({scale: 1});
      return {n: i + 1, proxy, w: vp.width, h: vp.height, cv: null, bands: []};
    }));
  } catch (e) {
    console.error(e); loading("Couldn’t open this PDF: " + e.message); S.open = false; return;
  }
  loading(null);
  $("#rd-of", root).textContent = "/ " + S.pages.length;
  let pos = null; try { pos = JSON.parse(localStorage.getItem("jw-pos:" + S.file)); } catch (e) {}
  if (pos && pos.scale) { S.scale = pos.scale; S.fit = pos.fit || false; }
  if (S.fit) S.scale = fitScale(S.fit);
  layout();
  if (pos && pos.n) jump(pos.n, pos.y || 0, {instant: true});
  if (S.tool) setTool(S.tool);
  refreshAll(); updUndo(); loadOutline(); emit("paper:opened"); updateProgress();
  setTimeout(() => { if (S.open) T.all().then(updateProgress).catch(() => {}); }, 1800);
  if (opts.goto) setTimeout(async () => {            // e.g. from the Library's highlights hub, or a limitation listed in a folder's Evidence view
    const g = opts.goto; let rects = g.rects, y = g.y || 0;
    if (!rects && g.quote) { try { rects = await T.quoteRects(g.page, g.quote); } catch (e) {} if (rects && rects.length) y = Math.max(0, rects[0][1] - 0.05); }
    if (S.open) jump(g.page, y, {flash: rects || undefined});
  }, 250);
  postState({opened: true, page: pos && pos.n || 1, total: S.pages.length});   // Library: status "reading", last opened
  showTab(opts.tab || localStorage.getItem("jw-rd-tab") || (S.notes.cards.length ? "notes" : "hl"));
}
async function teardown() {
  N.saveNow(); savePos.flush();
  try { await S.pdf?.destroy(); } catch (e) {}
  S.pdf = null; S.pages = []; S.bands = []; q.length = 0; flow && (flow.innerHTML = ""); hideFloats();
  emit("reader:closed");
}
export async function close(pop = true) {
  if (!S.open) return;
  S.open = false;
  await teardown();
  root.classList.add("hide"); document.body.classList.remove("reading");
  window.dispatchEvent(new Event("jw-reader-closed"));
  if (pop && pushed) { pushed = false; history.back(); }
}
window.Reader = {open, close, isOpen: () => S.open};

function loading(msg) { const l = $("#rd-load", root); l.classList.toggle("hide", !msg); l.textContent = msg || ""; }

// ---------------------------------------------------------------- layout: pages (or strips) -> bands
// S.fit is false (manual zoom), true (fit to width) or "page" (the whole page fits); fitW/fitH remember the pane size the scale was fitted to
function fitScale(mode = true) {
  const w = pane.clientWidth, h = pane.clientHeight; S.fitW = w; S.fitH = h;
  const byW = (w - 56) / Math.max(...S.pages.map(p => p.w));
  return clamp(mode === "page" ? Math.min(byW, (h - 24) / Math.max(...S.pages.map(p => p.h))) : byW, 0.4, 3);
}

function makeBand(p, y0, y1, head) {
  const W = Math.floor(p.w * S.scale), H = Math.floor(p.h * S.scale), bh = Math.max(6, Math.round((y1 - y0) * H));
  const el = document.createElement("div");
  el.className = "rd-band" + (y0 > 0 || y1 < 1 ? " cut" : "");
  el.dataset.n = p.n; el.dataset.y0 = y0; el.dataset.y1 = y1;
  el.style.cssText = `width:${W}px;height:${bh}px`;
  el.innerHTML = `<canvas class="rd-cv"></canvas><div class="rd-in" style="width:${W}px;height:${H}px;top:${-Math.round(y0 * H)}px"><div class="rd-ov"></div></div>` +
    (head ? `<span class="rd-pn">p. ${p.n}</span>` : "");
  const b = {el, p, y0, y1}; el._b = b; p.bands.push(b); S.bands.push(b); io.observe(el);
  return el;
}

function layout() {
  io.disconnect(); flow.innerHTML = ""; S.bands = []; S.pages.forEach(p => { p.bands = []; if (p.cvScale !== S.scale) { release(p); } });
  flow.style.setProperty("--total-scale-factor", S.scale);
  $("#rd-zoom", root).textContent = Math.round(S.scale * 100) + "%";
  $("#rd-fit", root).classList.toggle("on", S.fit === true); $("#rd-fitpage", root).classList.toggle("on", S.fit === "page");
  if (!S.view.keep) { for (const p of S.pages) flow.append(makeBand(p, 0, 1, false)); }
  else {
    const seen = new Set();
    for (const it of collapsedPlan()) {
      if (it.type === "band") { flow.append(makeBand(it.p, it.y0, it.y1, !seen.has(it.p.n))); seen.add(it.p.n); }
      else flow.append(makeGap(it));
    }
    if (!flow.children.length) flow.innerHTML = `<div class="rd-gap" data-a="expandall">Nothing to show — nothing matches. Click to show the whole paper.</div>`;
  }
  S.pages.forEach(p => { if (p.cv && p.cvScale === S.scale) attach(p); });
  renderTicks(); $("#rd-collapse", root).classList.toggle("on", !!S.view.keep);
}

// ---------------------------------------------------------------- render pages (2 at a time, nearest first)
const q = []; let running = 0;
function want(p) {
  if (p.cv && p.cvScale === S.scale) return attach(p);
  if (p._want) return;
  p._want = true; q.push(p); pump();
}
function pump() {
  while (running < 2 && q.length) {
    const p = q.shift(); running++;
    renderPage(p).catch(e => console.warn("render page", p.n, e)).finally(() => { p._want = false; running--; trim(); pump(); });
  }
}
async function renderPage(p) {
  const scale = S.scale, dpr = Math.min(window.devicePixelRatio || 1, 2), pdf = S.pdf;
  const vp = p.proxy.getViewport({scale});
  const cv = document.createElement("canvas");
  cv.width = Math.floor(vp.width * dpr); cv.height = Math.floor(vp.height * dpr); cv.className = "rd-cv";
  await p.proxy.render({canvasContext: cv.getContext("2d"), viewport: vp, transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : null}).promise;
  if (scale !== S.scale || pdf !== S.pdf) return;
  if (!p.textEl) {
    const el = document.createElement("div"); el.className = "textLayer";
    p.tl = new pdfjs.TextLayer({textContentSource: p.proxy.streamTextContent(), container: el, viewport: vp});
    await p.tl.render(); p.textEl = el;
  } else p.tl.update({viewport: vp});
  if (scale !== S.scale || pdf !== S.pdf) return;
  p.cv = cv; p.cvScale = scale;
  attach(p);
}
function release(p) { if (p.cv) { p.cv.width = 0; p.cv = null; } p.cvScale = 0; }
function trim() {                         // keep memory bounded on long papers
  const live = S.pages.filter(p => p.cv); if (live.length <= 14) return;
  const cur = currentPos().n;
  live.sort((a, b) => Math.abs(b.n - cur) - Math.abs(a.n - cur)).slice(0, live.length - 12).forEach(p => {
    release(p); p.bands.forEach(b => { const c = $(".rd-cv", b.el); if (c) c.width = 0; });
  });
}
function attach(p) {
  for (const b of p.bands) {
    let cv = $(".rd-cv", b.el);
    if (p.cv) {
      if (b.y0 === 0 && b.y1 === 1) { if (cv !== p.cv) { cv.replaceWith(p.cv); cv = p.cv; } }
      else {
        const sy = Math.round(b.y0 * p.cv.height), sh = Math.round((b.y1 - b.y0) * p.cv.height);
        cv.width = p.cv.width; cv.height = sh; cv.getContext("2d").drawImage(p.cv, 0, sy, p.cv.width, sh, 0, 0, p.cv.width, sh);
      }
    }
    const inner = $(".rd-in", b.el);
    if (p.textEl && !$(".textLayer", inner)) inner.append(p.textEl.cloneNode(true));
    paintOverlay(b);
  }
}

// ---------------------------------------------------------------- overlay: highlights, search hits, flashes
function box(r, y0, y1) { return `left:${r[0] * 100}%;top:${r[1] * 100}%;width:${r[2] * 100}%;height:${r[3] * 100}%`; }
function paintOverlay(b) {
  const ov = $(".rd-ov", b.el), n = b.p.n, f = document.createDocumentFragment();
  for (const h of S.notes.highlights) for (const part of h.parts) {
    if (part.page !== n) continue;
    const tg = h.tags.map(N.tagById).find(Boolean), kind = h.kind || "highlight";
    for (const r of part.rects) {
      if (r[1] + r[3] <= b.y0 || r[1] >= b.y1) continue;
      const d = document.createElement("div"); d.className = "hl " + kind + (tg ? " tagged" : "");
      d.style.cssText = box(r) + `;--c:${colorOf(h.color)}` + (kind === "highlight" ? `;background:${colorOf(h.color)}` : "") + (tg ? `;--tg:${tg.color}` : ""); d.dataset.id = h.id; f.append(d);
    }
  }
  S.find.hits.forEach((h, i) => { if (h.page !== n) return;
    for (const r of h.rects) { const d = document.createElement("div"); d.className = "hit" + (i === S.find.cur ? " cur" : ""); d.style.cssText = box(r); f.append(d); } });
  for (const hook of S.fn.overlayHooks || []) hook(n, b, f, box);          // extra layers (the skim layer's ghost highlights)
  ov.replaceChildren(f, ...ov.querySelectorAll(".flash"));     // a jump's flash must survive the repaint when the page finishes rendering
}
const repaint = pages => S.bands.forEach(b => { if (!pages || pages.includes(b.p.n)) paintOverlay(b); });
S.fn.repaint = repaint; S.fn.addOverlay = fn => (S.fn.overlayHooks ||= []).push(fn);
function flashRects(n, rects) {
  for (const b of S.bands) if (b.p.n === n) { const ov = $(".rd-ov", b.el);
    rects.forEach(r => { const d = document.createElement("div"); d.className = "flash"; d.style.cssText = box(r); ov.append(d); setTimeout(() => d.remove(), 1800); }); }
}

// ---------------------------------------------------------------- position, jump, zoom
const READ_LINE = 70;      // px below the top of the pane: where jump() puts the place you go to, and where currentPos() reads the place you are at. They must be the same line: with two (8 and 70), the page number fell back to the page above after a jump, and every zoom, Fit, "back" or reopened reading place moved the text by the difference
// Where you are: the point of the paper on the reading line. dy is how far that point lies below the line, in px, for the two times the line is not on a strip of the paper:
// in the gap before a strip (collapsed view's "lines hidden" bar, or the space between pages) the point is that strip's start, below the line (dy > 0);
// past the end of the last strip (a flow shorter than the line) it is that strip's end, above the line (dy < 0). Give dy back to jump() as {off} and the text stays exactly where it was.
function currentPos() {
  const line = pane.getBoundingClientRect().top + READ_LINE;
  let r = null;
  for (const b of S.bands) {
    r = b.el.getBoundingClientRect();
    if (r.bottom > line) return {n: b.p.n, y: b.y0 + clamp((line - r.top) / r.height, 0, 1) * (b.y1 - b.y0), dy: Math.max(0, r.top - line)};
  }
  const last = S.bands[S.bands.length - 1];
  return last ? {n: last.p.n, y: last.y1, dy: r.bottom - line} : {n: 1, y: 0, dy: 0};
}
export function jump(n, y = 0, {flash = null, instant = true, off = 0} = {}) {
  n = clamp(Math.round(n), 1, S.pages.length || 1);
  let b = S.bands.find(b => b.p.n === n && y >= b.y0 - 1e-4 && y <= b.y1 + 1e-4);
  if (!b && S.view.keep) { reveal(n, y); b = S.bands.find(b => b.p.n === n && y >= b.y0 - 1e-4 && y <= b.y1 + 1e-4); }
  if (!b) b = S.bands.find(b => b.p.n === n);
  if (!b) return;
  const r = b.el.getBoundingClientRect(), fr = (y - b.y0) / Math.max(1e-6, b.y1 - b.y0);
  pane.scrollTo({top: Math.max(0, r.top - pane.getBoundingClientRect().top + pane.scrollTop + fr * r.height - READ_LINE - off), behavior: instant ? "auto" : "smooth"});
  if (flash) setTimeout(() => flashRects(n, flash), 30);
  else $("#rd-num", root).value = n;
}
S.fn.jump = jump; S.fn.currentPos = currentPos;
const goPage = n => jump(n, 0);

function setScale(s, fit = false) {
  const a = currentPos(); S.fit = fit; S.scale = clamp(s, 0.4, 4);
  S.pages.forEach(p => { release(p); p.bands = []; });
  layout(); jump(a.n, a.y, {off: a.dy}); savePos();
}
const zoomBy = f => setScale(S.scale * f);
function postState(patch) {   // reading status / progress for the Library; failures never matter to reading
  fetch("/api/paper/state", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({file: S.file, ...patch}), keepalive: true}).catch(() => {});
}
const progressLater = debounce(() => { if (S.open && S.bands.length) postState({page: currentPos().n, total: S.pages.length}); }, 4000);
const savePos = debounce(() => {
  if (!S.open || !S.bands.length) return; const a = currentPos(); progressLater();
  try { localStorage.setItem("jw-pos:" + S.file, JSON.stringify({n: a.n, y: +a.y.toFixed(4), scale: S.scale, fit: S.fit})); } catch (e) {}
}, 500);

// ---------------------------------------------------------------- collapse (squeeze the paper to what matters)
async function keepLines() {
  await T.all((d, t) => { $("#rd-cnt", root).textContent = `Reading text ${d}/${t}…`; });
  const off = []; let tot = 0; S.pages.forEach(p => { off.push(tot); tot += p.tm.lines.length; });
  S.view.off = off; S.view.total = tot;
  const keep = new Set(), ctx = S.view.context, src = S.view.source;
  const mark = (n, y0, y1) => { const p = S.pages[n - 1], L = p.tm.lines; if (!L.length) return;
    const a = T.lineAt(p.tm, y0), b = T.lineAt(p.tm, y1);
    for (let i = Math.max(0, a - ctx); i <= Math.min(L.length - 1, b + ctx); i++) keep.add(off[n - 1] + i); };
  const ext = rects => [Math.min(...rects.map(r => r[1])), Math.max(...rects.map(r => r[1] + r[3]))];
  if (src === "results") S.find.hits.forEach(h => { const [a, b] = ext(h.rects); mark(h.page, a, b); });
  else S.notes.highlights.filter(h => src === "highlights" || h.tags.includes(src.slice(4))).forEach(h =>
    h.parts.forEach(pt => { const [a, b] = ext(pt.rects); mark(pt.page, a, b); }));
  return keep;
}
function collapsedPlan() {
  const keep = new Set([...S.view.keep, ...S.view.extra]), off = S.view.off, items = [];
  let gap = null;
  const flush = () => { if (gap) { items.push(gap); gap = null; } };
  for (const p of S.pages) {
    const L = p.tm.lines; let i = 0;
    while (i < L.length) {
      const g = off[p.n - 1] + i;
      if (keep.has(g)) {
        let j = i; while (j + 1 < L.length && keep.has(off[p.n - 1] + j + 1)) j++;
        flush(); items.push({type: "band", p, y0: Math.max(0, L[i].y0 - 0.004), y1: Math.min(1, L[j].y1 + 0.004)}); i = j + 1;
      } else { gap ||= {type: "gap", lines: 0, g0: g, g1: g, p0: p.n, p1: p.n}; gap.lines++; gap.g1 = g; gap.p1 = p.n; i++; }
    }
  }
  flush(); return items;
}
function makeGap(g) {
  const d = document.createElement("div"); d.className = "rd-gap";
  d.textContent = `⋯ ${g.lines} line${g.lines === 1 ? "" : "s"} hidden · ${g.p0 === g.p1 ? "p. " + g.p0 : "pp. " + g.p0 + "–" + g.p1} — click to show more`;
  d.onclick = () => { const a = currentPos(), k = 8;
    for (let i = 0; i < k; i++) { if (g.g0 + i <= g.g1) S.view.extra.add(g.g0 + i); if (g.g1 - i >= g.g0) S.view.extra.add(g.g1 - i); }
    layout(); jump(a.n, a.y, {off: a.dy}); };
  return d;
}
function reveal(n, y) {   // make sure the line at (page, y) is shown, with its context
  const p = S.pages[n - 1]; if (!p.tm) return;
  const i = T.lineAt(p.tm, y); if (i < 0) return;
  for (let k = Math.max(0, i - S.view.context); k <= Math.min(p.tm.lines.length - 1, i + S.view.context); k++) S.view.extra.add(S.view.off[n - 1] + k);
  layout();
}
async function setCollapse(on, source) {
  if (!on) { const a = currentPos(); S.view.keep = null; layout(); jump(a.n, a.y, {off: a.dy}); refreshFind(); return; }
  if (source) S.view.source = source;
  const a = currentPos();
  S.view.keep = await keepLines(); S.view.extra = new Set();
  layout(); jump(a.n, a.y, {off: a.dy}); refreshFind();
  if (!S.view.keep.size) toast("Nothing to collapse to yet — search for something or highlight some text first.");
}

// ---------------------------------------------------------------- search
let findSeq = 0;
async function runFind(text) {
  const seq = ++findSeq, needle = T.norm(text);
  S.find = {q: text, hits: [], cur: -1};
  if (needle.length < 2) { repaint(); renderTicks(); refreshFind(); if (S.view.keep && S.view.source === "results") await setCollapse(true); return; }
  await T.all((d, t) => { $("#rd-cnt", root).textContent = `Reading text ${d}/${t}…`; });
  if (seq !== findSeq) return;
  const hits = [];
  for (const p of S.pages) for (const m of T.findAll(p.tm, needle)) { if (hits.length >= 3000) break; hits.push({page: p.n, a: m.a, b: m.b, rects: T.rects(p.tm, m.a, m.b)}); }
  S.find.hits = hits.filter(h => h.rects.length); S.find.cur = S.find.hits.length ? 0 : -1;
  if (S.view.keep && S.view.source === "results") await setCollapse(true);
  repaint(); renderTicks(); refreshFind();
  if (S.find.cur === 0) gotoHit(0);
}
function gotoHit(i) {
  const hs = S.find.hits; if (!hs.length) return;
  const prev = S.find.cur; S.find.cur = (i + hs.length) % hs.length;
  const h = hs[S.find.cur]; repaint([h.page, hs[prev]?.page].filter(Boolean));
  const vis = S.bands.some(b => b.p.n === h.page && h.rects[0][1] >= b.y0 && h.rects[0][1] <= b.y1);
  if (!vis || !isOnScreen(h)) jump(h.page, Math.max(0, h.rects[0][1] - 0.05));
  refreshFind();
}
function isOnScreen(h) {
  const b = S.bands.find(b => b.p.n === h.page && h.rects[0][1] >= b.y0 && h.rects[0][1] <= b.y1); if (!b) return false;
  const r = b.el.getBoundingClientRect(), pr = pane.getBoundingClientRect(), y = r.top + (h.rects[0][1] - b.y0) / (b.y1 - b.y0) * r.height;
  return y > pr.top + 40 && y < pr.bottom - 40;
}
function refreshFind() {
  const f = S.find, el = $("#rd-cnt", root);
  el.textContent = !f.q ? "" : f.hits.length ? `${f.cur + 1} of ${f.hits.length}` : "No results";
  const sel = $("#rd-src", root), cur = S.view.source;
  sel.innerHTML = `<option value="results">search results</option><option value="highlights">all highlights</option>` +
    S.tags.map(t => `<option value="tag:${esc(t.id)}">tag · ${esc(N.tagLabel(t))}</option>`).join("");
  sel.value = cur;
  $("#rd-collapse", root).classList.toggle("on", !!S.view.keep);
}
function openFind(text) {
  const bar = $("#rd-find", root); bar.classList.remove("hide");
  const inp = $("#rd-q", root); if (text != null) inp.value = text; inp.focus(); inp.select(); refreshFind();
  if (text) runFind(text);
}
function closeFind() {
  $("#rd-find", root).classList.add("hide"); findSeq++; S.find = {q: "", hits: [], cur: -1}; $("#rd-q", root).value = "";
  repaint(); renderTicks();
  if (S.view.keep && S.view.source === "results") setCollapse(false);
}

// minimap beside the scrollbar: highlights (their colour) and search hits over the whole paper
function renderTicks() {
  const t = $("#rd-ticks", root); if (!S.pages.length) { t.innerHTML = ""; return; }
  const cum = [0]; S.pages.forEach(p => cum.push(cum[cum.length - 1] + p.h)); const tot = cum[cum.length - 1];
  const at = (n, y) => ((cum[n - 1] + y * S.pages[n - 1].h) / tot * 100).toFixed(2);
  let h = "";
  S.notes.highlights.forEach(x => x.parts.forEach(pt => { h += `<i style="top:${at(pt.page, pt.rects[0][1])}%;background:${colorOf(x.color)}"></i>`; }));
  S.find.hits.forEach(x => { h += `<i class="hit" style="top:${at(x.page, x.rects[0][1])}%"></i>`; });
  t.innerHTML = h; t._cum = cum;
}
function tickClick(e) {
  const t = $("#rd-ticks", root), cum = t._cum; if (!cum) return;
  const f = (e.clientY - t.getBoundingClientRect().top) / t.clientHeight, pos = f * cum[cum.length - 1];
  let n = 1; while (n < S.pages.length && cum[n] < pos) n++;
  jump(n, clamp((pos - cum[n - 1]) / S.pages[n - 1].h, 0, 1));
}

// ---------------------------------------------------------------- selection -> highlights
function bandAt(x, y) { for (const b of S.bands) { const r = b.el.getBoundingClientRect(); if (x >= r.left && x <= r.right && y >= r.top && y <= r.bottom) return b; } return null; }
function describeSelection() {
  const sel = getSelection(); if (!sel || sel.isCollapsed || !sel.rangeCount) return null;
  const range = sel.getRangeAt(0); if (!pane.contains(range.commonAncestorContainer)) return null;
  const by = new Map();
  for (const r of range.getClientRects()) {
    if (r.width < 1 || r.height < 3) continue;
    const b = bandAt(r.left + r.width / 2, r.top + r.height / 2); if (!b) continue;
    const bb = b.el.getBoundingClientRect();
    if (r.height > bb.height * 0.5 && bb.height > 120 || r.width > bb.width * 1.01) continue;      // a container, not a line of text
    const x = clamp((r.left - bb.left) / bb.width, 0, 1), w = Math.min(1 - x, r.width / bb.width);
    const y = b.y0 + (r.top - bb.top) / bb.height * (b.y1 - b.y0), h = r.height / bb.height * (b.y1 - b.y0);
    (by.get(b.p.n) || by.set(b.p.n, []).get(b.p.n)).push([x, y, w, h]);
  }
  if (!by.size) return null;
  const parts = [...by].sort((a, b) => a[0] - b[0]).map(([page, rs]) => ({page, rects: T.mergeRects(rs).map(r => r.map(v => +v.toFixed(4)))}));
  const text = sel.toString().replace(/\s+/g, " ").replace(/(\w)- ([a-z])/g, "$1$2").trim();
  return text ? {parts, text, range} : null;
}
async function makeHighlight(color, kind = S.mode) {
  const sel = describeSelection(); if (!sel) return null;
  S.lastColor = color; delete sel.range;
  try { const tm = await T.model(S.pages[sel.parts[0].page - 1]), loc = T.locate(tm, sel.text.slice(0, 240));
    sel.quote = {exact: sel.text, ...(loc ? T.context(tm, loc.a, loc.b) : {})}; } catch (e) {}
  const h = N.addHighlight(sel, color, kind);
  getSelection().removeAllRanges(); hideFloats();
  return h;
}
const MODES = [["highlight", "▮", "Highlight"], ["underline", "U", "Underline"], ["strike", "S", "Strikethrough"]];
function showSelBar() {
  const d = describeSelection(), bar = $("#rd-sel", root);
  if (!d) { bar.classList.add("hide"); return; }
  bar.innerHTML = `<span class="modes">${MODES.map(([k, g, t]) => `<button class="rd-btn mode m-${k}${S.mode === k ? " on" : ""}" data-mode="${k}" title="${t}">${g}</button>`).join("")}</span>` +
    COLORS.map(c => `<span class="rd-dot${c.id === S.lastColor ? " on" : ""}" data-c="${c.id}" style="--c:${c.c}" title="${esc(colorName(c.id))} (${COLORS.indexOf(c) + 1})"></span>`).join("") +
    `<span style="width:4px"></span><button class="rd-btn" data-s="excerpt" title="Copy this passage onto the notes canvas">→ Notes</button>
     <button class="rd-btn" data-s="comment" title="Highlight and add a comment or tags">💬</button><button class="rd-btn" data-s="cite" title="Copy the quote with its citation">❝</button><button class="rd-btn" data-s="explain" title="Explain it, define it, translate it — or ask AI">✨</button>`;
  const rr = d.range.getBoundingClientRect(); bar.classList.remove("hide");
  bar.style.left = clamp(rr.right - 200, 8, innerWidth - 440) + "px";
  bar.style.top = (rr.bottom + 46 > innerHeight ? Math.max(70, rr.top - 46) : rr.bottom + 8) + "px";
}
const hideFloats = () => { $("#rd-sel", root)?.classList.add("hide"); $("#rd-pop", root)?.classList.add("hide"); };

async function selAction(e) {
  const dot = e.target.closest("[data-c]"), btn = e.target.closest("[data-s]"), mode = e.target.closest("[data-mode]");
  if (mode) { S.mode = mode.dataset.mode; try { localStorage.setItem("jw-rd-mode", S.mode); } catch (er) {} return showSelBar(); }
  if (dot) return void makeHighlight(dot.dataset.c);
  if (!btn) return;
  const d = describeSelection();
  if (btn.dataset.s === "cite") { if (d) copyText(citeString(d.text, d.parts[0].page), "Copied with its citation."); return; }
  if (btn.dataset.s === "explain") { if (d && S.fn.explainMenu) S.fn.explainMenu(btn, {text: d.text, page: d.parts[0].page}); else if (d && A) { showTab("ask"); A.askAbout(d.text); } return; }
  const h = await makeHighlight(S.lastColor, "highlight"); if (!h) return;
  if (btn.dataset.s === "excerpt" && C) { C.excerpt(h); showTab("notes"); }
  else if (btn.dataset.s === "comment") openPop(h.id, innerWidth / 2 - 135, innerHeight / 3, true);
}

// citing and linking
function authorsShort() {
  const raw = S.meta.authors, xs = Array.isArray(raw) ? raw : String(raw || "").split(/;\s*/).filter(Boolean), sn = a => a.trim().split(/\s+/).pop();
  return !xs.length ? "" : xs.length === 1 ? sn(xs[0]) : xs.length === 2 ? sn(xs[0]) + " & " + sn(xs[1]) : sn(xs[0]) + " et al.";
}
const citeString = (text, page) => `“${text}” (${[authorsShort(), S.meta.year].filter(Boolean).join(", ")}${authorsShort() || S.meta.year ? ", " : ""}p. ${page})` + (S.meta.doi ? " https://doi.org/" + S.meta.doi : "");
const deepLink = (page, y) => location.origin + "/#read=" + encodeURIComponent(S.file) + "&p=" + page + "&y=" + (+y).toFixed(3);
async function copyText(t, msg) { try { await navigator.clipboard.writeText(t); toast(msg); } catch (e) { prompt("Copy this:", t); } }
S.fn.hideFloats = hideFloats; S.fn.citeString = citeString; S.fn.deepLink = deepLink; S.fn.copyText = copyText;

// tools: sticky notes and area boxes are modes of the pointer
function setTool(t) {
  S.tool = S.tool === t ? null : t;
  root.classList.toggle("tool-note", S.tool === "note"); root.classList.toggle("tool-area", S.tool === "area");
  $("#rd-t-note", root).classList.toggle("on", S.tool === "note"); $("#rd-t-area", root).classList.toggle("on", S.tool === "area");
  if (S.tool) { hideFloats(); getSelection().removeAllRanges(); }
}
S.fn.setTool = setTool;
function placeNote(e) {
  const b = e.target.closest(".rd-band")?._b; if (!b) return;
  const bb = b.el.getBoundingClientRect(), x = (e.clientX - bb.left) / bb.width, y = b.y0 + (e.clientY - bb.top) / bb.height * (b.y1 - b.y0);
  const h = N.addHighlight({parts: [{page: b.p.n, rects: [[+clamp(x - 0.0125, 0, 0.975).toFixed(4), +clamp(y - 0.01, 0, 0.98).toFixed(4), 0.025, 0.02]]}], text: "", quote: {}}, S.lastColor, "note");
  setTool(null); openPop(h.id, e.clientX + 12, e.clientY + 12, true);
}
function updUndo() { $("#rd-undo", root).disabled = !N.canUndo(); $("#rd-redo", root).disabled = !N.canRedo(); }
const KINDS = {highlight: "Highlight", underline: "Underline", strike: "Strikethrough", note: "Sticky note", area: "Marked area"};
const hlLabel = h => h.kind === "note" ? "📝 " + (h.comment || "(empty note)") : h.kind === "area" ? "▭ Figure or table" + (h.comment ? " — " + h.comment : "") : h.text;

// click on a highlight -> popover (comment, colour, tags, delete)
function hlAt(e) {
  const b = e.target.closest(".rd-band")?._b; if (!b) return null;
  const bb = b.el.getBoundingClientRect(), x = (e.clientX - bb.left) / bb.width, y = b.y0 + (e.clientY - bb.top) / bb.height * (b.y1 - b.y0);
  let best = null;
  for (const h of S.notes.highlights) for (const pt of h.parts) if (pt.page === b.p.n)
    for (const r of pt.rects) {
      const inside = x >= r[0] - .002 && x <= r[0] + r[2] + .002 && y >= r[1] - .002 && y <= r[1] + r[3] + .002;
      if (!inside) continue;
      if (h.kind === "area" && x > r[0] + .008 && x < r[0] + r[2] - .008 && y > r[1] + .006 && y < r[1] + r[3] - .006) continue;   // only the frame of an area is clickable
      best = h;
    }
  return best;
}
function openPop(id, x, y, focus = false) {
  const h = N.hlById(id), pop = $("#rd-pop", root); if (!h) return;
  const tags = S.tags.map(t => `<span class="tgc pick${h.tags.includes(t.id) ? " on" : ""}" data-t="${esc(t.id)}" style="--tc:${esc(t.color)}">${esc(N.tagLabel(t))}</span>`).join("");
  const hasText = !!h.text;
  pop.innerHTML = `<div class="pkind">${KINDS[h.kind || "highlight"]} · p. ${N.firstPage(h)}</div><div class="dots">${COLORS.map(c => `<span class="rd-dot${c.id === h.color ? " on" : ""}" data-c="${c.id}" style="--c:${c.c}" title="${esc(colorName(c.id))}"></span>`).join("")}</div>
    <textarea placeholder="${h.kind === "note" ? "Write your note…" : "Comment…"}" id="rd-pc">${esc(h.comment)}</textarea>
    <div class="tgrow">${tags}<input type="text" id="rd-pt" placeholder="+ tag (category: name)" style="border:1px solid var(--line);border-radius:99px;padding:1px 8px;background:var(--panel-2);width:150px"></div>
    <div class="acts">${hasText ? `<button class="rd-btn" data-p="excerpt">→ Notes</button><button class="rd-btn" data-p="cite" title="Copy the quote with its citation">❝</button><button class="rd-btn" data-p="ask">✨</button>` : ""}<button class="rd-btn" data-p="link" title="Copy a link that opens the paper here">🔗</button><button class="rd-btn" data-p="del" style="color:var(--err)">Delete</button></div>`;
  pop.dataset.id = id; pop.classList.remove("hide");
  pop.style.left = clamp(x, 8, innerWidth - 290) + "px"; pop.style.top = clamp(y, 70, innerHeight - 300) + "px";
  if (focus) setTimeout(() => $("#rd-pc", root).focus(), 30);
}
function popEvents() {
  const pop = $("#rd-pop", root);
  pop.addEventListener("mousedown", e => { if (!e.target.closest("textarea,input")) e.preventDefault(); });
  pop.addEventListener("click", async e => {
    const id = pop.dataset.id, dot = e.target.closest("[data-c]"), tg = e.target.closest("[data-t]"), b = e.target.closest("[data-p]");
    if (dot) { N.updateHighlight(id, {color: dot.dataset.c}); S.lastColor = dot.dataset.c; openPop(id, parseFloat(pop.style.left), parseFloat(pop.style.top)); }
    else if (tg) { N.toggleTag("hl", id, tg.dataset.t); openPop(id, parseFloat(pop.style.left), parseFloat(pop.style.top)); }
    else if (b) {
      const h = N.hlById(id);
      if (b.dataset.p === "del") { N.deleteHighlight(id); pop.classList.add("hide"); }
      else if (b.dataset.p === "excerpt" && C && h) { C.excerpt(h); showTab("notes"); pop.classList.add("hide"); }
      else if (b.dataset.p === "ask" && h) { pop.classList.add("hide"); if (S.fn.explainMenu) S.fn.explainMenu(b, {text: h.text, page: N.firstPage(h)}); else if (A) { showTab("ask"); A.askAbout(h.text); } }
      else if (b.dataset.p === "cite" && h) copyText(citeString(h.text, N.firstPage(h)), "Copied with its citation.");
      else if (b.dataset.p === "link" && h) copyText(deepLink(N.firstPage(h), h.parts[0].rects[0][1]), "Link copied — it opens this paper at this spot.");
    }
  });
  pop.addEventListener("input", e => { if (e.target.id === "rd-pc") saveComment(pop.dataset.id, e.target.value); });
  pop.addEventListener("keydown", async e => {
    if (e.target.id === "rd-pt" && e.key === "Enter" && e.target.value.trim()) {
      const t = await N.ensureTag(e.target.value); if (t && !N.hlById(pop.dataset.id).tags.includes(t.id)) N.toggleTag("hl", pop.dataset.id, t.id);
      openPop(pop.dataset.id, parseFloat(pop.style.left), parseFloat(pop.style.top));
    }
  });
}
const saveComment = debounce((id, v) => N.updateHighlight(id, {comment: v}), 400);

// ---------------------------------------------------------------- side panels
function showTab(k) {
  $$(".rd-tabs button", root).forEach(b => b.classList.toggle("on", b.dataset.tab === k));
  $$(".rd-panel", root).forEach(p => p.classList.toggle("on", p.dataset.p === k));
  root.classList.remove("noside"); S.tab = k;
  try { localStorage.setItem("jw-rd-tab", k); } catch (e) {}
  if (k === "sum") loadSum();
  emit("tab:show", k);
}
S.fn.showTab = showTab;

// -- highlights tab (with the tag manager)
const HF = {color: "", tag: "", q: ""};
function buildHl() {
  const p = panels.hl;
  p.innerHTML = `<div class="rd-sub"><select id="hf-c"><option value="">All colours</option>${COLORS.map(c => `<option value="${c.id}">${c.name}</option>`).join("")}</select>
    <select id="hf-t"></select><input type="search" id="hf-q" placeholder="Filter…">
    <button class="rd-btn" id="hf-view" title="Collapse the paper to just your highlights">HighlightView</button></div>
    <div class="rd-scroll" id="hl-list"></div>
    <div class="tgm"><h4>Tags <span style="text-transform:none;letter-spacing:0;font-weight:400">— shared by all papers</span></h4><div id="tg-list"></div>
      <div class="row"><input type="text" id="tg-new" placeholder="New tag, e.g. method: survey  (Enter)"></div></div>`;
  $("#hf-c", p).onchange = e => { HF.color = e.target.value; renderHl(); };
  $("#hf-t", p).onchange = e => { HF.tag = e.target.value; renderHl(); };
  $("#hf-q", p).oninput = e => { HF.q = e.target.value.toLowerCase(); renderHl(); };
  $("#hf-view", p).onclick = () => { if (S.view.keep) setCollapse(false); else { S.view.source = "highlights"; setCollapse(true, "highlights"); } };
  $("#tg-new", p).onkeydown = async e => { if (e.key === "Enter" && e.target.value.trim()) { await N.ensureTag(e.target.value); e.target.value = ""; } };
  p.addEventListener("click", e => {
    const it = e.target.closest(".hlitem"), more = e.target.closest("[data-more]");
    if (more) { const r = more.getBoundingClientRect(); return void openPop(more.dataset.more, r.left - 250, r.bottom + 4); }
    if (it) { const h = N.hlById(it.dataset.id); if (h) { const pt = h.parts[0]; jump(pt.page, Math.max(0, pt.rects[0][1] - 0.04), {flash: pt.rects}); } }
  });
}
function renderHl() {
  const p = panels.hl, hs = S.notes.highlights;
  $("#hf-c", p).innerHTML = `<option value="">All colours</option>` + COLORS.map(c => `<option value="${c.id}"${HF.color === c.id ? " selected" : ""}>${esc(colorName(c.id))}</option>`).join("");
  $("#hf-t", p).innerHTML = `<option value="">All tags</option>` + S.tags.map(t => `<option value="${esc(t.id)}"${HF.tag === t.id ? " selected" : ""}>${esc(N.tagLabel(t))}</option>`).join("");
  const list = hs.filter(h => (!HF.color || h.color === HF.color) && (!HF.tag || h.tags.includes(HF.tag)) &&
    (!HF.q || (h.text + " " + h.comment).toLowerCase().includes(HF.q)))
    .sort((a, b) => N.firstPage(a) - N.firstPage(b) || a.parts[0].rects[0][1] - b.parts[0].rects[0][1]);
  $("#rd-n-hl", root).textContent = hs.length ? ` ${hs.length}` : "";
  let html = "", pg = 0;
  for (const h of list) {
    const n = N.firstPage(h); if (n !== pg) { pg = n; html += `<div class="hlpage">Page ${n}</div>`; }
    html += `<div class="hlitem" data-id="${h.id}" style="--hc:${colorOf(h.color)}" tabindex="0"><q class="k-${h.kind || "highlight"}">${esc(h.kind === "note" || h.kind === "area" ? hlLabel(h) : h.text)}</q>${h.comment && h.kind !== "note" && h.kind !== "area" ? `<div class="cm">${esc(h.comment)}</div>` : ""}
      <div class="tg">${h.tags.map(N.tagById).filter(Boolean).map(t => `<span class="tgc" style="--tc:${esc(t.color)}">${esc(N.tagLabel(t))}</span>`).join("")}
      <button class="rd-btn" data-more="${h.id}" style="margin-left:auto;padding:0 7px" title="Edit">⋯</button></div></div>`;
  }
  $("#hl-list", p).innerHTML = html || `<div class="rd-empty">${hs.length ? "No highlights match." : "Select text in the paper and pick a colour.<br>Everything you highlight is listed here."}</div>`;
  $("#tg-list", p).innerHTML = S.tags.map(t => `<div class="row" data-id="${esc(t.id)}"><input type="color" value="${esc(t.color)}"><input type="text" value="${esc(N.tagLabel(t))}">
    <button class="rd-btn" data-del="${esc(t.id)}" title="Delete tag">✕</button></div>`).join("") || `<div class="rd-empty" style="padding:6px">No tags yet.</div>`;
  $$("#tg-list .row", p).forEach(r => {
    const t = N.tagById(r.dataset.id);
    $("input[type=color]", r).onchange = e => { t.color = e.target.value; N.saveTags(); repaint(); };
    $("input[type=text]", r).onchange = e => { Object.assign(t, N.parseTag(e.target.value)); N.saveTags(); };
    $("[data-del]", r).onclick = () => { if (confirm(`Delete the tag “${N.tagLabel(t)}” from every highlight and note?`)) N.deleteTag(t.id); };
  });
}

// -- contents tab: bookmarks + the PDF's outline
function buildToc() {
  panels.toc.innerHTML = `<div class="rd-sub"><button class="rd-btn" id="bm-add">🔖 Bookmark this page</button><span class="seg" id="toc-seg"><button data-v="outline" class="on">Outline</button><button data-v="pages">Pages</button></span></div>
    <div class="rd-scroll" id="toc-list"></div><div class="rd-scroll thumbs hide" id="thumb-list"></div>`;
  $("#bm-add", panels.toc).onclick = () => N.toggleBookmark(currentPos().n);
  panels.toc.addEventListener("click", e => { const b = e.target.closest("[data-pg]"); if (b) jump(+b.dataset.pg, +b.dataset.y || 0);
    const th = e.target.closest(".thumb"); if (th) jump(+th.dataset.n, 0);
    const sg = e.target.closest("#toc-seg button"); if (sg) { $$("#toc-seg button", panels.toc).forEach(x => x.classList.toggle("on", x === sg));
      $("#toc-list", panels.toc).classList.toggle("hide", sg.dataset.v === "pages"); $("#thumb-list", panels.toc).classList.toggle("hide", sg.dataset.v !== "pages"); if (sg.dataset.v === "pages") buildThumbs(); } });
}
let thumbChain = Promise.resolve(), thumbFor = null;
function buildThumbs() {            // page thumbnails, drawn only when scrolled into view
  const box = $("#thumb-list", panels.toc); if (thumbFor === S.pdf) return; thumbFor = S.pdf;
  box.innerHTML = S.pages.map(p => `<div class="thumb" data-n="${p.n}" style="aspect-ratio:${p.w}/${p.h}"><canvas></canvas><small>${p.n}</small></div>`).join("");
  const mine = S.pdf, tio = new IntersectionObserver(es => es.forEach(en => { if (!en.isIntersecting) return; tio.unobserve(en.target);
    const p = S.pages[+en.target.dataset.n - 1], cv = $("canvas", en.target);
    thumbChain = thumbChain.then(async () => { if (S.pdf !== mine) return; const dpr = Math.min(devicePixelRatio || 1, 2), vp = p.proxy.getViewport({scale: 120 / p.w});
      cv.width = Math.floor(vp.width * dpr); cv.height = Math.floor(vp.height * dpr);
      await p.proxy.render({canvasContext: cv.getContext("2d"), viewport: vp, transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : null}).promise; }).catch(() => {}); }), {root: box, rootMargin: "300px"});
  box.querySelectorAll(".thumb").forEach(t => tio.observe(t)); updateProgress();
}
async function destPage(dest) {
  try {
    const d = typeof dest === "string" ? await S.pdf.getDestination(dest) : dest; if (!d) return null;
    const idx = typeof d[0] === "object" ? await S.pdf.getPageIndex(d[0]) : d[0];
    let y = 0; const p = S.pages[idx];
    if (d[1] && d[1].name === "XYZ" && d[3] != null && p) { const v = p.proxy.view; y = clamp((v[3] - d[3]) / (v[3] - v[1]), 0, 1); }
    return {page: idx + 1, y};
  } catch (e) { return null; }
}
S.fn.destPage = destPage;
async function loadOutline() {
  S.outline = [];
  try {
    const ol = await S.pdf.getOutline(), walk = (items, d) => items.forEach(it => { S.outline.push({title: it.title, depth: d, dest: it.dest}); walk(it.items || [], d + 1); });
    if (ol) walk(ol, 0);
    for (const o of S.outline) { const r = await destPage(o.dest); if (r) Object.assign(o, r); }
  } catch (e) {}
  renderToc();
}
function renderToc() {
  const bm = S.notes.bookmarks.slice().sort((a, b) => a.page - b.page);
  $("#rd-n-toc", root).textContent = bm.length ? ` ${bm.length}` : "";
  let h = bm.length ? `<div class="hlpage">Bookmarks</div><ul class="ol">${bm.map(b => `<li><button data-pg="${b.page}">🔖 ${esc(b.label)}<small>p. ${b.page}</small></button></li>`).join("")}</ul>` : "";
  const ol = (S.outline || []).filter(o => o.page);
  h += ol.length ? `<div class="hlpage">Outline</div><ul class="ol">${ol.map(o => `<li><button data-pg="${o.page}" data-y="${o.y || 0}" style="padding-left:${6 + o.depth * 14}px">${esc(o.title)}<small>${o.page}</small></button></li>`).join("")}</ul>` : "";
  $("#toc-list", panels.toc).innerHTML = h || `<div class="rd-empty">This PDF has no outline.<br>Bookmark pages with the button above, or just start typing to jump around.</div>`;
}

// -- summary tab: the markdown the pipeline wrote next to the PDF, and "generate with another template" (shown, not saved, until you say so)
function buildSum() {
  panels.sum.innerHTML = `<div class="rd-sub"><select id="sum-t" title="Summary template (edit them in Settings)"></select><button class="rd-btn" id="sum-go" title="Write this kind of summary with the model chosen in Settings; the saved summary is not touched">Generate</button>
    <button class="rd-btn hide" id="sum-saved">Show saved</button><span class="pmeta hide" id="sum-bk">A book is summarised chapter by chapter in Library → Books → Chapters, not with a paper template.</span></div><div class="rd-scroll" id="sum-body"></div><div class="ex-act hide" id="sum-act"></div>`;
  wireOut($("#sum-body", panels.sum));
  $("#sum-go", panels.sum).onclick = () => genSummary(false);
  $("#sum-saved", panels.sum).onclick = () => { delete $("#sum-body", root).dataset.file; $("#sum-act", root).classList.add("hide"); $("#sum-saved", root).classList.add("hide"); loadSum(); };
  panels.sum.addEventListener("click", e => { const x = e.target.closest("[data-sx]")?.dataset.sx; if (!x) return; const text = $("#sum-body", root)._gen;
    if (x === "again") genSummary(true);
    else if (x === "copy") copyText(text, "Summary copied.");
    else if (x === "notes" && C) { const p = C.nextPos(); N.addCard({type: "ai", question: "Summary — " + ($("#sum-body", root)._title || ($("#sum-t", root).selectedOptions[0] || {}).textContent || ""), text, cites: [], model: $("#sum-body", root)._model || "", x: p.x, y: p.y, w: 360}); showTab("notes"); toast("Saved to your notes."); }
    else if (x === "replace") replaceSummary(text); });
  fillTemplates();
}
// a summary in the shared frame: the model's "## headings" become sections (TL;DR as the lead), "Matched interests" become chips
function sumFrame(text, o = {}) {
  const mi = /^\*\*Matched interests\*\*:\s*(.*)$/m.exec(text);
  text = text.replace(/^# .*\n/, "").replace(/^\*\*Authors\*\*.*\n/m, "").replace(/^\*\*Matched interests\*\*.*\n/m, "").trim();
  return frame({tool: "sum", kind: "Summary", icon: "sum", key: "sum:" + S.file + ":" + (o.key || "saved"), title: o.title || "", jump: !o.live, notices: o.notices || "",
    sub: mi ? chipList(mi[1].split(/,\s*/).filter(Boolean).map(t => `<span class="of-badge">${esc(t)}</span>`)) : "", meta: o.meta || [],
    actions: o.live ? "" : `<button class="rd-btn" data-sx="copy" title="Copy the summary">Copy</button><button class="rd-btn" data-sx="notes" title="Put the summary on your notes canvas">→ Notes</button>`, sections: mdParts(text)});
}
function fillTemplates() {
  const sel = $("#sum-t", root); if (!sel) return;
  sel.innerHTML = (S.summaries || []).map(t => `<option value="${esc(t.id)}">${esc(t.name)}</option>`).join("");
  if (S.prefs && S.prefs.summary_template) sel.value = S.prefs.summary_template;
}
async function genSummary(fresh) {
  const el = $("#sum-body", root), act = $("#sum-act", root), tpl = $("#sum-t", root).value, tplName = $("#sum-t", root).selectedOptions[0].textContent; let text = "", done = null;
  const forFile = S.file; el._title = tplName;
  el.dataset.file = "_generated"; el.innerHTML = `<span class="ask-status">Reading the whole paper…</span>`; act.classList.add("hide"); $("#sum-go", root).disabled = true;
  try {
    const r = await fetch("/api/ai/summary", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({file: S.file, template: tpl, fresh})});
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
    const rd = r.body.getReader(), dec = new TextDecoder(); let buf = "";
    for (;;) {
      const {value, done: fin} = await rd.read(); if (fin) break; buf += dec.decode(value, {stream: true}); let i;
      while ((i = buf.indexOf("\n")) >= 0) { const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1); if (!line) continue; const m = JSON.parse(line);
        if (S.file !== forFile) { rd.cancel().catch(() => {}); return; }       // (another paper was opened meanwhile: its tab is not written over)
        if (m.type === "text") { text += m.delta; el.innerHTML = sumFrame(text, {live: true, key: "gen", title: tplName}); } else if (m.type === "error") throw new Error(m.error); else if (m.type === "done") done = m; }
    }
    el._gen = text; el._model = done ? done.model : "";
    el.innerHTML = sumFrame(text, {key: "gen", title: tplName, meta: [done && {t: done.provider + " · " + done.model}, done && (done.cached ? {t: "cached"} : done.cost != null ? {t: "$" + done.cost.toFixed(3)} : null), {t: "not saved yet", cls: "edited", title: "Use the buttons below to keep it"}]});
    act.innerHTML = `<button class="rd-btn" data-sx="notes">→ Save to notes</button><button class="rd-btn" data-sx="replace">${S.meta.summary ? "Replace the saved summary" : "Save as this paper’s summary"}</button><button class="rd-btn" data-sx="copy">Copy</button><button class="rd-btn" data-sx="again" title="Generate again, ignoring the stored answer">⟳</button>`;
    act.classList.remove("hide"); $("#sum-saved", root).classList.toggle("hide", !S.meta.summary);
  } catch (e) { if (S.file === forFile) el.innerHTML = errBox(e, {retry: 'data-sx="again"'}); }
  finally { $("#sum-go", root).disabled = false; }            // (also when another paper was opened meanwhile: its Generate button works)
}
async function replaceSummary(text) {
  if (S.meta.summary && !confirm("Replace the saved summary of this paper with this one?\nThe old one is kept in state/summary_backups/.")) return;
  try {
    const r = await (await fetch("/api/doc/summary", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({file: S.file, text})})).json();
    if (r.error) throw new Error(r.error);
    S.meta.summary = r.path; const el = $("#sum-body", root); delete el.dataset.file; $("#sum-act", root).classList.add("hide"); $("#sum-saved", root).classList.add("hide"); loadSum(); toast("Summary saved.");
  } catch (e) { notifyError(e); }
}
let SUM_FOR = "";
async function loadSum() {
  const el = $("#sum-body", root), path = S.meta.summary, book = !!(S.meta && S.meta.journal === "Books");
  if (SUM_FOR !== S.file) { SUM_FOR = S.file; delete el.dataset.file; el._gen = ""; el._title = ""; $("#sum-act", root).classList.add("hide"); $("#sum-go", root).disabled = false; }      // (a summary of the last paper may still be coming: it stops at its next line)      // (another paper: nothing of the last one stays)
  $("#sum-t", root).classList.toggle("hide", book); $("#sum-go", root).classList.toggle("hide", book); $("#sum-bk", root).classList.toggle("hide", !book);
  if (book) { $("#sum-saved", root).classList.add("hide"); $("#sum-act", root).classList.add("hide"); if (await loadBookSum(el)) return; }
  if (!path) { delete el.dataset.file; el.innerHTML = book ? el.innerHTML : `<div class="rd-empty">No summary has been written for this paper yet.<br>Pick a template above and press Generate.</div>`; return; }
  if (el.dataset.file === path) return; el.dataset.file = path; el.textContent = "Loading…";
  try { const t = await (await fetch(fileUrl(path))).text(); el._gen = t.replace(/^# .*\n/, "").replace(/^\*\*Authors\*\*.*\n/m, ""); el.innerHTML = sumFrame(t, {meta: [{t: "saved with the paper"}]}); }
  catch (e) { el.textContent = "Could not load the summary."; delete el.dataset.file; }
}
async function loadBookSum(el) {       // a book: the overview of the whole book, read from its own record each time. True when it was shown (else the caller may show an old saved summary)
  const file = S.file; el.dataset.file = "book:" + file; el.textContent = "Loading…";
  let R = null; try { R = await (await fetch("/api/book?file=" + encodeURIComponent(file))).json(); if (R.error) R = null; } catch (e) { R = null; }
  if (S.file !== file || el.dataset.file !== "book:" + file) return true;
  const ov = R && R.overview;
  if (ov && ov.text) {
    el._gen = ov.text; el._title = "The whole book";
    el.innerHTML = sumFrame(ov.text, {key: "book", title: "The whole book", meta: [{t: "from the summaries of its " + R.parts + " chapters and sections"}, {t: ov.model || "the AI"}, ov.cost ? {t: usd(ov.cost)} : null,
      R.overview_stale ? {t: "out of date", cls: "edited", title: BOOK_STALE} : null].filter(Boolean), notices: R.overview_stale ? `<p class="pmeta warn">${esc(BOOK_STALE)}</p>` : ""});
    return true;
  }
  if (S.meta.summary) { delete el.dataset.file; return false; }
  const [say, how] = bookEmpty(R);
  el.innerHTML = `<div class="rd-empty">${esc(say)}${how ? "<br>" + esc(BOOK_HOW) : ""}</div>`;
  return true;
}

// ---------------------------------------------------------------- quick navigation: just start typing
let qn = {items: [], i: 0};
function qnOpen(first) { const b = $("#rd-qn", root), inp = $("#rd-qni", root); b.classList.remove("hide"); inp.value = first || ""; inp.focus(); qnUpdate(); }
const qnClose = () => { $("#rd-qn", root).classList.add("hide"); };
function qnUpdate() {
  const v = $("#rd-qni", root).value.trim(), lv = v.toLowerCase(), items = [];
  if (/^\d+$/.test(v)) items.push({t: `Go to page ${v}`, s: `of ${S.pages.length}`, run: () => goPage(+v)});
  if (v) {
    (S.outline || []).filter(o => o.page && o.title.toLowerCase().includes(lv)).slice(0, 6).forEach(o => items.push({t: o.title, s: "p. " + o.page, run: () => jump(o.page, o.y || 0)}));
    S.notes.highlights.filter(h => h.text.toLowerCase().includes(lv)).slice(0, 4).forEach(h => items.push({t: "▍" + h.text.slice(0, 90), s: "highlight p. " + N.firstPage(h), run: () => jump(N.firstPage(h), h.parts[0].rects[0][1], {flash: h.parts[0].rects})}));
    items.push({t: `Search the paper for “${v}”`, s: "", run: () => openFind(v)});
    if (A) items.push({t: `Ask AI: ${v}`, s: "", run: () => { showTab("ask"); A.ask(v); }});
  } else {
    S.notes.bookmarks.slice(0, 6).forEach(b => items.push({t: "🔖 " + b.label, s: "p. " + b.page, run: () => goPage(b.page)}));
    (S.outline || []).filter(o => o.page && o.depth === 0).slice(0, 8).forEach(o => items.push({t: o.title, s: "p. " + o.page, run: () => jump(o.page, o.y || 0)}));
  }
  qn = {items, i: 0}; qnDraw();
}
const qnDraw = () => { $("#rd-qnl", root).innerHTML = qn.items.map((o, i) => `<button class="opt${i === qn.i ? " on" : ""}" data-i="${i}">${esc(o.t)}<small>${esc(o.s)}</small></button>`).join(""); };
function qnRun(i) { const o = qn.items[i]; qnClose(); if (o) o.run(); }

// reading progress and time left (needs the text of every page, which is read quietly in the background after opening)
function updateProgress() {
  if (!S.pages.length) return;
  const pos = currentPos(), n = S.pages.length;
  $("#rd-progi", root).style.width = (100 * (pos.n - 1 + pos.y) / n).toFixed(1) + "%";
  const words = S.pages.map(p => p.tm ? p.tm.str.split(/\s+/).length : null);
  if (words.some(w => w == null)) return;
  const left = words.slice(pos.n).reduce((a, b) => a + b, 0) + words[pos.n - 1] * (1 - pos.y), m = Math.round(left / ((S.prefs && S.prefs.wpm) || 230));
  $("#rd-left", root).textContent = m < 1 ? "· finished" : m < 90 ? `· ~${m} min left` : `· ~${(m / 60).toFixed(1)} h left`;
  $$("#thumb-list .thumb", root).forEach(t => t.classList.toggle("cur", +t.dataset.n === pos.n));
}

// ---------------------------------------------------------------- events
function refreshAll() { renderHl(); renderToc(); repaint(); renderTicks(); refreshFind(); saveState(); }
function saveState() {
  const st = $("#rd-state", root), m = {saved: "Saved", saving: "Saving…", dirty: "Unsaved…", error: "Not saved — click to retry", conflict: "Changed elsewhere — click to reload"}[S.save];
  st.textContent = m; st.classList.toggle("bad", S.save === "error" || S.save === "conflict");
}
function wire() {
  on("save:state", saveState);
  on("notes:changed", d => { if (!S.open) return; renderHl(); renderToc(); repaint(d.all ? null : d.pages); renderTicks(); refreshFind();
    if (S.view.keep && S.view.source !== "results" && d.hl !== undefined) { /* highlights changed while collapsed: keep the current view */ } });
  on("tags:changed", () => { renderHl(); refreshFind(); repaint(); });
  on("undo:state", updUndo);
  root.addEventListener("click", e => {
    const a = e.target.closest("[data-a]"); if (a) return void act(a.dataset.a, e);
    const tb = e.target.closest(".rd-tabs button"); if (tb) return void showTab(tb.dataset.tab);
    const q = e.target.closest("#rd-qnl .opt"); if (q) return void qnRun(+q.dataset.i);
    if (e.target.closest("#rd-ticks")) return void tickClick(e);
    if (e.target.closest(".rd-band") && S.tool === "note") return void placeNote(e);
    if (e.target.closest(".rd-band") && !S.tool && getSelection().isCollapsed) {      // a click on an annotation opens it; on a link, follows it
      const h = hlAt(e); if (h) openPop(h.id, e.clientX + 6, e.clientY + 10);
      else { $("#rd-pop", root).classList.add("hide"); if (S.fn.followLink) S.fn.followLink(e); }
    }
  });
  $("#rd-sel", root).addEventListener("mousedown", e => e.preventDefault());
  $("#rd-sel", root).addEventListener("click", selAction);
  popEvents();
  pane.addEventListener("mouseup", () => setTimeout(showSelBar, 0));
  pane.addEventListener("scroll", () => { requestAnimationFrame(() => { $("#rd-num", root).value = currentPos().n; savePos(); updateProgress(); }); }, {passive: true});
  pane.addEventListener("wheel", e => { if (e.ctrlKey || e.metaKey) { e.preventDefault(); zoomWheel(e.deltaY < 0 ? 1.08 : 1 / 1.08); } }, {passive: false});
  pane.addEventListener("dragstart", e => {            // drag a selection onto the notes canvas to make an excerpt
    const d = describeSelection(); if (!d) return; delete d.range;
    e.dataTransfer.setData("application/x-jw-sel", JSON.stringify(d)); e.dataTransfer.setData("text/plain", d.text); e.dataTransfer.effectAllowed = "copy";
  });
  $("#rd-num", root).onkeydown = e => { if (e.key === "Enter") goPage(+e.target.value || 1); e.stopPropagation(); };
  const fq = $("#rd-q", root);
  fq.oninput = debounce(() => runFind(fq.value), 280);
  fq.onkeydown = e => { if (e.key === "Enter") { e.preventDefault(); if (S.find.q !== fq.value) runFind(fq.value); else gotoHit(S.find.cur + (e.shiftKey ? -1 : 1)); } else if (e.key === "Escape") { e.stopPropagation(); closeFind(); } };
  $("#rd-src", root).onchange = e => { S.view.source = e.target.value; if (S.view.keep) setCollapse(true); };
  $("#rd-ctx", root).oninput = e => { S.view.context = +e.target.value; $("#rd-ctxn", root).textContent = e.target.value; ctxLater(); };
  const qi = $("#rd-qni", root);
  qi.oninput = qnUpdate;
  qi.onkeydown = e => {
    if (e.key === "Escape") { e.stopPropagation(); qnClose(); } else if (e.key === "Enter") qnRun(qn.i);
    else if (e.key === "ArrowDown") { qn.i = Math.min(qn.items.length - 1, qn.i + 1); qnDraw(); e.preventDefault(); }
    else if (e.key === "ArrowUp") { qn.i = Math.max(0, qn.i - 1); qnDraw(); e.preventDefault(); }
  };
  const sp = $("#rd-split", root);
  sp.onmousedown = e => { e.preventDefault(); const side = $("#rd-side", root), move = ev => { side.style.width = clamp(innerWidth - ev.clientX, 260, innerWidth * 0.7) + "px"; },
    up = () => { removeEventListener("mousemove", move); removeEventListener("mouseup", up); try { localStorage.setItem("jw-rd-side", parseInt(side.style.width)); } catch (er) {} };
    addEventListener("mousemove", move); addEventListener("mouseup", up); };
  // a fitted page follows the space it has: window resize, the divider, hiding the notes and switching tabs all change the pane's size
  let refitAt = 0, refitTimer = 0;
  const refit = () => {
    if (!S.open || !S.fit || !S.pages.length || root.classList.contains("hide")) return;
    const w = pane.clientWidth, h = pane.clientHeight; if (w < 120) return;
    if (Math.abs(w - S.fitW) < 3 && (S.fit !== "page" || Math.abs(h - S.fitH) < 3)) return;   // (a scrollbar appearing is not a resize)
    setScale(fitScale(S.fit), S.fit);
  };
  new ResizeObserver(() => {      // at most one refit per 90 ms while the divider is dragged, and always one for the final size
    if (refitTimer) return;
    refitTimer = setTimeout(() => { refitTimer = 0; refitAt = Date.now(); refit(); }, Math.max(0, 90 - (Date.now() - refitAt)));
  }).observe(pane);
  document.addEventListener("mousedown", e => { if (S.open && !e.target.closest("#rd-pop,#rd-sel,.rd-expmenu,.rd-exp,.rd-areamenu,#reader .rd-tabs")) hideFloats(); });
  document.addEventListener("selectionchange", debounce(() => { if (S.open && getSelection().isCollapsed) $("#rd-sel", root).classList.add("hide"); }, 150));
  document.addEventListener("keydown", keys);
}
const ctxLater = debounce(() => { if (S.view.keep) setCollapse(true); }, 250);
let zf = 1; const zoomLater = debounce(() => { const f = zf; zf = 1; zoomBy(f); }, 140);
function zoomWheel(f) { zf *= f; zoomLater(); }

function act(a, e) {
  const cur = currentPos();
  ({
    back: () => (pushed ? history.back() : close(false)), prev: () => goPage(cur.n - (cur.y > 0.05 ? 0 : 1)), next: () => goPage(cur.n + 1),
    zin: () => zoomBy(1.2), zout: () => zoomBy(1 / 1.2), fit: () => setScale(fitScale(), true), fitpage: () => setScale(fitScale("page"), "page"),
    find: () => openFind(), findclose: closeFind, fprev: () => gotoHit(S.find.cur - 1), fnext: () => gotoHit(S.find.cur + 1),
    hlall: hlAllHits, dark: () => { root.classList.toggle("dark"); try { localStorage.setItem("jw-rd-dark", root.classList.contains("dark") ? "1" : "0"); } catch (er) {} },
    reflow: () => S.fn.reflow && S.fn.reflow(), twin: () => window.Twin && window.Twin.open({file: S.file, page: cur.n}), side: () => root.classList.toggle("noside"), expandall: () => setCollapse(false),
    collapse: () => { if (S.view.keep) setCollapse(false); else { const src = S.find.hits.length ? "results" : S.notes.highlights.length ? "highlights" : null;
      if (!src) toast("Search for something or highlight some text first, then collapse."); else setCollapse(true, src); } },
    retry: () => { if (S.save === "conflict") N.takeConflict(); else N.saveNow(); },
    undo: () => { if (!N.undo()) toast("Nothing to undo."); }, redo: () => { if (!N.redo()) toast("Nothing to redo."); },
    "tool-note": () => setTool("note"), "tool-area": () => setTool("area"), skim: () => (S.fn.toggleSkim ? S.fn.toggleSkim() : toast("The skim layer didn’t load.")),
  }[a] || (() => {}))();
}
async function hlAllHits() {
  const hs = S.find.hits; if (!hs.length) return;
  if (hs.length > 40 && !confirm(`Highlight all ${hs.length} results?`)) return;
  N.batch("hlall", () => { for (const h of hs) { const tm = S.pages[h.page - 1].tm;
    N.addHighlight({parts: [{page: h.page, rects: h.rects.map(r => r.map(v => +v.toFixed(4)))}], text: tm.str.slice(h.a, h.b), quote: {exact: tm.str.slice(h.a, h.b), ...T.context(tm, h.a, h.b)}}, S.lastColor); } });
  toast(`Highlighted ${hs.length} results.`);
}

function keys(e) {
  if (!S.open) return;
  const t = e.target, typing = t.matches && t.matches("input,textarea,select,[contenteditable=true]");
  const mod = e.ctrlKey || e.metaKey;
  if (e.key === "Escape") {
    if (S.tool) return setTool(S.tool);
    if (!$("#rd-qn", root).classList.contains("hide")) return qnClose();
    if (!$("#rd-pop", root).classList.contains("hide") || !$("#rd-sel", root).classList.contains("hide")) { hideFloats(); return getSelection().removeAllRanges(); }
    if (typing) return t.blur();
    if (!$("#rd-find", root).classList.contains("hide")) return closeFind();
    if (S.view.keep) return setCollapse(false);
    return act("back");
  }
  if (mod && e.key.toLowerCase() === "f") { e.preventDefault(); return openFind(); }
  if (mod && !typing && e.key.toLowerCase() === "z") { e.preventDefault(); return act(e.shiftKey ? "redo" : "undo"); }
  if (mod && !typing && e.key.toLowerCase() === "y") { e.preventDefault(); return act("redo"); }
  if (mod && (e.key === "=" || e.key === "+")) { e.preventDefault(); return zoomBy(1.2); }
  if (mod && e.key === "-") { e.preventDefault(); return zoomBy(1 / 1.2); }
  if (mod && e.key === "0") { e.preventDefault(); return act("fit"); }
  if (mod && e.shiftKey && e.key === ")") { e.preventDefault(); return act("fitpage"); }
  if (e.altKey && e.key === "ArrowLeft" && !typing) { e.preventDefault(); if (S.fn.goBack) S.fn.goBack(); return; }
  if (typing || mod || e.altKey) return;
  if (!$("#rd-sel", root).classList.contains("hide") && /^[1-8]$/.test(e.key)) { e.preventDefault(); return void makeHighlight(COLORS[+e.key - 1].id); }
  if (e.altKey === false && e.key === "Backspace" && S.fn.goBack && S.fn.goBack(true)) { e.preventDefault(); return; }
  if (e.key.length === 1 && /\S/.test(e.key)) { e.preventDefault(); qnOpen(e.key); }
}
