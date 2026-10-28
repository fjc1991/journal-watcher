// "⧉ Beside": read two papers side by side. Opens a second window (half the screen, on the right) with another paper from your library; the second window is the whole app, so it has its own notes,
// highlights and Ask. Pick the paper from a short list. The current paper can be chosen too (to read two places of one paper at once; if you edit its notes in both windows, the second save asks which copy to keep).
import {$, esc, api, toast, notifyError} from "./state.js";

let dlg = null;
export async function open({file, page}) {
  if (dlg) dlg.remove();
  dlg = document.createElement("dialog"); dlg.className = "card adddlg twindlg";
  dlg.innerHTML = `<h2>Open beside this paper</h2><p class="pmeta">A second window opens on the right half of the screen with the paper you choose.</p><input type="search" id="tw-q" placeholder="Search your library…" autocomplete="off"><div class="tw-list" id="tw-list">Loading…</div>
    <div class="pmeta">Tip: on a Mac, hold the green button on this window and choose <i>Tile Window to Left of Screen</i>; on Windows press Win + ←.</div><div class="row" style="justify-content:flex-end"><button class="btn" id="tw-close">Close</button></div>`;
  document.body.append(dlg); dlg.showModal();
  $("#tw-close", dlg).onclick = () => dlg.close(); dlg.addEventListener("close", () => dlg && dlg.remove());
  let lib = [];
  try { lib = await api("/api/library"); } catch (e) { return notifyError(e); }
  lib = lib.slice().sort((a, b) => ((b.state || {}).opened || "").localeCompare((a.state || {}).opened || ""));
  const draw = () => {
    const ws = $("#tw-q", dlg).value.toLowerCase().split(/\s+/).filter(Boolean), rows = lib.filter(r => ws.every(w => ((r.title || "") + " " + (Array.isArray(r.authors) ? r.authors.join(" ") : r.authors || "") + " " + (r.journal || "")).toLowerCase().includes(w))).slice(0, 60);
    $("#tw-list", dlg).innerHTML = rows.map(r => `<button class="tw-r${r.file === file ? " cur" : ""}" data-f="${esc(r.file)}"><b>${esc(r.title)}</b><br><small>${esc(r.journal || "")}${r.year ? " · " + esc(r.year) : ""}${r.file === file ? " · this paper" : ""}</small></button>`).join("") || `<div class="pmeta">Nothing matches.</div>`;
  };
  draw(); $("#tw-q", dlg).oninput = draw; $("#tw-q", dlg).focus();
  $("#tw-list", dlg).onclick = e => {
    const b = e.target.closest("[data-f]"); if (!b) return;
    const w = Math.round(screen.availWidth / 2), url = location.pathname + "#read=" + encodeURIComponent(b.dataset.f) + (b.dataset.f === file && page ? "&p=" + page : "");
    const win = window.open(url, "jw-beside", `popup=yes,width=${w},height=${screen.availHeight},left=${(screen.availLeft || 0) + w},top=${screen.availTop || 0}`);
    if (!win) return toast("Your browser blocked the second window: allow pop-ups for this page and try again.");
    dlg.close();
  };
}
window.Twin = {open};
