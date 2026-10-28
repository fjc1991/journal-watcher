// Page text model: a page's text plus the geometry of every piece of it. Search, collapse-to-lines, quote
// anchoring and AI citations all work on this, so they share one idea of "where is this text on the page".
// Model: {str, lower, segs:[{s0,s1,item}], lines:[{y0,y1}], W, H}; all rects are fractions of the page [x,y,w,h].
import * as pdfjs from "/static/vendor/pdfjs/pdf.mjs";
import {S} from "./state.js";

const SUB = {"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl", " ": " ", "­": ""};
export const clean = s => s.replace(/[ﬁﬂﬀﬃﬄ ­]/g, c => SUB[c]);
export const norm = s => clean(s).replace(/\s+/g, " ").trim().toLowerCase();

let mctx = null;
function measure(font, size, s) {          // width of s in a font, via canvas (to place a match inside a text item)
  mctx ||= document.createElement("canvas").getContext("2d");
  const f = size.toFixed(2) + "px " + font;
  if (mctx.font !== f) mctx.font = f;
  return mctx.measureText(s).width;
}

export async function model(p) {
  if (p.tm) return p.tm;
  return p._tmP ||= (async () => {
    const tc = await p.proxy.getTextContent();
    const vp = p.proxy.getViewport({scale: 1}), W = vp.width, H = vp.height;
    let str = "", prev = null;
    const segs = [], items = [];
    for (const it of tc.items) {
      if (it.str === undefined) continue;
      const s = clean(it.str);
      const [a, b, c, d, e, f] = pdfjs.Util.transform(vp.transform, it.transform);
      const fh = Math.hypot(c, d) || 1, dl = Math.hypot(a, b) || 1;
      const item = {s, e, f, u: [a / dl, b / dl], v: [c / fh, d / fh], w: it.width || 0, fh,
        font: (tc.styles[it.fontName] || {}).fontFamily || "sans-serif", eol: !!it.hasEOL};
      // separator before this item: nothing, a space, or (hyphenated line end) a joined word
      if (prev && s.trim()) {
        const dx = (e - prev.e) * prev.u[0] + (f - prev.f) * prev.u[1] - prev.w;        // gap along the baseline
        const dy = Math.abs(-(e - prev.e) * prev.u[1] + (f - prev.f) * prev.u[0]);       // distance across it
        if (prev.eol || dy > 0.6 * fh) {
          const ps = segs[segs.length - 1];
          if (/[A-Za-z]-$/.test(prev.s) && /^[a-z]/.test(s) && ps.s1 === str.length) { str = str.slice(0, -1); ps.s1--; }  // "inno-" + "vation" -> "innovation"
          else str += " ";
        } else if (dx > 0.2 * fh && !prev.s.endsWith(" ") && !s.startsWith(" ")) str += " ";
      }
      if (s) { segs.push({s0: str.length, s1: str.length + s.length, item}); str += s; }
      if (s.trim()) prev = item;
      items.push(item);
    }
    // lines for collapse bands: cluster items by vertical centre
    const boxes = items.filter(i => i.s.trim()).map(i => itemBox(i, 0, i.s.length)).sort((m, n) => (m.y0 + m.y1) - (n.y0 + n.y1));
    const lines = [];
    for (const bx of boxes) {
      const l = lines[lines.length - 1], mid = (bx.y0 + bx.y1) / 2;
      if (l && Math.abs(mid - l.mid) < 0.55 * Math.min(bx.y1 - bx.y0, l.y1 - l.y0)) {
        l.y0 = Math.min(l.y0, bx.y0); l.y1 = Math.max(l.y1, bx.y1); l.mid = (l.y0 + l.y1) / 2;
      } else lines.push({y0: bx.y0, y1: bx.y1, mid});
    }
    return p.tm = {str, lower: str.toLowerCase(), segs, lines: lines.map(l => ({y0: l.y0 / H, y1: l.y1 / H})), W, H};
  })();
}

// bounding box (page units at scale 1) of item[i:j]
function itemBox(it, i, j) {
  let t0 = 0, t1 = 1;
  if (i > 0 || j < it.s.length) {
    const full = measure(it.font, it.fh, it.s);
    t0 = full ? measure(it.font, it.fh, it.s.slice(0, i)) / full : i / it.s.length;
    t1 = full ? measure(it.font, it.fh, it.s.slice(0, j)) / full : j / it.s.length;
  }
  const asc = it.fh * 0.82, desc = it.fh * 0.18, xs = [], ys = [];
  for (const t of [t0, t1]) for (const k of [asc, -desc]) {
    xs.push(it.e + it.u[0] * it.w * t + it.v[0] * k); ys.push(it.f + it.u[1] * it.w * t + it.v[1] * k);
  }
  return {x0: Math.min(...xs), x1: Math.max(...xs), y0: Math.min(...ys), y1: Math.max(...ys)};
}

// text range [a,b) of the model -> merged rects (fractions of the page)
export function rects(tm, a, b) {
  const out = [];
  for (const sg of tm.segs) {
    if (sg.s1 <= a || sg.s0 >= b) continue;
    const i = Math.max(a, sg.s0) - sg.s0, j = Math.min(b, sg.s1) - sg.s0;
    if (j <= i) continue;
    const bx = itemBox(sg.item, i, j);
    out.push([bx.x0 / tm.W, bx.y0 / tm.H, (bx.x1 - bx.x0) / tm.W, (bx.y1 - bx.y0) / tm.H]);
  }
  return mergeRects(out);
}

// join rects that sit on one line and nearly touch (words of one phrase) -> fewer, cleaner highlight boxes
export function mergeRects(rs) {
  const out = [];
  for (const r of rs.slice().sort((p, q) => (p[1] + p[3] / 2) - (q[1] + q[3] / 2) || p[0] - q[0])) {
    const l = out[out.length - 1];
    if (l) {
      const mid = r[1] + r[3] / 2, lm = l[1] + l[3] / 2;
      if (Math.abs(mid - lm) < 0.5 * Math.min(r[3], l[3]) && r[0] <= l[0] + l[2] + 1.5 * Math.max(r[3], l[3])) {
        const x1 = Math.max(l[0] + l[2], r[0] + r[2]), y0 = Math.min(l[1], r[1]), y1 = Math.max(l[1] + l[3], r[1] + r[3]);
        l[2] = x1 - l[0]; l[1] = y0; l[3] = y1 - y0; continue;
      }
    }
    out.push(r.slice());
  }
  return out;
}

// every non-overlapping occurrence of `needle` (already norm()ed) -> [{a,b}]
export function findAll(tm, needle) {
  const out = [];
  if (!needle) return out;
  for (let i = tm.lower.indexOf(needle); i >= 0; i = tm.lower.indexOf(needle, i + needle.length)) out.push({a: i, b: i + needle.length});
  return out;
}

// where does `text` (a quote / citation) sit on this page? -> {a,b} or null; used to re-anchor and for AI citations
export function locate(tm, text) {
  const n = norm(text); if (n.length < 3) return null;
  const hit = findAll(tm, n)[0];
  if (hit) return hit;
  const head = n.slice(0, 60), j = tm.lower.indexOf(head);   // long quotes: match the opening, extend by length
  if (j >= 0) return {a: j, b: Math.min(tm.lower.length, j + n.length)};
  // text extracted by another tool often differs only in spaces and hyphens ("rela tively", "Col- umn" for "Column"): match with those removed,
  // and for long quotes by their opening and closing words
  const cq = n.replace(/[\s\-\u2010\u2011\u00ad]+/g, "");
  if (cq.length < 12) return null;
  const c = tm._c ||= compact(tm.lower);
  const k = c.s.indexOf(cq);
  if (k >= 0) return {a: c.idx[k], b: c.idx[k + cq.length - 1] + 1};
  const h = cq.slice(0, 30), t = cq.slice(-30), kh = c.s.indexOf(h);
  if (kh >= 0 && cq.length > 70) { const kt = c.s.indexOf(t, kh + 30); if (kt >= 0 && kt + t.length - kh <= cq.length * 1.3 + 20) return {a: c.idx[kh], b: c.idx[kt + t.length - 1] + 1}; }
  return null;
}
function compact(lower) {          // the text without whitespace, and where each remaining character sits in the original
  let s = ""; const idx = [];
  for (let i = 0; i < lower.length; i++) { const ch = lower[i]; if (ch === " " || ch === "\n" || ch === "\t" || ch === "-" || ch === "\u2010" || ch === "\u2011" || ch === "\u00ad") continue; s += ch; idx.push(i); }
  return {s, idx};
}

// rects of a quoted passage on page n (fractions of the page), or null if the text isn't found there
export async function quoteRects(n, text) {
  const tm = await model(S.pages[n - 1]), loc = locate(tm, text);
  return loc ? rects(tm, loc.a, loc.b) : null;
}

export const context = (tm, a, b, n = 32) => ({prefix: tm.str.slice(Math.max(0, a - n), a), suffix: tm.str.slice(b, b + n)});

// index of the line a vertical position (fraction of page) falls in, or the nearest one
export function lineAt(tm, y) {
  let best = -1, bd = Infinity;
  tm.lines.forEach((l, i) => { const d = y >= l.y0 && y <= l.y1 ? 0 : Math.min(Math.abs(y - l.y0), Math.abs(y - l.y1)); if (d < bd) { bd = d; best = i; } });
  return best;
}

// text models of every page (collapse and search need the whole document); onProgress(done,total)
export async function all(onProgress) {
  let done = 0;
  for (const p of S.pages) { await model(p); onProgress && onProgress(++done, S.pages.length); }
}

// render part of a page (rect = [x,y,w,h] as fractions of the page) onto a new canvas about targetW pixels wide
export async function renderCrop(n, rect, targetW) {
  const p = S.pages[n - 1], base = p.proxy.getViewport({scale: 1}), scale = targetW / (rect[2] * base.width), vp = p.proxy.getViewport({scale});
  const cv = document.createElement("canvas");
  cv.width = Math.max(1, Math.round(rect[2] * vp.width)); cv.height = Math.max(1, Math.round(rect[3] * vp.height));
  await p.proxy.render({canvasContext: cv.getContext("2d"), viewport: vp, transform: [1, 0, 0, 1, -rect[0] * vp.width, -rect[1] * vp.height]}).promise;
  return cv;
}
