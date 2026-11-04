/* Your choices follow you between computers. The page keeps them in the browser's storage (fast, and the theme must be known before the first paint); this script, loaded first, makes
   the ones the server names (GET /api/uiprefs: theme, reader mode, list or grid, where you stopped reading…) agree with the file in the data folder, and sends every later change there.
   The rest of the page does not know: it keeps calling localStorage as before. What is not named (sidebar folded, reader panel width) stays in this browser only. */
(function () {
  "use strict";
  var LS;
  try { LS = window.localStorage; LS.getItem("jw-prefsync"); } catch (e) { return; }          // no storage (a private window): nothing to keep in step
  var rawSet = Storage.prototype.setItem, rawRemove = Storage.prototype.removeItem, rawGet = Storage.prototype.getItem;
  var MARK = "jw-prefsync";                                                                // "1" once this browser has been brought into step with the folder at least once
  var rules = null, pending = {}, timer = null, due = 0;

  function shared(k) { return !!rules && typeof k === "string" && (rules.exact.indexOf(k) >= 0 || rules.prefix.some(function (p) { return k.indexOf(p) === 0; })); }

  // ---- pull, synchronously: the page's other scripts read these values as they start
  var srv = null;
  try { var x = new XMLHttpRequest(); x.open("GET", "/api/uiprefs", false); x.send(); if (x.status === 200) srv = JSON.parse(x.responseText); } catch (e) { srv = null; }
  if (srv && srv.rules && srv.prefs) {
    rules = srv.rules;
    var seeded = rawGet.call(LS, MARK) === "1", seed = {}, own = [], i, k;
    Object.keys(srv.prefs).forEach(function (key) { if (rawGet.call(LS, key) !== srv.prefs[key]) rawSet.call(LS, key, srv.prefs[key]); });       // the folder wins
    for (i = 0; i < LS.length; i++) { k = LS.key(i); if (shared(k) && !(k in srv.prefs)) own.push(k); }
    own.forEach(function (key) {
      if (seeded) rawRemove.call(LS, key);                                                  // it was removed on another computer
      else seed[key] = rawGet.call(LS, key);                                                // first time: what this browser already had goes into the folder
    });
    try { rawSet.call(LS, MARK, "1"); } catch (e) { /* fine */ }
    Object.keys(seed).forEach(function (key) { pending[key] = seed[key]; });
  }

  // ---- push: every later change to a shared key
  function send(body, unloading) {
    try {
      if (unloading && navigator.sendBeacon && navigator.sendBeacon("/api/uiprefs", new Blob([body], {type: "application/json"}))) return;
      fetch("/api/uiprefs", {method: "POST", headers: {"Content-Type": "application/json"}, body: body, keepalive: true}).then(function (r) { if (!r.ok) throw new Error(r.status); }).catch(function () {
        var d = JSON.parse(body);                                                           // it did not arrive: try again with the next change (a newer value of the same key wins)
        Object.keys(d.set).forEach(function (key) { if (!(key in pending)) pending[key] = d.set[key]; });
        d.remove.forEach(function (key) { if (!(key in pending)) pending[key] = null; });
      });
    } catch (e) { /* fine */ }
  }
  function flush(unloading) {
    if (timer) { clearTimeout(timer); timer = null; due = 0; }
    var keys = Object.keys(pending);
    if (!keys.length) return;
    var set = {}, remove = [];
    keys.forEach(function (key) { if (pending[key] === null) remove.push(key); else set[key] = pending[key]; });
    pending = {};
    send(JSON.stringify({set: set, remove: remove}), unloading);
  }
  function queue(key, value) {
    pending[key] = value;
    // where you stopped reading changes every few seconds while you read: those go out at most every 15 s (and when the page closes); everything else within half a second
    var wait = key.indexOf("jw-pos:") === 0 ? 15000 : 400, at = Date.now() + wait;
    if (timer && due <= at) return;
    if (timer) clearTimeout(timer);
    due = at; timer = setTimeout(function () { flush(false); }, wait);
  }
  Storage.prototype.setItem = function (k, v) { rawSet.call(this, k, v); if (this === LS && shared(String(k))) queue(String(k), String(v)); };
  Storage.prototype.removeItem = function (k) { rawRemove.call(this, k); if (this === LS && shared(String(k))) queue(String(k), null); };
  window.addEventListener("pagehide", function () { flush(true); });
  document.addEventListener("visibilitychange", function () { if (document.visibilityState === "hidden") flush(true); });
  if (Object.keys(pending).length) flush(false);                                            // the seed from this browser's older choices
})();
