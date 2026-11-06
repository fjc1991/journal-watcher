// Folder tab "Claim check": paste something you wrote (or take your related-work draft); every sentence that makes a claim is checked against passages of your own papers.
// You choose which sentences to check (sentences about your own study are not ticked), and whether to look in this folder or the whole library. Each claim comes back as
// supported / partly / contradicted / not found in your papers, with the passages it rests on (click one: the paper opens at that page); when a claim names a paper
// ("Singhal et al., 2026") it says whether that paper's own passages really say it. The model may only point at passages it was given; anything else is dropped on the server.
// Saved as Papers/_folders/<id>.claims.json. Server: GET /api/folder/claims, POST /api/ai/claims[/estimate] (stream: status | progress | claims | done | error), POST /api/folder/claims (tick off).
import {$, $$, esc, ico, api, toast, notifyError, errBox, errInline, debounce, progress} from "./state.js";
import {frame, callout, wire as wireOut} from "./output.js";

let ctx = null;
const C = {fid: null, el: null, data: null, text: "", scope: "folder", split: [], sel: new Set(), est: null, busy: false};
const money = c => c == null ? "" : c < 0.01 ? "under 1¢" : "about $" + (c < 1 ? c.toFixed(2) : c.toFixed(1));
const VERDICT = {supported: ["Supported", "check"], partly: ["Partly supported", "info"], contradicted: ["Contradicted", "warn"], not_found: ["Not found in your papers", "search"], unchecked: ["Not checked", "info"]};

export async function mount(el, fid) {
  C.fid = fid; C.el = el; el.innerHTML = `<div class="card ev-card">Looking at the folder…</div>`;
  try { C.data = await api("/api/folder/claims?id=" + encodeURIComponent(fid)); }
  catch (e) { el.innerHTML = `<div class="card empty"><h2>The claim check isn’t available</h2>${esc(e.message)}</div>`; return; }
  const doc = C.data.doc;
  C.text = doc ? doc.text || "" : ""; C.scope = doc && doc.scope === "library" ? "library" : "folder"; C.split = []; C.sel = new Set(); C.est = null; C.busy = false;
  el.innerHTML = shell(); wire();
  drawOut(); drawRev(); recostRv();
  if (C.text) await split(doc ? doc.claims.map(c => c.text) : null);
  else drawList();
}

const shell = () => `<div class="card cl-setup" data-tool="claims"><div class="dr-h"><span class="of-ico">${ico("claims")}</span><div><div class="eyebrow t">Claim check</div><div class="of-title">Check what you wrote against your own papers</div></div></div>
  <label class="cl-lab">Your text <small>(a paragraph or several; headings and questions are skipped)</small><textarea id="cl-text" rows="7" maxlength="60000" placeholder="e.g. Platform rules shape how participants behave (Costa, 2026). Disclosure of AI use reduces backers’ funding by about half (Singhal et al., 2026).">${esc(C.text)}</textarea></label>
  <div class="row cl-bar"><label class="cl-scope">Look in <select id="cl-scope"><option value="folder"${C.scope === "folder" ? " selected" : ""}>this folder (${C.data.papers} paper${C.data.papers === 1 ? "" : "s"})</option><option value="library"${C.scope === "library" ? " selected" : ""}>my whole library (${C.data.library} papers)</option></select></label>
    ${C.data.draft ? `<button class="btn small" data-cl="draft" title="Put the text of your related-work draft here">${ico("draft", "sm")} Use my related-work draft</button>` : ""}<button class="btn small" data-cl="clear">Clear</button></div>
  <div id="cl-list"></div>
  <div class="row cl-rv"><button class="btn" data-cl="review" id="cl-rvb" title="A tough but fair review of the whole text, with your folder's papers in mind">${ico("warn", "sm")} Ask Reviewer 2<span id="cl-rvest"></span></button><span class="pmeta" id="cl-rvst">Reads the whole text, not only the sentences ticked, and says what a sceptical journal reviewer would say.</span></div></div><div id="cl-out"></div><div id="cl-rev"></div>`;

function wire() {
  const el = C.el;
  el.addEventListener("click", onClick); el.addEventListener("change", onChange); el.addEventListener("input", onInput); wireOut(el);
}

// ---------------------------------------------------------------- the sentences
let seq = 0;
async function split(keep) {
  const my = ++seq; if (!C.text.trim()) { C.split = []; C.sel = new Set(); C.est = null; return drawList(); }
  try {
    const r = await api("/api/ai/claims/estimate", {id: C.fid, scope: C.scope, text: C.text, texts: keep || undefined});
    if (my !== seq) return;
    C.split = r.claims; C.est = r; C.sel = new Set(keep ? r.claims.filter(c => keep.includes(c.text)).map(c => c.n) : r.claims.filter(c => !c.own).map(c => c.n));
  } catch (e) { if (my === seq) { C.split = []; $("#cl-list", C.el).innerHTML = errBox(e); } return; }
  drawList();
}
const splitSoon = debounce(() => split(null), 500);
const recost = debounce(async () => {
  const my = ++seq; try { const r = await api("/api/ai/claims/estimate", {id: C.fid, scope: C.scope, text: C.text, texts: C.split.filter(c => C.sel.has(c.n)).map(c => c.text)}); if (my === seq) { C.est = r; drawGo(); } } catch (e) { /* a courtesy */ }
}, 300);

function drawList() {
  const box = $("#cl-list", C.el); if (!box) return;
  if (!C.split.length) { box.innerHTML = C.text.trim() ? `<div class="pmeta">No sentence of seven words or more was found.</div>` : `<div class="pmeta">Paste your text, or use your related-work draft. Nothing is sent to a model until you press the button below.</div>`; return; }
  box.innerHTML = `<div class="eyebrow">Sentences to check <small>${C.sel.size} of ${C.split.length} ticked</small> <button class="linkbtn" data-cl="all">all</button> · <button class="linkbtn" data-cl="none">none</button></div>
    <div class="cl-sents">${C.split.map(c => `<label class="cl-s${C.sel.has(c.n) ? "" : " off"}"><input type="checkbox" data-cs="${c.n}"${C.sel.has(c.n) ? " checked" : ""}> <span>${esc(c.text)}${c.own ? ` <span class="of-badge">about your study</span>` : ""}${(c.cited || []).length ? ` <span class="of-badge cite">names ${esc(c.cited.join("; "))}</span>` : ""}</span></label>`).join("")}</div>
    <div class="row cl-go"><button class="btn primary" data-cl="go" id="cl-gob"></button><span class="pmeta" id="cl-st"></span></div>
    <div class="pmeta">The model sees only the passages found for each claim, never the whole papers, and must point at the passages it relied on; a verdict with no passage behind it is shown as “not found”.</div>`;
  drawGo();
}
function drawGo() {
  const b = $("#cl-gob", C.el); if (!b) return; const n = C.sel.size, e = C.est || {};
  b.disabled = C.busy || !n;
  b.innerHTML = `${ico("claims", "sm")} ${C.data.doc ? "Check again" : "Check"} ${n} claim${n === 1 ? "" : "s"}${e.cost ? " · " + money(e.cost) : ""}`;
  const st = $("#cl-st", C.el); if (st && !C.busy) st.textContent = !n ? "Tick at least one sentence." : e.unindexed ? `${e.unindexed} paper${e.unindexed === 1 ? " is" : "s are"} read for the first time (about ${Math.max(1, Math.round(e.seconds / 5) * 5)} s, free with the local model).` : "";
}

// ---------------------------------------------------------------- the result
function evHtml(e) {
  const st = {supports: "supports", contradicts: "contradicts", context: "related"}[e.stance] || e.stance;
  return `<button class="cl-e ${e.stance}" data-open="${esc(e.file)}" data-page="${e.page}" data-quote="${esc(e.quote || "")}" title="Open the paper at page ${e.page}"><span class="cl-es">${esc(st)}</span> <b>${esc(e.cite)}, p. ${e.page}</b> <span class="cl-eq">“${esc((e.text || e.quote || "").replace(/\s+/g, " ").slice(0, 230))}${(e.text || "").length > 230 ? "…" : ""}”</span></button>`;
}
function rowHtml(c) {
  const [lab, ic] = VERDICT[c.verdict] || VERDICT.unchecked, main = c.evidence.filter(e => e.stance !== "context"), rel = c.evidence.filter(e => e.stance === "context");
  const mism = c.cited && c.cited_ok === false ? callout("warn", `You name ${esc((c.cited_names || ["a paper"]).join("; "))}, but ${c.cited.length > 1 ? "their" : "its"} own passages do not clearly say this${c.verdict === "not_found" ? "" : "; the support comes from other papers"}. Check the citation.`) : "";
  const cand = c.verdict === "not_found" && c.candidates && c.candidates.length ? `<details class="cl-cand"><summary>The closest passages that were looked at</summary>${c.candidates.map(x => `<button class="cl-e context" data-open="${esc(x.file)}" data-page="${x.page}" data-quote="${esc(x.quote || "")}"><b>${esc(x.cite)}, p. ${x.page}</b> <span class="cl-eq">“${esc((x.quote || "").slice(0, 160))}”</span></button>`).join("")}</details>` : "";
  return `<div class="cl-row v-${c.verdict}${c.handled ? " done" : ""}" data-n="${c.n}"><div class="cl-top"><span class="cl-v ${c.verdict}">${ico(ic, "sm")} ${esc(lab)}</span><span class="cl-n">#${c.n}</span><label class="chk cl-hd" title="Tick when you have dealt with it"><input type="checkbox" data-hd="${c.n}"${c.handled ? " checked" : ""}> dealt with</label></div>
    <div class="cl-text">${esc(c.text)}</div>${c.reason ? `<div class="cl-why">${esc(c.reason)}</div>` : ""}${c.note ? `<div class="pmeta">${esc(c.note)}</div>` : ""}${mism}
    ${main.length ? `<div class="cl-ev">${main.map(evHtml).join("")}</div>` : ""}${rel.length ? `<details class="cl-cand"><summary>${rel.length} related passage${rel.length === 1 ? "" : "s"} that do not settle it</summary>${rel.map(evHtml).join("")}</details>` : ""}${cand}</div>`;
}
function drawOut() {
  const box = $("#cl-out", C.el), doc = C.data && C.data.doc; if (!box) return;
  if (!doc) { box.innerHTML = ""; return; }
  const by = v => doc.claims.filter(c => c.verdict === v), bad = doc.claims.filter(c => c.verdict === "contradicted" || c.verdict === "partly" || c.verdict === "unchecked" || (c.cited && c.cited_ok === false && c.verdict !== "not_found"));
  const rest = c => !bad.includes(c);
  const sup = by("supported").filter(rest), nf = by("not_found"), n = doc.claims.length;
  const sec = (id, title, icon, list, tone = "", collapsed = false) => list.length ? {id, title, icon, count: list.length, tone, collapsed, body: list.map(rowHtml).join("")} : null;
  const acts = `<button class="rd-btn" data-cl="copy" title="Copy the result as text">${ico("copy", "sm")} Copy</button><button class="rd-btn" data-cl="docx" title="Download as Word">Word</button><button class="rd-btn" data-cl="md" title="Download as Markdown">.md</button>
    <button class="rd-btn" data-cl="save" title="Save a Word file into your export folder (Settings → Reader)">Save to export folder</button><button class="rd-btn" data-cl="redo" title="Check again, ignoring the saved answer">${ico("redo", "sm")} Redo</button>`;
  const tally = `<span class="cl-t supported">${by("supported").length} supported</span><span class="cl-t partly">${by("partly").length} partly</span><span class="cl-t contradicted">${by("contradicted").length} contradicted</span><span class="cl-t not_found">${nf.length} not found</span>`;
  const notices = (doc.dropped ? callout("warn", `The model pointed at ${doc.dropped} passage${doc.dropped === 1 ? "" : "s"} it had not been given; ${doc.dropped === 1 ? "it was" : "they were"} ignored.`) : "") +
    (bad.length ? "" : callout("ok", "Nothing here contradicts your papers, and every paper you name says what you attribute to it."));
  box.innerHTML = frame({tool: "claims", kind: "Claim check", icon: "claims", key: "claims:" + C.fid, title: `${n} claim${n === 1 ? "" : "s"} checked against ${doc.papers} paper${doc.papers === 1 ? "" : "s"}`, sub: tally,
    actions: acts, meta: [{t: doc.model}, doc.cost != null ? {t: money(doc.cost)} : null, {t: doc.scope === "library" ? "whole library" : "this folder"}, {t: "checked " + (doc.generated || "").slice(0, 10)}], notices,
    sections: [sec("attn", "Needs your attention", "warn", bad, "warn"), sec("sup", "Supported by your papers", "check", sup), sec("nf", "Not found in your papers", "search", nf, "", true)]});
}

// ---------------------------------------------------------------- actions
function onInput(e) { if (e.target.id === "cl-text") { C.text = e.target.value; splitSoon(); recostRv(); } }
async function onChange(e) {
  const t = e.target;
  if (t.id === "cl-scope") { C.scope = t.value; return split([...C.sel].map(n => (C.split.find(c => c.n === n) || {}).text).filter(Boolean)); }
  if (t.matches("[data-cs]")) { const n = +t.dataset.cs; t.checked ? C.sel.add(n) : C.sel.delete(n); t.closest(".cl-s").classList.toggle("off", !t.checked); const h = $(".cl-go", C.el); $(".eyebrow small", C.el).textContent = `${C.sel.size} of ${C.split.length} ticked`; drawGo(); recost(); return; }
  if (t.matches("[data-hd]")) { try { const r = await api("/api/folder/claims", {id: C.fid, n: +t.dataset.hd, handled: t.checked, rev: C.data.doc.rev}); C.data.doc = r.doc; t.closest(".cl-row").classList.toggle("done", t.checked); } catch (er) { notifyError(er); } }
}
async function onClick(e) {
  const t = e.target, a = t.closest("[data-cl]")?.dataset.cl, o = t.closest("[data-open]");
  if (o) { const row = ctx.getLib().find(r => r.file === o.dataset.open); if (!row) return toast("That paper is no longer in your library."); return ctx.readPaper(row, {goto: {page: +o.dataset.page, quote: o.dataset.quote || ""}}); }
  if (!a) return;
  if (a === "draft") { C.text = C.data.draft; $("#cl-text", C.el).value = C.text; return split(null); }
  if (a === "clear") { C.text = ""; $("#cl-text", C.el).value = ""; return split(null); }
  if (a === "all" || a === "none") { C.sel = new Set(a === "all" ? C.split.map(c => c.n) : []); drawList(); return recost(); }
  if (a === "review") return review(false);
  if (a === "rvredo") return review(true);
  if (a === "rvcopy") { try { const r = await fetch(`/api/export/review?folder=${encodeURIComponent(C.fid)}`); await navigator.clipboard.writeText(await r.text()); toast("Copied."); } catch (er) { notifyError(er); } return; }
  if (a === "rvmd") { const l = document.createElement("a"); l.href = `/api/export/review?folder=${encodeURIComponent(C.fid)}`; l.download = ""; document.body.append(l); l.click(); l.remove(); return; }
  if (a === "go") return run(false);
  if (a === "redo") return run(true);
  if (a === "copy") { try { const r = await fetch(`/api/export/claims?folder=${encodeURIComponent(C.fid)}&fmt=md`); await navigator.clipboard.writeText(await r.text()); toast("Copied."); } catch (er) { notifyError(er); } return; }
  if (a === "docx" || a === "md") { const l = document.createElement("a"); l.href = `/api/export/claims?folder=${encodeURIComponent(C.fid)}&fmt=${a}`; l.download = ""; document.body.append(l); l.click(); l.remove(); return; }
  if (a === "save") { try { const r = await api("/api/export/save", {what: "claims", folder: C.fid, fmt: "docx"}); toast("Saved: " + r.path); } catch (er) { notifyError(er); } }
}
async function run(fresh) {
  if (C.busy) return;
  const texts = fresh && C.data.doc && !C.sel.size ? C.data.doc.claims.map(c => c.text) : C.split.filter(c => C.sel.has(c.n)).map(c => c.text);
  if (!texts.length) return toast("Tick at least one sentence.");
  C.busy = true; drawGo(); const st = () => $("#cl-st", C.el);
  try {
    const r = await fetch("/api/ai/claims", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({id: C.fid, scope: C.scope, text: C.text, texts, fresh})});
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
    const rd = r.body.getReader(), dec = new TextDecoder(); let buf = "", done = null;
    for (;;) {
      const {value, done: fin} = await rd.read(); if (fin) break; buf += dec.decode(value, {stream: true}); let i;
      while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1); if (!line) continue; const m = JSON.parse(line);
        if (m.type === "status" && st()) progress(st(), m); else if (m.type === "claims") C.data.doc = m.doc; else if (m.type === "done") done = m; else if (m.type === "error") throw new Error(m.error);
      }
    }
    C.busy = false; drawOut(); drawGo(); if (done) toast("Claims checked" + (done.cost != null ? " · " + money(done.cost) : "") + ".");
    $("#cl-out", C.el).scrollIntoView({behavior: "smooth", block: "start"});
  } catch (e) { C.busy = false; drawGo(); if (st()) st().innerHTML = errInline(e); if (!/Cancelled/.test(e.message)) notifyError(e); }
}


// ---------------------------------------------------------------- Reviewer 2 (G6): the same text, read as a sceptical journal reviewer would read it
const REC = {accept: ["Accept", "ok"], minor: ["Minor revision", "ok"], major: ["Major revision", "warn"], reject: ["Reject as it stands", "warn"]};
const recostRv = debounce(async () => {
  const b = $("#cl-rvest", C.el); if (!b) return; if (C.text.trim().split(/\s+/).length < 25) { b.textContent = ""; return; }
  try { const r = await api("/api/ai/review/estimate", {id: C.fid, text: C.text}); if ($("#cl-rvest", C.el)) $("#cl-rvest", C.el).textContent = r.cost != null ? " · " + money(r.cost) : ""; } catch (e) { /* a courtesy */ }
}, 500);
function drawRev() {
  const box = $("#cl-rev", C.el), doc = C.data && C.data.review; if (!box) return; if (!doc) { box.innerHTML = ""; return; }
  const nid = i => { const p = doc.papers[i.split("-")[0]], n = doc.notes[i]; return `<button class="dr-c" data-rvcite="${esc(i)}" title="${esc(n ? n.kind + ": " + n.text : i)}">${esc(p ? p.short.replace(/\s*\((\d{4})\)/, ", $1") : i)}${n && n.page ? ", p. " + n.page : ""}</button>`; };
  const list = (items, f) => items.map(f).join("");
  const [rl, tone] = REC[doc.recommendation] || REC.major;
  const lead = `<p class="sy-ov">${esc(doc.summary)}</p><div class="rv2-rec ${tone}">${ico(tone === "ok" ? "check" : "warn", "sm")} <b>${esc(rl)}</b> <span class="pmeta">if this text were submitted as it stands</span></div>`;
  const acts = `<button class="rd-btn" data-cl="rvcopy">${ico("copy", "sm")} Copy</button><button class="rd-btn" data-cl="rvmd">.md</button><button class="rd-btn" data-cl="rvredo" title="Review again, ignoring the saved answer">${ico("redo", "sm")} Redo</button>`;
  box.innerHTML = `<div style="margin-top:14px">` + frame({tool: "claims", kind: "Reviewer 2", icon: "warn", key: "rv2:" + C.fid, title: `${doc.major.length} major · ${doc.minor.length} minor · ${doc.unsupported.length} unsupported claim${doc.unsupported.length === 1 ? "" : "s"}`, actions: acts,
    meta: [{t: doc.model}, doc.cost != null ? {t: money(doc.cost)} : null, {t: "read " + (doc.generated || "").slice(0, 10)}], notices: doc.dropped ? callout("warn", `${doc.dropped} thing${doc.dropped === 1 ? "" : "s"} the reviewer said (a quotation that is not in your text, or a paper it was not given) ${doc.dropped === 1 ? "was" : "were"} left out.`) : "",
    sections: [{id: "v", title: "Verdict", icon: "sparkle", tone: "lead", body: lead},
      doc.major.length ? {id: "maj", title: "Major concerns", icon: "warn", count: doc.major.length, tone: "warn", body: list(doc.major, (m, i) => `<div class="rv2-i"><b>${i + 1}. ${esc(m.point)}</b><div class="cl-why">${esc(m.why)}</div>${m.suggestion ? `<div class="rv2-fix"><b>Suggestion</b> ${esc(m.suggestion)}</div>` : ""}</div>`)} : null,
      doc.minor.length ? {id: "min", title: "Minor concerns", icon: "info", count: doc.minor.length, collapsed: true, body: list(doc.minor, m => `<div class="rv2-i">${esc(m.point)}${m.suggestion ? `<div class="rv2-fix"><b>Suggestion</b> ${esc(m.suggestion)}</div>` : ""}</div>`)} : null,
      doc.unsupported.length ? {id: "uns", title: "Claims that need evidence", icon: "search", count: doc.unsupported.length, body: list(doc.unsupported, u => `<div class="rv2-i"><blockquote class="ol-q">${esc(u.quote)}</blockquote><div class="rv2-fix"><b>What would convince</b> ${esc(u.need)}</div></div>`)} : null,
      doc.literature.length ? {id: "lit", title: "Your own papers bear on this", icon: "papers", count: doc.literature.length, body: list(doc.literature, l => `<div class="rv2-i">${esc(l.point)} <div class="sy-based">${l.ids.map(nid).join(" ")}</div></div>`)} : null,
      doc.questions.length ? {id: "q", title: "Questions the reviewer would ask", icon: "ask", count: doc.questions.length, body: `<ul class="rv2-q">${doc.questions.map(q => `<li>${esc(q)}</li>`).join("")}</ul>`} : null]}) + `</div>`;
}
async function review(fresh) {
  if (C.busy) return; if (C.text.trim().split(/\s+/).length < 25) return toast("Write or paste at least a few sentences first.");
  C.busy = true; const st = () => $("#cl-rvst", C.el), btn = $("#cl-rvb", C.el); if (btn) btn.disabled = true;
  try {
    const r = await fetch("/api/ai/review", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({id: C.fid, text: C.text, fresh})});
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
    const rd = r.body.getReader(), dec = new TextDecoder(); let buf = "", done = null;
    for (;;) {
      const {value, done: fin} = await rd.read(); if (fin) break; buf += dec.decode(value, {stream: true}); let i;
      while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1); if (!line) continue; const m = JSON.parse(line);
        if (m.type === "status" && st()) progress(st(), m); else if (m.type === "review") C.data.review = m.doc; else if (m.type === "done") done = m; else if (m.type === "error") throw new Error(m.error);
      }
    }
    C.busy = false; if (btn) btn.disabled = false; if (st()) st().textContent = ""; drawRev(); if (done) toast("Reviewer 2 has read it" + (done.cost != null ? " · " + money(done.cost) : "") + ".");
    $("#cl-rev", C.el).scrollIntoView({behavior: "smooth", block: "start"});
  } catch (e) { C.busy = false; if (btn) btn.disabled = false; if (st()) st().innerHTML = errInline(e); if (!/Cancelled/.test(e.message)) notifyError(e); }
}
document.addEventListener("click", e => { const c = e.target.closest("[data-rvcite]"); if (!c || !C.data || !C.data.review || !c.closest("#cl-rev")) return; const doc = C.data.review, id = c.dataset.rvcite, p = doc.papers[id.split("-")[0]], n = doc.notes[id], row = p && ctx.getLib().find(r => r.file === p.file); if (row) ctx.readPaper(row, n && n.page ? {goto: {page: n.page, quote: n.quote || ""}} : {tab: "card"}); });

window.ClaimsUI = {init(c) { ctx = c; }, mount};
