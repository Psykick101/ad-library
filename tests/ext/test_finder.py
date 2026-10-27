"""finder.js on saved pages of each site, served at their real addresses (Playwright routing, no network):
does an area over a video / a click / a right-click find THAT post's own link?  Run: python3 tests/ext/test_finder.py"""
import pathlib
import sys

from playwright.sync_api import sync_playwright

HERE = pathlib.Path(__file__).resolve().parent
FINDER = HERE.parent.parent / "extension" / "finder.js"
FIX = HERE / "fixtures"
SHOTS = HERE / "out"
SHOTS.mkdir(exist_ok=True)
ok = n = 0


def case(name, cond, got=None):
    global ok, n
    n += 1
    ok += bool(cond)
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   got: {got}"))


# (fixture, address it is served at, what to do, selector, expected)
CASES = [
    ("tiktok_foryou", "https://www.tiktok.com/foryou", "area", "article:nth-of-type(1) video",
     {"kind": "video", "post_url": "https://www.tiktok.com/@glowskin.th/video/7425913153745980705", "blob": True, "detected": "tiktok-card"}),
    ("tiktok_foryou", "https://www.tiktok.com/foryou", "area", "article:nth-of-type(2) video",
     {"post_url": "https://www.tiktok.com/@casino.boss/video/7426000000000000002"}),
    ("tiktok_foryou", "https://www.tiktok.com/foryou", "click", "article:nth-of-type(1) .overlay",
     {"kind": "video", "post_url": "https://www.tiktok.com/@glowskin.th/video/7425913153745980705"}),
    ("tiktok_foryou", "https://www.tiktok.com/foryou", "element", "article:nth-of-type(2) .overlay",
     {"post_url": "https://www.tiktok.com/@casino.boss/video/7426000000000000002"}),
    ("tiktok_explore", "https://www.tiktok.com/explore", "area", "[data-e2e=explore-item]:nth-of-type(2) video",
     {"kind": "video", "post_url": "https://www.tiktok.com/@fitfuel/video/7420000000000000002", "detected": "link"}),
    ("tiktok_explore", "https://www.tiktok.com/explore", "area", "[data-e2e=explore-item]:nth-of-type(3) img",
     {"kind": "video", "post_url": "https://www.tiktok.com/@petsco/video/7420000000000000003"}),   # thumbnail of a video post -> the video
    ("tiktok_explore_v2", "https://www.tiktok.com/explore", "area", "[data-e2e=explore-item]:nth-of-type(2)",
     {"kind": "video", "post_url": "https://www.tiktok.com/@fitfuel/video/7420000000000000082", "detected": "tiktok-card-area"}),
    ("tiktok_explore_v2", "https://www.tiktok.com/explore", "area", "[data-e2e=explore-item]:nth-of-type(3)",
     {"kind": "video", "post_url": "https://www.tiktok.com/@petsco/video/7420000000000000083"}),
    ("tiktok_explore_v2", "https://www.tiktok.com/explore", "area", ".hero video",
     {"kind": "video", "post_url": "https://www.tiktok.com/@hero.brand/video/7420000000000000077", "detected": "tiktok-card"}),
    ("tiktok_video", "https://www.tiktok.com/@glowskin.th/video/7425913153745980705?is_from_webapp=1", "area", "video",
     {"kind": "video", "post_url": "https://www.tiktok.com/@glowskin.th/video/7425913153745980705"}),
    ("instagram_feed", "https://www.instagram.com/", "area", "article:nth-of-type(2) video",
     {"kind": "video", "post_url": "https://www.instagram.com/reel/C9ZyXwVuTs2/", "detected": "link"}),
    ("instagram_feed", "https://www.instagram.com/", "area", "article:nth-of-type(1) img",
     {"kind": "image", "post_url": "https://www.instagram.com/p/C9AbCdEfGh1/"}),
    ("instagram_reels", "https://www.instagram.com/reels/C9QwErTyUi3/", "area", ".reel:nth-of-type(1) video",
     {"kind": "video", "post_url": "https://www.instagram.com/reel/C9QwErTyUi3/", "detected": "location"}),
    ("facebook_feed", "https://www.facebook.com/", "area", "[aria-posinset='1'] video",
     {"kind": "video", "post_url": "https://www.facebook.com/sunbrand/posts/pfbid02xYzAbc123/"}),
    ("facebook_feed", "https://www.facebook.com/", "area", "[aria-posinset='2'] video",
     {"post_url": "https://www.facebook.com/watch/?v=1122334455667788"}),
    ("facebook_feed", "https://www.facebook.com/", "area", "[aria-posinset='3'] video",
     {"post_url": "https://www.facebook.com/watch/?v=998877665544332"}),
    ("meta_adlibrary", "https://www.facebook.com/ads/library/?active_status=all&ad_type=all&country=ALL&q=glow", "area",
     ".x1dr75xp:nth-of-type(2) video",
     {"kind": "video", "post_url": "https://www.facebook.com/ads/library/?id=6543210987654321", "detected": "adlib-id",
      "media_url": "https://video.xx.fbcdn.net/v/t42.1790-2/ad-6543210987654321.mp4?_nc_cat=2", "blob": False}),
    ("youtube_shorts", "https://www.youtube.com/shorts/aBcDeFgHiJk", "area", "ytd-reel-video-renderer[is-active] video",
     {"kind": "video", "post_url": "https://www.youtube.com/shorts/aBcDeFgHiJk"}),
    ("x_timeline", "https://x.com/home", "area", "article video",
     {"kind": "video", "post_url": "https://x.com/brandx/status/1850000000000000001"}),
    ("tiktok_creative_center", "https://ads.tiktok.com/business/creativecenter/inspiration/topads/pc/en", "area", ".card:nth-of-type(1) video",
     {"kind": "video", "post_url": "https://ads.tiktok.com/business/creativecenter/topads/7300000000000000009/pc/en",
      "media_url": "https://v16m-default.akamaized.net/abc/7300000000000000009/video/tos/alisg/x.mp4?a=1"}),
    ("landing", "https://shop.example/glow?utm_source=fb", "area", ".hero",
     {"post_url": ""}),
    ("blob_nolink", "https://somesite.example/p/1", "area", "video",
     {"kind": "video", "post_url": "", "blob": True, "media_url": ""}),
]

with sync_playwright() as pw:
    b = pw.chromium.launch()
    ctx = b.new_context(viewport={"width": 1280, "height": 900})
    cur = {}
    ctx.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html; charset=utf-8", body=cur["html"])
              if route.request.resource_type == "document" else route.fulfill(status=204, body=""))
    page = ctx.new_page()
    for fx, url, how, sel, want in CASES:
        cur["html"] = (FIX / f"{fx}.html").read_text()
        page.goto(url)
        page.add_script_tag(path=str(FINDER))
        el = page.locator(sel).first
        el.scroll_into_view_if_needed()
        bb = el.bounding_box()
        if how == "area":                      # a box a bit inside the element, like a person drags
            rect = {"x": bb["x"] + 10, "y": bb["y"] + 10, "w": bb["width"] - 20, "h": bb["height"] - 20, "vw": 1280}
            got = page.evaluate("r => PsykickFind.inRect(r)", rect)
        elif how == "click":
            rect = {"x": bb["x"] + bb["width"] / 2, "y": bb["y"] + bb["height"] / 2, "w": 2, "h": 2, "vw": 1280}
            got = page.evaluate("r => PsykickFind.inRect(r)", rect)
        else:                                  # right-click remembered by lastclick.js
            got = el.evaluate("e => PsykickFind.forElement(e, '')")
        good = all(got.get(k) == v for k, v in want.items())
        case(f"{fx} [{how} {sel}] -> {want.get('post_url') or want.get('kind')}", good, {k: got.get(k) for k in want})
    cur["html"] = (FIX / "instagram_reels.html").read_text()
    page.goto("https://www.instagram.com/reels/C9QwErTyUi3/")
    page.add_script_tag(path=str(FINDER))
    got = page.evaluate("() => PsykickFind.pagePost()")
    case("popup 'Save this page's video' on a Reels page", got["post_url"] == "https://www.instagram.com/reel/C9QwErTyUi3/", got)
    cur["html"] = (FIX / "tiktok_foryou.html").read_text()
    page.goto("https://www.tiktok.com/foryou")
    page.add_script_tag(path=str(FINDER))
    page.evaluate("() => { const v = document.querySelectorAll('video'); v[0].pause(); Object.defineProperty(v[1], 'paused', {get: () => false}); }")
    page.locator("article:nth-of-type(2) video").scroll_into_view_if_needed()
    got = page.evaluate("() => PsykickFind.pagePost()")
    case("popup on the For You feed: the video playing now", got["post_url"] == "https://www.tiktok.com/@casino.boss/video/7426000000000000002", got)
    page.screenshot(path=str(SHOTS / "finder-foryou.png"))
    b.close()
print(f"{ok}/{n} OK")
sys.exit(0 if ok == n else 1)
