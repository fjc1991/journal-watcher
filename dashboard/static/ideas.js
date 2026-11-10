// Folder tabs "Evidence" and "Ideas Lab" (the Papers tab is library.js).
//   Evidence: how many papers have cards, the topics / theories / methods they share, and their limitations and future-research
//             suggestions grouped by meaning — a limitation several papers share is a gap.
//   Ideas Lab: your brief -> current events from a web search (you untick or add) -> ideas generated from the evidence, scored by a tough
//             "editor" and checked against OpenAlex -> a board you star, shortlist, annotate, develop into an outline and export.
// Server streams are newline-delimited JSON ({type: status|progress|events|ideas|text|paper|done|error}).
import {errInline, errBox, notifyError, $, $$, esc, ico, api, toast, debounce, md, progress, aboutCost, exactCost, budgetOk} from "./state.js";
import {frame, kv, callout, mdParts, sectionsHtml, wire as wireOut} from "./output.js";

let ctx = null;
const money = aboutCost;
const lib = () => (ctx ? ctx.getLib() : []);
const L = {fid: null, doc: null, cards: null, est: null, filter: "all", sort: "score", busy: false, itab: {}, showRejected: false};

async function ndjson(url, body, onMsg) {
  const r = await fetch(url, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
  const rd = r.body.getReader(), dec = new TextDecoder(); let buf = "";
  for (;;) {
    const {value, done} = await rd.read(); if (done) break; buf += dec.decode(value, {stream: true}); let i;
    while ((i = buf.indexOf("\n")) >= 0) { const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1); if (line) onMsg(JSON.parse(line)); }
  }
}
function openAt(file, page, quote) {
  const row = lib().find(r => r.file === file);
  if (row) ctx.readPaper(row, quote ? {goto: {page: page || 1, quote}} : {goto: {page: page || 1}, tab: "card"}); else toast("That paper is no longer in your library.");
}
const dl = url => { const a = document.createElement("a"); a.href = url; a.download = ""; document.body.append(a); a.click(); a.remove(); };

// ================================================================ Evidence
export async function evidence(el, fid) {
  el.innerHTML = `<div class="card ev-card">Reading the paper cards…</div>`;
  let r;
  try { r = await api("/api/folder/evidence?id=" + encodeURIComponent(fid)); }
  catch (e) { el.innerHTML = `<div class="card empty"><h2>Evidence isn’t available</h2>${esc(e.message)}</div>`; return; }
  const ev = r.evidence, c = r.cards, byId = Object.fromEntries(ev.papers.map(p => [p.id, p]));
  const short = t => t.length > 64 ? t.slice(0, 62).replace(/\s+\S*$/, "") + "…" : t;
  const chip = x => `<span class="ev-chip" title="${esc(x.name + " — " + x.papers.map(i => byId[i] ? byId[i].cite : i).join("; "))}">${esc(short(x.name))}${Object.keys(x.extra || {}).length ? ` <small>${Object.entries(x.extra).map(([k, v]) => k + (v > 1 ? "×" + v : "")).join(", ")}</small>` : ""} <b>${x.n}</b></span>`;
  // chips shared by two or more papers first and prominent; what only one paper mentions folds away
  const names = (xs, n = 14) => { const shared = xs.filter(x => x.n >= 2), one = xs.filter(x => x.n < 2); if (!xs.length) return `<span class="pmeta">—</span>`;
    return (shared.length ? shared.slice(0, n).map(chip).join("") : "") + (one.length ? (shared.length ? `<details class="ev-one"><summary>${one.length} only in one paper</summary>${one.slice(0, 40).map(chip).join("")}</details>` : one.slice(0, n).map(chip).join("")) : ""); };
  const src = x => { const p = byId[x.paper] || {}; return `<button class="linkbtn" data-open="${esc(p.file)}" data-page="${x.page || 1}" data-q="${esc(x.quote || "")}">${esc(p.cite || x.paper)}${x.page ? ", p. " + x.page : ""}</button>${x.stated === false ? ` <span class="of-badge inferred">inferred</span>` : ""}`; };
  const cluster = cl => cl.items.length === 1 && cl.n < 2 ? `<div class="ev-cl one"><div class="ev-clh"><span class="ev-id">${cl.id}</span><span class="ev-lbl">${esc(cl.label)} ${src(cl.items[0])}</span></div></div>` : `<div class="ev-cl"><div class="ev-clh"><span class="ev-id">${cl.id}</span><span class="ev-n" title="${esc(cl.papers.map(i => byId[i] ? byId[i].cite : i).join("; "))}">${cl.n} paper${cl.n === 1 ? "" : "s"}</span><span class="ev-lbl">${esc(cl.label)}</span></div>
      <details><summary>${cl.items.length} sentence${cl.items.length === 1 ? "" : "s"}</summary>${cl.items.map(x => { const p = byId[x.paper] || {};
        return `<div class="ev-it"><button class="linkbtn" data-open="${esc(p.file)}" data-page="${x.page || 1}" data-q="${esc(x.quote || "")}">${esc(p.cite || x.paper)}${x.page ? ", p. " + x.page : ""}</button>${x.stated === false ? ` <span class="of-badge inferred">inferred</span>` : ""} ${esc(x.text)}</div>`; }).join("")}</details></div>`;
  const clusters = cs => { const shared = cs.filter(x => x.n >= 2), one = cs.filter(x => x.n < 2); if (!cs.length) return `<div class="pmeta">Nothing yet.</div>`;
    if (!shared.length) return `<div class="of-co">${ico("info", "sm")}<div>No point is shared by two or more of these papers yet. Shared points show up when a folder holds several papers on one topic.</div></div>` + one.slice(0, 6).map(cluster).join("") + (one.length > 6 ? `<details class="ev-one"><summary>${one.length - 6} more, each from a single paper</summary>${one.slice(6, 40).map(cluster).join("")}</details>` : "");
    return shared.slice(0, 12).map(cluster).join("") + (one.length ? `<details class="ev-one"><summary>${one.length} point${one.length === 1 ? "" : "s"} from a single paper</summary>${one.slice(0, 30).map(cluster).join("")}</details>` : ""); };
  const group = (title, xs, n) => `<div class="ev-sec"><h4>${title} <span class="of-n">${xs.length}</span></h4>${names(xs, n)}</div>`;
  const nShared = xs => xs.filter(x => x.n >= 2).length;
  const sections = [
    {id: "cards", title: "Cards", icon: "card", count: `${c.have}/${c.total}`, collapsed: !c.missing && !!c.have, body: `<div>${c.have === 1 ? "1 paper has" : c.have + " papers have"} a card out of ${c.total}.${ev.stale.length ? ` <span class="of-badge inferred">${ev.stale.length} may be out of date</span>` : ""}</div><div id="ev-prog" class="ev-prog hide"></div>
      ${c.missing && c.have ? `<div class="pmeta" style="margin-top:6px">Without a card: ${ev.missing.map(m => esc(m.title)).join(" · ")}</div>` : ""}
      ${!c.have ? `<div class="pmeta" style="margin-top:8px">A paper card is a short, cited summary of one paper (question, theory, method, findings, limitations, future research). It is what this tab and the Ideas Lab read. You can also make one in the reader’s <b>Card</b> tab.</div>` : ""}`},
    c.have ? {id: "about", title: "What these papers are about", short: "About", icon: "ideas", count: nShared(ev.topics) + nShared(ev.theories) + nShared(ev.methods) + nShared(ev.constructs) + " shared", body: group("Topics", ev.topics) + group("Theories", ev.theories) + group("Methods", ev.methods) + group("Constructs", ev.constructs, 20) + `<div class="pmeta">The number is how many papers share it; hover for which.</div>`} : null,
    c.have ? {id: "limits", title: "Limitations, grouped by meaning", short: "Limitations", icon: "warn", tone: "warn", count: ev.limitations.filter(x => x.n >= 2).length + " shared", body: `<div class="pmeta">A point several papers share is a gap in this part of the literature. Most shared first.</div>` + clusters(ev.limitations)} : null,
    c.have ? {id: "future", title: "Future research the authors suggest", short: "Future research", icon: "discover", count: ev.future.filter(x => x.n >= 2).length + " shared", body: clusters(ev.future)} : null,
    c.have ? {id: "papers", title: "The papers", icon: "papers", count: ev.papers.length, collapsed: ev.papers.length > 6, body: ev.papers.map(p => `<div class="ev-paper"><b>${esc(p.cite)}</b> <span class="pmeta">${esc(p.kind)}${p.design ? " · " + esc(p.design) : ""}</span><div>${esc(p.question || "")}</div><button class="linkbtn" data-open="${esc(p.file)}" data-page="1" data-q="">Open the card</button></div>`).join("")} : null,
  ];
  el.innerHTML = frame({tool: "evidence", kind: "Evidence", icon: "evidence", key: "evidence:" + fid, title: c.have ? `What ${c.have} paper${c.have === 1 ? "" : "s"} say together` : "No cards yet", sections,
    meta: [{t: `${c.have} of ${c.total} with a card`}, c.have ? {t: nShared(ev.limitations) + " shared limitation" + (nShared(ev.limitations) === 1 ? "" : "s")} : null, ev.stale.length ? {t: ev.stale.length + " out of date", cls: "edited"} : null],
    actions: `${c.missing ? `<button class="btn primary small" id="ev-make">${ico("card", "sm")} Make ${c.missing} card${c.missing === 1 ? "" : "s"}${r.estimate.cards != null ? " · " + money(r.estimate.cards) : ""}</button>` : ""}${c.have ? `<button class="btn small" id="ev-exp">${ico("download", "sm")} Export cards (Markdown)</button>` : ""}`});
  wireOut(el);
  el.onclick = e => { const o = e.target.closest("[data-open]"); if (o) openAt(o.dataset.open, +o.dataset.page, o.dataset.q); };
  $("#ev-exp", el)?.addEventListener("click", () => dl("/api/export/cards?folder=" + encodeURIComponent(fid)));
  $("#ev-make", el)?.addEventListener("click", async () => {
    if (!(await budgetOk(r.estimate.cards))) return;
    const prog = $("#ev-prog", el), btn = $("#ev-make", el); btn.disabled = true; prog.classList.remove("hide"); prog.innerHTML = "Starting…"; let total = 0, ok = 0; const errs = [];
    try {
      await ndjson("/api/ai/cards", {folder: fid}, m => {
        if (m.type === "start") { total = m.total; prog.innerHTML = `Making ${total} card${total === 1 ? "" : "s"}, three at a time…`; }
        else if (m.type === "paper") { ok = m.done; prog.innerHTML = `<b>${ok} of ${total}</b> done · last: ${esc(m.title.slice(0, 70))}${errs.length ? ` · ${errs.length} failed` : ""}`; }
        else if (m.type === "error") errs.push(m);
        else if (m.type === "done") { toast(`Made ${m.made} card${m.made === 1 ? "" : "s"}${m.cost ? " · " + exactCost(m.cost) : ""}.`);
          prog.innerHTML = `Made <b>${m.made}</b> card${m.made === 1 ? "" : "s"}${m.cost ? " · " + exactCost(m.cost) : ""}.${errs.length ? `<div class="ask-warn">${errs.map(x => esc((x.title || "").slice(0, 50) + ": " + x.error)).join("<br>")}</div>` : ""}`; }
      });
    } catch (e) { prog.innerHTML = errBox(e, {retry: 'data-retry="#ev-make"'}); }
    if (ctx) await ctx.reload(); window.LibraryUI && window.LibraryUI.updateFolderHead(); if (!errs.length) evidence(el, fid); else btn.disabled = false;
  });
}

// ================================================================ Ideas Lab
let chain = Promise.resolve();
const saveNow = () => (chain = chain.then(doSave));          // saves go one after another, so each one carries the rev the last one produced
const saveBoard = debounce(saveNow, 600);
async function doSave() {
  if (!L.doc) return;
  try {
    const r = await api("/api/folder/ideas", {id: L.fid, rev: L.doc.rev, doc: {brief: L.doc.brief, events: L.doc.events, ideas: L.doc.ideas.map(x => ({id: x.id, star: x.star, status: x.status, note: x.note}))}});
    L.doc.rev = r.doc.rev; if (L.doc.events.items.length !== r.doc.events.items.length || L.doc.events.items.some((e, i) => e.id !== r.doc.events.items[i].id)) { L.doc.events = r.doc.events; renderEvents(); }
  } catch (e) { if (e.status === 409) { toast("The board changed in another window; showing the newest."); L.doc = e.body.current; renderAll(); } else notifyError(e); }
}

export async function lab(el, fid) {
  L.fid = fid; L.root = el;
  el.innerHTML = `<div class="card ev-card">Opening the lab…</div>`;
  try { const r = await api("/api/folder/ideas?id=" + encodeURIComponent(fid)); L.doc = r.doc; L.cards = r.cards; L.folder = r.folder; }
  catch (e) { el.innerHTML = `<div class="card empty"><h2>The Ideas Lab isn’t available</h2>${esc(e.message)}</div>`; return; }
  const b = L.doc.brief;
  el.innerHTML = `<div class="card lab-intro"><b>Research Ideas Lab</b> — new research ideas from the papers in <b>${esc(L.folder.name)}</b>: what their limitations and future-research sections leave open, plus what is happening in the world now. Everything an idea rests on is listed, and what is a guess is marked as a guess.</div>
    <div class="card lab-sec"><h3><span class="lab-step">1</span>Your brief</h3>
      <div class="lab-grid"><label>Focus <small>(optional)</small><input type="text" id="lb-focus" maxlength="400" placeholder="e.g. generative AI and platform governance" value="${esc(b.focus)}"></label>
        <label>Constraints <small>(methods, data you can get, time, target journals)</small><textarea id="lb-cons" rows="2" maxlength="600" placeholder="e.g. no interviews; public data only; aim for ISR / MISQ; 12 months">${esc(b.constraints)}</textarea></label>
        <label>How many ideas<select id="lb-n">${[3, 5, 8, 10].map(n => `<option${b.n === n ? " selected" : ""}>${n}</option>`).join("")}</select></label>
        <label>Ambition<select id="lb-amb">${[["safe", "Safe — feasible extensions"], ["balanced", "Balanced"], ["bold", "Bold — unconventional"]].map(([k, t]) => `<option value="${k}"${b.ambition === k ? " selected" : ""}>${t}</option>`).join("")}</select></label></div>
      <div class="pmeta" id="lb-cards"></div></div>
    <div class="card lab-sec"><h3><span class="lab-step">2</span>What is happening now <small>(optional)</small></h3><div id="lab-events"></div></div>
    <div class="card lab-sec"><h3><span class="lab-step">3</span>Generate</h3><div id="lab-gen"></div></div>
    <div id="lab-board"></div>`;
  const brief = () => { L.doc.brief = {focus: $("#lb-focus", el).value, constraints: $("#lb-cons", el).value, n: +$("#lb-n", el).value, ambition: $("#lb-amb", el).value}; saveBoard(); estimate(); };
  ["lb-focus", "lb-cons"].forEach(id => $("#" + id, el).addEventListener("input", brief)); ["lb-n", "lb-amb"].forEach(id => $("#" + id, el).addEventListener("change", brief));
  el.onclick = onClick; el.onchange = onChange; el.oninput = onInput; wireOut(el);
  renderAll(); estimate();
}
const renderAll = () => { renderCards(); renderEvents(); renderGen(); renderBoard(); };
function renderCards() {
  const c = L.cards, el = $("#lb-cards", L.root); if (!el) return;
  el.innerHTML = c.have >= 2 ? `Built from <b>${c.have}</b> paper card${c.have === 1 ? "" : "s"}${c.missing ? ` — ${c.missing} paper${c.missing === 1 ? "" : "s"} without a card ${c.missing === 1 ? "is" : "are"} left out (make them in the <button class="linkbtn" data-k="evtab">Evidence tab</button>)` : ""}.`
    : `<span class="ask-warn" style="display:inline-block">The lab needs paper cards for at least two papers; this folder has ${c.have}. <button class="linkbtn" data-k="evtab">Make them in the Evidence tab</button>.</span>`;
}
async function estimate() {
  if (!L.root || L.cards.have < 2) return;
  const my = L.estSeq = (L.estSeq || 0) + 1;
  try { const r = await api("/api/ai/ideas/estimate", {id: L.fid, brief: L.doc.brief}); if (my === L.estSeq) { L.est = r; renderGen(); renderEvents(); } } catch (e) { /* the estimate is a courtesy */ }
}

// -- events
function renderEvents() {
  const el = $("#lab-events", L.root); if (!el) return; const ev = L.doc.events, items = ev.items;
  el.innerHTML = `<div class="pmeta">The provider’s web search looks for regulation, launches, incidents and market shifts around your papers’ topics. Untick anything you don’t want; <b>the model may only cite events listed here</b>.</div>
    <div class="row" style="gap:8px;margin:8px 0;flex-wrap:wrap"><button class="btn" data-k="events"${L.busy || L.cards.have < 2 ? " disabled" : ""}>🌐 ${ev.fetched ? "Search again" : "Search current events"}${L.est && L.est.events != null ? " · " + money(L.est.events) : ""}</button>
      <button class="btn" data-k="addevent">＋ Add one of my own</button><span class="pmeta" id="ev-st"></span></div>
    ${items.map((e, i) => `<label class="lab-event"><input type="checkbox" data-ev="${i}"${e.keep !== false ? " checked" : ""}><span><b>${esc(e.id)}</b> ${e.url ? `<a href="${esc(e.url)}" target="_blank" rel="noopener">${esc(e.title)}</a>` : esc(e.title)}
        <small>${esc(e.date || "undated")}${e.user ? " · yours" : e.verified === true ? " · ✓ found in the search results" : e.verified === false ? " · ⚠ link not among the search results" : ""}</small>
        <span class="pmeta">${esc(e.summary)}${e.why ? " — <i>" + esc(e.why) + "</i>" : ""}</span></span>${e.user ? `<button class="cd-x" data-delevent="${i}" style="opacity:1" title="Remove">✕</button>` : ""}</label>`).join("") || `<div class="pmeta">${ev.fetched ? "The search found nothing usable." : "No events yet — the ideas will rest on the papers alone."}</div>`}
    ${ev.fetched ? `<div class="pmeta">Searched ${esc(ev.fetched.slice(0, 10))} · ${esc(ev.model || "")}${ev.cost ? " · " + exactCost(ev.cost) : ""}</div>` : ""}`;
}
async function findEvents() {
  if (L.busy) return; L.busy = true; renderEvents(); const st = () => $("#ev-st", L.root);
  try { await saveNow();          // what you typed so far is on the board before the server changes it
    await ndjson("/api/ai/ideas/events", {id: L.fid, focus: $("#lb-focus", L.root).value}, m => {
    if (m.type === "status" && st()) progress(st(), m); else if (m.type === "events") { const b = L.doc.brief; L.doc = m.doc; L.doc.brief = b; } else if (m.type === "error") throw new Error(m.error);
    else if (m.type === "done") { toast(m.found ? `${m.found} event${m.found === 1 ? "" : "s"} found${m.cost ? " · " + exactCost(m.cost) : ""}.` : "The search found no usable events" + (m.note ? ": " + m.note.slice(0, 120) : "") + "."); if (!m.found && st()) st().textContent = m.note || ""; } }); }
  catch (e) { notifyError(e); }
  L.busy = false; renderEvents(); renderGen(); estimate();
}
function addEvent() {
  const title = prompt("What happened? (a short title)"); if (!title || !title.trim()) return;
  const url = prompt("Link (optional):", "") || "", date = prompt("Date (optional, e.g. 2026-03):", "") || "", summary = prompt("One line on why it matters (optional):", "") || "";
  L.doc.events.items.push({id: "E?", title: title.trim(), url: url.trim(), date: date.trim(), summary: summary.trim(), user: true, keep: true}); renderEvents(); saveBoard();
}

// -- generate
function renderGen() {
  const el = $("#lab-gen", L.root); if (!el) return; const ok = L.cards.have >= 2, n = L.doc.brief.n;
  el.innerHTML = `<div class="row" style="gap:8px;flex-wrap:wrap;align-items:center"><button class="btn primary" data-k="gen"${L.busy || !ok ? " disabled" : ""}>💡 Generate ${n} ideas${L.est && L.est.generate != null ? " · " + money(L.est.generate) : ""}</button>
    <select id="lab-mode" title="What happens to ideas already on the board"><option value="add">Add to the board</option><option value="replace">Replace ideas I haven’t starred or shortlisted</option></select>
    <span class="pmeta" id="gen-st"></span></div>
    <div class="pmeta" style="margin-top:6px">Three steps: the ideas are written from your evidence, an “editor” scores them (novelty, feasibility, significance, fit), and OpenAlex is searched for the closest existing work. ${L.est ? `${L.est.papers} paper${L.est.papers === 1 ? "" : "s"} · ${L.doc.events.items.filter(e => e.keep !== false).length} event${L.doc.events.items.filter(e => e.keep !== false).length === 1 ? "" : "s"}.` : ""}</div>`;
}
async function generate() {
  if (L.busy) return; L.busy = true; const mode = $("#lab-mode", L.root).value; renderGen(); renderEvents(); const st = () => $("#gen-st", L.root);
  await saveNow();
  try { await ndjson("/api/ai/ideas/generate", {id: L.fid, brief: L.doc.brief, mode}, m => {
    if (m.type === "status" && st()) progress(st(), m); else if (m.type === "progress" && st()) progress(st(), {msg: `Writing the ideas… ${(m.chars / 1000).toFixed(1)}k characters`, step: 2, steps: 4});
    else if (m.type === "ideas") L.doc = m.doc; else if (m.type === "error") throw new Error(m.error);
    else if (m.type === "done") toast(`${m.made} ideas added${m.cost ? " · " + exactCost(m.cost) : ""}${m.cached ? " (from the saved answer)" : ""}.`); }); }
  catch (e) { notifyError(e); if ($("#gen-st", L.root)) $("#gen-st", L.root).innerHTML = errInline(e); }
  L.busy = false; renderGen(); renderEvents(); renderBoard();
}

// -- the board
const snapOf = x => ((L.doc.runs || {})[x.run] || {}).evidence || {papers: {}, limitations: {}, future: {}, events: {}};
function evChip(x, id) {
  const s = snapOf(x), p = s.papers[id], c = s.limitations[id] || s.future[id];
  const label = p ? p.cite : c ? `${id} · ${c.n} paper${c.n === 1 ? "" : "s"}` : id;
  return `<button class="ev-chip ${p ? "p" : id[0] === "L" ? "l" : "f"}" data-ev-open="${x.id}|${id}" title="${esc(p ? p.title : c ? c.label : "")}">${esc(label)}</button>`;
}
function evDetail(x, id) {
  const s = snapOf(x), p = s.papers[id], c = s.limitations[id] || s.future[id];
  if (p) return `<div class="ev-it"><b>${esc(p.cite)}</b> ${esc(p.title)} <button class="linkbtn" data-open="${esc(p.file)}" data-page="1" data-q="">Open</button></div>`;
  if (c) return `<div class="ev-it"><b>${id}</b> ${esc(c.label)}</div>` + c.items.map(i => { const q = s.papers[i.paper] || {};
    return `<div class="ev-it sub"><button class="linkbtn" data-open="${esc(q.file || "")}" data-page="${i.page || 1}" data-q="${esc(i.quote || "")}">${esc(q.cite || i.paper)}${i.page ? ", p. " + i.page : ""}</button> ${esc(i.text)}</div>`; }).join("");
  return "";
}
const meter = (label, v, tip) => `<span class="lab-sc" title="${label}: ${v} of 5 — ${tip}"><small>${label}</small><i><b style="width:${v * 20}%"></b></i><em>${v}</em></span>`;
const ITABS = [["overview", "Overview", "ask"], ["design", "Design", "matrix"], ["check", "Novelty", "evidence"], ["risks", "Risks & next", "warn"], ["outline", "Outline", "sum"]];
const outlineHtml = (x, text) => sectionsHtml(mdParts(text, {leadTitle: ""}), "outline:" + x.id);
function ideaHtml(x) {
  const c = x.critique, nv = x.novelty || {}, s = snapOf(x), tab = L.itab[x.id] || "overview";
  const evs = x.why_now.events.map(id => s.events[id] ? `<a href="${esc(s.events[id].url)}" target="_blank" rel="noopener">${esc(s.events[id].title)}</a> <small>${esc(s.events[id].date || "")}</small>` : "").filter(Boolean);
  const f = (t, html, cls = "") => `<div class="idea-f ${cls}"><div class="idea-k">${t}</div><div class="idea-v">${html}</div></div>`;
  const list = (t, xs) => xs && xs.length ? f(t, `<ul>${xs.map(i => `<li>${esc(i)}</li>`).join("")}</ul>`) : "";
  const pane = (id, body) => `<div class="idea-pane${tab === id ? " on" : ""}" data-pane="${id}">${body || `<div class="pmeta">Nothing to show.</div>`}</div>`;
  const works = nv.works && nv.works.length ? f("Closest existing work", `<small>(OpenAlex${nv.query ? ": “" + esc(nv.query) + "”" : ""})</small><ul>${nv.works.slice(0, 4).map(w => `<li><a href="${esc(w.url)}" target="_blank" rel="noopener">${esc(w.title)}</a> <small>${w.year || ""} · closeness ${w.sim}${w.in_folder ? " · in this folder" : ""}</small></li>`).join("")}</ul>`)
    : nv.checked === false ? f("Closest existing work", `<span class="pmeta">Couldn’t check OpenAlex (${esc(nv.error || "offline")}).</span>`) : "";
  return `<div class="card idea st-${x.status}" data-id="${x.id}" data-tool="ideas">
    <div class="idea-h">${c ? `<span class="lab-score" title="The editor’s average score, 1 to 5">${c.score.toFixed(1)}</span>` : `<span class="lab-score none" title="Not scored yet">–</span>`}
      <div class="idea-ht"><div class="idea-title">${esc(x.title)}</div>
        <div class="idea-badges"><span class="gr gr-${x.grounding}" title="${x.grounding === "grounded" ? "Rests on limitations or future-research suggestions in your papers" : x.grounding === "partly" ? "Rests on papers or events, but not on a stated gap" : "Not tied to anything in your papers: a guess"}">${x.grounding}</span>
          ${nv.level ? `<span class="nv nv-${nv.level.startsWith("very") ? "hi" : nv.level.startsWith("related") ? "mid" : "lo"}" title="How close existing work is, from OpenAlex">${esc(nv.level)}</span>` : ""}${x.contribution.kind ? `<span class="of-badge">${esc(x.contribution.kind)}</span>` : ""}</div></div>
      <div class="idea-acts"><button class="btn small star${x.star ? " on" : ""}" data-a="star" title="Star">${x.star ? "★" : "☆"}</button>
        <button class="btn small${x.status === "shortlist" ? " on" : ""}" data-a="shortlist" title="Keep on the shortlist">✓ Shortlist</button>
        <button class="btn small" data-a="reject" title="${x.status === "rejected" ? "Bring back" : "Hide from the board and exports"}">${x.status === "rejected" ? "↩ Restore" : "✕"}</button></div></div>
    ${c ? `<div class="idea-scores well">${meter("Novelty", c.novelty, "is the question new?")}${meter("Feasibility", c.feasibility, "can you do it with data and methods you can get?")}${meter("Significance", c.significance, "does it matter to the field?")}${meter("Fit", c.fit, "does it fit your brief and these papers?")}</div>` : ""}
    <div class="idea-tabs" role="tablist">${ITABS.map(([k, t, i]) => `<button data-itab="${k}" data-tool="${i}" class="${tab === k ? "on" : ""}" role="tab">${ico(i, "sm")}${t}${k === "outline" && x.developed ? " ✓" : ""}</button>`).join("")}</div>
    ${pane("overview", f("Research question", esc(x.research_question), "lead") + (x.why_now.text ? f("Why now", `${esc(x.why_now.text)} ${evs.length ? "<br>" + evs.join(" · ") : ""}`) : "") +
      f("The gap", `${esc(x.gap.text)}<div class="ev-chips">${x.gap.evidence.map(id => evChip(x, id)).join("")}</div><div class="ev-detail" data-detail="${x.id}"></div>`))}
    ${pane("design", f("Lens", esc(x.lens)) + f("Method", `${esc(x.method.approach)}${x.method.identification ? `<br><i>${esc(x.method.identification)}</i>` : ""}`) + f("Data", esc(x.data)) + f("Contribution", `<span class="of-badge">${esc(x.contribution.kind)}</span> ${esc(x.contribution.text)}`) +
      (x.journals.length ? f("Fitting journals", x.journals.map(esc).join(", ")) : ""))}
    ${pane("check", (c ? f("Editor’s view", esc(c.verdict)) : "") + works)}
    ${pane("risks", (c ? f("Main risk", esc(c.main_risk), "warn") : "") + list("Risks", x.risks) + list("First steps", x.first_steps) + list("Not grounded in your papers (a guess)", x.speculative))}
    ${pane("outline", `<div class="idea-dev${x.developed ? "" : " empty"}" id="dev-${x.id}">${x.developed ? outlineHtml(x, x.developed.text) + `<div class="pmeta">${esc(x.developed.model || "")}${x.developed.cost ? " · " + exactCost(x.developed.cost) : ""} <button class="linkbtn" data-a="copydev">Copy outline</button></div>` : `<span class="pmeta">Not developed yet: press “Develop this idea” below to get a working title, abstract, research questions, design and a first plan.</span>`}</div>`)}
    <textarea class="idea-note" data-note="${x.id}" rows="1" placeholder="My note…">${esc(x.note || "")}</textarea>
    <div class="idea-foot"><button class="btn small" data-a="develop">${x.developed ? "Show the outline" : "Develop this idea"}</button><button class="btn small" data-a="copy">Copy</button><button class="btn small danger" data-a="delete">Delete</button><span class="pmeta" style="margin-left:auto">${esc(x.created ? x.created.slice(0, 10) : "")}</span></div></div>`;
}
const redrawIdea = id => { const el = $(`.idea[data-id="${id}"]`, L.root), x = L.doc.ideas.find(i => i.id === id); if (!el || !x) return; el.outerHTML = ideaHtml(x); const t = $(`.idea[data-id="${id}"] .idea-note`, L.root); if (t) { t.style.height = "auto"; t.style.height = Math.max(30, t.scrollHeight) + "px"; } };
function renderBoard() {
  const el = $("#lab-board", L.root); if (!el) return; let xs = L.doc.ideas.slice();
  const rej = xs.filter(x => x.status === "rejected").length;
  xs = xs.filter(x => L.showRejected ? true : x.status !== "rejected");
  if (L.filter === "star") xs = xs.filter(x => x.star); else if (L.filter === "shortlist") xs = xs.filter(x => x.status === "shortlist");
  xs.sort(L.sort === "new" ? (a, b) => (b.created || "").localeCompare(a.created || "") : (a, b) => (b.status === "shortlist") - (a.status === "shortlist") || (b.star - a.star) || ((b.critique || {}).score || 0) - ((a.critique || {}).score || 0));
  el.innerHTML = `<div class="card lab-sec"><div class="row" style="justify-content:space-between;flex-wrap:wrap;gap:8px"><h3 style="margin:0"><span class="lab-step">4</span>The board <small>(${L.doc.ideas.length} idea${L.doc.ideas.length === 1 ? "" : "s"})</small></h3>
      <div class="row" style="gap:6px;flex-wrap:wrap"><select id="bd-filter"><option value="all">All</option><option value="star"${L.filter === "star" ? " selected" : ""}>★ Starred</option><option value="shortlist"${L.filter === "shortlist" ? " selected" : ""}>✓ Shortlist</option></select>
        <select id="bd-sort"><option value="score">Best first</option><option value="new"${L.sort === "new" ? " selected" : ""}>Newest first</option></select>
        ${rej ? `<label class="chk" style="margin:0"><input type="checkbox" id="bd-rej"${L.showRejected ? " checked" : ""}> show ${rej} hidden</label>` : ""}
        <span class="menuwrap"><button class="btn" data-k="exp">Export ▾</button><div class="menu card hide" id="bd-expmenu"><button data-x="md">Markdown (.md)</button><button data-x="docx">Word (.docx)</button><button data-x="save-md">Save .md to my export folder</button><button data-x="save-docx">Save .docx to my export folder</button></div></span></div></div></div>
    ${xs.length ? `<div class="lab-legend">The number is the editor’s average of four scores (1–5): <b>novelty</b>, <b>feasibility</b>, <b>significance</b>, <b>fit</b>. Evidence chips: <span class="ev-chip p">paper</span> <span class="ev-chip l">limitation (n papers share it)</span> <span class="ev-chip f">future research</span>. <span class="gr gr-grounded">grounded</span> rests on a stated gap · <span class="gr gr-partly">partly</span> · <span class="gr gr-speculative">speculative</span> a guess.</div>` : ""}
    ${xs.map(ideaHtml).join("") || `<div class="card empty"><h2>${L.doc.ideas.length ? "Nothing matches the filter" : "No ideas yet"}</h2>${L.doc.ideas.length ? "" : "Write a brief, then press Generate."}</div>`}`;
  $$(".idea-note", el).forEach(t => { t.style.height = "auto"; t.style.height = Math.max(30, t.scrollHeight) + "px"; });
}

// -- events on the whole lab
function onInput(e) {
  const n = e.target.closest("[data-note]"); if (!n) return;
  const x = L.doc.ideas.find(i => i.id === n.dataset.note); if (x) { x.note = n.value; saveBoard(); n.style.height = "auto"; n.style.height = Math.max(30, n.scrollHeight) + "px"; }
}
function onChange(e) {
  const ev = e.target.closest("[data-ev]");
  if (ev && ev.type === "checkbox") { L.doc.events.items[+ev.dataset.ev].keep = ev.checked; saveBoard(); estimate(); return; }
  if (e.target.id === "bd-filter") { L.filter = e.target.value; renderBoard(); } else if (e.target.id === "bd-sort") { L.sort = e.target.value; renderBoard(); }
  else if (e.target.id === "bd-rej") { L.showRejected = e.target.checked; renderBoard(); }
}
async function onClick(e) {
  const t = e.target, k = t.closest("[data-k]")?.dataset.k;
  if (k === "events") return findEvents();
  if (k === "addevent") return addEvent();
  if (k === "gen") return generate();
  if (k === "evtab") return window.LibraryUI.folderTab("evidence");
  if (k === "exp") { e.stopPropagation(); $("#bd-expmenu", L.root).classList.toggle("hide"); return; }
  const x = t.closest("[data-x]")?.dataset.x;
  if (x) {
    $("#bd-expmenu", L.root).classList.add("hide");
    if (x === "md" || x === "docx") return dl(`/api/export/ideas?folder=${encodeURIComponent(L.fid)}&fmt=${x}`);
    try { const r = await api("/api/export/save", {what: "ideas", folder: L.fid, fmt: x.slice(5)}); toast("Saved: " + r.path); } catch (er) { notifyError(er); } return;
  }
  const de = t.closest("[data-delevent]"); if (de) { L.doc.events.items.splice(+de.dataset.delevent, 1); renderEvents(); saveBoard(); return; }
  const o = t.closest("[data-open]"); if (o) return openAt(o.dataset.open, +o.dataset.page, o.dataset.q);
  const eo = t.closest("[data-ev-open]");
  if (eo) {
    const [iid, eid] = eo.dataset.evOpen.split("|"), box = $(`[data-detail="${iid}"]`, L.root), x2 = L.doc.ideas.find(i => i.id === iid);
    const on = eo.classList.toggle("on"); $$(`[data-ev-open^="${iid}|"]`, L.root).forEach(b => { if (b !== eo) b.classList.remove("on"); });
    box.innerHTML = on ? evDetail(x2, eid) : ""; return;
  }
  const it = t.closest("[data-itab]"); if (it) { const id = it.closest(".idea").dataset.id; L.itab[id] = it.dataset.itab; return redrawIdea(id); }
  const a = t.closest("[data-a]")?.dataset.a, card = t.closest(".idea"); if (!a || !card) return;
  const idea = L.doc.ideas.find(i => i.id === card.dataset.id); if (!idea) return;
  if (a === "star") { idea.star = !idea.star; saveBoard(); renderBoard(); }
  else if (a === "shortlist") { idea.status = idea.status === "shortlist" ? "new" : "shortlist"; saveBoard(); renderBoard(); }
  else if (a === "reject") { idea.status = idea.status === "rejected" ? "new" : "rejected"; saveBoard(); renderBoard(); }
  else if (a === "delete") { if (confirm("Delete this idea from the board?")) { L.doc.ideas = L.doc.ideas.filter(i => i !== idea); saveBoard(); renderBoard(); } }
  else if (a === "copy") navigator.clipboard.writeText(`${idea.title}\n\nResearch question: ${idea.research_question}\nGap: ${idea.gap.text}\nMethod: ${idea.method.approach}; ${idea.method.identification}\nData: ${idea.data}\nContribution (${idea.contribution.kind}): ${idea.contribution.text}`).then(() => toast("Copied."));
  else if (a === "copydev") navigator.clipboard.writeText(idea.developed.text).then(() => toast("Outline copied."));
  else if (a === "develop") develop(idea, card);
}
async function develop(idea, card) {
  if (idea.developed) { L.itab[idea.id] = "outline"; return redrawIdea(idea.id); }
  if (L.busy) return; L.busy = true; L.itab[idea.id] = "outline"; redrawIdea(idea.id);
  const box = $(`#dev-${idea.id}`, L.root); box.classList.remove("empty"); box.innerHTML = `<span class="ask-status">Developing the idea…</span>`; let text = "", cut = false;
  try { await ndjson("/api/ai/ideas/develop", {id: L.fid, idea: idea.id}, m => {
    if (m.type === "text") { text += m.delta; box.innerHTML = outlineHtml(idea, text); } else if (m.type === "error") throw new Error(m.error);
    else if (m.type === "done") { L.doc.rev = m.rev || L.doc.rev; if (m.cut_off) cut = true; else idea.developed = {text, model: m.model, cost: m.cost}; } });
    if (cut) box.insertAdjacentHTML("beforeend", callout("warn", `This outline was cut off at the model’s length limit; the end is missing, so it was not kept with the idea. <button class="rd-btn" data-a="develop">Develop it again</button>`)); }
  catch (e) { box.innerHTML = errBox(e, {retry: 'data-a="develop"'}); }
  L.busy = false; if (idea.developed) redrawIdea(idea.id);
}

window.IdeasUI = {init(c) { ctx = c; }, evidence, lab};
