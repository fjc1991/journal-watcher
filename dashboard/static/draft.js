// Folder tab "Related work": a related-work section written from the folder's paper cards. You say what your own study is about (optional), how long it
// should be and which papers to use; the model sees only the cards' notes and must end every claim with the ids of the notes it rests on. Those ids become
// citations you can click (they open the paper at the page the note came from), the reference list comes from your library's own data (the model never writes a
// reference), and a check lists citations that did not exist, sentences with no source and papers that were never cited. Edit the text, switch between
// author-year and numbered citations, copy it, or export it as Word or Markdown. Saved as Papers/_folders/<id>.draft.json.
// Server: GET /api/folder/draft, POST /api/ai/draft[/estimate] (stream: status | progress | draft | done | error), POST /api/folder/draft (edits, citation style).
import {$, $$, esc, ico, api, toast, notifyError, errBox, errInline, md, debounce, progress, aboutCost} from "./state.js";
import {frame, mdParts, callout, wire as wireOut} from "./output.js";

let ctx = null;
const D = {fid: null, el: null, data: null, edit: false, busy: false, sel: new Set(), form: {topic: "", themes: "", words: 600, style: "author-year", pages: false}};
const money = aboutCost;
const ID = "P\\d+(?:-\\d+)?";
const RE = new RegExp("\\[\\s*(" + ID + "(?:\\s*[,;]\\s*" + ID + ")*)\\s*\\]|\\{\\s*(" + ID + ")\\s*\\}", "g");

export async function mount(el, fid) {
  D.fid = fid; D.el = el; D.edit = false; el.innerHTML = `<div class="card ev-card">Looking at the folder…</div>`;
  try { D.data = await api("/api/folder/draft?id=" + encodeURIComponent(fid)); }
  catch (e) { el.innerHTML = `<div class="card empty"><h2>The drafter isn’t available</h2>${esc(e.message)}</div>`; return; }
  const st = (D.data.doc && D.data.doc.settings) || {};
  D.form = {topic: st.topic || "", themes: st.themes || "", words: st.words || 600, style: (D.data.doc && D.data.doc.style) || "author-year", pages: !!(D.data.doc && D.data.doc.pages)};
  const have = D.data.papers.filter(p => p.card).map(p => p.file);
  D.sel = new Set(st.files && st.files.length ? st.files.filter(f => have.includes(f)) : have);
  if (!el._dr) { el._dr = true; el.addEventListener("click", onClick); el.addEventListener("change", onChange); el.addEventListener("input", onInput); wireOut(el); }
  draw();
}

// ---------------------------------------------------------------- the draft, with clickable citations
function cites(html, doc, order) {
  const P = doc.papers, N = doc.notes, style = doc.style, pages = doc.pages;
  const num = pid => { let i = order.indexOf(pid); if (i < 0) { order.push(pid); i = order.length - 1; } return i + 1; };
  const tip = id => { const n = N[id]; return n ? `${n.kind}: ${n.text}${n.page ? " (p. " + n.page + ")" : ""}` : (P[id.split("-")[0]] || {}).title || id; };
  const pg = (ids, pid) => { const ps = [...new Set(ids.filter(i => i.startsWith(pid + "-") && N[i] && N[i].page).map(i => N[i].page))].sort((a, b) => a - b); return pages && ps.length ? (ps.length === 1 ? ", p. " + ps[0] : ", pp. " + ps[0] + "–" + ps[ps.length - 1]) : ""; };
  const chip = (id, text) => `<button class="dr-c" data-cite="${id}" title="${esc(tip(id))}">${esc(text)}</button>`;
  return html.replace(RE, (m, g1, g2) => {
    if (g2) { const pid = g2.split("-")[0]; if (!P[pid]) return ""; const n = num(pid); return chip(g2, style === "numbered" ? `${P[pid].label} [${n}]` : `${P[pid].label} (${P[pid].year}${pg([g2], pid)})`); }
    const ids = (g1.match(new RegExp(ID, "g")) || []).filter(i => P[i.split("-")[0]]), by = new Map();
    ids.forEach(i => { const pid = i.split("-")[0]; if (!by.has(pid)) by.set(pid, []); by.get(pid).push(i); });
    if (!by.size) return "";
    const nums = new Map([...by.keys()].map(p => [p, num(p)]));
    return style === "numbered" ? "[" + [...by].map(([p, xs]) => chip(xs[0], String(nums.get(p)))).join(", ") + "]"
      : "(" + [...by].map(([p, xs]) => chip(xs[0], `${P[p].label}, ${P[p].year}${pg(xs, p)}`)).join("; ") + ")";
  });
}
function sectionsOf(doc) {
  const order = [], R = doc.render;
  const parts = mdParts(doc.raw, {render: t => cites(md(t), doc, order), leadTitle: ""});
  const refs = (R.refs || []).map(r => `<li${doc.style === "numbered" ? ` value="${r.n}"` : ""}>${esc(r.ref[0])}${r.ref[1] ? `<i>${esc(r.ref[1])}</i>` : ""}${esc(r.ref[2])} <button class="linkbtn" data-open="${esc(r.file)}">Open</button></li>`).join("");
  const probs = (doc.invalid ? 1 : 0) + (doc.uncited.length ? 1 : 0) + (doc.unused.length ? 1 : 0) + ((doc.missing || []).length ? 1 : 0);
  const check = (doc.invalid ? callout("warn", `${doc.invalid} citation${doc.invalid === 1 ? "" : "s"} pointed at notes that don’t exist and ${doc.invalid === 1 ? "was" : "were"} removed from the text you copy or export.`) : "") +
    (doc.uncited.length ? `<details class="dr-unc"><summary>${doc.uncited.length} sentence${doc.uncited.length === 1 ? "" : "s"} carry no citation: check them, add a source or cut them</summary><ul>${doc.uncited.map(s => `<li>${esc(s)}</li>`).join("")}</ul></details>` : "") +
    (doc.unused.length ? `<div class="dr-ck">${ico("info", "sm")} Not cited: ${doc.unused.map(u => esc(u.cite)).join("; ")}</div>` : "") +
    ((doc.missing || []).length ? `<div class="dr-ck">${ico("info", "sm")} Left out because they have no card: ${doc.missing.map(u => esc((u.title || "").slice(0, 60))).join("; ")}</div>` : "") +
    (!probs ? callout("ok", "Every citation points at a real note, every sentence has a source, and every paper you chose is cited.") : "");
  return [...parts, {id: "refs", title: "References", icon: "link", count: (R.refs || []).length, body: `<ol class="dr-refs">${refs}</ol><div class="pmeta">Built from your library’s own data (authors, year, title, journal, DOI), not by the model.</div>`},
    {id: "check", title: "Check before you use it", short: "Check", icon: "warn", tone: probs ? "warn" : "", count: probs || "✓", collapsed: !probs, body: check}];
}

// ---------------------------------------------------------------- the page
function draw() {
  const el = D.el, d = D.data, doc = d.doc, F = D.form, withCard = d.papers.filter(p => p.card);
  const setup = `<div class="card dr-setup" data-tool="draft"><div class="dr-h"><span class="of-ico">${ico("draft")}</span><div><div class="eyebrow t">Related work</div><div class="of-title">${doc ? "Write it again, or change what it uses" : "Write a related-work section from this folder’s cards"}</div></div></div>
    <div class="dr-grid"><label>Your own study <small>(optional: it is used for the closing “Gap and contribution” paragraph)</small><textarea id="dr-topic" rows="3" maxlength="1500" placeholder="e.g. We study how disclosing generative-AI use shapes backers’ trust and funding decisions on crowdfunding platforms.">${esc(F.topic)}</textarea></label>
      <div class="dr-side"><label>Themes you want <small>(optional)</small><input type="text" id="dr-themes" maxlength="400" placeholder="e.g. first by theory, then by method and context" value="${esc(F.themes)}"></label>
        <div class="dr-opts"><label>Length<select id="dr-words">${[[300, "About 300 words"], [600, "About 600 words"], [1000, "About 1000 words"]].map(([w, t]) => `<option value="${w}"${F.words === w ? " selected" : ""}>${t}</option>`).join("")}</select></label>
          <label>Citations<select id="dr-style"><option value="author-year"${F.style === "author-year" ? " selected" : ""}>Author, year</option><option value="numbered"${F.style === "numbered" ? " selected" : ""}>Numbered [1]</option></select></label></div></div></div>
    <div class="dr-papers"><div class="eyebrow">Papers to use <small>${D.sel.size} of ${withCard.length} with a card</small> <button class="linkbtn" data-dr="all">all</button> · <button class="linkbtn" data-dr="none">none</button></div>
      ${d.papers.map(p => `<label class="dr-p${p.card ? "" : " off"}"><input type="checkbox" data-pf="${esc(p.file)}"${D.sel.has(p.file) ? " checked" : ""}${p.card ? "" : " disabled"}> <b>${esc(p.cite)}</b> ${esc((p.title || "").slice(0, 90))}${p.card ? "" : ` <span class="of-badge inferred">no card</span>`}</label>`).join("") || `<div class="pmeta">This folder has no papers yet.</div>`}
      ${withCard.length < d.papers.length ? `<div class="pmeta">Papers without a card can’t be used: <button class="linkbtn" data-dr="evtab">make the cards in the Evidence tab</button>.</div>` : ""}</div>
    <div class="row dr-go"><button class="btn primary" data-dr="go"${D.busy || D.sel.size < 2 ? " disabled" : ""}>${ico("notes", "sm")} ${doc ? "Write it again" : "Write the section"}<span id="dr-est"></span></button><span class="pmeta" id="dr-st">${D.sel.size < 2 ? "Choose at least two papers that have a card." : ""}</span></div>
    <div class="pmeta">The model sees only your cards (not the PDFs). Every claim has to end with the notes it rests on; those become citations you can click, and nothing it writes is trusted until the check below says so.</div></div>`;
  el.innerHTML = setup + (doc ? out(doc) : "");
  estimate();
}
function out(doc) {
  const R = doc.render, used = (R.cited || []).length, total = Object.keys(doc.papers).length;
  const acts = `<button class="rd-btn" data-dr="copy" title="Copy the text with its citations and references">${ico("copy", "sm")} Copy</button><button class="rd-btn" data-dr="docx" title="Download as Word">Word</button><button class="rd-btn" data-dr="md" title="Download as Markdown">.md</button><button class="rd-btn" data-dr="tex" title="LaTeX for Overleaf: \\subsection, \\citep{key} and \\citet{key} (natbib), with the same keys as the .bib">.tex</button><button class="rd-btn" data-dr="bib" title="The .bib entries of the papers this text cites">.bib</button>
    <button class="rd-btn" data-dr="save" title="Save a Word file into your export folder (Settings → Reader)">Save to export folder</button><button class="rd-btn${D.edit ? " on" : ""}" data-dr="edit">${ico("notes", "sm")} ${D.edit ? "Done editing" : "Edit"}</button>`;
  const sub = `Citations <select id="dr-cs"><option value="author-year"${doc.style === "author-year" ? " selected" : ""}>Author, year</option><option value="numbered"${doc.style === "numbered" ? " selected" : ""}>Numbered</option></select> <label class="chk" style="display:inline-flex"><input type="checkbox" id="dr-cp"${doc.pages ? " checked" : ""}${doc.style === "numbered" ? " disabled" : ""}> page numbers</label>`;
  const body = D.edit ? `<div class="dr-edit"><textarea id="dr-raw" spellcheck="true">${esc(doc.raw)}</textarea><div class="pmeta">Citations are written as <code>[P2-3]</code> (in brackets) or <code>{P2-3}</code> (the study’s name): P2 is the second paper, -3 its third note. Hover a citation in the preview to see which note an id is. They turn into proper citations when you copy or export.</div>
      <div class="row"><button class="btn primary" data-dr="saveedit">Save my edits</button><button class="btn" data-dr="edit">Cancel</button></div></div>` : "";
  const f = frame({tool: "draft", kind: "Related work", icon: "draft", key: "draft:" + D.fid, title: `${doc.words} words · ${used} of ${total} papers cited`, sub, actions: acts,
    meta: [{t: doc.model}, doc.cost != null ? {t: money(doc.cost)} : null, doc.edited ? {t: "✎ edited by you", cls: "edited"} : null, {t: "written " + (doc.generated || "").slice(0, 10)}], sections: D.edit ? [{id: "edit", title: "Edit the text", icon: "notes", tone: "lead", body}] : sectionsOf(doc)});
  return f;
}

// ---------------------------------------------------------------- actions
const form = () => { const g = id => $("#" + id, D.el); const F = D.form; if (g("dr-topic")) { F.topic = g("dr-topic").value; F.themes = g("dr-themes").value; F.words = +g("dr-words").value; F.style = g("dr-style").value; } return F; };
const settings = () => { const F = form(); return {topic: F.topic, themes: F.themes, words: F.words, style: F.style, pages: F.pages || (D.data.doc && D.data.doc.pages) || false, files: [...D.sel]}; };
let seq = 0;
const estimate = debounce(async () => {
  const my = ++seq, e = $("#dr-est", D.el); if (!e || D.sel.size < 2) { if (e) e.textContent = ""; return; }
  try { const r = await api("/api/ai/draft/estimate", {id: D.fid, settings: settings()}); if (my === seq && $("#dr-est", D.el)) $("#dr-est", D.el).textContent = r.cost != null ? " · " + money(r.cost) : ""; } catch (er) { /* a courtesy */ }
}, 350);
function onInput(e) { if (e.target.matches("#dr-topic, #dr-themes")) form(); }
async function onChange(e) {
  const t = e.target;
  if (t.matches("[data-pf]")) { t.checked ? D.sel.add(t.dataset.pf) : D.sel.delete(t.dataset.pf); form(); return draw(); }
  if (t.matches("#dr-words, #dr-style")) { form(); estimate(); return; }
  if (t.matches("#dr-cs, #dr-cp")) { try { const r = await api("/api/folder/draft", {id: D.fid, style: $("#dr-cs", D.el).value, pages: $("#dr-cp", D.el).checked, rev: D.data.doc.rev}); D.data.doc = r.doc; D.form.style = r.doc.style; draw(); } catch (er) { notifyError(er); } }
}
async function onClick(e) {
  const t = e.target, a = t.closest("[data-dr]")?.dataset.dr, c = t.closest("[data-cite]"), o = t.closest("[data-open]");
  if (c) return openCite(c.dataset.cite);
  if (o) { const r = ctx.getLib().find(x => x.file === o.dataset.open); if (r) ctx.readPaper(r, {tab: "card"}); return; }
  if (!a) return;
  const doc = D.data.doc;
  if (a === "all") { D.data.papers.filter(p => p.card).forEach(p => D.sel.add(p.file)); form(); return draw(); }
  if (a === "none") { D.sel.clear(); form(); return draw(); }
  if (a === "evtab") return window.LibraryUI.folderTab("evidence");
  if (a === "go") return write(!!doc);
  if (a === "copy") { const r = doc.render; const refs = r.refs.map(x => (doc.style === "numbered" ? x.n + ". " : "") + x.text).join("\n"); try { await navigator.clipboard.writeText(r.text + "\n\nReferences\n" + refs); toast("Copied, with citations and references."); } catch (er) { notifyError(er); } return; }
  if (a === "docx" || a === "md" || a === "tex" || a === "bib") { const l = document.createElement("a"); l.href = `/api/export/draft?folder=${encodeURIComponent(D.fid)}&fmt=${a}`; l.download = ""; document.body.append(l); l.click(); l.remove(); return; }
  if (a === "save") { try { const r = await api("/api/export/save", {what: "draft", folder: D.fid, fmt: "docx"}); toast("Saved: " + r.path); } catch (er) { notifyError(er); } return; }
  if (a === "edit") { D.edit = !D.edit; return draw(); }
  if (a === "saveedit") { try { const r = await api("/api/folder/draft", {id: D.fid, raw: $("#dr-raw", D.el).value, rev: doc.rev}); D.data.doc = r.doc; D.edit = false; draw(); toast("Saved. The citations were checked again."); } catch (er) { notifyError(er); } }
}
function openCite(id) {
  const doc = D.data.doc, pid = id.split("-")[0], p = doc.papers[pid], n = doc.notes[id], row = p && ctx.getLib().find(r => r.file === p.file);
  if (!row) return toast("That paper is no longer in your library.");
  ctx.readPaper(row, n && n.page ? {goto: {page: n.page, quote: n.quote || ""}} : {tab: "card"});
}
async function write(again) {
  if (D.busy) return; const s = settings(); if (s.files.length < 2) return toast("Choose at least two papers that have a card.");
  if (again && D.data.doc && D.data.doc.edited && !confirm("You edited this draft. Writing it again replaces your edits.\n\nContinue?")) return;
  D.busy = true; draw(); const st = () => $("#dr-st", D.el);
  try {
    const r = await fetch("/api/ai/draft", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({id: D.fid, settings: s, fresh: again})});
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
    const rd = r.body.getReader(), dec = new TextDecoder(); let buf = "", done = null;
    for (;;) {
      const {value, done: fin} = await rd.read(); if (fin) break; buf += dec.decode(value, {stream: true}); let i;
      while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1); if (!line) continue; const m = JSON.parse(line);
        if (m.type === "status" && st()) progress(st(), m); else if (m.type === "progress" && st()) progress(st(), `Writing… ${(m.chars / 1000).toFixed(1)}k characters`);
        else if (m.type === "draft") { D.data.doc = m.doc; D.form.style = m.doc.style; D.form.pages = m.doc.pages; } else if (m.type === "done") done = m; else if (m.type === "error") throw new Error(m.error);
      }
    }
    if (done) toast("Draft written" + (done.cost != null ? " · " + money(done.cost) : "") + ".");
  } catch (e) { D.busy = false; draw(); if ($("#dr-st", D.el)) $("#dr-st", D.el).innerHTML = errInline(e); if (!/Cancelled/.test(e.message)) notifyError(e); return; }
  D.busy = false; draw();
}

window.DraftUI = {init(c) { ctx = c; }, mount};
