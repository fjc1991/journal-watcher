// The one structure every AI output shares, so outputs are easy to scan and easy to tell apart:
//   header      the kind of output (icon + label in the tool's colour), a title, who wrote it and what it cost, Copy / → Notes / Redo
//   jump bar    one button per section with its count; click to go there; the section you are in is lit
//   sections    titled, iconed, collapsible boxes ("Short answer", "Evidence", "Not in the passages"…); fields sit in sunken wells
// Outputs build their HTML with frame()/section()/kv()/callout()/pageChips(), and call wire(container) once; model Markdown is split into
// sections with mdSections()/mdFrame(). Collapsed sections are remembered per output while the page is open (frame key).
import {S, esc, ico, md} from "./state.js";

const COLL = new Map();                           // output key -> Map(section id -> collapsed?)
const isCol = (key, id, dflt) => { const m = key && COLL.get(key); return m && m.has(id) ? m.get(id) : !!dflt; };
function setCol(key, id, col) { if (!key) return; if (!COLL.has(key)) COLL.set(key, new Map()); COLL.get(key).set(id, col); }

const ICONS = [[/short answer|tl;?dr|answer|overview|in brief|summary/i, "sparkle"], [/not in the passages|limit|caveat|risk|weakness|gap|missing/i, "warn"], [/evidence|support|result|finding|data/i, "evidence"],
  [/method|design|approach|sample|analysis/i, "matrix"], [/contribution|implication|so what|takeaway|insight/i, "bolt"], [/theor|concept|construct|framework/i, "ideas"],
  [/question|problem|motivation|background|purpose/i, "ask"], [/future|next|follow|open/i, "discover"], [/hypothes|proposition/i, "check"], [/source|passage|reference/i, "link"]];
export const secIcon = t => (ICONS.find(([re]) => re.test(t)) || [0, "papers"])[1];

// ---------------------------------------------------------------- building blocks
export function section({id, title, icon, count, body, collapsed, tone = "", key = "", actions = "", note = ""}) {
  const col = isCol(key, id, collapsed);
  return `<section class="of-sec ${tone}${col ? " col" : ""}" data-sec="${esc(id)}">
    <header class="of-sh" data-of-t="${esc(id)}" role="button" tabindex="0" aria-expanded="${!col}"><span class="of-ch">${ico("down", "sm")}</span><span class="of-si">${ico(icon || secIcon(title), "sm")}</span><h4>${esc(title)}</h4>${count != null && count !== "" ? `<span class="of-n">${esc(count)}</span>` : ""}${note ? `<span class="of-note">${note}</span>` : ""}<span class="of-sa">${actions}<button class="of-mini" data-of-copy="${esc(id)}" title="Copy this section" aria-label="Copy this section">${ico("copy", "sm")}</button></span></header>
    <div class="of-sb">${body}</div></section>`;
}
export const callout = (kind, html, title = "") => `<div class="of-co ${kind}">${ico(kind === "warn" ? "warn" : kind === "ok" ? "check" : "info", "sm")}<div>${title ? `<b>${esc(title)}</b> ` : ""}${html}</div></div>`;
export const kv = rows => `<dl class="of-kv">${rows.filter(r => r && r[1]).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("")}</dl>`;
export const chipList = (xs, cls = "") => `<div class="of-chips ${cls}">${xs.join("")}</div>`;
export const badge = (text, cls = "") => `<span class="of-badge ${cls}">${esc(text)}</span>`;
// page chips with one chip per page (several passages on one page are one chip); ns = passage numbers, P = {n: {page, quote}}
export function pageChips(ns, P, cls = "cd-cite") {
  const seen = new Map();
  (ns || []).forEach(n => { const p = P && P[n]; if (!p) return; if (!seen.has(p.page)) seen.set(p.page, {n, quotes: []}); seen.get(p.page).quotes.push(p.quote); });
  return [...seen.entries()].sort((a, b) => a[0] - b[0]).map(([pg, v]) => `<button class="${cls}" data-n="${v.n}" title="${esc(v.quotes[0].slice(0, 160))}…${v.quotes.length > 1 ? " (+" + (v.quotes.length - 1) + " more on this page)" : ""}">p. ${pg}</button>`).join("");
}

// the whole output: header, optional meta chips, jump bar, optional notices, sections
export function frame({tool, kind, icon, title = "", sub = "", meta = [], actions = "", sections, notices = "", foot = "", key = "", jump = true, cls = ""}) {
  const secs = sections.filter(Boolean);
  return `<article class="of ${cls}" data-tool="${tool}" data-of="${esc(key)}">
    <header class="of-head"><span class="of-ico">${ico(icon || tool)}</span><div class="of-hm"><div class="eyebrow t">${esc(kind)}</div>${title ? `<div class="of-title">${esc(title)}</div>` : ""}${sub ? `<div class="of-sub">${sub}</div>` : ""}</div><div class="of-act">${actions}</div></header>
    ${meta.length ? `<div class="of-meta">${meta.filter(Boolean).map(m => `<span class="of-m ${m.cls || ""}"${m.title ? ` title="${esc(m.title)}"` : ""}>${m.html || esc(m.t)}</span>`).join("")}</div>` : ""}
    ${jump && secs.length >= 3 ? `<nav class="of-jump" aria-label="Sections">${secs.map(s => `<button data-of-j="${esc(s.id)}"${s.count === 0 ? ' class="zero"' : ""}>${esc(s.short || s.title)}${s.count ? `<span class="n">${esc(s.count)}</span>` : ""}</button>`).join("")}<button class="of-all" data-of-all title="Expand or collapse every section" aria-label="Expand or collapse all">${ico("filter", "sm")}</button></nav>` : ""}
    ${notices ? `<div class="of-notices">${notices}</div>` : ""}
    <div class="of-body">${secs.map(s => section({...s, key})).join("")}</div>${foot ? `<footer class="of-foot">${foot}</footer>` : ""}</article>`;
}

// ---------------------------------------------------------------- Markdown from the model -> sections
// "## Heading" lines start a section; text before the first heading is the lead. Deeper headings ("### …") stay inside their section, so an outline
// with ## parts and ### steps becomes a few sections, not dozens. Only when the model used no # headings at all do lines that are just **Bold** count as headings.
export function mdSections(text) {
  const lines = String(text || "").replace(/\r/g, "").split("\n"); let fence = false;
  const info = lines.map(l => {
    if (/^```/.test(l)) { fence = !fence; return null; }
    if (fence) return null;
    let m = /^(#{1,4})\s+(.+?)\s*#*\s*$/.exec(l); if (m) return {level: m[1].length, title: m[2]};
    m = /^\s*\*\*([^*]{2,80}?)\*\*\s*:?\s*$/.exec(l) || /^\s*__([^_]{2,80})__\s*:?\s*$/.exec(l); return m ? {level: 9, title: m[1]} : null;
  });
  const hashes = info.filter(h => h && h.level < 9).map(h => h.level), base = hashes.length ? (hashes.includes(2) ? 2 : Math.min(...hashes)) : 9;
  const out = {lead: [], sections: []}; let cur = null;
  lines.forEach((l, i) => {
    const h = info[i];
    if (h && h.level <= base) { cur = {title: h.title.replace(/[*_`#]/g, "").replace(/:\s*$/, "").trim(), body: []}; out.sections.push(cur); }
    else (cur ? cur.body : out.lead).push(l);
  });
  out.lead = out.lead.join("\n").trim();
  out.sections = out.sections.map(s => ({title: s.title, body: s.body.join("\n").trim()})).filter(s => s.title);
  return out;
}
const slug = (t, i) => (String(t).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "s") + "-" + i;
// Markdown -> frame sections. render(md) turns a part into HTML (default: plain Markdown; Ask passes one that makes citation buttons).
// leadTitle names the text before the first heading (e.g. "Short answer"); warnRe marks sections that are caveats.
export function mdParts(text, {render = md, leadTitle = "", warnRe = /not in the passages|caveat|limit|missing|cannot be determined/i, collapseRe = null, leadRe = /^(tl;?dr|short answer|in brief|key takeaway)$/i} = {}) {
  const p = mdSections(text), parts = [];
  if (p.lead) parts.push({id: "lead", title: leadTitle || "Overview", short: leadTitle || "Overview", icon: secIcon(leadTitle || "Overview"), tone: leadTitle ? "lead" : "", body: `<div class="of-md">${render(p.lead)}</div>`});
  p.sections.forEach((s, i) => parts.push({id: slug(s.title, i), title: s.title, icon: secIcon(s.title), tone: leadRe && leadRe.test(s.title) ? "lead" : warnRe && warnRe.test(s.title) ? "warn" : "", collapsed: collapseRe ? collapseRe.test(s.title) : false, body: `<div class="of-md">${s.body ? render(s.body) : `<i class="of-none">Nothing here.</i>`}</div>`}));
  return parts;
}

// just the sections (for small cards that have their own header)
export const sectionsHtml = (parts, key = "") => `<div class="of-body">${parts.map(s => section({...s, key})).join("")}</div>`;

// ---------------------------------------------------------------- behaviour (call once on a container that holds outputs)
export function wire(root) {
  if (root._of) return; root._of = true;
  const scroller = () => { let e = root; while (e && e !== document.body) { const o = getComputedStyle(e).overflowY; if ((o === "auto" || o === "scroll") && e.scrollHeight > e.clientHeight + 4) return e; e = e.parentElement; } return window; };
  const toggle = (sec, force) => { const col = force != null ? force : !sec.classList.contains("col"); sec.classList.toggle("col", col); sec.querySelector(".of-sh").setAttribute("aria-expanded", String(!col)); setCol(sec.closest("[data-of]")?.dataset.of, sec.dataset.sec, col); };
  root.addEventListener("click", e => {
    const t = e.target, art = t.closest(".of"); if (!art) return;
    const cp = t.closest("[data-of-copy]");
    if (cp) { e.stopPropagation(); const s = cp.closest(".of-sec"); const txt = s.querySelector("h4").textContent + "\n" + s.querySelector(".of-sb").innerText.trim(); if (S.fn.copyText) S.fn.copyText(txt, "Section copied."); else if (navigator.clipboard) navigator.clipboard.writeText(txt); return; }
    const j = t.closest("[data-of-j]");
    if (j) { const s = art.querySelector(`[data-sec="${CSS.escape(j.dataset.ofJ)}"]`); if (s) { toggle(s, false); const sc = scroller(), jb = art.querySelector(".of-jump"), off = (jb ? jb.offsetHeight : 0) + 6;
      if (sc === window) window.scrollTo({top: window.scrollY + s.getBoundingClientRect().top - off - 70, behavior: "smooth"}); else sc.scrollTo({top: sc.scrollTop + s.getBoundingClientRect().top - sc.getBoundingClientRect().top - off, behavior: "smooth"}); } return; }
    if (t.closest("[data-of-all]")) { const secs = [...art.querySelectorAll(".of-sec")], anyOpen = secs.some(s => !s.classList.contains("col")); secs.forEach(s => toggle(s, anyOpen)); return; }
    const h = t.closest("[data-of-t]"); if (h && !t.closest(".of-sa, button, a, select, input, textarea")) toggle(h.closest(".of-sec"));
  });
  root.addEventListener("keydown", e => { const h = e.target.closest && e.target.closest("[data-of-t]"); if (h && (e.key === "Enter" || e.key === " ") && e.target === h) { e.preventDefault(); toggle(h.closest(".of-sec")); } });
  // the jump bar lights the section you are reading
  let raf = 0; const spy = () => { raf = 0; root.querySelectorAll(".of").forEach(art => {
    const jb = art.querySelector(".of-jump"); if (!jb) return; const top = (jb.getBoundingClientRect().bottom) + 8; let cur = null;
    art.querySelectorAll(".of-sec").forEach(s => { if (s.getBoundingClientRect().top <= top) cur = s.dataset.sec; });
    jb.querySelectorAll("button[data-of-j]").forEach(b => { const on = b.dataset.ofJ === cur; if (b.classList.contains("on") !== on) { b.classList.toggle("on", on); if (on) { const l = b.offsetLeft - jb.clientWidth / 2 + b.offsetWidth / 2; jb.scrollTo({left: Math.max(0, l)}); } } }); }); };
  const hook = () => { const sc = scroller(); if (root._sc === sc) return; root._sc = sc; (sc === window ? window : sc).addEventListener("scroll", () => { if (!raf) raf = requestAnimationFrame(spy); }, {passive: true}); };
  new MutationObserver(hook).observe(root, {childList: true}); hook();
}
