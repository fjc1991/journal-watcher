// A small note in the top bar when the data folder is also open on another computer (Dropbox), or Dropbox has had to keep two copies of a file.
// It asks the server every 45 seconds (/api/sync/status) and opens Settings → Computers & sync when pressed. It says nothing when there is nothing to say.
import {$, api} from "./state.js";

const EVERY = 45000;
let timer = 0;

export async function refresh() {
  const pill = $("#syncpill"); if (!pill) return;
  let r; try { r = await api("/api/sync/status"); } catch (e) { return; }          // (the next poll tries again)
  const open = r.open || [], n = r.conflicts || 0, parts = [];
  if (open.length) parts.push("Also open on " + (open.length === 1 ? open[0] : open.length + " computers"));
  if (n) parts.push(n === 1 ? "1 conflicting copy" : n + " conflicting copies");
  pill.classList.toggle("hide", !parts.length);
  $("span", pill).textContent = parts.join(" · ");
  pill.title = parts.length ? "The data folder is shared between computers. Press to see which, and what to do." : "";
}

function start() {
  const pill = $("#syncpill"); if (!pill) return;
  pill.onclick = () => window.Settings && window.Settings.open("sync");
  refresh(); timer = setInterval(refresh, EVERY);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
}
start();
window.SyncBar = {refresh};
