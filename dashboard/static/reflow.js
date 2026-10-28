// Text view (R6): the paper as flowing text instead of page images: a larger, calmer way to read on a small window, with a font size and a line width you choose, your highlights marked,
// the page of each paragraph shown (click it to open that page of the PDF), and references hidden unless you ask. Built from the same paragraph blocks the AI features read (GET /api/doc/blocks).
// It is also a text a screen reader can read, which the PDF page images are not.
import {S, $, $$, esc, api, toast, notifyError, colorOf} from "./state.js";

let wrap = null, on = false, blocks = null, loadedFor = null, prefs = {size: 18, width: 680, refs: false}, btn = null;
const norm = s => String(s || "").replace(/\s+/g, " ").trim().toLowerCase();
try { Object.assign(prefs, JSON.parse(localStorage.getItem("jw-reflow") || "{}")); } catch (e) { /* defaults */ }
const savePrefs = () => { try { localStorage.setItem("jw-reflow", JSON.stringify(prefs)); } catch (e) { /* fine */ } };

export function init() {
  const host = $(".rd-pdfwrap", S.ui.root);
  wrap = document.createElement("div"); wrap.className = "rd-reflow hide"; wrap.id = "rd-reflow"; wrap.setAttribute("role", "document"); wrap.setAttribute("aria-label", "The paper as text");
  host.append(wrap); btn = $('[data-a="reflow"]', S.ui.root);
  wrap.addEventListener("click", onClick); wrap.addEventListener("change", onChange);
  S.fn.reflow = toggle; S.fn.reflowOn = () => on;
  addEventListener("jw-reader-closed", () => { on = false; blocks = null; loadedFor = null; wrap.classList.add("hide"); });
}
async function toggle() {
  if (on) return leave();
  const pos = S.fn.currentPos ? S.fn.currentPos().n : 1;
  try { if (loadedFor !== S.file) { blocks = (await api("/api/doc/blocks?file=" + encodeURIComponent(S.file))).blocks; loadedFor = S.file; } } catch (e) { return notifyError(e); }
  on = true; draw(); wrap.classList.remove("hide"); S.ui.pane.classList.add("hide"); $("#rd-ticks", S.ui.root)?.classList.add("hide"); btn && btn.classList.add("on");
  const p = $(`.rf-p[data-page="${pos}"]`, wrap) || $(".rf-p", wrap); if (p) wrap.scrollTop = p.offsetTop - 70;
}
function leave(page) {
  const top = [...$$(".rf-p", wrap)].find(p => p.offsetTop + p.offsetHeight > wrap.scrollTop + 60), n = page || (top ? +top.dataset.page : 1);
  on = false; wrap.classList.add("hide"); S.ui.pane.classList.remove("hide"); $("#rd-ticks", S.ui.root)?.classList.remove("hide"); btn && btn.classList.remove("on");
  setTimeout(() => S.fn.jump(n, 0), 30);
}
function marked(text, page) {                      // your highlights on this page, drawn into the paragraph where their words are found
  const src = String(text).replace(/\s+/g, " ").trim(), low = src.toLowerCase(), spans = [];
  S.notes.highlights.filter(h => (h.text || "").trim().length > 8 && (h.kind || "highlight") !== "area" && h.parts.some(p => p.page === page)).forEach(h => { const w = norm(h.text), i = low.indexOf(w); if (i >= 0) spans.push([i, i + w.length, h.color]); });
  if (!spans.length) return esc(src);
  spans.sort((a, b) => a[0] - b[0]); let out = "", at = 0;
  spans.forEach(([a, b, c]) => { if (a < at) return; out += esc(src.slice(at, a)) + `<mark class="rf-hl" style="--c:${esc(colorOf(c))}">${esc(src.slice(a, b))}</mark>`; at = b; });
  return out + esc(src.slice(at));
}
function draw() {
  const list = blocks.filter(b => b.kind !== "meta" && (prefs.refs || b.kind !== "ref")); let page = 0, refs = blocks.filter(b => b.kind === "ref").length;
  wrap.style.setProperty("--rf-size", prefs.size + "px"); wrap.style.setProperty("--rf-width", prefs.width + "px");
  wrap.innerHTML = `<div class="rf-bar"><button class="rd-btn" data-rf="back" title="Back to the pages (the PDF)">← PDF pages</button><span class="sep"></span><button class="rd-btn" data-rf="smaller" aria-label="Smaller text">A−</button><button class="rd-btn" data-rf="bigger" aria-label="Larger text">A+</button>
      <label>Line width <select id="rf-w">${[[520, "Narrow"], [680, "Medium"], [860, "Wide"]].map(([v, t]) => `<option value="${v}"${prefs.width === v ? " selected" : ""}>${t}</option>`).join("")}</select></label>
      ${refs ? `<label class="chk"><input type="checkbox" id="rf-refs"${prefs.refs ? " checked" : ""}> references</label>` : ""}<span class="pmeta">${list.length} paragraphs · highlights are marked · click a page number to open that page</span></div>
    <article class="rf-doc">${list.map(b => { const pg = b.page + 1, head = pg !== page ? `<div class="rf-pg">Page ${pg}</div>` : ""; page = pg; return head + `<p class="rf-p${b.kind === "ref" ? " ref" : ""}" data-page="${pg}" data-y="${b.y}"><button class="rf-n" data-rf="page" data-pg="${pg}" data-y="${b.y}" title="Open page ${pg}">p. ${pg}</button>${marked(b.text, pg)}</p>`; }).join("")}</article>`;
}
function onClick(e) {
  const b = e.target.closest("[data-rf]"); if (!b) return; const a = b.dataset.rf;
  if (a === "back") return leave();
  if (a === "page") { on = false; wrap.classList.add("hide"); S.ui.pane.classList.remove("hide"); $("#rd-ticks", S.ui.root)?.classList.remove("hide"); btn && btn.classList.remove("on"); const pg = +b.dataset.pg, y = +b.dataset.y || 0; return void setTimeout(() => S.fn.jump(pg, Math.max(0, y - 0.02)), 30); }
  if (a === "smaller" || a === "bigger") { prefs.size = Math.max(13, Math.min(30, prefs.size + (a === "bigger" ? 2 : -2))); savePrefs(); wrap.style.setProperty("--rf-size", prefs.size + "px"); }
}
function onChange(e) {
  if (e.target.id === "rf-w") { prefs.width = +e.target.value; savePrefs(); wrap.style.setProperty("--rf-width", prefs.width + "px"); }
  if (e.target.id === "rf-refs") { prefs.refs = e.target.checked; savePrefs(); const top = wrap.scrollTop; draw(); wrap.scrollTop = top; }
}
