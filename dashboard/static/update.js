// "A new version is ready": the pill in the top bar, the dialog with what is new and the Update now button, and the quiet checks behind them.
// The server (updater.py) looks at the update feed by itself a little after start-up and every six hours; this file only asks it what it found
// (every few minutes, and when the window comes back to the front) and, when you press Update now, shows the progress until the app restarts.
import {$, esc, api, toast, ico} from "./state.js";

let S = null, dlg = null, poll = null;
const mb = n => n ? Math.round(n / 1048576) + " MB" : "";
const PHASE = {downloading: s => `Downloading… ${s.progress.pct ? s.progress.pct + "%" : ""}`, verifying: () => "Checking the download…", installing: () => "Installing…", restarting: () => "Restarting Journal Watcher… the window closes and opens again by itself.",
  revealed: () => "The new version is in your Downloads folder, shown in Finder. Drag it onto Applications to replace this one."};
const busy = s => s && ["downloading", "verifying", "installing", "restarting"].includes(s.progress.phase);

export async function refresh() {
  try { S = await api("/api/update"); } catch (e) { if (S && busy(S)) { S.progress.phase = "restarting"; draw(); } return S; }
  const pill = $("#updatepill");
  if (pill) { const show = S.available && !S.skipped; pill.classList.toggle("hide", !show); if (show) $("span", pill).textContent = "Update to " + S.latest; pill.title = `Version ${S.latest} is ready (you have ${S.current})`; }
  if (S.just_updated) { toast(`Journal Watcher is now version ${S.current}.`); api("/api/update/settings", {ack: true}).catch(() => {}); }
  if (dlg && dlg.open) draw();
  return S;
}

function draw() {
  if (!dlg || !S) return;
  const s = S, p = s.progress, run = busy(s) || p.phase === "revealed", notes = [{version: s.latest, released: s.released, notes: s.notes}, ...(s.history || [])].filter(h => h.notes && h.notes.length);
  dlg.innerHTML = `<div class="upd-h"><span class="upd-ico">${ico("download")}</span><div><h2>${s.available ? `Journal Watcher ${esc(s.latest)} is ready` : "You have the newest version"}</h2>
      <div class="meta">You have ${esc(s.current)}${s.available ? ` · released ${esc(s.released || "recently")}${s.size ? ` · ${mb(s.size)}` : ""}` : ""}</div></div></div>
    ${s.available && notes.length ? `<div class="upd-notes"><h3>What’s new</h3>${notes.map(h => `<div class="upd-v">${notes.length > 1 ? `<b>${esc(h.version)}</b>` : ""}<ul>${h.notes.map(n => `<li>${esc(n)}</li>`).join("")}</ul></div>`).join("")}</div>` : ""}
    ${s.available && !s.can_install && !run ? `<p class="meta">${s.app ? "This copy can’t replace itself here (it may be in a folder you don’t own). The new version will be downloaded to your Downloads folder: drag it onto Applications." : "You are running Journal Watcher from the project folder, so it can’t install itself. Build the app (app/build_app.sh) or open the newest Journal Watcher.app."}</p>` : ""}
    ${p.phase === "error" ? `<div class="errbox"><div class="eb-b"><b>The update didn’t work.</b> <span class="eb-d">${esc(p.error)}</span></div></div>` : ""}
    ${run ? `<div class="upd-prog"><div class="at-bar${p.phase === "downloading" && p.pct ? "" : " ind"}"><i style="width:${p.phase === "downloading" ? p.pct : 100}%"></i></div><div class="meta">${esc((PHASE[p.phase] || (() => ""))(s))}</div></div>` : ""}
    ${s.error && !s.available ? `<p class="meta">${esc(s.error)}</p>` : ""}
    <div class="upd-btns">${s.available && !run ? `<button class="btn primary" data-u="go">${s.can_install ? "Update now" : "Download it"}</button><button class="btn" data-u="later">Remind me later</button><button class="btn" data-u="skip">Skip this version</button>` : `<button class="btn${s.available ? "" : " primary"}" data-u="later"${p.phase === "restarting" ? " disabled" : ""}>${run ? "Close" : "OK"}</button>`}</div>`;
  dlg.querySelector('[data-u="later"]').onclick = () => dlg.close();
  const go = dlg.querySelector('[data-u="go"]'), skip = dlg.querySelector('[data-u="skip"]');
  if (go) go.onclick = async () => { try { S = await api("/api/update/install", {}); } catch (e) { toast(e.message); return; } draw(); watch(); };
  if (skip) skip.onclick = async () => { S = await api("/api/update/settings", {skipped: s.latest}); dlg.close(); refresh(); toast(`Version ${s.latest} skipped. You will hear about the next one.`); };
}

function watch() {       // while an update runs: look at its progress, until it ends or the app restarts
  clearInterval(poll);
  poll = setInterval(async () => { await refresh(); if (!busy(S)) clearInterval(poll); }, 700);
}

export async function show(check) {
  if (check) { toast("Looking for updates…"); try { S = await api("/api/update/check", {}); } catch (e) { toast(e.message); return; } await refresh(); if (!S.available) { toast(S.error || `You have the newest version (${S.current}).`); return; } }
  else await refresh();
  if (!S) return;
  if (!dlg) { dlg = document.createElement("dialog"); dlg.className = "card upd"; dlg.setAttribute("aria-label", "Update Journal Watcher"); document.body.append(dlg); dlg.addEventListener("click", e => { if (e.target === dlg) dlg.close(); }); }
  draw(); if (!dlg.open) dlg.showModal(); if (busy(S)) watch();
}

window.JWUpdate = {show, refresh};
{ const p = $("#updatepill"); if (p) p.onclick = () => show(false); }
window.addEventListener("load", () => setTimeout(refresh, 1500));
setInterval(() => { if (!document.hidden && !(dlg && dlg.open)) refresh(); }, 5 * 60 * 1000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
