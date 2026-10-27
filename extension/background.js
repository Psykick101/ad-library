// Save to Psykick Studio (v1.4) - service worker.
// Workflow: click / area / right-click -> finder.js works out WHAT it is (a video post, a picture ad, a landing page)
// and the post's own link -> POST /api/ext/save with the extension token -> the Studio downloads the real video ->
// we poll /api/ext/status and tell you when it's in Inspiration (or why it failed). A screenshot rides along only as
// the thumbnail / fallback. Auth: a per-browser token from the Studio's "Connect the extension" page (no cookies).
importScripts("config.js", "finder.js");
const STUDIO = self.PSYKICK_STUDIO;
const F = self.PsykickFind;
const VERSION = chrome.runtime.getManifest().version;
const CONNECT_PATH = "/studio/extension/connect";
const FINAL = new Set(["done", "failed", "gone"]);

// ---------------- version + update feed (/ui/psykick-saver.json) ----------------
const newer = (a, b) => { const x = String(a).split(".").map(Number), y = String(b).split(".").map(Number);
  for (let i = 0; i < 3; i++) { if ((x[i] || 0) !== (y[i] || 0)) return (x[i] || 0) > (y[i] || 0); } return false; };
async function checkUpdate() {
  try {
    let j;
    try {                                   // /api/ext/ping is reachable without the Cloudflare sign-in
      const p = await (await fetch(STUDIO + "/api/ext/ping", {cache: "no-store", credentials: "omit"})).json();
      j = {version: p.latest, notes: p.notes || "", config: p.config || {}};
    } catch (e) {
      j = await (await fetch(STUDIO + "/ui/psykick-saver.json", {cache: "no-store", credentials: "include"})).json();
    }
    await chrome.storage.local.set({latest: j, checked: Date.now()});
    await badge();
    return j;
  } catch (e) { return null; }
}
async function badge() {
  const {latest, recent = []} = await chrome.storage.local.get(["latest", "recent"]);
  const busy = recent.filter(x => !FINAL.has(x.status)).length;
  const upd = latest && latest.version && newer(latest.version, VERSION);
  chrome.action.setBadgeText({text: busy ? String(busy) : upd ? "NEW" : ""});
  chrome.action.setBadgeBackgroundColor({color: busy ? "#4b8bd6" : "#7cc4a8"});
}

// ---------------- auth: token from the Connect page ----------------
async function getAuth() { const {auth} = await chrome.storage.local.get("auth"); return auth && auth.token ? auth : null; }
function deviceLabel() {
  const p = (navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform || "";
  return "Chrome extension" + (p ? " on " + p.replace(/^Win.*/, "Windows").replace(/^Mac.*/, "Mac") : "");
}
async function openConnect() {
  const url = `${STUDIO}${CONNECT_PATH}?ext=${chrome.runtime.id}&label=${encodeURIComponent(deviceLabel())}`;
  const [old] = await chrome.tabs.query({url: STUDIO + CONNECT_PATH + "*"});
  if (old) { chrome.tabs.update(old.id, {active: true, url}); return; }
  chrome.tabs.create({url});
}
async function storeToken(m) {
  if (!m || typeof m.token !== "string" || !m.token.startsWith("psx_")) return {ok: false};
  const r = await api("/api/ext/ping", {}, m.token);
  if (!r.ok || !r.data.user) return {ok: false, error: r.error || "token refused"};
  await chrome.storage.local.set({auth: {token: m.token, email: r.data.user, role: r.data.role || "", id: m.id || "", at: Date.now()}});
  notify("Connected ✓", `Saving as ${r.data.user}. Right-click any ad, or press Alt+Shift+S.`);
  return {ok: true, email: r.data.user};
}
const fromConnectPage = url => typeof url === "string" && url.startsWith(STUDIO + CONNECT_PATH);
chrome.runtime.onMessageExternal.addListener((m, sender, reply) => {     // the Connect page itself (externally_connectable)
  if (!m || m.type !== "psykick-connect" || sender.origin !== new URL(STUDIO).origin || !fromConnectPage(sender.url)) return;
  storeToken(m).then(reply);
  return true;
});

// One call to the Studio. -> {ok, status, data, error, connect}
async function api(path, opts = {}, token) {
  const a = token ? {token} : await getAuth();
  const headers = {...(opts.headers || {})};
  if (a) headers.Authorization = "Bearer " + a.token;
  let r;
  try {
    r = await fetch(STUDIO + path, {...opts, headers, credentials: a ? "omit" : "include", redirect: "manual", cache: "no-store"});
  } catch (e) { return {ok: false, status: 0, error: "Couldn't reach the Studio - check your connection."}; }
  if (r.type === "opaqueredirect" || r.status === 0 || (r.status >= 300 && r.status < 400))
    return {ok: false, status: r.status, connect: true, error: "The Studio's sign-in got in the way - press Connect in the extension."};
  let data = {};
  try { data = await r.json(); } catch (e) { return {ok: false, status: r.status, error: `The Studio answered strangely (${r.status}).`}; }
  if (r.status === 401) {
    if (a && !token) await chrome.storage.local.remove("auth");
    return {ok: false, status: 401, connect: true, data, error: "Not connected - press Connect in the extension."};
  }
  return {ok: r.ok, status: r.status, data, error: r.ok ? "" : (data.error || `Error ${r.status}`)};
}

// ---------------- admins: "Connect as saver account" (the company's Instagram / Facebook account) ----------------
// Reads ONLY these cookies of the account logged in in this Chrome profile and sends them once to the Studio, which
// keeps them encrypted in the Ad Library (downloads + Instagram DMs run as that account). Nothing else is read.
const SAVER = {instagram: {url: "https://www.instagram.com/", names: ["sessionid", "ds_user_id", "csrftoken"], label: "Instagram"},
               facebook: {url: "https://www.facebook.com/", names: ["c_user", "xs", "datr"], label: "Facebook"}};
async function connectSaver(platform) {
  const s = SAVER[platform];
  const a = await getAuth();
  if (!s || !a) return {ok: false, error: "Connect the extension first."};
  if (!["owner", "admin"].includes(a.role)) return {ok: false, error: "Only admins can connect the saver account."};
  let all = [];
  try { all = await chrome.cookies.getAll({url: s.url}); } catch (e) { return {ok: false, error: "Chrome didn't allow reading the login."}; }
  const cookies = all.filter(c => s.names.includes(c.name))
    .map(c => ({name: c.name, value: c.value, domain: c.domain, expirationDate: c.expirationDate || 0}));
  const missing = s.names.filter(n => !cookies.some(c => c.name === n));
  if (missing.length) return {ok: false, error: `Log in to the ${s.label} saver account in this Chrome profile first (not signed in here).`};
  const r = await api("/api/ext/saver-account", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({platform, cookies})});
  if (!r.ok) return {ok: false, error: r.error};
  const st = r.data;
  return {ok: st.status === "ok", status: st, error: st.status === "ok" ? "" : (st.error || st.status)};
}

// ---------------- feedback: notification + a small toast in the page ----------------
function notify(title, message) {
  try { const p = chrome.notifications.create({type: "basic", iconUrl: "icon128.png", title, message}); if (p && p.catch) p.catch(() => {}); }
  catch (e) { /* no notifications */ }
}
async function toast(tab, msg, tone) {
  if (!tab || !tab.id) return;
  try {
    await chrome.scripting.executeScript({target: {tabId: tab.id}, args: [msg, tone || "ok"], func: (msg, tone) => {
      let t = document.getElementById("__psykick_toast");
      if (!t) { t = document.createElement("div"); t.id = "__psykick_toast"; document.documentElement.appendChild(t); }
      t.textContent = msg;
      t.style.cssText = "position:fixed;z-index:2147483647;left:50%;bottom:28px;transform:translateX(-50%);max-width:min(560px,92vw);" +
        "padding:12px 18px;border-radius:12px;font:600 14px/1.4 system-ui,sans-serif;box-shadow:0 8px 30px rgba(0,0,0,.35);" +
        (tone === "warn" ? "background:#3a2a12;color:#ffd79a;border:1px solid #8a6a2a" : tone === "bad" ? "background:#3a1515;color:#ffb3b3;border:1px solid #8a3a3a"
          : "background:#0f1216;color:#e6eaef;border:1px solid #7cc4a8");
      clearTimeout(t.__h); t.__h = setTimeout(() => t.remove(), tone === "warn" ? 9000 : 5000);
    }});
  } catch (e) { /* chrome:// pages etc. */ }
}

// ---------------- save + status tracking ----------------
async function track(item) {
  const {recent = []} = await chrome.storage.local.get("recent");
  const i = recent.findIndex(x => x.id === item.id);
  if (i >= 0) recent.splice(i, 1);
  recent.unshift(item);
  await chrome.storage.local.set({recent: recent.slice(0, 25)});
  await badge();
}

async function save(fields, tab) {
  if (!(await getAuth())) {
    notify("Connect the extension first", "A Studio tab opens - press Connect there, then save again.");
    toast(tab, "Connect the extension first - a Studio tab opens: press Connect, then save again.", "warn");
    openConnect();
    return {ok: false, connect: true};
  }
  const {defaults = {}} = await chrome.storage.local.get("defaults");
  const body = {...fields};
  for (const k of ["tags", "note"]) if (defaults[k] && !body[k]) body[k] = defaults[k];
  const r = await api("/api/ext/save", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  if (!r.ok) {
    if (r.connect) { notify("Connect the extension", r.error); toast(tab, r.error, "warn"); openConnect(); }
    else { notify("Not saved", r.error); toast(tab, "Not saved: " + r.error, "bad"); }
    return r;
  }
  const j = r.data;
  const status = j.duplicate ? (j.status || "done") : (j.status || "reading");
  await track({id: j.id, ts: Date.now(), status, error: "", platform: j.platform || fields.platform || "",
               title: (fields.title || "").slice(0, 120), url: fields.post_url || fields.page_url || "", kind: fields.kind,
               duplicate: !!j.duplicate});
  const msg = j.duplicate ? "Already in the library - someone saved this one before."
    : j.retrying ? "Trying this one again - it failed last time."
    : status === "queued" ? "Saving the video… it appears in Inspiration when it's downloaded."
    : "Saved ✓ The AI is reading it now - find it under Inspiration.";
  toast(tab, msg, "ok");
  notify(j.duplicate ? "Already in the library" : "Saving to Psykick Studio", msg);
  if (!FINAL.has(status)) pollSoon();
  return {ok: true, ...j};
}

let pollTimer = null;
function pollSoon(ms) { clearTimeout(pollTimer); pollTimer = setTimeout(poll, ms || 3000); chrome.alarms.create("poll", {periodInMinutes: 0.5}); }
async function poll() {
  const {recent = []} = await chrome.storage.local.get("recent");
  const open = recent.filter(x => !FINAL.has(x.status) && Date.now() - x.ts < 6 * 3600e3);
  if (!open.length) { chrome.alarms.clear("poll"); await badge(); return recent; }
  const r = await api("/api/ext/status?ids=" + open.map(x => x.id).join(","));
  if (r.ok) {
    const by = Object.fromEntries((r.data.items || []).map(x => [x.id, x]));
    for (const x of recent) {
      const s = by[x.id];
      if (!s || s.status === x.status) continue;
      const was = x.status;
      Object.assign(x, {status: s.status, error: s.error || "", title: s.title || x.title, platform: s.platform || x.platform});
      if (s.status === "done" && was !== "done") notify("In Inspiration ✓", (x.title || "Your save") + " - the real video is in the library.");
      if (s.status === "failed") notify("Couldn't save the video", s.error || "Unknown reason");
    }
    await chrome.storage.local.set({recent});
  }
  await badge();
  if (recent.some(x => !FINAL.has(x.status))) pollSoon(5000); else chrome.alarms.clear("poll");
  return recent;
}

// ---------------- screenshots ----------------
async function shot(tab, rect) {
  const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, {format: rect ? "png" : "jpeg", quality: 85});
  if (!rect) return dataUrl;
  const bmp = await createImageBitmap(await (await fetch(dataUrl)).blob());
  const k = bmp.width / rect.vw;
  const x = Math.max(0, rect.x), y = Math.max(0, rect.y);
  const w = Math.max(1, Math.min(rect.w, rect.vw - x)), h = Math.max(1, rect.h);
  const c = new OffscreenCanvas(Math.round(w * k), Math.round(h * k));
  c.getContext("2d").drawImage(bmp, x * k, y * k, w * k, h * k, 0, 0, c.width, c.height);
  const out = await c.convertToBlob({type: "image/jpeg", quality: 0.85});
  return await new Promise(res => { const fr = new FileReader(); fr.onload = () => res(fr.result); fr.readAsDataURL(out); });
}

async function inject(tab) {
  await chrome.scripting.executeScript({target: {tabId: tab.id}, files: ["finder.js"]});
}

// What was found -> the right kind of save. A blob: video with no post link is NOT saved as a useless frame.
// The tab's title only describes the post when the post IS the page; on a feed / Explore grid it is boilerplate
// ("Explore - Find your favorite videos on TikTok") - then the card's own text (or the video's real title) names it.
function titleFor(tab, f) {
  const here = F.canonical(tab.url || "");
  if (!f.post_url || (here && f.post_key && here.key === f.post_key) || ["location", "og"].includes(f.detected)) return tab.title || "";
  return (f.text || "").split("\n").map(s => s.trim()).find(s => s.length > 3 && !/^@?[\w.-]+$/.test(s))?.slice(0, 160) || "";
}

async function decideAndSave(tab, f, shotUrl, source) {
  const base = {page_url: tab.url, title: titleFor(tab, f), ad_text: f.text || "", data_url: shotUrl || "", source,
                author: f.author || "", detected: f.detected || "", platform: f.platform || ""};
  if (f.kind === "video") {
    if (f.post_url) return save({...base, kind: "video", post_url: f.post_url, media_url: f.media_url || ""}, tab);
    if (f.media_url) return save({...base, kind: "video", media_url: f.media_url}, tab);
    const msg = "Couldn't find this video's own post link. Open the post itself (click it so it gets its own address) and press Save again.";
    toast(tab, msg, "warn");
    notify("Open the post itself and press Save", msg);
    return {ok: false, reason: "no-link", message: msg};
  }
  // a picture inside a post: send the post link too, so the server downloads the post's real media (video if it has one)
  if (f.post_url) return save({...base, kind: "image", post_url: f.post_url, media_url: f.media_url || ""}, tab);
  if (f.kind === "image" && f.media_url && !shotUrl) return save({...base, kind: "image", media_url: f.media_url}, tab);
  return save({...base, kind: f.kind || "image"}, tab);
}

// Drag a box around an ad - or just click a video. Returns what it found (for the tests too).
async function selectArea(tab) {
  if (!tab || !tab.id) return null;
  try { await inject(tab); } catch (e) { notify("Can't save from this page", "Chrome doesn't let extensions into this page."); return null; }
  const [{result}] = await chrome.scripting.executeScript({target: {tabId: tab.id}, func: () => new Promise(resolve => {
    const ov = document.createElement("div");
    ov.style.cssText = "position:fixed;inset:0;z-index:2147483647;cursor:crosshair;background:rgba(18,11,46,.22)";
    const box = document.createElement("div");
    box.style.cssText = "position:fixed;border:2px solid #7cc4a8;background:rgba(124,196,168,.15);display:none;pointer-events:none";
    const tip = document.createElement("div");
    tip.textContent = "Drag around the ad - or click a video · Esc to cancel";
    tip.style.cssText = "position:fixed;top:12px;left:50%;transform:translateX(-50%);background:#0f1216;color:#fff;font:14px system-ui,sans-serif;padding:8px 14px;border-radius:999px;pointer-events:none";
    ov.append(box, tip); document.documentElement.appendChild(ov);
    let sx = null, sy = null;
    const done = v => { ov.remove(); document.removeEventListener("keydown", esc, true); resolve(v); };
    const esc = e => { if (e.key === "Escape") done(null); };
    document.addEventListener("keydown", esc, true);
    ov.addEventListener("mousedown", e => { e.preventDefault(); sx = e.clientX; sy = e.clientY; box.style.display = "block"; });
    ov.addEventListener("mousemove", e => { if (sx == null) return;
      Object.assign(box.style, {left: Math.min(sx, e.clientX) + "px", top: Math.min(sy, e.clientY) + "px",
        width: Math.abs(e.clientX - sx) + "px", height: Math.abs(e.clientY - sy) + "px"}); });
    ov.addEventListener("mouseup", e => {
      if (sx == null) return;
      const r = {x: Math.min(sx, e.clientX), y: Math.min(sy, e.clientY), w: Math.abs(e.clientX - sx), h: Math.abs(e.clientY - sy), vw: innerWidth};
      ov.style.display = "none";                                  // look underneath, and keep it out of the screenshot
      let found;
      try { found = self.PsykickFind.inRect(r); } catch (err) { found = {kind: "page", error: String(err)}; }
      if (r.w < 24 || r.h < 24) {                                  // a click: crop to the video (or nothing to crop)
        if (found.box) Object.assign(r, {x: found.box.x, y: found.box.y, w: found.box.w, h: found.box.h});
        else if (found.kind !== "video") { done({cancel: true}); return; }
      }
      setTimeout(() => done({rect: r, found}), 80);
    });
  })});
  if (!result || result.cancel) return result;
  let crop = "";
  try { crop = await shot(tab, result.rect); } catch (e) { /* screenshot is optional */ }
  const res = await decideAndSave(tab, result.found, crop, "extension-area");
  return {found: result.found, result: res};
}

async function savePageVideo(tab) {
  let f = null;
  const c = F.canonical(tab.url);
  try {
    await inject(tab);
    [{result: f}] = await chrome.scripting.executeScript({target: {tabId: tab.id}, func: () => self.PsykickFind.pagePost()});
  } catch (e) { f = c ? {kind: "video", post_url: c.url, platform: c.platform, detected: "location"} : null; }
  if (!f || !f.post_url) {
    const msg = "This page isn't a post. Open the video's own page (click the post) and press Save again.";
    toast(tab, msg, "warn"); notify("No video post on this page", msg);
    return {ok: false, reason: "no-post"};
  }
  let crop = "";
  try { crop = await shot(tab, f.box ? {...f.box, vw: (await tabWidth(tab))} : null); } catch (e) { /* optional */ }
  return decideAndSave(tab, f, crop, "extension-page");
}
async function tabWidth(tab) {
  const [{result}] = await chrome.scripting.executeScript({target: {tabId: tab.id}, func: () => innerWidth});
  return result;
}

async function saveScreen(tab, extra) {
  let crop = "";
  try { crop = await shot(tab); } catch (e) { notify("Couldn't take a screenshot", String(e).slice(0, 120)); return; }
  return save({kind: "page", page_url: tab.url, title: tab.title, data_url: crop, source: "extension-screen", ...(extra || {})}, tab);
}

// ---------------- context menus ----------------
function menus() {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({id: "vid", title: "Save this video to Psykick Studio", contexts: ["video", "link"]});
    chrome.contextMenus.create({id: "img", title: "Save this image to Psykick Studio", contexts: ["image"]});
    chrome.contextMenus.create({id: "post", title: "Save this post's video to Psykick Studio", contexts: ["page"],
      documentUrlPatterns: ["*://*.tiktok.com/*", "*://*.instagram.com/*", "*://*.facebook.com/*", "*://*.youtube.com/*",
                            "*://*.x.com/*", "*://*.twitter.com/*", "*://*.pinterest.com/*", "*://*.snapchat.com/*", "*://*.linkedin.com/*"]});
    chrome.contextMenus.create({id: "area", title: "Select an area (or click a video) and save it", contexts: ["page", "image", "video", "link"]});
    chrome.contextMenus.create({id: "shot", title: "Save this page (screenshot + link)", contexts: ["page"]});
  });
}
chrome.runtime.onInstalled.addListener(() => { menus(); checkUpdate(); });
chrome.runtime.onStartup.addListener(() => { menus(); checkUpdate(); poll(); });
chrome.alarms.create("upd", {periodInMinutes: 360});
chrome.alarms.onAlarm.addListener(a => { if (a.name === "upd") checkUpdate(); if (a.name === "poll") poll(); });

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId === "area") return selectArea(tab);
  if (info.menuItemId === "shot") return saveScreen(tab);
  if (info.menuItemId === "post") return savePageVideo(tab);
  let f = null;
  try {
    await inject(tab);
    [{result: f}] = await chrome.scripting.executeScript({target: {tabId: tab.id}, args: [info.linkUrl || ""],
      func: link => self.PsykickFind.forElement(window.__psykickLast, link)});
  } catch (e) { const c = F.canonical(info.linkUrl || ""); f = c ? {kind: "video", post_url: c.url, platform: c.platform} : {kind: "page"}; }
  if (info.menuItemId === "img") {
    const src = /^https?:/.test(info.srcUrl || "") ? info.srcUrl : "";
    return save({kind: "image", media_url: src, page_url: (f && f.post_url) || info.pageUrl || tab.url, title: tab.title,
                 ad_text: (f && f.text) || "", source: "extension"}, tab);
  }
  // "vid": a video or a link
  if (info.mediaType === "video" && f.kind !== "video") f = {...f, kind: "video"};
  if (f.kind === "video" && !f.post_url && !f.media_url && /^https?:/.test(info.srcUrl || "")) f.media_url = info.srcUrl;
  if (f.kind !== "video" && !f.post_url) {
    toast(tab, "That link isn't a post. Right-click the video itself, or open the post and press Save.", "warn");
    return;
  }
  let crop = "";
  try { if (f.box) crop = await shot(tab, {...f.box, vw: await tabWidth(tab)}); } catch (e) { /* optional */ }
  return decideAndSave(tab, {...f, kind: "video"}, crop, "extension-video");
});

chrome.commands.onCommand.addListener(async cmd => {
  if (cmd !== "save-area") return;
  const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
  if (tab) selectArea(tab);
});

// ---------------- popup + connect-page messages ----------------
chrome.runtime.onMessage.addListener((m, sender, reply) => {
  (async () => {
    if (m.do === "connect-token") {                            // from connect.js on the Studio's Connect page only
      if (sender.id !== chrome.runtime.id || !sender.tab || !fromConnectPage(sender.tab.url) || !fromConnectPage(sender.url)) return reply({ok: false});
      return reply(await storeToken(m));
    }
    if (sender.id !== chrome.runtime.id || !String(sender.url || "").startsWith(chrome.runtime.getURL(""))) return reply({ok: false});
    // everything below: the extension's own pages (the popup) only - never a web page's content script
    const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
    if (m.do === "state") {
      let {auth, recent = [], latest} = await chrome.storage.local.get(["auth", "recent", "latest"]);
      if (auth && auth.role === undefined) {               // connected before 1.4: learn the role once
        const p = await api("/api/ext/ping");
        if (p.ok && p.data.user) { auth = {...auth, role: p.data.role || ""}; await chrome.storage.local.set({auth}); }
      }
      const c = tab ? F.canonical(tab.url || "") : null;
      return reply({email: auth && auth.email, role: auth && auth.role, recent, latest, version: VERSION,
                    post: c ? {url: c.url, platform: c.platform} : null, studio: STUDIO});
    }
    if (m.do === "connect") { await openConnect(); return reply({ok: true}); }
    if (m.do === "saver") return reply(await connectSaver(m.platform));
    if (m.do === "disconnect") { await api("/api/ext/disconnect", {method: "POST"}); await chrome.storage.local.remove("auth"); return reply({ok: true}); }
    if (m.do === "poll") return reply({recent: await poll()});
    if (m.do === "retry") { const r = await api("/api/ext/retry/" + encodeURIComponent(m.id), {method: "POST"});
      if (r.ok) { const {recent = []} = await chrome.storage.local.get("recent"); const x = recent.find(y => y.id === m.id);
        if (x) { x.status = "queued"; x.error = ""; await chrome.storage.local.set({recent}); } pollSoon(); }
      return reply({ok: r.ok, error: r.error}); }
    if (m.do === "check") return reply({latest: await checkUpdate(), version: VERSION});
    if (m.do === "reload") { chrome.runtime.reload(); return; }
    if (m.do === "area") { selectArea(tab); return reply({ok: true}); }
    if (m.do === "post") { savePageVideo(tab); return reply({ok: true}); }
    if (m.do === "shot") { saveScreen(tab, {note: m.note, tags: m.tags}); return reply({ok: true}); }
    reply({ok: false});
  })();
  return true;
});

// for the tests (and the console): self.PS.selectArea(tab) etc.
self.PS = {selectArea, savePageVideo, saveScreen, decideAndSave, save, poll, storeToken, openConnect, api, connectSaver};
