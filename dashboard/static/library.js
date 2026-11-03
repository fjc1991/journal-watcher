// Library front door: search + filters, reading status and stars, virtual folders, "continue reading", adding your own papers,
// exports (BibTeX / RIS / notes / annotated PDF), the highlights hub and the library-wide tag manager.
// index.html calls window.LibraryUI.init({...}) once and delegates the Library's rows and home page here.
import {notifyError, $, $$, esc, api, fileUrl, debounce, toast, COLORS, colorOf, ico, pageHead} from "./state.js";

let ctx = null;
const F = {q: "", status: "", star: false, folder: "", journal: "", year: "", tag: "", notes: false, sort: "", list: false, tab: "papers"};
let FOLDERS = [], TAGS = [], SMART = [];
const FILTER_KEYS = ["q", "status", "folder", "journal", "year", "tag"];
const SEL = new Set();
const ST = {toread: "To read", reading: "Reading", read: "Read"};
const lib = () => ctx.getLib();
const authors = a => { const xs = Array.isArray(a) ? a : (a ? String(a).split(/;\s*/) : []); return xs.slice(0, 4).join(", ") + (xs.length > 4 ? ` +${xs.length - 4} more` : ""); };
const byFile = f => lib().findIndex(r => r.file === f);
const q2s = o => Object.entries(o).filter(([, v]) => v !== "" && v !== false && v != null).map(([k, v]) => k + "=" + encodeURIComponent(v === true ? "1" : v)).join("&");

async function loadFolders() { try { FOLDERS = await api("/api/folders"); } catch (e) { FOLDERS = []; } ctx && ctx.syncNav && ctx.syncNav(); }
async function loadSmart() { try { SMART = (await api("/api/smart")).searches; } catch (e) { SMART = []; } }
async function loadTags() { try { TAGS = (await api("/api/tags")).tags; } catch (e) { TAGS = []; } }
const folderName = id => (FOLDERS.find(f => f.id === id) || {}).name || id;
const download = url => { const a = document.createElement("a"); a.href = url; a.download = ""; document.body.append(a); a.click(); a.remove(); };

// the key to the green bar on a row: a paper whose AI summary is written, ready to be explored (rows = the papers of the list it explains)
function readyKey(rows) {
  const n = (rows || []).filter(r => r && r.summary).length;
  return `<span class="readykey" title="A green bar beside a paper means its AI summary is written: open it to explore"><i aria-hidden="true"></i>Ready to read <small>${n} of ${(rows || []).length} summarised by AI</small></span>`;
}
// ---------------------------------------------------------------- one paper row (used by every Library list)
function row(r, i, snips) {
  const st = r.state || {}, fl = (r.folders || []).map(id => FOLDERS.find(f => f.id === id)).filter(Boolean);
  const nt = r.notes ? `<span class="tag you" title="Highlights and notes you made in the reader">✎ ${r.notes.highlights} highlight${r.notes.highlights === 1 ? "" : "s"}${r.notes.cards ? ` · ${r.notes.cards} note${r.notes.cards === 1 ? "" : "s"}` : ""}</span>` : "";
  const prog = st.status === "reading" && st.progress ? `<span class="prog-mini" title="Page ${st.page || "?"}"><i style="width:${Math.round(st.progress * 100)}%"></i></span>` : "";
  const sn = (snips || []).map(s => `<div class="snip"><b>${s.kind === "highlight" ? "highlight" + (s.page ? " p." + s.page : "") : s.kind}</b> ${esc(s.text || "")}</div>`).join("");
  return `<div class="card librow${r.summary ? " ready" : ""}" data-file="${esc(r.file)}">
    <div class="rowmain">
      <div class="rowhead"><input type="checkbox" class="rsel" data-sel="${esc(r.file)}"${SEL.has(r.file) ? " checked" : ""} aria-label="Select"><div class="ptitle" data-i="${i}">${esc(r.title)}${r.summary ? `<span class="sr-only"> (ready to read: summarised by AI)</span>` : ""}</div></div>
      <div class="pauth">${esc(authors(r.authors))}</div>
      <div class="pmeta">${r.book ? esc("Book" + (r.book.publisher ? " · " + r.book.publisher : "") + (r.year ? " · " + r.year : "") + (r.book.pages ? " · " + r.book.pages + " pages" : "") + (r.book.source === "epub" ? " · EPUB" : " · PDF") + ((r.book.toc || []).length ? " · outline of " + r.book.toc.length + " item" + (r.book.toc.length === 1 ? "" : "s") : "")) : `${esc(r.journal)} · ${esc(r.year || "")}${r.matched_topics ? " · " + esc(r.matched_topics) : ""}`}</div>
      ${st.takeaway ? `<div class="take" data-take="${esc(r.file)}" title="Click to edit">“${esc(st.takeaway)}”</div>` : ""}
      <div class="tags">${st.status ? `<span class="tag stt st-${esc(st.status)}" title="Reading status">${esc(ST[st.status] || st.status)}</span>` : ""}${nt}${r.card ? `<span class="tag you" data-card="${esc(r.file)}" title="This paper has a card (question, theory, method, findings, limitations…). Click to open it." style="cursor:pointer">📇 card</span>` : ""}${fl.map(f => `<span class="tag fol" data-openfolder="${esc(f.id)}">📁 ${esc(f.name)}</span>`).join("")}${prog}</div>
      ${sn}
    </div>
    <div class="row actions">
      <button class="btn small star${st.star ? " on" : ""}" data-star="${esc(r.file)}" title="${st.star ? "Starred" : "Star this paper"}" aria-label="Star">${st.star ? "★" : "☆"}</button>
      <button class="btn primary small" data-read="${i}">${ico("read", "sm")} Read</button>
      <span class="menuwrap"><button class="btn small" data-more aria-haspopup="true" aria-label="More actions" title="More: status, summary, PDF, folders, takeaway">${ico("more", "sm")}</button>
        <div class="menu card hide rowmenu" role="menu">
          <label class="rm-st"><span>Reading status</span><select class="stsel" data-st="${esc(r.file)}"><option value="">None</option>${Object.entries(ST).map(([k, v]) => `<option value="${k}"${st.status === k ? " selected" : ""}>${v}</option>`).join("")}</select></label>
          ${r.summary ? `<button data-i="${i}" role="menuitem">${ico("sum", "sm")} Summary</button>` : ""}
          <a target="_blank" href="${fileUrl(r.file)}" role="menuitem">${ico("papers", "sm")} Open the PDF</a>
          <button data-fold="${esc(r.file)}" role="menuitem">${ico("folders", "sm")} Folders…</button>
          <button data-take="${esc(r.file)}" role="menuitem">${ico("notes", "sm")} One-line takeaway…</button>
        </div></span>
    </div></div>`;
}
function redrawRow(file) {
  const i = byFile(file), el = document.querySelector(`.librow[data-file="${CSS.escape(file)}"]`);
  if (i < 0 || !el) return;
  el.outerHTML = row(lib()[i], i);
  document.querySelectorAll(`.librow[data-file="${CSS.escape(file)}"] [data-read]`).forEach(b => b.onclick = () => ctx.readPaper(lib()[+b.dataset.read]));
  document.querySelectorAll(`.librow[data-file="${CSS.escape(file)}"] [data-i]`).forEach(b => b.onclick = () => ctx.openLib(lib()[+b.dataset.i]));
}
async function setState(file, patch) {
  const i = byFile(file); if (i < 0) return;
  const r = await api("/api/paper/state", {file, ...patch});
  lib()[i].state = r.state; redrawRow(file); shelf();
}

// ---------------------------------------------------------------- the Library home: tabs, search, filters, shelf, folders
let TOP = null;
function top(el, opts) {
  TOP = {el, opts}; QUEUE = null;
  el.innerHTML = `<div class="libtabs tooltabs" role="tablist">${[["papers", "Papers"], ["books", "Books"], ["ask", "Ask"], ["discover", "Discover"], ["hl", "Highlights"], ["review", "Review"], ["tags", "Tags"]].map(([k, t]) => `<button data-tab="${k}" data-tool="${k}" role="tab">${ico(k)}${t}</button>`).join("")}</div>
    <div id="lib-papers">
      <div class="card libtools">
        <div class="row lt-top"><input type="search" id="lf-q" placeholder="Search titles, authors, summaries — and your own highlights and notes…" autocomplete="off">
          <button class="btn" id="lf-ft" title="Look for these words inside the PDFs themselves, not only in titles, summaries and your notes (Shift+Enter)">${ico("search")} In the PDFs</button>
          <span class="menuwrap"><button class="btn" id="lf-filters" aria-haspopup="true" title="Filter by status, folder, journal, year, tag, notes">${ico("filter")} Filters <span class="nn" id="lf-nf"></span></button>
            <div class="menu card hide lf-pop" id="lf-pop">
              <label>Status<select id="lf-status"><option value="">Any status</option><option value="toread">To read</option><option value="reading">Reading</option><option value="read">Read</option><option value="none">No status</option></select></label>
              <label>Folder<select id="lf-folder"></select></label><label>Journal<select id="lf-journal"></select></label><label>Year<select id="lf-year"></select></label><label>Tag<select id="lf-tag"></select></label>
              <button class="btn small" id="lf-notes" title="Only papers where you made highlights or notes">✎ Only papers with my notes</button></div></span>
          <button class="btn" id="lf-star" title="Only starred papers">☆ Starred</button>
          <select id="lf-sort" aria-label="Sort"><option value="">Sort: best match / newest</option><option value="title">Sort: title</option><option value="journal">Sort: journal</option><option value="opened">Sort: last opened</option><option value="notes">Sort: most highlights</option></select>
          <button class="btn" id="lf-list" title="List every paper instead of browsing by journal">☰ List all</button>
          <button class="btn primary" id="lf-add">＋ Add paper</button>
          <span class="menuwrap"><button class="btn" id="lf-export">Export ▾</button><div class="menu card hide" id="lf-exportmenu"></div></span></div>
        <div class="lf-chips hide" id="lf-chips"></div>
        <div class="fstrip" id="lf-fstrip"></div>
      </div>
      <div id="lib-shelf"></div>
      <div id="lib-upnext"></div>
      <div id="lib-results"></div>
    </div>
    <div id="lib-folders" class="hide"></div><div id="lib-books" class="hide"></div><div id="lib-ask" class="hide"></div><div id="lib-discover" class="hide"></div><div id="lib-hl" class="hide"></div><div id="lib-review" class="hide"></div><div id="lib-tags" class="hide"></div><div class="actionbar card hide" id="lib-actionbar"></div>`;
  const bind = (id, ev, fn) => $("#" + id, el).addEventListener(ev, fn);
  const refresh = debounce(() => results(opts), 220);
  bind("lf-q", "input", e => { F.q = e.target.value; refresh(); });
  bind("lf-q", "keydown", e => { if (e.key === "Enter" && e.shiftKey) { e.preventDefault(); fulltext(opts); } });
  bind("lf-ft", "click", () => fulltext(opts));
  [["lf-status", "status"], ["lf-folder", "folder"], ["lf-journal", "journal"], ["lf-year", "year"], ["lf-tag", "tag"], ["lf-sort", "sort"]]
    .forEach(([id, k]) => bind(id, "change", e => { F[k] = e.target.value; results(opts); }));
  bind("lf-star", "click", () => { F.star = !F.star; results(opts); });
  bind("lf-notes", "click", () => { F.notes = !F.notes; results(opts); });
  bind("lf-list", "click", () => { F.list = !F.list; results(opts); });
  bind("lf-add", "click", () => addDialog());
  bind("lf-filters", "click", e => { e.stopPropagation(); const m = $("#lf-pop", el), was = m.classList.contains("hide"); $$(".menuwrap .menu").forEach(x => x.classList.add("hide")); m.classList.toggle("hide", !was); });
  $("#lf-chips", el).addEventListener("click", async e => {
    if (e.target.closest("[data-savesearch]")) { const n = prompt("Name this search (it will be kept next to your folders):", F.q || ""); if (!n || !n.trim()) return; try { SMART = (await api("/api/smart", {action: "add", name: n.trim(), filters: pickFilters()})).searches; toast("Saved."); fillControls(el); } catch (er) { notifyError(er); } return; }
    const c = e.target.closest("[data-unf]"); if (!c) return; if (c.dataset.unf === "all") Object.assign(F, {status: "", star: false, folder: "", journal: "", year: "", tag: "", notes: false}); else F[c.dataset.unf] = c.dataset.unf === "star" || c.dataset.unf === "notes" ? false : ""; fillControls(el); results(opts); });
  bind("lf-export", "click", e => { e.stopPropagation(); exportMenu($("#lf-exportmenu", el), {}); });
  $$(".libtabs button", el).forEach(b => b.onclick = () => { F.tab = b.dataset.tab; tabs(el, opts); });
  Promise.all([loadFolders(), loadTags(), loadSmart()]).then(() => { fillControls(el); tabs(el, opts); });
  fillControls(el); tabs(el, opts);
  window.ReviewUI && window.ReviewUI.refreshBadge();
}
function fillControls(el) {
  const L = lib(), opt = (v, t, cur) => `<option value="${esc(v)}"${cur === v ? " selected" : ""}>${esc(t)}</option>`;
  const P = L.filter(r => r.journal !== "Books");           // (books have their own tab: they are not a journal to filter papers by)
  const js = [...new Map(P.map(r => [r.abbr, r.journal])).entries()].sort((a, b) => a[1].localeCompare(b[1]));
  const ys = [...new Set(P.map(r => r.year).filter(Boolean))].sort().reverse();
  $("#lf-q", el).value = F.q; $("#lf-status", el).value = F.status; $("#lf-sort", el).value = F.sort;
  $("#lf-folder", el).innerHTML = opt("", "Any folder", F.folder) + FOLDERS.map(f => opt(f.id, "📁 " + f.name + " (" + f.count + ")", F.folder)).join("");
  $("#lf-journal", el).innerHTML = opt("", "Any journal", F.journal) + js.map(([a, n]) => opt(a, n, F.journal)).join("");
  $("#lf-year", el).innerHTML = opt("", "Any year", F.year) + ys.map(y => opt(y, y, F.year)).join("");
  $("#lf-tag", el).innerHTML = opt("", "Any tag", F.tag) + TAGS.map(t => opt(t.id, (t.category ? t.category + ": " : "") + t.name, F.tag)).join("");
  $("#lf-star", el).classList.toggle("on", F.star); $("#lf-notes", el).classList.toggle("on", F.notes); $("#lf-list", el).classList.toggle("on", F.list);
  $("#lf-fstrip", el).innerHTML = (FOLDERS.length ? `<span class="chip" data-gotab="folders" title="See all folders as tiles">📂 All folders</span>` : "") + FOLDERS.map(f => `<span class="chip fchip" data-openfolder="${esc(f.id)}" data-dropfolder="${esc(f.id)}">📁 ${esc(f.name)} <small>${f.count}</small></span>`).join("") +
    SMART.map(s => `<span class="chip smart" data-smart="${esc(s.id)}" title="A saved search: ${esc(smartText(s))}">${ico("search", "sm")} ${esc(s.name)} <button class="chipx" data-smartdel="${esc(s.id)}" title="Forget this search" aria-label="Forget this search">✕</button></span>`).join("") +
    `<span class="chip" id="lf-newfolder">＋ New folder</span>`;
  $$("[data-smart]", el).forEach(c => c.onclick = e => { const del = e.target.closest("[data-smartdel]");
    if (del) { e.stopPropagation(); if (confirm("Forget the saved search “" + (SMART.find(s => s.id === del.dataset.smartdel) || {}).name + "”? The papers are not affected.")) api("/api/smart", {action: "delete", id: del.dataset.smartdel}).then(r => { SMART = r.searches; fillControls(el); }).catch(notifyError); return; }
    const s = SMART.find(x => x.id === c.dataset.smart); if (!s) return; Object.assign(F, {q: "", status: "", star: false, folder: "", journal: "", year: "", tag: "", notes: false, list: false}, s.filters); F.tab = "papers"; tabs(el, TOP && TOP.opts); fillControls(el); results(TOP && TOP.opts); });
  $("#lf-newfolder", el).onclick = () => newFolder();
  const gt = $('[data-gotab="folders"]', el); if (gt) gt.onclick = () => openTab("folders");
}
function tabs(el, opts) {
  if (F.tab !== "folders" && !["papers", "books", "ask", "discover", "hl", "review", "tags"].includes(F.tab)) F.tab = "papers";
  $$(".libtabs button", el).forEach(b => b.classList.toggle("on", b.dataset.tab === F.tab));
  $(".libtabs", el).classList.toggle("hide", F.tab === "folders"); $("#lib-head")?.classList.toggle("hide", F.tab === "folders");
  ctx.tabChanged && ctx.tabChanged();
  $("#lib-papers", el).classList.toggle("hide", F.tab !== "papers"); $("#lib-hl", el).classList.toggle("hide", F.tab !== "hl"); $("#lib-tags", el).classList.toggle("hide", F.tab !== "tags"); $("#lib-review", el).classList.toggle("hide", F.tab !== "review"); $("#lib-ask", el).classList.toggle("hide", F.tab !== "ask"); $("#lib-discover", el).classList.toggle("hide", F.tab !== "discover"); $("#lib-folders", el).classList.toggle("hide", F.tab !== "folders");
  if (opts && opts.browse) opts.browse.classList.toggle("hide", F.tab !== "papers" || active());
  $("#lib-books", el).classList.toggle("hide", F.tab !== "books");
  if (F.tab === "papers") results(opts); else if (F.tab === "books") booksTab(); else if (F.tab === "hl") highlightsHub(); else if (F.tab === "tags") tagManager();
  else if (F.tab === "folders" && window.FoldersUI) window.FoldersUI.mount($("#lib-folders", el));
  else if (F.tab === "review" && window.ReviewUI) window.ReviewUI.mount($("#lib-review", el));
  else if (F.tab === "discover" && window.DiscoverUI) { const box = $("#lib-discover", el); if (!box.dataset.mounted) { window.DiscoverUI.mount(box, {folder: null}); box.dataset.mounted = "1"; } }
  else if (F.tab === "ask" && window.AskUI) { const box = $("#lib-ask", el); if (!box.dataset.mounted) { window.AskUI.init(ctx); window.AskUI.mount(box, {folder: null}); box.dataset.mounted = "1"; } }
}
// ---------------------------------------------------------------- the Books tab: your PDF and EPUB books (books.py). They are library rows under the journal "Books", read in the same reader, and searched and asked about with your papers
let BQ = "", BSORT = "title", BFT = false;
const BOOK_SORTS = {title: ["Sort: title", (a, b) => a[0].title.localeCompare(b[0].title)], newest: ["Sort: newest", (a, b) => (b[0].year || "").localeCompare(a[0].year || "") || a[0].title.localeCompare(b[0].title)],
  opened: ["Sort: last opened", (a, b) => ((b[0].state || {}).opened || "").localeCompare((a[0].state || {}).opened || "")], author: ["Sort: author", (a, b) => authors(a[0].authors).localeCompare(authors(b[0].authors))]};
function booksTab() {
  const box = $("#lib-books"); if (!box) return;
  box.innerHTML = `<div class="card libtools"><div class="row lt-top"><input type="search" id="bk-q" placeholder="Search your books by title, author, publisher, ISBN…" autocomplete="off" value="${esc(BQ)}" aria-label="Search your books">
      <button class="btn" id="bk-ft" title="Look for these words inside the books themselves (Shift+Enter)">${ico("search")} In the books</button>
      <select id="bk-sort" aria-label="Sort">${Object.entries(BOOK_SORTS).map(([k, [t]]) => `<option value="${k}"${BSORT === k ? " selected" : ""}>${t}</option>`).join("")}</select>
      <button class="btn primary" id="bk-add">＋ Add a book</button></div>
    <p class="pmeta" style="margin:8px 0 0">PDF and EPUB. Press Add a book, or drop the files here: you check the details before anything is filed. An EPUB is turned into pages that the reader shows (the file itself is kept). Books are searched, highlighted and asked about like your papers.</p></div>
    <div id="bk-out"></div><div id="bk-list"></div>`;
  const draw = () => {
    const q = BQ.trim().toLowerCase(), all = lib().map((r, i) => [r, i]).filter(([r]) => r.journal === "Books");
    const hit = all.filter(([r]) => !q || [r.title, authors(r.authors), (r.book || {}).publisher, (r.book || {}).isbn, r.year].join(" ").toLowerCase().includes(q)).sort(BOOK_SORTS[BSORT][1]);
    $("#bk-list", box).innerHTML = (all.length ? `<div class="reshead">${hit.length} book${hit.length === 1 ? "" : "s"}${q ? ` matching “${esc(BQ.trim())}”` : ` in your library`}</div>` : "") +
      (hit.map(([r, i]) => row(r, i)).join("") || (all.length ? `<div class="card empty"><h2>No book matches</h2>Try other words.</div>` : `<div class="card empty"><h2>No books yet</h2>Press <b>＋ Add a book</b> or drop PDF and EPUB files onto this page. Nothing is filed until you have checked the details.</div>`));
    bindRows($("#bk-list", box));
  };
  $("#bk-q", box).oninput = e => { BQ = e.target.value; BFT = false; $("#bk-out", box).innerHTML = ""; draw(); };
  $("#bk-q", box).onkeydown = e => { if (e.key === "Enter" && e.shiftKey) { e.preventDefault(); inBooks(box); } };
  $("#bk-sort", box).onchange = e => { BSORT = e.target.value; draw(); };
  $("#bk-ft", box).onclick = () => inBooks(box);
  $("#bk-add", box).onclick = () => window.DropImport && window.DropImport.chooseBooks();
  draw();
}
async function inBooks(box) {                       // the words inside the books: the same search as "In the PDFs", limited to the Books journal
  const q = BQ.trim(), out = $("#bk-out", box); if (!q) return toast("Type the words to look for first.");
  out.innerHTML = `<div class="reshead">Looking inside the books…</div>`; let res = null;
  try {
    const r = await fetch("/api/library/fulltext", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({q, scope: {journal: "BOOK"}})});
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
    const rd = r.body.getReader(), dec = new TextDecoder(); let buf = "";
    for (;;) { const {value, done} = await rd.read(); if (done) break; buf += dec.decode(value, {stream: true}); let i;
      while ((i = buf.indexOf("\n")) >= 0) { const m = JSON.parse(buf.slice(0, i)); buf = buf.slice(i + 1); if (m.type === "status") out.innerHTML = `<div class="reshead">${esc(m.msg)}</div>`; else if (m.type === "results") res = m; else if (m.type === "error") throw new Error(m.error); } }
  } catch (e) { out.innerHTML = `<div class="card empty">${esc(e.message)}</div>`; return; }
  const keys = [...res.phrases, ...res.words].filter(Boolean).map(k => k.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")), re = keys.length ? new RegExp("(" + keys.join("|") + ")", "gi") : null;
  const hl = t => re ? t.split(re).map((p, i) => i % 2 ? `<mark>${esc(p)}</mark>` : esc(p)).join("") : esc(t);
  out.innerHTML = `<div class="reshead">${res.n} passage${res.n === 1 ? "" : "s"} in ${res.papers} book${res.papers === 1 ? "" : "s"} (of ${res.searched} read) for “${esc(q)}” <button class="linkbtn" id="bk-ftx">close</button></div>` +
    (res.items.map((it, i) => `<div class="card ft-i" data-ft="${i}" tabindex="0" role="button"><div class="ft-h"><b>${esc(it.cite)}</b> · ${esc(it.title)} · file page ${it.page}</div><div class="ft-s">${hl(it.snippet)}</div></div>`).join("") || `<div class="card empty"><h2>Not found in the books</h2>All the words have to be in one paragraph. Try fewer words, or put an exact phrase in “quotes”.</div>`);
  $("#bk-ftx", out).onclick = () => { out.innerHTML = ""; };
  const open = i => { const it = res.items[i], r2 = lib()[byFile(it.file)]; if (r2) ctx.readPaper(r2, {goto: {page: it.page, quote: it.quote}}); };
  out.querySelectorAll("[data-ft]").forEach(c => { c.onclick = () => open(+c.dataset.ft); c.onkeydown = e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(+c.dataset.ft); } }; });
}

// ---------------------------------------------------------------- the words inside the PDFs (fulltext.py)
let FT = null, FT_BUSY = false;                        // the words being searched for inside the PDFs; while they are the search box's text, a filter or a late keystroke repeats that search instead of the title search
async function fulltext(opts) {
  const q = F.q.trim(), box = $("#lib-results"); if (!box) return; if (!q) return toast("Type the words to look for first.");
  if (FT_BUSY) return; FT = F.q; FT_BUSY = true; ++seq; if (opts && opts.browse) opts.browse.classList.add("hide"); $("#lib-shelf").classList.add("hide"); $("#lib-upnext").classList.add("hide");
  box.innerHTML = `<div class="reshead">Looking inside the PDFs…</div>`; let res = null;
  try {
    const r = await fetch("/api/library/fulltext", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({q, scope: {folder: F.folder, journal: F.journal, year: F.year, tag: F.tag, status: F.status}})});
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
    const rd = r.body.getReader(), dec = new TextDecoder(); let buf = "";
    for (;;) { const {value, done} = await rd.read(); if (done) break; buf += dec.decode(value, {stream: true}); let i;
      while ((i = buf.indexOf("\n")) >= 0) { const m = JSON.parse(buf.slice(0, i)); buf = buf.slice(i + 1); if (m.type === "status") box.innerHTML = `<div class="reshead">${esc(m.msg)}</div>`; else if (m.type === "results") res = m; else if (m.type === "error") throw new Error(m.error); } }
  } catch (e) { FT_BUSY = false; box.innerHTML = `<div class="card empty">${esc(e.message)}</div>`; return; }
  FT_BUSY = false;
  const keys = [...res.phrases, ...res.words].filter(Boolean).map(k => k.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")), re = keys.length ? new RegExp("(" + keys.join("|") + ")", "gi") : null;
  const hl = t => re ? t.split(re).map((p, i) => i % 2 ? `<mark>${esc(p)}</mark>` : esc(p)).join("") : esc(t);
  box.innerHTML = `<div class="reshead">${res.n} passage${res.n === 1 ? "" : "s"} in ${res.papers} paper${res.papers === 1 ? "" : "s"} (of ${res.searched} read) for “${esc(q)}” <button class="linkbtn" id="ft-back">back to the normal results</button></div>` +
    (res.items.map((it, i) => `<div class="card ft-i" data-ft="${i}" tabindex="0" role="button"><div class="ft-h"><b>${esc(it.cite)}</b> · ${esc(it.title)} · p. ${it.page}</div><div class="ft-s">${hl(it.snippet)}</div></div>`).join("") || `<div class="card empty"><h2>Not found in the PDFs</h2>All the words have to be in one paragraph. Try fewer words, or put an exact phrase in “quotes”.</div>`);
  $("#ft-back", box).onclick = () => { FT = null; results(opts); };
  const open = i => { const it = res.items[i], row = lib()[byFile(it.file)]; if (row) ctx.readPaper(row, {goto: {page: it.page, quote: it.quote}}); };
  box.querySelectorAll("[data-ft]").forEach(c => { c.onclick = () => open(+c.dataset.ft); c.onkeydown = e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(+c.dataset.ft); } }; });
}
const active = () => !!(F.q || F.status || F.star || F.folder || F.journal || F.year || F.tag || F.notes || F.list);
let seq = 0;
async function results(opts) {
  const box = $("#lib-results"); if (!box) return;
  if (FT !== null && F.q === FT) return fulltext(opts);
  FT = null;
  const on = active();
  if (opts && opts.browse) opts.browse.classList.toggle("hide", on);
  $("#lib-shelf").classList.toggle("hide", on); $("#lib-upnext").classList.toggle("hide", on);
  fillFlags();
  if (!on) { box.innerHTML = ""; shelf(); return; }
  const my = ++seq;
  let res = [];
  try { res = await api("/api/library/search?" + q2s(F)); } catch (e) { box.innerHTML = `<div class="card empty">${esc(e.message)}</div>`; return; }
  if (my !== seq) return;
  const found = res.map(x => lib()[byFile(x.file)]).filter(Boolean);
  box.innerHTML = `<div class="reshead">${res.length} paper${res.length === 1 ? "" : "s"}${F.q ? ` matching “${esc(F.q)}”` : ""}${found.length ? readyKey(found) : ""}</div>` +
    (res.map(x => { const i = byFile(x.file); return i < 0 ? "" : row(lib()[i], i, x.snippets); }).join("") || `<div class="card empty"><h2>Nothing matches</h2>Try fewer filters or other words.</div>`);
  bindRows(box);
  actionbar();
}
const pickFilters = () => ({...Object.fromEntries(FILTER_KEYS.filter(k => F[k]).map(k => [k, F[k]])), ...(F.star ? {star: true} : {}), ...(F.notes ? {notes: true} : {})});
const smartText = s => { const f = s.filters, t = []; if (f.q) t.push("“" + f.q + "”"); if (f.status) t.push(ST[f.status] || f.status); if (f.star) t.push("starred"); if (f.folder) t.push("folder " + folderName(f.folder)); if (f.journal) t.push(f.journal); if (f.year) t.push(f.year); if (f.tag) t.push("tag"); if (f.notes) t.push("with my notes"); return t.join(" · "); };
function drawChips() {
  const box = $("#lf-chips"); if (!box) return;
  const sel = id => { const s = $("#" + id); return s && s.selectedOptions[0] ? s.selectedOptions[0].textContent.replace(/^📁\s*/, "").replace(/\s*\(\d+\)$/, "") : ""; };
  const cs = [F.status && ["status", "Status: " + (ST[F.status] || "No status")], F.star && ["star", "★ Starred"], F.folder && ["folder", "Folder: " + sel("lf-folder")], F.journal && ["journal", "Journal: " + sel("lf-journal")],
    F.year && ["year", "Year: " + F.year], F.tag && ["tag", "Tag: " + sel("lf-tag")], F.notes && ["notes", "✎ With my notes"]].filter(Boolean);
  const can = Object.keys(pickFilters()).length > 0;
  box.classList.toggle("hide", !can);
  box.innerHTML = cs.map(([k, t]) => `<span class="chip on" data-unf="${k}" title="Remove this filter">${esc(t)} ${ico("x", "sm")}</span>`).join("") + (cs.length > 1 ? `<button class="linkbtn" data-unf="all">Clear all</button>` : "") + (can ? `<button class="linkbtn" data-savesearch title="Keep this search and its filters as a chip next to your folders">${ico("search", "sm")} Save this search</button>` : "");
  const n = cs.filter(c => c[0] !== "star").length, nf = $("#lf-nf"); if (nf) nf.textContent = n || "";
  $("#lf-filters")?.classList.toggle("on", n > 0);
}
const fillFlags = () => { drawChips(); $("#lf-star")?.classList.toggle("on", F.star); $("#lf-notes")?.classList.toggle("on", F.notes); $("#lf-list")?.classList.toggle("on", F.list); };
function bindRows(box) {
  box.querySelectorAll("[data-i]").forEach(b => b.onclick = () => ctx.openLib(lib()[+b.dataset.i]));
  box.querySelectorAll("[data-read]").forEach(b => b.onclick = () => ctx.readPaper(lib()[+b.dataset.read]));
}
// ---------------------------------------------------------------- Up next: what to read next, from what you marked "to read" and what the weekly check found (personal.py)
let QUEUE = null, queueSeq = 0;
async function upNext(force) {
  const box = $("#lib-upnext"); if (!box) return; const my = ++queueSeq;
  if (force || !QUEUE) { try { QUEUE = await api("/api/queue?n=6"); } catch (e) { QUEUE = null; box.innerHTML = ""; return; } if (my !== queueSeq) return; }
  const q = QUEUE, its = q.items;
  if (!its.length) { box.innerHTML = ""; return; }
  box.innerHTML = `<div class="shelfhead">Up next <small>${q.total} unread${q.profile.ready ? " · ordered with what you have read in mind" : ` · it learns from what you read, star or mark up (${q.profile.pos} of 3 so far)`}</small></div><div class="shelf upnext">${its.map(it => `<div class="card shelfcard unx" data-file="${esc(it.file)}"><div class="shtitle">${esc(it.title)}</div>
    <div class="pmeta">${esc(it.journal)}${it.year ? " · " + esc(it.year) : ""}${it.fit === "high" ? ` <span class="tag fit high">close to what you read</span>` : ""}</div><div class="pmeta unx-why">${esc(it.why)}</div>
    <div class="row unx-act"><button class="btn small primary" data-unx="read">Read</button>${it.status === "toread" ? `<span class="pmeta">in your list</span>` : `<button class="btn small" data-unx="later" title="Keep it in your To read list">＋ To read</button>`}<button class="btn small" data-unx="skip" title="Not for me: hide it here and learn from it">Skip</button>
    ${it.folder ? `<button class="btn small" data-unx="folder" data-fid="${esc(it.folder.id)}" title="It is much closer to this folder than the rest of your library is">＋ ${esc(it.folder.name)}</button>` : ""}</div></div>`).join("")}</div>`;
  box.querySelectorAll("[data-unx]").forEach(b => b.onclick = async e => {
    e.stopPropagation(); const file = b.closest("[data-file]").dataset.file, a = b.dataset.unx, i = byFile(file);
    try {
      if (a === "read") return ctx.readPaper(lib()[i]);
      if (a === "folder") { await folderOp({action: "add", id: b.dataset.fid, files: [file]}); toast("Added to " + folderName(b.dataset.fid)); }
      else await setState(file, a === "later" ? {status: "toread"} : {skip: true});
      upNext(true);
    } catch (er) { notifyError(er); }
  });
}
function shelf() {
  upNext();
  const box = $("#lib-shelf"); if (!box) return;
  const cur = lib().filter(r => (r.state || {}).status === "reading").sort((a, b) => (b.state.opened || "").localeCompare(a.state.opened || "")).slice(0, 6);
  box.innerHTML = cur.length ? `<div class="shelfhead">Continue reading</div><div class="shelf">${cur.map(r => { const i = byFile(r.file);
    return `<div class="card shelfcard" data-read="${i}"><div class="shtitle">${esc(r.title)}</div><div class="pmeta">${esc(r.journal)} · ${esc(r.year || "")}</div>
      <div class="prog-mini wide"><i style="width:${Math.round((r.state.progress || 0) * 100)}%"></i></div><div class="pmeta">${r.state.page ? "page " + r.state.page : "not started"}</div></div>`; }).join("")}</div>` : "";
  box.querySelectorAll("[data-read]").forEach(b => b.onclick = () => ctx.readPaper(lib()[+b.dataset.read]));
}

// ---------------------------------------------------------------- delegated row events (status, star, folders, takeaway, select)
function wireRows() {
  const root = $("#libview");
  root.addEventListener("change", e => {
    const s = e.target.closest("[data-st]"), c = e.target.closest("[data-sel]");
    if (s) setState(s.dataset.st, {status: s.value}).catch(er => notifyError(er));
    if (c) { c.checked ? SEL.add(c.dataset.sel) : SEL.delete(c.dataset.sel); actionbar(); }
  });
  root.addEventListener("click", e => {
    const more = e.target.closest("[data-more]");
    if (more) { e.stopPropagation(); const m = more.parentElement.querySelector(".menu"), was = m.classList.contains("hide"); $$(".menuwrap .menu").forEach(x => x.classList.add("hide")); m.classList.toggle("hide", !was); return; }
    const pick = e.target.closest(".rowmenu button, .rowmenu a"); if (pick) pick.closest(".menu").classList.add("hide");
    const star = e.target.closest("[data-star]"), fold = e.target.closest("[data-fold]"), take = e.target.closest("[data-take]"), of = e.target.closest("[data-openfolder]"), cd = e.target.closest("[data-card]");
    if (cd) { const i = byFile(cd.dataset.card); if (i >= 0) ctx.readPaper(lib()[i], {tab: "card"}); }
    else if (star) { const r = lib()[byFile(star.dataset.star)]; setState(star.dataset.star, {star: !(r.state || {}).star}).catch(er => notifyError(er)); }
    else if (fold) { e.stopPropagation(); folderPop(fold.closest(".menuwrap")?.querySelector("[data-more]") || fold, fold.dataset.fold); }
    else if (take) { const r = lib()[byFile(take.dataset.take)], v = prompt("One-line takeaway for this paper (what to remember):", (r.state || {}).takeaway || ""); if (v !== null) setState(take.dataset.take, {takeaway: v}).catch(er => notifyError(er)); }
    else if (of) ctx.go({j: "@" + of.dataset.openfolder});
  });
  // drag a paper onto a folder chip
  root.addEventListener("dragstart", e => { const c = e.target.closest(".librow"); if (c) { e.dataTransfer.setData("text/x-jw-file", c.dataset.file); e.dataTransfer.effectAllowed = "copy"; } });
  root.addEventListener("dragover", e => { if (e.target.closest("[data-dropfolder]") && e.dataTransfer.types.includes("text/x-jw-file")) { e.preventDefault(); e.target.closest("[data-dropfolder]").classList.add("over"); } });
  root.addEventListener("dragleave", e => e.target.closest?.("[data-dropfolder]")?.classList.remove("over"));
  root.addEventListener("drop", async e => {
    const t = e.target.closest("[data-dropfolder]"), f = e.dataTransfer.getData("text/x-jw-file");
    if (!t || !f) return; e.preventDefault(); t.classList.remove("over");
    await folderOp({action: "add", id: t.dataset.dropfolder, files: [f]}); toast("Added to " + folderName(t.dataset.dropfolder));
  });
  document.addEventListener("keydown", e => { if (e.key === "Escape") $$(".menuwrap .menu").forEach(m => m.classList.add("hide")); });
  document.addEventListener("click", e => { if (!e.target.closest(".fpop")) $$(".fpop").forEach(p => p.remove()); if (!e.target.closest(".menuwrap")) $$(".menuwrap .menu").forEach(m => m.classList.add("hide")); });
  // (PDFs dropped from your desktop are handled by dropimport.js: onto a folder, inside a folder, or anywhere in the Library with a chooser)
}
function actionbar() {
  const bar = $("#lib-actionbar"); if (!bar) return;
  for (const f of [...SEL]) if (byFile(f) < 0) SEL.delete(f);
  bar.classList.toggle("hide", !SEL.size);
  if (!SEL.size) return;
  bar.innerHTML = `<b>${SEL.size} selected</b><span class="menuwrap"><button class="btn small" id="ab-fold">📁 Add to folder ▾</button></span>
    <span class="menuwrap"><button class="btn small" id="ab-exp">Export ▾</button><div class="menu card hide" id="ab-expmenu"></div></span><button class="btn small" id="ab-clear">Clear</button>`;
  $("#ab-clear").onclick = () => { SEL.clear(); $$(".rsel").forEach(c => c.checked = false); actionbar(); };
  $("#ab-fold").onclick = e => { e.stopPropagation(); folderPop(e.currentTarget, [...SEL], true); };
  $("#ab-exp").onclick = e => { e.stopPropagation(); exportMenu($("#ab-expmenu"), {files: [...SEL]}); };
}

// ---------------------------------------------------------------- folders
async function folderOp(body) {
  try { const r = await api("/api/folders", body); FOLDERS = r.folders; }
  catch (e) { notifyError(e); throw e; }
  await ctx.reload(); const el = $("#lib-papers") && $("#libview"); if (el && $("#lf-folder")) fillControls($("#libview")); results();
}
async function newFolder(files) {
  const n = prompt("Name of the new folder:"); if (!n || !n.trim()) return null;
  await folderOp({action: "create", name: n.trim(), files: files || []}); return FOLDERS.find(f => f.name === n.trim().replace(/\s+/g, " "));
}
function folderPop(anchor, target, bulk) {
  $$(".fpop").forEach(p => p.remove());
  const files = Array.isArray(target) ? target : [target], first = lib()[byFile(files[0])];
  const pop = document.createElement("div"); pop.className = "fpop card";
  const draw = () => { pop.innerHTML = (FOLDERS.length ? FOLDERS.map(f => { const inn = files.every(x => (lib()[byFile(x)].folders || []).includes(f.id));
      return `<label><input type="checkbox" data-f="${esc(f.id)}"${inn ? " checked" : ""}> 📁 ${esc(f.name)}</label>`; }).join("") : `<div class="pmeta" style="padding:6px">No folders yet.</div>`) +
    `<div class="fnew"><input type="text" placeholder="＋ new folder…"></div>`; };
  draw(); document.body.append(pop);
  const r = anchor.getBoundingClientRect(); pop.style.left = Math.min(r.left, innerWidth - 260) + "px"; pop.style.top = (bulk ? Math.max(8, r.top - pop.offsetHeight - 6) : r.bottom + 4) + "px";
  pop.onclick = e => e.stopPropagation();
  pop.onchange = async e => { const f = e.target.dataset.f; if (!f) return;
    await folderOp({action: e.target.checked ? "add" : "remove", id: f, files}); draw(); };
  pop.querySelector(".fnew input").onkeydown = async e => { if (e.key === "Enter" && e.target.value.trim()) { try { await folderOp({action: "create", name: e.target.value.trim(), files}); pop.remove(); } catch (er) { /* toast shown */ } } };
}
let FV = {box: null, id: null, tab: "papers"}, pendingFT = null;
const FVT = {};                       // remembers which tab each folder was left on
function folderView(box, id) {
  const f = FOLDERS.find(x => x.id === id);
  if (!f) { box.innerHTML = `<div class="card empty"><h2>Folder not found</h2></div>`; return; }
  ["IdeasUI", "AskUI", "MatrixUI", "DiscoverUI", "DraftUI", "ClaimsUI", "SynthUI", "WorkspaceUI"].forEach(n => window[n] && window[n].init(ctx));
  if (pendingFT) { FVT[id] = pendingFT; pendingFT = null; }
  FV = {box, id, tab: window.IdeasUI ? (FVT[id] || "papers") : "papers"};
  const L = lib(), rows = L.map((r, i) => [r, i]).filter(([r]) => (r.folders || []).includes(id)), cards = rows.filter(([r]) => r.card).length;
  box.innerHTML = pageHead({tool: "folders", cls: "fv-head", icon: `<span class="fv-ico">${window.FoldersUI ? window.FoldersUI.icon() : "📁"}</span>`, eyebrow: "Folder", title: f.name,
      sub: `${rows.length} paper${rows.length === 1 ? "" : "s"} · ${cards} with a card · papers stay where they are; a folder is just a list`,
      actions: `<span class="menuwrap"><button class="btn small" id="fv-add">${ico("plus", "sm")} Add papers ${ico("down", "sm")}</button><div class="menu card hide" id="fv-addmenu"><button data-add="pdf">PDFs from my computer…</button><button data-add="lib">Papers already in my library…</button><div class="pmeta" style="padding:6px 10px">Or drop PDFs anywhere on this page.</div></div></span>
      <button class="btn small" id="fv-rename">Rename</button><button class="btn small danger" id="fv-del">Delete folder</button>
      <span class="menuwrap"><button class="btn small" id="fv-export">${ico("download", "sm")} Export ${ico("down", "sm")}</button><div class="menu card hide" id="fv-expmenu"></div></span>`}) + `
    ${window.IdeasUI ? `<div class="libtabs tooltabs fvtabs" style="margin-top:12px">${[["papers", "Papers", "papers"], ["ask", "Ask", "ask"], ["matrix", "Matrix", "matrix"], ["discover", "Discover", "discover"], ["evidence", "Evidence", "evidence"], ["synthesis", "Synthesis", "synthesis"], ["workspace", "Workspace", "workspace"], ["lab", "Ideas Lab", "ideas"], ["draft", "Related work", "draft"], ["claims", "Claim check", "claims"]].map(([k, t, i]) => `<button data-ft="${k}" data-tool="${i}">${ico(i)}${t}</button>`).join("")}</div>` : ""}
    <div id="fv-body" style="margin-top:14px"></div>`;
  $("#fv-add").onclick = e => { e.stopPropagation(); $("#fv-addmenu").classList.toggle("hide"); };
  $$("#fv-addmenu [data-add]").forEach(b => b.onclick = e => { e.stopPropagation(); $("#fv-addmenu").classList.add("hide"); const D = window.DropImport; if (D) b.dataset.add === "pdf" ? D.choosePdfs(id) : D.pickFromLibrary(id); });
  $("#fv-rename").onclick = async () => { const n = prompt("Rename folder:", f.name); if (n && n.trim()) { await folderOp({action: "rename", id, name: n.trim()}); folderView(box, id); ctx.renderNav?.(); } };
  $("#fv-del").onclick = async () => { if (confirm(`Delete the folder “${f.name}”? The papers are not deleted, but its Ideas Lab board, workspace, drafts and checks are.`)) { await folderOp({action: "delete", id}); ctx.go({j: null}); } };
  $("#fv-export").onclick = e => { e.stopPropagation(); exportMenu($("#fv-expmenu"), {folder: id}); };
  $$(".fvtabs button", box).forEach(b => b.onclick = () => folderTab(b.dataset.ft));
  showFolderTab();
}
function folderTab(t) { FVT[FV.id] = FV.tab = t; showFolderTab(); ctx.tabChanged && ctx.tabChanged(); }
function showFolderTab() {
  let body = $("#fv-body", FV.box); if (!body) return;
  const fresh = body.cloneNode(false); body.replaceWith(fresh); body = fresh;       // a new element for every tab, so a tab never keeps listeners or late renders from the one you left
  $$(".fvtabs button", FV.box).forEach(b => b.classList.toggle("on", b.dataset.ft === FV.tab));
  if (FV.tab === "papers") {
    const rows = lib().map((r, i) => [r, i]).filter(([r]) => (r.folders || []).includes(FV.id));
    body.innerHTML = (rows.length ? `<div class="reshead">${readyKey(rows.map(([r]) => r))}</div>` : "") + `<div class="papers" id="fv-list">${rows.map(([r, i]) => row(r, i)).join("") || `<div class="card empty"><h2>This folder is empty</h2>Drop PDFs here, use <b>＋ Add papers</b>, or use the 📁 button on any paper in the Library.</div>`}</div>`;
    bindRows(body);
  } else if (FV.tab === "ask") window.AskUI.mount(body, {folder: FV.id});
  else if (FV.tab === "matrix") window.MatrixUI.mount(body, FV.id);
  else if (FV.tab === "discover") window.DiscoverUI.mount(body, {folder: FV.id});
  else if (FV.tab === "draft") window.DraftUI.mount(body, FV.id);
  else if (FV.tab === "claims") window.ClaimsUI.mount(body, FV.id);
  else if (FV.tab === "synthesis") window.SynthUI.mount(body, FV.id);
  else if (FV.tab === "workspace") window.WorkspaceUI.mount(body, FV.id);
  else window.IdeasUI[FV.tab](body, FV.id);
}

// ---------------------------------------------------------------- exports
function exportMenu(menu, scope) {
  const q = scope.folder ? "folder=" + encodeURIComponent(scope.folder) : (scope.files || []).map(f => "file=" + encodeURIComponent(f)).join("&");
  const go = (what, extra = "") => () => { download(`/api/export/${what}?${[q, extra].filter(Boolean).join("&")}`); menu.classList.add("hide"); };
  const items = [["BibTeX (.bib) — Overleaf / LaTeX", go("bib")], ["RIS — EndNote / Mendeley", go("ris")], ["Notes & highlights — Markdown", go("notes", "fmt=md")], ["Notes & highlights — Word (.docx)", go("notes", "fmt=docx")]];
  if ((scope.files || []).length === 1) items.push(["Annotated PDF (highlights embedded)", go("pdf")]);
  items.push(["LaTeX: \\citep{…} with every key (copy)", async () => { menu.classList.add("hide"); try { const r = await fetch(`/api/export/cites?${q}`); if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || "Nothing to cite."); await navigator.clipboard.writeText((await r.text()).trim()); toast("Copied. The keys are the ones in the .bib export."); } catch (er) { notifyError(er); } }]);
  if (scope.folder) items.push(["Folder as a directory in my export folder (PDFs, .bib, notes)", async () => { menu.classList.add("hide"); try { const r = await api("/api/export/folderdir", {folder: scope.folder}); toast(`Copied ${r.pdfs} PDF${r.pdfs === 1 ? "" : "s"}${r.unchanged ? ` (${r.unchanged} already there)` : ""}, references.bib and notes.md to ${r.path}`); } catch (er) { notifyError(er); } }]);
  menu.innerHTML = items.map(([t], i) => `<button data-i="${i}">${esc(t)}</button>`).join("") + (scope.files || scope.folder ? "" : `<div class="pmeta" style="padding:6px 10px">Everything in the library. Tick papers or open a folder to export just those.</div>`);
  menu.querySelectorAll("button").forEach(b => b.onclick = e => { e.stopPropagation(); items[+b.dataset.i][1](); });
  menu.classList.toggle("hide");
}

// ---------------------------------------------------------------- add papers
function addDialog() {
  let d = $("#adddlg"); if (d) d.remove();
  d = document.createElement("dialog"); d.id = "adddlg"; d.className = "card adddlg";
  d.innerHTML = `<h2>Add a paper</h2><p class="pmeta">It is filed like every other paper: <code>Papers/YYYY-MM/&lt;Journal&gt;/</code>, with an entry in the index.</p>
    <label class="lbl">From a DOI (needs a free copy: open access, arXiv, SSRN…)</label><div class="row"><input type="text" id="ad-doi" placeholder="10.1287/isre.2024.0984  or  https://doi.org/…"><button class="btn primary" id="ad-doibtn">Add</button></div>
    <label class="lbl">Or a PDF from your computer (you check its details before it is added)</label><div class="dropzone" id="ad-drop">Drop a PDF here or <a href="#" id="ad-pick">choose a file</a><input type="file" id="ad-file" accept="application/pdf" multiple class="hide"></div>
    <div class="pmeta" style="margin-top:8px">Have a reference list from EndNote, Mendeley or Zotero? <a href="#" id="ad-bib">Import a bibliography (.ris / .bib)…</a></div>
    <label class="chk"><input type="checkbox" id="ad-sum"> Write a summary after adding a DOI (uses Claude, about 1–2 cents)</label>
    <div class="pmeta" id="ad-msg" style="min-height:20px;margin-top:8px"></div><div class="row" style="justify-content:flex-end"><button class="btn" id="ad-close">Close</button></div>`;
  document.body.append(d); d.showModal();
  const msg = t => { $("#ad-msg").textContent = t; };
  $("#ad-close").onclick = () => d.close();
  $("#ad-bib").onclick = e => { e.preventDefault(); d.close(); window.BibImport && window.BibImport.open(); };
  $("#ad-doibtn").onclick = async () => {
    msg("Looking it up…");
    try { const r = await api("/api/import/doi", {doi: $("#ad-doi").value, summarize: $("#ad-sum").checked}); msg(r.existing ? "Already in your library." : "Added: " + r.title); await ctx.reload(); results(); }
    catch (e) { msg(e.message); }
  };
  const files = fs => {       // the same review dialog as a drop on the Library: the details are checked before anything is filed
    if (!fs.length) return;
    if (!fs.some(f => /\.pdf$/i.test(f.name) || f.type === "application/pdf")) return msg("Those aren’t PDFs.");
    d.close(); window.DropImport.handle(fs, {kind: "library"});
  };
  $("#ad-pick").onclick = e => { e.preventDefault(); $("#ad-file").click(); };
  $("#ad-file").onchange = e => files([...e.target.files]);
  const dz = $("#ad-drop"); dz.ondragover = e => { e.preventDefault(); dz.classList.add("over"); }; dz.ondragleave = () => dz.classList.remove("over");
  dz.ondrop = e => { e.preventDefault(); e.stopPropagation(); dz.classList.remove("over"); files([...e.dataTransfer.files]); };
}

// ---------------------------------------------------------------- highlights hub
const H = {q: "", color: "", tag: "", folder: "", journal: ""};
async function highlightsHub() {
  const box = $("#lib-hl"); if (!box) return;
  const L = lib(), js = [...new Map(L.map(r => [r.abbr, r.journal])).entries()];
  box.innerHTML = `<div class="card libtools"><div class="row filters"><input type="search" id="h-q" placeholder="Search your highlights and comments…" value="${esc(H.q)}">
    <select id="h-color"><option value="">Any colour</option>${COLORS.map(c => `<option value="${c.id}"${H.color === c.id ? " selected" : ""}>${c.name}</option>`).join("")}</select>
    <select id="h-tag"><option value="">Any tag</option>${TAGS.map(t => `<option value="${esc(t.id)}"${H.tag === t.id ? " selected" : ""}>${esc((t.category ? t.category + ": " : "") + t.name)}</option>`).join("")}</select>
    <select id="h-folder"><option value="">Any folder</option>${FOLDERS.map(f => `<option value="${esc(f.id)}"${H.folder === f.id ? " selected" : ""}>${esc(f.name)}</option>`).join("")}</select>
    <select id="h-journal"><option value="">Any journal</option>${js.map(([a, n]) => `<option value="${esc(a)}"${H.journal === a ? " selected" : ""}>${esc(n)}</option>`).join("")}</select></div></div><div id="h-list"></div>`;
  const run = async () => {
    const d = await api("/api/library/highlights?" + q2s(H)), by = new Map();
    d.items.forEach(x => (by.get(x.file) || by.set(x.file, []).get(x.file)).push(x));
    $("#h-list").innerHTML = `<div class="reshead">${d.n} highlight${d.n === 1 ? "" : "s"} across ${by.size} paper${by.size === 1 ? "" : "s"}${d.n > d.items.length ? ` (showing the newest ${d.items.length})` : ""}</div>` +
      ([...by].map(([f, xs]) => `<div class="card hubpaper"><div class="hubtitle" data-hub="${esc(f)}">${esc(xs[0].title)}<small>${esc(xs[0].journal)} · ${esc(xs[0].year)}</small></div>` +
        xs.map(x => `<div class="hubhl" style="--hc:${colorOf(x.color)}" data-hub="${esc(f)}" data-p="${x.page}" data-y="${x.y}"><q>${esc(x.text || (x.kind === "note" ? "📝 " + x.comment : "▭ Figure or table"))}</q><span class="pmeta">p. ${x.page}</span>${x.comment && x.text ? `<div class="cm">${esc(x.comment)}</div>` : ""}${x.tags.map(id => TAGS.find(t => t.id === id)).filter(Boolean).map(t => `<span class="tgc" style="--tc:${esc(t.color)}">${esc((t.category ? t.category + ": " : "") + t.name)}</span>`).join("")}</div>`).join("") + `</div>`).join("") ||
        `<div class="card empty"><h2>No highlights yet</h2>Open a paper with <b>Read</b>, select text and pick a colour.</div>`);
    $$("#h-list [data-hub]").forEach(el => el.onclick = e => { e.stopPropagation(); const i = byFile(el.dataset.hub), hit = d.items.find(x => x.file === el.dataset.hub && String(x.page) === el.dataset.p && String(x.y) === el.dataset.y);
      if (i >= 0) ctx.readPaper(lib()[i], el.dataset.p ? {goto: {page: +el.dataset.p, y: Math.max(0, +el.dataset.y - 0.04), rects: hit ? hit.rects : null}} : undefined); });
  };
  const rd = debounce(run, 250);
  $("#h-q").oninput = e => { H.q = e.target.value; rd(); };
  [["h-color", "color"], ["h-tag", "tag"], ["h-folder", "folder"], ["h-journal", "journal"]].forEach(([id, k]) => $("#" + id).onchange = e => { H[k] = e.target.value; run(); });
  run();
}

// ---------------------------------------------------------------- tag manager (library-wide)
async function tagManager() {
  const box = $("#lib-tags"); if (!box) return;
  await loadTags();
  const use = await api("/api/tags/usage").catch(() => ({})), full = await api("/api/tags");
  box.innerHTML = `<div class="card libtools"><div class="pmeta">Tags are shared by every paper. Renaming changes them everywhere; merging or deleting rewrites your notes in all papers (close the reader first).</div></div>
    <div class="card tagtable">${TAGS.map(t => `<div class="tagrow" data-id="${esc(t.id)}"><input type="color" value="${esc(t.color)}" data-k="color"><input type="text" value="${esc((t.category ? t.category + ": " : "") + t.name)}" data-k="name">
      <span class="pmeta">${use[t.id] || 0} use${use[t.id] === 1 ? "" : "s"}</span><select data-k="merge"><option value="">Merge into…</option>${TAGS.filter(x => x.id !== t.id).map(x => `<option value="${esc(x.id)}">${esc((x.category ? x.category + ": " : "") + x.name)}</option>`).join("")}</select>
      <button class="btn small danger" data-k="del">Delete</button></div>`).join("") || `<div class="empty">No tags yet — add one in the reader's Highlights tab.</div>`}</div>`;
  let rev = full.rev;
  const save = async () => { try { const r = await api("/api/tags", {rev, tags: TAGS}); rev = r.rev; } catch (e) { notifyError(e); } };
  $$(".tagrow", box).forEach(rw => {
    const t = TAGS.find(x => x.id === rw.dataset.id), k = s => $(`[data-k="${s}"]`, rw);
    k("color").onchange = e => { t.color = e.target.value; save(); };
    k("name").onchange = e => { const v = e.target.value, i = v.indexOf(":"); t.category = i > 0 ? v.slice(0, i).trim() : ""; t.name = (i > 0 ? v.slice(i + 1) : v).trim(); save(); };
    k("merge").onchange = async e => { if (e.target.value && confirm("Merge this tag into the other one in all papers?")) { const r = await api("/api/tags/retag", {from: t.id, into: e.target.value}); toast(`Merged (${r.changed} items updated).`); await loadTags(); tagManager(); } };
    k("del").onclick = async () => { if (confirm(`Delete the tag “${t.name}” from all highlights and notes?`)) { const r = await api("/api/tags/retag", {from: t.id}); toast(`Deleted (${r.changed} items updated).`); await loadTags(); tagManager(); } };
  });
}

// ---------------------------------------------------------------- public
function openTab(t) { F.tab = t; if (ctx.go && t === "folders") return ctx.go({view: "lib", j: null, tab: "folders"}); if (TOP) tabs(TOP.el, TOP.opts); }
window.LibraryUI = {
  init(c) { ctx = c; const wire = () => ["IdeasUI", "AskUI", "MatrixUI", "DiscoverUI", "FoldersUI", "DropImport", "Palette", "Activity", "DraftUI", "ClaimsUI", "SynthUI", "ReviewUI", "WorkspaceUI", "BibImport"].forEach(n => window[n] && window[n].init(c)); wire(); addEventListener("load", wire, {once: true});   /* the other tab modules may still be loading */ wireRows(); return Promise.all([loadFolders(), loadTags()]); },
  row, readyKey, top, shelf, folderView, folderName, folderTab, addDialog, openTab,
  refreshFolders: loadFolders, state: F,
  folderCount: () => FOLDERS.length, folders: () => FOLDERS,
  updateFolderHead() {                                                                          // the counts under a folder's name, after cards were made or papers added
    if (!FV.id || !$("#libview:not(.hide) #fv-body")) return; const rows = lib().filter(r => (r.folders || []).includes(FV.id)), m = $("#libview .pagehead .meta");
    if (m) m.textContent = `${rows.length} paper${rows.length === 1 ? "" : "s"} · ${rows.filter(r => r.card).length} with a card · papers stay where they are; a folder is just a list`;
  },
  folderTabName: () => FV.tab,                                                                  // for the address bar: #/folder/<id>/<tab>
  setFolderTab: t => { if (["papers", "ask", "matrix", "discover", "evidence", "lab", "draft", "claims", "synthesis", "workspace"].includes(t)) pendingFT = t; },
  currentFolder: () => ($("#libview:not(.hide) #fv-body") && FV.id) || null,                 // the id of the folder whose page is showing
  async refreshView() {                                                                         // re-draw what is on screen after papers were added from outside
    await loadFolders(); await ctx.reload();
    if ($("#libview:not(.hide) #fv-body")) return folderView(FV.box, FV.id);
    const el = $("#libview"); if (el && $("#lf-folder")) fillControls(el); results(); if (F.tab === "books") booksTab();
  },
};
