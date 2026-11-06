// Drop PDFs you already have onto the app. Where you drop decides what happens:
//   · on a folder (a tile on the Folders page, or a folder chip in the Library) -> they go straight into that folder
//   · anywhere inside an open folder                                           -> they go into that folder
//   · on the Folders page, away from the tiles                                 -> a small chooser: new folder / an existing folder / library only
//   · anywhere else in the Library                                             -> the same chooser, "library only" preselected
// Right after the drop ONE dialog shows every PDF as a card: the records found online (Crossref) for what the PDF says about itself, with the details in boxes you can correct, and
// where it will go (fixed when you dropped on a folder, a choice otherwise). Only after you press Add is each PDF filed under Papers/YYYY-MM/<Journal>/ (/api/import/confirm) and added
// to the folder. A paper you already have is not imported twice, only added. The result appears as a snackbar with Open folder / Undo.
import {notifyError, $, $$, esc, api, toast} from "./state.js";

let ctx = null, chain = Promise.resolve();
const D = {overlay: null, snack: null, timer: 0};
const ls = {get: k => { try { return localStorage.getItem(k) || ""; } catch (e) { return ""; } }, set: (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* private window */ } }};
const isPdf = f => /\.pdf$/i.test(f.name) || f.type === "application/pdf";
const isEpub = f => /\.epub$/i.test(f.name) || f.type === "application/epub+zip";
const isDoc = f => isPdf(f) || isEpub(f);         // a paper (PDF) or a book (PDF or EPUB)
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
const onRunList = () => visible($("#papers .paper.can-attach"));        // a run's list with papers that can take a PDF you downloaded yourself (attach.js)
function hint(w) {
  if (!w) return onRunList() ? "Drop the PDF on its paper’s card to add it to that paper" : "Drop PDFs on the Library, Folders page or inside a folder to add them";
  if (w.kind === "folder") return w.name ? "Add to “" + w.name + "”" : "Add to this folder";
  if (w.kind === "open") return "Add to this folder";
  return onBooksTab() ? "Drop PDF or EPUB files to add them as books — you’ll check the details first" : "Drop PDFs (or EPUB books) to add them — you’ll check the details and choose where they go";
}
function mark(w) {      // highlight the tile / chip under the cursor
  const el = w && w.kind === "folder" ? $(`[data-drop="${CSS.escape(w.id)}"], [data-dropfolder="${CSS.escape(w.id)}"]`) : null;
  if (el === overFolder) return;
  if (overFolder) overFolder.classList.remove("fp-over", "over");
  if ((overFolder = el)) el.classList.add(el.hasAttribute("data-drop") ? "fp-over" : "over");
}

// ---------------------------------------------------------------- the review: what each PDF says about itself and what the databases know, checked by you before anything is filed
// One dialog right after the drop. Every PDF is sent to /api/import/analyse (it is read, looked up on Crossref and staged, nothing is filed), shown as a card with the records that
// were found, and filed by /api/import/confirm with the details you left in the boxes. Cancelling, or unticking a card, throws the staged copy away.
const dateOk = s => {       // a real calendar date: YYYY, YYYY-MM or YYYY-MM-DD (not 2024-13, not 2024-02-31, nothing after it)
  const m = /^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?$/.exec(s); if (!m) return false;
  const y = +m[1], mo = m[2] ? +m[2] : 1, d = m[3] ? +m[3] : 1, t = new Date(Date.UTC(y, mo - 1, d));
  return y >= 1000 && t.getUTCFullYear() === y && t.getUTCMonth() === mo - 1 && t.getUTCDate() === d;
};
const why = k => k.own ? "The file’s own details (title page / EPUB metadata)" : k.verdict === "mismatch" ? "Does not look like this PDF"      // what backs the record up, in words that say no more than was found
  : k.verdict === "match" ? (k.authors_found ? "Title and authors are on the PDF’s first pages" : k.doi_printed ? "This DOI is printed in the PDF" : "Title found on the PDF; authors not confirmed")
  : "Could not be confirmed against the PDF";
const fieldsOf = (c, kind) => kind === "book" ? {title: c.title || "", authors: (c.authors || []).join("; "), publisher: c.publisher || "", pub_date: c.pub_date || c.year || "", isbn: c.isbn || "", doi: c.doi || ""}
  : {title: c.title || "", authors: (c.authors || []).join("; "), journal: c.journal || "", pub_date: c.pub_date || "", doi: c.doi || ""};
// what kind of paper (kinds.py): its venue box says what to type
const PTYPES = [["journal", "Journal article"], ["preprint", "Preprint or working paper"], ["conference", "Conference paper"], ["other", "Report, thesis or other"]];
const VENUE = {journal: ["Journal", "e.g. Information Systems Research"], preprint: ["Repository or series", "e.g. SSRN, arXiv, NBER Working Papers"],
  conference: ["Conference or proceedings", "e.g. ICIS 2024 Proceedings"], other: ["Source", "e.g. a university, a report series or a book title"]};
const TYPEWORD = {preprint: "preprint", conference: "conference paper", other: "report, thesis or other"};
const ptypeSel = (c, name) => `<label class="ir-type">Type <select data-ptype="${c.i}" aria-label="What kind of paper is ${esc(name)}?">${PTYPES.map(([v, t]) => `<option value="${v}"${c.ptype === v ? " selected" : ""}>${t}</option>`).join("")}</select></label>`;
const blank = (kind, title) => kind === "book" ? {title: title || "", authors: "", publisher: "", pub_date: "", isbn: "", doi: ""} : {title: title || "", authors: "", journal: "", pub_date: "", doi: ""};
const isbnOk = s => {           // an ISBN-10 or ISBN-13 with a correct check digit (empty is fine: not every book has one)
  const d = String(s || "").replace(/[\s-]/g, "").toUpperCase(); if (!d) return true;
  if (/^(978|979)\d{10}$/.test(d)) return (10 - [...d.slice(0, 12)].reduce((a, c, i) => a + +c * (i % 2 ? 3 : 1), 0) % 10) % 10 === +d[12];
  if (/^\d{9}[\dX]$/.test(d)) return [...d].reduce((a, c, i) => a + (10 - i) * (c === "X" ? 10 : +c), 0) % 11 === 0;
  return false;
};

async function analyse(file, kind) {
  const x = await fetch("/api/import/analyse", {method: "POST", headers: {"Content-Type": isEpub(file) ? "application/epub+zip" : "application/pdf", "X-Filename": encodeURIComponent(file.name), ...(kind === "book" ? {"X-Kind": "book"} : {})}, body: file});
  const j = await x.json().catch(() => ({}));
  if (!x.ok) throw new Error(j.error || x.statusText);
  return j;
}
const discard = tokens => tokens.length ? fetch("/api/import/discard", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({tokens})}).catch(() => {}) : null;

function cardHtml(c) {
  const book = c.kind === "book", epub = isEpub(c.file);
  const kindSel = epub ? `<span class="ir-kind"><b>Book</b> (EPUB)</span>` : `<span class="ir-kind" role="radiogroup" aria-label="What is ${esc(c.file.name)}?"><label><input type="radio" name="ir-k${c.i}" data-kind="${c.i}" value="paper"${book ? "" : " checked"}> Paper</label><label><input type="radio" name="ir-k${c.i}" data-kind="${c.i}" value="book"${book ? " checked" : ""}> Book</label></span>`;
  const head = `<div class="ir-h"><label class="ir-inc"><input type="checkbox" data-inc="${c.i}"${c.include ? " checked" : ""} aria-label="Add ${esc(c.file.name)}"><b>${esc(c.file.name)}</b></label>${kindSel}<small>${size(c.file.size)}${c.a ? " · " + plural(c.a.pdf.pages, "page") + (book && c.a.pdf.chapters ? ", outline of " + plural(c.a.pdf.chapters, "item") : "") : ""}</small></div>`;
  if (c.state === "reading") return `${head}<div class="ir-st">${epub ? "Opening the EPUB, making its pages" : "Reading the " + (book ? "book" : "PDF")} and looking it up…</div>`;
  if (c.state === "error") return `${head}<div class="ir-st bad">${esc(c.error)}</div>`;
  const a = c.a, cands = a.candidates;
  const say = a.confidence === "high" ? ["ok", book ? "✓ The file’s own details were found" + (cands[0] && cands[0].own ? "" : " and a record the book backs up") + ". Please check them." : "✓ A record was found that the PDF backs up (its title and authors, or its printed DOI). Please check it."]
    : a.confidence === "check" ? ["warn", cands[0] && cands[0].verdict === "match" ? "Please check: the title matches, but the authors could not be confirmed." : "Please check: the record below could not be confirmed against the " + (book ? "book" : "PDF") + "."]
    : a.pdf.scan ? ["bad", "This " + (book ? "book" : "PDF") + " looks scanned (it has no text to read). Type the details yourself."] : ["bad", "No record was found online. Type the details; the " + (book ? "file’s" : "PDF’s") + " own title is filled in."];
  const opts = cands.map((k, n) => `<label class="ir-cand${k.verdict === "mismatch" ? " mis" : ""}"><input type="radio" name="ir-c${c.i}" data-sel="${c.i}" value="${n}"${c.sel === n ? " checked" : ""}>
      <span><b>${esc(k.title)}</b><small>${esc((k.authors || []).slice(0, 4).join(", ") + ((k.authors || []).length > 4 ? " et al." : ""))}${!book && TYPEWORD[k.kind] ? " · " + esc(TYPEWORD[k.kind]) : ""}${book ? (k.publisher ? " · " + esc(k.publisher) : "") : k.journal ? " · " + esc(k.journal) : ""}${k.year ? " · " + esc(k.year) : ""}${book && k.isbn ? " · ISBN " + esc(k.isbn) : ""}${k.doi ? " · " + esc(k.doi) : ""}</small>
      <small class="ir-why">${esc(why(k))}${k.doi_printed && k.verdict === "match" && k.authors_found ? " · DOI printed in the PDF" : k.source === "title" ? " · found by its title" : ""}</small></span></label>`).join("");
  const f = c.fields, fld = (k, label, ph, wide) => `<label class="ir-f${wide ? " wide" : ""}"><span>${label}</span><input type="text" data-k="${k}" data-i="${c.i}" value="${esc(f[k])}" placeholder="${esc(ph || "")}" autocomplete="off" spellcheck="false"></label>`;
  return `${head}<div class="ir-st ${say[0]}">${say[1]}</div>${!book && a.kind_hint ? `<div class="ir-st ir-hint">${esc(a.kind_hint)}</div>` : ""}<div class="ir-st warn${c.existing ? "" : " hide"}" id="ir-x${c.i}">You already have this ${book ? "book" : "paper"}. It will not be added twice.</div>
    ${cands.length ? `<div class="ir-cands" role="radiogroup" aria-label="Records found for ${esc(c.file.name)}">${opts}<label class="ir-cand"><input type="radio" name="ir-c${c.i}" data-sel="${c.i}" value="-1"${c.sel === -1 ? " checked" : ""}><span><b>None of these</b><small>I will type the details myself</small></span></label></div>` : ""}
    <div class="ir-fields">${fld("title", "Title", "", true)}${fld("authors", "Authors or editors (separate with ;)", "Jane Doe; John Smith", true)}${book ? fld("publisher", "Publisher", "") + fld("pub_date", "Year", "YYYY") + fld("isbn", "ISBN", "978…") + fld("doi", "DOI (if it has one)", "10.…") : ptypeSel(c, c.file.name) + fld("journal", VENUE[c.ptype || "journal"][0], VENUE[c.ptype || "journal"][1]) + fld("pub_date", "Date", "YYYY or YYYY-MM-DD") + fld("doi", "DOI", "10.…")}</div>
    <div class="ir-err" id="ir-e${c.i}" role="alert"></div>`;
}

async function review(files, w, o = {}) {
  const pdfs = files.filter(isDoc), skipped = files.filter(f => !isDoc(f));
  const fs = (await folders()).slice().sort((a, b) => (b.id === ls.get("jw-drop-last")) - (a.id === ls.get("jw-drop-last")) || (b.updated || "").localeCompare(a.updated || ""));
  const fixed = w.kind === "folder" || w.kind === "open" ? w.id : null, fixedName = fixed ? (w.name || folderName(fs, fixed)) : "";
  const today = new Date().toLocaleDateString(undefined, {day: "numeric", month: "short", year: "numeric"});
  const last = fs.find(f => f.id === ls.get("jw-drop-last")), first = w.kind === "library" ? "lib" : last ? "f:" + last.id : "new";
  const cards = pdfs.map((f, i) => ({i, file: f, kind: isEpub(f) || o.book ? "book" : "paper", ptype: "journal", ptypeTouched: false, label: "", state: "reading", include: true, sel: -1, a: null, existing: "", fields: blank("paper"), error: ""}));
  return new Promise(resolve => {
    $("#ir-dlg") && $("#ir-dlg").remove();
    const d = document.createElement("dialog"); d.id = "ir-dlg"; d.className = "card ir-dlg"; d.setAttribute("aria-labelledby", "ir-h");
    d.innerHTML = `<h2 id="ir-h">Check the details of ${plural(pdfs.length, pdfs.every(isEpub) ? "EPUB" : pdfs.some(isEpub) ? "file" : "PDF")}</h2>
      <p class="pmeta">Nothing is filed until you press Add. Each file is read and looked up online (Crossref for papers; Open Library and Crossref for books); fix anything that is wrong. Say what each file is: a paper (a journal article, a preprint or working paper, a conference paper, or a report, thesis or other) or a book.</p>
      ${pdfs.length > 1 ? `<div class="row ir-all"><label>Set every paper's type to <select id="ir-all" aria-label="Set the type of every paper you have not set yourself"><option value="">(as each one says)</option>${PTYPES.map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select></label></div>` : ""}
      <div class="ir-list">${cards.map(c => `<section class="ir-card" id="ir-c${c.i}"></section>`).join("")}${skipped.slice(0, 3).map(f => `<div class="ir-skip">${esc(f.name)} — not a PDF or EPUB, skipped</div>`).join("")}</div>
      <div class="ir-foot">
        ${fixed ? `<div class="ir-dest">Adding to the folder <b>“${esc(fixedName)}”</b></div>` : `<div class="ir-dest"><label for="ir-to">Add to</label><select id="ir-to"><option value="lib">Library only (no folder)</option>${fs.map(f => `<option value="f:${esc(f.id)}">Folder: ${esc(f.name)} (${plural(f.count, "paper")})</option>`).join("")}<option value="new">A new folder…</option></select>
          <input type="text" id="ir-name" class="hide" maxlength="60" value="Added ${esc(today)}" aria-label="New folder name"></div>`}
        <label class="chk"><input type="checkbox" id="ir-sum"> Also write a summary for new papers (uses AI, about 1–2 cents each)</label>
        <div class="ir-err" id="ir-err" role="alert"></div>
        <div class="row di-btns"><button class="btn" id="ir-cancel">Cancel</button><button class="btn primary" id="ir-go" disabled>Add</button></div>
      </div>`;
    document.body.append(d); d.showModal();
    let closed = false;
    const draw = c => { $("#ir-c" + c.i, d).innerHTML = cardHtml(c); $("#ir-c" + c.i, d).classList.toggle("off", !c.include); };
    const ready = () => cards.filter(c => c.include && c.state === "ok");
    const status = () => {
      const busy = cards.some(c => c.include && c.state === "reading"), n = ready().length;
      const rd = ready(), nb = rd.filter(c => c.kind === "book").length;
      $("#ir-go", d).disabled = busy || !n; $("#ir-go", d).textContent = busy ? "Reading…" : n ? "Add " + (nb === n ? plural(n, "book") : nb ? plural(n, "item") : plural(n, "paper")) : "Add";
    };
    const to = $("#ir-to", d), nm = $("#ir-name", d);
    if (to) { to.value = first; nm.classList.toggle("hide", to.value !== "new"); to.onchange = () => { nm.classList.toggle("hide", to.value !== "new"); if (to.value === "new") { nm.focus(); nm.select(); } }; }
    cards.forEach(draw); status();
    const take = c => {                                                      // fill the boxes from the chosen record, or (none of them) from the file's own title
      if (c.sel >= 0) {
        const cand = c.a.candidates[c.sel]; c.fields = fieldsOf(cand, c.kind);
        if (c.kind !== "book" && !c.ptypeTouched) { c.ptype = cand.kind || "journal"; c.label = cand.label || ""; }
      } else {
        const typed = c.kind !== "book" && c.fields && c.fields.journal;     // (a venue you typed stays; else what the PDF itself says, if anything)
        c.fields = blank(c.kind, c.a.pdf.title);
        if (c.kind !== "book") {
          c.fields.journal = typed || c.a.pdf.venue || "";
          if (!c.ptypeTouched) { c.ptype = c.a.pdf.kind || "journal"; c.label = c.a.pdf.label || ""; }
        }
      }
    };
    const relabel = c => {                                                   // the type and the venue box's words, changed in place (focus and what you typed stay)
      const sel = $(`[data-ptype="${c.i}"]`, d); if (sel && sel.value !== c.ptype) sel.value = c.ptype;
      const inp = $(`[data-k="journal"][data-i="${c.i}"]`, d); if (!inp) return;
      const v = VENUE[c.ptype] || VENUE.journal; inp.placeholder = v[1]; if (inp.previousElementSibling) inp.previousElementSibling.textContent = v[0];
    };
    const sync = c => $$(`[data-k][data-i="${c.i}"]`, d).forEach(el => { el.value = c.fields[el.dataset.k]; });
    const timers = {};         // "you already have this paper" follows the record you pick and what you type (the answer is for the title and DOI as they are now, not as they were)
    const recheck = c => {
      clearTimeout(timers[c.i]);
      timers[c.i] = setTimeout(async () => {
        const ask = {doi: c.fields.doi, title: c.fields.title, kind: c.kind, isbn: c.fields.isbn || ""};
        try {
          const r = await api("/api/import/check", ask);
          if (closed || ask.doi !== c.fields.doi || ask.title !== c.fields.title || ask.isbn !== (c.fields.isbn || "")) return;       // (they typed on: a newer answer is coming)
          c.existing = r.existing || ""; const el = $("#ir-x" + c.i, d); if (el) el.classList.toggle("hide", !c.existing);
        } catch (e) { /* filing refuses a second copy anyway */ }
      }, 350);
    };
    const source = c => {
      if (c.sel < 0) return "hand";
      const cand = c.a.candidates[c.sel], k = fieldsOf(cand, c.kind), same = Object.keys(k).every(x => norm(k[x]) === norm(c.fields[x])) && (c.kind === "book" || (cand.kind || "journal") === c.ptype);
      return c.kind === "book" ? (same ? (cand.own ? "file" : "lookup") : "edited") : same ? "crossref" : "edited";
    };
    // look at the files one after the other (each one waits on the lookup), the cards fill in as they come. Switching a PDF between paper and book looks at it again.
    const look = async c => {
      try {
        c.a = await analyse(c.file, c.kind); c.state = "ok"; c.sel = c.a.pick; c.existing = c.a.existing || ""; c.kind = c.a.type === "book" ? "book" : "paper";
        if (closed) { discard([c.a.token]); return false; }
        take(c);
      } catch (e) { c.state = "error"; c.error = e.message; c.include = false; }
      if (!closed) { draw(c); status(); }
      return !closed;
    };
    (async () => { for (const c of cards) { if (closed) break; if (!(await look(c)) && closed) break; } })();
    d.addEventListener("change", e => {
      if (e.target.id === "ir-all") {                              // every paper whose type you have not set yourself
        const v = e.target.value; if (!v) return;
        cards.forEach(c => { if (c.kind !== "book" && !c.ptypeTouched) { c.ptype = v; c.label = ""; relabel(c); } });
        return;
      }
      if (e.target.dataset.ptype !== undefined) {                  // a paper's type: only the words of its venue box change (nothing is read again)
        const c = cards[+e.target.dataset.ptype]; c.ptype = e.target.value; c.ptypeTouched = true; if (c.ptype !== "other") c.label = ""; relabel(c); return;
      }
      const t = e.target, i = +(t.dataset.inc ?? t.dataset.sel ?? t.dataset.kind ?? -1); if (i < 0) return; const c = cards[i];
      if (t.dataset.kind !== undefined) {                          // paper <-> book: the old look is thrown away and the file is looked at again
        if (c.a) discard([c.a.token]); c.kind = t.value; c.a = null; c.state = "reading"; c.sel = -1; c.existing = ""; c.fields = blank(c.kind); draw(c); status(); look(c);
      } else if (t.dataset.inc !== undefined) { c.include = t.checked; $("#ir-c" + i, d).classList.toggle("off", !c.include); }
      else { c.sel = +t.value; take(c); sync(c); relabel(c); $("#ir-e" + i, d).textContent = ""; recheck(c); }
      status();
    });
    d.addEventListener("input", e => {
      const t = e.target; if (t.dataset.k === undefined) return; const c = cards[+t.dataset.i];
      c.fields[t.dataset.k] = t.value; $("#ir-e" + c.i, d).textContent = ""; $("#ir-err", d).textContent = "";
      if (t.dataset.k === "title" || t.dataset.k === "doi") recheck(c);
    });
    const finish = v => { if (closed) return; closed = true; resolve(v); d.close(); };
    const go = () => {
      const err = t => { $("#ir-err", d).textContent = t; }, use = ready();
      for (const c of use) {
        const f = c.fields, what = c.kind === "book" ? "book" : "paper";
        const badIsbn = c.kind === "book" && !isbnOk(f.isbn), bad = !norm(f.title) ? `Give this ${what} a title.` : norm(f.pub_date) && !dateOk(norm(f.pub_date)) ? "The date must be a year (2024), a month (2024-05) or a day (2024-05-17), or empty." : badIsbn ? "That ISBN is not valid (its check digit does not match). Correct it or leave it empty." : "";
        if (bad) { $("#ir-e" + c.i, d).textContent = bad; err(`Fix the highlighted ${what} first.`); const el = $(`[data-k="${!norm(f.title) ? "title" : badIsbn && !(norm(f.pub_date) && !dateOk(norm(f.pub_date))) ? "isbn" : "pub_date"}"][data-i="${c.i}"]`, d); el && el.focus(); return; }
      }
      let dest;
      if (fixed) dest = {mode: "folder", id: fixed};
      else if (to.value === "lib") dest = {mode: "lib"};
      else if (to.value === "new") {
        const n = norm(nm.value); if (!n) { err("Give the new folder a name."); return nm.focus(); }
        const dup = fs.find(f => f.name.toLowerCase() === n.toLowerCase()); if (dup) { err(`A folder called “${dup.name}” already exists — choose it from the list.`); to.value = "f:" + dup.id; nm.classList.add("hide"); return; }
        dest = {mode: "new", name: n};
      } else dest = {mode: "folder", id: to.value.slice(2)};
      discard(cards.filter(c => c.state === "ok" && !c.include).map(c => c.a.token));
      const extra = c => {                                       // a paper's type; the record's Crossref type and ISSN (only while its venue is the record's)
        if (c.kind === "book") return {kind: "book"};
        const cand = c.sel >= 0 ? c.a.candidates[c.sel] : null, sameVenue = cand && norm(cand.journal || "") === norm(c.fields.journal || "");
        return {kind: c.ptype, label: c.ptype === "other" ? c.label : "", type: cand ? cand.type || "" : "", issn: sameVenue ? cand.issn || [] : []};
      };
      finish({items: use.map(c => ({name: c.file.name, kind: c.kind, token: c.a.token, fields: {...c.fields, source: source(c), ...extra(c)}})), dest, summarize: $("#ir-sum", d).checked});
    };
    $("#ir-go", d).onclick = go;
    $("#ir-cancel", d).onclick = () => d.close();
    d.onkeydown = e => { if (e.key === "Enter" && e.target.matches("input[type=text]") && !$("#ir-go", d).disabled) { e.preventDefault(); go(); } };
    d.addEventListener("close", () => {
      if (!closed) { closed = true; discard(cards.filter(c => c.a).map(c => c.a.token)); resolve(null); }
      d.remove();
    }, {once: true});
  });
}

// ---------------------------------------------------------------- the snackbar: progress, then the result with Open folder / Undo
function snack() {
  clearTimeout(D.timer); D.snack && D.snack.remove();
  const s = D.snack = document.createElement("div"); s.className = "di-snack card"; s.setAttribute("role", "status"); document.body.append(s);
  return s;
}
function bye(s, ms) { clearTimeout(D.timer); if (ms) D.timer = setTimeout(() => { s.classList.add("gone"); setTimeout(() => s.remove(), 300); }, ms); }

async function run({items, dest, summarize}) {
  const s = snack(), res = [], total = items.length; let stop = false, folderId = dest.id || null, fname = "", created = false;
  const paint = (n, msg) => { s.innerHTML = `<div class="di-msg"><b>${esc(msg)}</b></div><div class="di-bar"><i style="width:${Math.round(100 * n / total)}%"></i></div><div class="row"><button class="btn small" data-di="stop">Stop</button></div>`; };
  s.onclick = e => { if (e.target.closest("[data-di=stop]")) stop = true; };
  const noun = items.every(x => x.kind === "book") ? "book" : items.some(x => x.kind === "book") ? "item" : "paper";
  paint(0, "Adding " + plural(total, noun) + "…");
  try {
    if (dest.mode === "new") {
      const r = await api("/api/folders", {action: "create", name: dest.name, files: []});
      const f = r.folders.find(x => x.name.toLowerCase() === dest.name.toLowerCase()); folderId = f && f.id; fname = dest.name; created = true;
      if (!folderId) throw new Error("couldn’t create the folder");
    } else if (folderId) fname = folderName(await folders(), folderId);
  } catch (e) { s.innerHTML = `<div class="di-msg"><b>${esc(e.message)}</b></div><div class="row"><button class="btn small" data-di="x">Close</button></div>`; s.onclick = () => s.remove(); return; }
  for (let i = 0; i < total && !stop; i++) {
    const f = items[i]; paint(i, `Adding ${i + 1} of ${total}: ${f.name.length > 46 ? f.name.slice(0, 44) + "…" : f.name}`);
    try {
      const r = await api("/api/import/confirm", {token: f.token, fields: f.fields, summarize});
      if (folderId) await api("/api/folders", {action: "add", id: folderId, files: [r.file]});
      res.push({name: f.name, file: r.file, title: r.title, existing: !!r.existing});
    } catch (e) { res.push({name: f.name, error: e.message}); }
  }
  if (folderId) ls.set("jw-drop-last", folderId);
  if (stop) discard(items.slice(res.length).map(x => x.token));          // the ones not reached are forgotten
  const ok = res.filter(r => r.file), isNew = ok.filter(r => !r.existing), had = ok.filter(r => r.existing), bad = res.filter(r => r.error), left = total - res.length;
  const msg = (folderId ? (ok.length ? `Added ${plural(ok.length, noun)} to “${fname}”` : "Nothing was added") : (isNew.length ? `Added ${plural(isNew.length, noun)} to your library` : "Nothing new to add")) +
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
        s.innerHTML = `<div class="di-msg"><b>Undone.</b> ${isNew.length ? plural(isNew.length, noun) + " stay" + (isNew.length === 1 ? "s" : "") + " in your library." : ""}</div>`; bye(s, 4000);
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
const onBooksTab = () => visible($("#lib-books"));           // the Books tab is open: a PDF dropped there is a book
export async function handle(fileList, w, o = {}) {
  const files = [...fileList];              // copy now: the browser empties the live list after the event
  if (!files.length) return;
  if (!files.some(isDoc)) return toast("Those aren’t PDFs or EPUBs — drop PDF files to add papers, or PDF and EPUB files to add books.");
  w = w || where(document.body);
  if (!w) return toast("Open the Library or a folder first, then drop the PDFs there.");
  const job = await review(files, w, {book: o.book || onBooksTab()});
  if (!job) return;
  chain = chain.then(() => run(job)).catch(e => notifyError(e));
}

// "＋ Add papers" in an open folder, for when dragging isn't convenient
export function choosePdfs(folderId) {
  const i = document.createElement("input"); i.type = "file"; i.accept = "application/pdf,.pdf"; i.multiple = true; i.style.display = "none"; document.body.append(i);
  i.onchange = () => { handle(i.files, {kind: "open", id: folderId}); setTimeout(() => i.remove(), 0); };
  i.click();
}
// "＋ Add a book" on the Books tab: PDF and EPUB files, checked in the same dialog
export function chooseBooks() {
  const i = document.createElement("input"); i.type = "file"; i.accept = "application/epub+zip,.epub,application/pdf,.pdf"; i.multiple = true; i.style.display = "none"; document.body.append(i);
  i.onchange = () => { handle(i.files, {kind: "library"}, {book: true}); setTimeout(() => i.remove(), 0); };
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
  D.off = off;
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
    if (!w) return toast(onRunList() ? "Drop the PDF on its paper’s card: it is added to that paper. To add it as a new paper, open the Library and drop it there."
      : "Open the Library or a folder first, then drop the PDFs there.");
    handle(e.dataTransfer.files, w);
  });
}

window.DropImport = {init(c) { ctx = c; }, handle, where, choosePdfs, chooseBooks, pickFromLibrary, off: () => D.off && D.off()};
wire();
