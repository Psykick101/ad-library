// Save to Psykick Studio - context menus + area capture. Uses your Studio sign-in (no keys).
const STUDIO = "https://studio.psykick.cc";
const SOCIAL = ["*://*.tiktok.com/*", "*://*.instagram.com/*", "*://*.facebook.com/*", "*://*.youtube.com/*",
                "*://*.x.com/*", "*://*.twitter.com/*", "*://*.pinterest.com/*", "*://*.linkedin.com/*", "*://*.snapchat.com/*"];
let FETCH_HOSTS = ["tiktok.com", "instagram.com", "facebook.com", "fb.watch", "youtube.com", "youtu.be", "x.com",
                   "twitter.com", "pinterest.com", "linkedin.com", "snapchat.com"];
const fetchable = u => { try { const h = new URL(u).hostname; return FETCH_HOSTS.some(x => h === x || h.endsWith("." + x)); } catch (e) { return false; } };

// ---- version + updates: the Studio publishes /ui/psykick-saver.json {version, notes, config}. A newer version shows a
// "NEW" badge and an Update button in the popup. config = data only (supported sites / link patterns), so most fixes
// for sites changing their pages reach everyone without reinstalling.
const VERSION = chrome.runtime.getManifest().version;
const newer = (a, b) => { const x = a.split(".").map(Number), y = b.split(".").map(Number);
  for (let i = 0; i < 3; i++) { if ((x[i] || 0) !== (y[i] || 0)) return (x[i] || 0) > (y[i] || 0); } return false; };
async function checkUpdate() {
  try {
    const r = await fetch(STUDIO + "/ui/psykick-saver.json", {credentials: "include", cache: "no-store"});
    const j = await r.json();
    await chrome.storage.local.set({latest: j, checked: Date.now()});
    if (j.config && Array.isArray(j.config.fetch_hosts)) FETCH_HOSTS = j.config.fetch_hosts;
    chrome.action.setBadgeText({text: newer(j.version || "0", VERSION) ? "NEW" : ""});
    chrome.action.setBadgeBackgroundColor({color: "#7cc4a8"});
    return j;
  } catch (e) { return null; }
}
chrome.runtime.onStartup.addListener(checkUpdate);
chrome.alarms.create("upd", {periodInMinutes: 360});
chrome.alarms.onAlarm.addListener(a => { if (a.name === "upd") checkUpdate(); });
chrome.storage.local.get("latest").then(({latest}) => { if (latest && latest.config && Array.isArray(latest.config.fetch_hosts)) FETCH_HOSTS = latest.config.fetch_hosts; });

// Runs in the page: the right-clicked element -> that post's own link + its caption / author text.
function findPost(linkUrl) {
  const pats = [/\/video\/\d+/, /\/reels?\/[\w-]+/, /\/p\/[\w-]+/, /\/watch\?v=/, /\/shorts\/[\w-]+/, /\/status\/\d+/,
                /ads\/library\/\?.*id=\d+/, /\/videos\/\d+/, /\/pin\/\d+/, /\/posts\//];
  const good = h => pats.some(p => p.test(h));
  if (linkUrl && good(linkUrl)) return {url: linkUrl, text: ""};
  const el = window.__psykickLast || document.activeElement;
  let n = el, box = null;
  for (let i = 0; i < 18 && n; i++, n = n.parentElement) {
    const as = n.tagName === "A" ? [n] : [];
    if (n.querySelectorAll) as.push(...n.querySelectorAll("a[href]"));
    const hit = as.find(a => good(a.href));
    if (hit) { box = n; return {url: hit.href, text: (box.innerText || "").slice(0, 600)}; }
  }
  if (location.hostname.includes("tiktok.com")) {       // For You feed: build the link from the author + the video id
    n = el;
    for (let i = 0; i < 18 && n; i++, n = n.parentElement) {
      const id = ((n.id || "") + " " + (n.getAttribute && (n.getAttribute("data-video-id") || "") || "")).match(/(\d{17,21})/);
      const au = n.querySelector && n.querySelector('a[href*="/@"]');
      if (id && au) { const user = new URL(au.href).pathname.split("/")[1];
        return {url: `https://www.tiktok.com/${user}/video/${id[1]}`, text: (n.innerText || "").slice(0, 600)}; }
    }
  }
  return {url: good(location.href) ? location.href : "", text: ""};
}

async function savePost(tab, linkUrl) {
  let r = {url: "", text: ""};
  try { [{result: r}] = await chrome.scripting.executeScript({target: {tabId: tab.id}, func: findPost, args: [linkUrl || ""]}); }
  catch (e) { /* page didn't allow scripts */ }
  const url = r && r.url;
  if (!url || !fetchable(url)) { notify("Couldn't find this post's link", "Open the post itself (its own page) and try again, or use 'Select an area'."); return false; }
  let fallback = "";
  try { fallback = await shot(tab); } catch (e) {}
  await post({page_url: url, fetch: "1", data_url: fallback, ad_text: r.text || "", title: tab.title, source: "extension-video"});
  return true;
}

chrome.runtime.onInstalled.addListener(() => {
  checkUpdate();
  chrome.contextMenus.create({id: "img", title: "Save image to Psykick Studio", contexts: ["image"]});
  chrome.contextMenus.create({id: "vid", title: "Save video to Psykick Studio (with source)", contexts: ["video"]});
  chrome.contextMenus.create({id: "post", title: "Save this post's video (with source)", contexts: ["page", "link"],
    documentUrlPatterns: SOCIAL});
  chrome.contextMenus.create({id: "area", title: "Select an area and save it", contexts: ["page", "image", "video", "link"]});
  chrome.contextMenus.create({id: "shot", title: "Save what's on screen", contexts: ["page", "image", "video", "link"]});
});

function notify(title, message) {
  chrome.notifications.create({type: "basic", iconUrl: "icon128.png", title, message});
}

async function post(fields) {
  const fd = new FormData();
  for (const [k, v] of Object.entries(fields)) if (v != null && v !== "") fd.append(k, v);
  const {defaults = {}} = await chrome.storage.local.get("defaults");
  for (const k of ["tags", "note"]) if (defaults[k] && !fields[k]) fd.append(k, defaults[k]);
  let r;
  try { r = await fetch(STUDIO + "/api/studio/library/save", {method: "POST", body: fd, credentials: "include", redirect: "manual"}); }
  catch (e) { notify("Couldn't reach the Studio", "Check your connection and try again."); return; }
  if (r.type === "opaqueredirect" || r.status === 0 || r.status === 302 || r.status === 403 ||
      !(r.headers.get("content-type") || "").includes("json")) {
    notify("Sign in first", "Opening the Studio - sign in, then save again.");
    chrome.tabs.create({url: STUDIO + "/studio"}); return;
  }
  const j = await r.json();
  if (!r.ok) { notify("Not saved", j.error || "Something went wrong."); return; }
  notify(j.duplicate ? "Already in the library" : "Saved to Psykick Studio ✓",
         j.duplicate ? "Someone saved this one before." : j.fetching ? "Downloading the video with its source (author, caption, stats) - it shows under Inspiration in a minute."
         : "The AI is reading it now. Find it under Inspiration.");
  const {recent = []} = await chrome.storage.local.get("recent");
  recent.unshift({id: j.id, ts: Date.now(), page: fields.page_url || ""});
  chrome.storage.local.set({recent: recent.slice(0, 20)});
}

async function shot(tab, rect) {
  const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, {format: "png"});
  if (!rect) return dataUrl;
  // crop in an offscreen canvas (service worker: OffscreenCanvas + createImageBitmap)
  const blob = await (await fetch(dataUrl)).blob();
  const bmp = await createImageBitmap(blob);
  const k = bmp.width / rect.vw;
  const c = new OffscreenCanvas(Math.round(rect.w * k), Math.round(rect.h * k));
  c.getContext("2d").drawImage(bmp, rect.x * k, rect.y * k, rect.w * k, rect.h * k, 0, 0, c.width, c.height);
  const out = await c.convertToBlob({type: "image/png"});
  return await new Promise(res => { const fr = new FileReader(); fr.onload = () => res(fr.result); fr.readAsDataURL(out); });
}

async function selectArea(tab) {
  const [{result}] = await chrome.scripting.executeScript({target: {tabId: tab.id}, func: () => new Promise(resolve => {
    const ov = document.createElement("div");
    ov.style.cssText = "position:fixed;inset:0;z-index:2147483647;cursor:crosshair;background:rgba(18,11,46,.25)";
    const box = document.createElement("div");
    box.style.cssText = "position:fixed;border:2px solid #7cc4a8;background:rgba(124,196,168,.15);display:none";
    const tip = document.createElement("div");
    tip.textContent = "Drag around the ad · Esc to cancel";
    tip.style.cssText = "position:fixed;top:12px;left:50%;transform:translateX(-50%);background:#0f1216;color:#fff;font:14px sans-serif;padding:8px 14px;border-radius:999px";
    ov.append(box, tip); document.documentElement.appendChild(ov);
    let sx, sy;
    const done = v => { ov.remove(); document.removeEventListener("keydown", esc, true); resolve(v); };
    const esc = e => { if (e.key === "Escape") done(null); };
    document.addEventListener("keydown", esc, true);
    ov.addEventListener("mousedown", e => { sx = e.clientX; sy = e.clientY; box.style.display = "block"; });
    ov.addEventListener("mousemove", e => { if (sx == null) return;
      Object.assign(box.style, {left: Math.min(sx, e.clientX) + "px", top: Math.min(sy, e.clientY) + "px",
        width: Math.abs(e.clientX - sx) + "px", height: Math.abs(e.clientY - sy) + "px"}); });
    ov.addEventListener("mouseup", e => {
      const r = {x: Math.min(sx, e.clientX), y: Math.min(sy, e.clientY), w: Math.abs(e.clientX - sx), h: Math.abs(e.clientY - sy), vw: innerWidth};
      ov.style.display = "none"; setTimeout(() => done(r.w > 20 && r.h > 20 ? r : null), 60);
    });
  })});
  if (!result) return;
  const dataUrl = await shot(tab, result);
  await post({data_url: dataUrl, page_url: tab.url, title: tab.title, source: "extension-area"});
}

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId === "post") {
    await savePost(tab, info.linkUrl);
  } else if (info.menuItemId === "img" || info.menuItemId === "vid") {
    const src = info.srcUrl || "";
    const social = fetchable(info.pageUrl || tab.url || "");
    if (info.menuItemId === "vid" && (social || src.startsWith("blob:") || !src)) {   // real video + source via the post link
      if (await savePost(tab, "")) return;
      if (!src || src.startsWith("blob:")) return;
    }
    if (src.startsWith("blob:") || src.startsWith("data:") || !src) {
      notify("This video can't be downloaded directly", "Use 'Select an area and save it' instead.");
      return;
    }
    await post({media_url: src, page_url: info.pageUrl || tab.url, title: tab.title, source: "extension"});
  } else if (info.menuItemId === "area") {
    await selectArea(tab);
  } else if (info.menuItemId === "shot") {
    await post({data_url: await shot(tab), page_url: tab.url, title: tab.title, source: "extension-screen"});
  }
});

chrome.commands.onCommand.addListener(async cmd => {
  if (cmd !== "save-area") return;
  const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
  if (tab) selectArea(tab);
});

chrome.runtime.onMessage.addListener((m, _s, reply) => {
  (async () => {
    const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
    if (m.do === "area") await selectArea(tab);
    if (m.do === "post") await savePost(tab, "");
    if (m.do === "check") { reply({latest: await checkUpdate(), version: VERSION}); return; }
    if (m.do === "reload") { chrome.runtime.reload(); return; }
    if (m.do === "shot") await post({data_url: await shot(tab), page_url: tab.url, title: tab.title, source: "extension-screen", note: m.note, tags: m.tags});
    reply({ok: true});
  })();
  return true;
});
