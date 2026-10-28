// The command palette: Ctrl/Cmd+K from anywhere (the top-bar search pill does the same). Type to jump to a paper, a folder (or one of its tabs), a page,
// a Settings tab or a quick action; ↑ ↓ choose, ↵ open, Alt+↵ opens a paper at its Card, Esc closes. Papers match by title, author, journal and year as you type;
// after a short pause the library's full-text search adds papers that mention your words in their summary, highlights or notes. With nothing typed it offers
// where you can go, the papers you are reading and your folders. It only navigates: nothing here starts an AI run or changes your library.
import {$, $$, esc, api, ico, debounce} from "./state.js";

let ctx = null, root = null, items = [], mentions = [], q = "", sel = 0, seq = 0, prevFocus = null, built = [];
const MAC = /Mac|iPhone|iPad/.test(navigator.platform), MOD = MAC ? "⌘" : "Ctrl";
const GROUPS = [["go", "Go to"], ["folder", "Folders"], ["paper", "Papers"], ["text", "Mentioned in your papers, highlights and notes"], ["act", "Actions"]];
const TABS = {papers: "Papers", ask: "Ask", matrix: "Matrix", discover: "Discover", evidence: "Evidence", lab: "Ideas Lab", draft: "Related work", claims: "Claim check", synthesis: "Synthesis", workspace: "Workspace"}, TICON = {papers: "papers", ask: "ask", matrix: "matrix", discover: "discover", evidence: "evidence", lab: "ideas", draft: "draft", claims: "claims", synthesis: "synthesis", workspace: "workspace"};

// ---------------------------------------------------------------- doing things (the reader and Settings sit on top of the pages, so close them first)
async function closeReader() {
  if (!(window.Reader && window.Reader.isOpen())) return;
  const gone = new Promise(r => { addEventListener("popstate", r, {once: true}); setTimeout(r, 450); });
  await window.Reader.close(); await gone;
}
async function nav(n) { if (window.Settings) window.Settings.close(); await closeReader(); ctx.go(n); }
const readPaper = (row, opts) => { if (window.Settings) window.Settings.close(); ctx.readPaper(row, opts); };

// ---------------------------------------------------------------- what can be found
function build() {
  const L = ctx.getLib(), F = (window.LibraryUI && window.LibraryUI.folders && window.LibraryUI.folders()) || [], out = [];
  const add = (g, title, icon, tool, run, o = {}) => out.push({g, title, icon, tool, run, sub: o.sub || "", hint: o.hint || "", hay: (title + " " + (o.hay || "") + " " + (o.sub || "")).toLowerCase(), boost: o.boost || 0, card: o.card, quick: !!o.quick});
  add("go", "Watch", "watch", "watch", () => nav({view: "run", j: null}), {hint: "page", quick: true, hay: "check for new papers weekly archive search runs journals", sub: "check journals for new papers; your past runs"});
  add("go", "Library", "library", "library", () => nav({view: "lib", j: null, tab: "papers"}), {hint: "page", quick: true, hay: "papers search browse journals", sub: "every paper you have downloaded"});
  [["ask", "Ask the whole library", "ask"], ["discover", "Discover papers you don’t have", "discover"], ["hl", "Highlights hub", "hl"], ["review", "Review flashcards", "review"], ["tags", "Tags", "tags"]].forEach(([t, name, ic]) =>
    add("go", "Library › " + name, ic, t, () => nav({view: "lib", j: null, tab: t}), {hint: "tool", quick: t === "ask", hay: "library " + t}));
  add("go", "Folders", "folders", "folders", () => nav({view: "lib", j: null, tab: "folders"}), {hint: "page", quick: true, hay: "folder tiles projects", sub: "your folders of papers"});
  [["ai", "AI & privacy"], ["usage", "Usage & cost"], ["prompts", "Prompts"], ["reader", "Reader & summaries"], ["look", "Appearance"]].forEach(([t, name]) =>
    add("go", "Settings › " + name, "settings", "settings", async () => { await closeReader(); window.Settings && window.Settings.open(t); }, {hint: "settings", quick: t === "ai", hay: "settings preferences models keys budget cost " + t}));
  add("go", "About Journal Watcher", "heart", "about", async () => { await closeReader(); window.About && window.About.open(); }, {hint: "page", hay: "about author made by support coffee tip donate ko-fi licence version", sub: "who makes it, and how to support it"});
  F.forEach(f => {
    add("folder", f.name, "folders", "folders", () => nav({view: "lib", j: "@" + f.id, tab: "folders"}), {hint: "folder", sub: (f.count || 0) + " paper" + (f.count === 1 ? "" : "s"), boost: 2});
    Object.entries(TABS).forEach(([t, name]) => t !== "papers" && add("folder", f.name + " › " + name, TICON[t], t === "lab" ? "ideas" : t, () => nav({view: "lib", j: "@" + f.id, tab: "folders", ftab: t}), {hint: "folder tool", hay: f.name + " " + name}));
  });
  L.forEach(r => add("paper", r.title, "papers", "papers", () => readPaper(r), {sub: [(r.authors && (Array.isArray(r.authors) ? r.authors : String(r.authors).split(/;\s*/))[0]) || "", r.journal, r.year].filter(Boolean).join(" · "),
    hay: [Array.isArray(r.authors) ? r.authors.join(" ") : r.authors, r.journal, r.year, r.matched_topics].filter(Boolean).join(" "), hint: r.card ? "paper · card" : "paper", boost: (r.state || {}).status === "reading" ? 3 : 0, card: r}));
  add("act", "Switch between light and dark", "moon", "settings", () => $("#themebtn").click(), {hint: "action", hay: "theme dark light mode night"});
  add("act", "Look: Subtle or Bold…", "sparkle", "settings", async () => { await closeReader(); window.Settings && window.Settings.open("look"); }, {hint: "action", hay: "appearance colours subtle bold tint"});
  add("act", "Add a paper (PDF or DOI)", "add", "library", () => window.LibraryUI && window.LibraryUI.addDialog(), {hint: "action", hay: "import upload drop pdf doi new paper"});
  add("act", "Import a bibliography (.ris / .bib)", "add", "library", () => window.BibImport && window.BibImport.open(), {hint: "action", hay: "endnote mendeley zotero reference list references import bibtex ris overleaf"});
  add("act", "New folder", "plus", "folders", async () => { await nav({view: "lib", j: null, tab: "folders"}); setTimeout(() => { const b = $('[data-fp="new"]'); b && b.click(); }, 250); }, {hint: "action", hay: "create folder project"});
  return out;
}

// ---------------------------------------------------------------- matching
const words = s => s.toLowerCase().split(/[\s,;:]+/).filter(Boolean);
function score(it, ws) {
  if (!ws.length) return it.boost;
  const t = it.title.toLowerCase(); let s = it.boost;
  for (const w of ws) {
    if (!it.hay.includes(w)) return -1;
    const i = t.indexOf(w); s += i === 0 ? 30 : i > 0 ? (/\W/.test(t[i - 1]) ? 20 : 8) : 4;
  }
  return s - Math.min(it.title.length, 120) / 25;
}
function mark(text, ws) {
  if (!ws.length) return esc(text);
  const re = new RegExp("(" + ws.map(w => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|") + ")", "ig");
  return text.split(re).map((p, i) => i % 2 ? `<mark>${esc(p)}</mark>` : esc(p)).join("");
}
function results() {
  const ws = words(q); let flat = []; const order = ws.length ? GROUPS : [GROUPS[0], GROUPS[2], GROUPS[1], GROUPS[3], GROUPS[4]];
  const LIM = {go: ws.length ? 6 : 9, folder: ws.length ? 7 : 6, paper: ws.length ? 8 : 5, text: 5, act: ws.length ? 4 : 4};
  for (const [g] of order) {
    let xs = g === "text" ? mentions : built.filter(i => i.g === g);
    if (!ws.length && g === "go") xs = xs.filter(i => i.quick);
    if (!ws.length && g === "paper") xs = xs.filter(i => i.boost >= 3).sort((a, b) => ((b.card && b.card.state && b.card.state.opened) || "").localeCompare((a.card && a.card.state && a.card.state.opened) || ""));
    if (!ws.length && g === "folder") xs = xs.filter(i => i.hint === "folder");
    if (!ws.length && g === "text") continue;
    if (!ws.length && g === "act") xs = xs.slice(0, 4);
    if (ws.length && g !== "text") xs = xs.map(i => [i, score(i, ws)]).filter(([, s]) => s >= 0).sort((a, b) => b[1] - a[1]).map(([i]) => i);
    flat = flat.concat(xs.slice(0, LIM[g]));
  }
  return flat;
}

// ---------------------------------------------------------------- drawing
function draw() {
  items = results(); sel = Math.min(sel, Math.max(0, items.length - 1));
  const ws = words(q), list = $("#pal-list", root);
  if (!items.length) { list.innerHTML = `<div class="pal-none">Nothing matches “${esc(q)}”. Try fewer words, or a paper’s author or journal.</div>`; $("#pal-q", root).removeAttribute("aria-activedescendant"); return; }
  let h = "", last = "";
  items.forEach((it, i) => {
    if (it.g !== last) { last = it.g; h += `<div class="pal-g eyebrow">${esc((GROUPS.find(x => x[0] === it.g) || [0, it.g])[1])}</div>`; }
    h += `<div class="pal-it${i === sel ? " on" : ""}" role="option" id="pal-i${i}" data-i="${i}" data-tool="${it.tool}" aria-selected="${i === sel}"><span class="pal-ic">${ico(it.icon)}</span>
      <span class="pal-tx"><span class="pal-t">${mark(it.title, ws)}</span>${it.sub ? `<span class="pal-s">${it.g === "text" ? esc(it.sub) : mark(it.sub, ws)}</span>` : ""}</span><span class="pal-k">${esc(it.hint)}</span></div>`;
  });
  list.innerHTML = h; $("#pal-q", root).setAttribute("aria-activedescendant", "pal-i" + sel);
  const on = $(".pal-it.on", list); if (on) on.scrollIntoView({block: "nearest"});
}
function move(d) { if (!items.length) return; sel = (sel + d + items.length) % items.length; $$(".pal-it", root).forEach((e, i) => { e.classList.toggle("on", i === sel); e.setAttribute("aria-selected", String(i === sel)); });
  $("#pal-q", root).setAttribute("aria-activedescendant", "pal-i" + sel); $(".pal-it.on", root).scrollIntoView({block: "nearest"}); }

// papers that mention the words (the library's own full-text search: summaries, highlights, notes, cards)
const search = debounce(async () => {
  const my = ++seq, text = q.trim(); if (text.length < 3) { if (mentions.length) { mentions = []; draw(); } return; }
  try {
    const r = await api("/api/library/search?q=" + encodeURIComponent(text)), L = ctx.getLib(), have = new Set(items.filter(i => i.g === "paper").map(i => i.card && i.card.file));
    if (my !== seq || !root || root.classList.contains("hide")) return;
    const ws = words(text); mentions = r.map(x => { const row = L.find(p => p.file === x.file); const sn = (x.snippets || [])[0], seen = (((sn && sn.text) || "") + " " + (row ? row.title : "")).toLowerCase(); return row && !have.has(row.file) && ws.every(w => seen.includes(w)) ? {g: "text", title: row.title, icon: sn && sn.kind === "highlight" ? "hl" : "notes", tool: "hl", run: () => readPaper(row, sn && sn.page ? {goto: {page: sn.page}} : undefined),
      sub: sn ? (sn.kind === "highlight" ? "highlight" + (sn.page ? " p." + sn.page : "") + ": " : sn.kind + ": ") + (sn.text || "").slice(0, 110) : "", hint: "paper", hay: "", boost: 0, card: row} : null; }).filter(Boolean);
    draw();
  } catch (e) { /* the instant matches are enough */ }
}, 240);

// ---------------------------------------------------------------- open and close
export function open() {
  if (!ctx) return;
  if (!root) {
    root = document.createElement("div"); root.id = "pal"; root.className = "pal-bg hide";
    root.innerHTML = `<div class="pal card raised" role="dialog" aria-modal="true" aria-label="Jump to…">
      <div class="pal-in">${ico("search")}<input id="pal-q" type="text" placeholder="Jump to a paper, folder, tool or setting…" autocomplete="off" spellcheck="false" role="combobox" aria-expanded="true" aria-controls="pal-list" aria-autocomplete="list"><kbd>Esc</kbd></div>
      <div class="pal-list" id="pal-list" role="listbox" aria-label="Results"></div>
      <div class="pal-foot"><span><kbd>↑</kbd><kbd>↓</kbd> choose</span><span><kbd>↵</kbd> open</span><span class="pal-alt"><kbd>⌥↵</kbd> a paper at its card</span><span><kbd>Esc</kbd> close</span></div></div>`;
    document.body.append(root);
    root.addEventListener("mousedown", e => { if (e.target === root) close(); });
    root.addEventListener("mousemove", e => { const it = e.target.closest(".pal-it"); if (it && +it.dataset.i !== sel) { sel = +it.dataset.i; move(0); } });
    root.addEventListener("click", e => { const it = e.target.closest(".pal-it"); if (it) run(+it.dataset.i, e.altKey); });
    $("#pal-q", root).addEventListener("input", e => { q = e.target.value; sel = 0; mentions = []; draw(); search(); });
    $("#pal-q", root).addEventListener("keydown", e => {
      if (e.key === "ArrowDown") { e.preventDefault(); move(1); } else if (e.key === "ArrowUp") { e.preventDefault(); move(-1); }
      else if (e.key === "Enter") { e.preventDefault(); run(sel, e.altKey); } else if (e.key === "Escape") { e.preventDefault(); close(); }
      else if (e.key === "Tab") { e.preventDefault(); move(e.shiftKey ? -1 : 1); }
    });
  }
  prevFocus = document.activeElement; built = build(); q = ""; sel = 0; mentions = [];
  $("#pal-q", root).value = ""; root.classList.remove("hide"); document.body.classList.add("pal-open"); draw(); $("#pal-q", root).focus();
}
export function close() {
  if (!root || root.classList.contains("hide")) return;
  root.classList.add("hide"); document.body.classList.remove("pal-open"); seq++;
  try { prevFocus && prevFocus.isConnected && prevFocus.focus && prevFocus.focus(); } catch (e) { /* the page it was on is gone */ }
}
async function run(i, alt) {
  const it = items[i]; if (!it) return; close();
  try { if (alt && it.g !== "act" && it.card && it.card.file) readPaper(it.card, {tab: "card"}); else await it.run(); } catch (e) { console.error(e); }
}

// the keyboard: Ctrl/Cmd+K anywhere (also while typing in a box, and inside the reader)
window.addEventListener("keydown", e => {
  if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === "k") {
    e.preventDefault(); e.stopPropagation();
    root && !root.classList.contains("hide") ? close() : open();
  }
}, true);
document.addEventListener("click", e => { if (e.target.closest("#palettebtn")) open(); });

window.Palette = {init(c) { ctx = c; const b = $("#palettebtn"); if (b) { b.removeAttribute("disabled"); $(".pk", b).textContent = MOD + " K"; } }, open, close};
