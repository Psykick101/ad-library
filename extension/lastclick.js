// Remembers what was right-clicked, so "Save video" can find that post's own link (TikTok/Reels videos are blob: and
// can't be downloaded from the page - the Ad Library downloads them from the post link instead, with the source).
document.addEventListener("contextmenu", e => { window.__psykickLast = e.target; }, true);
