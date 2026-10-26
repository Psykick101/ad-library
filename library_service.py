"""Ad Library - our own swipe file (microservice, separate from the lab and the Studio).

Saves ads the team finds anywhere (BigSpy, PiPiAds, Meta Ad Library, TikTok, any site) via the "Save to Psykick Studio"
Chrome extension or the Studio, plus search in Foreplay's database while we still have Foreplay. Each saved ad gets an
AI read (what it sells, hook, style, layout, text) so the Studio's creative AI can learn from it.

Videos with their source: the extension (or "Paste a link", or the phone bots) sends the post's link with fetch=1.
2026-10-27 pipeline: canonical(link) -> one row with status "queued" (+ the screenshot as its thumbnail) and a job in the
`jobs` table -> background workers download it (yt-dlp, TikTok via curl_cffi impersonation, h264 mp4 <=1080p, or the
video file's own address for sites yt-dlp can't read) -> "downloading" -> "reading" (AI) -> "done", or "failed" with a
reason a person understands; network-type failures retry 3x with backoff. The same post saved twice (any link shape)
is one row. GET /v1/status?ids=... is what the extension and the Inspiration "Saving..." cards poll.

Runs on 127.0.0.1:8775, own SQLite (/var/lib/ad-library/library.db), media in Nextcloud Creative/Inspiration/.
Only the Studio talks to it (shared key /etc/ai-secrets/ad-library.key in header X-Library-Key, user in X-User).
Foreplay key (optional): /etc/ai-secrets/foreplay.env FOREPLAY_API_KEY=...; monthly credit cap FOREPLAY_MONTHLY_CAP
(default 8000 - stays inside the 10,000 credits every Foreplay plan includes).
"""
import base64
import datetime as dt
import hashlib
import io
import json
import os
import pathlib
import re
import sqlite3
import threading
import time
import uuid

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

PORT = int(os.getenv("LIBRARY_PORT", "8775"))
DB = pathlib.Path(os.getenv("LIBRARY_DB", "/var/lib/ad-library/library.db"))
MEDIA = pathlib.Path(os.getenv("LIBRARY_MEDIA", "/data/nextcloud/data/Uniclicks/files/Creative/Inspiration"))
KEY_FILE = pathlib.Path(os.getenv("LIBRARY_KEY", "/etc/ai-secrets/ad-library.key"))
FP_ENV = pathlib.Path(os.getenv("FOREPLAY_ENV", "/etc/ai-secrets/foreplay.env"))
LLM_URL = os.getenv("LIBRARY_LLM_URL", "http://127.0.0.1:4000/v1/chat/completions")
LLM_KEY = pathlib.Path(os.getenv("LIBRARY_LLM_KEY", "/etc/ai-secrets/litellm-ad-library.key"))
MAX_BYTES = 120 * 2**20
EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif", "video/mp4": ".mp4",
       "video/webm": ".webm", "video/quicktime": ".mov"}
PLATFORMS = {"facebook.com": "Meta", "instagram.com": "Meta", "tiktok.com": "TikTok", "bigspy.com": "BigSpy",
             "pipiads.com": "PiPiAds", "foreplay.co": "Foreplay", "youtube.com": "YouTube", "google.com": "Google",
             "snapchat.com": "Snapchat", "linkedin.com": "LinkedIn", "x.com": "X", "pinterest.com": "Pinterest"}
_lk = threading.Lock()
_con = None


def q(sql, args=(), one=False):
    global _con
    with _lk:
        if _con is None:
            DB.parent.mkdir(parents=True, exist_ok=True)
            _con = sqlite3.connect(DB, check_same_thread=False)
            _con.row_factory = sqlite3.Row
            _con.executescript("""
            create table if not exists ads(id text primary key, ts real, by text, source text, platform text, page_url text,
              media text, kind text, sha text, title text, brand text, ad_text text, tags text, note text, ai text,
              foreplay_id text, extra text);
            create index if not exists ads_ts on ads(ts);
            create table if not exists usage(month text primary key, foreplay_credits int default 0);
            create table if not exists folders(id text primary key, name text, parent text default '', by text, ts real);
            create table if not exists ad_folders(ad_id text, folder_id text, ts real, primary key(ad_id, folder_id));
            create table if not exists jobs(id text primary key, ad_id text, mode text, url text, media_url text, referer text,
              status text, tries int default 0, error text default '', created real, updated real, next_at real default 0);
            create index if not exists jobs_due on jobs(status, next_at);
            """)
            _migrate(_con)
        cur = _con.execute(sql, args)
        _con.commit()
        rows = [dict(r) for r in cur.fetchall()]
    return (rows[0] if rows else None) if one else rows


def _migrate(con) -> None:
    """2026-10-27: canon (dedupe key of the post: "tiktok:<id>", "instagram:<code>"...) + status (queued / downloading /
    reading / done / failed) on every save. Old rows get both filled in once."""
    cols = [r[1] for r in con.execute("pragma table_info(ads)")]
    if "canon" not in cols:
        con.execute("alter table ads add column canon text default ''")
        con.execute("alter table ads add column status text default ''")
        for r in con.execute("select id, page_url, media, extra from ads").fetchall():
            c = canonical(r[1] or "")
            ex = json.loads(r[3] or "{}")
            st = "failed" if ex.get("fetch") == "failed" else ("done" if r[2] else "queued" if ex.get("fetch") == "working" else "")
            con.execute("update ads set canon=?, status=? where id=?", (c["key"] if c else "", st, r[0]))
        con.execute("create index if not exists ads_canon on ads(canon)")
    con.commit()


def _id() -> str:
    return uuid.uuid4().hex[:12]


COLS = ("id", "ts", "by", "source", "platform", "page_url", "media", "kind", "sha", "title", "brand", "ad_text", "tags",
        "note", "ai", "foreplay_id", "extra", "canon", "status")


def insert_ad(**v) -> str:
    v.setdefault("id", _id())
    v.setdefault("ts", time.time())
    row = {c: v.get(c, "") for c in COLS}
    row["extra"] = row["extra"] or "{}"
    row["tags"] = row["tags"] or "[]"
    q(f"insert into ads({', '.join(COLS)}) values({', '.join('?' * len(COLS))})", tuple(row[c] for c in COLS))
    return row["id"]


def platform_of(url: str) -> str:
    host = re.sub(r"^www\.", "", (httpx.URL(url).host if url and url.startswith("http") else "") or "")
    return next((v for k, v in PLATFORMS.items() if host.endswith(k)), host or "other")


def _scan(p: pathlib.Path) -> None:
    try:
        import subprocess
        rel = "Uniclicks/files/" + str(p.relative_to("/data/nextcloud/data/Uniclicks/files"))
        subprocess.run(["docker", "exec", "-u", "www-data", "nextcloud", "php", "occ", "files:scan", "--path=" + rel],
                       capture_output=True, timeout=120)
    except Exception:  # noqa: BLE001
        pass


def store(data: bytes, ctype: str, platform: str) -> tuple[str, str, str]:
    """-> (path, kind, sha). Same file saved twice is kept once."""
    if len(data) > MAX_BYTES:
        raise ValueError("file too big (max 120 MB)")
    ext = EXT.get(ctype.split(";")[0].strip().lower())
    if not ext:
        raise ValueError(f"not a picture or video ({ctype})")
    sha = hashlib.sha256(data).hexdigest()[:20]
    d = MEDIA / re.sub(r"[^\w-]+", "-", platform or "other") / dt.date.today().strftime("%Y-%m")
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{sha}{ext}"
    if not p.exists():
        p.write_bytes(data)
    return str(p), ("video" if ext in (".mp4", ".webm", ".mov") else "image"), sha


# ---------- video + source from a post link (yt-dlp) ----------
FETCH_HOSTS = ("tiktok.com", "instagram.com", "facebook.com", "fb.watch", "youtube.com", "youtu.be", "x.com",
               "twitter.com", "snapchat.com", "pinterest.com", "pin.it", "linkedin.com", "vm.tiktok.com")
FFMPEG = os.getenv("FFMPEG", "/usr/local/bin/ffmpeg" if pathlib.Path("/usr/local/bin/ffmpeg").exists() else "ffmpeg")
MAX_TRIES = 3


def fetchable(url: str) -> bool:
    try:
        host = httpx.URL(url).host or ""
    except Exception:  # noqa: BLE001
        return False
    return url.startswith("http") and (any(host == h or host.endswith("." + h) for h in FETCH_HOSTS)
                                       or bool(re.search(r"(^|\.)pinterest\.[a-z.]+$", host)))


def canonical(url: str) -> dict | None:
    """A post link in any of its shapes -> {"url": the clean post link, "key": dedupe key, "platform", "id",
    "direct": True when yt-dlp can't read that site (TikTok Creative Center / TikTok ad library: the extension sends the
    video file's own address along)}. None = not a post (a feed, a profile, a search...).
    Mirrors canonical() in extension/finder.js - keep both in step (tests: test_library.py + tests/ext)."""
    from urllib.parse import parse_qs, urlsplit
    try:
        u = urlsplit((url or "").strip())
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or not u.hostname:
        return None
    h, path, qs = u.hostname.lower(), re.sub(r"/{2,}", "/", u.path or "/"), parse_qs(u.query)
    one = lambda k: (qs.get(k) or [""])[0]  # noqa: E731

    def out(plat, key, clean, pid="", direct=False):
        return {"url": clean, "key": key, "platform": plat, "id": pid, "direct": direct}
    if h == "tiktok.com" or h.endswith(".tiktok.com"):
        if h.startswith("ads.tiktok.com") or "/creativecenter/" in path:
            m = re.search(r"/topads/(\d{6,})", path) or re.search(r"/(\d{15,})", path)
            if m:
                return out("TikTok", f"tiktok-cc:{m[1]}", f"https://ads.tiktok.com/business/creativecenter/topads/{m[1]}/pc/en", m[1], True)
            return None
        if h.startswith("library.tiktok.com"):
            aid = one("ad_id") or one("id")
            return out("TikTok", f"tiktok-ad:{aid}", f"https://library.tiktok.com/ads/detail/?ad_id={aid}", aid, True) if aid.isdigit() else None
        m = re.match(r"/(@[\w.-]+)/(video|photo)/(\d{8,})", path)
        if m:
            return out("TikTok", f"tiktok:{m[3]}", f"https://www.tiktok.com/{m[1]}/{m[2]}/{m[3]}", m[3])
        m = re.match(r"/(?:embed(?:/v2)?|v)/(\d{8,})", path)
        if m:
            return out("TikTok", f"tiktok:{m[1]}", f"https://www.tiktok.com/embed/v2/{m[1]}", m[1])
        if h.split(".")[0] in ("vm", "vt") or path.startswith("/t/"):
            return out("TikTok", "url:" + h + path.rstrip("/"), f"https://{h}{path}", "")
        return None
    if h.endswith("instagram.com"):
        m = re.match(r"/(?:[\w.]+/)?(p|reels?|tv)/([\w-]{5,})", path)
        if m:
            kind = "p" if m[1] == "p" else "reel" if m[1].startswith("reel") else "tv"
            return out("Meta", f"instagram:{m[2]}", f"https://www.instagram.com/{kind}/{m[2]}/", m[2])
        m = re.match(r"/stories/([\w.]+)/(\d+)", path)
        if m:
            return out("Meta", f"ig-story:{m[2]}", f"https://www.instagram.com/stories/{m[1]}/{m[2]}/", m[2])
        return None
    if h == "fb.watch" or h.endswith(".fb.watch"):
        return out("Meta", "url:fb.watch" + path.rstrip("/"), f"https://fb.watch{path}", "") if len(path) > 2 else None
    if h.endswith("facebook.com"):
        if path.startswith("/ads/library"):
            aid = one("id")
            return out("Meta", f"fb-ad:{aid}", f"https://www.facebook.com/ads/library/?id={aid}", aid) if aid.isdigit() else None
        if path.rstrip("/") in ("/watch", "/watch/live") and one("v").isdigit():
            return out("Meta", f"facebook:{one('v')}", f"https://www.facebook.com/watch/?v={one('v')}", one("v"))
        m = re.match(r"/reel/(\d+)", path) or re.match(r"/(?:[\w.-]+/)?videos/(?:[\w.-]+/)?(\d{6,})", path)
        if m:
            return out("Meta", f"facebook:{m[1]}", f"https://www.facebook.com/watch/?v={m[1]}", m[1])
        m = re.match(r"/share/([vrp])/([\w-]+)", path)
        if m:
            return out("Meta", f"url:fb-share-{m[1]}-{m[2]}", f"https://www.facebook.com/share/{m[1]}/{m[2]}/", "")
        m = re.match(r"/[\w.-]+/posts/([\w-]+)", path)
        if m:
            return out("Meta", f"facebook-post:{m[1]}", f"https://www.facebook.com{m[0]}/", m[1])
        if path.startswith("/permalink.php") and one("story_fbid"):
            return out("Meta", f"facebook-post:{one('story_fbid')}", f"https://www.facebook.com/permalink.php?story_fbid={one('story_fbid')}&id={one('id')}", one("story_fbid"))
        return None
    if h == "youtu.be":
        vid = path.strip("/").split("/")[0]
        return out("YouTube", f"youtube:{vid}", f"https://www.youtube.com/watch?v={vid}", vid) if re.fullmatch(r"[\w-]{11}", vid) else None
    if h.endswith("youtube.com"):
        m = re.match(r"/(shorts|embed|live)/([\w-]{11})", path)
        if m:
            return out("YouTube", f"youtube:{m[2]}", (f"https://www.youtube.com/shorts/{m[2]}" if m[1] == "shorts" else
                                                      f"https://www.youtube.com/watch?v={m[2]}"), m[2])
        if path.rstrip("/") == "/watch" and re.fullmatch(r"[\w-]{11}", one("v")):
            return out("YouTube", f"youtube:{one('v')}", f"https://www.youtube.com/watch?v={one('v')}", one("v"))
        return None
    if h in ("x.com", "twitter.com", "mobile.twitter.com", "www.x.com", "www.twitter.com", "mobile.x.com"):
        m = re.match(r"/([\w]{1,15})/status(?:es)?/(\d{5,})", path)
        return out("X", f"x:{m[2]}", f"https://x.com/{m[1]}/status/{m[2]}", m[2]) if m else None
    if h.endswith("snapchat.com"):
        m = re.match(r"/spotlight/([\w-]{10,})", path)
        if m:
            return out("Snapchat", f"snapchat:{m[1]}", f"https://www.snapchat.com/spotlight/{m[1]}", m[1])
        if path.startswith("/t/") or h.startswith("story."):
            return out("Snapchat", "url:" + h + path.rstrip("/"), f"https://{h}{path}", "")
        return None
    if h == "pin.it":
        return out("Pinterest", "url:pin.it" + path.rstrip("/"), f"https://pin.it{path}", "") if len(path) > 2 else None
    if re.search(r"(^|\.)pinterest\.[a-z.]+$", h):
        m = re.match(r"/pin/(?:[\w-]*--)?(\d{6,})", path)
        return out("Pinterest", f"pinterest:{m[1]}", f"https://www.pinterest.com/pin/{m[1]}/", m[1]) if m else None
    if h.endswith("linkedin.com"):
        m = re.search(r"urn:li:(?:activity|ugcPost):(\d+)", path) or re.search(r"activity-(\d{10,})", path)
        if m:
            return out("LinkedIn", f"linkedin:{m[1]}", f"https://www.linkedin.com/feed/update/urn:li:activity:{m[1]}/", m[1])
        return None
    return None


def _ytdlp_opts(td: str) -> dict:
    h264 = "[vcodec~='^(avc|h264)']"             # browsers play h264 everywhere; TikTok's h265 doesn't play in Chrome
    return {"outtmpl": f"{td}/v.%(ext)s", "quiet": True, "no_warnings": True, "noplaylist": True, "playlist_items": "1",
            "format": (f"bv*[height<=1080][ext=mp4]{h264}+ba[ext=m4a]/b[height<=1080][ext=mp4]{h264}/"
                       "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]/bv*[height<=1080]+ba/b[height<=1080]/b"),
            "merge_output_format": "mp4", "max_filesize": MAX_BYTES, "socket_timeout": 30, "retries": 2,
            "ffmpeg_location": FFMPEG}


def fetch_source(url: str) -> tuple[bytes, str, dict]:
    """-> (video bytes, content type, source facts). Raises with yt-dlp's reason (explain() makes it human)."""
    import tempfile
    import yt_dlp
    with tempfile.TemporaryDirectory() as td:
        opts = _ytdlp_opts(td)
        try:                                     # TikTok (and sometimes Instagram) need a browser-like client: curl_cffi
            import curl_cffi  # noqa: F401,PLC0415
            from yt_dlp.networking.impersonate import ImpersonateTarget
            opts["impersonate"] = ImpersonateTarget("chrome")
        except Exception:  # noqa: BLE001
            pass
        try:
            with yt_dlp.YoutubeDL(opts) as y:
                info = y.extract_info(url, download=True)
        except Exception:  # noqa: BLE001
            if "impersonate" not in opts:
                raise
            opts.pop("impersonate")
            with yt_dlp.YoutubeDL(opts) as y2:
                info = y2.extract_info(url, download=True)
        if info.get("entries"):
            info = (info["entries"] or [{}])[0]
        files = sorted(pathlib.Path(td).glob("v.*"), key=lambda f: f.stat().st_size, reverse=True)
        if not files:
            raise ValueError("no video in that post")
        f = files[0]
        ctype = {".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime", ".jpg": "image/jpeg",
                 ".png": "image/png", ".webp": "image/webp"}.get(f.suffix.lower(), "video/mp4")
        facts = {k: info.get(k) for k in ("webpage_url", "uploader", "uploader_id", "uploader_url", "channel", "title",
                                          "description", "view_count", "like_count", "comment_count", "repost_count",
                                          "upload_date", "timestamp", "duration", "track", "artist", "thumbnail",
                                          "width", "height", "id")
                 if info.get(k) not in (None, "")}
        return f.read_bytes(), ctype, facts


def explain(err: Exception | str) -> tuple[str, bool]:
    """yt-dlp / network error -> (a reason a person understands, worth retrying?)."""
    e = str(err).lower()
    rules = [
        (("unsupported url",), "This kind of link isn't supported - open the post itself and save from there", False),
        (("private", "only available for registered", "this account is private"), "The post is private", False),
        (("login required", "log in", "requires authentication", "use --cookies", "cookies"),
         "The site wants a login to show this post (common on Instagram/Facebook) - we'll keep the screenshot", False),
        (("not available", "removed", "deleted", "does not exist", "404", "no video in that post", "no video formats"),
         "The post was deleted, isn't public, or has no video", False),
        (("larger than max-filesize", "file too big", "max-filesize"), "The video is over 120 MB", False),
        (("impersonat", "curl_cffi"), "TikTok blocked the download (the server needs curl_cffi) - retrying", True),
        (("429", "rate", "too many requests"), "The site is rate-limiting us - retrying in a bit", True),
        (("timed out", "timeout", "connection", "temporarily", "403", "blocked", "502", "503", "network"),
         "The site didn't answer (network / blocked) - retrying", True),
    ]
    for keys, msg, retry in rules:
        if any(k in e for k in keys):
            return msg, retry
    return "Couldn't download it: " + str(err).strip().splitlines()[0][:140] if str(err).strip() else "Couldn't download it", True


def _set(aid: str, status: str, **extra) -> None:
    a = q("select extra from ads where id=?", (aid,), one=True)
    if not a:
        return
    ex = {**json.loads(a["extra"] or "{}"), **extra,
          "fetch": {"done": "done", "failed": "failed"}.get(status, "working")}      # old Studio pages read extra.fetch
    q("update ads set status=?, extra=? where id=?", (status, json.dumps(ex), aid))


def _thumb(video_path: str) -> str:
    """First-second frame of a video as a jpg next to it ("" if ffmpeg isn't there)."""
    import subprocess
    out = str(pathlib.Path(video_path).with_suffix(".jpg"))
    try:
        if not pathlib.Path(out).exists():
            subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-ss", "0.5", "-i", video_path, "-frames:v", "1",
                            "-vf", "scale='min(720,iw)':-2", out], timeout=60, check=True, capture_output=True)
        return out if pathlib.Path(out).exists() else ""
    except Exception:  # noqa: BLE001
        return ""


_wake = threading.Event()
_job_lk = threading.Lock()


def enqueue(aid: str, mode: str, url: str, media_url: str = "", referer: str = "") -> str:
    jid = _id()
    now = time.time()
    q("insert into jobs(id, ad_id, mode, url, media_url, referer, status, tries, error, created, updated, next_at) "
      "values(?,?,?,?,?,?,?,?,?,?,?,?)", (jid, aid, mode, url, media_url, referer, "queued", 0, "", now, now, 0))
    _set(aid, "queued")
    _wake.set()
    return jid


def work_once() -> bool:
    """Runs ONE due download job (yt-dlp for post links, a plain download for a video file's own address).
    -> False when nothing was due. The workers loop on this; the tests call it directly."""
    with _job_lk:
        j = q("select * from jobs where status='queued' and next_at<=? order by created limit 1", (time.time(),), one=True)
        if not j:
            return False
        q("update jobs set status='downloading', tries=tries+1, updated=? where id=?", (time.time(), j["id"]))
    aid = j["ad_id"]
    a = q("select * from ads where id=?", (aid,), one=True)
    if not a:
        q("update jobs set status='gone', updated=? where id=?", (time.time(), j["id"]))
        return True
    _set(aid, "downloading")
    try:
        if j["mode"] == "direct":
            data, ctype = _download(j["media_url"], j["referer"] or j["url"])
            if "octet-stream" in ctype or not ctype:
                ctype = "video/mp4"
            facts = {}
        else:
            data, ctype, facts = fetch_source(j["url"])
        path, kind, sha = store(data, ctype, a.get("platform") or platform_of(j["url"]))
        dup = q("select id from ads where sha=? and id<>?", (sha, aid), one=True)
        if dup:                                  # same file already in the library (saved from another link)
            q("delete from ads where id=?", (aid,))
            q("delete from ad_folders where ad_id=?", (aid,))
            q("update jobs set status='done', error=?, updated=? where id=?", ("duplicate of " + dup["id"], time.time(), j["id"]))
            return True
        ex = json.loads(a.get("extra") or "{}")
        th = _thumb(path) if kind == "video" else ""
        ex.update({"source": facts, **({"thumb": th} if th else {}), "fetched_from": j["mode"]})
        desc = str(facts.get("description") or facts.get("title") or "")
        page = str(facts.get("webpage_url") or a.get("page_url") or j["url"])[:500]
        q("update ads set media=?, kind=?, sha=?, brand=coalesce(nullif(brand,''), ?), ad_text=coalesce(nullif(ad_text,''), ?), "
          "title=coalesce(nullif(title,''), ?), page_url=?, extra=? where id=?",
          (path, kind, sha, str(facts.get("uploader") or facts.get("channel") or "")[:120], desc[:2000],
           str(facts.get("title") or "")[:200], page, json.dumps(ex), aid))
        threading.Thread(target=_scan, args=(pathlib.Path(path).parent,), daemon=True).start()
        _set(aid, "reading")
        ai_read(aid)
        _set(aid, "done", fetch_error="")
        q("update jobs set status='done', error='', updated=? where id=?", (time.time(), j["id"]))
    except Exception as e:  # noqa: BLE001
        reason, retry = explain(e)
        tries = (j["tries"] or 0) + 1
        if retry and tries < MAX_TRIES:
            q("update jobs set status='queued', error=?, next_at=?, updated=? where id=?",
              (reason, time.time() + 20 * tries * tries, time.time(), j["id"]))
            _set(aid, "queued", fetch_error=reason, tries=tries)
        else:
            q("update jobs set status='failed', error=?, updated=? where id=?", (reason, time.time(), j["id"]))
            _set(aid, "failed", fetch_error=reason, tries=tries)
    return True


def retry(aid: str) -> bool:
    j = q("select * from jobs where ad_id=? order by created desc limit 1", (aid,), one=True)
    if not j or j["status"] not in ("failed", "queued"):
        return False
    q("update jobs set status='queued', tries=0, next_at=0, error='', updated=? where id=?", (time.time(), j["id"]))
    _set(aid, "queued", fetch_error="")
    _wake.set()
    return True


def workers(n: int = 2) -> None:
    q("update jobs set status='queued', next_at=0 where status='downloading'")       # a restart mid-download: run it again

    def loop():
        while True:
            try:
                if work_once():
                    continue
            except Exception:  # noqa: BLE001
                time.sleep(2)
            _wake.wait(5)
            _wake.clear()
    for _ in range(n):
        threading.Thread(target=loop, daemon=True).start()


# ---------- AI read ----------
READ = ("This is an ad saved as inspiration by a performance-marketing team. JSON only: {\"summary\": \"one sentence: what it "
        "shows and sells\", \"hook\": \"the persuasion angle in a few words\", \"style\": \"visual style in 6-12 words\", "
        "\"layout\": \"where text, product and people sit\", \"text\": \"the visible headline / on-screen text\", "
        "\"vertical\": \"ecommerce | igaming | leadgen | apps | other\", \"why_it_works\": \"one short sentence\"}")


def ai_read(aid: str) -> None:
    a = q("select * from ads where id=?", (aid,), one=True)
    if not a or not a["media"]:
        return
    try:
        from PIL import Image
        src = a["media"]
        if a["kind"] == "video":
            import subprocess
            tmp = f"/tmp/adlib-{aid}.jpg"
            subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-ss", "1", "-i", src, "-frames:v", "1", tmp],
                           timeout=60, check=True)
            src = tmp
        im = Image.open(src).convert("RGB")
        im.thumbnail((896, 896))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        ctx = " ".join(x for x in (a["title"], a["brand"], a["ad_text"]) if x)[:600]
        body = {"model": "claude-haiku", "temperature": 0, "max_tokens": 400, "messages": [{"role": "user", "content": [
            {"type": "text", "text": READ + (f"\nKnown details: {ctx}" if ctx else "")},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()}}]}]}
        r = httpx.post(LLM_URL, headers={"Authorization": f"Bearer {LLM_KEY.read_text().strip()}"}, json=body, timeout=90)
        m = re.search(r"\{.*\}", r.json()["choices"][0]["message"]["content"] or "", re.S)
        q("update ads set ai=? where id=?", (m.group(0) if m else "{}", aid))
    except Exception as e:  # noqa: BLE001
        q("update ads set ai=? where id=?", (json.dumps({"summary": f"(couldn't read: {str(e)[:80]})"}), aid))
    finally:
        q("update ads set status='done' where id=? and status='reading' and not exists "
          "(select 1 from jobs where ad_id=? and status in ('queued','downloading'))", (aid, aid))


# ---------- Foreplay (while we still have it) ----------
def fp_key() -> str:
    try:
        for line in FP_ENV.read_text().splitlines():
            if line.startswith("FOREPLAY_API_KEY="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def fp_cap() -> int:
    try:
        for line in FP_ENV.read_text().splitlines():
            if line.startswith("FOREPLAY_MONTHLY_CAP="):
                return int(line.split("=", 1)[1])
    except (OSError, ValueError):
        pass
    return 8000


def fp_used() -> int:
    r = q("select foreplay_credits c from usage where month=?", (dt.date.today().strftime("%Y-%m"),), one=True)
    return r["c"] if r else 0


FP_BASE = "https://public.api.foreplay.co"


def _fp_count(n: int) -> None:
    if n:
        q("insert into usage(month, foreplay_credits) values(?,?) on conflict(month) do update set "
          "foreplay_credits=foreplay_credits+excluded.foreplay_credits", (dt.date.today().strftime("%Y-%m"), n))


def fp_get(path: str, params: dict, paid: bool = True) -> dict:
    """One Foreplay call. paid = it returns ads (1 credit per ad) -> checked against our monthly cap first."""
    key = fp_key()
    if not key:
        raise RuntimeError("Foreplay isn't connected (an admin saves the API key)")
    p = {k: v for k, v in params.items() if v not in (None, "")}
    if paid:
        p["limit"] = max(1, min(50, int(p.get("limit") or 24)))
        if fp_used() + p["limit"] > fp_cap():
            raise RuntimeError(f"Foreplay monthly credit cap reached ({fp_used()} of {fp_cap()})")
    r = httpx.get(FP_BASE + path, params=p, headers={"Authorization": key}, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"Foreplay said {r.status_code}: {r.text[:200]}")
    j = r.json()
    if paid and isinstance(j.get("data"), list):
        _fp_count(len(j["data"]))
    return j


def fp_norm(a: dict) -> dict:
    """A Foreplay ad -> what the Studio shows (incl. Foreplay's AI fields: transcript, emotions, persona…)."""
    cards = a.get("cards") or [{}]
    media = a.get("video") or a.get("image") or cards[0].get("video") or cards[0].get("image") or a.get("thumbnail") or ""
    pp = a.get("publisher_platform")
    tl = lambda v: ", ".join(str(x) for x in v) if isinstance(v, list) else (str(v) if v else "")  # noqa: E731
    return {"foreplay_id": a.get("id") or a.get("ad_id"), "ad_id": a.get("ad_id"), "brand": a.get("brand_name") or a.get("name") or "",
            "brand_id": a.get("brand_id"), "avatar": a.get("avatar") or "",
            "platform": ",".join(pp) if isinstance(pp, list) else pp or "", "format": a.get("display_format") or "",
            "text": (a.get("description") or a.get("headline") or "")[:1500], "headline": a.get("headline") or "",
            "media": media, "thumb": a.get("thumbnail") or a.get("image") or cards[0].get("image") or "",
            "landing": a.get("link_url") or "", "live": a.get("live"), "started": a.get("started_running") or "",
            "running": a.get("running_duration"), "cta": a.get("cta_title") or "",
            "transcript": (a.get("full_transcription") or "")[:3000], "emotions": tl(a.get("emotional_drivers")),
            "persona": tl(a.get("persona")), "niches": tl(a.get("niches")), "category": tl(a.get("product_category")),
            "market": tl(a.get("market_target")), "languages": tl(a.get("languages")),
            "foreplay_url": a.get("foreplay_url") or "", "video_duration": a.get("video_duration")}


def _ads(j: dict) -> dict:
    return {"ads": [fp_norm(a) for a in (j.get("data") or [])], "cursor": (j.get("metadata") or {}).get("cursor"),
            "used": fp_used(), "cap": fp_cap(),
            "imported": {r["foreplay_id"] for r in q("select foreplay_id from ads where foreplay_id<>''")}}


def fp_search(params: dict) -> dict:
    """Foreplay discovery search (their whole ad database)."""
    allowed = ("query", "publisher_platform", "display_format", "order", "live", "languages", "niches", "cursor",
               "running_duration_min_days", "limit")
    try:
        return _ads(fp_get("/api/discovery/ads", {k: v for k, v in params.items() if k in allowed}))
    except RuntimeError as e:
        return {"error": str(e), "ads": []}


def fp_call(what: str, params: dict) -> dict:
    """Our own Foreplay account: boards, swipe file, Spyder (tracked brands), brands, analytics, credits."""
    try:
        c = params.get("cursor")
        lim = params.get("limit") or 24
        if what == "usage":
            d = fp_get("/api/usage", {}, paid=False).get("data") or {}
            return {"usage": d, "used": fp_used(), "cap": fp_cap()}
        if what == "boards":
            return {"boards": fp_get("/api/boards", {"limit": 100}, paid=False).get("data") or []}
        if what == "board":
            return _ads(fp_get("/api/board/ads", {"board_id": params["board_id"], "cursor": c, "limit": lim}))
        if what == "board_brands":
            return {"brands": fp_get("/api/board/brands", {"board_id": params["board_id"]}, paid=False).get("data") or []}
        if what == "swipefile":
            return _ads(fp_get("/api/swipefile/ads", {"cursor": c, "limit": lim}))
        if what == "spyder":
            return {"brands": fp_get("/api/spyder/brands", {"limit": 100}, paid=False).get("data") or []}
        if what == "spyder_ads":
            return _ads(fp_get("/api/spyder/brand/ads", {"brand_id": params["brand_id"], "cursor": c, "limit": lim}))
        if what == "brand_ads":
            return _ads(fp_get("/api/brand/getAdsByBrandId", {"brand_ids": params["brand_id"], "cursor": c, "limit": lim,
                                                             "order": params.get("order") or "newest",
                                                             "live": params.get("live")}))
        if what == "brands":
            if params.get("domain"):
                return {"brands": fp_get("/api/brand/getBrandsByDomain", {"domain": params["domain"]}, paid=False).get("data") or []}
            return {"brands": fp_get("/api/discovery/brands", {"query": params.get("query", "")}, paid=False).get("data") or []}
        if what == "analytics":
            return {"analytics": fp_get("/api/brand/analytics", {"id": params["brand_id"]}, paid=False).get("data")}
        return {"error": "unknown"}
    except KeyError as e:
        return {"error": f"missing {e}"}
    except RuntimeError as e:
        return {"error": str(e), "ads": []}


def _download(url: str, referer: str = "") -> tuple[bytes, str]:
    with httpx.stream("GET", url, timeout=90, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0", "Referer": referer or url}) as r:
        r.raise_for_status()
        ctype = r.headers.get("content-type", "")
        chunks, size = [], 0
        for ch in r.iter_bytes():
            size += len(ch)
            if size > MAX_BYTES:
                raise ValueError("file too big")
            chunks.append(ch)
    return b"".join(chunks), ctype


def fp_import(ad: dict, by: str, tags: list | None = None, note: str = "") -> dict:
    """Save one Foreplay ad into OUR library (media + Foreplay's AI fields), once."""
    fid = str(ad.get("foreplay_id") or "")
    if fid:
        old = q("select id from ads where foreplay_id=?", (fid,), one=True)
        if old:
            return {"id": old["id"], "duplicate": True}
    url = ad.get("media") or ad.get("thumb") or ""
    if not url:
        raise ValueError("no media on that ad")
    data, ctype = _download(url)
    if "octet-stream" in ctype or not ctype:
        ctype = "video/mp4" if ".mp4" in url else "image/jpeg"
    path, kind, sha = store(data, ctype, "Foreplay")
    dup = q("select id from ads where sha=?", (sha,), one=True)
    if dup:
        return {"id": dup["id"], "duplicate": True}
    extra = {"foreplay": {k: ad.get(k) for k in ("transcript", "emotions", "persona", "niches", "category", "market",
                                                 "languages", "cta", "running", "live", "started", "foreplay_url",
                                                 "platform", "format", "brand_id", "ad_id")}}
    aid = insert_ad(by=by, source="foreplay", platform="Foreplay",
                    page_url=str(ad.get("landing") or ad.get("foreplay_url") or "")[:500], media=path, kind=kind, sha=sha,
                    title=str(ad.get("headline") or "")[:200], brand=str(ad.get("brand") or "")[:120],
                    ad_text=str(ad.get("text") or "")[:2000], tags=json.dumps([t[:30] for t in (tags or [])][:12]),
                    note=note[:600], foreplay_id=fid, extra=json.dumps(extra), status="done")
    threading.Thread(target=ai_read, args=(aid,), daemon=True).start()
    threading.Thread(target=_scan, args=(pathlib.Path(path).parent,), daemon=True).start()
    return {"id": aid}


def _state(k: str, v=None):
    q("create table if not exists state(k text primary key, v text)")
    if v is None:
        r = q("select v from state where k=?", (k,), one=True)
        return json.loads(r["v"]) if r else None
    q("insert or replace into state values(?,?)", (k, json.dumps(v)))


def fp_sync(by: str = "sync", max_new: int | None = None) -> dict:
    """Bring everything new from OUR Foreplay boards + swipe file into our library (newest first, stops at the first
    ad it already has, so a daily run only costs credits for new saves). Capped per run."""
    max_new = max_new or int(os.getenv("FOREPLAY_SYNC_MAX", "150"))
    done, errors, per = 0, [], {}
    try:
        boards = fp_get("/api/boards", {"limit": 100}, paid=False).get("data") or []
    except RuntimeError as e:
        return {"error": str(e)}
    sources = [("swipefile", "/api/swipefile/ads", {}, "foreplay-swipefile")] + \
              [(b["name"], "/api/board/ads", {"board_id": b["id"]}, "fp:" + b["name"].lstrip("#")[:26]) for b in boards]
    for name, path, extra_p, tag in sources:
        cursor, n, stop = None, 0, False
        while not stop and done < max_new:
            try:
                j = fp_get(path, {**extra_p, "cursor": cursor, "limit": min(25, max_new - done)})
            except RuntimeError as e:
                errors.append(f"{name}: {e}")
                break
            rows = j.get("data") or []
            if not rows:
                break
            for a in rows:
                ad = fp_norm(a)
                if q("select 1 from ads where foreplay_id=?", (str(ad["foreplay_id"]),), one=True):
                    stop = True        # already have this one = everything older is in too
                    break
                try:
                    fp_import(ad, by, [tag])
                    n += 1
                    done += 1
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{ad.get('brand')}: {str(e)[:80]}")
            cursor = (j.get("metadata") or {}).get("cursor")
            if not cursor:
                break
        per[name] = n
    _state("fp_last_sync", {"ts": time.time(), "new": done, "per": per, "errors": errors[:10]})
    return {"new": done, "per": per, "errors": errors[:10], "used": fp_used(), "cap": fp_cap()}


# ---------- http ----------
def ok(request: Request) -> bool:
    try:
        return request.headers.get("x-library-key", "") == KEY_FILE.read_text().strip() and bool(request.headers.get("x-user"))
    except OSError:
        return False


def guard(fn):
    async def w(request: Request):
        if not ok(request):
            return JSONResponse({"error": "not allowed"}, status_code=403)
        return await fn(request)
    return w


@guard
async def save(request: Request):
    """multipart: file | fields media_url / data_url, page_url, title, brand, ad_text, tags, note, source."""
    by = request.headers.get("x-user", "")
    form = await request.form()
    page = str(form.get("page_url") or "")[:500]
    plat = str(form.get("platform") or "") or platform_of(page or str(form.get("media_url") or ""))
    data, ctype = None, ""
    target = str(form.get("post_url") or "") or page
    media_url = str(form.get("media_url") or "")
    c = canonical(target)
    if str(form.get("fetch") or "") == "1":
        direct_ok = media_url.startswith(("https://", "http://"))
        if c and (not c["direct"] or direct_ok) or (direct_ok and not c):
            return _queue_save(form, by, c, target, page, media_url if direct_ok else "")
        if not (form.get("data_url") or form.get("file")):
            return JSONResponse({"error": "That isn't a post link. Open the post itself (TikTok, Instagram, Facebook, YouTube, "
                                          "X, Snapchat, Pinterest or the Meta Ad Library) and save from there."}, status_code=400)
        # not a post (a landing page, a feed...) but a screenshot came along: keep that picture
    f = form.get("file")
    if f is not None and getattr(f, "filename", None):
        data, ctype = await f.read(), f.content_type or "application/octet-stream"
    elif form.get("data_url"):
        m = re.match(r"data:([\w/+.-]+);base64,(.*)", str(form.get("data_url")), re.S)
        if not m:
            return JSONResponse({"error": "bad screenshot"}, status_code=400)
        ctype, data = m.group(1), base64.b64decode(m.group(2))
    elif form.get("media_url"):
        u = str(form.get("media_url"))
        if not u.startswith("https://") and not u.startswith("http://"):
            return JSONResponse({"error": "that media can't be downloaded - use the screenshot button"}, status_code=400)
        try:
            with httpx.stream("GET", u, timeout=60, follow_redirects=True,
                              headers={"User-Agent": "Mozilla/5.0", "Referer": page or u}) as r:
                r.raise_for_status()
                ctype = r.headers.get("content-type", "")
                chunks, size = [], 0
                for c in r.iter_bytes():
                    size += len(c)
                    if size > MAX_BYTES:
                        raise ValueError("file too big")
                    chunks.append(c)
                data = b"".join(chunks)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": f"couldn't download it ({str(e)[:80]}) - use the screenshot button"}, status_code=400)
    else:
        return JSONResponse({"error": "nothing to save"}, status_code=400)
    try:
        path, kind, sha = store(data, ctype, plat)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    dup = q("select id from ads where sha=?", (sha,), one=True)
    if dup:
        return JSONResponse({"id": dup["id"], "duplicate": True})
    aid = insert_ad(by=by, source=str(form.get("source") or "extension")[:20], platform=plat[:30], page_url=page, media=path,
                    kind=kind, sha=sha, title=str(form.get("title") or "")[:200], brand=str(form.get("brand") or "")[:120],
                    ad_text=str(form.get("ad_text") or "")[:2000], tags=json.dumps(_clean_tags(str(form.get("tags") or ""))),
                    note=str(form.get("note") or "")[:600], foreplay_id=str(form.get("foreplay_id") or ""),
                    status="reading")            # no canon: a picture of a post must not block saving its real video later
    threading.Thread(target=ai_read, args=(aid,), daemon=True).start()
    threading.Thread(target=_scan, args=(pathlib.Path(path).parent,), daemon=True).start()
    if form.get("folder"):
        try:
            folder_put(str(form.get("folder")), [aid])
        except ValueError:
            pass
    return JSONResponse({"id": aid, "kind": kind, "platform": plat, "status": "reading"})


def _shot(form, plat: str) -> str:
    """The screenshot / crop the extension sends along -> its path (thumbnail + fallback only)."""
    m = re.match(r"data:([\w/+.-]+);base64,(.*)", str(form.get("data_url") or ""), re.S)
    if not m:
        return ""
    try:
        return store(base64.b64decode(m.group(2)), m.group(1), plat)[0]
    except (ValueError, base64.binascii.Error):
        return ""


def _queue_save(form, by: str, c: dict | None, target: str, page: str, media_url: str):
    """A post link (or a video file's own address) -> one row "queued" + a download job. Same post twice = the first save
    (a failed one is simply tried again)."""
    plat = str(form.get("platform") or "") or (c or {}).get("platform") or platform_of(target or media_url)
    key = c["key"] if c else "media:" + re.sub(r"[?#].*$", "", media_url)[:400]
    old = q("select id, status from ads where canon=? order by ts limit 1", (key,), one=True) or \
        (q("select id, status from ads where page_url=? and kind='video' and media<>'' limit 1", (c["url"],), one=True) if c else None)
    if old:
        if old["status"] == "failed" and retry(old["id"]):
            return JSONResponse({"id": old["id"], "status": "queued", "fetching": True, "retrying": True, "platform": plat})
        return JSONResponse({"id": old["id"], "duplicate": True, "status": old["status"] or "done", "platform": plat})
    extra = {"saved_from": page[:500]} if page and page != (c or {}).get("url") else {}
    shot = _shot(form, plat)
    if shot:
        extra["thumb"] = shot
    for k in ("author", "post_id", "detected"):
        if form.get(k):
            extra[k] = str(form.get(k))[:200]
    aid = insert_ad(by=by, source=str(form.get("source") or "extension")[:20], platform=plat[:30],
                    page_url=(c["url"] if c else page or media_url)[:500], kind="video", sha=f"pending-{_id()}",
                    title=str(form.get("title") or "")[:200], brand=str(form.get("brand") or "")[:120],
                    ad_text=str(form.get("ad_text") or "")[:2000], tags=json.dumps(_clean_tags(str(form.get("tags") or ""))),
                    note=str(form.get("note") or "")[:600], extra=json.dumps(extra), canon=key, status="queued")
    if c and not (c["direct"] and media_url):
        enqueue(aid, "ytdlp", c["url"])
    else:
        enqueue(aid, "direct", c["url"] if c else page, media_url, page)
    if form.get("folder"):
        try:
            folder_put(str(form.get("folder")), [aid])
        except ValueError:
            pass
    return JSONResponse({"id": aid, "status": "queued", "fetching": True, "platform": plat})


def status_of(ids: list) -> list:
    ids = [i for i in ids if re.fullmatch(r"[\w-]{1,40}", i)][:50]
    if not ids:
        return []
    rows = q(f"select id, ts, by, platform, page_url, kind, media, title, brand, status, extra, ai from ads where id in "
             f"({','.join('?' * len(ids))})", tuple(ids))
    by_id = {r["id"]: r for r in rows}
    out = []
    for i in ids:
        r = by_id.get(i)
        if not r:                                 # merged into an older copy of the same file, or deleted
            j = q("select error from jobs where ad_id=? order by created desc limit 1", (i,), one=True)
            dup = (j or {}).get("error", "")
            out.append({"id": i, "status": "done" if dup.startswith("duplicate of ") else "gone",
                        **({"same_as": dup.split()[-1]} if dup.startswith("duplicate of ") else {})})
            continue
        ex = json.loads(r["extra"] or "{}")
        ai = json.loads(r["ai"] or "{}")
        out.append({"id": i, "status": r["status"] or ("done" if r["media"] else "queued"), "error": ex.get("fetch_error", ""),
                    "kind": r["kind"], "platform": r["platform"], "title": (ai.get("summary") or r["title"] or "")[:140],
                    "brand": r["brand"], "page_url": r["page_url"], "ts": r["ts"], "has_media": bool(r["media"]),
                    "has_thumb": bool(ex.get("thumb")), "tries": ex.get("tries", 0)})
    return out


@guard
async def status(request: Request):
    return JSONResponse({"items": status_of(request.query_params.get("ids", "").split(","))})


@guard
async def retry_ep(request: Request):
    aid = request.path_params["aid"]
    return JSONResponse({"ok": retry(aid), "items": status_of([aid])})


@guard
async def thumb(request: Request):
    a = q("select media, kind, extra from ads where id=?", (request.path_params["aid"],), one=True)
    if not a:
        return Response(status_code=404)
    t = json.loads(a["extra"] or "{}").get("thumb") or (a["media"] if a["kind"] == "image" else "")
    if not t or not pathlib.Path(t).is_file():
        return Response(status_code=404)
    return FileResponse(t)


@guard
async def items(request: Request):
    qp = request.query_params
    where, args = ["1=1"], []
    if qp.get("q"):
        where.append("(title like ? or brand like ? or ad_text like ? or note like ? or ai like ? or tags like ?)")
        args += [f"%{qp['q']}%"] * 6
    if qp.get("platform"):
        where.append("platform=?")
        args.append(qp["platform"])
    if qp.get("by"):
        where.append("by=?")
        args.append(qp["by"])
    if qp.get("status") == "working":
        where.append("status in ('queued','downloading','reading')")
    elif qp.get("status"):
        where.append("status=?")
        args.append(qp["status"])
    src = qp.get("source", "library")           # library = our own saves (extension, phone bots, uploads) - the default
    if src == "library":
        where.append("source<>'foreplay'")
    elif src == "foreplay":
        where.append("source='foreplay'")
    if qp.get("folder") == "none":
        where.append("id not in (select ad_id from ad_folders)")
    elif qp.get("folder"):
        where.append("id in (select ad_id from ad_folders where folder_id=?)")
        args.append(qp["folder"])
    if qp.get("tag"):
        where.append("tags like ?")
        args.append('%"' + qp["tag"].replace('"', "") + '"%')
    lim, off = min(200, int(qp.get("limit", 60))), int(qp.get("offset", 0))
    rows = q(f"select * from ads where {' and '.join(where)} order by ts desc limit ? offset ?", (*args, lim, off))
    inf = {}
    if rows:
        ids = [r["id"] for r in rows]
        for x in q(f"select ad_id, folder_id from ad_folders where ad_id in ({','.join('?' * len(ids))})", ids):
            inf.setdefault(x["ad_id"], []).append(x["folder_id"])
    for r in rows:
        r["ai"] = json.loads(r["ai"] or "{}")
        r["tags"] = json.loads(r["tags"] or "[]")
        r["extra"] = json.loads(r["extra"] or "{}")
        r["folders"] = inf.get(r["id"], [])
    plats = [r["platform"] for r in q("select distinct platform from ads order by platform")]
    return JSONResponse({"items": rows, "platforms": plats, "total": q(f"select count(*) c from ads where {' and '.join(where)}",
                                                                        tuple(args), one=True)["c"],
                         "foreplay": {"connected": bool(fp_key()), "used": fp_used(), "cap": fp_cap()}})


@guard
async def item(request: Request):
    aid = request.path_params["aid"]
    if request.method == "GET":
        r = q("select * from ads where id=?", (aid,), one=True)
        if not r:
            return JSONResponse({"error": "not found"}, status_code=404)
        r["ai"], r["tags"] = json.loads(r["ai"] or "{}"), json.loads(r["tags"] or "[]")
        return JSONResponse(r)
    if request.method == "DELETE":                 # people delete their own saves; admins anything
        r = q("select by from ads where id=?", (aid,), one=True)
        if not r:
            return JSONResponse({"ok": True})
        if r["by"] != request.headers.get("x-user", "") and request.headers.get("x-admin") != "1":
            return JSONResponse({"error": "you can only delete what you saved"}, status_code=403)
        q("delete from ads where id=?", (aid,))
        q("delete from ad_folders where ad_id=?", (aid,))
        return JSONResponse({"ok": True})
    d = await request.json()
    for k in ("note", "title", "brand"):
        if k in d:
            q(f"update ads set {k}=? where id=?", (str(d[k])[:600], aid))
    if "tags" in d:
        q("update ads set tags=? where id=?", (json.dumps(_clean_tags(d["tags"])), aid))
    return JSONResponse({"ok": True})


@guard
async def media(request: Request):
    a = q("select media from ads where id=?", (request.path_params["aid"],), one=True)
    if not a or not pathlib.Path(a["media"]).is_file():
        return Response(status_code=404)
    return FileResponse(a["media"])


@guard
async def foreplay(request: Request):
    import asyncio
    res = await asyncio.get_running_loop().run_in_executor(None, fp_search, dict(request.query_params))
    if "imported" in res:
        res["imported"] = sorted(res["imported"])
    return JSONResponse(res)


@guard
async def foreplay_call(request: Request):
    import asyncio
    what = request.path_params["what"]
    if what == "sync" and request.method == "POST":
        res = await asyncio.get_running_loop().run_in_executor(None, fp_sync, request.headers.get("x-user", ""))
        return JSONResponse(res)
    if what == "import" and request.method == "POST":
        d = await request.json()
        out = []
        for ad in (d.get("ads") or [])[:50]:
            try:
                out.append(await asyncio.get_running_loop().run_in_executor(
                    None, fp_import, ad, request.headers.get("x-user", ""), d.get("tags") or [], str(d.get("note") or "")))
            except Exception as e:  # noqa: BLE001
                out.append({"error": str(e)[:120]})
        return JSONResponse({"results": out})
    if what == "last_sync":
        return JSONResponse(_state("fp_last_sync") or {})
    res = await asyncio.get_running_loop().run_in_executor(None, fp_call, what, dict(request.query_params))
    if "imported" in res:
        res["imported"] = sorted(res["imported"])
    return JSONResponse(res)


# ---------- folders + tags (2026-10-26): drop saves into folders, label them, filter by either ----------
def _clean_tags(tags) -> list:
    out = []
    for t in tags if isinstance(tags, list) else str(tags or "").split(","):
        t = re.sub(r"\s+", " ", str(t)).strip().lower()[:30]
        if t and t not in out:
            out.append(t)
    return out[:12]


def folder_list(src: str = "library") -> list:
    cond = {"library": "and a.source<>'foreplay'", "foreplay": "and a.source='foreplay'"}.get(src, "")
    rows = q("select f.*, (select count(*) from ad_folders af join ads a on a.id=af.ad_id where af.folder_id=f.id "
             f"{cond}) n from folders f order by f.name collate nocase")
    return rows


def tag_counts(src: str = "library") -> list:
    cond = {"library": "where source<>'foreplay'", "foreplay": "where source='foreplay'"}.get(src, "")
    c = {}
    for r in q(f"select tags from ads {cond}"):
        for t in json.loads(r["tags"] or "[]"):
            c[t] = c.get(t, 0) + 1
    return [{"tag": t, "n": n} for t, n in sorted(c.items(), key=lambda x: (-x[1], x[0]))]


def folder_create(name: str, by: str, parent: str = "") -> dict:
    name = re.sub(r"\s+", " ", name or "").strip()[:60]
    if not name:
        raise ValueError("give the folder a name")
    old = q("select * from folders where lower(name)=lower(?) and parent=?", (name, parent or ""), one=True)
    if old:
        return old
    fid = _id()
    q("insert into folders values(?,?,?,?,?)", (fid, name, parent or "", by, time.time()))
    return q("select * from folders where id=?", (fid,), one=True)


def folder_put(fid: str, ids: list, remove: bool = False) -> int:
    if not q("select 1 from folders where id=?", (fid,), one=True):
        raise ValueError("no such folder")
    n = 0
    for aid in ids[:500]:
        if remove:
            q("delete from ad_folders where ad_id=? and folder_id=?", (aid, fid))
        elif q("select 1 from ads where id=?", (aid,), one=True):
            q("insert or ignore into ad_folders values(?,?,?)", (aid, fid, time.time()))
        n += 1
    return n


@guard
async def folders(request: Request):
    if request.method == "POST":
        d = await request.json()
        try:
            return JSONResponse({"folder": folder_create(str(d.get("name") or ""), request.headers.get("x-user", ""),
                                                         str(d.get("parent") or ""))})
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
    src = request.query_params.get("source", "library")
    return JSONResponse({"folders": folder_list(src), "tags": tag_counts(src),
                         "unfiled": q("select count(*) c from ads where id not in (select ad_id from ad_folders)"
                                      + {"library": " and source<>'foreplay'", "foreplay": " and source='foreplay'"}.get(src, ""),
                                      one=True)["c"]})


@guard
async def folder(request: Request):
    fid = request.path_params["fid"]
    if request.method == "DELETE":                 # the folder goes, the saved ads stay (its maker or an admin)
        f = q("select by from folders where id=?", (fid,), one=True)
        if f and f["by"] != request.headers.get("x-user", "") and request.headers.get("x-admin") != "1":
            return JSONResponse({"error": "only whoever made the folder (or an admin) can delete it"}, status_code=403)
        q("delete from ad_folders where folder_id=?", (fid,))
        q("delete from folders where id=?", (fid,))
        return JSONResponse({"ok": True})
    d = await request.json()
    if "name" in d:
        q("update folders set name=? where id=?", (re.sub(r"\s+", " ", str(d["name"])).strip()[:60] or "Folder", fid))
    return JSONResponse({"folder": q("select * from folders where id=?", (fid,), one=True)})


@guard
async def folder_items(request: Request):
    d = await request.json()
    try:
        n = folder_put(request.path_params["fid"], [str(x) for x in d.get("ids") or []], bool(d.get("remove")))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"ok": True, "n": n})


@guard
async def bulk_tags(request: Request):
    """{ids:[...], add:[...], remove:[...]} - label many saves at once."""
    d = await request.json()
    add, rem = _clean_tags(d.get("add") or []), set(_clean_tags(d.get("remove") or []))
    for aid in [str(x) for x in d.get("ids") or []][:500]:
        r = q("select tags from ads where id=?", (aid,), one=True)
        if r:
            cur = [t for t in json.loads(r["tags"] or "[]") if t not in rem]
            q("update ads set tags=? where id=?", (json.dumps(_clean_tags(cur + add)), aid))
    return JSONResponse({"ok": True})


async def health(request):
    return JSONResponse({"ok": True, "ads": q("select count(*) c from ads", one=True)["c"]})


app = Starlette(routes=[Route("/health", health), Route("/v1/save", save, methods=["POST"]), Route("/v1/items", items),
                        Route("/v1/items/{aid}", item, methods=["GET", "PATCH", "DELETE"]), Route("/v1/media/{aid}", media),
                        Route("/v1/thumb/{aid}", thumb), Route("/v1/status", status),
                        Route("/v1/retry/{aid}", retry_ep, methods=["POST"]),
                        Route("/v1/foreplay/search", foreplay),
                        Route("/v1/folders", folders, methods=["GET", "POST"]),
                        Route("/v1/folders/{fid}", folder, methods=["PATCH", "DELETE"]),
                        Route("/v1/folders/{fid}/items", folder_items, methods=["POST"]),
                        Route("/v1/tags", bulk_tags, methods=["POST"]),
                        Route("/v1/foreplay/{what}", foreplay_call, methods=["GET", "POST"])])

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "sync":      # daily timer: ad-library-sync
        print(json.dumps(fp_sync("daily sync")))
        raise SystemExit(0)
    workers(int(os.getenv("LIBRARY_WORKERS", "2")))
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
