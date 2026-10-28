// Discover: papers you don't have yet.
//   · folder tab / Library tab "Discover": papers cited by several of the papers in scope but missing from your library, what OpenAlex calls related to them,
//     and the papers you already have that are closest in meaning (folders). Tick some and press Download: they go into the same download queue as a weekly
//     check (free copies first, then your publisher and library access) and each gets a summary.
//   · the reader's "Related" tab: the same for the open paper (similar papers you have; what it cites that you don't have; related papers you don't have).
// Server: POST /api/related/scope (stream: status | result | error), GET /api/related/paper, POST /api/related/{ignore,download}.
import {errBox, notifyError, S as RS, $, $$, esc, ico, api, toast, on} from "./state.js";
import {frame, wire as wireOut} from "./output.js";

let ctx = null;
const money = c => c == null ? "" : "about $" + c.toFixed(2);
const authorsOf = c => { const a = [...new Set(c.authors || [])]; return a.slice(0, 3).join(", ") + (a.length > 3 ? " et al." : ""); };

function openPaper(file, page) {
  const row = ctx && ctx.getLib().find(r => r.file === file);
  if (row) ctx.readPaper(row, page ? {goto: {page}} : undefined); else toast("That paper is no longer in your library.");
}

// ---------------------------------------------------------------- one candidate row (used by every list)
function candRow(c, kind, compact) {
  const link = c.doi ? "https://doi.org/" + c.doi : "https://openalex.org/" + c.id;
  const via = (c.via || []).length ? `<div class="dc-via"><button class="linkbtn" data-via="${esc(c.id)}">${kind === "missing" ? `cited by ${c.n} of your papers` : `related to ${c.n} of your papers`} ▾</button>
      <div class="dc-vialist hide">${c.via.map(v => `<div><button class="linkbtn" data-openpaper="${esc(v.file)}">${esc(v.cite)}</button> <span class="pmeta">${esc((v.title || "").slice(0, 70))}</span></div>`).join("")}</div></div>` : "";
  return `<div class="dc-row${compact ? " compact" : ""}" data-id="${esc(c.id)}"><input type="checkbox" data-sel="${esc(c.id)}" aria-label="Select">
    <div class="dc-main"><a class="dc-title" href="${esc(link)}" target="_blank" rel="noopener">${esc(c.title)}</a>
      <div class="pmeta">${esc(authorsOf(c))}${authorsOf(c) ? " · " : ""}${esc(c.venue || "")}${c.year ? " · " + c.year : ""}${c.cited_by ? ` · ${c.cited_by.toLocaleString()} citations` : ""}${c.doi ? "" : " · no DOI"}</div>${via}</div>
    <button class="cd-x" data-ign="${esc(c.id)}" style="opacity:.6" title="Not interested: hide this paper from these lists">✕</button></div>`;
}
function simRow(x) {
  return `<div class="dc-row compact sim"><div class="dc-main"><button class="linkbtn dc-title" data-openpaper="${esc(x.file)}">${esc(x.title)}</button><div class="pmeta">${esc(x.cite)} · ${esc(x.journal || "")} · closeness ${Math.round(x.sim * 100)}%</div></div></div>`;
}

// ---------------------------------------------------------------- shared actions: select, ignore, download
function actions(S) {
  const el = S.el;
  const items = () => {
    const picked = new Set($$("[data-sel]:checked", el).map(b => b.dataset.sel)), out = [];
    for (const [list, kind] of [[S.missing, "missing"], [S.related, "related"], [S.cited, "cited"]]) for (const c of list || []) if (picked.has(c.id) && !out.find(o => o.id === c.id))
      out.push({id: c.id, doi: c.doi, title: c.title, authors: c.authors, year: c.year, venue: c.venue, date: c.date, reason: kind === "missing" ? `Cited by ${c.n} of your papers` : kind === "cited" ? "Cited by a paper in your library" : "Related to papers in your library"});
    return out;
  };
  const bar = () => { const n = items().length, b = $(".dc-bar", el); if (!b) return; b.classList.toggle("hide", !n); $(".dc-n", b).textContent = n; };
  el.addEventListener("change", e => { if (e.target.matches("[data-sel]")) bar(); });
  el.addEventListener("click", async e => {
    const t = e.target, via = t.closest("[data-via]"), op = t.closest("[data-openpaper]"), ig = t.closest("[data-ign]"), k = t.closest("[data-k]")?.dataset.k;
    if (via) { via.parentElement.querySelector(".dc-vialist").classList.toggle("hide"); return; }
    if (op) return openPaper(op.dataset.openpaper);
    if (ig) { const all = [].concat(S.missing || [], S.related || [], S.cited || []), c = all.find(x => x.id === ig.dataset.ign);
      try { await api("/api/related/ignore", {items: [{id: c.id, title: c.title}]}); for (const k2 of ["missing", "related", "cited"]) if (S[k2]) S[k2] = S[k2].filter(x => x.id !== c.id); S.hidden = (S.hidden || 0) + 1; S.redraw(); toast("Hidden. Restore it from “Hidden papers”."); } catch (er) { notifyError(er); }
      return; }
    if (k === "download") {
      const its = items(); if (!its.length) return;
      t.closest("[data-k]").disabled = true;
      try { const r = await api("/api/related/download", {items: its, label: S.label});
        toast(`Downloading ${r.n} paper${r.n === 1 ? "" : "s"}${r.have && r.have.length ? ` (${r.have.length} already in your library)` : ""}…`); if (ctx.openRun) ctx.openRun(r.run); }
      catch (er) { notifyError(er); t.closest("[data-k]").disabled = false; }
    }
    if (k === "unhide") { try { const r = await api("/api/related/ignored"); const ids = Object.keys(r.ignored); if (!ids.length) return toast("Nothing is hidden."); if (!confirm(`Bring back ${ids.length} hidden paper${ids.length === 1 ? "" : "s"}?\n\n` + ids.slice(0, 8).map(i => "• " + r.ignored[i].title).join("\n"))) return;
      await api("/api/related/ignore", {undo: ids}); toast("Restored."); S.rerun && S.rerun(); } catch (er) { notifyError(er); } }
  });
  S.bar = bar;
}
const barHtml = () => `<div class="dc-bar hide card"><b><span class="dc-n">0</span> selected</b><button class="btn primary" data-k="download">⬇ Download</button>
  <span class="pmeta">Free copies first, then your publisher and library access. Each paper that arrives gets a summary (about 2–4¢). Watch it in the Download queue.</span></div>`;

// ---------------------------------------------------------------- folder / library tab
export function mount(el, opts = {}) {
  const nIn = opts.folder ? ctx.getLib().filter(r => (r.folders || []).includes(opts.folder)).length : 99;
  const S = {el, folder: opts.folder || null, scope: {}, min: nIn >= 4 ? 2 : 1, missing: null, related: null, similar: null, busy: false, label: "library"};
  S.label = S.folder ? "this folder" : "the whole library";
  el.innerHTML = `<div class="card ev-card dc-top"><div class="row" style="gap:8px;flex-wrap:wrap;align-items:center">
      ${S.folder ? "" : `<select data-s="folder"><option value="">Whole library</option></select><select data-s="journal"><option value="">Any journal</option></select><select data-s="year"><option value="">Any year</option></select>`}
      <label class="pmeta" style="display:inline-flex;gap:5px;align-items:center">cited by at least <select data-s="min">${[1, 2, 3, 4, 5].map(n => `<option${n === S.min ? " selected" : ""}>${n}</option>`).join("")}</select> of these papers</label>
      <button class="btn primary" data-k="find" id="dc-find">🔍 Find papers</button><button class="btn small" data-k="unhide">Hidden papers…</button><span class="pmeta" id="dc-st"></span></div>
    <div class="pmeta" style="margin-top:6px">Reads the reference lists of the papers in scope (OpenAlex; for papers it has no references for yet, the DOIs in the PDF’s own bibliography) and lists what several of them cite that you don’t have. Free; nothing is downloaded until you press Download.</div></div>
    <div id="dc-out"></div>${barHtml()}`;
  actions(S); S.redraw = () => draw(S); S.rerun = () => find(S);
  el.addEventListener("change", e => { const s = e.target.closest("[data-s]"); if (!s) return; if (s.dataset.s === "min") S.min = +s.value; else S.scope[s.dataset.s] = s.value; });
  el.addEventListener("click", e => { if (e.target.closest("#dc-find")) find(S); if (e.target.closest("[data-k=min1]")) { S.min = 1; const m = $('[data-s="min"]', el); if (m) m.value = "1"; find(S); } });
  if (!S.folder) fill(S);
  if (S.folder) find(S);
}
async function fill(S) {
  const L = ctx.getLib(), sel = k => $(`[data-s="${k}"]`, S.el), opt = (v, t) => `<option value="${esc(v)}">${esc(t)}</option>`;
  const js = [...new Map(L.map(r => [r.abbr, r.journal])).entries()].sort((a, b) => a[1].localeCompare(b[1])), ys = [...new Set(L.map(r => r.year).filter(Boolean))].sort().reverse();
  let folders = []; try { folders = await api("/api/folders"); } catch (e) { /* optional */ }
  sel("folder").insertAdjacentHTML("beforeend", folders.map(f => opt(f.id, "📁 " + f.name)).join(""));
  sel("journal").insertAdjacentHTML("beforeend", js.map(([a, n]) => opt(a, n)).join("")); sel("year").insertAdjacentHTML("beforeend", ys.map(y => opt(y, y)).join(""));
}
async function find(S) {
  if (S.busy) return; S.busy = true; const st = $("#dc-st", S.el), btn = $("#dc-find", S.el); btn.disabled = true; $("#dc-out", S.el).innerHTML = "";
  try {
    const r = await fetch("/api/related/scope", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({scope: {folder: S.folder || S.scope.folder || "", journal: S.scope.journal || "", year: S.scope.year || ""}, min: S.min})});
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
    const rd = r.body.getReader(), dec = new TextDecoder(); let buf = "";
    for (;;) {
      const {value, done} = await rd.read(); if (done) break; buf += dec.decode(value, {stream: true}); let i;
      while ((i = buf.indexOf("\n")) >= 0) { const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1); if (!line) continue; const m = JSON.parse(line);
        if (m.type === "status") st.textContent = m.msg; else if (m.type === "error") throw new Error(m.error);
        else if (m.type === "result") { Object.assign(S, {missing: m.missing, related: m.related, similar: m.similar, stats: m.stats, minUsed: m.min_n, hidden: 0}); } }
    }
    st.textContent = "";
  } catch (e) { st.textContent = ""; $("#dc-out", S.el).innerHTML = errBox(e, {retry: 'data-retry="#dc-find"'}); }
  S.busy = false; btn.disabled = false; if (S.missing) draw(S);
}
function draw(S) {
  const s = S.stats, out = $("#dc-out", S.el);
  const note = s ? `${s.papers} paper${s.papers === 1 ? "" : "s"} read · ${s.with_refs} with reference lists${s.via_pdf ? ` (${s.via_pdf} from the PDF’s own bibliography)` : ""}${s.no_refs.length ? ` · no reference list for: ${s.no_refs.slice(0, 4).map(t => esc(t.slice(0, 40))).join("; ")}${s.no_refs.length > 4 ? "…" : ""}` : ""}${S.hidden ? ` · ${S.hidden} hidden just now` : ""}` : "";
  const none = `<div class="pmeta">Nothing is cited by ${S.minUsed} or more of these papers and missing from your library.${S.minUsed > 1 ? ` <button class="linkbtn" data-k="min1">Show what any of them cites</button>, or add more papers on this topic.` : ""}</div>`;
  out.innerHTML = frame({tool: "discover", kind: "Discover", icon: "discover", key: "discover:" + (S.folder || "library"), title: `${S.missing.length} paper${S.missing.length === 1 ? "" : "s"} you may want`, sub: note, jump: true,
    sections: [
      {id: "missing", title: `Cited by ${S.minUsed >= 2 ? "at least " + S.minUsed + " of" : "any of"} these papers, not in your library`, short: "Missing from your library", icon: "download", tone: "lead", count: S.missing.length, body: S.missing.map(c => candRow(c, "missing")).join("") || none},
      S.related.length ? {id: "related", title: "OpenAlex suggests these are related", short: "Suggested", icon: "discover", count: S.related.length, body: `<div class="pmeta">OpenAlex’s suggestions are noisy, so only those whose title is close to the paper that points at them are shown. The number is how many of your papers point at it.</div>` + S.related.map(c => candRow(c, "related")).join("")} : null,
      S.similar && S.similar.length ? {id: "similar", title: "Already in your library, close in meaning", short: "Already yours", icon: "library", count: S.similar.length, collapsed: true, body: S.similar.map(simRow).join("")} : null]});
  wireOut(out);
  S.bar();
}

// ---------------------------------------------------------------- the reader's Related tab
export function mountPaper(el) {
  const S = {el, missing: [], related: [], cited: [], label: "related papers", file: null};
  el.innerHTML = `<div class="rd-scroll dc-paper" id="dcp-body"><div class="rd-empty">Open this tab to see related papers.</div></div><div class="dc-bar hide"><b><span class="dc-n">0</span> selected</b> <button class="rd-btn" data-k="download">⬇ Download</button></div>`;
  actions(S); S.redraw = () => paperDraw(S); wireOut($("#dcp-body", el));
  const load = async () => {
    const f = RS.file; if (!f || S.file === f) return; S.file = f; const body = $("#dcp-body", el);
    body.innerHTML = `<div class="rd-empty"><span class="ask-status">Asking OpenAlex and comparing with your library…</span></div>`;
    try { const r = await api("/api/related/paper?file=" + encodeURIComponent(f)); if (RS.file !== f) return; Object.assign(S, {related: r.related, cited: r.cited, similar: r.similar, stats: r.stats}); paperDraw(S); }
    catch (e) { body.innerHTML = `<div class="rd-empty">${errBox(e)}</div>`; S.file = null; }
  };
  on("tab:show", k => { if (k === "related") load(); });
  on("paper:opened", () => { S.file = null; S.missing = S.related = S.cited = []; $("#dcp-body", el).innerHTML = `<div class="rd-empty">Open this tab to see related papers.</div>`; if (RS.tab === "related") load(); });
}
function paperDraw(S) {
  const b = $("#dcp-body", S.el), st = S.stats || {};
  b.innerHTML = frame({tool: "related", kind: "Related papers", icon: "related", key: "related:" + S.file, sections: [
    {id: "similar", title: "Already in your library, close in meaning", short: "In your library", icon: "library", count: (S.similar || []).length, body: (S.similar || []).map(simRow).join("") || `<div class="pmeta">Nothing close yet.</div>`},
    {id: "cited", title: "Cited by this paper, not in your library", short: "It cites", icon: "link", count: (S.cited || []).length, tone: "lead", body: (st.refs ? "" : `<div class="pmeta">OpenAlex has no reference list for this paper yet, and the PDF’s bibliography has no DOIs.</div>`) + (S.cited || []).map(c => candRow(c, "cited", true)).join("") || `<div class="pmeta">Nothing missing.</div>`},
    {id: "related", title: "Suggested by OpenAlex (close in title), not in your library", short: "Suggested", icon: "discover", count: (S.related || []).length, body: (S.related || []).map(c => candRow(c, "related", true)).join("") || `<div class="pmeta">${st.openalex ? "Nothing new." : "OpenAlex doesn’t know this paper (yet), or can’t be reached."}</div>`}]});
  S.bar();
}

window.DiscoverUI = {init(c) { ctx = c; }, mount, mountPaper};
