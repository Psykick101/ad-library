"""Inspiration saves from an area selection (no network, yt-dlp stubbed): a thumbnail of a video post fetches the real
video, a feed's tab title ("Explore - Find your favorite videos on TikTok") never becomes the ad's name (the video's own
title does), a post with no video keeps the selected picture instead of a failed card, login problems stay failed.
Run: python3 tests/test_inspiration.py"""
import base64
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile

T = pathlib.Path(tempfile.mkdtemp())
(T / "key").write_text("k")
os.environ.update(LIBRARY_DB=str(T / "lib.db"), LIBRARY_MEDIA=str(T / "media"), LIBRARY_KEY=str(T / "key"),
                  FOREPLAY_ENV=str(T / "fp.env"), LIBRARY_VAULT=str(T / "vault"), LIBRARY_VAULT_KEY=str(T / "s" / "vault.key"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import library_service as L  # noqa: E402
from PIL import Image  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

L.ai_read = lambda aid: None
L._scan = lambda p: None
c = TestClient(L.app)
H = {"X-Library-Key": "k", "X-User": "dana@x.com"}
ok = n = 0


def case(name, cond, got=None):
    global ok, n
    n += 1
    ok += bool(cond)
    print(("PASS " if cond else "FAIL ") + name + ("" if cond or got is None else f"   got: {got}"))


def shot(col):
    b = io.BytesIO()
    Image.new("RGB", (60, 100), col).save(b, "JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(b.getvalue()).decode()


VID = T / "v.mp4"
subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=green:s=320x568:d=1", "-c:v", "libx264",
                "-pix_fmt", "yuv420p", str(VID)], check=True)
MODE = {"v": "ok"}


def fake_fetch(url):
    if MODE["v"] == "novideo":
        raise ValueError("no video in that post")
    if MODE["v"] == "login":
        raise ValueError("ERROR: [Instagram] login required, use --cookies")
    return VID.read_bytes(), "video/mp4", {"title": "Best Taco Sushi #tiktokfood", "uploader": "bankiii", "webpage_url": url}


L.fetch_source = fake_fetch

# ---- titles
for t in ("Explore - Find your favorite videos on TikTok", "TikTok - Make Your Day", "(3) Facebook", "Instagram", "YouTube",
          "For You | TikTok", "Meta Ad Library", "Log in or sign up | TikTok"):
    case(f"boilerplate title dropped: {t!r}", L.clean_title(t) == "")
for t in ("Best Taco Sushi Best Movie #tiktokfood | bankiii | TikTok", "Watch me cook | TikTok", "Glow serum ad"):
    case(f"real title kept: {t!r}", L.clean_title(t) == t)

# ---- area over an Explore thumbnail: the extension sends the post link -> the real video, named by the video
r = c.post("/v1/save", headers=H, data={"page_url": "https://www.tiktok.com/explore", "post_url": "https://www.tiktok.com/@bankiii/video/7691803400949730581",
                                        "fetch": "1", "title": "Explore - Find your favorite videos on TikTok", "source": "extension-area",
                                        "data_url": shot("red"), "detected": "link-area"}).json()
case("area over a thumbnail -> a video download is queued (not a picture)", r.get("fetching") and r.get("status") == "queued", r)
L.work_once()
a = c.get(f"/v1/items/{r['id']}", headers=H).json()
case("...the real video is saved", a["kind"] == "video" and a["media"].endswith(".mp4") and a["status"] == "done", a.get("status"))
case("...named by the video's own title, not the Explore page", a["title"] == "Best Taco Sushi #tiktokfood", a["title"])

# ---- a post with no video (photo post) + the selected picture -> keep the picture
MODE["v"] = "novideo"
r = c.post("/v1/save", headers=H, data={"page_url": "https://www.tiktok.com/explore", "post_url": "https://www.tiktok.com/@petsco/photo/7420000000000000003",
                                        "fetch": "1", "source": "extension-area", "data_url": shot("blue")}).json()
L.work_once()
a = c.get(f"/v1/items/{r['id']}", headers=H).json()
ex = json.loads(L.q("select extra from ads where id=?", (r["id"],), one=True)["extra"])
case("photo post: no video -> the selected picture is kept (done, not failed)", a["status"] == "done" and a["kind"] == "image"
     and ex.get("fallback") == "screenshot" and pathlib.Path(a["media"]).is_file(), (a["status"], a["kind"], ex))
st = c.get(f"/v1/status?ids={r['id']}", headers=H).json()["items"][0]
case("...status says done with no error", st["status"] == "done" and not st.get("error"), st)

# ---- no picture came along -> failed with the reason (nothing to keep)
r = c.post("/v1/save", headers=H, data={"post_url": "https://www.tiktok.com/@petsco/photo/7420000000000000009", "fetch": "1",
                                        "source": "extension"}).json()
L.work_once()
case("no picture to keep -> failed with a clear reason", c.get(f"/v1/items/{r['id']}", headers=H).json()["status"] == "failed")

# ---- login problems stay failed (an admin fixes the account and presses Try again)
MODE["v"] = "login"
r = c.post("/v1/save", headers=H, data={"post_url": "https://www.instagram.com/reel/C9ZyXwVuTs9/", "fetch": "1",
                                        "source": "extension-area", "data_url": shot("yellow")}).json()
L.work_once()
a = c.get(f"/v1/items/{r['id']}", headers=H).json()
case("login wall -> stays failed (no screenshot pretending to be the ad)", a["status"] == "failed" and a["kind"] == "video", a["status"])

# ---- a screenshot-only save from a feed keeps no boilerplate title
r = c.post("/v1/save", headers=H, data={"page_url": "https://www.tiktok.com/explore", "title": "Explore - Find your favorite videos on TikTok",
                                        "data_url": shot("purple"), "source": "extension-area"}).json()
case("picture from a feed: no boilerplate title", c.get(f"/v1/items/{r['id']}", headers=H).json()["title"] == "")

print(f"{ok}/{n} OK")
sys.exit(0 if ok == n else 1)
