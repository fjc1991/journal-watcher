// Area tool (▭ Area in the toolbar): drag a box around a figure or a table. Then copy it to the notes as an image, mark it,
// copy the picture, or ask AI to explain it. The notes keep only the page and the box, and redraw the picture from the PDF.
import {S, $, esc, toast, clamp, COLORS, colorName} from "./state.js";
import * as N from "./notes.js";
import * as T from "./text.js";
import * as C from "./canvas.js";

let box, menu, drag = null, sel = null;

export function init() {
  const {root, pane} = S.ui;
  box = document.createElement("div"); box.className = "rd-areabox hide"; root.append(box);
  menu = document.createElement("div"); menu.className = "rd-float col hide rd-areamenu"; root.append(menu);
  pane.addEventListener("pointerdown", down); addEventListener("pointermove", move); addEventListener("pointerup", up);
  menu.addEventListener("mousedown", e => e.preventDefault()); menu.addEventListener("click", onMenu);
  document.addEventListener("keydown", e => { if (e.key === "Escape") cancel(); });
}
const cancel = () => { box?.classList.add("hide"); menu?.classList.add("hide"); sel = null; drag = null; };
function down(e) {
  if (S.tool !== "area" || e.button !== 0) return;
  const b = e.target.closest(".rd-band")?._b; if (!b) return;
  e.preventDefault(); menu.classList.add("hide");
  drag = {b, bb: b.el.getBoundingClientRect(), x0: e.clientX, y0: e.clientY, x1: e.clientX, y1: e.clientY}; box.classList.remove("hide"); draw();
}
function move(e) { if (!drag) return; drag.x1 = clamp(e.clientX, drag.bb.left, drag.bb.right); drag.y1 = clamp(e.clientY, drag.bb.top, drag.bb.bottom); draw(); }
function draw() { const d = drag; Object.assign(box.style, {left: Math.min(d.x0, d.x1) + "px", top: Math.min(d.y0, d.y1) + "px", width: Math.abs(d.x1 - d.x0) + "px", height: Math.abs(d.y1 - d.y0) + "px"}); }
function up(e) {
  if (!drag) return;
  const d = drag, w = Math.abs(d.x1 - d.x0), h = Math.abs(d.y1 - d.y0); drag = null;
  if (w < 14 || h < 14) { box.classList.add("hide"); return; }
  const {b, bb} = d, span = b.y1 - b.y0;
  sel = {page: b.p.n, rect: [(Math.min(d.x0, d.x1) - bb.left) / bb.width, b.y0 + (Math.min(d.y0, d.y1) - bb.top) / bb.height * span, w / bb.width, h / bb.height * span].map(v => +v.toFixed(4))};
  menu.innerHTML = `<div class="pkind">Figure or table · p. ${sel.page}</div><button class="rd-btn" data-m="notes">→ Notes (as a picture)</button><button class="rd-btn" data-m="explain">✨ Explain it…</button>
    <div class="dots" title="Mark the area in a colour">${COLORS.map(c => `<span class="rd-dot" data-c="${c.id}" style="--c:${c.c}" title="Mark in ${esc(colorName(c.id))}"></span>`).join("")}</div>
    <div class="acts"><button class="rd-btn" data-m="copy">Copy picture</button><button class="rd-btn" data-m="x">Cancel</button></div>`;
  menu.classList.remove("hide"); menu.style.left = clamp(e.clientX, 8, innerWidth - 290) + "px"; menu.style.top = clamp(e.clientY + 8, 70, innerHeight - 220) + "px";
}
async function onMenu(e) {
  const m = e.target.closest("[data-m]")?.dataset.m, dot = e.target.closest("[data-c]"); if (!sel) return;
  const s = sel, part = {page: s.page, rects: [s.rect]};
  if (dot) { N.addHighlight({parts: [part], text: "", quote: {}}, dot.dataset.c, "area"); S.lastColor = dot.dataset.c; S.fn.setTool(null); return cancel(); }
  if (m === "notes") { const p = C.nextPos(); N.addCard({type: "image", text: "", note: "", src: {hl: null, page: s.page, rect: s.rect, parts: [part]}, w: 340, x: p.x, y: p.y}); S.fn.showTab("notes"); S.fn.setTool(null); return cancel(); }
  if (m === "explain") { const anchor = e.target.closest("[data-m]"); S.fn.setTool(null); menu.classList.add("hide"); box.classList.add("hide"); if (S.fn.explainMenu) S.fn.explainMenu(anchor, {area: s}); return; }
  if (m === "copy") {
    try { const cv = await T.renderCrop(s.page, s.rect, 1200); const blob = await new Promise(r => cv.toBlob(r, "image/png")); await navigator.clipboard.write([new ClipboardItem({"image/png": blob})]); toast("Picture copied."); }
    catch (er) { toast("Couldn’t copy the picture: " + er.message); }
    return cancel();
  }
  if (m === "x") { S.fn.setTool(null); cancel(); }
}
// a PNG of the area for the AI (base64, no prefix)
export async function areaPng(page, rect) {
  const cv = await T.renderCrop(page, rect, 1400);
  return cv.toDataURL("image/png").split(",")[1];
}
