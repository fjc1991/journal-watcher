// A paper's annotations: load, change, autosave to <paper>.notes.json (via /api/doc/notes) and the shared tag list.
// Highlight: {id, color, parts:[{page, rects:[[x,y,w,h]...]}], text, quote:{exact,prefix,suffix}, tags:[], comment, created}
//   rects are fractions of the page (0-1, top-left origin), so they survive any zoom.
// Card (notes canvas): {id, type:"excerpt"|"note"|"ai", x, y, w, text, note, src:{hl|page,rects,quote}, tags:[], cites, created}
import {S, api, uid, debounce, emit, toast} from "./state.js";

export async function load(file) {
  U.undo = []; U.redo = []; U.kind = null;
  const d = await api("/api/doc?file=" + encodeURIComponent(file));
  S.meta = {...(S.meta || {}), ...d.meta};
  S.notes = d.notes; S.rev = d.notes.rev || 0;
  S.notes.frames = S.notes.frames || []; S.notes.links = S.notes.links || []; S.notes.paper_note = S.notes.paper_note || "";
  S.tags = d.tags.tags; S.tagsRev = d.tags.rev || 0;
  S.save = "saved";
  return d;
}

// ---------------------------------------------------------------- autosave
let busy = false, again = false;
const later = debounce(() => saveNow(), 800);

function mark() {
  S.save = "dirty"; emit("save:state"); later();
}
export async function saveNow() {
  if (!S.notes || S.save === "conflict" || S.save === "saved") return;   // nothing changed: never create a sidecar for a paper you only read
  if (busy) { again = true; return; }
  busy = true; S.save = "saving"; emit("save:state");
  try {
    const r = await api("/api/doc/notes", {file: S.file, rev: S.rev, notes: S.notes});
    S.rev = r.rev; S.notes.rev = r.rev; S.save = again ? "dirty" : "saved";
  } catch (e) {
    if (e.status === 409) { S.save = "conflict"; S.conflict = e.body.current; }
    else { S.save = "error"; toast("Couldn’t save notes: " + e.message); }
  }
  busy = false; emit("save:state");
  if (again) { again = false; saveNow(); }
}
// push pending changes when the tab is hidden/closed (fetch keepalive survives unload)
addEventListener("pagehide", () => { if (S.save === "dirty" && S.notes) fetch("/api/doc/notes", {method: "POST", keepalive: true,
  headers: {"Content-Type": "application/json"}, body: JSON.stringify({file: S.file, rev: S.rev, notes: S.notes})}); });

export function takeConflict() {            // user chose to reload the other window's copy
  S.notes = S.conflict; S.rev = S.notes.rev || 0; S.save = "saved"; S.conflict = null;
  emit("notes:changed", {all: true}); emit("save:state");
}

// ---------------------------------------------------------------- undo / redo (snapshots of highlights, cards, bookmarks)
const U = {undo: [], redo: [], kind: null, t: 0, batch: false};
const copy = () => JSON.stringify({highlights: S.notes.highlights, cards: S.notes.cards, bookmarks: S.notes.bookmarks, frames: S.notes.frames || [], links: S.notes.links || [], paper_note: S.notes.paper_note || ""});
export function snap(kind = "edit") {          // call BEFORE changing anything; edits of the same kind within 1.5 s (typing) share one step
  if (U.batch || !S.notes) return;
  const now = Date.now();
  if (/^(card|hl|move|tag):/.test(kind) && kind === U.kind && now - U.t < 1500 && U.undo.length) { U.t = now; return; }
  U.undo.push(copy()); if (U.undo.length > 100) U.undo.shift();
  U.redo = []; U.kind = kind; U.t = now; emit("undo:state");
}
export function batch(kind, fn) { snap(kind); U.batch = true; try { fn(); } finally { U.batch = false; } }
function restore(json) {
  const d = JSON.parse(json);
  S.notes.highlights = d.highlights; S.notes.cards = d.cards; S.notes.bookmarks = d.bookmarks; S.notes.frames = d.frames || []; S.notes.links = d.links || []; S.notes.paper_note = d.paper_note || "";
  U.kind = null; mark(); emit("notes:changed", {all: true}); emit("undo:state");
}
export function undo() { if (!U.undo.length) return false; U.redo.push(copy()); restore(U.undo.pop()); return true; }
export function redo() { if (!U.redo.length) return false; U.undo.push(copy()); restore(U.redo.pop()); return true; }
export const canUndo = () => U.undo.length > 0, canRedo = () => U.redo.length > 0;
export function resetUndo() { U.undo = []; U.redo = []; U.kind = null; emit("undo:state"); }

// ---------------------------------------------------------------- highlights (kind: highlight | underline | strike | note | area)
export const hlById = id => S.notes.highlights.find(h => h.id === id);
export const hlOnPage = n => S.notes.highlights.filter(h => h.parts.some(p => p.page === n));
export const firstPage = h => Math.min(...h.parts.map(p => p.page));

export function addHighlight(sel, color, kind = "highlight") {
  snap("add");
  const h = {id: uid(), kind, color, parts: sel.parts, text: sel.text || "", quote: sel.quote || {exact: sel.text || ""}, tags: [], comment: "", created: new Date().toISOString()};
  S.notes.highlights.push(h); mark(); emit("notes:changed", {hl: h.id, pages: h.parts.map(p => p.page)});
  return h;
}
export function updateHighlight(id, patch) {
  const h = hlById(id); if (!h) return;
  snap("hl:" + id + ":" + Object.keys(patch).join());
  Object.assign(h, patch); mark(); emit("notes:changed", {hl: id, pages: h.parts.map(p => p.page)});
}
export function deleteHighlight(id) {
  const h = hlById(id); if (!h) return;
  snap("del");
  S.notes.highlights = S.notes.highlights.filter(x => x.id !== id);
  S.notes.cards.forEach(c => { if (c.src && c.src.hl === id) { c.src = {...c.src, hl: null, ...snapshot(h)}; } });  // excerpts keep their own copy
  mark(); emit("notes:changed", {hl: id, pages: h.parts.map(p => p.page)});
}
const snapshot = h => ({page: firstPage(h), parts: h.parts, quote: h.quote});

// ---------------------------------------------------------------- cards (notes canvas)
export const cardById = id => S.notes.cards.find(c => c.id === id);
export function addCard(c) {
  snap("addcard");
  const card = {id: uid(), type: "note", x: 40, y: 40, w: 280, text: "", note: "", tags: [], created: new Date().toISOString(), ...c};
  S.notes.cards.push(card); mark(); emit("cards:changed", {add: card.id});
  return card;
}
export function updateCard(id, patch, quiet = false) {
  const c = cardById(id); if (!c) return;
  snap(("x" in patch || "w" in patch ? "move:" : "card:") + id);
  Object.assign(c, patch); mark(); if (!quiet) emit("cards:changed", {id});
}
export function deleteCard(id) {
  snap("delcard");
  S.notes.cards = S.notes.cards.filter(c => c.id !== id); S.notes.links = (S.notes.links || []).filter(l => l.a !== id && l.b !== id); mark(); emit("cards:changed", {del: id});
}
export function moveCards(list) {              // [{id, x, y}] in one undo step (a frame carried its cards)
  snap("movecards"); list.forEach(m => { const c = cardById(m.id); if (c) { c.x = m.x; c.y = m.y; } }); mark();
}

// ---------------------------------------------------------------- frames and arrows (notes canvas) and the note on the paper as a whole
export const frameById = id => (S.notes.frames || []).find(f => f.id === id);
export function addFrame(f) { snap("addframe"); const fr = {id: uid(), x: 40, y: 40, w: 420, h: 280, label: "", color: "#5b7fa6", ...f}; (S.notes.frames ||= []).push(fr); mark(); emit("board:changed", {frames: true}); return fr; }
export function updateFrame(id, patch, quiet = false) { const f = frameById(id); if (!f) return; snap("frame:" + id + ":" + Object.keys(patch).join()); Object.assign(f, patch); mark(); if (!quiet) emit("board:changed", {frames: true}); }
export function deleteFrame(id) { snap("delframe"); S.notes.frames = (S.notes.frames || []).filter(f => f.id !== id); mark(); emit("board:changed", {frames: true}); }
export function addLink(a, b) {
  if (a === b || (S.notes.links || []).some(l => (l.a === a && l.b === b) || (l.a === b && l.b === a))) return null;
  snap("addlink"); const l = {id: uid(), a, b, label: ""}; (S.notes.links ||= []).push(l); mark(); emit("board:changed", {links: true}); return l;
}
export function updateLink(id, patch) { const l = (S.notes.links || []).find(x => x.id === id); if (!l) return; snap("link:" + id); Object.assign(l, patch); mark(); emit("board:changed", {links: true}); }
export function deleteLink(id) { snap("dellink"); S.notes.links = (S.notes.links || []).filter(l => l.id !== id); mark(); emit("board:changed", {links: true}); }
export function setPaperNote(text) { snap("papernote"); S.notes.paper_note = String(text || "").slice(0, 20000); mark(); }
export function excerptFrom(h, pos = {}) {
  return addCard({type: "excerpt", text: h.text, src: {hl: h.id, ...snapshot(h)}, tags: [...h.tags], ...pos});
}

// ---------------------------------------------------------------- bookmarks
export function toggleBookmark(page, label) {
  snap("bookmark");
  const i = S.notes.bookmarks.findIndex(b => b.page === page);
  if (i >= 0) S.notes.bookmarks.splice(i, 1); else S.notes.bookmarks.push({id: uid(), page, label: label || "Page " + page, created: new Date().toISOString()});
  mark(); emit("notes:changed", {bookmarks: true});
}

// ---------------------------------------------------------------- tags (shared by every paper: Papers/_tags.json)
export const tagById = id => S.tags.find(t => t.id === id);
const TAG_COLORS = ["#2f5d8a", "#1f7a4d", "#a26a08", "#b3413a", "#6b4f9e", "#17808f", "#b0602a", "#5a6b2e"];
export async function saveTags() {
  try { const r = await api("/api/tags", {rev: S.tagsRev, tags: S.tags}); S.tagsRev = r.rev; }
  catch (e) {
    if (e.status === 409) { S.tags = e.body.current.tags; S.tagsRev = e.body.current.rev; toast("Tags changed elsewhere — reloaded."); }
    else toast("Couldn’t save tags: " + e.message);
  }
  emit("tags:changed");
}
// "method: survey" -> category "method", name "survey"; plain "survey" has no category
export function parseTag(s) {
  const i = s.indexOf(":"); const cat = i > 0 ? s.slice(0, i).trim() : "", name = (i > 0 ? s.slice(i + 1) : s).trim();
  return {category: cat, name};
}
export const tagLabel = t => (t.category ? t.category + ": " : "") + t.name;
export async function ensureTag(label) {
  const {category, name} = parseTag(label); if (!name) return null;
  let t = S.tags.find(x => x.name.toLowerCase() === name.toLowerCase() && (x.category || "").toLowerCase() === category.toLowerCase());
  if (!t) { t = {id: uid(), category, name, color: TAG_COLORS[S.tags.length % TAG_COLORS.length]}; S.tags.push(t); await saveTags(); }
  return t;
}
export async function deleteTag(id) {
  snap("deltag");
  S.tags = S.tags.filter(t => t.id !== id);
  S.notes.highlights.forEach(h => { h.tags = h.tags.filter(x => x !== id); });
  S.notes.cards.forEach(c => { c.tags = (c.tags || []).filter(x => x !== id); });
  mark(); emit("notes:changed", {all: true}); await saveTags();
}
export function toggleTag(kind, id, tagId) {
  const o = kind === "hl" ? hlById(id) : cardById(id); if (!o) return;
  snap("tag:" + id);
  o.tags = o.tags || [];
  o.tags = o.tags.includes(tagId) ? o.tags.filter(x => x !== tagId) : [...o.tags, tagId];
  mark(); emit(kind === "hl" ? "notes:changed" : "cards:changed", {hl: id, id});
}
