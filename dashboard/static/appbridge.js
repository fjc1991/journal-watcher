/* In the Mac app the page runs in a plain web view: there are no pop-up windows and no download panel for files the page makes itself.
   This file hands those to the app (window.pywebview.api, see app/jw_app.py). In a normal browser it does nothing. */
(() => {
  const api = () => window.pywebview && window.pywebview.api && window.pywebview.api.save_url ? window.pywebview.api : null;
  const say = m => (window.toast ? window.toast(m) : console.log(m));
  const done = r => { if (r && r.ok) say("Saved " + r.name + " to " + r.folder + "."); else if (r && r.error) say(r.error); };   // cancelled: nothing to say

  // <a download> (the exports, the backup, the notes): the app asks where to save and writes the file
  document.addEventListener("click", e => {
    const a = e.target.closest && e.target.closest("a[download]"), A = api();
    if (!a || !A) return;
    e.preventDefault(); e.stopPropagation();
    const name = a.getAttribute("download") || "";
    if (a.href.startsWith("blob:")) {
      fetch(a.href).then(r => r.blob()).then(b => new Promise(res => { const fr = new FileReader(); fr.onload = () => res(String(fr.result).split(",")[1] || ""); fr.readAsDataURL(b); }))
        .then(b64 => A.save_data(name || "download", b64)).then(done).catch(err => say("Could not save the file: " + err));
    } else A.save_url(a.href, name).then(done).catch(err => say("Could not save the file: " + err));
  }, true);

  // window.open: a second window on this app (“open beside”) or the page in your own browser
  const nativeOpen = window.open.bind(window);
  window.open = (url, target, features) => {
    const A = api(); if (!A) return nativeOpen(url, target, features);
    const u = new URL(url || "", location.href);
    if (u.origin === location.origin) A.open_beside(u.href, features || ""); else A.open_external(u.href);
    return {closed: false, focus() {}, close() {}};
  };
})();
