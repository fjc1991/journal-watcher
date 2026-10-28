// Import a bibliography you already have (EndNote / Mendeley / Zotero .ris, Overleaf / Mendeley .bib): see which references are in your library, fetch the others through the usual
// download routes (open access first, then your sign-ins), and put the ones you already have into a folder. Nothing is added to the library here; downloads arrive as a "Picks" run.
// Server: POST /api/bib/parse, POST /api/related/download, POST /api/folders.
import {$, $$, esc, api, toast, notifyError} from "./state.js";

let ctx = null, d = null;
const B = {name: "", entries: [], sel: new Set(), only: true};
const auth = e => e.authors.length > 3 ? e.authors.slice(0, 3).map(a => a.split(" ").pop()).join(", ") + " et al." : e.authors.map(a => a.split(" ").pop()).join(", ");

export function open() {
  if (d) d.remove();
  d = document.createElement("dialog"); d.id = "bibdlg"; d.className = "card adddlg bibdlg";
  d.innerHTML = `<h2>Import a bibliography</h2><p class="pmeta">From EndNote, Mendeley, Zotero or Overleaf: a <b>.ris</b> or <b>.bib</b> file. You will see which references you already have; the ones you tick are fetched like every other pick (open access first, then your sign-ins and library access).</p>
    <div class="dropzone" id="bd-drop">Drop a .ris or .bib file here or <a href="#" id="bd-pick">choose a file</a><input type="file" id="bd-file" accept=".ris,.bib,.bibtex,.txt,text/plain" class="hide"></div><div id="bd-body"></div>
    <div class="row" style="justify-content:flex-end;margin-top:10px"><button class="btn" id="bd-close">Close</button></div>`;
  document.body.append(d); d.showModal();
  $("#bd-close", d).onclick = () => d.close();
  $("#bd-pick", d).onclick = e => { e.preventDefault(); $("#bd-file", d).click(); };
  $("#bd-file", d).onchange = e => e.target.files[0] && read(e.target.files[0]);
  const dz = $("#bd-drop", d); dz.ondragover = e => { e.preventDefault(); dz.classList.add("over"); }; dz.ondragleave = () => dz.classList.remove("over");
  dz.ondrop = e => { e.preventDefault(); e.stopPropagation(); dz.classList.remove("over"); const f = e.dataTransfer.files[0]; if (f) read(f); };
  d.addEventListener("click", onClick); d.addEventListener("change", onChange);
}
async function read(file) {
  const body = $("#bd-body", d); body.innerHTML = `<div class="pmeta">Reading ${esc(file.name)}…</div>`;
  try {
    const text = await file.text(), r = await api("/api/bib/parse", {text, name: file.name});
    B.name = file.name; B.entries = r.entries; B.only = true; B.sel = new Set(r.entries.map((e, i) => (!e.file ? i : -1)).filter(i => i >= 0).slice(0, 40)); B.have = r.have; B.noDoi = r.no_doi; draw();
  } catch (e) { body.innerHTML = `<div class="of-co warn"><div>${esc(e.message)}</div></div>`; }
}
function draw() {
  const body = $("#bd-body", d), todo = B.entries.filter(e => !e.file).length, shown = B.entries.map((e, i) => [e, i]).filter(([e]) => !B.only || !e.file);
  const folders = (window.LibraryUI && window.LibraryUI.folders && window.LibraryUI.folders()) || [];
  body.innerHTML = `<div class="bd-sum"><b>${B.entries.length}</b> reference${B.entries.length === 1 ? "" : "s"} in ${esc(B.name)} · <b>${B.have}</b> already in your library · <b>${todo}</b> you don’t have${B.noDoi ? ` (${B.noDoi} without a DOI: harder to find)` : ""}</div>
    <div class="row bd-bar"><label class="chk"><input type="checkbox" id="bd-only"${B.only ? " checked" : ""}> only the ones I don’t have</label><button class="linkbtn" data-bd="all">tick all</button> · <button class="linkbtn" data-bd="none">none</button><span class="pmeta">${B.sel.size} ticked</span></div>
    <div class="bd-list">${shown.map(([e, i]) => `<label class="bd-r${e.file ? " have" : ""}"><input type="checkbox" data-i="${i}"${B.sel.has(i) ? " checked" : ""}${e.file ? " disabled" : ""}><span><b>${esc(e.title)}</b><br><small>${esc(auth(e))}${e.year ? " · " + esc(e.year) : ""}${e.journal ? " · " + esc(e.journal) : ""}</small>
      ${e.file ? ` <span class="of-badge">in your library</span>` : e.doi ? ` <span class="of-badge">${esc(e.doi)}</span>` : ` <span class="of-badge inferred">no DOI</span>`}</span></label>`).join("") || `<div class="pmeta">Nothing to show.</div>`}</div>
    <div class="row bd-act"><button class="btn primary" data-bd="go"${B.sel.size ? "" : " disabled"}>Fetch ${B.sel.size} paper${B.sel.size === 1 ? "" : "s"}</button><span class="pmeta">${B.sel.size >= 40 ? "40 at a time; the rest stay in the list." : ""}</span></div>
    ${B.have ? `<div class="row bd-act"><span class="pmeta">Put the ${B.have} you already have into a folder:</span><select id="bd-folder"><option value="">a new folder named “${esc(B.name.replace(/\.[^.]+$/, ""))}”</option>${folders.map(f => `<option value="${esc(f.id)}">📁 ${esc(f.name)}</option>`).join("")}</select><button class="btn" data-bd="folder">Do it</button></div>` : ""}`;
}
function onChange(e) {
  const t = e.target;
  if (t.id === "bd-only") { B.only = t.checked; return draw(); }
  if (t.matches("[data-i]")) { const i = +t.dataset.i; if (t.checked) { if (B.sel.size >= 40) { t.checked = false; return toast("Forty at a time: fetch these first."); } B.sel.add(i); } else B.sel.delete(i); draw(); }
}
async function onClick(e) {
  const b = e.target.closest("[data-bd]"); if (!b) return; const a = b.dataset.bd;
  if (a === "all") { B.entries.forEach((en, i) => { if (!en.file && B.sel.size < 40) B.sel.add(i); }); return draw(); }
  if (a === "none") { B.sel.clear(); return draw(); }
  try {
    if (a === "go") {
      const picks = [...B.sel].map(i => B.entries[i]), label = "Imported: " + B.name.replace(/\.[^.]+$/, "").slice(0, 40);
      b.disabled = true;
      const r = await api("/api/related/download", {items: picks.map(e => ({id: e.doi || e.title.toLowerCase().slice(0, 60), doi: e.doi, title: e.title, authors: e.authors, year: e.year, venue: e.journal, reason: "imported from " + B.name})), label});
      toast(`Fetching ${r.n} paper${r.n === 1 ? "" : "s"}${r.have && r.have.length ? ` (${r.have.length} were already in your library)` : ""}…`);
      B.entries = B.entries.filter((e, i) => !B.sel.has(i)); B.sel.clear(); d.close(); if (ctx.openRun) ctx.openRun(r.run); return;
    }
    if (a === "folder") {
      const files = B.entries.filter(e => e.file).map(e => e.file), id = $("#bd-folder", d).value;
      const r = id ? await api("/api/folders", {action: "add", id, files}) : await api("/api/folders", {action: "create", name: B.name.replace(/\.[^.]+$/, "").slice(0, 60) || "Imported", files});
      toast(`${files.length} paper${files.length === 1 ? "" : "s"} put in a folder.`); await ctx.reload(); window.LibraryUI && window.LibraryUI.refreshView && window.LibraryUI.refreshView();
    }
  } catch (er) { notifyError(er); if (b) b.disabled = false; }
}

window.BibImport = {init(c) { ctx = c; }, open};
