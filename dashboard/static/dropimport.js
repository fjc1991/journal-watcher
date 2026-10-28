// Drop PDFs you already have onto the app. Where you drop decides what happens:
//   · on a folder (a tile on the Folders page, or a folder chip in the Library) -> they go straight into that folder
//   · anywhere inside an open folder                                           -> they go into that folder
//   · on the Folders page, away from the tiles                                 -> a small chooser: new folder / an existing folder / library only
//   · anywhere else in the Library                                             -> the same chooser, "library only" preselected
// Each file is posted to /api/import/pdf (the server files it under Papers/YYYY-MM/<Journal>/ and says whether you already had it), then added
// to the folder. A paper you already have is not imported twice, only added. The result appears as a snackbar with Open folder / Undo.
import {notifyError, $, $$, esc, api, toast} from "./state.js";

let ctx = null, chain = Promise.resolve();
const D = {overlay: null, snack: null, timer: 0};
const ls = {get: k => { try { return localStorage.getItem(k) || ""; } catch (e) { return ""; } }, set: (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* private window */ } }};
const isPdf = f => /\.pdf$/i.test(f.name) || f.type === "application/pdf";
const hasFiles = e => [...(e.dataTransfer && e.dataTransfer.types || [])].includes("Files");
const size = n => n > 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB";
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
const norm = s => String(s || "").replace(/\s+/g, " ").trim();

// ---------------------------------------------------------------- where did it land?
const visible = el => !!el && el.offsetParent !== null;
function where(t) {
  const el = t instanceof Element ? t : null;
  const tile = el && el.closest("[data-drop]"), chip = el && el.closest("[data-dropfolder]");
  if (tile || chip) {
    const c = (tile || chip).cloneNode(true); $$("small", c).forEach(x => x.remove());      // the folder's name, without the paper count
    return {kind: "folder", id: tile ? tile.dataset.drop : chip.dataset.dropfolder, name: norm((tile ? $(".fp-name", c) || c : c).textContent).replace(/^📁\s*/, "")};
  }
  const open = window.LibraryUI && window.LibraryUI.currentFolder && window.LibraryUI.currentFolder();
  if (open) return {kind: "open", id: open};
  if (visible($("#libview .fp"))) return {kind: "home"};
  if (visible($("#libview"))) return {kind: "library"};
  return null;
}
const folders = async () => { try { return await api("/api/folders"); } catch (e) { return []; } };
const folderName = (fs, id) => (fs.find(f => f.id === id) || {}).name || "the folder";

// ---------------------------------------------------------------- the page-wide overlay while files are dragged over the app
function overlay(show, text) {
  if (!D.overlay) { D.overlay = document.createElement("div"); D.overlay.className = "di-ov hide"; D.overlay.innerHTML = `<div class="di-pill"></div>`; document.body.append(D.overlay); }
  D.overlay.classList.toggle("hide", !show);
  if (show && text != null) $(".di-pill", D.overlay).textContent = text;
}
let overFolder = null;
function hint(w) {
  if (!w) return "Drop PDFs on the Library, Folders page or inside a folder to add them";
  if (w.kind === "folder") return w.name ? "Add to “" + w.name + "”" : "Add to this folder";
  if (w.kind === "open") return "Add to this folder";
  return "Drop PDFs to add them — you’ll choose where they go";
}
function mark(w) {      // highlight the tile / chip under the cursor
  const el = w && w.kind === "folder" ? $(`[data-drop="${CSS.escape(w.id)}"], [data-dropfolder="${CSS.escape(w.id)}"]`) : null;
  if (el === overFolder) return;
  if (overFolder) overFolder.classList.remove("fp-over", "over");
  if ((overFolder = el)) el.classList.add(el.hasAttribute("data-drop") ? "fp-over" : "over");
}

// ---------------------------------------------------------------- the chooser
async function choose(files, w) {
  const fs = (await folders()).slice().sort((a, b) => (b.id === ls.get("jw-drop-last")) - (a.id === ls.get("jw-drop-last")) || (b.updated || "").localeCompare(a.updated || ""));
  const pdfs = files.filter(isPdf), skipped = files.filter(f => !isPdf(f));
  const today = new Date().toLocaleDateString(undefined, {day: "numeric", month: "short", year: "numeric"});
  const last = fs.find(f => f.id === ls.get("jw-drop-last"));
  const first = !fs.length ? "new" : w.kind === "library" ? "lib" : last ? "f:" + last.id : "new";
  return new Promise(resolve => {
    $("#di-dlg") && $("#di-dlg").remove();
    const d = document.createElement("dialog"); d.id = "di-dlg"; d.className = "card di-dlg"; d.setAttribute("aria-labelledby", "di-h");
    const li = f => `<li><span>${esc(f.name)}</span><small>${size(f.size)}</small></li>`;
    d.innerHTML = `<h2 id="di-h">Add ${plural(pdfs.length, "PDF")}</h2>
      <ul class="di-files">${pdfs.slice(0, 5).map(li).join("")}${pdfs.length > 5 ? `<li class="more">+ ${pdfs.length - 5} more</li>` : ""}${skipped.slice(0, 3).map(f => `<li class="skip"><span>${esc(f.name)}</span><small>not a PDF — skipped</small></li>`).join("")}</ul>
      <div class="lbl">Where should ${pdfs.length === 1 ? "it" : "they"} go?</div>
      <div class="di-opts" role="radiogroup" aria-label="Destination">
        <label class="di-opt"><input type="radio" name="di-to" value="new"><span class="di-ic">＋</span><span class="di-t">New folder</span><input type="text" id="di-name" maxlength="60" value="${esc(pdfs.length === 1 ? norm(pdfs[0].name.replace(/\.pdf$/i, "").replace(/[_-]+/g, " ")).slice(0, 60) : "Added " + today)}" aria-label="New folder name"></label>
        ${fs.length > 7 ? `<input type="search" id="di-find" placeholder="Find a folder…" aria-label="Find a folder">` : ""}
        <div class="di-list">${fs.map(f => `<label class="di-opt" data-n="${esc(f.name.toLowerCase())}"><input type="radio" name="di-to" value="f:${esc(f.id)}"><span class="di-ic">📁</span><span class="di-t">${esc(f.name)}</span><small>${plural(f.count, "paper")}</small></label>`).join("")}</div>
        <label class="di-opt"><input type="radio" name="di-to" value="lib"><span class="di-ic">▤</span><span class="di-t">Library only</span><small>no folder</small></label>
      </div>
      <label class="chk"><input type="checkbox" id="di-sum"> Also write a summary for new papers (uses AI, about 1–2 cents each)</label>
      <div class="di-err" id="di-err" role="alert"></div>
      <div class="row di-btns"><button class="btn" id="di-cancel">Cancel</button><button class="btn primary" id="di-go">Add ${plural(pdfs.length, "paper")}</button></div>`;
    document.body.append(d); d.showModal();
    const pick = v => { const r = $(`input[name="di-to"][value="${CSS.escape(v)}"]`, d); if (r) r.checked = true; };
    pick(first); const nm = $("#di-name", d);
    const go = () => {
      const v = ($('input[name="di-to"]:checked', d) || {}).value, err = t => { $("#di-err", d).textContent = t; };
      if (v === "new") {
        const n = norm(nm.value); if (!n) { err("Give the new folder a name."); return nm.focus(); }
        const dup = fs.find(f => f.name.toLowerCase() === n.toLowerCase()); if (dup) { err(`A folder called “${dup.name}” already exists — choose it from the list.`); pick("f:" + dup.id); return; }
        resolve({files: pdfs, dest: {mode: "new", name: n}, summarize: $("#di-sum", d).checked});
      } else resolve({files: pdfs, dest: v === "lib" ? {mode: "lib"} : {mode: "folder", id: v.slice(2)}, summarize: $("#di-sum", d).checked});
      d.close();
    };
    $("#di-go", d).onclick = go; $("#di-cancel", d).onclick = () => d.close();
    nm.onfocus = () => pick("new"); nm.oninput = () => { $("#di-err", d).textContent = ""; };
    d.onkeydown = e => { if (e.key === "Enter" && !e.target.matches("button, textarea")) { e.preventDefault(); go(); } };
    const find = $("#di-find", d); if (find) find.oninput = () => { const q = find.value.toLowerCase(); $$(".di-list .di-opt", d).forEach(l => l.classList.toggle("hide", !!q && !l.dataset.n.includes(q))); };
    d.addEventListener("close", () => { resolve(null); d.remove(); }, {once: true});
    d.addEventListener("click", e => { if (e.target === d) d.close(); });
    (first === "new" ? nm : $('input[name="di-to"]:checked', d)).focus(); if (first === "new") nm.select();
  });
}

// ---------------------------------------------------------------- the snackbar: progress, then the result with Open folder / Undo
function snack() {
  clearTimeout(D.timer); D.snack && D.snack.remove();
  const s = D.snack = document.createElement("div"); s.className = "di-snack card"; s.setAttribute("role", "status"); document.body.append(s);
  return s;
}
function bye(s, ms) { clearTimeout(D.timer); if (ms) D.timer = setTimeout(() => { s.classList.add("gone"); setTimeout(() => s.remove(), 300); }, ms); }

async function post(file, summarize) {
  const x = await fetch("/api/import/pdf", {method: "POST", headers: {"Content-Type": "application/pdf", "X-Filename": encodeURIComponent(file.name), "X-Summarize": summarize ? "1" : "0"}, body: file});
  const j = await x.json().catch(() => ({}));
  if (!x.ok) throw new Error(j.error || x.statusText);
  return j;
}
async function run({files, dest, summarize}) {
  const s = snack(), res = [], total = files.length; let stop = false, folderId = dest.id || null, fname = "", created = false;
  const paint = (n, msg) => { s.innerHTML = `<div class="di-msg"><b>${esc(msg)}</b></div><div class="di-bar"><i style="width:${Math.round(100 * n / total)}%"></i></div><div class="row"><button class="btn small" data-di="stop">Stop</button></div>`; };
  s.onclick = e => { if (e.target.closest("[data-di=stop]")) stop = true; };
  paint(0, "Adding " + plural(total, "paper") + "…");
  try {
    if (dest.mode === "new") {
      const r = await api("/api/folders", {action: "create", name: dest.name, files: []});
      const f = r.folders.find(x => x.name.toLowerCase() === dest.name.toLowerCase()); folderId = f && f.id; fname = dest.name; created = true;
      if (!folderId) throw new Error("couldn’t create the folder");
    } else if (folderId) fname = folderName(await folders(), folderId);
  } catch (e) { s.innerHTML = `<div class="di-msg"><b>${esc(e.message)}</b></div><div class="row"><button class="btn small" data-di="x">Close</button></div>`; s.onclick = () => s.remove(); return; }
  for (let i = 0; i < total && !stop; i++) {
    const f = files[i]; paint(i, `Adding ${i + 1} of ${total}: ${f.name.length > 46 ? f.name.slice(0, 44) + "…" : f.name}`);
    try {
      const r = await post(f, summarize);
      if (folderId) await api("/api/folders", {action: "add", id: folderId, files: [r.file]});
      res.push({name: f.name, file: r.file, title: r.title, existing: !!r.existing});
    } catch (e) { res.push({name: f.name, error: e.message}); }
  }
  if (folderId) ls.set("jw-drop-last", folderId);
  const ok = res.filter(r => r.file), isNew = ok.filter(r => !r.existing), had = ok.filter(r => r.existing), bad = res.filter(r => r.error), left = total - res.length;
  const msg = (folderId ? (ok.length ? `Added ${plural(ok.length, "paper")} to “${fname}”` : "Nothing was added") : (isNew.length ? `Added ${plural(isNew.length, "paper")} to your library` : "Nothing new to add")) +
    (had.length ? ` · ${had.length} already in your library` : "") + (bad.length ? ` · ${bad.length} failed` : "") + (left ? ` · stopped, ${left} left` : "");
  const detail = res.map(r => `<li class="${r.error ? "bad" : r.existing ? "had" : "new"}"><span>${esc(r.title || r.name)}</span><small>${r.error ? esc(r.error) : r.existing ? "already in your library" + (folderId ? " — added to the folder" : "") : "added"}</small></li>`).join("");
  s.innerHTML = `<div class="di-msg"><b>${esc(msg)}</b></div>${bad.length ? `<ul class="di-det">${detail}</ul>` : `<ul class="di-det hide">${detail}</ul>`}
    <div class="row">${folderId && ok.length ? `<button class="btn small" data-di="open">Open folder</button><button class="btn small" data-di="undo">Undo</button>` : ""}<button class="btn small" data-di="det">Details</button><button class="btn small" data-di="x">Close</button></div>`;
  s.onclick = async e => {
    const a = e.target.closest("[data-di]"); if (!a) return; const k = a.dataset.di;
    if (k === "x") s.remove();
    else if (k === "det") $(".di-det", s).classList.toggle("hide");
    else if (k === "open") { s.remove(); ctx.go({j: "@" + folderId}); }
    else if (k === "undo") {
      try {
        await api("/api/folders", {action: "remove", id: folderId, files: ok.map(r => r.file)});
        if (created) await api("/api/folders", {action: "delete", id: folderId});
        s.innerHTML = `<div class="di-msg"><b>Undone.</b> ${isNew.length ? plural(isNew.length, "paper") + " stay" + (isNew.length === 1 ? "s" : "") + " in your library." : ""}</div>`; bye(s, 4000);
        const inside = window.LibraryUI.currentFolder && window.LibraryUI.currentFolder() === folderId;
        if (created && inside) { await ctx.reload(); ctx.go({j: null}); } else await refreshView();
      } catch (er) { notifyError(er); }
    }
  };
  bye(s, bad.length || left ? 0 : 15000);
  await refreshView();
}

// re-draw whatever is on screen so counts and rows are current
async function refreshView() {
  try {
    if (window.LibraryUI) await window.LibraryUI.refreshFolders();
    await ctx.reload();
    const L = window.LibraryUI, home = $("#libview .fp");
    if (L && L.currentFolder && L.currentFolder()) return L.refreshView();
    if (home && visible(home) && window.FoldersUI) return window.FoldersUI.mount(home.parentElement);
    if (L && L.refreshView) return L.refreshView();
  } catch (e) { /* the next navigation redraws it */ }
}

// ---------------------------------------------------------------- handling a drop
export async function handle(fileList, w) {
  const files = [...fileList];              // copy now: the browser empties the live list after the event
  if (!files.length) return;
  if (!files.some(isPdf)) return toast("Those aren’t PDFs — drop PDF files to add papers.");
  w = w || where(document.body);
  if (!w) return toast("Open the Library or a folder first, then drop the PDFs there.");
  let job;
  if (w.kind === "folder" || w.kind === "open") job = {files: files.filter(isPdf), dest: {mode: "folder", id: w.id}, summarize: false};
  else job = await choose(files, w);
  if (!job) return;
  const skipped = files.length - job.files.length; if (skipped) toast(`${plural(skipped, "file")} skipped: not a PDF.`);
  chain = chain.then(() => run(job)).catch(e => notifyError(e));
}

// "＋ Add papers" in an open folder, for when dragging isn't convenient
export function choosePdfs(folderId) {
  const i = document.createElement("input"); i.type = "file"; i.accept = "application/pdf,.pdf"; i.multiple = true; i.style.display = "none"; document.body.append(i);
  i.onchange = () => { handle(i.files, {kind: "open", id: folderId}); setTimeout(() => i.remove(), 0); };
  i.click();
}
export function pickFromLibrary(folderId) {
  const rows = ctx.getLib().filter(r => !(r.folders || []).includes(folderId)), name = window.LibraryUI.folderName(folderId);
  $("#di-pick") && $("#di-pick").remove();
  const d = document.createElement("dialog"); d.id = "di-pick"; d.className = "card di-dlg";
  d.innerHTML = `<h2>Add papers from your library</h2><div class="pmeta" style="margin-bottom:8px">${plural(rows.length, "paper")} not yet in “${esc(name)}”</div>
    <input type="search" id="dp-find" placeholder="Search by title, author or journal…" aria-label="Search your library"><div class="di-list dp-list" id="dp-list"></div>
    <div class="row di-btns" style="margin-top:12px"><span class="pmeta" id="dp-n" style="margin-right:auto"></span><button class="btn" id="dp-cancel">Cancel</button><button class="btn primary" id="dp-go" disabled>Add</button></div>`;
  document.body.append(d); d.showModal();
  const sel = new Set(), list = $("#dp-list", d), find = $("#dp-find", d);
  const draw = () => {
    const q = find.value.trim().toLowerCase(), hit = rows.filter(r => !q || `${r.title} ${[].concat(r.authors || []).join(" ")} ${r.journal}`.toLowerCase().includes(q));
    list.innerHTML = hit.slice(0, 200).map(r => `<label class="di-opt"><input type="checkbox" data-f="${esc(r.file)}"${sel.has(r.file) ? " checked" : ""}><span class="dp-t"><b>${esc(r.title)}</b><small>${esc(r.journal)} · ${esc(r.year || "")}</small></span></label>`).join("") +
      (hit.length > 200 ? `<div class="pmeta" style="padding:6px">${hit.length - 200} more — narrow the search</div>` : "") || `<div class="pmeta" style="padding:10px">${rows.length ? "Nothing matches." : "Every paper in your library is already here."}</div>`;
    $("#dp-n", d).textContent = sel.size ? plural(sel.size, "paper") + " selected" : ""; $("#dp-go", d).disabled = !sel.size;
  };
  list.onchange = e => { const f = e.target.dataset.f; if (!f) return; e.target.checked ? sel.add(f) : sel.delete(f); draw(); };
  find.oninput = draw; draw(); find.focus();
  $("#dp-cancel", d).onclick = () => d.close(); d.addEventListener("close", () => d.remove(), {once: true});
  $("#dp-go", d).onclick = async () => {
    try { await api("/api/folders", {action: "add", id: folderId, files: [...sel]}); ls.set("jw-drop-last", folderId); toast(`Added ${plural(sel.size, "paper")} to “${name}”.`); d.close(); await refreshView(); }
    catch (e) { notifyError(e); }
  };
}

function wire() {
  let hideT = 0;
  const off = () => { clearTimeout(hideT); overlay(false); mark(null); };
  document.addEventListener("dragover", e => {
    if (!hasFiles(e) || e.defaultPrevented) return;                     // (the "Add a paper" box handles its own drops)
    if (e.target.closest && e.target.closest("dialog")) return;
    e.preventDefault(); const w = where(e.target);
    e.dataTransfer.dropEffect = w ? "copy" : "none";
    mark(w); overlay(true, hint(w)); clearTimeout(hideT); hideT = setTimeout(off, 800);   // dragover repeats while you hover; silence means you left
  });
  document.addEventListener("dragleave", e => { if (e.clientX <= 0 || e.clientY <= 0 || e.clientX >= innerWidth || e.clientY >= innerHeight) off(); });
  document.addEventListener("dragend", off);
  document.addEventListener("drop", e => {
    if (!hasFiles(e) || e.defaultPrevented) return;
    if (e.target.closest && e.target.closest("dialog")) { e.preventDefault(); return; }
    e.preventDefault(); const w = where(e.target); off();
    if (!w) return toast("Open the Library or a folder first, then drop the PDFs there.");
    handle(e.dataTransfer.files, w);
  });
}

window.DropImport = {init(c) { ctx = c; }, handle, where, choosePdfs, pickFromLibrary};
wire();
