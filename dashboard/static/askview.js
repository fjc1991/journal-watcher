// One question and its answer, in the shared output frame (output.js): the question as a strip, the answer split into
// "Short answer" / "Evidence" / "Not in the passages" (or "By paper", "Agreement and disagreement") sections, the sources as a
// collapsed section, follow-up questions as chips, and a second model's answer (compare) as its own frame. Used by the paper's Ask tab
// and by the folder / library Ask, so both look and behave the same.
import {esc, ico, errBox} from "./state.js";
import {answerHtml} from "./cite.js";
import {frame, mdParts, callout} from "./output.js";

export function answerParts(text, passages) {
  const parts = mdParts(text, {render: t => answerHtml(t, passages), leadTitle: "Short answer"});
  if (parts.length === 1 && parts[0].id === "lead") Object.assign(parts[0], {title: "Answer", short: "Answer"});     // no headings from the model: the whole text is the answer
  return parts;
}
const chips = t => [t.model ? {t: t.model} : null, t.cached ? {t: "cached"} : t.cost != null ? {t: "$" + t.cost.toFixed(4)} : null, t.label ? {t: t.label.replace(/\s*·\s*$/, "")} : null];
const btn = (k, v, icon, title, text = "") => `<button class="rd-btn" data-${k}="${v}" title="${esc(title)}" aria-label="${esc(title)}">${icon ? ico(icon, "sm") : ""}${text}</button>`;

// o = {key, sources: {count, note, html} | null, actions: html, notices: html}
function answerFrame(t, o, cls = "") {
  if (t.error) return errBox(t.error, {retry: cls === "alt" ? "" : 'data-t="regen"'});
  const live = !t.done, parts = t.text ? answerParts(t.text, t.passages) : [];
  if (!parts.length) parts.push({id: "lead", title: "Answer", short: "Answer", icon: "sparkle", tone: "lead", body: `<span class="ask-status">${esc(t.status || "Thinking…")}</span>`});
  if (o.sources && !live) parts.push({id: "sources", title: "Sources", icon: "link", count: o.sources.count, note: o.sources.note || "", collapsed: true, body: o.sources.html});
  const invalid = t.invalid && t.invalid.length ? callout("warn", `Citation${t.invalid.length > 1 ? "s" : ""} ${t.invalid.join(", ")} referred to passages the model was never given — don’t rely on ${t.invalid.length > 1 ? "them" : "it"}.`) : "";
  return frame({tool: "ask", kind: cls === "alt" ? "Second opinion" : "Answer", icon: "ask", key: o.key, cls, meta: chips(t), sections: parts, jump: !live, actions: live ? "" : o.actions || "", notices: (o.notices || "") + invalid});
}

export function turnHtml(t, i, o) {
  const key = (o.key || "ask") + ":" + i;
  const q = `<div class="ask-q ask-qs" title="Your question"><span class="ask-qi">${ico("ask", "sm")}</span><span>${esc(t.q)}</span></div>`;
  const follow = t.follow && t.follow.length ? `<div class="ask-fu"><div class="eyebrow">Ask next</div>${t.follow.map(x => `<button class="rd-btn" data-starter="${esc(x)}">${esc(x)}</button>`).join("")}</div>` : "";
  const alt = t.alt ? `<div class="ask-alt"><div class="ask-a">${answerFrame(t.alt, {key: key + ":alt", sources: null, actions: (t.alt.done ? btn("t", "usealt", "check", "Use this answer instead of the first one", " Use this") : "") + btn("t", "dropalt", "x", "Drop this answer")}, "alt")}</div></div>` : "";
  const first = answerFrame(t, {key, sources: o.sources, notices: o.notices, actions: o.actions});
  return `<div class="ask-turn" data-i="${i}">${q}<div class="ask-a">${first}</div>${follow}${alt}</div>`;
}
export const actionButtons = (extra = "") => btn("t", "copy", "copy", "Copy this answer") + btn("t", "save", "notes", "Put this answer on your notes canvas", " Notes") + btn("t", "regen", "redo", "Ask again, ignoring the saved answer") + extra;
export const compareButtons = () => btn("t", "compare", "related", "Ask a second model the same question") + btn("t", "follow", "sparkle", "Suggest follow-up questions");
