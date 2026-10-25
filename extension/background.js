// Save to Psykick Studio - context menus + area capture. Uses your Studio sign-in (no keys).
const STUDIO = "https://studio.psykick.cc";

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({id: "img", title: "Save image to Psykick Studio", contexts: ["image"]});
  chrome.contextMenus.create({id: "vid", title: "Save video to Psykick Studio", contexts: ["video"]});
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
         j.duplicate ? "Someone saved this one before." : "The AI is reading it now. Find it under Inspiration.");
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
  if (info.menuItemId === "img" || info.menuItemId === "vid") {
    const src = info.srcUrl || "";
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
    if (m.do === "shot") await post({data_url: await shot(tab), page_url: tab.url, title: tab.title, source: "extension-screen", note: m.note, tags: m.tags});
    reply({ok: true});
  })();
  return true;
});
