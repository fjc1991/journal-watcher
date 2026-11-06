// A PDF you downloaded yourself (the app's download failed, so you opened the paper in your browser): drop it on that paper's card in a run, or press
// "Add the PDF I downloaded…" on the card or in its drawer. The server checks it is this paper, as a download is checked, and files it under the same name
// and folder as a download (pipeline.attach_pdf). A PDF that does not look like the paper is kept aside until you say it is the right one, or cancel.
// No AI call: its summary is asked for in the Library, with the cost shown first.
import {$, esc, api, toast} from "./state.js";

const LIMIT = 150 * 1024 * 1024;
const BUSY = new Set();                       // papers whose PDF is being sent (a second drop on the same card waits for the first)
const isPdf = f => /\.pdf$/i.test(f.name || "") || f.type === "application/pdf";
const cardOf = id => document.querySelector(`#papers .paper[data-id="${CSS.escape(id)}"]`);
const hasFiles = e => [...(e.dataTransfer && e.dataTransfer.types || [])].includes("Files");
const cap = s => (s = String(s || "")) && s[0].toUpperCase() + s.slice(1);
const SUMMARY_LATER = "Its summary is not written by itself: Library → “Summarise N papers…” says what it costs first.";

export async function send(file, id) {
  if (!file) return;
  if (!isPdf(file)) return toast(`Only a PDF can be added to a paper: “${file.name}” is not one.`);
  if (!file.size || file.size > LIMIT) return toast("The file is empty or larger than 150 MB, so it cannot be added.");
  if (!window.RunView || !RunView.paper(id)) return toast("That paper is not in this run any more: reload the page.");
  if (BUSY.has(id)) return;
  BUSY.add(id); const c = cardOf(id); if (c) c.classList.add("at-busy");
  toast(`Checking “${file.name}” against this paper…`);
  try {
    const r = await fetch("/api/run/attach", {method: "POST", body: file, headers: {"Content-Type": "application/pdf", "X-Run": encodeURIComponent(RunView.runId() || ""),
      "X-Paper": encodeURIComponent(id), "X-Filename": encodeURIComponent(file.name)}});
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || r.statusText);
    if (j.filed) done(id, j); else await refused(id, file, j);
  } catch (e) { toast(e.message); }
  finally { BUSY.delete(id); const c2 = cardOf(id); if (c2) c2.classList.remove("at-busy"); }
}

function done(id, j) {
  RunView.applied(id, j.paper);
  const folder = j.folder ? ` It is in the folder “${j.folder}”.` : "";
  toast(j.existing ? "You already have this paper in your library, so nothing was added twice: the card now points to your copy."
    : j.kept ? "Added to your library, as you said it is the right paper. " + SUMMARY_LATER + folder
    : j.unverified ? `Added to your library. The app could not check that it is this paper (${j.unverified}), so the card says “Identity not checked”.` + folder
    : (j.was_deleted ? "It is back in your library (you had deleted it). " : "Added to your library: it is this paper. ") + SUMMARY_LATER + folder);
}

// the PDF did not pass the check: say what it seems to be and why; nothing is filed unless you say it is the right paper
function refused(id, file, j) {
  return new Promise(resolve => {
    const old = $("#at-dlg"); if (old) old.remove();
    const d = document.createElement("dialog"); d.id = "at-dlg"; d.className = "card di-dlg"; d.setAttribute("aria-labelledby", "at-h");
    d.innerHTML = `<h2 id="at-h">This PDF does not look like this paper</h2>
      <p>You are adding “${esc(file.name)}” to “${esc(j.title)}”.</p>
      <p>${j.seems ? `The PDF seems to be: “${esc(j.seems)}”` : "The app could not tell what the PDF is."}</p>
      <p>Why: ${esc(cap(j.why))}.</p>
      <p class="pmeta">This is the same check that refuses a wrong PDF when the app downloads. Nothing has been added yet.</p>
      <div class="row di-btns" style="margin-top:12px;justify-content:flex-end"><button class="btn" id="at-no">Cancel</button><button class="btn primary" id="at-yes">It is the right paper</button></div>`;
    document.body.append(d); d.showModal(); $("#at-no", d).focus();
    let keeping = false;
    const discard = () => api("/api/import/discard", {tokens: [j.token]}).catch(() => {});
    d.addEventListener("cancel", e => { if (keeping) e.preventDefault(); });          // (Esc while it is being filed: wait for the answer)
    d.addEventListener("close", () => { if (!keeping) discard(); d.remove(); resolve(); }, {once: true});
    $("#at-no", d).onclick = () => d.close();
    $("#at-yes", d).onclick = async () => {
      keeping = true; $("#at-yes", d).disabled = $("#at-no", d).disabled = true;
      try { done(id, await api("/api/run/attach/keep", {run_id: RunView.runId(), id, token: j.token})); }
      catch (e) { toast(e.message); if (!/no longer waiting/.test(e.message)) discard(); }
      d.close();
    };
  });
}

// "Add the PDF I downloaded…": the file chooser opens during your click (a browser opens it only then)
export function pick(id) {
  const i = document.createElement("input"); i.type = "file"; i.accept = "application/pdf,.pdf"; i.style.display = "none"; document.body.append(i);
  i.onchange = () => { const f = i.files && i.files[0]; setTimeout(() => i.remove(), 0); if (f) send(f, id); };
  i.click();
}

// a drop on a paper's card: taken here, before the page-wide import (dropimport.js) sees it (capture phase; it skips events whose default was prevented)
function wire() {
  let over = null;
  const unmark = () => { if (over) over.classList.remove("at-over"); over = null; };
  const cardAt = e => e.target && e.target.closest ? e.target.closest("#papers .paper") : null;
  document.addEventListener("dragover", e => {
    if (!hasFiles(e)) return;
    const c = cardAt(e); if (!c) { unmark(); return; }
    e.preventDefault(); if (window.DropImport && DropImport.off) DropImport.off();
    if (c.classList.contains("can-attach")) { e.dataTransfer.dropEffect = "copy"; if (over !== c) { unmark(); over = c; c.classList.add("at-over"); } }
    else { e.dataTransfer.dropEffect = "none"; unmark(); }
  }, true);
  document.addEventListener("dragleave", e => { if (over && !(e.relatedTarget && over.contains(e.relatedTarget))) unmark(); }, true);
  document.addEventListener("dragend", unmark, true);
  document.addEventListener("drop", e => {
    if (!hasFiles(e)) return;
    const c = cardAt(e); if (!c) return;
    e.preventDefault(); e.stopPropagation(); unmark();
    const id = c.dataset.id, p = window.RunView && RunView.paper(id), fs = [...e.dataTransfer.files];
    if (!c.classList.contains("can-attach"))
      return toast(p && p.dl_status === "downloading" ? "This paper is being downloaded right now. Wait until it is done; if it does not come, add your PDF then." : "This paper is already in your library.");
    if (fs.length !== 1) return toast(`Drop one PDF on a paper (you dropped ${fs.length} files).`);
    send(fs[0], id);
  }, true);
}
wire();
window.AttachPDF = {pick, send};
