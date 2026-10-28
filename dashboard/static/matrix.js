// Folder tab "Matrix": papers as rows, questions as columns, each cell with the pages it comes from.
// Built-in columns come from the paper cards (edit them in the reader's Card tab). Your own columns ("Sample size?", "Which industry?") are filled
// by the model from each paper's closest passages, one call per paper; "Not reported" is a valid answer; click a cell to write your own.
// The matrix is saved in Papers/_folders/<id>.matrix.json. Export: CSV, Word, LaTeX (with \citet keys), Markdown.
import {notifyError, $, $$, esc, ico, api, toast} from "./state.js";

let ctx = null;
const M = {fid: null, t: null, busy: false};
const money = c => c == null ? "" : c < 0.01 ? "under 1¢" : "about $" + (c < 1 ? c.toFixed(2) : c.toFixed(1));
const dl = url => { const a = document.createElement("a"); a.href = url; a.download = ""; document.body.append(a); a.click(); a.remove(); };
function openAt(file, page, quote) {
  const row = ctx.getLib().find(r => r.file === file);
  if (row) ctx.readPaper(row, {goto: {page: page || 1, quote: quote || ""}, tab: quote ? undefined : "card"}); else toast("That paper is no longer in your library.");
}

export async function mount(el, fid) {
  M.fid = fid; M.el = el; el.innerHTML = `<div class="card ev-card">Building the table…</div>`;
  await load();
  el.onclick = onClick; el.onchange = onChange;
}
async function load() {
  try { M.t = await api("/api/folder/matrix?id=" + encodeURIComponent(M.fid)); }
  catch (e) { M.el.innerHTML = `<div class="card empty"><h2>The matrix isn’t available</h2>${esc(e.message)}</div>`; return; }
  draw();
}

function cellHtml(r, c) {
  const cell = r.cells[c.id], key = r.key + "|" + c.id;
  if (!cell) return c.builtin ? `<span class="mx-none">${r.card ? "—" : ""}</span>` : `<span class="mx-none mx-edit" data-cell="${esc(key)}" title="Click to write it yourself">—</span>`;
  const lines = cell.text.split("\n").filter(Boolean), nr = cell.status === "not reported";
  const txt = lines.length > 1 ? `<ul>${lines.map(l => `<li>${esc(l)}</li>`).join("")}</ul>` : esc(lines[0] || "");
  const pages = (cell.cites || []).map(x => `<button class="mx-cite" data-open="${esc(r.file)}" data-page="${x.page}" data-q="${esc(x.quote || "")}" title="${esc((x.quote || "").slice(0, 140))}…">p. ${x.page}</button>`).join("");
  return `<div class="mx-c ${nr ? "mx-nr" : ""}${c.builtin ? "" : " mx-edit"}"${c.builtin ? "" : ` data-cell="${esc(key)}" title="Click to edit"`}>${txt}</div>${pages}${cell.edited ? `<span class="cd-edmark" title="Written by you">✎</span>` : ""}`;
}
function draw() { drawNow(); foldLong(); }
function drawNow() {
  const t = M.t, cols = t.columns, doc = t.doc, custom = doc.custom;
  const fill = t.fill || {cells: 0};
  const nCols = cols.length, built = cols.filter(c => c.builtin).length;
  M.el.innerHTML = `<div class="card ev-card mx-bar mx-top" data-tool="matrix"><div class="mx-head"><span class="of-ico">${ico("matrix")}</span><div><div class="eyebrow t">Comparison matrix</div><div class="of-title">${t.rows.length} paper${t.rows.length === 1 ? "" : "s"} × ${nCols} column${nCols === 1 ? "" : "s"}</div></div></div>
    <div class="row" style="gap:8px;flex-wrap:wrap;align-items:center">
      <span class="menuwrap"><button class="btn" data-k="cols">Columns ▾</button><div class="menu card mx-menu hide" id="mx-colmenu">${colMenu(t)}</div></span>
      ${custom.length && (fill.cells || M.busy) ? `<button class="btn primary" data-k="fill"${M.busy ? " disabled" : ""}>${ico("sparkle", "sm")} Fill ${fill.cells} empty cell${fill.cells === 1 ? "" : "s"}${fill.cost ? " · " + money(fill.cost) : ""}</button>` : custom.length ? `<span class="of-badge">${ico("check", "sm")} every cell is filled</span>` : ""}
      <span class="pmeta" id="mx-st"></span><span style="flex:1"></span>
      <span class="menuwrap"><button class="btn" data-k="exp">Export ▾</button><div class="menu card hide" id="mx-expmenu"><button data-x="csv">CSV (Excel)</button><button data-x="docx">Word table (.docx)</button><button data-x="tex">LaTeX table</button><button data-x="md">Markdown</button>
        <button data-x="save-docx">Save .docx to my export folder</button><button data-x="save-csv">Save .csv to my export folder</button></div></span></div>
      <div class="pmeta" style="margin-top:6px">Built-in columns come from the paper cards. “＋ New column” adds a question of your own that the model answers for every paper, quoting the page.${t.missing_cards ? ` <span class="cd-tag">${t.missing_cards} paper${t.missing_cards === 1 ? " has" : "s have"} no card</span> — make cards in the Evidence tab.` : ""}</div></div>
    <div class="mx-legend"><span><i class="mx-kb"></i>from the paper cards</span><span><i class="mx-ky"></i>your own columns</span><span><i class="mx-knr">abc</i>not reported in the paper</span></div></div>
    ${t.rows.length ? `<div class="mx-wrap"><table class="mx"><thead><tr><th class="mx-p">Paper</th>${cols.map((c, i) => `<th data-col="${esc(c.id)}" class="${c.builtin ? "mx-b" : "mx-y"}"><div class="mx-h">${esc(c.name)}${c.builtin ? "" : ` <span class="cd-tag">yours</span>`}</div>
        <div class="mx-hb"><button data-mv="${i}|-1" title="Move left"${i ? "" : " disabled"}>◀</button><button data-mv="${i}|1" title="Move right"${i < cols.length - 1 ? "" : " disabled"}>▶</button><button data-hide="${esc(c.id)}" title="Hide this column">✕</button></div></th>`).join("")}</tr></thead>
      <tbody>${t.rows.map(r => `<tr><th class="mx-p"><button class="linkbtn" data-open="${esc(r.file)}" data-page="1" data-q="">${esc(r.cite)}</button><div class="pmeta">${esc(r.title.slice(0, 90))}</div>${r.card ? "" : `<span class="cd-tag">no card</span>`}</th>${cols.map(c => `<td>${cellHtml(r, c)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`
      : `<div class="card empty"><h2>This folder is empty</h2>Add papers to it first.</div>`}`;
}
function foldLong() {      // a cell taller than ~9 lines is folded behind "more"
  M.el.querySelectorAll(".mx-c").forEach(c => { if (c.scrollHeight > c.clientHeight + 6 && !c.nextElementSibling?.classList.contains("mx-more")) c.insertAdjacentHTML("afterend", `<button class="mx-more" data-more>more</button>`); });
}
function colMenu(t) {
  const shown = new Set(t.doc.columns);
  return `<div class="mx-sec">Built-in (from the cards)</div>${t.builtin.map(b => `<label><input type="checkbox" data-showcol="${b.id}"${shown.has(b.id) ? " checked" : ""}> ${esc(b.name)}</label>`).join("")}
    <div class="mx-sec">Your columns</div>${t.doc.custom.map(c => `<div class="mx-cust"><label><input type="checkbox" data-showcol="${c.id}"${shown.has(c.id) ? " checked" : ""}> ${esc(c.name)}</label><span><button data-editcol="${c.id}" title="Change the question">✎</button><button data-delcol="${c.id}" title="Delete the column">🗑</button></span></div>`).join("") || `<div class="pmeta" style="padding:2px 10px">None yet.</div>`}
    <button class="mx-new" data-k="newcol">＋ New column…</button><button class="mx-new" data-k="preset" title="Adds four columns you fill with one click: measures, data, identification, robustness">＋ Methods &amp; measures (4 columns)</button>`;
}

async function saveDoc(patch) {
  try { const r = await api("/api/folder/matrix", {id: M.fid, rev: M.t.doc.rev, doc: patch}); M.t.doc = r.doc; return true; }
  catch (e) { if (e.status === 409) toast("The matrix changed in another window; reloading."); else notifyError(e); await load(); return false; }
}
async function onChange(e) {
  const c = e.target.closest("[data-showcol]"); if (!c) return;
  const cols = M.t.doc.columns.slice(), id = c.dataset.showcol;
  if (c.checked && !cols.includes(id)) cols.push(id); else if (!c.checked) cols.splice(cols.indexOf(id), 1);
  if (await saveDoc({columns: cols})) { await load(); $("#mx-colmenu", M.el).classList.remove("hide"); }
}
async function onClick(e) {
  const t = e.target, k = t.closest("[data-k]")?.dataset.k;
  if (k === "cols") { e.stopPropagation(); $("#mx-colmenu", M.el).classList.toggle("hide"); return; }
  if (k === "exp") { e.stopPropagation(); $("#mx-expmenu", M.el).classList.toggle("hide"); return; }
  if (k === "fill") return fill();
  if (k === "newcol") return newColumn();
  if (k === "preset") return addPreset();
  const x = t.closest("[data-x]")?.dataset.x;
  if (x) { $("#mx-expmenu", M.el).classList.add("hide");
    if (x.startsWith("save-")) { try { const r = await api("/api/export/save", {what: "matrix", folder: M.fid, fmt: x.slice(5)}); toast("Saved: " + r.path); } catch (er) { notifyError(er); } return; }
    return dl(`/api/export/matrix?folder=${encodeURIComponent(M.fid)}&fmt=${x}`); }
  const mo = t.closest("[data-more]"); if (mo) { const c = mo.previousElementSibling; const open = c.classList.toggle("open"); mo.textContent = open ? "less" : "more"; return; }
  const o = t.closest("[data-open]"); if (o) return openAt(o.dataset.open, +o.dataset.page, o.dataset.q);
  const mv = t.closest("[data-mv]");
  if (mv) { const [i, d] = mv.dataset.mv.split("|").map(Number), cols = M.t.doc.columns.slice(), j = i + d; if (j < 0 || j >= cols.length) return;
    [cols[i], cols[j]] = [cols[j], cols[i]]; if (await saveDoc({columns: cols})) load(); return; }
  const hd = t.closest("[data-hide]"); if (hd) { if (await saveDoc({columns: M.t.doc.columns.filter(c => c !== hd.dataset.hide)})) load(); return; }
  const ec = t.closest("[data-editcol]"), dc = t.closest("[data-delcol]");
  if (ec) { const c = M.t.doc.custom.find(x => x.id === ec.dataset.editcol), name = prompt("Column name:", c.name); if (!name) return;
    const q = prompt("The question to answer for every paper (changing it clears the old answers):", c.prompt); if (!q) return;
    if (await saveDoc({custom: M.t.doc.custom.map(x => x.id === c.id ? {...x, name, prompt: q} : x)})) load(); return; }
  if (dc) { const c = M.t.doc.custom.find(x => x.id === dc.dataset.delcol); if (!confirm(`Delete the column “${c.name}” and its answers?`)) return;
    if (await saveDoc({custom: M.t.doc.custom.filter(x => x.id !== c.id)})) load(); return; }
  const ce = t.closest("[data-cell]"); if (ce && !t.closest("textarea")) return editCell(ce);
}
// G7: what a literature review table usually needs from the methods sections, as four ready-made columns
const PRESET = [["Constructs & measures", "What are the main constructs or variables, and how was each one measured or operationalised (scale, items, proxy)?"],
  ["Data source & period", "What data source, time period and unit of analysis does the study use?"],
  ["Identification strategy", "How does the paper deal with causality or endogeneity (experiment, difference-in-differences, instrumental variable, matching, fixed effects…)? If it does not, say so."],
  ["Robustness checks", "Which robustness checks, alternative specifications or validity tests did the authors run?"]];
async function addPreset() {
  $("#mx-colmenu", M.el).classList.add("hide");
  const have = new Set(M.t.doc.custom.map(c => c.name.toLowerCase())), todo = PRESET.filter(([n]) => !have.has(n.toLowerCase()));
  if (!todo.length) return toast("Those columns are already there.");
  let rev = M.t.doc.rev;
  try { for (const [name, prompt] of todo) { const r = await api("/api/folder/matrix", {id: M.fid, rev, action: "add", name, prompt}); rev = r.doc.rev; } await load(); toast(`${todo.length} columns added: press “Fill” to answer them for every paper.`); }
  catch (e) { notifyError(e); await load(); }
}
async function newColumn() {
  const name = prompt("Name of the column (e.g. “Sample size”):"); if (!name || !name.trim()) return;
  const q = prompt("The question to answer for every paper:", name.trim() + "?"); if (!q) return;
  try { await api("/api/folder/matrix", {id: M.fid, rev: M.t.doc.rev, action: "add", name: name.trim(), prompt: q.trim()}); await load(); toast("Column added: press “Fill” to answer it for every paper."); }
  catch (e) { notifyError(e); await load(); }
}
function editCell(td) {
  const [key, col] = td.dataset.cell.split("|"), row = M.t.rows.find(r => r.key === key), cell = row.cells[col], ta = document.createElement("textarea");
  ta.className = "cd-ed"; ta.value = cell ? cell.text : ""; ta.rows = 3; td.replaceWith(ta); ta.focus();
  let done = false;
  const commit = async cancel => { if (done) return; done = true;
    if (!cancel && ta.value.trim() !== (cell ? cell.text : "")) { if (await saveDoc({edits: {[key]: {[col]: ta.value.trim()}}})) { await load(); return; } }
    draw(); };
  ta.onblur = () => commit(false); ta.onkeydown = e => { if (e.key === "Escape") commit(true); else if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); ta.blur(); } };
}
async function fill() {
  if (M.busy) return; M.busy = true; draw(); const st = () => $("#mx-st", M.el); let total = 0, errs = 0;
  try {
    const r = await fetch("/api/ai/matrix/fill", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({id: M.fid})});
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
    const rd = r.body.getReader(), dec = new TextDecoder(); let buf = "";
    for (;;) {
      const {value, done} = await rd.read(); if (done) break; buf += dec.decode(value, {stream: true}); let i;
      while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1); if (!line) continue; const m = JSON.parse(line);
        if (m.type === "start") { total = m.total; if (st()) st().textContent = `Reading ${total} paper${total === 1 ? "" : "s"}…`; }
        else if (m.type === "paper") { const row = M.t.rows.find(x => x.key === m.key); if (row) Object.assign(row.cells, m.cells); if (st()) st().textContent = `${m.done} of ${m.total} papers done`; M.busy = true; const keep = st() && st().textContent; draw(); if (st()) st().textContent = keep; }
        else if (m.type === "error") { errs++; if (!m.file) throw new Error(m.error); toast((m.title || "A paper").slice(0, 40) + ": " + m.error); }
        else if (m.type === "done") toast(`${m.made} paper${m.made === 1 ? "" : "s"} filled${m.cost ? " · " + money(m.cost) : ""}${errs ? ` · ${errs} failed` : ""}.`);
      }
    }
  } catch (e) { notifyError(e); }
  M.busy = false; await load();
}

window.MatrixUI = {init(c) { ctx = c; }, mount};
