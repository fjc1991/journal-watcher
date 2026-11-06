// Folder tab "Synthesis": what the folder's papers say together, from their cards. One model call reads every card's notes and returns an overview (the digest), the themes the papers
// speak to with each paper's position (the map: themes down, papers across, a cell per paper that has something to say) and what is still open (gaps). Every cell and gap points at
// the notes it rests on (click: the paper opens at that page); the server drops anything that points at a note it was not given, and marks a gap with no note behind it as "not grounded".
// Saved as Papers/_folders/<id>.synthesis.json. Server: GET /api/folder/synthesis, POST /api/ai/synthesis[/estimate] (stream: status | progress | synthesis | done | error).
import {$, esc, ico, api, toast, notifyError, errInline, debounce, progress} from "./state.js";
import {frame, callout, wire as wireOut} from "./output.js";

let ctx = null;
const Y = {fid: null, el: null, data: null, busy: false, sel: new Set(), focus: ""};
const money = c => c == null ? "" : c < 0.01 ? "under 1¢" : "about $" + (c < 1 ? c.toFixed(2) : c.toFixed(1));
const CONS = {agree: "They agree", mixed: "Mixed", contested: "Contested", single: "One paper only"};
const STANCE = {supports: "supports", qualifies: "qualifies", opposes: "opposes", neutral: "neutral"};

export async function mount(el, fid) {
  Y.fid = fid; Y.el = el; Y.busy = false; el.innerHTML = `<div class="card ev-card">Looking at the folder…</div>`;
  try { Y.data = await api("/api/folder/synthesis?id=" + encodeURIComponent(fid)); }
  catch (e) { el.innerHTML = `<div class="card empty"><h2>The synthesis isn’t available</h2>${esc(e.message)}</div>`; return; }
  const st = (Y.data.doc && Y.data.doc.settings) || {}, have = Y.data.papers.filter(p => p.card).map(p => p.file);
  Y.focus = st.focus || ""; Y.sel = new Set(st.files && st.files.length ? st.files.filter(f => have.includes(f)) : have);
  el.addEventListener("click", onClick); el.addEventListener("change", onChange); el.addEventListener("input", e => { if (e.target.id === "sy-focus") Y.focus = e.target.value; }); wireOut(el);
  draw();
}

// ---------------------------------------------------------------- the page
function draw() {
  const d = Y.data, doc = d.doc, withCard = d.papers.filter(p => p.card);
  Y.el.innerHTML = `<div class="card sy-setup" data-tool="synthesis"><div class="dr-h"><span class="of-ico">${ico("synthesis")}</span><div><div class="eyebrow t">Synthesis</div><div class="of-title">${doc ? "Make it again, or change what it uses" : "What do these papers say together, and what is still open?"}</div></div></div>
    <label class="cl-lab">What should it be about? <small>(optional: a question or a topic to organise the themes around)</small><input type="text" id="sy-focus" maxlength="600" placeholder="e.g. how disclosure rules change trust and entry" value="${esc(Y.focus)}"></label>
    <div class="dr-papers"><div class="eyebrow">Papers to use <small>${Y.sel.size} of ${withCard.length} with a card</small> <button class="linkbtn" data-sy="all">all</button> · <button class="linkbtn" data-sy="none">none</button></div>
      ${d.papers.map(p => `<label class="dr-p${p.card ? "" : " off"}"><input type="checkbox" data-pf="${esc(p.file)}"${Y.sel.has(p.file) ? " checked" : ""}${p.card ? "" : " disabled"}> <b>${esc(p.cite)}</b> ${esc((p.title || "").slice(0, 90))}${p.card ? "" : ` <span class="of-badge inferred">no card</span>`}</label>`).join("") || `<div class="pmeta">This folder has no papers yet.</div>`}
      ${withCard.length < d.papers.length ? `<div class="pmeta">Papers without a card can’t be used: <button class="linkbtn" data-sy="evtab">make the cards in the Evidence tab</button>.</div>` : ""}</div>
    <div class="row dr-go"><button class="btn primary" data-sy="go"${Y.busy || Y.sel.size < 2 ? " disabled" : ""}>${ico("synthesis", "sm")} ${doc ? "Make it again" : "Make the synthesis"}<span id="sy-est"></span></button><span class="pmeta" id="sy-st">${Y.sel.size < 2 ? "Choose at least two papers that have a card." : ""}</span></div>
    <div class="pmeta">The model sees only your cards (not the PDFs). Every theme position and every gap has to point at the notes it rests on; what points at nothing is dropped or marked “not grounded”.</div></div>${doc ? out(doc) : ""}`;
  estimate();
}
const settings = () => ({files: [...Y.sel], focus: ($("#sy-focus", Y.el) || {}).value ?? Y.focus});
let seq = 0;
const estimate = debounce(async () => {
  const my = ++seq, e = $("#sy-est", Y.el); if (!e || Y.sel.size < 2) { if (e) e.textContent = ""; return; }
  try { const r = await api("/api/ai/synthesis/estimate", {id: Y.fid, settings: settings()}); if (my === seq && $("#sy-est", Y.el)) $("#sy-est", Y.el).textContent = r.cost != null ? " · " + money(r.cost) : ""; } catch (er) { /* a courtesy */ }
}, 350);

const noteTip = (doc, id) => { const n = doc.notes[id]; return n ? `${n.kind}: ${n.text}${n.page ? " (p. " + n.page + ")" : ""}` : id; };
const chip = (doc, id) => { const p = doc.papers[id.split("-")[0]], n = doc.notes[id]; return `<button class="dr-c" data-cite="${esc(id)}" title="${esc(noteTip(doc, id))}">${esc(p ? p.short.replace(/\s*\((\d{4})\)/, ", $1") : id)}${n && n.page ? ", p. " + n.page : ""}</button>`; };
const chips = (doc, ids) => (ids || []).map(i => chip(doc, i)).join(" ");

function out(doc) {
  const P = doc.papers, cols = Object.keys(P).filter(pid => doc.themes.some(t => t.positions.some(p => p.paper === pid)));
  const overview = `<p class="sy-ov">${esc(doc.overview)}</p>${doc.overview_ids.length ? `<div class="pmeta sy-based">Based on ${chips(doc, doc.overview_ids.slice(0, 8))}</div>` : ""}`;
  const map = doc.themes.length ? `<div class="sy-wrap"><table class="sy-map"><thead><tr><th class="sy-corner">Theme</th>${cols.map(pid => `<th><button class="linkbtn" data-open="${esc(P[pid].file)}" title="${esc(P[pid].title)}">${esc(P[pid].short)}</button></th>`).join("")}</tr></thead><tbody>
    ${doc.themes.map(t => `<tr><th class="sy-t"><b>${esc(t.name)}</b> <span class="sy-c ${t.consensus}">${esc(CONS[t.consensus])}</span><div class="sy-sum">${esc(t.summary)}</div>${t.tension ? `<div class="sy-tn">${ico("warn", "sm")} ${esc(t.tension)}</div>` : ""}</th>
      ${cols.map(pid => { const p = t.positions.find(x => x.paper === pid); return p ? `<td><button class="sy-cell st-${p.stance}" data-cite="${esc(p.ids[0])}" title="${esc(p.claim + "\n" + p.ids.map(i => noteTip(doc, i)).join("\n"))}"><span class="sy-st">${esc(STANCE[p.stance])}</span>${esc(p.claim)}</button></td>` : `<td class="sy-no">·</td>`; }).join("")}</tr>`).join("")}</tbody></table></div>
    <div class="pmeta sy-key"><span class="sy-st-k st-supports">supports</span> <span class="sy-st-k st-qualifies">qualifies</span> <span class="sy-st-k st-opposes">opposes</span> <span class="sy-st-k st-neutral">neutral</span> · a dot means the paper is silent on that theme · click a cell to open the paper at the note it rests on</div>` : callout("info", "No theme was found that the notes back up.");
  const gaps = doc.gaps.length ? doc.gaps.map(g => `<div class="sy-gap${g.grounded ? "" : " ung"}"><div class="sy-gh"><span class="of-badge">${esc(g.kind)}</span>${g.grounded ? "" : `<span class="of-badge inferred">not grounded in your papers</span>`}</div><div class="sy-gt">${esc(g.gap)}</div>
      ${g.why ? `<div class="sy-gw"><b>Why it matters</b> ${esc(g.why)}</div>` : ""}${g.question ? `<div class="sy-gq"><b>A question it suggests</b> ${esc(g.question)} <button class="linkbtn" data-copy="${esc(g.question)}">copy</button></div>` : ""}${g.ids.length ? `<div class="pmeta sy-based">Based on ${chips(doc, g.ids)}</div>` : ""}</div>`).join("") : callout("info", "No gap was found that the notes back up.");
  const miss = (doc.missing || []).length;
  const check = (doc.dropped ? callout("warn", `The model pointed at ${doc.dropped} note${doc.dropped === 1 ? "" : "s"} it had not been given; ${doc.dropped === 1 ? "it was" : "they were"} ignored. Positions without a note behind them were removed.`) : callout("ok", "Every position and every grounded gap points at a real note.")) +
    (miss ? `<div class="dr-ck">${ico("info", "sm")} Left out because they have no card: ${doc.missing.map(u => esc((u.title || "").slice(0, 60))).join("; ")}</div>` : "");
  const acts = `<button class="rd-btn" data-sy="listen" title="A two-minute spoken briefing of this folder, read aloud by your browser">🎧 Listen</button><button class="rd-btn" data-sy="copy" title="Copy the synthesis as text">${ico("copy", "sm")} Copy</button><button class="rd-btn" data-sy="docx" title="Download as Word">Word</button><button class="rd-btn" data-sy="md" title="Download as Markdown">.md</button>
    <button class="rd-btn" data-sy="save" title="Save a Word file into your export folder (Settings → Reader)">Save to export folder</button><button class="rd-btn" data-sy="redo" title="Make it again, ignoring the saved answer">${ico("redo", "sm")} Redo</button>`;
  const agree = doc.themes.filter(t => t.consensus === "agree").length, split = doc.themes.filter(t => t.consensus === "mixed" || t.consensus === "contested").length;
  return `<div id="sy-out">${frame({tool: "synthesis", kind: "Synthesis", icon: "synthesis", key: "synthesis:" + Y.fid, title: `${Object.keys(P).length} papers · ${doc.themes.length} theme${doc.themes.length === 1 ? "" : "s"} · ${doc.gaps.length} gap${doc.gaps.length === 1 ? "" : "s"}`,
    sub: doc.themes.length ? `${agree} where the papers agree, ${split} where they differ` : "", actions: acts, meta: [{t: doc.model}, doc.cost != null ? {t: money(doc.cost)} : null, {t: "made " + (doc.generated || "").slice(0, 10)}],
    sections: [{id: "ov", title: "Overview", icon: "sparkle", tone: "lead", body: overview}, {id: "map", title: "Map of the papers", icon: "synthesis", count: doc.themes.length, body: map}, {id: "gaps", title: "What is still open", icon: "warn", count: doc.gaps.length, body: gaps},
      {id: "check", title: "Check", icon: "check", collapsed: !doc.dropped, count: doc.dropped ? "!" : "✓", tone: doc.dropped ? "warn" : "", body: check}]})}</div>`;
}

// ---------------------------------------------------------------- actions
function onChange(e) { const t = e.target; if (t.matches("[data-pf]")) { t.checked ? Y.sel.add(t.dataset.pf) : Y.sel.delete(t.dataset.pf); Y.focus = ($("#sy-focus", Y.el) || {}).value ?? Y.focus; draw(); } }
async function onClick(e) {
  const t = e.target, a = t.closest("[data-sy]")?.dataset.sy, c = t.closest("[data-cite]"), o = t.closest("[data-open]"), cp = t.closest("[data-copy]");
  if (cp) { try { await navigator.clipboard.writeText(cp.dataset.copy); toast("Copied."); } catch (er) { notifyError(er); } return; }
  if (c) return openNote(c.dataset.cite);
  if (o) { const r = ctx.getLib().find(x => x.file === o.dataset.open); if (r) ctx.readPaper(r, {tab: "card"}); return; }
  if (!a) return;
  if (a === "all") { Y.data.papers.filter(p => p.card).forEach(p => Y.sel.add(p.file)); Y.focus = ($("#sy-focus", Y.el) || {}).value ?? Y.focus; return draw(); }
  if (a === "none") { Y.sel.clear(); Y.focus = ($("#sy-focus", Y.el) || {}).value ?? Y.focus; return draw(); }
  if (a === "evtab") return window.LibraryUI.folderTab("evidence");
  if (a === "listen") return void (window.Listen && window.Listen.open({title: "Briefing: " + (window.LibraryUI.folderName ? window.LibraryUI.folderName(Y.fid) : "this folder"), body: {folder: Y.fid}}));
  if (a === "go") return run(!!Y.data.doc);
  if (a === "redo") return run(true);
  if (a === "copy") { try { const r = await fetch(`/api/export/synthesis?folder=${encodeURIComponent(Y.fid)}&fmt=md`); await navigator.clipboard.writeText(await r.text()); toast("Copied."); } catch (er) { notifyError(er); } return; }
  if (a === "docx" || a === "md") { const l = document.createElement("a"); l.href = `/api/export/synthesis?folder=${encodeURIComponent(Y.fid)}&fmt=${a}`; l.download = ""; document.body.append(l); l.click(); l.remove(); return; }
  if (a === "save") { try { const r = await api("/api/export/save", {what: "synthesis", folder: Y.fid, fmt: "docx"}); toast("Saved: " + r.path); } catch (er) { notifyError(er); } }
}
function openNote(id) {
  const doc = Y.data.doc, p = doc.papers[id.split("-")[0]], n = doc.notes[id], row = p && ctx.getLib().find(r => r.file === p.file);
  if (!row) return toast("That paper is no longer in your library.");
  ctx.readPaper(row, n && n.page ? {goto: {page: n.page, quote: n.quote || ""}} : {tab: "card"});
}
async function run(again) {
  if (Y.busy) return; const s = settings(); if (s.files.length < 2) return toast("Choose at least two papers that have a card.");
  Y.focus = s.focus; Y.busy = true; const btn = $('[data-sy="go"]', Y.el); if (btn) btn.disabled = true; const st = () => $("#sy-st", Y.el);
  try {
    const r = await fetch("/api/ai/synthesis", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({id: Y.fid, settings: s, fresh: again})});
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
    const rd = r.body.getReader(), dec = new TextDecoder(); let buf = "", done = null;
    for (;;) {
      const {value, done: fin} = await rd.read(); if (fin) break; buf += dec.decode(value, {stream: true}); let i;
      while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1); if (!line) continue; const m = JSON.parse(line);
        if (m.type === "status" && st()) progress(st(), m); else if (m.type === "synthesis") Y.data.doc = m.doc; else if (m.type === "done") done = m; else if (m.type === "error") throw new Error(m.error);
      }
    }
    Y.busy = false; draw(); if (done) toast("Synthesis made" + (done.cost != null ? " · " + money(done.cost) : "") + ".");
    $("#sy-out", Y.el)?.scrollIntoView({behavior: "smooth", block: "start"});
  } catch (e) { Y.busy = false; draw(); if (st()) st().innerHTML = errInline(e); if (!/Cancelled/.test(e.message)) notifyError(e); }
}

window.SynthUI = {init(c) { ctx = c; }, mount};
