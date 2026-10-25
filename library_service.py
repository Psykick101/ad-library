"""Ad Library - our own swipe file (microservice, separate from the lab and the Studio).

Saves ads the team finds anywhere (BigSpy, PiPiAds, Meta Ad Library, TikTok, any site) via the "Save to Psykick Studio"
Chrome extension or the Studio, plus search in Foreplay's database while we still have Foreplay. Each saved ad gets an
AI read (what it sells, hook, style, layout, text) so the Studio's creative AI can learn from it.

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


def fp_search(params: dict) -> dict:
    """Foreplay discovery search. 1 credit per ad returned; stops at the monthly cap."""
    key = fp_key()
    if not key:
        return {"error": "Foreplay isn't connected (save the API key as D:\\AI\\ops\\secrets\\foreplay.txt)", "ads": []}
    limit = max(1, min(50, int(params.get("limit") or 24)))
    if fp_used() + limit > fp_cap():
        return {"error": f"Foreplay monthly credit cap reached ({fp_used()} of {fp_cap()})", "ads": []}
    allowed = ("query", "publisher_platform", "display_format", "order", "live", "languages", "niches", "cursor",
               "running_duration_min_days")
    p = {k: v for k, v in params.items() if k in allowed and v not in (None, "")}
    p["limit"] = limit
    last = None
    for base in ("https://public.api.foreplay.co/api/discovery/ads", "https://public.api.foreplay.co/api/ads/search"):
        r = httpx.get(base, params=p, headers={"Authorization": key}, timeout=45)
        last = r
        if r.status_code != 404:
            break
    if last.status_code != 200:
        return {"error": f"Foreplay said {last.status_code}: {last.text[:200]}", "ads": []}
    j = last.json()
    rows = j.get("data") or []
    q("insert into usage(month, foreplay_credits) values(?,?) on conflict(month) do update set "
      "foreplay_credits=foreplay_credits+excluded.foreplay_credits", (dt.date.today().strftime("%Y-%m"), len(rows)))
    ads = []
    for a in rows:
        media = a.get("video") or a.get("image") or (a.get("cards") or [{}])[0].get("video") or \
            (a.get("cards") or [{}])[0].get("image") or a.get("thumbnail") or ""
        ads.append({"foreplay_id": a.get("id") or a.get("ad_id"), "brand": a.get("brand_name") or a.get("name") or "",
                    "platform": ",".join(a.get("publisher_platform") or []) if isinstance(a.get("publisher_platform"), list)
                    else a.get("publisher_platform") or "", "format": a.get("display_format") or "",
                    "text": (a.get("description") or a.get("headline") or "")[:400], "media": media,
                    "thumb": a.get("thumbnail") or a.get("image") or "", "landing": a.get("link_url") or "",
                    "live": a.get("live"), "started": a.get("started_running") or a.get("start_date") or ""})
    return {"ads": ads, "cursor": (j.get("metadata") or {}).get("cursor"), "used": fp_used(), "cap": fp_cap()}


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
        json.dumps([t.strip()[:30] for t in str(form.get("tags") or "").split(",") if t.strip()][:12]),
        str(form.get("note") or "")[:600], "", str(form.get("foreplay_id") or ""), "{}"))
    threading.Thread(target=ai_read, args=(aid,), daemon=True).start()
    threading.Thread(target=_scan, args=(pathlib.Path(path).parent,), daemon=True).start()
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
    lim, off = min(200, int(qp.get("limit", 60))), int(qp.get("offset", 0))
    rows = q(f"select * from ads where {' and '.join(where)} order by ts desc limit ? offset ?", (*args, lim, off))
    for r in rows:
        r["ai"] = json.loads(r["ai"] or "{}")
        r["tags"] = json.loads(r["tags"] or "[]")
    plats = [r["platform"] for r in q("select distinct platform from ads order by platform")]
    return JSONResponse({"items": rows, "platforms": plats, "total": q("select count(*) c from ads", one=True)["c"],
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
    if request.method == "DELETE":
        q("delete from ads where id=?", (aid,))
        return JSONResponse({"ok": True})
    d = await request.json()
    for k in ("note", "title", "brand"):
        if k in d:
            q(f"update ads set {k}=? where id=?", (str(d[k])[:600], aid))
    if "tags" in d:
        q("update ads set tags=? where id=?", (json.dumps([str(t)[:30] for t in d["tags"]][:12]), aid))
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
    return JSONResponse(await asyncio.get_running_loop().run_in_executor(None, fp_search, dict(request.query_params)))


async def health(request):
    return JSONResponse({"ok": True, "ads": q("select count(*) c from ads", one=True)["c"]})


app = Starlette(routes=[Route("/health", health), Route("/v1/save", save, methods=["POST"]), Route("/v1/items", items),
                        Route("/v1/items/{aid}", item, methods=["GET", "PATCH", "DELETE"]), Route("/v1/media/{aid}", media),
                        Route("/v1/foreplay/search", foreplay)])

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
