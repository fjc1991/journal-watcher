// Library tab "Review": flashcards from your paper cards and a short daily review. A card is a question, an answer and the place in the paper it came from; you answer from memory,
// show the answer and say how it went (Again / Hard / Good / Easy, or keys 1-4) and the card comes back after a longer or shorter gap (plain SM-2; at most N new cards a day).
// Make cards from papers that have a card (one model call per six papers; every card has to rest on notes it was given, and carries the page it came from), add your own,
// browse and edit all of them, suspend or delete any. Saved in Papers/_flashcards.json. Server: GET /api/flashcards, POST /api/flashcards/{rate,add,update,delete,settings}, POST /api/ai/flashcards[/estimate].
import {$, $$, esc, ico, api, toast, notifyError, errInline, debounce, progress, aboutCost} from "./state.js";

let ctx = null;
const R = {el: null, d: null, view: "study", queue: [], shown: false, done: 0, total: 0, folder: "", sel: new Set(), per: 8, q: "", showSusp: false, edit: null, busy: false, last: null};
const money = aboutCost;
const days = n => n <= 0 ? "today" : n === 1 ? "1 day" : n < 14 ? n + " days" : n < 60 ? Math.round(n / 7) + " weeks" : Math.round(n / 30) + " months";
const rowOf = f => ctx.getLib().find(r => r.file === f);
const cite = f => { const r = rowOf(f); if (!r) return "a paper"; const a = Array.isArray(r.authors) ? r.authors : String(r.authors || "").split(/;\s*/); const nm = (a[0] || "").trim(), s = nm.includes(",") ? nm.split(",")[0].trim() : nm.split(/\s+/).pop(); return (s || r.title.slice(0, 24)) + (a.length > 1 ? " et al." : "") + (r.year ? " (" + r.year + ")" : ""); };

export async function mount(el) {
  R.el = el; el.innerHTML = `<div class="card ev-card">Loading your cards…</div>`;
  if (!el._rv) { el._rv = true; el.addEventListener("click", onClick); el.addEventListener("change", onChange); el.addEventListener("input", onInput); }
  if (!R.keys) { R.keys = true; document.addEventListener("keydown", onKey); }
  await load(); R.view = "study"; startSession(); draw();
}
async function load() {
  R.d = await api("/api/flashcards?all=1" + (R.folder ? "&folder=" + encodeURIComponent(R.folder) : ""));
  badge(R.d.all_counts);
}
function badge(c) { const g = document.getElementById("reviewcount"); if (g) g.textContent = (c.due + c.new_today) || ""; const b = document.querySelector('.libtabs [data-tab="review"]'); if (!b) return; let n = b.querySelector(".nn"); if (!n) { n = document.createElement("span"); n.className = "nn"; b.append(n); } const k = c.due + c.new_today; n.textContent = k || ""; }
export async function refreshBadge() { try { badge((await api("/api/flashcards")).all_counts); } catch (e) { /* no flashcards yet */ } }
function startSession() { R.queue = R.d.queue.slice(); R.shown = false; R.done = 0; R.again = 0; R.total = R.queue.length; }

// ---------------------------------------------------------------- the page
function draw() {
  const d = R.d, c = d.counts, folders = (window.LibraryUI && window.LibraryUI.folders && window.LibraryUI.folders()) || [];
  R.el.innerHTML = `<div class="card rv-head" data-tool="review"><div class="dr-h"><span class="of-ico">${ico("review")}</span><div><div class="eyebrow t">Review</div><div class="of-title">${c.due + c.new_today ? `${c.due + c.new_today} card${c.due + c.new_today === 1 ? "" : "s"} for today` : "Nothing is due today"}</div></div></div>
    <div class="rv-stats"><span><b>${c.due}</b> due</span><span><b>${c.new_today}</b> new today <small>(of ${c.new} not yet seen)</small></span><span><b>${c.total}</b> cards</span><span><b>${c.learned}</b> learned</span>${c.suspended ? `<span>${c.suspended} suspended</span>` : ""}</div>
    <div class="row rv-bar"><div class="seg">${[["study", "Study"], ["browse", "Browse cards"], ["make", "Make cards from papers"], ["add", "＋ Add a card"]].map(([k, t]) => `<button class="${R.view === k ? "on" : ""}" data-rv="view" data-v="${k}">${t}</button>`).join("")}</div>
      <label class="rv-scope">Cards from <select id="rv-folder"><option value="">all my papers</option>${folders.map(f => `<option value="${esc(f.id)}"${R.folder === f.id ? " selected" : ""}>📁 ${esc(f.name)}</option>`).join("")}</select></label></div></div>
    <div id="rv-body">${body()}</div>`;
  if (R.view === "make") estimate();
}
function body() {
  if (R.view === "study") return study();
  if (R.view === "browse") return browse();
  if (R.view === "make") return make();
  return addForm();
}

// ---------------------------------------------------------------- study
function study() {
  const c = R.queue[0], d = R.d;
  if (!d.all_counts.total) return `<div class="card empty"><h2>No flashcards yet</h2>Make cards from the papers you have summarised (they need a paper card), or add your own.<div class="row" style="justify-content:center;margin-top:10px"><button class="btn primary" data-rv="view" data-v="make">Make cards from papers</button></div></div>`;
  if (!c) { const nx = (d.cards || []).filter(x => !x.suspended && x.due && x.reps).sort((a, b) => a.due.localeCompare(b.due))[0];
    return `<div class="card rv-done"><div class="rv-big">${ico("check", "lg")}</div><h2>${R.total ? "That’s all for today" : "Nothing is due today"}</h2><div class="pmeta">${R.total ? `You went through ${R.total} card${R.total === 1 ? "" : "s"}. ` : ""}${nx ? `The next card is due ${nx.due === new Date().toISOString().slice(0, 10) ? "today" : "on " + nx.due}.` : ""}</div>
      <div class="row" style="justify-content:center;margin-top:10px">${d.counts.new > d.counts.new_today ? `<button class="btn" data-rv="more">Learn 5 more new cards</button>` : ""}<button class="btn" data-rv="view" data-v="browse">Browse cards</button></div></div>`; }
  const pv = c.preview || {};
  return `<div class="rv-prog"><i style="width:${R.total ? Math.round(R.done / R.total * 100) : 0}%"></i></div><div class="pmeta">${R.done} of ${R.total}${R.again ? ` · ${R.again} came back to be repeated` : ""}</div>
    <div class="card rv-card"><div class="rv-src">${c.file ? `<button class="linkbtn" data-rv="open" data-f="${esc(c.file)}" data-p="${c.page || ""}" data-qt="${esc(c.quote || "")}" title="Open the paper at the place this came from">${esc(cite(c.file))}${c.page ? ", p. " + c.page : ""}</button>` : "my own card"}${!c.reps ? ` <span class="of-badge">new</span>` : ""}</div>
      <div class="rv-q">${esc(c.q)}</div>${R.shown ? `<hr><div class="rv-a">${esc(c.a)}</div>` : ""}</div>
    ${R.shown ? `<div class="rv-rate">${[[1, "Again", "again"], [2, "Hard", "hard"], [3, "Good", "good"], [4, "Easy", "easy"]].map(([k, t, cl]) => `<button class="btn rv-r ${cl}" data-rv="rate" data-r="${k}"><b>${t}</b><small>${k === 1 ? "again today" : days(pv[k])}</small><kbd>${k}</kbd></button>`).join("")}</div>`
      : `<div class="rv-rate"><button class="btn primary rv-show" data-rv="show">Show answer <kbd>Space</kbd></button></div>`}
    <div class="pmeta rv-tools"><button class="linkbtn" data-rv="edit1" title="Fix this card">edit</button> · <button class="linkbtn" data-rv="susp1" title="Stop showing this card">suspend</button></div>`;
}

// ---------------------------------------------------------------- browse
function browse() {
  const ws = R.q.toLowerCase().split(/\s+/).filter(Boolean), cs = (R.d.cards || []).filter(c => (R.showSusp || !c.suspended) && ws.every(w => (c.q + " " + c.a + " " + cite(c.file)).toLowerCase().includes(w)));
  return `<div class="row rv-find"><input type="search" id="rv-q" placeholder="Search your cards…" value="${esc(R.q)}"><label class="chk"><input type="checkbox" id="rv-susp"${R.showSusp ? " checked" : ""}> show suspended</label><span class="pmeta">${cs.length} card${cs.length === 1 ? "" : "s"}</span>
      <label class="rv-scope">New cards a day <input type="number" id="rv-npd" min="0" max="100" value="${R.d.settings.new_per_day}" style="width:60px"></label></div>
    <div class="rv-list">${cs.slice(0, 200).map(c => R.edit === c.id ? `<div class="rv-item edit" data-id="${c.id}"><textarea id="rv-eq" rows="2">${esc(c.q)}</textarea><textarea id="rv-ea" rows="3">${esc(c.a)}</textarea><div class="row"><button class="btn primary small" data-rv="saveedit" data-id="${c.id}">Save</button><button class="btn small" data-rv="canceledit">Cancel</button></div></div>`
      : `<div class="rv-item${c.suspended ? " susp" : ""}" data-id="${c.id}"><div class="rv-iq">${esc(c.q)}</div><div class="rv-ia">${esc(c.a)}</div><div class="pmeta">${c.file ? `<button class="linkbtn" data-rv="open" data-f="${esc(c.file)}" data-p="${c.page || ""}" data-qt="${esc(c.quote || "")}">${esc(cite(c.file))}${c.page ? ", p. " + c.page : ""}</button> · ` : ""}${!c.reps && !c.introduced ? "new" : "due " + (c.due || "")}${c.lapses ? " · " + c.lapses + " lapse" + (c.lapses === 1 ? "" : "s") : ""}${c.suspended ? " · suspended" : ""}
        · <button class="linkbtn" data-rv="edit" data-id="${c.id}">edit</button> · <button class="linkbtn" data-rv="susp" data-id="${c.id}">${c.suspended ? "resume" : "suspend"}</button> · <button class="linkbtn" data-rv="del" data-id="${c.id}">delete</button></div></div>`).join("") || `<div class="card empty">No card matches.</div>`}${cs.length > 200 ? `<div class="pmeta">Showing the first 200.</div>` : ""}</div>`;
}

// ---------------------------------------------------------------- make and add
function make() {
  const L = ctx.getLib().filter(r => r.card && (!R.folder || (r.folders || []).includes(R.folder))), have = new Map();
  (R.d.cards || []).forEach(c => have.set(c.file, (have.get(c.file) || 0) + 1));
  return `<div class="card rv-make"><div class="of-title">Make cards from papers</div><div class="pmeta">Only papers that have a card can be used (make cards in a folder’s Evidence tab, or the reader’s Card tab). The model sees the card’s notes, not the PDF; every flashcard has to rest on notes it was given and remembers the page.</div>
    <div class="dr-papers"><div class="eyebrow">Papers <small>${R.sel.size} ticked of ${L.length}</small> <button class="linkbtn" data-rv="selall">all without cards yet</button> · <button class="linkbtn" data-rv="selnone">none</button></div>
      ${L.map(r => `<label class="dr-p"><input type="checkbox" data-rvp="${esc(r.file)}"${R.sel.has(r.file) ? " checked" : ""}> <b>${esc(cite(r.file))}</b> ${esc((r.title || "").slice(0, 90))}${have.get(r.file) ? ` <span class="of-badge">${have.get(r.file)} cards</span>` : ""}</label>`).join("") || `<div class="pmeta">No paper here has a card yet.</div>`}</div>
    <div class="row dr-go"><label class="cl-scope">Cards per paper <select id="rv-per">${[4, 8, 12].map(n => `<option${R.per === n ? " selected" : ""}>${n}</option>`).join("")}</select></label>
      <button class="btn primary" data-rv="go"${R.busy || !R.sel.size ? " disabled" : ""}>${ico("review", "sm")} Make the cards<span id="rv-est"></span></button><span class="pmeta" id="rv-st">${R.sel.size ? "" : "Tick at least one paper."}</span></div></div>`;
}
function addForm() {
  const L = ctx.getLib();
  return `<div class="card rv-make"><div class="of-title">Add a card</div><label class="cl-lab">Question<textarea id="rv-nq" rows="2" maxlength="500" placeholder="What does … mean? / What did … find?"></textarea></label><label class="cl-lab">Answer<textarea id="rv-na" rows="3" maxlength="900"></textarea></label>
    <label class="cl-lab">Paper <small>(optional)</small><select id="rv-nf"><option value="">none</option>${L.map(r => `<option value="${esc(r.file)}">${esc(cite(r.file))} — ${esc((r.title || "").slice(0, 70))}</option>`).join("")}</select></label>
    <div class="row" style="margin-top:8px"><button class="btn primary" data-rv="addcard">Add the card</button></div></div>`;
}
let seq = 0;
const estimate = debounce(async () => {
  const my = ++seq, e = $("#rv-est", R.el); if (!e || !R.sel.size) { if (e) e.textContent = ""; return; }
  try { const r = await api("/api/ai/flashcards/estimate", {files: [...R.sel], per: R.per}); if (my === seq && $("#rv-est", R.el)) $("#rv-est", R.el).textContent = r.cost != null ? " · " + money(r.cost) : ""; } catch (er) { /* a courtesy */ }
}, 350);

// ---------------------------------------------------------------- actions
function onInput(e) { if (e.target.id === "rv-q") { R.q = e.target.value; const l = $("#rv-body", R.el); const pos = e.target.selectionStart; l.innerHTML = browse(); const q = $("#rv-q", R.el); q.focus(); q.setSelectionRange(pos, pos); } }
async function onChange(e) {
  const t = e.target;
  if (t.id === "rv-folder") { R.folder = t.value; await load(); startSession(); return draw(); }
  if (t.id === "rv-susp") { R.showSusp = t.checked; return void ($("#rv-body", R.el).innerHTML = browse()); }
  if (t.id === "rv-per") { R.per = +t.value; return estimate(); }
  if (t.id === "rv-npd") { try { await api("/api/flashcards/settings", {new_per_day: +t.value}); await load(); toast("Saved."); } catch (er) { notifyError(er); } return; }
  if (t.matches("[data-rvp]")) { t.checked ? R.sel.add(t.dataset.rvp) : R.sel.delete(t.dataset.rvp); return draw(); }
}
async function onClick(e) {
  const b = e.target.closest("[data-rv]"); if (!b) return; const a = b.dataset.rv, id = b.dataset.id;
  try {
    if (a === "view") { R.view = b.dataset.v; R.edit = null; if (R.view === "study") { await load(); startSession(); } return draw(); }
    if (a === "show") { R.shown = true; return void ($("#rv-body", R.el).innerHTML = study()); }
    if (a === "rate") return rate(+b.dataset.r);
    if (a === "open") { const r = rowOf(b.dataset.f); if (!r) return toast("That paper is no longer in your library."); return ctx.readPaper(r, b.dataset.p ? {goto: {page: +b.dataset.p, quote: b.dataset.qt || ""}} : {tab: "card"}); }
    if (a === "more") { await api("/api/flashcards/settings", {new_per_day: R.d.settings.new_per_day + 5}); await load(); startSession(); return draw(); }
    if (a === "edit1") { R.view = "browse"; R.edit = R.queue[0].id; return draw(); }
    if (a === "susp1") { const c = R.queue.shift(); await api("/api/flashcards/update", {id: c.id, suspended: true}); R.total--; R.shown = false; await load(); return void ($("#rv-body", R.el).innerHTML = study()); }
    if (a === "edit") { R.edit = id; return void ($("#rv-body", R.el).innerHTML = browse()); }
    if (a === "canceledit") { R.edit = null; return void ($("#rv-body", R.el).innerHTML = browse()); }
    if (a === "saveedit") { await api("/api/flashcards/update", {id, q: $("#rv-eq", R.el).value, a: $("#rv-ea", R.el).value}); R.edit = null; await load(); return draw(); }
    if (a === "susp") { const c = R.d.cards.find(x => x.id === id); await api("/api/flashcards/update", {id, suspended: !c.suspended}); await load(); return draw(); }
    if (a === "del") { if (!confirm("Delete this card? Its review history goes with it.")) return; await api("/api/flashcards/delete", {ids: [id]}); await load(); return draw(); }
    if (a === "selall") { const have = new Set((R.d.cards || []).map(c => c.file)); ctx.getLib().filter(r => r.card && !have.has(r.file) && (!R.folder || (r.folders || []).includes(R.folder))).forEach(r => R.sel.add(r.file)); return draw(); }
    if (a === "selnone") { R.sel.clear(); return draw(); }
    if (a === "go") return generate();
    if (a === "addcard") { await api("/api/flashcards/add", {q: $("#rv-nq", R.el).value, a: $("#rv-na", R.el).value, file: $("#rv-nf", R.el).value}); toast("Card added."); await load(); R.view = "browse"; return draw(); }
  } catch (er) { notifyError(er); }
}
async function rate(r) {
  const c = R.queue.shift(); if (!c) return;
  try { const res = await api("/api/flashcards/rate", {id: c.id, rating: r}); badge(res.counts); R.d.all_counts = res.counts;
    if (r === 1) { R.again++; R.queue.push({...res.card, preview: await previewOf(res.card)}); } else R.done++;
    R.shown = false; $("#rv-body", R.el).innerHTML = study(); if (!R.queue.length) { await load(); $("#rv-body", R.el).innerHTML = study(); } }
  catch (er) { R.queue.unshift(c); notifyError(er); }
}
async function previewOf(c) { const q = (await api("/api/flashcards")).queue.find(x => x.id === c.id); return q ? q.preview : {1: 0, 2: 1, 3: 1, 4: 4}; }
function onKey(e) {
  if (R.view !== "study" || !R.el || !R.el.isConnected || R.el.closest(".hide") || document.body.classList.contains("reading") || e.target.closest("input, textarea, select") || e.metaKey || e.ctrlKey || e.altKey) return;
  if (!R.queue.length) return;
  if (!R.shown && (e.key === " " || e.key === "Enter")) { e.preventDefault(); R.shown = true; $("#rv-body", R.el).innerHTML = study(); }
  else if (R.shown && /^[1-4]$/.test(e.key)) { e.preventDefault(); rate(+e.key); }
}
async function generate() {
  if (R.busy || !R.sel.size) return; R.busy = true; const st = () => $("#rv-st", R.el); const btn = $('[data-rv="go"]', R.el); if (btn) btn.disabled = true;
  try {
    const r = await fetch("/api/ai/flashcards", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({files: [...R.sel], per: R.per})});
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
    const rd = r.body.getReader(), dec = new TextDecoder(); let buf = "", res = null, done = null;
    for (;;) {
      const {value, done: fin} = await rd.read(); if (fin) break; buf += dec.decode(value, {stream: true}); let i;
      while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1); if (!line) continue; const m = JSON.parse(line);
        if (m.type === "status" && st()) progress(st(), m); else if (m.type === "cards") res = m; else if (m.type === "done") done = m; else if (m.type === "error") throw new Error(m.error);
      }
    }
    R.busy = false; R.sel.clear(); await load(); startSession(); R.view = "study"; draw();
    toast(`${res ? res.added : 0} card${res && res.added === 1 ? "" : "s"} added${res && res.skipped ? " · " + res.skipped + " were already yours" : ""}${res && res.dropped ? " · " + res.dropped + " dropped (no note behind them)" : ""}${done && done.cost != null ? " · " + money(done.cost) : ""}.`);
  } catch (e) { R.busy = false; draw(); if (st()) st().innerHTML = errInline(e); if (!/Cancelled/.test(e.message)) notifyError(e); }
}

window.ReviewUI = {init(c) { ctx = c; }, mount, refreshBadge};
