// The ✨ menu: your one-click prompts (Settings → Prompts) on a selected passage or a boxed figure/table, with the answer
// streaming into a small card you can save to your notes. Answers are cached on the server, so repeating a prompt is free.
import {errBox, S, $, esc, toast, md, clamp, exactCost} from "./state.js";
import * as N from "./notes.js";
import * as C from "./canvas.js";
import * as AREA from "./area.js";
import {mdParts, sectionsHtml, wire as wireOut} from "./output.js";

let menu, card, run = 0, last = null;

export function init() {
  const {root} = S.ui;
  menu = document.createElement("div"); menu.className = "rd-float col hide rd-expmenu"; root.append(menu);
  card = document.createElement("div"); card.className = "rd-exp hide"; card.dataset.tool = "explain"; root.append(card); wireOut(card);
  menu.addEventListener("mousedown", e => e.preventDefault());
  menu.addEventListener("click", onMenu); card.addEventListener("click", onCard);
  document.addEventListener("mousedown", e => { if (!e.target.closest(".rd-expmenu,.rd-exp,#rd-sel,#rd-pop,.rd-areamenu")) menu.classList.add("hide"); });
  S.fn.explainMenu = openMenu;
  S.fn.explainFigure = (c, cap) => explain({area: {page: cap.page, rect: [0, 0, 1, 1]}, full: true, text: "", page: cap.page, label: c.label}, {id: null, name: "Explain " + c.label,
    custom: `The attached image is page ${cap.page} of the paper. Explain ${c.label} on this page (its caption begins: “${cap.text.replace(/\s+/g, " ").slice(0, 140)}”): what it shows, how to read it (axes, columns, units), the main takeaway, and any caveat visible in it. If it is a table, list the key numbers. Ignore the rest of the page.`});
}

function openMenu(anchor, target) {
  const all = (S.prompts || []).filter(p => target.area ? p.scope === "area" : p.scope === "selection");
  const pinned = all.filter(p => p.pinned), more = all.filter(p => !p.pinned);
  menu._target = target;
  menu.innerHTML = `<div class="pkind">${target.area ? "Ask about this figure or table" : "Ask about this passage"}</div>` +
    (pinned.concat(more).map(p => `<button class="rd-btn" data-id="${esc(p.id)}">${esc(p.name)}</button>`).join("") || `<div class="hint">No prompts yet — add some in Settings → Prompts.</div>`) +
    `<button class="rd-btn" data-id="__custom">✎ Write your own…</button>` +
    (target.area ? "" : `<button class="rd-btn" data-id="__chat">💬 Ask in the chat…</button>`) +
    `<button class="rd-btn" data-id="__settings" style="font-size:11px;opacity:.7">Edit prompts…</button>`;
  const r = anchor.getBoundingClientRect(); menu.classList.remove("hide");
  menu.style.left = clamp(r.left, 8, innerWidth - 270) + "px"; menu.style.top = clamp(r.bottom + 6, 70, innerHeight - menu.offsetHeight - 10) + "px";
  S.fn.hideFloats && S.fn.hideFloats();          // the selection bar / popover have done their job; the menu is now the thing to look at
}
async function onMenu(e) {
  const id = e.target.closest("[data-id]")?.dataset.id; if (!id) return;
  const t = menu._target; menu.classList.add("hide");
  if (id === "__settings") return void (window.Settings && window.Settings.open("prompts"));
  if (id === "__chat") { S.fn.showTab("ask"); return void (S.fn.askAbout && S.fn.askAbout(t.text)); }
  let spec = (S.prompts || []).find(p => p.id === id);
  if (id === "__custom") { const q = prompt("What should I do with this " + (t.area ? "figure or table" : "passage") + "?", "Explain this"); if (!q) return; spec = {id: null, name: q.slice(0, 50), custom: q}; }
  explain(t, spec);
}

// an answer that has headings becomes titled sections; a plain answer stays plain
const answerHtml = text => { const parts = mdParts(text); return parts.length > 1 || (parts[0] && parts[0].id !== "lead") ? sectionsHtml(parts, "explain:" + (last && last.spec.name)) : md(text); };
async function explain(t, spec, fresh = false) {
  const my = ++run; last = {t, spec};
  card.innerHTML = `<div class="ex-h"><b>${esc(spec.name)}</b><span class="ex-meta"></span><button data-x="close" title="Close">✕</button></div>
    <div class="ex-quote">${t.area ? "▭ " + (t.label || "figure / table") + ", p. " + t.area.page + (t.full ? " (the whole page is sent)" : "") : "“" + esc(t.text.slice(0, 160)) + (t.text.length > 160 ? "…" : "") + "”"}</div><div class="ex-body md"><span class="ask-status">Thinking…</span></div><div class="ex-act hide"></div>`;
  card.classList.remove("hide"); card.style.left = clamp(innerWidth / 2 - 190, 8, innerWidth - 400) + "px"; card.style.top = "110px";
  const body = $(".ex-body", card), meta = $(".ex-meta", card);
  let text = "", done = null;
  try {
    const image = t.area ? await AREA.areaPng(t.area.page, t.area.rect) : undefined;
    const r = await fetch("/api/ai/explain", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({
      file: S.file, text: t.text || "", page: t.page || (t.area && t.area.page) || 1, prompt_id: spec.id, custom: spec.custom, image, fresh})});
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
    const rd = r.body.getReader(), dec = new TextDecoder(); let buf = "";
    for (;;) {
      const {value, done: fin} = await rd.read(); if (fin) break; if (my !== run) { rd.cancel(); return; }
      buf += dec.decode(value, {stream: true}); let i;
      while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1); if (!line) continue;
        const m = JSON.parse(line);
        if (m.type === "text") { text += m.delta; body.innerHTML = answerHtml(text); }
        else if (m.type === "error") throw new Error(m.error);
        else if (m.type === "done") done = m;
      }
    }
  } catch (e) { if (my === run) body.innerHTML = errBox(e, {retry: 'data-x="again"'}); return; }
  if (my !== run) return;
  meta.textContent = done ? `${done.provider} · ${done.model}${done.cached ? " · cached" : done.cost != null ? " · " + exactCost(done.cost) : ""}` : "";
  const act = $(".ex-act", card); act.classList.remove("hide");
  act.innerHTML = `<button class="rd-btn" data-x="save">→ Save to notes</button><button class="rd-btn" data-x="copy">Copy</button><button class="rd-btn" data-x="again" title="Ask again, ignoring the saved answer">⟳</button>` + (t.area ? "" : `<button class="rd-btn" data-x="chat">Follow up in chat</button>`);
  if (done && done.cut_off) body.insertAdjacentHTML("beforeend", `<div class="of-co warn"><div>This answer was cut off at the model’s length limit; the end is missing. Ask again, or ask for something shorter. <button class="rd-btn" data-x="again">Ask again</button></div></div>`);
  card._text = text; card._done = done;
}
function onCard(e) {
  const x = e.target.closest("[data-x]")?.dataset.x; if (!x) return;
  if (x === "close") { run++; card.classList.add("hide"); }
  else if (x === "copy") S.fn.copyText(card._text, "Copied.");
  else if (x === "again" && last) explain(last.t, last.spec, true);
  else if (x === "chat" && last) { card.classList.add("hide"); S.fn.showTab("ask"); S.fn.askAbout && S.fn.askAbout(last.t.text); }
  else if (x === "save" && last) {
    const t = last.t, p = C.nextPos(), pg = t.page || (t.area && t.area.page);
    N.addCard({type: "ai", question: last.spec.name + (t.text ? ": “" + t.text.slice(0, 90) + (t.text.length > 90 ? "…" : "") + "”" : " (figure / table, p. " + pg + ")"), text: card._text, cites: [], model: card._done ? card._done.model : "",
      src: t.area && !t.full ? {hl: null, page: pg, rect: t.area.rect, parts: [{page: pg, rects: [t.area.rect]}]} : undefined, x: p.x, y: p.y, w: 340});
    S.fn.showTab("notes"); toast("Saved to your notes.");
  }
}
