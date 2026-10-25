"""Ad Library - our own swipe file (microservice, separate from the lab and the Studio).

Saves ads the team finds anywhere (BigSpy, PiPiAds, Meta Ad Library, TikTok, any site) via the "Save to Psykick Studio"
Chrome extension or the Studio, plus search in Foreplay's database while we still have Foreplay. Each saved ad gets an
AI read (what it sells, hook, style, layout, text) so the Studio's creative AI can learn from it.

Videos with their source (2026-10-25): for TikTok / Instagram / Facebook / YouTube / X posts the extension (or "Paste a
link" in the Studio) sends the post's link with fetch=1; the service downloads the real video with yt-dlp and keeps the
source: author, caption, views/likes/comments/shares, post date, sound, link (in `extra`). Blob videos that a browser
can't download (TikTok, Reels) work this way too.

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
            """)
        cur = _con.execute(sql, args)
        _con.commit()
        rows = [dict(r) for r in cur.fetchall()]
    return (rows[0] if rows else None) if one else rows


def _id() -> str:
    return uuid.uuid4().hex[:12]


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
               "twitter.com", "snapchat.com", "pinterest.com", "linkedin.com", "vm.tiktok.com")


def fetchable(url: str) -> bool:
    try:
        host = httpx.URL(url).host or ""
    except Exception:  # noqa: BLE001
        return False
    return url.startswith("http") and any(host == h or host.endswith("." + h) for h in FETCH_HOSTS)


def fetch_source(url: str) -> tuple[bytes, str, dict]:
    """-> (video bytes, content type, source facts). Raises with a short reason."""
    import tempfile
    import yt_dlp
    with tempfile.TemporaryDirectory() as td:
        opts = {"outtmpl": f"{td}/v.%(ext)s", "quiet": True, "no_warnings": True, "noplaylist": True,
                "format": "best[ext=mp4][height<=1080]/bv*[ext=mp4][height<=1080]+ba[ext=m4a]/best[height<=1080]/best",
                "merge_output_format": "mp4", "max_filesize": MAX_BYTES, "socket_timeout": 30, "retries": 2,
                "ffmpeg_location": "/usr/local/bin/ffmpeg"}
        try:
            from yt_dlp.networking.impersonate import ImpersonateTarget   # TikTok needs a browser-like client
            opts["impersonate"] = ImpersonateTarget("chrome")
        except Exception:  # noqa: BLE001
            pass
        with yt_dlp.YoutubeDL(opts) as y:
            try:
                info = y.extract_info(url, download=True)
            except Exception:  # noqa: BLE001
                opts.pop("impersonate", None)
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
                                          "upload_date", "timestamp", "duration", "track", "artist", "thumbnail")
                 if info.get(k) not in (None, "")}
        return f.read_bytes(), ctype, facts


def _fetch_job(aid: str, url: str) -> None:
    try:
        data, ctype, facts = fetch_source(url)
        a = q("select * from ads where id=?", (aid,), one=True) or {}
        path, kind, sha = store(data, ctype, a.get("platform") or platform_of(url))
        dup = q("select id from ads where sha=? and id<>?", (sha, aid), one=True)
        if dup:
            q("delete from ads where id=?", (aid,))
            return
        extra = {**json.loads(a.get("extra") or "{}"), "fetch": "done", "source": facts}
        desc = str(facts.get("description") or facts.get("title") or "")
        q("update ads set media=?, kind=?, sha=?, brand=coalesce(nullif(brand,''), ?), ad_text=coalesce(nullif(ad_text,''), ?), "
          "title=coalesce(nullif(title,''), ?), page_url=?, extra=? where id=?",
          (path, kind, sha, str(facts.get("uploader") or facts.get("channel") or "")[:120], desc[:2000],
           str(facts.get("title") or "")[:200], str(facts.get("webpage_url") or url)[:500], json.dumps(extra), aid))
        threading.Thread(target=_scan, args=(pathlib.Path(path).parent,), daemon=True).start()
        ai_read(aid)
    except Exception as e:  # noqa: BLE001
        a = q("select extra, media from ads where id=?", (aid,), one=True) or {}
        extra = {**json.loads(a.get("extra") or "{}"), "fetch": "failed", "fetch_error": str(e)[:200]}
        q("update ads set extra=? where id=?", (json.dumps(extra), aid))
        if a.get("media"):
            ai_read(aid)       # the screenshot that came with it still gets read


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
            subprocess.run(["/usr/local/bin/ffmpeg", "-y", "-loglevel", "error", "-ss", "1", "-i", src, "-frames:v", "1", tmp],
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
    aid = _id()
    extra = {"foreplay": {k: ad.get(k) for k in ("transcript", "emotions", "persona", "niches", "category", "market",
                                                 "languages", "cta", "running", "live", "started", "foreplay_url",
                                                 "platform", "format", "brand_id", "ad_id")}}
    q("insert into ads values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
        aid, time.time(), by, "foreplay", "Foreplay", str(ad.get("landing") or ad.get("foreplay_url") or "")[:500], path,
        kind, sha, str(ad.get("headline") or "")[:200], str(ad.get("brand") or "")[:120], str(ad.get("text") or "")[:2000],
        json.dumps([t[:30] for t in (tags or [])][:12]), note[:600], "", fid, json.dumps(extra)))
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
    want = str(form.get("fetch") or "") == "1" and fetchable(page)
    if str(form.get("fetch") or "") == "1" and not want and not (form.get("data_url") or form.get("file") or form.get("media_url")):
        return JSONResponse({"error": "paste a TikTok, Instagram, Facebook, YouTube or X post link"}, status_code=400)
    if want:   # the real video + its source, downloaded in the background (a screenshot may come along as a fallback)
        same = q("select id from ads where page_url=? and media<>''", (page,), one=True)
        if same:
            return JSONResponse({"id": same["id"], "duplicate": True})
        aid = _id()
        shot_path, shot_kind, shot_sha = "", "", ""
        if form.get("data_url"):
            m = re.match(r"data:([\w/+.-]+);base64,(.*)", str(form.get("data_url")), re.S)
            if m:
                try:
                    shot_path, shot_kind, shot_sha = store(base64.b64decode(m.group(2)), m.group(1), plat)
                except ValueError:
                    pass
        q("insert into ads values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            aid, time.time(), by, str(form.get("source") or "extension")[:20], plat[:30], page, shot_path, shot_kind,
            shot_sha or f"pending-{aid}", str(form.get("title") or "")[:200], str(form.get("brand") or "")[:120],
            str(form.get("ad_text") or "")[:2000],
            json.dumps(_clean_tags(str(form.get("tags") or ""))),
            str(form.get("note") or "")[:600], "", "", json.dumps({"fetch": "working"})))
        threading.Thread(target=_fetch_job, args=(aid, page), daemon=True).start()
        if form.get("folder"):
            try:
                folder_put(str(form.get("folder")), [aid])
            except ValueError:
                pass
        return JSONResponse({"id": aid, "fetching": True, "platform": plat})
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
    aid = _id()
    q("insert into ads values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
        aid, time.time(), by, str(form.get("source") or "extension")[:20], plat[:30], page, path, kind, sha,
        str(form.get("title") or "")[:200], str(form.get("brand") or "")[:120], str(form.get("ad_text") or "")[:2000],
        json.dumps(_clean_tags(str(form.get("tags") or ""))),
        str(form.get("note") or "")[:600], "", str(form.get("foreplay_id") or ""), "{}"))
    threading.Thread(target=ai_read, args=(aid,), daemon=True).start()
    threading.Thread(target=_scan, args=(pathlib.Path(path).parent,), daemon=True).start()
    if form.get("folder"):
        try:
            folder_put(str(form.get("folder")), [aid])
        except ValueError:
            pass
    return JSONResponse({"id": aid, "kind": kind, "platform": plat})


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
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
