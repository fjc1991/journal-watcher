// A paper's details after it is in the library: Edit details (one paper) and Read details from the PDFs (many).
//   · Edit details: title, the authors with affiliation, place and e-mail, DOI (for a paper filed without one), the conference in full, the kind of submission, the session,
//     volume / issue / pages, keywords, abstract and your own note. "Read from the PDF" fills the boxes from the first page; nothing is kept until Save.
//   · Read details from the PDFs: each paper's first page is read (pdfmeta.py; the AI helps where the rules are unsure, if you leave that on) and what it would add is listed.
//     Nothing is changed until you press Apply, and only for the papers you leave ticked.
// Your corrections are kept beside the PDF (/api/paper/details); the paper stays where it is filed, in its folders and with its reading status.
import {notifyError, $, $$, esc, api, toast} from "./state.js";

const norm = s => String(s || "").replace(/\s+/g, " ").trim();
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
const FIELDS = [["conference", "Conference or workshop, in full", "wide"], ["paper_type", "Kind of submission", ""], ["session", "Session", ""], ["volume", "Volume", ""], ["issue", "Issue", ""], ["pages", "Pages", ""]];
const personRow = (p, k) => `<div class="dt-person" data-row="${k}"><input type="text" data-pf="name" value="${esc(p.name || "")}" placeholder="Name" aria-label="Author ${k + 1}: name"><input type="text" data-pf="affiliation" value="${esc(p.affiliation || "")}" placeholder="Affiliation" aria-label="Author ${k + 1}: affiliation">
  <input type="text" data-pf="place" value="${esc(p.place || "")}" placeholder="City, country" aria-label="Author ${k + 1}: city, country"><input type="text" data-pf="email" value="${esc(p.email || "")}" placeholder="E-mail" aria-label="Author ${k + 1}: e-mail">
  <button type="button" class="btn small" data-up title="Move up" aria-label="Move author ${k + 1} up">↑</button><button type="button" class="btn small" data-rm title="Remove" aria-label="Remove author ${k + 1}">×</button></div>`;

// what a first page's reading holds, in words
export function foundSay(f) {
  const n = (f.authors || []).length, aff = (f.people || []).filter(x => x.affiliation || x.email).length;
  return [n ? plural(n, "author") + (aff ? ` (${aff === n ? "all" : aff} with affiliation or e-mail)` : "") : "", f.conference ? "conference: " + f.conference : "", f.paper_type || "", (f.keywords || []).length ? plural(f.keywords.length, "keyword") : "", f.abstract ? "abstract" : ""].filter(Boolean).join(" · ");
}

export async function editDetails(file, done) {
  let D; try { D = await api("/api/paper/details?file=" + encodeURIComponent(file)); } catch (e) { return notifyError(e); }
  $("#dt-dlg") && $("#dt-dlg").remove();
  const d = document.createElement("dialog"); d.id = "dt-dlg"; d.className = "card ir-dlg dt-dlg"; d.setAttribute("aria-labelledby", "dt-h");
  const det = D.details || {}, filedDoi = D.index.doi;
  d.innerHTML = `<h2 id="dt-h">Edit details</h2>
    <p class="pmeta">Filed under <b>${esc(D.journal)}</b>${D.year ? " · " + esc(D.year) : ""}. The file stays where it is, in its folders and with your notes; what you change here is kept beside it.</p>
    <div class="ir-list"><section class="ir-card">
      <label class="ir-f wide"><span>Title</span><input type="text" id="dt-title" value="${esc(D.title)}" autocomplete="off"></label>
      <div class="ir-st ir-hint hide" id="dt-tsug"></div>
      <div class="dt-h3">Authors</div><div class="dt-people" id="dt-people">${D.people.map(personRow).join("")}</div>
      <button type="button" class="linkbtn" id="dt-add">+ Add an author</button>
      <div class="ir-fields ir-dfields" style="margin-top:10px">
        <label class="ir-f wide"><span>DOI${filedDoi ? " (the paper is filed under it: it cannot be changed here)" : " (if it has one)"}</span><input type="text" id="dt-doi" value="${esc(D.doi)}" placeholder="10.…"${filedDoi ? " disabled" : ""} autocomplete="off" spellcheck="false"></label>
        ${FIELDS.map(([k, label, cls]) => `<label class="ir-f ${cls}"><span>${label}</span><input type="text" data-d="${k}" value="${esc(det[k] || "")}" autocomplete="off"></label>`).join("")}
        <label class="ir-f wide"><span>Keywords (separate with ;)</span><input type="text" data-d="keywords" value="${esc((det.keywords || []).join("; "))}" autocomplete="off"></label>
        <label class="ir-f wide"><span>Abstract</span><textarea data-d="abstract" rows="6">${esc(det.abstract || "")}</textarea></label>
        <label class="ir-f wide"><span>Your note (only for you)</span><textarea data-d="note" rows="2">${esc(det.note || "")}</textarea></label></div>
    </section></div>
    <div class="ir-foot"><div class="ir-st ir-hint" id="dt-read" role="status"></div><div class="ir-err" id="dt-err" role="alert"></div>
      <div class="row di-btns"><button class="btn" id="dt-pdf" title="Read the authors, affiliations, conference, abstract and keywords from the PDF’s first page into these boxes. Nothing is kept until you press Save.">Read from the PDF</button><span style="flex:1"></span><button class="btn" id="dt-no">Cancel</button><button class="btn primary" id="dt-go">Save</button></div></div>`;
  document.body.append(d); d.showModal();
  const box = $("#dt-people", d);
  const people = () => $$(".dt-person", box).map(r => Object.fromEntries($$("[data-pf]", r).map(i => [i.dataset.pf, norm(i.value)]))).filter(p => p.name);
  const setPeople = ps => { box.innerHTML = ps.map(personRow).join(""); };
  $("#dt-add", d).onclick = () => { box.insertAdjacentHTML("beforeend", personRow({}, $$(".dt-person", box).length)); const rows = $$(".dt-person", box); $("[data-pf=name]", rows[rows.length - 1]).focus(); };
  box.onclick = e => {
    const row = e.target.closest(".dt-person"); if (!row) return;
    if (e.target.closest("[data-rm]")) row.remove();
    else if (e.target.closest("[data-up]") && row.previousElementSibling) row.previousElementSibling.before(row);
  };
  $("#dt-pdf", d).onclick = async e => {
    const b = e.currentTarget, say = $("#dt-read", d); b.disabled = true; say.textContent = "Reading the first page…";
    try {
      const r = await api("/api/paper/details/read", {file, ai: true}), f = r.found;
      if (f.people.length) setPeople(f.people);
      for (const k of ["conference", "paper_type", "abstract"]) { const el = $(`[data-d="${k}"]`, d); if (el && !norm(el.value) && f[k]) el.value = f[k]; }
      const kw = $('[data-d="keywords"]', d); if (kw && !norm(kw.value) && (f.keywords || []).length) kw.value = f.keywords.join("; ");
      const sug = $("#dt-tsug", d), cur = norm($("#dt-title", d).value);
      if (f.title && norm(f.title).toLowerCase() !== cur.toLowerCase()) {
        sug.innerHTML = `The first page’s title is “${esc(f.title)}”. <button type="button" class="linkbtn" id="dt-tuse">Use it</button>`; sug.classList.remove("hide");
        $("#dt-tuse", d).onclick = () => { $("#dt-title", d).value = f.title; sug.classList.add("hide"); };
      } else sug.classList.add("hide");
      say.textContent = (foundSay(f) ? "From the first page: " + foundSay(f) + "." : "The first page gave nothing to add.") + (f.sure && f.sure !== "high" && f.people.length ? " Please check the names." : "") + (f.ai_note ? " The AI was not used: " + f.ai_note : "") + " Nothing is kept until you press Save.";
    } catch (er) { say.textContent = ""; $("#dt-err", d).textContent = er.message; }
    b.disabled = false;
  };
  $("#dt-no", d).onclick = () => d.close();
  $("#dt-go", d).onclick = async () => {
    const fields = {title: norm($("#dt-title", d).value), authors: people()};
    $$("[data-d]", d).forEach(el => { fields[el.dataset.d] = el.value; });
    if (!filedDoi) fields.doi = norm($("#dt-doi", d).value);
    if (!fields.title) { $("#dt-err", d).textContent = "Give the paper a title."; return $("#dt-title", d).focus(); }
    try { await api("/api/paper/details", {file, fields}); d.close(); toast("Details saved."); done && (await done()); }
    catch (er) { $("#dt-err", d).textContent = er.message; }
  };
  d.addEventListener("close", () => d.remove(), {once: true});
}

// Read the first page of each of these papers and list what it would add; Apply writes it for the ticked ones.
export async function readMany(files, done) {
  files = [...files]; if (!files.length) return;
  $("#dr-dlg") && $("#dr-dlg").remove();
  const d = document.createElement("dialog"); d.id = "dr-dlg"; d.className = "card ir-dlg dt-dlg"; d.setAttribute("aria-labelledby", "dr-h");
  d.innerHTML = `<h2 id="dr-h">Read details from ${plural(files.length, "PDF")}</h2>
    <p class="pmeta">Each paper’s first page is read for its authors (with affiliation and e-mail), conference, kind of submission, abstract and keywords. Nothing is changed until you press Apply, and only for the papers you leave ticked. What you typed yourself is kept: only empty details are filled, and the author list is replaced only where the page gives more.</p>
    <label class="chk"><input type="checkbox" id="dr-ai" checked> Where the rules are not sure who wrote a paper, let the AI read its first page (a fraction of a cent each, counted in your monthly budget; never when it is switched off in Settings → Reader & summaries)</label>
    <div class="row di-btns" id="dr-start"><button class="btn" id="dr-no0">Cancel</button><button class="btn primary" id="dr-run">Read</button></div>
    <div class="ir-st ir-hint hide" id="dr-prog" role="status"></div>
    <div class="ir-list hide" id="dr-list"></div>
    <div class="ir-foot hide" id="dr-foot"><div class="ir-err" id="dr-err" role="alert"></div><div class="row di-btns"><button class="btn" id="dr-no">Close</button><button class="btn primary" id="dr-go" disabled>Apply</button></div></div>`;
  document.body.append(d); d.showModal();
  let closed = false; const out = [];
  d.addEventListener("close", () => { closed = true; d.remove(); }, {once: true});
  $("#dr-no0", d).onclick = () => d.close(); $("#dr-no", d).onclick = () => d.close();
  const count = () => { const n = $$("[data-use]:checked", d).length; $("#dr-go", d).disabled = !n; $("#dr-go", d).textContent = n ? "Apply to " + plural(n, "paper") : "Apply"; };
  d.addEventListener("change", e => { if (e.target.dataset.use !== undefined) count(); });
  $("#dr-run", d).onclick = async () => {
    const ai = $("#dr-ai", d).checked; $("#dr-start", d).classList.add("hide"); $("#dr-ai", d).disabled = true;
    const prog = $("#dr-prog", d), list = $("#dr-list", d); prog.classList.remove("hide"); list.classList.remove("hide"); $("#dr-foot", d).classList.remove("hide");
    let cost = 0, asked = 0;
    for (let i = 0; i < files.length && !closed; i++) {
      prog.textContent = `Reading ${i + 1} of ${files.length}…`;
      let r; try { r = await api("/api/paper/details/read", {file: files[i], ai}); } catch (er) { r = {file: files[i], title: files[i].split("/").pop(), error: er.message, changes: [], found: {}}; }
      if (closed) return;
      out.push(r); const f = r.found || {}, ch = r.changes || [], k = out.length - 1;
      if (f.ai) { asked++; cost += Number(f.ai.cost) || 0; }
      const adds = ch.filter(x => x !== "title");
      list.insertAdjacentHTML("beforeend", `<section class="ir-card dr-row"><div class="ir-h"><label class="ir-inc"><input type="checkbox" data-use="${k}"${adds.length ? " checked" : " disabled"} aria-label="Apply to ${esc(r.title)}"><b>${esc(r.title)}</b></label></div>
        <div class="ir-st ${r.error ? "bad" : adds.length ? "ok" : "ir-hint"}">${r.error ? esc(r.error) : adds.length ? "Would add: " + esc(foundSay(Object.fromEntries(Object.entries(f).filter(([key]) => ch.includes(key) || (key === "people" && ch.includes("authors")))))) : "Nothing to add: the library already has what the first page says" + (f.authors && !f.authors.length ? " (no author could be read)" : "") + "."}${f.sure && f.sure !== "high" && ch.includes("authors") ? " Please check the names." : ""}${f.ai_note ? " The AI was not used: " + esc(f.ai_note) : ""}</div>
        ${ch.includes("authors") ? `<div class="pmeta">${esc(f.authors.join("; "))}</div>` : ""}
        ${ch.includes("title") ? `<label class="chk"><input type="checkbox" data-title="${k}"> Also use the first page’s title: “${esc(f.title)}”</label>` : ""}</section>`);
      count();
    }
    if (!closed) prog.textContent = `Read ${plural(out.length, "paper")}.` + (asked ? ` The AI read ${plural(asked, "first page")}${cost ? " ($" + cost.toFixed(4) + ")" : ""}.` : "");
  };
  $("#dr-go", d).onclick = async () => {
    const go = $("#dr-go", d); go.disabled = true; let n = 0; const bad = [];
    for (const el of $$("[data-use]:checked", d)) {
      const r = out[+el.dataset.use], f = r.found, ch = r.changes, fields = {};
      if (ch.includes("authors")) fields.authors = f.people;
      for (const k of ["abstract", "keywords", "conference", "paper_type"]) if (ch.includes(k)) fields[k] = f[k];
      const t = $(`[data-title="${el.dataset.use}"]`, d); if (t && t.checked) fields.title = f.title;
      try { await api("/api/paper/details", {file: r.file, fields}); n++; el.checked = false; el.disabled = true; } catch (er) { bad.push(r.title + ": " + er.message); }
    }
    $("#dr-err", d).textContent = bad.join(" · "); toast(`Details saved for ${plural(n, "paper")}.`); count();
    done && (await done());
    if (!bad.length) d.close();
  };
}
