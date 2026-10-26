"""End to end, no network: the unpacked extension in Chromium (new headless) -> the real Studio app (lab_app, with
Cloudflare Access simulated: it adds the sign-in header everywhere EXCEPT /api/ext/*, like the bypass policy) -> the real
Ad Library service (yt-dlp stubbed). Connect -> drag over a fake TikTok For You card with a <video> -> the save carries
the post's own link -> status goes queued -> downloading -> done in the popup and in Inspiration. Screenshots: tests/ext/out/.
Run: python3 tests/ext/test_e2e.py   (STUDIO_SRC = folder with lab_app.py, default ../w or ~/ai-infra/workers)
"""
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent
OUT = HERE / "out"
OUT.mkdir(exist_ok=True)
STUDIO_SRC = pathlib.Path(os.getenv("STUDIO_SRC") or next(
    (p for p in (REPO.parent / "w", pathlib.Path.home() / "ai-infra" / "workers") if (p / "lab_app.py").exists()), REPO.parent / "w"))
T = pathlib.Path(tempfile.mkdtemp())
(T / "lib.key").write_text("k")


def port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


SP, LP = port(), port()
STUDIO = f"http://localhost:{SP}"
os.environ.update(LAB_TEST="1", LAB_DB=str(T / "lab.db"), WS_ROOT=str(T / "ws"), STUDIO_LOCK=str(T / "lock"),
                  LIBRARY_KEY_FILE=str(T / "lib.key"), STUDIO_HOSTS="localhost", LIBRARY_URL=f"http://127.0.0.1:{LP}",
                  LIBRARY_DB=str(T / "library.db"), LIBRARY_MEDIA=str(T / "media"), LIBRARY_KEY=str(T / "lib.key"),
                  FOREPLAY_ENV=str(T / "fp.env"), STUDIO_CREATIVE=str(T / "creative"))
sys.path.insert(0, str(REPO))
sys.path.insert(1, str(STUDIO_SRC))
import uvicorn  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

import library_service as L  # noqa: E402
import lab_app as la  # noqa: E402

ok = n = 0


def case(name, cond, got=None):
    global ok, n
    n += 1
    ok += bool(cond)
    print(("PASS " if cond else "FAIL ") + name + ("" if cond or got is None else f"   got: {got}"))


# ---- Ad Library: real service, yt-dlp + AI stubbed. The download waits for `release` so we can see "Saving...".
VID = T / "v.mp4"
subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=s=360x640:d=2", "-c:v", "libx264",
                "-pix_fmt", "yuv420p", str(VID)], check=True)
release, fetched = threading.Event(), []


def fake_fetch(url):
    fetched.append(url)
    release.wait(90)
    return VID.read_bytes(), "video/mp4", {"webpage_url": url, "uploader": "glowskin.th", "description": "Serum that actually works ✨",
                                           "view_count": 128000, "like_count": 12400, "upload_date": "20261020"}


L.fetch_source = fake_fetch
L._scan = lambda p: None
L.ai_read = lambda aid: L.q("update ads set ai=? where id=?", (json.dumps(
    {"summary": "Skincare serum UGC: before/after in 5 seconds", "hook": "Proof in 5 seconds", "why_it_works": "Fast visual proof"}), aid))
L.workers(1)

# ---- the Studio, behind a pretend Cloudflare Access
OWNER = la.OWNER
ext_calls = []


class FakeAccess:
    """Adds cf-access-authenticated-user-email like Access does after its login - except on /api/ext/* (bypass)."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            h = [(k, v) for k, v in scope["headers"] if k.lower() != b"cf-access-authenticated-user-email"]
            if scope["path"].startswith("/api/ext/"):
                ext_calls.append((scope["method"], scope["path"], dict(h).get(b"authorization", b"")[:11].decode()))
            else:
                h.append((b"cf-access-authenticated-user-email", OWNER.encode()))
            scope = dict(scope, headers=h)
        await self.app(scope, receive, send)


la.studio.init(la.q)


def serve(app, p):
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=p, log_level="error", loop="asyncio"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", p), 0.2).close()
            return
        except OSError:
            time.sleep(0.1)


serve(L.app, LP)
serve(FakeAccess(la.app), SP)

# ---- the extension, pointed at the local Studio
EXT = T / "ext"
shutil.copytree(REPO / "extension", EXT)
(EXT / "config.js").write_text(f'self.PSYKICK_STUDIO = "{STUDIO}";\n')
m = json.loads((EXT / "manifest.json").read_text())
m["externally_connectable"]["matches"] = ["http://localhost/*"]
m["content_scripts"][1]["matches"] = ["http://localhost/studio/extension/connect*"]
(EXT / "manifest.json").write_text(json.dumps(m, indent=1))
FIX = HERE / "fixtures"
PAGES = {"www.tiktok.com": "tiktok_foryou", "somesite.example": "blob_nolink", "www.instagram.com": "instagram_reels"}


def fake_site(route):
    host = re.match(r"https?://([^/]+)", route.request.url)[1]
    if route.request.resource_type != "document":
        return route.fulfill(status=204, body="")
    route.fulfill(status=200, content_type="text/html; charset=utf-8", body=(FIX / f"{PAGES[host]}.html").read_text())


def lib_row(aid):
    return L.q("select * from ads where id=?", (aid,), one=True) or {}


with sync_playwright() as pw:
    ctx = pw.chromium.launch_persistent_context(str(T / "profile"), channel="chromium", headless=True,
                                                viewport={"width": 1280, "height": 860},
                                                args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}"])
    for h in PAGES:
        ctx.route(f"https://{h}/**", fake_site)
    sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker")
    eid = sw.url.split("/")[2]
    case("extension loaded (service worker running)", bool(eid))

    # 1. not connected -> popup shows Connect
    pop = ctx.new_page()
    pop.set_viewport_size({"width": 360, "height": 640})
    pop.goto(f"chrome-extension://{eid}/popup.html")
    pop.wait_for_selector("#cn:not([hidden])")
    case("popup: not connected -> Connect shown, save buttons off", pop.is_disabled("#area") and "not connected" in pop.inner_text("#who"))
    pop.screenshot(path=str(OUT / "1-popup-not-connected.png"))

    # 2. Connect -> the Studio's connect page (signed in through 'Access') -> token handed to the extension
    with ctx.expect_page() as pinfo:
        pop.click("#connect")
    cp = pinfo.value
    cp.wait_for_load_state()
    case("Connect opens the Studio's connect page for this extension", f"/studio/extension/connect?ext={eid}" in cp.url, cp.url)
    cp.screenshot(path=str(OUT / "2-connect-page.png"))
    cp.click("#go")
    cp.wait_for_selector("text=Connected. You can close this tab", timeout=15000)
    cp.screenshot(path=str(OUT / "3-connected.png"))
    auth = sw.evaluate("async () => (await chrome.storage.local.get('auth')).auth")
    case("token stored in the extension (psx_...), for the right person", auth and auth["token"].startswith("psx_") and auth["email"] == OWNER)
    rows = la.q("select * from ext_tokens")
    case("Studio keeps only the hash; listed for Settings", len(rows) == 1 and auth["token"] not in json.dumps(rows))
    cp.close()

    # 2b. the fallback hand-over (page -> content script) works too: open the page without the extension id
    cp = ctx.new_page()
    cp.goto(f"{STUDIO}/studio/extension/connect")
    cp.click("#go")
    cp.wait_for_selector("text=Connected. You can close this tab", timeout=15000)
    auth2 = sw.evaluate("async () => (await chrome.storage.local.get('auth')).auth")
    case("fallback hand-over through the content script; reconnect replaces the token", auth2["token"] != auth["token"]
         and len(la.q("select * from ext_tokens where revoked=0")) == 1)
    cp.close()

    # 3. area selection over a TikTok For You card -> the post link, not a frame
    feed = ctx.new_page()
    feed.goto("https://www.tiktok.com/foryou")
    feed.bring_to_front()
    sw.evaluate("async () => { const [t] = await chrome.tabs.query({url: 'https://www.tiktok.com/*'}); self.__sel = PS.selectArea(t); return t.id; }")
    feed.wait_for_selector("text=Drag around the ad")
    bb = feed.locator("article:nth-of-type(1) video").bounding_box()
    feed.screenshot(path=str(OUT / "4-area-overlay.png"))
    feed.mouse.move(bb["x"] + 30, bb["y"] + 40)
    feed.mouse.down()
    feed.mouse.move(bb["x"] + bb["width"] - 30, bb["y"] + bb["height"] - 60, steps=8)
    feed.mouse.up()
    res = sw.evaluate("() => self.__sel")
    f = res["found"]
    case("area over the video -> TikTok post link found (author + video id from the player)",
         f["kind"] == "video" and f["post_url"] == "https://www.tiktok.com/@glowskin.th/video/7425913153745980705", f)
    case("save accepted -> queued", res["result"]["ok"] and res["result"]["status"] == "queued", res["result"])
    saves = [c for c in ext_calls if c[1] == "/api/ext/save"]
    case("save went to /api/ext/save with a Bearer token (no Access header needed)", saves and saves[-1][2] == "Bearer psx_")
    aid = res["result"]["id"]
    row = lib_row(aid)
    ex = json.loads(row["extra"])
    case("library row: post link as the address, fetch queued, crop kept only as thumbnail",
         row["page_url"] == "https://www.tiktok.com/@glowskin.th/video/7425913153745980705" and row["media"] == ""
         and ex.get("thumb") and row["source"] == "extension-area" and row["by"] == OWNER, row)
    feed.wait_for_selector("#__psykick_toast")
    feed.screenshot(path=str(OUT / "5-saving-toast.png"))
    for _ in range(50):
        if fetched:
            break
        time.sleep(0.2)
    case("worker asked yt-dlp for the clean post link", fetched[:1] == ["https://www.tiktok.com/@glowskin.th/video/7425913153745980705"], fetched)

    # 4. popup shows it downloading; Inspiration shows a "Saving..." card
    pop = ctx.new_page()
    pop.set_viewport_size({"width": 360, "height": 640})
    pop.goto(f"chrome-extension://{eid}/popup.html")
    pop.wait_for_selector(".pill.downloading", timeout=15000)
    case("popup: connected (no Connect box) + recent save with status 'Downloading'", not pop.is_visible("#cn")
         and OWNER in pop.inner_text("#who"))
    pop.screenshot(path=str(OUT / "6-popup-downloading.png"))
    st = ctx.new_page()
    st.set_viewport_size({"width": 1280, "height": 900})
    st.goto(f"{STUDIO}/studio")
    try:
        st.wait_for_selector("#nav button[data-v=inspiration]", timeout=20000)
        try:
            st.click("text=Skip for now", timeout=4000)          # first-visit welcome
        except Exception:  # noqa: BLE001
            pass
        st.evaluate("() => show('inspiration')")
        st.wait_for_selector("[data-saving]", timeout=15000)
        case("Inspiration: a 'Saving…' card with the screenshot as its thumbnail", "Saving" in st.inner_text(f".card[data-aid='{aid}']"))
        st.screenshot(path=str(OUT / "7-inspiration-saving.png"))
    except Exception as e:  # noqa: BLE001
        case("Inspiration page loads", False, str(e)[:300])

    # 5. download finishes -> done everywhere
    release.set()
    for _ in range(100):
        if lib_row(aid).get("status") == "done":
            break
        time.sleep(0.2)
    row = lib_row(aid)
    case("job done: real video stored, source kept", row.get("status") == "done" and row["kind"] == "video" and row["media"].endswith(".mp4")
         and row["brand"] == "glowskin.th", {k: row.get(k) for k in ("status", "kind", "media", "brand")})
    pop.wait_for_selector(".pill.done", timeout=20000)
    case("popup: 'In Inspiration'", "In Inspiration" in pop.inner_text("#rec"))
    pop.screenshot(path=str(OUT / "8-popup-done.png"))
    try:
        st.wait_for_selector(f".card[data-aid='{aid}'] video", timeout=25000)
        case("Inspiration: the card turned into the video (no reload)", True)
        st.wait_for_timeout(600)
        st.screenshot(path=str(OUT / "9-inspiration-done.png"))
    except Exception as e:  # noqa: BLE001
        case("Inspiration: the card turned into the video (no reload)", False, str(e)[:200])

    # 5b. Settings lists this browser's extension (with Disconnect); Admin lists everyone's (with Revoke)
    try:
        st.bring_to_front()
        st.evaluate("() => show('settings')")
        st.wait_for_selector("#ext-list [data-xrv]", timeout=10000)
        case("Settings → Chrome extension: this browser listed with Disconnect", "Chrome extension" in st.inner_text("#ext-list"))
        st.locator("#ext-box").screenshot(path=str(OUT / "11-settings-extensions.png"))
        st.evaluate("() => show('admin')")
        st.wait_for_selector("#p-ext [data-xra]", timeout=10000)
        case("Admin → People & devices: extensions listed with Revoke", OWNER.split("@")[0] in st.inner_text("#p-ext"))
    except Exception as e:  # noqa: BLE001
        case("Settings / Admin extension lists", False, str(e)[:200])

    # 6. a blob video with no post link anywhere -> clear message, nothing useless saved
    before = L.q("select count(*) c from ads", one=True)["c"]
    other = ctx.new_page()
    other.goto("https://somesite.example/p/1")
    other.bring_to_front()
    sw.evaluate("async () => { const [t] = await chrome.tabs.query({url: 'https://somesite.example/*'}); self.__sel = PS.selectArea(t); }")
    other.wait_for_selector("text=Drag around the ad")
    vb = other.locator("video").bounding_box()
    other.mouse.move(vb["x"] + 20, vb["y"] + 20)
    other.mouse.down()
    other.mouse.move(vb["x"] + vb["width"] - 20, vb["y"] + vb["height"] - 20, steps=6)
    other.mouse.up()
    res = sw.evaluate("() => self.__sel")
    other.wait_for_selector("#__psykick_toast")
    case("blob video, no link -> 'Open the post itself and press Save', nothing saved",
         res["result"].get("reason") == "no-link" and L.q("select count(*) c from ads", one=True)["c"] == before
         and "Open the post itself" in other.inner_text("#__psykick_toast"), res)
    other.screenshot(path=str(OUT / "10-no-link-message.png"))

    # 7. popup "Save this page's video" on a Reels page (address = the post)
    reels = ctx.new_page()
    reels.goto("https://www.instagram.com/reels/C9QwErTyUi3/")
    reels.bring_to_front()
    r = sw.evaluate("async () => { const [t] = await chrome.tabs.query({url: 'https://www.instagram.com/*'}); return PS.savePageVideo(t); }")
    job = L.q("select url from jobs where ad_id=?", (r.get("id"),), one=True) or {}
    case("'Save this page's video' on a Reel -> queued with the reel link", r.get("ok") and
         job.get("url") == "https://www.instagram.com/reel/C9QwErTyUi3/", (r, job))

    # 8. disconnect in the popup -> token revoked server side; the next save asks to connect again
    pop = ctx.new_page()
    pop.goto(f"chrome-extension://{eid}/popup.html")
    pop.click("#dis")
    pop.wait_for_selector("#cn:not([hidden])")
    case("disconnect revokes the token in the Studio", la.q("select count(*) c from ext_tokens where revoked=0", one=True)["c"] == 0)
    with ctx.expect_page() as pinfo:
        sw.evaluate("async () => { const [t] = await chrome.tabs.query({url: 'https://www.instagram.com/*'}); return PS.savePageVideo(t); }")
    case("save while disconnected -> opens the Connect page (not a login popup)", "/studio/extension/connect" in pinfo.value.url)
    ctx.close()
print(f"{ok}/{n} OK  (screenshots: {OUT})")
os._exit(0 if ok == n else 1)
