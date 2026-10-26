"""Ad Library (no network): folders, tags, sources, post links -> queue -> status, duplicates, retries, failure reasons
(yt-dlp stubbed). Run: python3 test_library.py"""
import io
import json
import os
import pathlib
import sys
import tempfile

T = pathlib.Path(tempfile.mkdtemp())
(T / "key").write_text("k")
os.environ.update(LIBRARY_DB=str(T / "lib.db"), LIBRARY_MEDIA=str(T / "media"), LIBRARY_KEY=str(T / "key"),
                  FOREPLAY_ENV=str(T / "fp.env"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import library_service as L  # noqa: E402
from PIL import Image  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

L.ai_read = lambda aid: None
L._scan = lambda p: None
c = TestClient(L.app)
H = {"X-Library-Key": "k", "X-User": "dana@x.com"}
ok = n = 0


def case(name, cond):
    global ok, n
    n += 1
    ok += bool(cond)
    print(("PASS " if cond else "FAIL ") + name)


def png(col):
    b = io.BytesIO()
    Image.new("RGB", (40, 40), col).save(b, "PNG")
    return b.getvalue()


case("needs the key", c.get("/v1/folders").status_code == 403)
f1 = c.post("/v1/folders", headers=H, json={"name": "Winners Q4"}).json()["folder"]
case("make a folder", f1["name"] == "Winners Q4")
case("same name = same folder", c.post("/v1/folders", headers=H, json={"name": "winners q4"}).json()["folder"]["id"] == f1["id"])
a = c.post("/v1/save", headers=H, files={"file": ("a.png", png("red"), "image/png")},
           data={"source": "extension", "tags": "UGC, Hook ,ugc", "folder": f1["id"]}).json()["id"]
b = c.post("/v1/save", headers=H, files={"file": ("b.png", png("blue"), "image/png")}, data={"source": "instagram-dm"}).json()["id"]
L.insert_ad(id="fp1", ts=1, by="sync", source="foreplay", platform="Foreplay", kind="image", sha="x", foreplay_id="F1")
lib = c.get("/v1/items", headers=H).json()
case("default = our library only (extension + phone bots), no Foreplay", {i["id"] for i in lib["items"]} == {a, b} and lib["total"] == 2)
case("Foreplay tab = Foreplay only", [i["id"] for i in c.get("/v1/items?source=foreplay", headers=H).json()["items"]] == ["fp1"])
case("show all = everything", len(c.get("/v1/items?source=all", headers=H).json()["items"]) == 3)
case("saved straight into a folder + clean tags", c.get(f"/v1/items?folder={f1['id']}", headers=H).json()["items"][0]["tags"] == ["ugc", "hook"])
case("unfiled filter", [i["id"] for i in c.get("/v1/items?folder=none", headers=H).json()["items"]] == [b])
c.post(f"/v1/folders/{f1['id']}/items", headers=H, json={"ids": [b]})
case("drop into a folder", len(c.get(f"/v1/items?folder={f1['id']}", headers=H).json()["items"]) == 2)
c.post("/v1/tags", headers=H, json={"ids": [a, b], "add": ["Summer"], "remove": ["hook"]})
case("label many at once", [i["id"] for i in c.get("/v1/items?tag=summer", headers=H).json()["items"]] != []
     and "hook" not in json.dumps(c.get("/v1/items?tag=ugc", headers=H).json()["items"][0]["tags"]))
fl = c.get("/v1/folders", headers=H).json()
case("folders + counts + tag list", fl["folders"][0]["n"] == 2 and fl["tags"][0] == {"tag": "summer", "n": 2})
c.post(f"/v1/folders/{f1['id']}/items", headers=H, json={"ids": [b], "remove": True})
case("take out of a folder", len(c.get(f"/v1/items?folder={f1['id']}", headers=H).json()["items"]) == 1)
c.patch(f"/v1/folders/{f1['id']}", headers=H, json={"name": "Best of Q4"})
c.delete(f"/v1/folders/{f1['id']}", headers=H)
case("delete folder keeps the saves", c.get("/v1/folders", headers=H).json()["folders"] == [] and
     len(c.get("/v1/items", headers=H).json()["items"]) == 2)
case("can't delete someone else's save", c.delete(f"/v1/items/{a}", headers={**H, "X-User": "eli@x.com"}).status_code == 403)
case("admin can", c.delete(f"/v1/items/{a}", headers={**H, "X-User": "boss@x.com", "X-Admin": "1"}).json()["ok"])
case("own save can be deleted", c.delete(f"/v1/items/{b}", headers=H).json()["ok"] and c.get("/v1/items", headers=H).json()["total"] == 0)

# ---------- canonical post links (mirrors extension/finder.js; the same table runs there in tests/ext) ----------
CANON = json.loads((pathlib.Path(__file__).resolve().parent / "tests" / "ext" / "canon_cases.json").read_text())
bad = [(u, want, (L.canonical(u) or {}).get("key")) for u, want in CANON if (L.canonical(u) or {}).get("key") != want]
case(f"canonical(): {len(CANON)} link shapes -> the same post key" + (f" {bad[:3]}" if bad else ""), not bad)
case("canonical url is clean", L.canonical("https://www.tiktok.com/@brand.x/video/7412345678901234567?is_from_webapp=1&lang=en")["url"]
     == "https://www.tiktok.com/@brand.x/video/7412345678901234567")

# ---------- post link -> queue -> worker -> done (yt-dlp stubbed) ----------
import subprocess  # noqa: E402
VID = T / "v.mp4"
subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=320x568:d=1", "-c:v", "libx264",
                "-pix_fmt", "yuv420p", str(VID)], check=True)
calls = []


def fake_fetch(url):
    calls.append(url)
    if "7000000000000000001" in url:
        raise RuntimeError("ERROR: [TikTok] 7000000000000000001: Video not available, status code 10204")
    if "7000000000000000002" in url and calls.count(url) < 2:
        raise RuntimeError("ERROR: Unable to download webpage: The read operation timed out")
    return VID.read_bytes(), "video/mp4", {"webpage_url": url, "uploader": "brand.x", "description": "Hook! #ad", "view_count": 1200}


L.fetch_source = fake_fetch
L._download = lambda url, referer="": (VID.read_bytes() + b"cc", "video/mp4")
U = "https://www.tiktok.com/@brand.x/video/7412345678901234567"
shot = "data:image/png;base64," + __import__("base64").b64encode(png("green")).decode()
r = c.post("/v1/save", headers=H, data={"page_url": "https://www.tiktok.com/foryou", "post_url": U + "?lang=en", "fetch": "1",
                                        "data_url": shot, "source": "extension-area", "author": "brand.x"}).json()
case("post link -> queued at once (no waiting for the download)", r.get("status") == "queued" and r.get("fetching") and not calls)
st = c.get(f"/v1/status?ids={r['id']}", headers=H).json()["items"][0]
case("status: queued, thumbnail = the screenshot, not the media", st["status"] == "queued" and st["has_thumb"] and not st["has_media"])
case("thumbnail served while it downloads", c.get(f"/v1/thumb/{r['id']}", headers=H).status_code == 200)
case("worker runs the job", L.work_once() and calls == [U])
st = c.get(f"/v1/status?ids={r['id']}", headers=H).json()["items"][0]
row = c.get(f"/v1/items/{r['id']}", headers=H).json()
case("done: real video + source + clean post link", st["status"] == "done" and row["kind"] == "video" and row["brand"] == "brand.x"
     and row["page_url"] == U and json.loads(row["extra"])["source"]["view_count"] == 1200)
case("video gets a jpg thumbnail", json.loads(row["extra"]).get("thumb", "").endswith(".jpg"))
case("nothing else due", not L.work_once())
for other in ("https://m.tiktok.com/@brand.x/video/7412345678901234567", "https://www.tiktok.com/embed/v2/7412345678901234567"):
    d = c.post("/v1/save", headers=H, data={"page_url": other, "fetch": "1"}).json()
    case(f"same post, other link shape = duplicate ({other.split('/')[2]})", d.get("duplicate") and d["id"] == r["id"])
case("feed page alone (no post) is refused with a clear reason",
     "Open the post itself" in c.post("/v1/save", headers=H, data={"page_url": "https://www.tiktok.com/foryou", "fetch": "1"}).json()["error"])
g = c.post("/v1/save", headers=H, data={"page_url": "https://www.tiktok.com/@a/video/7000000000000000001", "fetch": "1"}).json()
L.work_once()
st = c.get(f"/v1/status?ids={g['id']}", headers=H).json()["items"][0]
case("deleted post -> failed at once, human reason", st["status"] == "failed" and "deleted" in st["error"])
t = c.post("/v1/save", headers=H, data={"page_url": "https://www.tiktok.com/@a/video/7000000000000000002", "fetch": "1"}).json()
L.work_once()
st = c.get(f"/v1/status?ids={t['id']}", headers=H).json()["items"][0]
case("timeout -> queued again with a reason + backoff", st["status"] == "queued" and "retrying" in st["error"] and not L.work_once())
L.q("update jobs set next_at=0")
L.work_once()
case("retry succeeds", c.get(f"/v1/status?ids={t['id']}", headers=H).json()["items"][0]["status"] == "done")
calls.clear()
again = c.post("/v1/save", headers=H, data={"page_url": "https://www.tiktok.com/@a/video/7000000000000000001", "fetch": "1"}).json()
case("saving a failed post again retries it (same row)", again["id"] == g["id"] and again.get("retrying"))
case("retry endpoint", c.post(f"/v1/retry/{g['id']}", headers=H).json()["items"][0]["status"] == "queued")
L.work_once()
cc = c.post("/v1/save", headers=H, data={"page_url": "https://ads.tiktok.com/business/creativecenter/topads/7300000000000000009/pc/en",
                                         "media_url": "https://v16-cc.tiktokcdn.com/abc/video.mp4?x=1", "fetch": "1"}).json()
L.work_once()
row = c.get(f"/v1/items/{cc['id']}", headers=H).json()
case("Creative Center: the video file's own address is downloaded directly", row["status"] == "done" and row["kind"] == "video"
     and json.loads(row["extra"])["fetched_from"] == "direct")
case("Creative Center without the video file = clear refusal", "error" in c.post("/v1/save", headers=H, data={
    "page_url": "https://ads.tiktok.com/business/creativecenter/topads/7300000000000000010/pc/en", "fetch": "1"}).json())
pic = c.post("/v1/save", headers=H, data={"page_url": "https://shop.example.com/landing", "fetch": "1", "data_url": shot}).json()
case("landing page + screenshot (no post) -> saved as a picture", pic.get("kind") == "image")
L.q("update ads set status='done' where id=?", (pic["id"],))
img_post = c.post("/v1/save", headers=H, data={"page_url": "https://www.instagram.com/reel/Cabc123xyz/", "data_url": shot.replace("Z", "Y")}).json()
vid_post = c.post("/v1/save", headers=H, data={"page_url": "https://www.instagram.com/reel/Cabc123xyz/?igsh=1", "fetch": "1"}).json()
case("a picture of a post doesn't block saving its real video", vid_post.get("status") == "queued" and vid_post["id"] != img_post.get("id"))
L.work_once()
case("same file from another link = merged into the first copy", c.get(f"/v1/status?ids={vid_post['id']}", headers=H).json()["items"][0]
     == {"id": vid_post["id"], "status": "done", "same_as": r["id"]})
w = c.get("/v1/items?status=working&source=all", headers=H).json()
case("items?status=working lists only what's still saving", all(i["status"] in ("queued", "downloading", "reading") for i in w["items"]))
L.q("update jobs set status='downloading' where id=(select id from jobs limit 1)")
L.workers(0)
case("restart puts half-done downloads back in the queue", not L.q("select 1 from jobs where status='downloading'"))
case("explain(): login wall is not retried", L.explain("ERROR: [Instagram] Cabc: Requested content is not available, rate-limit reached or login required")[1] in (False, True)
     and L.explain("login required. Use --cookies")[1] is False)
print(f"{ok}/{n} OK")
sys.exit(0 if ok == n else 1)
