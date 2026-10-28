// The parts every note board shares (the reader's notes canvas and a folder's workspace): zoom and pan, arrows between cards, and labelled frames that carry the cards inside them.
// They know nothing about what a card is: a board gives them elements and the lists to draw, and is told what changed.
import {$, esc, clamp} from "./state.js";

// ---------------------------------------------------------------- zoom and pan
// scroller > sizer (its size is the content size x zoom, so the scroll bars are right) > world (scaled, origin top left; cards and frames are absolutely positioned in it)
export class Viewport {
  constructor(scroller, sizer, world, {min = 0.35, max = 2.2, onZoom = null} = {}) {
    Object.assign(this, {scroller, sizer, world, min, max, onZoom, z: 1, w: 600, h: 500});
    scroller.addEventListener("wheel", e => {
      if (!(e.ctrlKey || e.metaKey)) return;            // a plain wheel scrolls; the pinch gesture of a trackpad arrives as ctrl + wheel
      e.preventDefault(); this.zoomTo(this.z * Math.exp(-e.deltaY * 0.0025), e.clientX, e.clientY);
    }, {passive: false});
    scroller.addEventListener("pointerdown", e => this.panStart(e));
  }
  resize(w, h) { this.w = w; this.h = h; this.sizer.style.width = Math.round(w * this.z) + "px"; this.sizer.style.height = Math.round(h * this.z) + "px"; }
  zoomTo(z, cx, cy) {                                    // keep the point under the pointer where it is
    z = clamp(+z.toFixed(3), this.min, this.max); if (z === this.z) return;
    const r = this.scroller.getBoundingClientRect(), px = (cx ?? r.left + r.width / 2) - r.left, py = (cy ?? r.top + r.height / 2) - r.top;
    const wx = (this.scroller.scrollLeft + px) / this.z, wy = (this.scroller.scrollTop + py) / this.z;
    this.z = z; this.world.style.transform = `scale(${z})`; this.resize(this.w, this.h);
    this.scroller.scrollLeft = wx * z - px; this.scroller.scrollTop = wy * z - py; this.onZoom && this.onZoom(z);
  }
  fit() { const r = this.scroller.getBoundingClientRect(); this.zoomTo(Math.min(1, (r.width - 24) / Math.max(this.w, 1), (r.height - 24) / Math.max(this.h, 1)), r.left, r.top); this.scroller.scrollLeft = 0; this.scroller.scrollTop = 0; }
  toWorld(cx, cy) { const r = this.world.getBoundingClientRect(); return {x: (cx - r.left) / this.z, y: (cy - r.top) / this.z}; }
  panStart(e) {                                          // drag the empty background (or the middle button anywhere) to move around
    const bg = e.target === this.scroller || e.target === this.sizer || e.target === this.world;
    if (!(e.button === 1 || (e.button === 0 && bg))) return;
    const sx = e.clientX, sy = e.clientY, l0 = this.scroller.scrollLeft, t0 = this.scroller.scrollTop; let moved = false;
    const move = ev => { if (!moved && Math.hypot(ev.clientX - sx, ev.clientY - sy) < 4) return; moved = true; this.scroller.classList.add("panning"); this.scroller.scrollLeft = l0 - (ev.clientX - sx); this.scroller.scrollTop = t0 - (ev.clientY - sy); };
    const up = () => { removeEventListener("pointermove", move); removeEventListener("pointerup", up); this.scroller.classList.remove("panning"); };
    addEventListener("pointermove", move); addEventListener("pointerup", up);
    if (e.button === 1) e.preventDefault();
  }
}

// ---------------------------------------------------------------- arrows between cards
// links: [{id, a, b, label}] (a and b are card ids). The board owns the list and tells us what to change; we only draw and ask.
export class Links {
  constructor({world, elOf, list, onAdd, onLabel, onRemove, vp}) {
    Object.assign(this, {world, elOf, list, onAdd, onLabel, onRemove, vp, from: null});
    this.svg = document.createElementNS("http://www.w3.org/2000/svg", "svg"); this.svg.setAttribute("class", "bd-links");
    this.svg.innerHTML = `<defs><marker id="bd-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" class="bd-ah"/></marker></defs><g class="bd-lg"></g>`;
    world.prepend(this.svg);
    this.pop = document.createElement("div"); this.pop.className = "bd-pop hide"; world.append(this.pop);
    this.svg.addEventListener("click", e => { const p = e.target.closest("[data-lk]"); if (p) this.edit(p.dataset.lk, e); });
    this.pop.addEventListener("click", e => { const b = e.target.closest("[data-bd]"); if (!b) return; const id = this.pop.dataset.id; if (b.dataset.bd === "del") { this.pop.classList.add("hide"); this.onRemove(id); } else if (b.dataset.bd === "ok") this.commit(); });
    this.pop.addEventListener("keydown", e => { if (e.key === "Enter") this.commit(); else if (e.key === "Escape") this.pop.classList.add("hide"); });
    document.addEventListener("pointerdown", e => { if (!this.pop.contains(e.target) && !e.target.closest("[data-lk]")) this.pop.classList.add("hide"); });
  }
  anchor(r, tx, ty) {                                    // the middle of the side of a card that faces the other card
    const cx = r.x + r.w / 2, cy = r.y + r.h / 2, dx = tx - cx, dy = ty - cy;
    return Math.abs(dx) * r.h > Math.abs(dy) * r.w ? {x: dx > 0 ? r.x + r.w : r.x, y: cy, nx: dx > 0 ? 1 : -1, ny: 0} : {x: cx, y: dy > 0 ? r.y + r.h : r.y, nx: 0, ny: dy > 0 ? 1 : -1};
  }
  rect(el) { return {x: el.offsetLeft, y: el.offsetTop, w: el.offsetWidth, h: el.offsetHeight}; }
  draw() {
    const g = this.svg.querySelector(".bd-lg"), out = [];
    for (const l of this.list()) {
      const ea = this.elOf(l.a), eb = this.elOf(l.b); if (!ea || !eb) continue;
      const ra = this.rect(ea), rb = this.rect(eb), a = this.anchor(ra, rb.x + rb.w / 2, rb.y + rb.h / 2), b = this.anchor(rb, ra.x + ra.w / 2, ra.y + ra.h / 2), k = Math.max(40, Math.hypot(b.x - a.x, b.y - a.y) / 3);
      const d = `M${a.x} ${a.y} C${a.x + a.nx * k} ${a.y + a.ny * k} ${b.x + b.nx * k} ${b.y + b.ny * k} ${b.x} ${b.y}`, mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
      out.push(`<g class="bd-l" data-lk="${esc(l.id)}"><path class="bd-hit" d="${d}"/><path class="bd-line" d="${d}" marker-end="url(#bd-arrow)"/>${l.label ? `<text x="${mx}" y="${my - 5}" class="bd-lt" text-anchor="middle">${esc(l.label)}</text>` : ""}</g>`);
    }
    g.innerHTML = out.join("");
  }
  edit(id, e) {
    const l = this.list().find(x => x.id === id); if (!l) return; const p = this.vp.toWorld(e.clientX, e.clientY);
    this.pop.dataset.id = id; this.pop.innerHTML = `<input type="text" maxlength="60" placeholder="Label this arrow…" value="${esc(l.label || "")}"><button data-bd="ok" title="Save the label">✓</button><button data-bd="del" title="Remove the arrow">✕</button>`;
    this.pop.style.left = Math.max(0, p.x - 60) + "px"; this.pop.style.top = Math.max(0, p.y + 8) + "px"; this.pop.classList.remove("hide"); $("input", this.pop).focus();
  }
  commit() { const id = this.pop.dataset.id; this.pop.classList.add("hide"); this.onLabel(id, $("input", this.pop).value.trim()); }
  // "connect" mode: the next card you click becomes the other end
  start(fromId, hint) {
    this.from = fromId; this.world.classList.add("connecting"); hint && hint(true);
    const stop = () => { this.from = null; this.world.classList.remove("connecting"); hint && hint(false); removeEventListener("keydown", key, true); removeEventListener("pointerdown", pick, true); };
    const key = e => { if (e.key === "Escape") stop(); };
    const pick = e => { const c = e.target.closest("[data-card]"); if (c && c.dataset.card !== fromId) { e.preventDefault(); e.stopPropagation(); this.onAdd(fromId, c.dataset.card); } stop(); };
    addEventListener("keydown", key, true); addEventListener("pointerdown", pick, true);
  }
}

// ---------------------------------------------------------------- frames
// frames: [{id, x, y, w, h, label, color}]; moving a frame moves the cards whose middle is inside it
export const FRAME_COLORS = ["#5b7fa6", "#6a9a62", "#c0883a", "#a8607a", "#7f6ab0"];
export class Frames {
  constructor({world, vp, list, cards, onMove, onUpdate, onRemove}) { Object.assign(this, {world, vp, list, cards, onMove, onUpdate, onRemove}); this.els = new Map(); }
  render() {
    this.els.forEach(e => e.remove()); this.els.clear();
    for (const f of this.list()) {
      const el = document.createElement("div"); el.className = "bd-frame"; el.dataset.frame = f.id; el.style.cssText = `left:${f.x}px;top:${f.y}px;width:${f.w}px;height:${f.h}px;--fc:${f.color || FRAME_COLORS[0]}`;
      el.innerHTML = `<div class="bd-fh" data-fdrag><span class="bd-fm" title="Drag to move the frame and the cards inside it">⠿</span><input class="bd-fl" type="text" maxlength="80" placeholder="Name this group…" value="${esc(f.label || "")}"><button data-fc="color" title="Change the colour">●</button><button data-fc="del" title="Remove the frame (the cards stay)">✕</button></div><div class="bd-fg" title="Drag to resize"></div>`;
      this.world.insertBefore(el, this.world.firstChild.nextSibling); this.els.set(f.id, el); this.wire(el, f.id);
    }
  }
  inside(f, c) { const cx = c.el.offsetLeft + c.el.offsetWidth / 2, cy = c.el.offsetTop + c.el.offsetHeight / 2; return cx >= f.x && cx <= f.x + f.w && cy >= f.y && cy <= f.y + f.h; }
  wire(el, id) {
    const frame = () => this.list().find(x => x.id === id);
    el.addEventListener("change", e => { if (e.target.matches(".bd-fl")) this.onUpdate(id, {label: e.target.value.trim()}, true); });
    el.addEventListener("click", e => {
      const b = e.target.closest("[data-fc]"); if (!b) return;
      if (b.dataset.fc === "del") this.onRemove(id);
      else { const f = frame(), i = FRAME_COLORS.indexOf(f.color || FRAME_COLORS[0]); this.onUpdate(id, {color: FRAME_COLORS[(i + 1) % FRAME_COLORS.length]}); }
    });
    $("[data-fdrag]", el).addEventListener("pointerdown", e => {
      if (e.target.closest("button, input") || e.button !== 0) return; const f = frame(), z = this.vp.z, sx = e.clientX, sy = e.clientY, inner = this.cards().filter(c => this.inside(f, c)).map(c => ({c, x: c.el.offsetLeft, y: c.el.offsetTop}));
      const box = e.currentTarget; box.setPointerCapture(e.pointerId); let dx = 0, dy = 0;
      const move = ev => { dx = Math.max(-f.x, (ev.clientX - sx) / z); dy = Math.max(-f.y, (ev.clientY - sy) / z); el.style.left = f.x + dx + "px"; el.style.top = f.y + dy + "px"; inner.forEach(o => { o.c.el.style.left = o.x + dx + "px"; o.c.el.style.top = o.y + dy + "px"; }); this.onMove && this.onMove(); };
      const up = () => { box.releasePointerCapture(e.pointerId); box.removeEventListener("pointermove", move); box.removeEventListener("pointerup", up); if (!dx && !dy) return;
        this.onUpdate(id, {x: Math.round(f.x + dx), y: Math.round(f.y + dy)}, true, inner.map(o => ({id: o.c.id, x: Math.round(o.x + dx), y: Math.round(o.y + dy)}))); };
      box.addEventListener("pointermove", move); box.addEventListener("pointerup", up);
    });
    $(".bd-fg", el).addEventListener("pointerdown", e => {
      e.preventDefault(); e.stopPropagation(); const f = frame(), z = this.vp.z, sx = e.clientX, sy = e.clientY, g = e.currentTarget; g.setPointerCapture(e.pointerId); let w = f.w, h = f.h;
      const move = ev => { w = clamp(f.w + (ev.clientX - sx) / z, 160, 3000); h = clamp(f.h + (ev.clientY - sy) / z, 100, 3000); el.style.width = w + "px"; el.style.height = h + "px"; };
      const up = () => { g.removeEventListener("pointermove", move); g.removeEventListener("pointerup", up); this.onUpdate(id, {w: Math.round(w), h: Math.round(h)}, true); };
      g.addEventListener("pointermove", move); g.addEventListener("pointerup", up);
    });
  }
}
