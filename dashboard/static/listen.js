// A spoken briefing: the AI writes a two-minute script (from the paper card, or from a folder's synthesis) and your browser reads it aloud with the voices on this computer
// (Web Speech API: free, offline voices stay offline). A small player floats at the bottom: play / pause / stop, speed, voice, the script with the sentence being read lit, and a download of the text.
// Server: POST /api/ai/audio {file | folder} (stream: status | text | done | error).
import {$, esc, ico, toast, notifyError, errBox} from "./state.js";

let box = null, run = 0;
const P = {tok: 0, sents: [], i: -1, playing: false, paused: false, rate: 1, voice: "", title: "", script: ""};
const tts = () => window.speechSynthesis;
const store = (k, v) => { try { if (v === undefined) return localStorage.getItem("jw-lsn-" + k); localStorage.setItem("jw-lsn-" + k, v); } catch (e) { return null; } };
const sentences = text => text.replace(/\s+/g, " ").match(/[^.!?]+[.!?]+["”’)]*|[^.!?]+$/g)?.map(s => s.trim()).filter(Boolean) || [];

export function open({title, body}) {
  close();
  P.title = title; P.script = ""; P.sents = []; P.i = -1; P.rate = +(store("rate") || 1) || 1; P.voice = store("voice") || ""; const my = ++run;
  box = document.createElement("div"); box.className = "lsn card"; box.dataset.tool = "explain";
  box.innerHTML = `<div class="lsn-h"><b>${ico("sparkle", "sm")} ${esc(title)}</b><button data-l="close" aria-label="Close" title="Close (stops the reading)">✕</button></div><div class="lsn-body" id="lsn-body"><span class="ask-status">Writing the briefing…</span></div>
    <div class="lsn-bar" hidden><button class="btn small primary" data-l="play" id="lsn-play">▶ Listen</button><button class="btn small" data-l="stop" title="Stop and go back to the start">■</button>
      <label>Speed <select id="lsn-rate">${[0.8, 1, 1.15, 1.3, 1.5].map(r => `<option value="${r}"${r === P.rate ? " selected" : ""}>${r}×</option>`).join("")}</select></label><label>Voice <select id="lsn-voice"></select></label>
      <button class="btn small" data-l="dl" title="Download the script as text">Script</button><span class="pmeta" id="lsn-meta"></span></div>`;
  document.body.append(box);
  box.addEventListener("click", onClick); box.addEventListener("change", onChange);
  voices(); if (tts() && tts().addEventListener) tts().addEventListener("voiceschanged", voices);
  write(body, my);
}
export function close() { run++; P.tok++; if (tts()) tts().cancel(); P.playing = false; if (box) box.remove(); box = null; }

async function write(body, my) {
  let meta = "", done = null;
  try {
    const r = await fetch("/api/ai/audio", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
    const rd = r.body.getReader(), dec = new TextDecoder(); let buf = "";
    for (;;) {
      const {value, done: fin} = await rd.read(); if (fin) break; if (my !== run) { rd.cancel(); return; }
      buf += dec.decode(value, {stream: true}); let i;
      while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1); if (!line) continue; const m = JSON.parse(line);
        if (m.type === "text") { P.script += m.delta; $("#lsn-body", box).textContent = P.script; } else if (m.type === "done") done = m; else if (m.type === "error") throw new Error(m.error);
      }
    }
  } catch (e) { if (my === run && box) $("#lsn-body", box).innerHTML = errBox(e); return; }
  if (my !== run || !box) return;
  P.script = P.script.trim(); P.sents = sentences(P.script);
  $("#lsn-body", box).innerHTML = P.script.split(/\n{2,}/).map(par => `<p>${sentences(par).map(s => `<span class="lsn-s" data-i="${P.sents.indexOf(s)}">${esc(s)} </span>`).join("")}</p>`).join("");
  $(".lsn-bar", box).hidden = false;
  $("#lsn-meta", box).textContent = `${P.script.split(/\s+/).length} words · about ${Math.max(1, Math.round(P.script.split(/\s+/).length / 150))} min${done ? " · " + (done.cached ? "saved" : done.cost != null ? "$" + done.cost.toFixed(4) : "") : ""}`;
  if (!tts()) $("#lsn-meta", box).textContent += " · this browser can't speak aloud, but you can read the script";
}
function voices() {
  const sel = box && $("#lsn-voice", box); if (!sel || !tts()) return; const vs = tts().getVoices().filter(v => /^en/i.test(v.lang)).sort((a, b) => (b.localService - a.localService) || a.name.localeCompare(b.name));
  sel.innerHTML = `<option value="">default</option>` + vs.map(v => `<option value="${esc(v.name)}"${v.name === P.voice ? " selected" : ""}>${esc(v.name)}</option>`).join("");
}
function speak(i) {
  if (!box || !tts() || i >= P.sents.length) { P.playing = false; P.i = -1; mark(); btn(); return; }
  P.i = i; mark(); const t = ++P.tok;
  const u = new SpeechSynthesisUtterance(P.sents[i]); u.rate = P.rate; const v = tts().getVoices().find(x => x.name === P.voice); if (v) u.voice = v; u.lang = (v && v.lang) || "en-US";
  const next = () => { if (t === P.tok && P.playing && !P.paused) speak(i + 1); };           // an utterance that was cancelled to start another must not start the one after it
  u.onend = next; u.onerror = next;
  tts().speak(u);
}
const mark = () => box && box.querySelectorAll(".lsn-s").forEach(s => { const on = +s.dataset.i === P.i; s.classList.toggle("on", on); if (on) s.scrollIntoView({block: "nearest"}); });
const btn = () => { const b = box && $("#lsn-play", box); if (b) b.textContent = !P.playing ? "▶ Listen" : P.paused ? "▶ Continue" : "❚❚ Pause"; };
function onClick(e) {
  const b = e.target.closest("[data-l]"), s = e.target.closest(".lsn-s");
  if (s && P.sents.length) { if (!tts()) return; P.tok++; tts().cancel(); P.playing = true; P.paused = false; btn(); return speak(+s.dataset.i); }          // click a sentence to start from there
  if (!b) return; const a = b.dataset.l;
  if (a === "close") return close();
  if (a === "dl") { const l = document.createElement("a"); l.href = URL.createObjectURL(new Blob([P.title + "\n\n" + P.script + "\n"], {type: "text/plain"})); l.download = "briefing.txt"; document.body.append(l); l.click(); l.remove(); return; }
  if (!tts()) return toast("This browser can't read aloud. The script is above.");
  if (a === "stop") { P.tok++; tts().cancel(); P.playing = false; P.paused = false; P.i = -1; mark(); return btn(); }
  if (a === "play") {
    if (!P.playing) { P.playing = true; P.paused = false; P.tok++; tts().cancel(); speak(Math.max(0, P.i)); }
    else if (P.paused) { P.paused = false; tts().resume(); if (!tts().speaking) speak(Math.max(0, P.i)); }
    else { P.paused = true; tts().pause(); }
    btn();
  }
}
function onChange(e) {
  if (e.target.id === "lsn-rate") { P.rate = +e.target.value; store("rate", String(P.rate)); }
  if (e.target.id === "lsn-voice") { P.voice = e.target.value; store("voice", P.voice); }
  if (P.playing && tts()) { P.tok++; tts().cancel(); P.paused = false; speak(Math.max(0, P.i)); btn(); }       // a new speed or voice starts the current sentence again
}

window.Listen = {open, close};
