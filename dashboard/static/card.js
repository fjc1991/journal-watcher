// "Card" tab: a structured, cited summary of the open paper — question, topics, theory, constructs, hypotheses, method, findings, contribution,
// limitations, future research. Every statement shows the pages it rests on (click to jump there). Click any text to edit it; your edits are saved
// next to the paper (<name>.card.json) and marked. The same cards feed the folder's Evidence view and the Research Ideas Lab.
// The server streams newline-delimited JSON from POST /api/ai/card: {type:"status"|"progress"|"card"|"done"|"error"}.
import {errBox, notifyError, S, $, esc, ico, toast, on, debounce, api, progress, aboutCost} from "./state.js";
import * as N from "./notes.js";
import * as C from "./canvas.js";
import {jumpCite} from "./cite.js";
import {frame, kv, callout, pageChips, chipList, wire} from "./output.js";

let panel, body, card = null, est = null, busy = false, loadedFor = null;
const VERDICT = {"supported": "ok", "partly supported": "mid", "not supported": "bad", "not tested": "na", "unclear": "na"};
const KINDS = ["empirical (quantitative)", "empirical (qualitative)", "mixed methods", "design science", "conceptual / theory", "literature review", "other"];
const ROLES = ["uses", "extends", "tests", "challenges", "develops"];
const VERDICTS = Object.keys(VERDICT);
const money = aboutCost;

export function mount(el) {
  panel = el;
  el.innerHTML = `<div class="rd-sub" id="cd-bar"></div><div class="rd-scroll cd" id="cd-body"></div>`;
  body = $("#cd-body", el); wire(body);
  el.addEventListener("click", onClick); el.addEventListener("change", onChange);
  body.addEventListener("focusout", e => { if (e.target.matches(".cd-ed")) commitEdit(e.target); });
  body.addEventListener("keydown", e => { if (e.target.matches(".cd-ed")) { if (e.key === "Escape") { e.target.dataset.cancel = "1"; e.target.blur(); } else if (e.key === "Enter" && (e.ctrlKey || e.metaKey || !e.shiftKey)) { e.preventDefault(); e.target.blur(); } } });
  on("paper:opened", () => { card = null; est = null; loadedFor = null; body.innerHTML = ""; bar(); $("#rd-n-card", document).textContent = S.meta && S.meta.card ? " ✓" : ""; });   // loaded when the tab is shown
  on("reader:closed", () => { card = null; loadedFor = null; });
  on("tab:show", k => { if (k === "card" && loadedFor !== S.file) load(); });
}

async function load() {
  if (!S.file) return;
  const f = S.file; loadedFor = f;
  try {
    const r = await api("/api/doc/card?file=" + encodeURIComponent(f));
    if (S.file !== f) return;
    card = r.card; est = r.estimate || null;
  } catch (e) { if (e.status === 501) { body.innerHTML = `<div class="rd-empty">AI features aren’t set up, so paper cards can’t be made.</div>`; return; } notifyError(e); }
  draw();
}

function bar() { $("#cd-bar", panel).classList.add("hide"); }      // (Copy, → Notes and ⟳ are in the output's header)

function draw() {
  bar();
  const n = $("#rd-n-card", document); if (n) n.textContent = card ? " ✓" : "";
  if (!card) {
    body.innerHTML = `<div class="rd-empty cd-start" data-tool="card"><span class="cd-bigico">${ico("card")}</span><b>Make a card for this paper</b><br>A short, structured summary: the question, theory, constructs, hypotheses (and whether they held), method, findings, contribution, limitations and future research.
      Every line cites the pages it comes from, and you can edit anything.<br><br><button class="btn primary" data-k="make">Make the card</button>
      <div class="cd-est">${est && est.model ? `${esc(est.provider)} · ${esc(est.model)}${est.cost != null ? " — " + money(est.cost) : ""}` : ""}</div></div>`;
    return;
  }
  const d = card.data, P = card.passages || {}, m = d.method, kind = d.kind || "", empirical = /empirical|mixed|design science/.test(kind);
  const cites = (cs, p) => pageChips(cs, P) || (p ? `<span class="cd-nosrc" title="No passage supports this line: check it in the paper, or delete it">no source</span>` : "");
  const ed = (path, text, cls = "") => `<span class="cd-t ${cls}" data-path="${path}" title="Click to edit">${esc(text) || `<i class="cd-none">—</i>`}</span>`;
  const mark = it => it.edited ? `<span class="cd-edmark" title="Edited by you">✎</span>` : "";
  const del = path => `<button class="cd-x" data-del="${path}" title="Remove">✕</button>`;
  const items = (key, fmt, addable, empty = "Nothing found in the paper.") => {
    const xs = d[key] || [];
    return (xs.map((x, i) => `<div class="cd-item">${fmt(x, `${key}.${i}`)}${cites(x.cites, true)}${mark(x)}${del(key + "." + i)}</div>`).join("") || `<div class="cd-none">${empty}</div>`) + (addable ? `<button class="cd-add" data-add="${key}">＋ add</button>` : "");
  };
  const sel = (path, opts, cur) => `<select class="cd-sel" data-sel="${path}">${opts.map(o => `<option${o === cur ? " selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
  const sub = t => `<div class="cd-subh">${t}</div>`;
  const nTheory = d.theories.length + d.constructs.length, methodRows = ["design", "data", "sample", "analysis"].filter(k => empirical || m[k]);
  const contribN = (d.contribution.text ? 1 : 0) + d.implications.length;
  const sections = [
    {id: "question", short: "Question", title: "Research question", icon: "ask", tone: "lead", body: `<div class="cd-item">${ed("question.text", d.question.text, "big")}${cites(d.question.cites, !!d.question.text)}${mark(d.question)}</div>`},
    {id: "topics", title: "Topics", icon: "tags", count: d.topics.length, body: chipList(d.topics.map((t, i) => `<span class="cd-chip">${esc(t)}<button data-del="topics.${i}" title="Remove">✕</button></span>`).concat(`<button class="cd-add" data-add="topics">＋</button>`))},
    nTheory || empirical ? {id: "theory", short: "Theory", title: "Theory & constructs", icon: "ideas", count: nTheory, body:
      (d.theories.length ? sub("Theories") + items("theories", (x, p) => `<b>${ed(p + ".name", x.name)}</b> ${sel(p + ".role", ROLES, x.role)} ${x.note ? ed(p + ".note", x.note) : ""}`, false) : "") +
      (d.constructs.length ? sub("Constructs") + items("constructs", (x, p) => `<b>${ed(p + ".name", x.name)}</b> — ${ed(p + ".definition", x.definition)}`, false) : "") || `<div class="cd-none">No theory or constructs named in the paper.</div>`} : null,
    d.hypotheses.length ? {id: "hyp", short: "Hypotheses", title: "Hypotheses", icon: "check", count: d.hypotheses.length, body: items("hypotheses", (x, p) =>
      `<b>${ed(p + ".label", x.label)}</b> ${ed(p + ".text", x.text)} <span class="cd-v ${VERDICT[x.verdict] || "na"}">${sel(p + ".verdict", VERDICTS, x.verdict)}</span>`, false)} : null,
    methodRows.length ? {id: "method", short: empirical ? "Method" : "Approach", title: empirical ? "Method" : "Approach", icon: "matrix", count: "", body: kv(methodRows.map(k => [k, ed("method." + k, m[k])])) + `<div class="cd-item">${cites(m.cites, false)}${mark(m)}</div>`} : null,
    {id: "findings", short: empirical ? "Findings" : "Main points", title: empirical ? "Findings" : "Main points", icon: "evidence", count: d.findings.length, collapsed: !d.findings.length, body: items("findings", (x, p) => ed(p + ".text", x.text), true)},
    {id: "contribution", short: "Contribution", title: "Contribution & implications", icon: "bolt", count: contribN, body: `<div class="cd-item">${ed("contribution.text", d.contribution.text)}${cites(d.contribution.cites, !!d.contribution.text)}${mark(d.contribution)}</div>` + (d.implications.length ? sub("Implications") : "") + items("implications", (x, p) => ed(p + ".text", x.text), true, "")},
    {id: "limits", short: "Limits", title: "Limitations", icon: "warn", count: d.limitations.length, collapsed: !d.limitations.length, tone: d.limitations.length ? "warn" : "", body: items("limitations", (x, p) => `${x.stated === false ? `<span class="of-badge inferred cd-tag" title="You or the model noticed this; the authors do not say it">inferred</span> ` : ""}${ed(p + ".text", x.text)}`, true)},
    {id: "future", short: "Future", title: "Future research suggested", icon: "discover", count: d.future_research.length, collapsed: !d.future_research.length, body: items("future_research", (x, p) => ed(p + ".text", x.text), true)},
  ].filter(Boolean);
  const notices = [card.stale && callout("warn", "The PDF was replaced after this card was written: it may no longer match."), card.truncated && callout("warn", "This paper is very long: the card was written from its beginning and end."),
    card.invalid && callout("warn", `${card.invalid} citation${card.invalid > 1 ? "s" : ""} pointed at passages that don’t exist and ${card.invalid > 1 ? "were" : "was"} removed.`)].filter(Boolean).join("");
  body.innerHTML = frame({tool: "card", kind: "Paper card", icon: "card", key: "card:" + S.file, title: S.meta && S.meta.title || "", sub: `<label class="cd-type">Type ${sel("kind", KINDS, d.kind)}</label>`,
    meta: [{t: card.model || "model unknown"}, card.edited && {t: "✎ edited", cls: "edited", title: "You changed something"}, card.generated && {t: "written " + card.generated.slice(0, 10)}, card.cost != null && {t: money(card.cost)}],
    actions: `<button class="rd-btn" data-k="copy" title="Copy the card as Markdown">${ico("copy", "sm")} Copy</button><button class="rd-btn" data-k="listen" title="A two-minute spoken briefing of this paper, read aloud by your browser">🎧 Listen</button><button class="rd-btn" data-k="notes" title="Put the whole card on your notes canvas">→ Notes</button><button class="rd-btn" data-k="regen" title="Write the card again (the old one is kept in state/card_backups)">${ico("redo", "sm")}</button>`,
    notices, sections});
}

// ---------------------------------------------------------------- reading and editing
const walk = path => path.split(".");
const getAt = path => walk(path).reduce((o, k) => o && o[k], card.data);
function setAt(path, v) {
  const ks = walk(path), last = ks.pop(), parent = ks.reduce((o, k) => o[k], card.data);
  parent[last] = v;
  if (parent && !Array.isArray(parent) && typeof parent === "object") parent.edited = true;
}
function startEdit(span) {
  const path = span.dataset.path, v = getAt(path) || "", ta = document.createElement("textarea");
  ta.className = "cd-ed"; ta.value = v; ta.dataset.path = path; ta.rows = Math.max(1, Math.min(8, Math.ceil(v.length / 46)));
  span.replaceWith(ta); ta.focus(); ta.setSelectionRange(v.length, v.length);
  ta.addEventListener("input", () => { ta.style.height = "auto"; ta.style.height = ta.scrollHeight + "px"; }); ta.style.height = ta.scrollHeight + "px";
}
function commitEdit(ta) {
  const path = ta.dataset.path, was = getAt(path) || "", v = ta.value.trim();
  if (ta.dataset.cancel || v === was) return draw();
  setAt(path, v); draw(); save();
}
const save = debounce(async () => {
  try { const r = await api("/api/doc/card", {file: S.file, card, rev: card.rev}); card = r.card; draw(); }
  catch (e) { if (e.status === 409 && e.body && e.body.current) { card = e.body.current; draw(); toast("This card changed in another window; showing the newest version."); } else notifyError(e); }
}, 500);

function onChange(e) {
  const s = e.target.closest("[data-sel]"); if (!s || !card) return;
  setAt(s.dataset.sel, s.value); draw(); save();
}
async function onClick(e) {
  const t = e.target, k = t.closest("[data-k]")?.dataset.k;
  if (k === "make") return make(false);
  if (k === "regen") { if (card.edited && !confirm("This card has edits of yours. Writing it again replaces them (the old card is kept in state/card_backups).\n\nContinue?")) return; return make(true); }
  if (k === "listen") return void (window.Listen && window.Listen.open({title: "Briefing: " + (S.meta.title || "this paper").slice(0, 70), body: {file: S.file}}));
  if (k === "copy") { try { const r = await fetch("/api/export/cards?file=" + encodeURIComponent(S.file)); S.fn.copyText(await r.text(), "Card copied as Markdown."); } catch (er) { notifyError(er); } return; }
  if (k === "notes" && C) { try { const r = await fetch("/api/export/cards?file=" + encodeURIComponent(S.file)), p = C.nextPos(); N.addCard({type: "ai", question: "Paper card", text: await r.text(), cites: [], model: card.model, x: p.x, y: p.y, w: 360}); S.fn.showTab("notes"); toast("Saved to your notes."); } catch (er) { notifyError(er); } return; }
  const c = t.closest(".cd-cite"); if (c) { const p = card.passages[c.dataset.n]; if (p) jumpCite(p); return; }
  const tx = t.closest(".cd-t"); if (tx) return startEdit(tx);
  const d = t.closest("[data-del]");
  if (d) { const ks = walk(d.dataset.del), i = +ks.pop(), arr = ks.reduce((o, kk) => o[kk], card.data); arr.splice(i, 1); draw(); save(); return; }
  const a = t.closest("[data-add]");
  if (a) {
    const key = a.dataset.add;
    if (key === "topics") { const v = prompt("Topic:"); if (v && v.trim()) { card.data.topics.push(v.trim()); draw(); save(); } return; }
    card.data[key].push(key === "limitations" ? {text: "", cites: [], stated: true, edited: true} : {text: "", cites: [], edited: true}); draw();
    const items = $$all(`[data-path^="${key}."]`); const last = items[items.length - 1]; if (last) startEdit(last);
  }
}
const $$all = s => [...panel.querySelectorAll(s)];

// ---------------------------------------------------------------- making the card
async function make(fresh) {
  if (busy) return; busy = true;
  body.innerHTML = `<div class="rd-empty"><span class="ask-status" id="cd-st">Reading the paper…</span></div>`;
  let got = null, done = null;
  try {
    const r = await fetch("/api/ai/card", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({file: S.file, fresh})});
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
    const rd = r.body.getReader(), dec = new TextDecoder(); let buf = "";
    for (;;) {
      const {value, done: fin} = await rd.read(); if (fin) break; buf += dec.decode(value, {stream: true}); let i;
      while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1); if (!line) continue; const m = JSON.parse(line), st = $("#cd-st", panel);
        if (m.type === "status" && st) progress(st, m);
        else if (m.type === "progress" && st) st.textContent = `Writing the card… ${(m.chars / 1000).toFixed(1)}k characters`;
        else if (m.type === "card") got = m.card; else if (m.type === "done") done = m; else if (m.type === "error") throw new Error(m.error);
      }
    }
    card = got || card; loadedFor = S.file;
    if (done && !done.existing) toast("Card written" + (done.cost != null ? " · " + money(done.cost) : "") + ".");
  } catch (e) {
    busy = false;
    if (card) { notifyError(e); return draw(); }            // a failed rewrite leaves the card you had
    body.innerHTML = `<div class="rd-empty">${errBox(e, {retry: 'data-k="make"'})}</div>`; return;
  }
  busy = false; if (S.meta) S.meta.card = true; draw();
}
