// Citations in AI answers: "(3)" or "(3,7)" refer to numbered passages {n, page, quote}; each becomes a button that
// jumps to the passage in the paper and flashes it (the same trick as an excerpt's ⤴ link).
import {S, md, toast} from "./state.js";
import * as T from "./text.js";

export function answerHtml(text, cites) {
  const byN = new Map((cites || []).map(c => [c.n, c]));
  return md(text).replace(/\((\d+(?:\s*,\s*\d+)*)\)/g, (m, g) => {
    const ns = g.split(",").map(s => +s.trim());
    return ns.every(n => byN.has(n)) ? ns.map(n => `<button class="cite" data-n="${n}" title="Page ${byN.get(n).page}: ${byN.get(n).quote.slice(0, 80).replace(/"/g, "&quot;")}…">${n}</button>`).join("") : m;
  });
}

export async function jumpCite(c) {
  if (!S.pages.length) return;
  let rs = null;
  try { rs = await T.quoteRects(c.page, c.quote); } catch (e) {}
  if (rs && rs.length) S.fn.jump(c.page, Math.max(0, rs[0][1] - 0.05), {flash: rs});
  else { S.fn.jump(c.page, 0); toast("Page " + c.page + " — couldn’t pin down the exact passage."); }
}

// one click handler for a container that holds answers; cites = function(card/answer element) -> cite list
export function wireCites(el, getCites) {
  el.addEventListener("click", e => {
    const b = e.target.closest(".cite"); if (!b) return;
    const c = (getCites(b) || []).find(x => x.n === +b.dataset.n); if (c) jumpCite(c);
  });
}
