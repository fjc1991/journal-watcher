// The reminder that Journal Watcher is free and its author takes tips: a small card in the corner, shown at most three times, weeks apart, and only at a good moment
// (the window is in front, nothing is open over the page, you are not reading, the AI is not working, and the welcome guide is done). The server (support.py) counts
// visits on this computer and says when it is due; it never leaves the computer. "Maybe later" just closes it, "Don't ask again" and a click on the tip link end it for good.
// The same page is under About, where the reminders can also be switched off.
import {$, esc, api, toast, ico} from "./state.js";

let S = null, box = null, timer = null;
const retry = () => Math.max(S.delay, 1) * 3000;           // not a good moment: look again after three times the first wait (two minutes and a quarter)

function welcomed() { try { return !!localStorage.getItem("jw-welcome"); } catch (e) { return true; } }    // not on the very first start, before the guide has been seen
async function goodMoment() {
  const b = document.body.classList;
  if (document.hidden || document.querySelector("dialog[open]") || b.contains("reading") || b.contains("settings-open") || b.contains("about-open") || !welcomed()) return false;
  try { const a = await api("/api/activity"); return !(a.jobs || []).some(j => !j.ended); } catch (e) { return false; }
}
function later(ms) { clearTimeout(timer); if (S && S.due && !box) timer = setTimeout(attempt, ms); }
async function attempt() {
  try { S = await api("/api/support"); } catch (e) { return; }
  if (!S.due || box) return;
  if (await goodMoment()) show(); else later(retry());
}
function close() { if (box) { box.remove(); box = null; } }
function show() {
  const a = S.author, s = a.support;
  box = document.createElement("div"); box.className = "sup card"; box.setAttribute("role", "dialog"); box.setAttribute("aria-modal", "false"); box.setAttribute("aria-labelledby", "sup-t");
  box.innerHTML = `<h2 id="sup-t">${ico("heart")} Is Journal Watcher saving you time?</h2>
    <p>It is free and open source${a.name ? `, made by ${esc(a.name)}${a.university ? ` at ${esc(a.university)}` : ""}` : ""}. If it helps your research, you can chip in for a coffee. It is never required.</p>
    <div class="sup-btns"><a class="btn primary" id="sup-go" href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${ico("heart", "sm")} Buy a coffee on ${esc(s.service)}</a>
      <button class="btn" id="sup-later">Maybe later</button><button class="btn" id="sup-never">Don’t ask again</button></div>
    <div class="meta">You can change this under About. The app only counts, on this computer, how often you open it.</div>`;
  document.body.append(box);
  api("/api/support/action", {action: "shown"}).then(r => { S = r; }).catch(() => {});      // counts when it appears, so a closed window does not bring it back at once
  $("#sup-later", box).onclick = close;
  $("#sup-never", box).onclick = async () => { close(); try { S = await api("/api/support/action", {action: "never"}); toast("Understood: I won’t ask again. The About page keeps the link."); } catch (e) { toast(e.message); } };
  $("#sup-go", box).addEventListener("click", () => { api("/api/support/action", {action: "supported"}).catch(() => {}); close(); });
  box.addEventListener("keydown", e => { if (e.key === "Escape") { e.stopPropagation(); close(); } });
}

async function visit() {
  try { S = await api("/api/support/visit", {}); } catch (e) { return; }
  later(S.delay * 1000);
}
window.Support = {attempt};
window.addEventListener("load", () => setTimeout(visit, 1500));
document.addEventListener("visibilitychange", () => { if (!document.hidden) visit(); });      // coming back after 30 minutes or more counts as a new visit (the server decides)
