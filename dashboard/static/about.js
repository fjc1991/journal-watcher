// The About page: who makes Journal Watcher (name, role, university, a short text and links, from static/author.json by way of /api/support), what the app is
// (version, licence, where the source is) and how to support it. A layer over the page like Settings; the sidebar's About button and the palette open it.
// Fields the author left empty are not drawn, and with no tip page there is no support card and no reminder.
import {$, esc, api, toast, ico} from "./state.js";

let root = null, D = null;
const link = (u, label, cls = "") => `<a${cls ? ` class="${cls}"` : ""} href="${esc(u)}" target="_blank" rel="noopener noreferrer">${label}</a>`;
const day = t => t ? new Date(t * 1000).toLocaleDateString(undefined, {year: "numeric", month: "long", day: "numeric"}) : "";

function build() {
  if (root) return;
  root = document.createElement("div"); root.id = "about"; root.className = "st au hide"; root.setAttribute("aria-label", "About Journal Watcher");
  root.innerHTML = `<div class="st-head"><button class="btn back" id="au-back">← Back</button><h1>About</h1></div><div class="st-body" id="au-body"></div>`;
  document.body.append(root);
  $("#au-back", root).onclick = close;
  document.addEventListener("keydown", e => { if (e.key === "Escape" && !root.classList.contains("hide") && !e.target.closest("input,textarea,select") && !document.querySelector("dialog[open]")) close(); });
}
export function close() { root?.classList.add("hide"); document.body.classList.remove("about-open"); }
export async function open() {
  build(); if (window.Settings) Settings.close();
  root.classList.remove("hide"); document.body.classList.add("about-open");
  try { D = await api("/api/support"); } catch (e) { toast(e.message); return; }
  paint();
}

function paint() {
  const a = D.author, s = a.support, body = $("#au-body", root);
  const who = a.name ? `<section class="card st-card au-who">${a.photo ? `<img class="au-photo" src="/static/${esc(a.photo)}" alt="">` : ""}
      <div class="au-text"><div class="eyebrow">Made by</div><h2>${esc(a.name)}</h2>
        ${a.role || a.university ? `<p class="au-role">${esc(a.role)}${a.role && a.university ? " · " : ""}${a.university ? (a.university_url ? link(a.university_url, esc(a.university)) : esc(a.university)) : ""}</p>` : ""}
        ${a.bio.map(p => `<p>${esc(p)}</p>`).join("")}
        ${a.links.length ? `<p class="au-links">${a.links.map(l => link(l.url, esc(l.label), "au-pill")).join("")}</p>` : ""}</div></section>` : "";
  const tip = s.url ? `<section class="card st-card au-tip"><h2>${ico("heart")} Support Journal Watcher</h2>
      <p>Journal Watcher is free, and it stays free. If it saves you time in your research, you can chip in for a coffee on ${esc(s.service)}. It is never required, and the app works the same either way.</p>
      <p>${link(s.url, `${ico("heart", "sm")} Buy a coffee on ${esc(s.service)}`, "btn primary au-tipbtn")}</p>
      <label class="chk"><input type="checkbox" id="au-remind"${D.optout || D.supported ? "" : " checked"}> Remind me about this now and then (at most three times, weeks apart)</label>
      <p class="hint">To know when it is polite to ask, the app counts on this computer how many times you open it${D.visits ? ` (${D.visits} so far, since ${esc(day(D.since))})` : ""}. The count is kept in your data folder and never sent anywhere.</p>
      <div class="st-msg" id="au-msg"></div></section>` : "";
  body.innerHTML = `${who}
    <section class="card st-card"><h2>Journal Watcher</h2>
      <p>A research desk for journals: it checks the journals you follow, screens new papers against your interests, and gives you one place to read, highlight and ask questions about them.</p>
      <p>It is free software under the <b>GNU Affero General Public License, version 3 or later</b>${a.repo ? `: the source code is on ${link(a.repo, "GitHub")}` : ""}. Your papers, notes and settings stay in a folder on this computer; there is no account and no server of ours, and nothing is tracked.</p></section>
    ${tip}`;
  const cb = $("#au-remind", body), tb = $(".au-tipbtn", body);
  if (cb) cb.onchange = async () => { try { D = await api("/api/support/action", {action: "reminders", value: cb.checked}); $("#au-msg", body).textContent = cb.checked ? "I will remind you now and then." : "No more reminders."; } catch (e) { toast(e.message); } };
  if (tb) tb.addEventListener("click", () => { api("/api/support/action", {action: "supported"}).catch(() => {}); });
}

window.About = {open, close};
