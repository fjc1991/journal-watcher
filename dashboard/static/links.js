// Links inside the paper: links the PDF already has, citations ([14], Rai et al., 2019) and "Table 3" / "Figure 2" mentions.
// Hover a citation: its entry from the paper's own reference list (DOI, "in my library?", TL;DR). Click: jump there; ← Back returns.
import {notifyError, S, $, esc, api, toast, clamp, on} from "./state.js";
import * as T from "./text.js";

let hov, pill, REFS = null, refsLoad = null, timer = 0, hideT = 0, pillT = 0, cur = null, card = null, raf = 0, lastEv = null;
const BACK = [];
const NUM = /\[(\d{1,3}(?:\s*[,–\-]\s*\d{1,3})*)\]/g;
const AY = /(\p{Lu}[\p{L}'’\-]+)(?:\s+et\s+al\.?|\s+(?:and|&)\s+\p{Lu}[\p{L}'’\-]+)?,?\s*\(?((?:19|20)\d{2}[a-z]?)\)?/gu;
const FIG = /\b(Tables?|Figures?|Fig\.|Exhibit)\s+([A-Z]?\d+[A-Za-z]?)/g;
const fold = s => (s || "").normalize("NFKD").replace(/[^a-zA-Z]/g, "").toLowerCase();
const inside = (r, x, y, m = 0.002) => x >= r[0] - m && x <= r[0] + r[2] + m && y >= r[1] - m && y <= r[1] + r[3] + m;

export function init() {
  const {root, pane} = S.ui;
  hov = document.createElement("div"); hov.className = "rd-hov hide"; root.append(hov);
  pill = document.createElement("button"); pill.className = "rd-backpill hide"; root.append(pill);
  pane.addEventListener("mousemove", e => { lastEv = e; if (!raf) raf = requestAnimationFrame(() => { raf = 0; probe(lastEv); }); });
  pane.addEventListener("mouseleave", scheduleHide);
  pane.addEventListener("scroll", () => hideCard(), {passive: true});
  hov.addEventListener("mouseenter", () => clearTimeout(hideT)); hov.addEventListener("mouseleave", scheduleHide); hov.addEventListener("click", onCard);
  pill.onclick = () => goBack();
  S.fn.followLink = followLink; S.fn.goBack = goBack;
  on("paper:opened", () => { reset(); setTimeout(loadRefs, 3500); }); on("reader:closed", reset);
}
// ---------------------------------------------------------------- where the paper itself cites a reference (the text layer is searched; no model)
function numsOf(s) { const out = []; String(s).split(/\s*,\s*/).forEach(p => { const m = /^(\d+)\s*[–\-]\s*(\d+)$/.exec(p.trim()); if (m) for (let i = +m[1]; i <= Math.min(+m[2], +m[1] + 60); i++) out.push(i); else if (/^\d+$/.test(p.trim())) out.push(+p.trim()); }); return out; }
async function contextsOf(c, r) {
  await T.all();
  const out = [], seen = new Set();
  for (const p of S.pages) {
    const str = p.tm.str; let m;
    const hits = [];
    if (c.n != null) { NUM.lastIndex = 0; while ((m = NUM.exec(str))) if (numsOf(m[1]).includes(c.n)) hits.push([m.index, m[0].length]); }
    else { AY.lastIndex = 0; while ((m = AY.exec(str))) if (fold(m[1]) === fold(c.surname) && (m[2] || "").slice(0, 4) === (c.year || "").slice(0, 4)) hits.push([m.index, m[0].length]); }
    for (const [i, len] of hits) {
      if (/^\s*[A-Z][\p{L}'’\-]+,\s+[A-Z]\./u.test(str.slice(i + len, i + len + 40)) && p.n >= (r ? r.page : 99)) continue;              // an entry of the reference list itself
      const b0 = str.lastIndexOf(". ", i), a = Math.max(b0 < 0 ? 0 : b0 + 2, i - 240), z = (() => { const e = str.indexOf(". ", i + len); return e < 0 ? Math.min(str.length, i + len + 200) : Math.min(e + 1, i + len + 240); })();
      const text = str.slice(a, z).replace(/\s+/g, " ").trim(), key = p.n + "|" + text.slice(0, 40);
      if (text.length > 25 && !seen.has(key)) { seen.add(key); out.push({page: p.n, text}); }
    }
  }
  return out.slice(0, 8);
}
function reset() { REFS = null; refsLoad = null; BACK.length = 0; hideCard(); pill?.classList.add("hide"); S.pages.forEach(p => { delete p._links; }); }
const loadRefs = () => REFS ? Promise.resolve(REFS) : (refsLoad ||= api("/api/doc/refs?file=" + encodeURIComponent(S.file)).then(d => (REFS = d.refs)).catch(() => (REFS = [])));

// ---------------------------------------------------------------- the PDF's own link annotations
function norm(vp, rect) {
  const [a, b, c, d, e, f] = vp.transform, tx = (x, y) => a * x + c * y + e, ty = (x, y) => b * x + d * y + f;
  const xs = [tx(rect[0], rect[1]), tx(rect[2], rect[3])], ys = [ty(rect[0], rect[1]), ty(rect[2], rect[3])];
  const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
  return [x0 / vp.width, y0 / vp.height, (x1 - x0) / vp.width, (y1 - y0) / vp.height];
}
async function pageLinks(p) {
  if (p._links) return p._links;
  p._links = [];
  try {
    const ann = await p.proxy.getAnnotations({intent: "display"}), vp = p.proxy.getViewport({scale: 1});
    p._links = ann.filter(a => a.subtype === "Link" && a.rect).map(a => ({r: norm(vp, a.rect), dest: a.dest || null, url: a.url || a.unsafeUrl || null})).filter(l => l.dest || l.url);
  } catch (e) { /* no links on this page */ }
  return p._links;
}
function pointAt(e) {
  const b = e.target.closest?.(".rd-band")?._b; if (!b) return null;
  const bb = b.el.getBoundingClientRect();
  return {b, p: b.p, x: (e.clientX - bb.left) / bb.width, y: b.y0 + (e.clientY - bb.top) / bb.height * (b.y1 - b.y0)};
}

// ---------------------------------------------------------------- what text is under the pointer?
function textAt(e) {
  const r = document.caretRangeFromPoint ? document.caretRangeFromPoint(e.clientX, e.clientY) : null;
  if (!r || r.startContainer.nodeType !== 3) return null;
  const span = r.startContainer.parentElement; if (!span || !span.closest(".textLayer")) return null;
  let before = "", after = "", n = span.previousElementSibling, k = 0;
  while (n && k++ < 2 && n.tagName !== "BR") { before = n.textContent + before; n = n.previousElementSibling; }
  n = span.nextElementSibling; k = 0;
  while (n && k++ < 2 && n.tagName !== "BR") { after += n.textContent; n = n.nextElementSibling; }
  return {text: before + span.textContent + after, off: before.length + r.startOffset};
}
function citationAt(e) {
  const t = textAt(e); if (!t || !/[\[(]|\d{4}|Table|Fig|Exhibit/.test(t.text)) return null;
  for (const m of t.text.matchAll(NUM)) if (t.off >= m.index && t.off <= m.index + m[0].length) {
    const nums = [...m[1].matchAll(/\d+/g)].map(x => ({n: +x[0], a: m.index + 1 + x.index})), hit = nums.find(x => t.off >= x.a && t.off <= x.a + String(x.n).length) || nums[0];
    return {type: "ref", key: "n" + hit.n, n: hit.n};
  }
  for (const m of t.text.matchAll(FIG)) if (t.off >= m.index && t.off <= m.index + m[0].length) return {type: "fig", key: m[1] + m[2], kind: m[1], num: m[2], label: m[0]};
  for (const m of t.text.matchAll(AY)) if (t.off >= m.index && t.off <= m.index + m[0].length) return {type: "ref", key: "ay" + fold(m[1]) + m[2], surname: m[1], year: m[2]};
  return null;
}
function resolve(c, refs) {
  if (c.n != null) return refs.find(r => r.n === c.n) || (refs.every(r => r.n == null) ? refs[c.n - 1] : null) || null;
  const sn = fold(c.surname), yr = c.year, cand = refs.filter(r => fold(r.surname) === sn && (r.year || "").slice(0, 4) === yr.slice(0, 4));
  return cand.find(r => r.year === yr) || cand[0] || null;
}
async function findCaption(kind, num) {
  await T.all();
  const k = /^fig/i.test(kind) ? "(?:Figure|Fig\\.)" : /^exh/i.test(kind) ? "Exhibit" : "Table";
  const lab = "(" + k + "\\s*" + num + ")";
  for (const re of [new RegExp("(?:^|[.!?]\\s)" + lab + "\\s*[.:\\u2014\\u2013-]\\s*[A-Z]", "g"), new RegExp("(?:^|\\s)" + lab + "\\s*[.:\\u2014\\u2013-]\\s*[A-Z]", "g"),
                     new RegExp("(?:^|[.!?)\\d]\\s)" + lab + "\\s+[A-Z][a-z]", "g"), new RegExp("(?:^|\\s)" + lab + "\\s+[A-Z][\\w-]*\\s+[A-Z]", "g")]) {   // the last two are the Elsevier style: "Table 1 Summary Statistics", no punctuation after the label
    for (const p of S.pages) { re.lastIndex = 0; const m = re.exec(p.tm.str);
      if (m) { const a = m.index + m[0].indexOf(m[1]), rects = T.rects(p.tm, a, a + m[1].length); return {page: p.n, y: rects[0] ? rects[0][1] : 0, rects, text: p.tm.str.slice(a, a + 140)}; } }
  }
  return null;
}

// ---------------------------------------------------------------- hover card
async function probe(e) {
  if (S.tool || !getSelection().isCollapsed || e.buttons) return;
  const pt = pointAt(e); if (!pt) return scheduleHide();
  if (!pt.p._links) pageLinks(pt.p);
  const link = (pt.p._links || []).find(l => inside(l.r, pt.x, pt.y)), c = citationAt(e);
  S.ui.pane.classList.toggle("over-link", !!(link || c));
  const key = c ? c.key : link ? "link:" + pt.p.n + ":" + Math.round(link.r[1] * 1000) : null;
  if (!key) return scheduleHide();
  if (key === cur) return void clearTimeout(hideT);
  cur = key; clearTimeout(timer); clearTimeout(hideT);
  timer = setTimeout(() => showCard(e.clientX, e.clientY, c, link), 240);
}
const scheduleHide = () => { clearTimeout(timer); clearTimeout(hideT); hideT = setTimeout(hideCard, 280); };
function hideCard() { clearTimeout(timer); hov?.classList.add("hide"); cur = null; card = null; S.ui?.pane.classList.remove("over-link"); }
function place(x, y) { hov.classList.remove("hide"); hov.style.left = clamp(x + 10, 8, innerWidth - 400) + "px"; hov.style.top = clamp(y + 16, 70, innerHeight - hov.offsetHeight - 10) + "px"; }

async function showCard(x, y, c, link) {
  if (c && c.type === "fig") {
    card = {kind: "fig", c}; hov.innerHTML = `<div class="hv-h">${esc(c.label)}</div><div class="hv-t hv-load">Looking for it…</div>`; place(x, y);
    const cap = await findCaption(c.kind, c.num); if (cur !== c.key) return;
    card.cap = cap; hov.innerHTML = cap ? `<div class="hv-h">${esc(c.label)} · p. ${cap.page}</div><div class="hv-t">${esc(cap.text)}…</div><div class="hv-a"><button data-h="go">Go there</button><button data-h="explain" title="Send the page to the AI and have this ${/^fig/i.test(c.kind) ? "figure" : "table"} explained">✨ Explain it</button></div>`
      : `<div class="hv-h">${esc(c.label)}</div><div class="hv-t">I couldn’t find its caption.</div>`;
    return place(x, y);
  }
  if (c && c.type === "ref") {
    card = {kind: "ref", c}; hov.innerHTML = `<div class="hv-h">Reference</div><div class="hv-t hv-load">Reading the reference list…</div>`; place(x, y);
    const refs = await loadRefs(); if (cur !== c.key) return;
    const r = resolve(c, refs); card.r = r;
    if (!r) { hov.innerHTML = `<div class="hv-h">${esc(c.surname ? c.surname + " " + c.year : "[" + c.n + "]")}</div><div class="hv-t">Not found in this paper’s reference list.</div>`; return place(x, y); }
    hov.innerHTML = `<div class="hv-h">${r.n != null ? "Reference " + r.n : esc(r.surname) + " " + esc(r.year)} <small>p. ${r.page}</small></div><div class="hv-t">${esc(r.text)}</div><div class="hv-m" id="hv-info"></div><div class="hv-ctx" id="hv-ctx"></div>
      <div class="hv-a"><button data-h="go">Go to entry</button>${r.in_library ? `<button data-h="open">Open in my library</button>` : r.doi ? `<button data-h="add" title="Look for an open-access copy and add it to your library">Add to library</button>` : ""}
      ${r.doi ? `<a href="https://doi.org/${esc(r.doi)}" target="_blank" rel="noopener">DOI ↗</a>` : ""}<button data-h="copy">Copy</button></div>`;
    place(x, y);
    contextsOf(c, r).then(cs => { const el = $("#hv-ctx", hov); if (!el || cur !== c.key) return; card.ctx = cs;
      el.innerHTML = cs.length ? `<div class="hv-ch">Cited in this paper (${cs.length})</div>${cs.slice(0, 4).map((x, i) => `<div class="hv-cx" data-cx="${i}" title="Go there"><b>p. ${x.page}</b> ${esc(x.text.length > 170 ? x.text.slice(0, 170) + "…" : x.text)}</div>`).join("")}${cs.length > 4 ? `<div class="pmeta">and ${cs.length - 4} more</div>` : ""}<button class="hv-use" data-h="use">✨ How is it used?</button><div class="hv-ans" id="hv-ans"></div>` : `<div class="pmeta">No sentence of the text cites it by this number or name.</div>`; place(x, y); }).catch(() => {});
    if (r.doi) api("/api/cite/info?doi=" + encodeURIComponent(r.doi)).then(i => { const el = $("#hv-info", hov);
      if (el && cur === c.key && (i.tldr || i.citations != null)) el.innerHTML = (i.citations != null ? `<b>${i.citations}</b> citations${i.venue ? " · " + esc(i.venue) : ""}` : "") + (i.tldr ? `<br><i>TL;DR</i> ${esc(i.tldr)}` : ""); }).catch(() => {});
    return;
  }
  if (link && link.dest) {
    const d = await S.fn.destPage(link.dest); if (!d) return;
    card = {kind: "link", d}; hov.innerHTML = `<div class="hv-t">Link to page ${d.page} — click to go there</div>`; place(x, y);
  } else if (link && link.url) { card = {kind: "url"}; hov.innerHTML = `<div class="hv-t">${esc(link.url.slice(0, 120))}</div>`; place(x, y); }
}
async function onCard(e) {
  const cx = e.target.closest("[data-cx]");
  if (cx && card && card.ctx) { const x = card.ctx[+cx.dataset.cx], rs = await T.quoteRects(x.page, x.text.replace(/^[^A-Za-z0-9]+/, "").slice(0, 70)).catch(() => null); return go(x.page, rs && rs[0] ? rs[0][1] : 0, rs || []); }
  const b = e.target.closest("[data-h]"); if (!b || !card) return;
  const r = card.r;
  if (b.dataset.h === "use" && r && card.ctx) {
    b.disabled = true; const ans = $("#hv-ans", hov); ans.textContent = "Reading the sentences…";
    try { const x = await api("/api/ai/citeuse", {ref: r.text, contexts: card.ctx.map(c => c.text)}); ans.textContent = x.text; ans.title = (x.cached ? "saved answer" : x.cost != null ? "$" + x.cost.toFixed(4) : "") + " · " + x.model; b.remove(); }
    catch (er) { ans.textContent = ""; b.disabled = false; notifyError(er); }
    return;
  }
  if (b.dataset.h === "explain" && card.kind === "fig" && card.cap) { const k = card.c, cap = card.cap; hideCard(); return void (S.fn.explainFigure && S.fn.explainFigure(k, cap)); }
  if (b.dataset.h === "go") {
    if (card.kind === "fig" && card.cap) return go(card.cap.page, card.cap.y, card.cap.rects);
    if (r) { const rs = await T.quoteRects(r.page, r.text.slice(0, 90)).catch(() => null); go(r.page, (rs && rs[0] ? rs[0][1] : r.rect[1]), rs && rs.length ? rs : [r.rect]); }
  } else if (b.dataset.h === "copy" && r) S.fn.copyText(r.text, "Reference copied.");
  else if (b.dataset.h === "open" && r) {
    const d = await api("/api/doc?file=" + encodeURIComponent(r.in_library)).catch(() => null);
    if (d) window.Reader.open({file: r.in_library, title: d.meta.title, authors: d.meta.authors, abbr: d.meta.abbr, journal: d.meta.journal, year: d.meta.year, doi: d.meta.doi, summary: d.meta.summary}, false);
  } else if (b.dataset.h === "add" && r) {
    b.disabled = true; b.textContent = "Looking for a free copy…";
    try { const x = await api("/api/import/doi", {doi: r.doi}); b.textContent = x.existing ? "Already in your library" : "Added ✓"; r.in_library = x.file; toast(x.existing ? "Already in your library." : "Added to your library: " + x.title.slice(0, 70)); }
    catch (er) { b.textContent = "No free copy found"; notifyError(er); }
  }
}

// ---------------------------------------------------------------- following links, and the way back
function go(page, y, flash) {
  const here = S.fn.currentPos(); BACK.push(here); if (BACK.length > 30) BACK.shift();
  hideCard(); S.fn.jump(page, Math.max(0, y - 0.05), {flash}); showPill(here);
}
function showPill(pos) { pill.textContent = "← Back to p. " + pos.n; pill.classList.remove("hide"); clearTimeout(pillT); pillT = setTimeout(() => pill.classList.add("hide"), 25000); }
export function goBack(quiet) {
  const p = BACK.pop();
  if (!p) { if (!quiet) toast("Nothing to go back to."); return false; }
  S.fn.jump(p.n, p.y); if (BACK.length) showPill(BACK[BACK.length - 1]); else pill.classList.add("hide");
  return true;
}
async function followLink(e) {
  const pt = pointAt(e); if (!pt) return false;
  const link = (await pageLinks(pt.p)).find(l => inside(l.r, pt.x, pt.y));
  if (link && link.url) { window.open(link.url, "_blank", "noopener"); return true; }
  if (link && link.dest) { const d = await S.fn.destPage(link.dest); if (d) { go(d.page, d.y, [[0.04, Math.max(0, d.y - 0.003), 0.92, 0.018]]); return true; } }
  const c = citationAt(e); if (!c) return false;
  if (c.type === "fig") { const cap = await findCaption(c.kind, c.num); if (cap) { go(cap.page, cap.y, cap.rects); return true; } return false; }
  const r = resolve(c, await loadRefs()); if (!r) return false;
  const rs = await T.quoteRects(r.page, r.text.slice(0, 90)).catch(() => null);
  go(r.page, rs && rs[0] ? rs[0][1] : r.rect[1], rs && rs.length ? rs : [r.rect]); return true;
}
