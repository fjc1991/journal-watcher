// Import references you already have: a bibliography (EndNote / Mendeley / Zotero .ris, Overleaf / Mendeley .bib) or a document that cites papers (Word, PowerPoint,
// PDF, plain or pasted text: a syllabus, a reading list, a paper's reference list). You see which references you already have; the ones you tick are fetched through
// the usual download routes (open access first, then your sign-ins and your library) and go where you choose: the library only, one of your folders, or a new folder.
// A document's references are found by their shape and looked up on Crossref (free, no AI); the AI can be asked to list them too, with its cost shown first.
// Server: POST /api/bib/parse, POST /api/refs/extract (streamed), POST /api/related/download, POST /api/folders.
import {$, esc, api, toast, notifyError, progress, usd} from "./state.js";

let ctx = null, d = null;
const B = {name: "", entries: [], sel: new Set(), only: true, mode: "bib", file: null, ai: null, aiDone: false, to: "lib", toName: ""};
const CAP = 40;
const auth = e => e.authors.length > 3 ? e.authors.slice(0, 3).map(a => a.split(" ").pop()).join(", ") + " et al." : e.authors.map(a => a.split(" ").pop()).join(", ");
const stem = n => String(n || "").replace(/\.[^.]+$/, "").slice(0, 60);
const canTick = e => !e.file && e.status !== "book" && !!e.title;                       // (a book is not downloaded; a reference nothing could be read from has no title to look for)
const autoTick = e => canTick(e) && (!e.status || e.status === "found");                  // (an unsure match or one Crossref does not know: yours to tick)
const keyOf = e => e.doi ? "d:" + e.doi.toLowerCase() : "t:" + (e.title || e.ref || "").toLowerCase().replace(/[^a-z0-9]+/g, "").slice(0, 120);
const isBib = (name, text) => /\.(ris|bib|bibtex)$/i.test(name) || /^\s*@\w+\s*[{(]/m.test(text.slice(0, 20000)) || /^TY\s{1,2}-/m.test(text.slice(0, 20000));

export function open() {
  if (d) d.remove();
  d = document.createElement("dialog"); d.id = "bibdlg"; d.className = "card adddlg bibdlg";
  d.innerHTML = `<h2>Import references</h2><p class="pmeta">A document that cites papers (<b>Word</b>, <b>PowerPoint</b>, <b>PDF</b> or text: a syllabus, a reading list, a reference list) or a bibliography from EndNote, Mendeley, Zotero or Overleaf (<b>.ris</b>, <b>.bib</b>). You will see which references you already have; the ones you tick are fetched through your usual download routes: free copies first, then your publisher sign-ins and your library.</p>
    <div class="dropzone" id="bd-drop">Drop a file here or <a href="#" id="bd-pick">choose a file</a> · <a href="#" id="bd-paste">paste a reference list</a><input type="file" id="bd-file" accept=".docx,.pptx,.pdf,.txt,.md,.ris,.bib,.bibtex,text/plain,application/pdf" class="hide"></div>
    <div id="bd-pastebox" class="hide"><textarea id="bd-text" rows="6" style="width:100%" placeholder="Paste references here, one per line or paragraph" aria-label="References to read"></textarea><div class="row" style="justify-content:flex-end;margin-top:6px"><button class="btn small" id="bd-read">Read these</button></div></div>
    <div id="bd-body"></div>
    <div class="row" style="justify-content:flex-end;margin-top:10px"><button class="btn" id="bd-close">Close</button></div>`;
  document.body.append(d); d.showModal();
  $("#bd-close", d).onclick = () => d.close();
  $("#bd-pick", d).onclick = e => { e.preventDefault(); $("#bd-file", d).click(); };
  $("#bd-paste", d).onclick = e => { e.preventDefault(); $("#bd-pastebox", d).classList.toggle("hide"); $("#bd-text", d).focus(); };
  $("#bd-read", d).onclick = () => { const t = $("#bd-text", d).value.trim(); if (t.length < 20) return toast("Paste at least one reference first."); read(new File([t], "pasted text.txt", {type: "text/plain"}), "pasted text"); };
  $("#bd-file", d).onchange = e => e.target.files[0] && read(e.target.files[0]);
  const dz = $("#bd-drop", d); dz.ondragover = e => { e.preventDefault(); dz.classList.add("over"); }; dz.ondragleave = () => dz.classList.remove("over");
  dz.ondrop = e => { e.preventDefault(); e.stopPropagation(); dz.classList.remove("over"); const f = e.dataTransfer.files[0]; if (f) read(f); };
  d.addEventListener("click", onClick); d.addEventListener("change", onChange); d.addEventListener("input", onInput);
  if (B.entries.length) draw();                   // (the references of the last file you read that were not fetched yet: forty go at a time)
}

// a document: the server reads it and looks each reference up; it tells how far it is, line by line
async function stream(file, name, mode, box) {
  const r = await fetch("/api/refs/extract", {method: "POST", body: file, headers: {"Content-Type": "application/octet-stream", "X-Filename": encodeURIComponent(name), ...(mode ? {"X-Mode": mode} : {})}});
  if (!r.ok || !r.body) throw new Error(((await r.json().catch(() => ({}))).error) || r.statusText);
  const rd = r.body.getReader(), dec = new TextDecoder(); let buf = "", done = null;
  for (;;) {
    const {value, done: fin} = await rd.read(); if (fin) break; buf += dec.decode(value, {stream: true}); let i;
    while ((i = buf.indexOf("\n")) >= 0) { const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1); if (!line) continue; const m = JSON.parse(line);
      if (m.type === "status") progress(box, m); else if (m.type === "error") throw new Error(m.error); else if (m.type === "done") done = m; }
  }
  if (!done) throw new Error("The document could not be read to its end. Try again.");
  return done;
}
function take(entries) { B.entries = entries; B.only = true; B.sel = new Set(entries.map((e, i) => (autoTick(e) ? i : -1)).filter(i => i >= 0).slice(0, CAP)); }
async function read(file, shown) {
  const body = $("#bd-body", d), name = shown || file.name; body.innerHTML = `<div class="pmeta" id="bd-prog">Reading ${esc(name)}…</div>`;
  try {
    const small = file.size < 8_000_000 && !/\.(docx|pptx|pdf)$/i.test(file.name), text = small ? await file.text() : "";
    if (small && isBib(file.name, text)) {
      const r = await api("/api/bib/parse", {text, name: file.name});
      B.mode = "bib"; B.file = null; B.ai = null; B.aiDone = false; B.name = name; B.toName = stem(name); take(r.entries);
    } else {
      const r = await stream(file, name, "", $("#bd-prog", d));
      B.mode = "doc"; B.file = file; B.ai = r.ai || null; B.aiDone = false; B.name = name; B.toName = stem(name); take(r.entries);
    }
    draw();
  } catch (e) { body.innerHTML = `<div class="of-co warn"><div>${esc(e.message)}</div></div>`; }
}
async function askAI(btn) {
  btn.disabled = true; const box = $("#bd-aiprog", d);
  try {
    const r = await stream(B.file, B.name, "ai", box), have = new Set(B.entries.map(keyOf)), fresh = r.entries.filter(e => !have.has(keyOf(e)));
    fresh.forEach(e => { B.entries.push(e); if (autoTick(e) && B.sel.size < CAP) B.sel.add(B.entries.length - 1); });
    B.aiDone = true; draw();
    toast(fresh.length ? `The AI found ${fresh.length} more reference${fresh.length === 1 ? "" : "s"}${r.cost != null ? " (" + usd(r.cost) + ")" : ""}.` : `The AI found no reference that was not already listed${r.cost != null ? " (" + usd(r.cost) + ")" : ""}.`);
  } catch (e) { box.innerHTML = `<span class="pmeta bad">${esc(e.message)}</span>`; btn.disabled = false; }
}
const BADGE = {found: e => `<span class="of-badge" title="Found on Crossref: ${esc(e.why || "")}">${esc(e.doi)}</span>`,
  unsure: e => `<span class="of-badge inferred" title="${esc(e.why || "")}. Tick it only if this is the paper the document means.">not sure it is this one</span>`,
  book: e => `<span class="of-badge inferred" title="${esc(e.why || "")}">a book: not downloaded</span>`,
  none: e => `<span class="of-badge inferred" title="${esc(e.why || "")}">${e.unasked ? "Crossref did not answer" : "not on Crossref"}</span>`};
function draw() {
  const body = $("#bd-body", d), E = B.entries, doc = B.mode === "doc", todo = E.filter(e => !e.file).length, have = E.filter(e => e.file).length, shown = E.map((e, i) => [e, i]).filter(([e]) => !B.only || !e.file);
  const folders = (window.LibraryUI && window.LibraryUI.folders && window.LibraryUI.folders()) || [];
  const n = k => E.filter(e => e.status === k).length, noDoi = E.filter(e => !e.file && !e.doi).length, unasked = E.filter(e => e.unasked).length;
  const sum = doc ? `<b>${E.length}</b> reference${E.length === 1 ? "" : "s"} found in ${esc(B.name)} · <b>${n("found") + n("book")}</b> on Crossref${n("unsure") ? ` · <b>${n("unsure")}</b> not sure` : ""}${n("none") ? ` · <b>${n("none")}</b> not on Crossref` : ""} · <b>${have}</b> already in your library`
    : `<b>${E.length}</b> reference${E.length === 1 ? "" : "s"} in ${esc(B.name)} · <b>${have}</b> already in your library · <b>${todo}</b> you don’t have${noDoi ? ` (${noDoi} without a DOI: harder to find)` : ""}`;
  const badge = e => e.file ? `<span class="of-badge">in your library</span>` : e.status ? BADGE[e.status](e) : e.doi ? `<span class="of-badge">${esc(e.doi)}</span>` : `<span class="of-badge inferred">no DOI</span>`;
  const toFolder = B.to !== "lib", nHave = toFolder ? have : 0, nSel = B.sel.size;
  const goText = nSel ? `Fetch ${nSel} paper${nSel === 1 ? "" : "s"}${nHave ? ` and put the ${nHave} you have in the folder` : ""}` : nHave ? `Put the ${nHave} you have in the folder` : "Fetch";
  const ai = B.ai, aiRow = doc && B.file && !B.aiDone ? `<div class="row bd-act" id="bd-airow"><span class="pmeta">${E.length ? "Missing some?" : "Nothing was recognised by its shape."} The AI can read the document and list its references too (its text is sent to ${esc((ai && ai.provider) || "your AI provider")}${ai && ai.cut ? "; it is long, so only its first part" : ""}); each is still checked on Crossref.</span>
      <button class="btn small" data-bd="ai">Ask the AI${ai && ai.cost != null ? " (" + usd(ai.cost) + ")" : " (cost unknown: this model has no price)"}</button><span id="bd-aiprog"></span></div>` : "";
  body.innerHTML = `<div class="bd-sum">${sum}${unasked ? ` · <span class="bad">Crossref did not answer for ${unasked}</span> <button class="linkbtn" data-bd="again">read it again</button>` : ""} <button class="linkbtn" data-bd="clear" title="Forget this list">start over</button></div>
    ${E.length ? `<div class="row bd-bar"><label class="chk"><input type="checkbox" id="bd-only"${B.only ? " checked" : ""}> only the ones I don’t have</label><button class="linkbtn" data-bd="all">tick all</button> · <button class="linkbtn" data-bd="none">none</button><span class="pmeta">${nSel} ticked</span></div>
    <div class="bd-list">${shown.map(([e, i]) => `<label class="bd-r${e.file ? " have" : ""}${e.status && e.status !== "found" ? " weak" : ""}"><input type="checkbox" data-i="${i}"${B.sel.has(i) ? " checked" : ""}${canTick(e) ? "" : " disabled"}><span><b>${esc(e.title || e.ref || "")}</b><br><small>${esc(auth(e))}${e.year ? " · " + esc(e.year) : ""}${e.journal ? " · " + esc(e.journal) : ""}</small>
      ${badge(e)}${e.ref && e.status && e.status !== "found" && e.title ? `<br><small class="pmeta">In the document: ${esc(e.ref.slice(0, 200))}</small>` : ""}</span></label>`).join("") || `<div class="pmeta">Nothing to show.</div>`}</div>
    <div class="row bd-act"><label for="bd-to">Put them in</label><select id="bd-to"><option value="lib"${B.to === "lib" ? " selected" : ""}>Library only (no folder)</option>${folders.map(f => `<option value="f:${esc(f.id)}"${B.to === "f:" + f.id ? " selected" : ""}>Folder: ${esc(f.name)}</option>`).join("")}<option value="new"${B.to === "new" ? " selected" : ""}>New folder…</option></select>
      <input id="bd-toname" class="${B.to === "new" ? "" : "hide"}" maxlength="60" value="${esc(B.toName)}" placeholder="Name of the new folder" aria-label="Name of the new folder"></div>
    <div class="row bd-act"><button class="btn primary" data-bd="go"${nSel || nHave ? "" : " disabled"}>${goText}</button><span class="pmeta">${nSel >= CAP ? "Forty at a time; the rest stay in this list: open Import references again for them." : toFolder ? "Each paper goes into the folder once it is in your library." : ""}</span></div>` : ""}
    ${aiRow}`;
}
function onInput(e) { if (e.target.id === "bd-toname") B.toName = e.target.value; }
function onChange(e) {
  const t = e.target;
  if (t.id === "bd-only") { B.only = t.checked; return draw(); }
  if (t.id === "bd-to") { B.to = t.value; draw(); if (B.to === "new") { const nm = $("#bd-toname", d); nm.focus(); nm.select(); } return; }
  if (t.matches("[data-i]")) { const i = +t.dataset.i; if (t.checked) { if (B.sel.size >= CAP) { t.checked = false; return toast("Forty at a time: fetch these first."); } B.sel.add(i); } else B.sel.delete(i); draw(); }
}
async function folderId() {                                  // the folder the papers go to (made now if it is a new one); "" = the library only
  if (B.to === "lib") return "";
  if (B.to !== "new") return B.to.slice(2);
  const name = B.toName.trim(); if (!name) throw new Error("Give the new folder a name.");
  const r = await api("/api/folders", {action: "create", name}), want = name.replace(/\s+/g, " ").toLowerCase(), f = r.folders.find(x => x.name.toLowerCase() === want);
  if (!f) throw new Error("The folder could not be made.");
  B.to = "f:" + f.id; if (window.LibraryUI) await LibraryUI.refreshFolders();
  return f.id;
}
async function onClick(e) {
  const b = e.target.closest("[data-bd]"); if (!b) return; const a = b.dataset.bd;
  if (a === "all") { B.entries.forEach((en, i) => { if (canTick(en) && B.sel.size < CAP) B.sel.add(i); }); return draw(); }
  if (a === "none") { B.sel.clear(); return draw(); }
  if (a === "clear") { B.entries = []; B.sel.clear(); B.file = null; $("#bd-body", d).innerHTML = ""; return; }
  if (a === "again") { if (B.file) read(B.file, B.name); return; }
  if (a === "ai") return askAI(b);
  try {
    if (a === "go") {
      b.disabled = true;
      const picks = [...B.sel].map(i => B.entries[i]), label = "Imported: " + stem(B.name).slice(0, 40), fid = await folderId(), haveFiles = fid ? B.entries.filter(en => en.file).map(en => en.file) : [];
      if (haveFiles.length) await api("/api/folders", {action: "add", id: fid, files: haveFiles});
      const fname = fid ? ((window.LibraryUI.folders() || []).find(f => f.id === fid) || {}).name || "the folder" : "";
      if (!picks.length) {
        toast(`${haveFiles.length} paper${haveFiles.length === 1 ? "" : "s"} you already have put in “${fname}”.`); await ctx.reload(); window.LibraryUI && window.LibraryUI.refreshView && window.LibraryUI.refreshView(); return draw();
      }
      const r = await api("/api/related/download", {items: picks.map(en => ({id: en.doi || en.title.toLowerCase().slice(0, 60), doi: en.doi, title: en.title, authors: en.authors, year: en.year, venue: en.journal, reason: "imported from " + B.name})), label, folder: fid});
      const had = r.have && r.have.length ? r.have.length : 0;
      toast(r.n ? `Fetching ${r.n} paper${r.n === 1 ? "" : "s"}${fid ? ` into “${fname}”` : ""}${had ? ` (${had} ${had === 1 ? "was" : "were"} already in your library)` : ""}…`
        : `All ${had} were already in your library${fid ? `: now in “${fname}”` : ""}.`);
      const gone = new Set(B.sel); B.entries = B.entries.filter((en, i) => !gone.has(i)); B.sel.clear(); d.close();
      if (r.run && ctx.openRun) ctx.openRun(r.run); else if (ctx.reload) await ctx.reload();
      return;
    }
  } catch (er) { notifyError(er); if (b) b.disabled = false; }
}

window.BibImport = {init(c) { ctx = c; }, open};
