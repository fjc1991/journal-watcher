// The Folders page (Library → Folders): your virtual folders as big folder tiles, in the spirit of LiquidText's home screen.
// A folder is only a list of papers (nothing moves on disk). Here you see them all at a glance, sort and filter them, switch between
// tiles and a list, make a new one, rename or delete several in Edit mode, drop PDFs from your desktop onto a folder to add them to it,
// and open one to get its Papers / Ask / Matrix / Discover / Evidence / Ideas Lab tabs.
// Server: GET /api/folders -> [{id, name, count, created, updated, has:{ideas, matrix, ask}}], POST /api/folders {action, ...}.
import {notifyError, $, $$, esc, api, toast, debounce} from "./state.js";

let ctx = null;
const ls = {get: (k, d) => { try { return localStorage.getItem(k) || d; } catch (e) { return d; } }, set: (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* private window */ } }};
const P = {el: null, folders: [], sort: ls.get("jw-fp-sort", "name"), view: ls.get("jw-fp-view", "grid"), q: "", edit: false, sel: new Set(), adding: false, renaming: null};
const SORTS = [["name", "▲ By name"], ["name-desc", "▼ By name"], ["papers", "Most papers"], ["changed", "Recently changed"], ["created", "Newest first"]];

// the folder drawn as an icon (also used in the folder's own header); n = a number to print on it
export const icon = (n = "") => `<svg class="fp-svg" viewBox="0 0 120 94" aria-hidden="true"><path class="fp-back" d="M6 10a8 8 0 0 1 8-8h27l10 10h55a8 8 0 0 1 8 8v66a8 8 0 0 1-8 8H14a8 8 0 0 1-8-8z"/>
  <path class="fp-front" d="M6 30a8 8 0 0 1 8-8h92a8 8 0 0 1 8 8v54a8 8 0 0 1-8 8H14a8 8 0 0 1-8-8z"/>${n !== "" ? `<text x="60" y="73" text-anchor="middle" class="fp-num">${esc(n)}</text>` : ""}</svg>`;
const RAIL = {
  add: `<path d="M7 3h7l5 5v13H7z M14 3v5h5 M12 12v6 M9 15h6"/>`, ask: `<path d="M4 5h16v11H9l-5 4z"/>`,
  discover: `<circle cx="12" cy="12" r="9"/><path d="M15.5 8.5l-2 5-5 2 2-5z"/>`, hl: `<path d="M4 20l4-1 10-10-3-3L5 16z M14 7l3 3"/>`,
};
const ri = k => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${RAIL[k]}</svg>`;

function stats(f) {
  const rs = ctx.getLib().filter(r => (r.folders || []).includes(f.id));
  return {papers: rs.length, cards: rs.filter(r => r.card).length};
}
function ago(iso) {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 90) return "just now"; if (s < 3600) return Math.round(s / 60) + " min ago"; if (s < 86400) return Math.round(s / 3600) + " h ago";
  const d = Math.round(s / 86400); return d < 30 ? d + (d === 1 ? " day ago" : " days ago") : new Date(iso).toLocaleDateString(undefined, {year: "numeric", month: "short", day: "numeric"});
}
function shown() {
  const q = P.q.trim().toLowerCase();
  const xs = P.folders.filter(f => !q || f.name.toLowerCase().includes(q)).map(f => ({...f, ...stats(f)}));
  const by = {name: (a, b) => a.name.localeCompare(b.name, undefined, {numeric: true, sensitivity: "base"}), "name-desc": (a, b) => b.name.localeCompare(a.name, undefined, {numeric: true, sensitivity: "base"}),
    papers: (a, b) => b.papers - a.papers || a.name.localeCompare(b.name), changed: (a, b) => (b.updated || "").localeCompare(a.updated || ""), created: (a, b) => (b.created || "").localeCompare(a.created || "")};
  return xs.sort(by[P.sort] || by.name);
}
const flags = f => [f.has && f.has.ideas ? `<span title="Has an Ideas Lab board">💡</span>` : "", f.has && f.has.matrix ? `<span title="Has a comparison matrix">▦</span>` : "", f.has && f.has.ask ? `<span title="Has a saved conversation">💬</span>` : ""].join("");

// ---------------------------------------------------------------- the page
export async function mount(el) {
  P.el = el; P.edit = false; P.sel.clear(); P.adding = false; P.renaming = null;
  try { P.folders = await api("/api/folders"); } catch (e) { P.folders = []; notifyError(e); }
  if (!el._fp) { wire(el); el._fp = true; }
  draw();
}
function draw() {
  const el = P.el; if (!el) return;
  const xs = shown(), grid = P.view === "grid";
  const tile = f => `<div class="fp-tile${P.sel.has(f.id) ? " sel" : ""}" data-id="${esc(f.id)}" role="button" tabindex="0" aria-label="${esc(f.name)}, ${f.papers} papers" data-drop="${esc(f.id)}">
      ${P.edit ? `<span class="fp-check" aria-hidden="true">${P.sel.has(f.id) ? "✓" : ""}</span>` : ""}
      <div class="fp-ico">${icon(f.papers)}</div>
      ${P.renaming === f.id ? `<input class="fp-rename" value="${esc(f.name)}" maxlength="60" aria-label="Folder name">` : `<div class="fp-name">${esc(f.name)}</div>`}
      <div class="fp-meta">${f.papers} paper${f.papers === 1 ? "" : "s"}${f.cards ? ` · ${f.cards} with a card` : ""}</div><div class="fp-flags">${flags(f)}</div></div>`;
  const row = f => `<div class="fp-row${P.sel.has(f.id) ? " sel" : ""}" data-id="${esc(f.id)}" role="button" tabindex="0" data-drop="${esc(f.id)}">
      ${P.edit ? `<span class="fp-check" aria-hidden="true">${P.sel.has(f.id) ? "✓" : ""}</span>` : ""}<span class="fp-mini">${icon()}</span>
      ${P.renaming === f.id ? `<input class="fp-rename" value="${esc(f.name)}" maxlength="60" aria-label="Folder name">` : `<span class="fp-name">${esc(f.name)}</span>`}
      <span class="fp-flags">${flags(f)}</span><span class="fp-c">${f.papers} paper${f.papers === 1 ? "" : "s"}</span><span class="fp-c">${f.cards} with a card</span><span class="fp-c fp-when">${esc(ago(f.updated))}</span></div>`;
  const add = P.adding ? (grid ? `<div class="fp-tile fp-newtile"><div class="fp-ico">${icon("")}</div><input class="fp-newinput" placeholder="Folder name" maxlength="60" aria-label="New folder name"><div class="fp-meta">Enter to create · Esc to cancel</div></div>`
      : `<div class="fp-row fp-newtile"><span class="fp-mini">${icon()}</span><input class="fp-newinput" placeholder="Folder name" maxlength="60" aria-label="New folder name"></div>`)
    : (grid ? `<div class="fp-tile fp-add" data-fp="new" role="button" tabindex="0"><div class="fp-ico">${icon("＋")}</div><div class="fp-name">New folder</div><div class="fp-meta">Papers stay where they are</div></div>` : "");
  el.innerHTML = `<div class="fp">
    <div class="fp-bar"><div class="fp-title">Folders <small>${P.folders.length}</small></div><div class="fp-tools">
      <button class="fp-btn${P.edit ? " on" : ""}" data-fp="edit" title="Select folders to rename or delete">${P.edit ? "Done" : "Edit"}</button>
      <select class="fp-sort" aria-label="Sort folders">${SORTS.map(([k, t]) => `<option value="${k}"${P.sort === k ? " selected" : ""}>${t}</option>`).join("")}</select>
      <button class="fp-btn fp-ic${grid ? "" : " on"}" data-fp="view" title="${grid ? "Show as a list" : "Show as tiles"}" aria-label="Toggle tiles and list">${grid
        ? `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M8 6h13M8 12h13M8 18h13M3.5 6h.01M3.5 12h.01M3.5 18h.01"/></svg>`
        : `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><rect x="4" y="4" width="6.5" height="6.5" rx="1"/><rect x="13.5" y="4" width="6.5" height="6.5" rx="1"/><rect x="4" y="13.5" width="6.5" height="6.5" rx="1"/><rect x="13.5" y="13.5" width="6.5" height="6.5" rx="1"/></svg>`}</button>
      <button class="fp-btn fp-ic" data-fp="new" title="New folder" aria-label="New folder"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M12 10.5v5M9.5 13h5"/></svg></button>
      <input type="search" class="fp-find" placeholder="Find a folder" value="${esc(P.q)}" aria-label="Find a folder"></div></div>
    <div class="fp-body"><aside class="fp-rail" aria-label="Quick actions"><div class="fp-railh">Start</div>
      ${[["add", "Add paper"], ["ask", "Ask library"], ["discover", "Discover"], ["hl", "Highlights"]].map(([k, t]) => `<button class="fp-act" data-fp="rail-${k}"><i>${ri(k)}</i><span>${t}</span></button>`).join("")}</aside>
      <div class="fp-main"><div class="fp-path">Home</div>
        ${xs.length || P.adding ? `<div class="${grid ? "fp-grid" : "fp-list"}">${grid ? xs.map(tile).join("") : xs.map(row).join("")}${add}</div>` : (P.folders.length ? `<div class="fp-empty">No folder matches “${esc(P.q)}”.</div>` : "")}
        ${!P.folders.length && !P.adding ? `<div class="fp-empty"><b>No folders yet.</b><br>A folder is just a list of papers — they stay where they are in <code>Papers/</code>. Make one for a project or a literature review,
          then use the 📁 button on any paper (or drop PDFs onto the folder) to fill it. Each folder can also hold a comparison matrix, a question-and-answer history and an Ideas Lab board.<div class="fp-grid" style="margin-top:18px">${grid ? `<div class="fp-tile fp-add" data-fp="new" role="button" tabindex="0"><div class="fp-ico">${icon("＋")}</div><div class="fp-name">New folder</div></div>` : ""}</div></div>` : ""}</div></div>
    ${P.edit ? `<div class="fp-editbar"><b>${P.sel.size} selected</b><button class="fp-btn2" data-fp="rename"${P.sel.size === 1 ? "" : " disabled"}>Rename</button><button class="fp-btn2 danger" data-fp="delete"${P.sel.size ? "" : " disabled"}>Delete</button><button class="fp-btn2" data-fp="all">Select all</button></div>` : ""}</div>`;
  const inp = $(".fp-newinput", el) || $(".fp-rename", el); if (inp) { inp.focus(); inp.select && inp.select(); }
}

// ---------------------------------------------------------------- actions
async function refresh() { try { P.folders = await api("/api/folders"); } catch (e) { /* keep what we have */ } if (window.LibraryUI) await window.LibraryUI.refreshFolders(); await ctx.reload(); draw(); }
async function create(name) {
  name = (name || "").trim(); P.adding = false; if (!name) return draw();
  try { await api("/api/folders", {action: "create", name}); toast(`Folder “${name}” created.`); } catch (e) { notifyError(e); }
  await refresh();
}
async function rename(id, name) {
  P.renaming = null; P.sel.clear(); name = (name || "").trim(); const f = P.folders.find(x => x.id === id);
  if (name && f && name !== f.name) { try { await api("/api/folders", {action: "rename", id, name}); } catch (e) { notifyError(e); } }
  await refresh();
}
async function del() {
  const ids = [...P.sel]; if (!ids.length) return;
  const names = ids.map(i => (P.folders.find(f => f.id === i) || {}).name).filter(Boolean);
  if (!confirm(`Delete ${ids.length === 1 ? "the folder “" + names[0] + "”" : ids.length + " folders"}?\n\nThe papers are not deleted. The folder’s Ideas Lab board, matrix and saved conversation are.`)) return;
  for (const id of ids) { try { await api("/api/folders", {action: "delete", id}); } catch (e) { notifyError(e); } }
  P.sel.clear(); toast(`Deleted ${ids.length} folder${ids.length === 1 ? "" : "s"}.`); await refresh();
}
function wire(el) {
  el.addEventListener("click", e => {
    const t = e.target, a = t.closest("[data-fp]")?.dataset.fp;
    if (a === "edit") { P.edit = !P.edit; P.sel.clear(); P.renaming = null; return draw(); }
    if (a === "view") { P.view = P.view === "grid" ? "list" : "grid"; ls.set("jw-fp-view", P.view); return draw(); }
    if (a === "new") { P.adding = true; P.edit = false; return draw(); }
    if (a === "all") { shown().forEach(f => P.sel.add(f.id)); return draw(); }
    if (a === "rename") { P.renaming = [...P.sel][0]; return draw(); }
    if (a === "delete") return del();
    if (a && a.startsWith("rail-")) { const k = a.slice(5), L = window.LibraryUI; return k === "add" ? L.addDialog() : L.openTab(k); }
    if (t.closest(".fp-rename,.fp-newinput")) return;
    const item = t.closest(".fp-tile[data-id], .fp-row[data-id]"); if (!item) return;
    if (P.edit) { const id = item.dataset.id; P.sel.has(id) ? P.sel.delete(id) : P.sel.add(id); return draw(); }
    ctx.go({j: "@" + item.dataset.id});
  });
  el.addEventListener("keydown", e => {
    const inp = e.target.closest(".fp-newinput, .fp-rename");
    if (inp) {
      if (e.key === "Enter") { e.preventDefault(); inp.classList.contains("fp-newinput") ? create(inp.value) : rename(P.renaming, inp.value); }
      else if (e.key === "Escape") { e.preventDefault(); P.adding = false; P.renaming = null; draw(); }
      return;
    }
    if ((e.key === "Enter" || e.key === " ") && e.target.matches(".fp-tile, .fp-row")) { e.preventDefault(); e.target.click(); }
  });
  el.addEventListener("focusout", e => { const inp = e.target.closest && e.target.closest(".fp-newinput, .fp-rename"); if (inp && el.contains(inp)) setTimeout(() => { if (!document.body.contains(inp)) return; inp.classList.contains("fp-newinput") ? create(inp.value) : rename(P.renaming, inp.value); }, 120); });
  el.addEventListener("change", e => { if (e.target.matches(".fp-sort")) { P.sort = e.target.value; ls.set("jw-fp-sort", P.sort); draw(); } });
  el.addEventListener("input", debounce(e => { if (e.target.matches(".fp-find")) { P.q = e.target.value; const pos = e.target.selectionStart; draw(); const f = $(".fp-find", el); f.focus(); f.setSelectionRange(pos, pos); } }, 160));
  // (PDFs dropped from your desktop onto a tile, or on this page, are handled by dropimport.js)
}

window.FoldersUI = {init(c) { ctx = c; }, mount, icon};
