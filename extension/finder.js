// Psykick finder: (1) canonical(url) - any shape of a post link -> the clean post link + a dedupe key (mirror of
// canonical() in library_service.py; both run the same table in tests/ext/canon_cases.json); (2) in a page: which post
// does this video / selected area / right-clicked thing belong to? Loaded by the service worker (canonical only) and
// injected into pages (chrome.scripting) before area selection and right-click saves. No DOM access at load time.
(function (G) {
  "use strict";
  if (G.PsykickFind && G.PsykickFind.v === 3) return;

  function canonical(raw) {
    let u;
    try { u = new URL(String(raw || "").trim()); } catch (e) { return null; }
    if (!/^https?:$/.test(u.protocol) || !u.hostname) return null;
    const h = u.hostname.toLowerCase(), path = u.pathname.replace(/\/{2,}/g, "/") || "/", one = k => u.searchParams.get(k) || "";
    const out = (platform, key, url, id, direct) => ({url, key, platform, id: id || "", direct: !!direct});
    let m;
    if (h === "tiktok.com" || h.endsWith(".tiktok.com")) {
      if (h.startsWith("ads.tiktok.com") || path.includes("/creativecenter/")) {
        m = path.match(/\/topads\/(\d{6,})/) || path.match(/\/(\d{15,})/);
        return m ? out("TikTok", "tiktok-cc:" + m[1], `https://ads.tiktok.com/business/creativecenter/topads/${m[1]}/pc/en`, m[1], true) : null;
      }
      if (h.startsWith("library.tiktok.com")) {
        const id = one("ad_id") || one("id");
        return /^\d+$/.test(id) ? out("TikTok", "tiktok-ad:" + id, `https://library.tiktok.com/ads/detail/?ad_id=${id}`, id, true) : null;
      }
      if ((m = path.match(/^\/(@[\w.-]+)\/(video|photo)\/(\d{8,})/))) return out("TikTok", "tiktok:" + m[3], `https://www.tiktok.com/${m[1]}/${m[2]}/${m[3]}`, m[3]);
      if ((m = path.match(/^\/(?:embed(?:\/v2)?|v)\/(\d{8,})/))) return out("TikTok", "tiktok:" + m[1], `https://www.tiktok.com/embed/v2/${m[1]}`, m[1]);
      if (["vm", "vt"].includes(h.split(".")[0]) || path.startsWith("/t/")) return out("TikTok", "url:" + h + path.replace(/\/+$/, ""), `https://${h}${path}`);
      return null;
    }
    if (h.endsWith("instagram.com")) {
      if ((m = path.match(/^\/(?:[\w.]+\/)?(p|reels?|tv)\/([\w-]{5,})/))) {
        const kind = m[1] === "p" ? "p" : m[1].startsWith("reel") ? "reel" : "tv";
        return out("Meta", "instagram:" + m[2], `https://www.instagram.com/${kind}/${m[2]}/`, m[2]);
      }
      if ((m = path.match(/^\/stories\/([\w.]+)\/(\d+)/))) return out("Meta", "ig-story:" + m[2], `https://www.instagram.com/stories/${m[1]}/${m[2]}/`, m[2]);
      return null;
    }
    if (h === "fb.watch" || h.endsWith(".fb.watch")) return path.length > 2 ? out("Meta", "url:fb.watch" + path.replace(/\/+$/, ""), `https://fb.watch${path}`) : null;
    if (h.endsWith("facebook.com")) {
      if (path.startsWith("/ads/library")) { const id = one("id"); return /^\d+$/.test(id) ? out("Meta", "fb-ad:" + id, `https://www.facebook.com/ads/library/?id=${id}`, id) : null; }
      const p0 = path.replace(/\/+$/, "");
      if ((p0 === "/watch" || p0 === "/watch/live") && /^\d+$/.test(one("v"))) return out("Meta", "facebook:" + one("v"), `https://www.facebook.com/watch/?v=${one("v")}`, one("v"));
      if ((m = path.match(/^\/reel\/(\d+)/) || path.match(/^\/(?:[\w.-]+\/)?videos\/(?:[\w.-]+\/)?(\d{6,})/))) return out("Meta", "facebook:" + m[1], `https://www.facebook.com/watch/?v=${m[1]}`, m[1]);
      if ((m = path.match(/^\/share\/([vrp])\/([\w-]+)/))) return out("Meta", `url:fb-share-${m[1]}-${m[2]}`, `https://www.facebook.com/share/${m[1]}/${m[2]}/`);
      if ((m = path.match(/^\/[\w.-]+\/posts\/([\w-]+)/))) return out("Meta", "facebook-post:" + m[1], `https://www.facebook.com${m[0]}/`, m[1]);
      if (path.startsWith("/permalink.php") && one("story_fbid")) return out("Meta", "facebook-post:" + one("story_fbid"), `https://www.facebook.com/permalink.php?story_fbid=${one("story_fbid")}&id=${one("id")}`, one("story_fbid"));
      return null;
    }
    if (h === "youtu.be") { const v = path.replace(/^\/+/, "").split("/")[0]; return /^[\w-]{11}$/.test(v) ? out("YouTube", "youtube:" + v, `https://www.youtube.com/watch?v=${v}`, v) : null; }
    if (h.endsWith("youtube.com")) {
      if ((m = path.match(/^\/(shorts|embed|live)\/([\w-]{11})/))) return out("YouTube", "youtube:" + m[2], m[1] === "shorts" ? `https://www.youtube.com/shorts/${m[2]}` : `https://www.youtube.com/watch?v=${m[2]}`, m[2]);
      if (path.replace(/\/+$/, "") === "/watch" && /^[\w-]{11}$/.test(one("v"))) return out("YouTube", "youtube:" + one("v"), `https://www.youtube.com/watch?v=${one("v")}`, one("v"));
      return null;
    }
    if (["x.com", "twitter.com", "mobile.twitter.com", "www.x.com", "www.twitter.com", "mobile.x.com"].includes(h)) {
      m = path.match(/^\/(\w{1,15})\/status(?:es)?\/(\d{5,})/);
      return m ? out("X", "x:" + m[2], `https://x.com/${m[1]}/status/${m[2]}`, m[2]) : null;
    }
    if (h.endsWith("snapchat.com")) {
      if ((m = path.match(/^\/spotlight\/([\w-]{10,})/))) return out("Snapchat", "snapchat:" + m[1], `https://www.snapchat.com/spotlight/${m[1]}`, m[1]);
      if (path.startsWith("/t/") || h.startsWith("story.")) return out("Snapchat", "url:" + h + path.replace(/\/+$/, ""), `https://${h}${path}`);
      return null;
    }
    if (h === "pin.it") return path.length > 2 ? out("Pinterest", "url:pin.it" + path.replace(/\/+$/, ""), `https://pin.it${path}`) : null;
    if (/(^|\.)pinterest\.[a-z.]+$/.test(h)) {
      m = path.match(/^\/pin\/(?:[\w-]*--)?(\d{6,})/);
      return m ? out("Pinterest", "pinterest:" + m[1], `https://www.pinterest.com/pin/${m[1]}/`, m[1]) : null;
    }
    if (h.endsWith("linkedin.com")) {
      m = path.match(/urn:li:(?:activity|ugcPost):(\d+)/) || path.match(/activity-(\d{10,})/);
      return m ? out("LinkedIn", "linkedin:" + m[1], `https://www.linkedin.com/feed/update/urn:li:activity:${m[1]}/`, m[1]) : null;
    }
    return null;
  }

  // ---------------- in-page part ----------------
  const box = el => { const r = el.getBoundingClientRect(); return {x: r.left, y: r.top, w: r.width, h: r.height}; };
  const area = r => Math.max(0, r.w) * Math.max(0, r.h);
  const overlap = (a, b) => Math.max(0, Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x)) *
                            Math.max(0, Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y));
  const dist = (a, b) => {                       // 0 when they touch, else the gap between the two boxes
    const dx = Math.max(0, Math.max(a.x, b.x) - Math.min(a.x + a.w, b.x + b.w));
    const dy = Math.max(0, Math.max(a.y, b.y) - Math.min(a.y + a.h, b.y + b.h));
    return Math.hypot(dx, dy);
  };
  const shown = el => { const r = box(el); return r.w > 30 && r.h > 30 && r.y + r.h > 0 && r.x + r.w > 0 && r.y < innerHeight && r.x < innerWidth; };
  const httpSrc = v => { const s = v.currentSrc || v.src || (v.querySelector && (v.querySelector("source[src]") || {}).src) || "";
                         return /^https?:/.test(s) && !/\.m3u8|\.mpd/.test(s) ? s : ""; };
  const isBlob = v => { const s = v.currentSrc || v.src || ""; return !s || s.startsWith("blob:") || s.startsWith("mediasource:"); };
  const text = el => ((el && el.innerText) || "").replace(/\s+\n/g, "\n").slice(0, 600);

  // TikTok For You / Following: no /video/ link in the card - the id sits in the player's id ("xgwrapper-0-<id>"),
  // data-video-id, or a data-e2e container; the author in a "/@name" link.
  function tiktokCard(n) {
    if (!n.querySelectorAll) return null;
    const ids = [n, ...n.querySelectorAll("[id*='-7'],[id^='xgwrapper'],[data-video-id],[data-item-id],[id^='one-column-item']")];
    let id = "";
    for (const el of ids) {
      const s = (el.id || "") + " " + (el.getAttribute("data-video-id") || "") + " " + (el.getAttribute("data-item-id") || "");
      const m = s.match(/(?:^|\D)(\d{18,20})(?:\D|$)/);
      if (m) { id = m[1]; break; }
    }
    if (!id) return null;
    const au = n.querySelector("a[data-e2e='video-author-avatar'],a[data-e2e='video-author-uniqueid'],a[href^='/@'],a[href*='tiktok.com/@']");
    const user = au ? (new URL(au.getAttribute("href"), "https://www.tiktok.com").pathname.split("/")[1] || "") :
      ((n.querySelector("[data-e2e='video-author-uniqueid']") || {}).textContent || "").trim().replace(/^@?/, "@");
    if (!/^@[\w.-]+$/.test(user)) return null;
    return canonical(`https://www.tiktok.com/${user}/video/${id}`);
  }

  // Meta Ad Library cards have no link to the ad, only "Library ID: 1234..." text.
  function adLibraryId(n) {
    if (!/facebook\.com$/.test(location.hostname) || !location.pathname.startsWith("/ads/library")) return null;
    const t = (n.innerText || "");
    if (t.length > 6000) return null;
    const ids = [...new Set([...t.matchAll(/(?:Library ID|ID de la biblioteca|ID da biblioteca|ID библиотеки|ID)[:：]?\s*(\d{12,20})/g)].map(m => m[1]))];
    return ids.length === 1 ? canonical(`https://www.facebook.com/ads/library/?id=${ids[0]}`) : null;
  }

  // Post links inside a node -> Map(key -> {c, el})
  function postsIn(n) {
    const found = new Map();
    const add = (c, el, how) => { if (c && !found.has(c.key)) found.set(c.key, {c, el, how}); };
    if (n.tagName === "A" && n.href) add(canonical(n.href), n, "link");
    if (n.querySelectorAll) for (const a of n.querySelectorAll("a[href]")) add(canonical(a.href), a, "link");
    if (!found.size) { const t = tiktokCard(n); if (t) add(t, n, "tiktok-card"); }
    if (!found.size) { const f = adLibraryId(n); if (f) add(f, n, "adlib-id"); }
    return found;
  }

  // Walk up from the thing (video / clicked element) until the smallest container that holds a post link.
  // Several links at that level (a feed) -> the one nearest the thing; too far away -> none.
  function postFrom(start, focus) {
    let n = start;
    for (let i = 0; i < 28 && n && n !== document.documentElement; i++, n = n.parentElement) {
      const found = postsIn(n);
      if (!found.size) continue;
      let best = null, bd = Infinity;
      for (const x of found.values()) {
        const d = x.el === n ? 0 : dist(box(x.el), focus);
        if (d < bd) { bd = d; best = x; }
      }
      if (best && (found.size === 1 || bd < 420)) return {c: best.c, container: n, how: best.how};
      return null;                       // links here belong to other posts
    }
    return null;
  }

  function ogPost() {
    const m = document.querySelector("meta[property='og:url'],link[rel='canonical']");
    return m ? canonical(m.content || m.href) : null;
  }

  // The selected rectangle (viewport px) -> what's in it. kind: video | image | page.
  function inRect(rect) {
    const small = rect.w < 24 && rect.h < 24;              // a click, not a drag: the thing under the pointer
    const vids = [...document.querySelectorAll("video")].filter(shown).map(v => {
      const b = box(v), ov = overlap(rect, b);
      return {v, b, ov, score: small ? (ov > 0 ? 1 : 0) : Math.max(ov / Math.max(1, area(b)), ov / Math.max(1, area(rect)))};
    }).filter(x => x.score >= (small ? 1 : 0.3)).sort((a, b) => b.score - a.score || (!b.v.paused) - (!a.v.paused));
    let v = vids.length ? vids[0] : null;
    if (!v && !small) {                                     // overlays (canvas, divs) on top of the feed's playing video
      const p = [...document.querySelectorAll("video")].filter(x => shown(x) && !x.paused && overlap(rect, box(x)) > 0);
      if (p.length === 1) v = {v: p[0], b: box(p[0])};
    }
    const cx = rect.x + rect.w / 2, cy = rect.y + rect.h / 2;
    if (v) return describeVideo(v.v, v.b);
    const px = Math.min(innerWidth - 1, Math.max(0, cx)), py = Math.min(innerHeight - 1, Math.max(0, cy));
    const under = document.elementFromPoint(px, py);
    if (small && under) {                                   // clicked an overlay on top of a player: the video in that card
      const stack = document.elementsFromPoint(px, py).find(e => e.tagName === "VIDEO");
      const card = under.closest("article,[role=article],[data-e2e*='item'],[data-testid=cellInnerDiv],ytd-reel-video-renderer,section,li");
      const cv = stack || (card && card.querySelector("video"));
      if (cv && shown(cv)) return describeVideo(cv);
    }
    const p = under ? postFrom(under, rect) : null;
    const img = under && (under.tagName === "IMG" ? under : under.querySelector && under.querySelector("img"));
    return {kind: img || p ? "image" : "page", post_url: p ? p.c.url : "", platform: p ? p.c.platform : "",
            text: p ? text(p.container) : "", detected: p ? p.how : "", box: null,
            media_url: img && /^https?:/.test(img.currentSrc || img.src) ? (img.currentSrc || img.src) : ""};
  }

  function describeVideo(video, b) {
    b = b || box(video);
    let p = postFrom(video, b), how = p ? p.how : "";
    if (!p) {                                                // the post's own page / modal: its address is the link
      const here = canonical(location.href) || (document.querySelectorAll("video").length <= 2 ? ogPost() : null);
      if (here) { p = {c: here, container: video.closest("article,[role=dialog],main") || document.body}; how = "location"; }
    }
    const author = p && p.c.platform === "TikTok" ? ((p.c.url.match(/\/(@[\w.-]+)\//) || [])[1] || "") : "";
    return {kind: "video", post_url: p ? p.c.url : "", post_key: p ? p.c.key : "", platform: p ? p.c.platform : "",
            media_url: httpSrc(video), blob: isBlob(video), poster: video.poster || "", text: p ? text(p.container) : "",
            author, detected: how, box: b};
  }

  // Right-click: the remembered element (lastclick.js) -> its video / post.
  function forElement(el, linkUrl) {
    const c = canonical(linkUrl || "");
    if (c) return {kind: "video", post_url: c.url, post_key: c.key, platform: c.platform, detected: "link", media_url: "", box: null};
    el = el || document.activeElement || document.body;
    const video = el.tagName === "VIDEO" ? el : (el.closest && (el.closest("article,[role=article],[data-e2e*='item'],li,section") || el)).querySelector?.("video");
    if (video && shown(video)) return describeVideo(video);
    const p = postFrom(el, box(el));
    return {kind: p ? "image" : "page", post_url: p ? p.c.url : "", platform: p ? p.c.platform : "", detected: p ? p.how : "",
            text: p ? text(p.container) : "", media_url: "", box: box(el)};
  }

  // Popup's "Save this page's video": the page is a post, or the video playing right now in the middle of the screen.
  function pagePost() {
    const here = canonical(location.href);
    const playing = [...document.querySelectorAll("video")].filter(v => shown(v))
      .sort((a, b) => (!b.paused) - (!a.paused) || overlap(box(b), {x: 0, y: 0, w: innerWidth, h: innerHeight}) - overlap(box(a), {x: 0, y: 0, w: innerWidth, h: innerHeight}))[0];
    if (playing) { const d = describeVideo(playing); if (d.post_url) return d; }
    if (here) return {kind: "video", post_url: here.url, post_key: here.key, platform: here.platform, detected: "location", media_url: "", box: null};
    const og = ogPost();
    return og ? {kind: "video", post_url: og.url, post_key: og.key, platform: og.platform, detected: "og", media_url: "", box: null}
              : {kind: "page", post_url: "", box: null};
  }

  G.PsykickFind = {v: 3, canonical, inRect, forElement, pagePost, postFrom};
  if (typeof module !== "undefined" && module.exports) module.exports = G.PsykickFind;
})(typeof globalThis !== "undefined" ? globalThis : self);
