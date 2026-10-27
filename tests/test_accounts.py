"""Saver accounts + Instagram DMs (no network): encrypted cookie vault, secrets never shown / printed / logged, yt-dlp
gets a private cookie file only for IG/FB, health detection on saved answers, gentle-use gaps, DM parsing on saved
inbox JSON, replies, and the Inspiration filters / sorts (via, sender, views, label). Run: python3 tests/test_accounts.py"""
import contextlib
import io
import json
import logging
import os
import pathlib
import stat
import sys
import tempfile
import types

T = pathlib.Path(tempfile.mkdtemp())
(T / "key").write_text("k")
os.environ.update(LIBRARY_DB=str(T / "lib.db"), LIBRARY_MEDIA=str(T / "media"), LIBRARY_KEY=str(T / "key"),
                  FOREPLAY_ENV=str(T / "fp.env"), LIBRARY_VAULT=str(T / "vault"), LIBRARY_VAULT_KEY=str(T / "secrets" / "vault.key"))
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIX = ROOT / "tests" / "fixtures"
import httpx  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import accounts as A  # noqa: E402
import library_service as L  # noqa: E402

ok = n = 0
OUT = io.StringIO()                 # everything printed or logged while the secrets are in play


def case(name, cond, got=None):
    global ok, n
    n += 1
    ok += bool(cond)
    sys.__stdout__.write(("PASS " if cond else "FAIL ") + name + ("" if cond or got is None else f"   got: {got}") + "\n")


logging.basicConfig(stream=OUT, level=logging.DEBUG, force=True)
L.ai_read = lambda aid: None
L._scan = lambda p: None
c = TestClient(L.app)
H = {"X-Library-Key": "k", "X-User": "ido@x.com"}
SECRET_IG = "61000000001%3AsEcReTsEsSiOn%3A27%3AAYdQ"
SECRET_FB = "42%3AxsSeCrEtVaLuE%3A2%3A1761000000"
IG_COOKIES = [{"name": "sessionid", "value": SECRET_IG, "domain": ".instagram.com", "expirationDate": 1830000000},
              {"name": "ds_user_id", "value": "61000000001", "domain": ".instagram.com", "expirationDate": 1769000000},
              {"name": "csrftoken", "value": "csrfTOKENabc123", "domain": ".instagram.com"},
              {"name": "rur", "value": "NOTNEEDED", "domain": ".instagram.com"},
              {"name": "sessionid", "value": "EVIL", "domain": ".evil.com"}]
FB_COOKIES = [{"name": "c_user", "value": "100090000000001", "domain": ".facebook.com"},
              {"name": "xs", "value": SECRET_FB, "domain": ".facebook.com"}, {"name": "datr", "value": "dAtRvalue", "domain": ".facebook.com"}]

# ---- fake Instagram / Facebook
SEEN = []
STATE = {"ig": "ok", "fb": "ok"}


def site(req: httpx.Request):
    SEEN.append((req.method, str(req.url), req.headers.get("cookie", ""), req.content.decode() if req.content else ""))
    u = str(req.url)
    if "instagram.com/api/v1/accounts/current_user" in u:
        if STATE["ig"] == "login":
            return httpx.Response(302, headers={"location": "https://www.instagram.com/accounts/login/?next=/api/v1/"})
        if STATE["ig"] == "checkpoint":
            return httpx.Response(400, json={"message": "checkpoint_required", "checkpoint_url": "/challenge/abc/"})
        return httpx.Response(200, json={"user": {"username": "psykick.saver", "pk": "61000000001"}, "status": "ok"})
    if "direct_v2/" in u and STATE.get("dm"):
        mode = STATE["dm"]
        STATE["dm_calls"] = STATE.get("dm_calls", 0) + 1
        if mode == "down" or (mode == "blip" and STATE["dm_calls"] == 1):
            raise httpx.ConnectError("no route")
        if mode == "pending500" and "pending_inbox" in u:
            return httpx.Response(500, text="oops")
    if "direct_v2/inbox" in u:
        return httpx.Response(200, json=json.loads((FIX / "ig_inbox.json").read_text()))
    if "direct_v2/pending_inbox" in u:
        return httpx.Response(200, json=json.loads((FIX / "ig_pending.json").read_text()))
    if "direct_v2/threads" in u:
        return httpx.Response(200, json={"status": "ok"})
    if "facebook.com/settings" in u:
        if STATE["fb"] == "login":
            return httpx.Response(200, text=(FIX / "fb_login.html").read_text())
        if STATE["fb"] == "checkpoint":
            return httpx.Response(302, headers={"location": "https://www.facebook.com/checkpoint/828281030927956/"})
        return httpx.Response(200, text=(FIX / "fb_home_ok.html").read_text())
    return httpx.Response(404)


A.TRANSPORT = httpx.MockTransport(site)

with contextlib.redirect_stdout(OUT), contextlib.redirect_stderr(OUT):
    # ---- connect + vault
    r = c.post("/v1/accounts/instagram", headers=H, json={"cookies": IG_COOKIES})
    st = r.json()
    case("connect IG: checked at once -> connected as @psykick.saver", r.status_code == 200 and st["connected"] and st["status"] == "ok"
         and st["handle"] == "psykick.saver", st)
    enc = T / "vault" / "instagram.enc"
    case("vault file is encrypted (no cookie value in it) and mode 600, folder 700",
         enc.exists() and SECRET_IG.encode() not in enc.read_bytes() and b"sessionid" not in enc.read_bytes()
         and stat.S_IMODE(enc.stat().st_mode) == 0o600 and stat.S_IMODE(enc.parent.stat().st_mode) == 0o700)
    case("vault key created with mode 600", stat.S_IMODE((T / "secrets" / "vault.key").stat().st_mode) == 0o600)
    kept = A.load("instagram")
    case("only the needed cookies kept (no rur, nothing from other domains)",
         sorted(x["name"] for x in kept) == ["csrftoken", "ds_user_id", "sessionid"] and "EVIL" not in json.dumps(kept))
    case("status shows days left from the session cookie's expiry", st["days_left"] and st["days_left"] > 0, st["days_left"])
    exp_test = A.status("instagram")
    A._set_meta("instagram", expires=1000)
    case("expired session -> 'expired', not used", A.status("instagram")["status"] == "expired" and not A.usable("instagram"))
    A._set_meta("instagram", expires=1830000000)
    bad = c.post("/v1/accounts/instagram", headers=H, json={"cookies": IG_COOKIES[2:]})
    case("missing sessionid -> clear refusal", bad.status_code == 400 and "sessionid" in bad.json()["error"])
    case("unknown platform -> 404", c.post("/v1/accounts/myspace", headers=H, json={}).status_code == 404)
    case("needs the library key", c.get("/v1/accounts").status_code == 403)
    fb = c.post("/v1/accounts/facebook", headers=H, json={"cookies": FB_COOKIES}).json()
    case("connect FB -> ok, named from the page", fb["status"] == "ok" and fb["handle"] == "Psykick Saver", fb)

    # ---- yt-dlp gets a private cookie file, only for IG / FB, removed afterwards
    YT = {}

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=True):
            ck = self.opts.get("cookiefile")
            YT["url"], YT["cookiefile"] = url, ck
            if ck:
                YT["mode"] = stat.S_IMODE(os.stat(ck).st_mode)
                YT["text"] = pathlib.Path(ck).read_text()
                with open(ck, "a") as f:            # the site refreshed the session during the run
                    f.write(".instagram.com\tTRUE\t/\tTRUE\t1795000000\tsessionid\tREFRESHEDsession\n")
            pathlib.Path(self.opts["outtmpl"].replace("%(ext)s", "mp4")).write_bytes(b"\x00\x00\x00\x18ftypmp42" + os.urandom(64))
            return {"webpage_url": url, "uploader": "glowskin.th"}

    sys.modules["yt_dlp"] = types.SimpleNamespace(YoutubeDL=FakeYDL)
    L.fetch_source("https://www.instagram.com/reel/DBreelCode1/")
    case("IG download uses the saver cookies (private 600 file, Netscape format)",
         YT["cookiefile"] and YT["mode"] == 0o600 and "\tsessionid\t" in YT["text"] and YT["text"].startswith("# Netscape"))
    case("cookie file + its folder removed after the run", not pathlib.Path(YT["cookiefile"]).exists()
         and not pathlib.Path(YT["cookiefile"]).parent.exists())
    case("cookies the site refreshed are kept (re-encrypted)", next(x for x in A.load("instagram") if x["name"] == "sessionid")["value"]
         == "REFRESHEDsession")
    SECRET_IG2 = "REFRESHEDsession"
    L.fetch_source("https://www.tiktok.com/@a/video/7412345678901234567")
    case("TikTok download: no cookies", YT["cookiefile"] is None)
    L.fetch_source("https://www.facebook.com/reel/1122334455667788")
    case("FB download uses the FB saver cookies", YT["cookiefile"] and "\txs\t" in YT["text"] and "sessionid" not in YT["text"])

    # ---- health detection (saved answers)
    cl = A.classify
    case("IG: login redirect -> login_required", cl("instagram", 302, "https://www.instagram.com/accounts/login/?next=/", "")[0] == "login_required")
    case("IG: checkpoint json -> checkpoint", cl("instagram", 400, "", '{"message":"checkpoint_required"}')[0] == "checkpoint")
    case("IG: challenge redirect -> checkpoint", cl("instagram", 302, "https://www.instagram.com/challenge/?next=/", "")[0] == "checkpoint")
    case("IG: 429 -> rate_limited", cl("instagram", 429, "", "Please wait a few minutes before you try again.")[0] == "rate_limited")
    case("IG: 200 with user -> ok + handle", cl("instagram", 200, "", '{"user":{"username":"psykick.saver"}}') == ("ok", "psykick.saver"))
    case("FB: login page -> login_required", cl("facebook", 200, "", (FIX / "fb_login.html").read_text())[0] == "login_required")
    case("FB: checkpoint redirect -> checkpoint", cl("facebook", 302, "https://www.facebook.com/checkpoint/1/", "")[0] == "checkpoint")
    STATE["ig"] = "login"
    st = c.post("/v1/accounts/instagram/check", headers=H).json()
    case("check: session ended -> login_required, not usable", st["status"] == "login_required" and not st["usable"]
         and "reconnect" in st["error"])
    STATE["ig"] = "checkpoint"
    case("check: checkpoint", c.post("/v1/accounts/instagram/check", headers=H).json()["status"] == "checkpoint")
    L.fetch_source("https://www.instagram.com/reel/DBreelCode1/")
    case("account needing attention is NOT used for downloads", YT["cookiefile"] is None)
    STATE["ig"] = "ok"
    case("check ok again -> usable", c.post("/v1/accounts/instagram/check", headers=H).json()["usable"])
    case("a login-wall error during a download marks the account",
         (A.mark("facebook", "login_required", "x"), A.status("facebook")["status"])[1] == "login_required")
    c.post("/v1/accounts/facebook/check", headers=H)

    # ---- gentle use
    A._uses.clear()
    case("first use: no wait", A.slot_wait("instagram") == 0)
    A.slot_take("instagram")
    w = A.slot_wait("instagram")
    case(f"then a random 25-60 s gap ({w:.0f}s)", 20 <= w <= 60)
    A._uses["instagram"] = [__import__("time").time() - 3500 + i for i in range(A.HOURLY)]
    case("hourly cap", A.slot_wait("instagram") > 60)
    A._uses.clear()
    jid_ad = L.insert_ad(by="d@x.com", source="instagram-dm", platform="Meta", page_url="https://www.instagram.com/reel/DBz/", kind="video",
                         sha="pending-x", canon="instagram:DBz", status="queued")
    L.enqueue(jid_ad, "ytdlp", "https://www.instagram.com/reel/DBz/")
    A.slot_take("instagram")
    L.work_once()
    j = L.q("select * from jobs where ad_id=?", (jid_ad,), one=True)
    case("IG job waits for the account's gap (re-queued, not counted as a try)", j["status"] == "queued" and j["tries"] == 0
         and j["next_at"] > __import__("time").time() + 10)

    # ---- DM inbox (unofficial) on saved inbox JSON
    viewer, items = A.ig_parse(json.loads((FIX / "ig_inbox.json").read_text()))
    by_id = {i["item_id"][-2:]: i for i in items}
    case("viewer = the saver account", viewer == {"pk": "61000000001", "username": "psykick.saver"})
    case("own messages skipped", "05" not in by_id)
    case("shared reel -> reel link", by_id["01"]["urls"] == ["https://www.instagram.com/reel/DBreelCode1/"] and by_id["01"]["sender"] == "dana.creates")
    case("shared post -> post link", by_id["02"]["urls"] == ["https://www.instagram.com/p/DBpostCode2/"])
    case("new-style share (xma) -> its link", by_id["03"]["urls"] == ["https://www.instagram.com/p/DBxmaCode3/?img_index=1"]
         and by_id["08"]["urls"] == ["https://www.instagram.com/reel/DBxmaClip4/"])
    case("link message -> the link (TikTok too)", by_id["04"]["urls"] == ["https://www.tiktok.com/@brand.x/video/7412345678901234567?lang=en"])
    case("text with a link -> the link", by_id["06"]["urls"] == ["https://www.youtube.com/shorts/dQw4w9WgXcQ"])
    case("story share -> nothing to save (stories vanish)", by_id["07"]["type"] == "story_share" and by_id["07"]["urls"] == [])
    case("plain text kept (link codes)", by_id["10"]["text"] == "K7QM2P" and by_id["10"]["sender"] == "new.hire")
    case("timestamps in seconds", 1.7e9 < by_id["01"]["ts"] < 1.8e9)
    A._uses.clear()
    ib = c.get("/v1/inbox/instagram", headers=H).json()
    case("inbox endpoint: main + message requests, oldest first", len(ib["items"]) == 10 and ib["items"][-1]["pending"]
         and ib["items"][-1]["sender"] == "stranger.x", len(ib.get("items", [])))
    case("inbox read uses the account's gap", c.get("/v1/inbox/instagram", headers=H).json().get("busy", 0) > 0)
    SEEN.clear()
    r = c.post("/v1/inbox/instagram/reply", headers=H, json={"thread_id": "340282366841710300949128100000000003", "text": "Saved ✅", "pending": True}).json()
    case("reply to a message request: accept it, then send", r["ok"] and "/approve/" in SEEN[0][1] and "broadcast/text" in SEEN[1][1]
         and "Saved" in __import__("urllib.parse").parse.unquote_plus(SEEN[1][3]))
    case("reply refuses a bad thread id", not A.ig_reply("../../x", "hi"))
    STATE["ig"] = "login"
    A._uses.clear()
    A.mark("instagram", "login_required", "x")
    case("inbox with a broken session -> error, no call", "error" in c.get("/v1/inbox/instagram", headers=H).json())
    STATE["ig"] = "ok"

    # ---- DM reader robustness: blips retried, a broken request list doesn't lose the main inbox, clear errors
    A.IG_RETRY_WAIT = 0
    A.mark("instagram", "ok", "")
    A._uses.clear()
    STATE.update(dm="blip", dm_calls=0)
    ib = c.get("/v1/inbox/instagram", headers=H).json()
    case("network blip on the inbox -> tried again, DMs read", len(ib.get("items", [])) == 10 and STATE["dm_calls"] == 3, (STATE, ib.get("error")))
    A._uses.clear()
    STATE.update(dm="pending500", dm_calls=0)
    ib = c.get("/v1/inbox/instagram", headers=H).json()
    case("message-request list failing -> main inbox still read + a warning", len(ib["items"]) > 0 and not any(i["pending"] for i in ib["items"])
         and "message requests" in ib["warnings"][0], ib.get("warnings"))
    A._uses.clear()
    STATE.update(dm="down", dm_calls=0)
    r = c.get("/v1/inbox/instagram", headers=H)
    case("Instagram unreachable -> 502 with a plain reason (no crash page), account NOT marked broken",
         r.status_code == 502 and "couldn't reach Instagram" in r.json()["error"] and A.usable("instagram"), (r.status_code, r.text[:200]))
    STATE.pop("dm")
    A._uses.clear()
    orig = A.ig_inbox
    A.ig_inbox = lambda: 1 / 0
    r = c.get("/v1/inbox/instagram", headers=H)
    case("unexpected bug -> JSON error the Studio can show", r.status_code == 500 and "inbox read failed" in r.json()["error"])
    A.ig_inbox = orig

    # ---- disconnect wipes
    r = c.delete("/v1/accounts/facebook", headers=H).json()
    case("disconnect wipes the encrypted file", not r["connected"] and not (T / "vault" / "facebook.enc").exists() and A.load("facebook") is None)

    # ---- secrets never shown anywhere
    shown = json.dumps(c.get("/v1/accounts", headers=H).json()) + json.dumps(L.q("select * from accounts")) + \
        json.dumps(L.q("select * from state")) if L.q("select name from sqlite_master where name='state'") else ""
    shown += json.dumps(c.get("/v1/accounts", headers=H).json()) + json.dumps(L.q("select * from accounts"))
    case("no cookie value in any status / DB row", not any(s in shown for s in (SECRET_IG, SECRET_IG2, SECRET_FB, "csrfTOKENabc123")))

    # ---- Inspiration filters + sorts
    L.q("delete from ads")
    mk = lambda **k: L.insert_ad(**{"kind": "video", "media": "/x", "status": "done", **k})  # noqa: E731
    a1 = mk(by="dana@x.com", source="instagram-dm", platform="Meta", tags='["ugc"]', ts=10,
            extra=json.dumps({"sender": "@dana.creates", "source": {"view_count": 5000, "like_count": 9}}))
    a2 = mk(by="eli@x.com", source="telegram", platform="TikTok", tags='["hook","ugc"]', ts=20, extra=json.dumps({"source": {"view_count": 900000}}))
    a3 = mk(by="dana@x.com", source="extension-area", platform="YouTube", tags="[]", ts=30, extra="{}")
    a4 = mk(by="eli@x.com", source="android-share", platform="Meta", tags='["about"]', ts=40, extra=json.dumps({"source": {"view_count": 10}}))
    ids = lambda **p: [i["id"] for i in c.get("/v1/items", headers=H, params=p).json()["items"]]  # noqa: E731
    case("via=instagram (DM saves)", ids(via="instagram") == [a1])
    case("via=telegram / extension / android", ids(via="telegram") == [a2] and ids(via="extension") == [a3] and ids(via="android") == [a4])
    case("sender = IG handle", ids(sender="@dana.creates") == [a1])
    case("by = team member", ids(by="dana@x.com") == [a3, a1])
    case("sort: most viewed first (unknown views last)", ids(sort="views") == [a2, a1, a4, a3])
    case("sort: oldest", ids(sort="old") == [a1, a2, a3, a4])
    case("sort: label (A-Z, unlabelled last)", ids(sort="label")[-1] == a3 and ids(sort="label")[0] == a4)
    case("sort: platform", [i for i in ids(sort="platform")][:2] == [a4, a1])
    case("bad sort falls back to newest", ids(sort="x; drop table ads") == [a4, a3, a2, a1])
    j = c.get("/v1/items", headers=H).json()
    case("each item says how it came in", {i["id"]: i["via"] for i in j["items"]}[a1] == "instagram")
    case("facets: people, ways in, IG senders", {"by": "dana@x.com", "n": 2} in j["facets"]["by"]
         and {"via": "instagram", "n": 1} in j["facets"]["via"] and j["facets"]["senders"][0]["sender"] == "@dana.creates")
    s = c.post("/v1/save", headers=H, data={"page_url": "https://www.instagram.com/reel/DBnew1/", "fetch": "1", "source": "instagram-dm",
                                            "sender": "@dana.creates", "sender_name": "Dana K"}).json()
    case("DM save keeps who sent it", json.loads(L.q("select extra from ads where id=?", (s["id"],), one=True)["extra"])["sender"] == "@dana.creates")

logged = OUT.getvalue()
case("nothing printed or logged contains a cookie value", not any(s in logged for s in (SECRET_IG, SECRET_IG2, SECRET_FB, "csrfTOKENabc123")))
print(f"{ok}/{n} OK")
sys.exit(0 if ok == n else 1)
