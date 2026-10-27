"""Saver accounts: ONE company-owned Instagram account and ONE Facebook account the Ad Library downloads AS.

Why: Instagram and Facebook hide many posts / reels from anonymous downloads ("login required"). yt-dlp with the saver
account's session cookies gets them. The same Instagram session also reads the saver account's DMs, so the team can
share posts to it (see ig_inbox - UNOFFICIAL, switchable off in the Studio).

Secrets: only the few cookies that matter (IG: sessionid, ds_user_id, csrftoken; FB: c_user, xs, datr), sent once by an
admin through the extension ("Connect as saver account"). Stored ENCRYPTED (Fernet, key in LIBRARY_VAULT_KEY, both files
mode 600), decrypted only in memory or into a private 600 temp cookie file for the length of one yt-dlp run. Never
logged, never returned by any endpoint, never put in error messages.
Health: check() asks the site "who am I" with the session -> ok (@handle) / login_required / checkpoint / rate_limited;
the Studio's timer alerts the admin on Telegram when it isn't ok. Gentle use: 25-60 s random gap between uses of an
account and at most ACCOUNT_HOURLY (40) uses an hour - downloads and inbox reads queue behind that.
"""
import contextlib
import json
import os
import pathlib
import random
import re
import tempfile
import threading
import time
import uuid

import httpx

VAULT = pathlib.Path(os.getenv("LIBRARY_VAULT", "/var/lib/ad-library/accounts"))
VAULT_KEY = pathlib.Path(os.getenv("LIBRARY_VAULT_KEY", "/etc/ai-secrets/ad-library-vault.key"))
PLATFORMS = ("instagram", "facebook")
# MULTI-CONNECT: Instagram has a MAIN slot ("instagram") and a BACKUP slot ("instagram_backup"). Everything works as
# one account as before; the backup is only used while the main one isn't usable (see active_ig), and the main takes
# over again by itself as soon as its health check is ok.
IG_SLOTS = ("instagram", "instagram_backup")
SLOTS = PLATFORMS + ("instagram_backup",)
NEEDED = {"instagram": ("sessionid", "ds_user_id", "csrftoken"), "facebook": ("c_user", "xs", "datr"),
          "instagram_backup": ("sessionid", "ds_user_id", "csrftoken")}
DOMAIN = {"instagram": "instagram.com", "facebook": "facebook.com", "instagram_backup": "instagram.com"}


def base(platform: str) -> str:
    """instagram_backup -> instagram (the site it logs in to)."""
    return "instagram" if platform in IG_SLOTS else platform
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
IG_APP_ID = "936619743392459"                  # instagram.com's own web app id (public, sent by every browser)
GAP = (int(os.getenv("ACCOUNT_GAP_MIN", "25")), int(os.getenv("ACCOUNT_GAP_MAX", "60")))
HOURLY = int(os.getenv("ACCOUNT_HOURLY", "40"))
TRANSPORT = None                               # tests put an httpx.MockTransport here
_lk = threading.Lock()
_meta_q = None                                 # library_service.q, set by init()


def init(q) -> None:
    global _meta_q
    _meta_q = q
    q("create table if not exists accounts(platform text primary key, meta text)")


def platform_for(url: str) -> str:
    h = (httpx.URL(url).host or "") if url.startswith("http") else ""
    if h.endswith("instagram.com"):
        return "instagram"
    if h.endswith("facebook.com") or h.endswith("fb.watch"):
        return "facebook"
    return ""


# ---------- vault ----------
def _fernet():
    from cryptography.fernet import Fernet
    if not VAULT_KEY.exists():
        VAULT_KEY.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(VAULT_KEY, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(Fernet.generate_key())
    return Fernet(VAULT_KEY.read_bytes().strip())


def _write_private(p: pathlib.Path, data: bytes) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(p.parent, 0o700)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, p)


def meta(platform: str) -> dict:
    r = _meta_q("select meta from accounts where platform=?", (platform,), one=True)
    return json.loads(r["meta"]) if r else {}


def _set_meta(platform: str, **kv) -> dict:
    m = {**meta(platform), **kv}
    _meta_q("insert or replace into accounts values(?,?)", (platform, json.dumps(m)))
    return m


def connect(platform: str, cookies: list, by: str) -> dict:
    """Admin sent the saver account's cookies (from the extension). Keeps only the needed ones, encrypted."""
    if platform not in SLOTS:
        raise ValueError("unknown platform")
    keep = {}
    for c in cookies or []:
        if not isinstance(c, dict):
            continue
        name, dom = str(c.get("name") or ""), str(c.get("domain") or "").lstrip(".").lower()
        if name in NEEDED[platform] and (dom == DOMAIN[platform] or dom.endswith("." + DOMAIN[platform])):
            v = str(c.get("value") or "")
            if v and len(v) <= 4096 and re.fullmatch(r"[\x21-\x7e]+", v):
                keep[name] = {"name": name, "value": v, "domain": "." + DOMAIN[platform], "path": "/",
                              "expires": int(float(c.get("expirationDate") or c.get("expires") or 0)), "secure": True}
    missing = [n for n in NEEDED[platform] if n not in keep]
    if missing:
        raise ValueError(f"missing {', '.join(missing)} - log in to the saver account in this Chrome profile first")
    uid = keep["ds_user_id" if base(platform) == "instagram" else "c_user"]["value"]
    if platform in IG_SLOTS:                       # main and backup must be two different accounts
        other = IG_SLOTS[1 - IG_SLOTS.index(platform)]
        if status(other)["connected"] and status(other)["user_id"] == uid:
            raise ValueError("this is the same Instagram account that is already connected as the "
                             + ("backup" if platform == "instagram" else "main") + " saver account - log in to a different one")
    _store(platform, list(keep.values()))
    sess = keep["sessionid" if base(platform) == "instagram" else "xs"]["expires"]      # the session cookie decides
    exp = [sess] if sess else [c["expires"] for c in keep.values() if c["expires"]]
    _set_meta(platform, connected=True, by=by, connected_at=time.time(), user_id=uid, expires=min(exp) if exp else 0,
              status="unchecked", error="", handle="", checked=0)
    return status(platform)


def _store(platform: str, cookies: list) -> None:
    _write_private(VAULT / f"{platform}.enc", _fernet().encrypt(json.dumps(cookies).encode()))


def load(platform: str) -> list | None:
    p = VAULT / f"{platform}.enc"
    if not p.exists():
        return None
    try:
        return json.loads(_fernet().decrypt(p.read_bytes()))
    except Exception:  # noqa: BLE001  (wrong key / damaged file: treat as not connected, never print it)
        return None


def disconnect(platform: str, by: str = "") -> None:
    p = VAULT / f"{platform}.enc"
    if p.exists():
        p.write_bytes(os.urandom(p.stat().st_size))       # overwrite, then remove
        p.unlink()
    _set_meta(platform, connected=False, status="disconnected", error="", handle="", disconnected_by=by,
              disconnected_at=time.time())


def status(platform: str) -> dict:
    """What the Studio may show: no cookie values, ever."""
    m = meta(platform)
    on = bool(m.get("connected")) and (VAULT / f"{platform}.enc").exists()
    exp = m.get("expires") or 0
    st = m.get("status", "disconnected") if on else "disconnected"
    if on and exp and exp < time.time():
        st = "expired"
    return {"platform": platform, "connected": on, "handle": m.get("handle", ""), "user_id": m.get("user_id", "") if on else "",
            "status": st, "error": m.get("error", ""),
            "checked": m.get("checked", 0), "connected_at": m.get("connected_at", 0), "by": m.get("by", ""),
            "days_left": round((exp - time.time()) / 86400, 1) if exp else None, "usable": on and st in ("ok", "unchecked")}


def usable(platform: str) -> bool:
    return status(platform)["usable"]


def mark(platform: str, state: str, detail: str = "") -> None:
    """A download / inbox read hit a login wall or checkpoint -> stop using the account until it's reconnected or checks ok."""
    if meta(platform).get("connected"):
        _set_meta(platform, status=state, error=detail[:200], checked=time.time())


@contextlib.contextmanager
def cookie_file(platform: str):
    """A Netscape cookies.txt for yt-dlp in a private temp dir (700/600), removed afterwards. Cookies the site refreshed
    during the run are written back to the vault (keeps the session alive longer)."""
    cookies = load(platform)
    if not cookies:
        yield None
        return
    d = tempfile.mkdtemp(prefix="adlib-ck-")
    os.chmod(d, 0o700)
    p = pathlib.Path(d) / "cookies.txt"
    lines = ["# Netscape HTTP Cookie File"]
    for c in cookies:
        lines.append("\t".join([c["domain"], "TRUE", c.get("path") or "/", "TRUE", str(c.get("expires") or 0), c["name"], c["value"]]))
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    try:
        yield str(p)
    finally:
        try:
            fresh = {}
            for line in p.read_text().splitlines():
                parts = line.split("\t")
                if len(parts) == 7 and not line.startswith("#") and parts[5] in NEEDED[platform] and parts[6]:
                    fresh[parts[5]] = {"name": parts[5], "value": parts[6], "domain": "." + DOMAIN[platform], "path": "/",
                                       "expires": int(parts[4] or 0), "secure": True}
            old = {c["name"]: c for c in cookies}
            if fresh and set(fresh) >= set(NEEDED[platform]) and any(fresh[k]["value"] != old.get(k, {}).get("value") for k in fresh):
                _store(platform, list(fresh.values()))
        except Exception:  # noqa: BLE001
            pass
        for f in pathlib.Path(d).glob("*"):
            f.unlink(missing_ok=True)
        os.rmdir(d)


# ---------- gentle use ----------
_uses: dict = {}
_next_gap: dict = {}


def slot_wait(platform: str) -> float:
    """Seconds until this account may be used again (0 = now)."""
    now = time.time()
    with _lk:
        u = [t for t in _uses.get(platform, []) if t > now - 3600]
        _uses[platform] = u
        w = 0.0
        if u:
            w = max(w, u[-1] + _next_gap.get(platform, GAP[0]) - now)
        if len(u) >= HOURLY:
            w = max(w, u[0] + 3600 - now)
        return max(0.0, w)


def slot_take(platform: str) -> None:
    with _lk:
        _uses.setdefault(platform, []).append(time.time())
        _next_gap[platform] = random.uniform(*GAP)


# ---------- health ----------
def classify(platform: str, code: int, location: str, body: str) -> tuple[str, str]:
    """-> (ok | login_required | checkpoint | rate_limited | error, handle-or-detail). Pure (tested on saved answers)."""
    loc, b = (location or "").lower(), body or ""
    low = b[:20000].lower()
    if "checkpoint" in loc or "/challenge" in loc or "checkpoint_required" in low or "challenge_required" in low:
        return "checkpoint", "the site wants the account to confirm it's real (open it in Chrome and follow the steps)"
    if code == 429 or "please wait a few minutes" in low or "rate limit" in low or "temporarily blocked" in low:
        return "rate_limited", "the site is slowing the account down - it rests for a while"
    if "/accounts/login" in loc or "/login" in loc or '"login_required"' in low or "login_required" in low or code in (401,):
        return "login_required", "the session ended (logged out or expired) - reconnect the saver account"
    if base(platform) == "instagram" and code == 200:
        try:
            u = (json.loads(b).get("user") or {})
            if u.get("username"):
                return "ok", u["username"]
        except ValueError:
            pass
        return "error", "unexpected answer from Instagram"
    if platform == "facebook" and code == 200:
        if 'id="login_form"' in low:
            return "login_required", "the session ended (logged out or expired) - reconnect the saver account"
        m = re.search(r'"USER_ID":"(\d+)"', b) or re.search(r'"actorID":"(\d+)"', b)
        if m and m[1] != "0":
            name = re.search(r'"NAME":"([^"]{1,80})"', b) or re.search(r'"SHORT_NAME":"([^"]{1,80})"', b)
            return "ok", (name[1].encode().decode("unicode_escape", "ignore") if name else "id " + m[1])
        return "login_required", "Facebook didn't recognise the session"
    return "error", f"unexpected answer ({code})"


def _client(platform: str, cookies: list) -> httpx.Client:
    jar = httpx.Cookies()
    for c in cookies:
        jar.set(c["name"], c["value"], domain=c["domain"], path="/")
    h = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    if base(platform) == "instagram":
        csrf = next((c["value"] for c in cookies if c["name"] == "csrftoken"), "")
        h.update({"X-IG-App-ID": IG_APP_ID, "X-CSRFToken": csrf, "X-Requested-With": "XMLHttpRequest",
                  "Referer": "https://www.instagram.com/direct/inbox/", "Origin": "https://www.instagram.com"})
    return httpx.Client(cookies=jar, headers=h, timeout=25, follow_redirects=False, transport=TRANSPORT)


def check(platform: str) -> dict:
    cookies = load(platform)
    if not cookies:
        return status(platform)
    url = ("https://www.instagram.com/api/v1/accounts/current_user/?edit=true" if base(platform) == "instagram"
           else "https://www.facebook.com/settings/?tab=account")
    try:
        with _client(platform, cookies) as c:
            r = c.get(url)
        state, detail = classify(platform, r.status_code, r.headers.get("location", ""), r.text)
    except httpx.HTTPError as e:
        state, detail = "error", f"couldn't reach {platform} ({type(e).__name__})"
    kv = {"status": state, "checked": time.time(), "error": "" if state == "ok" else detail}
    if state == "ok":
        kv["handle"] = detail
    _set_meta(platform, **kv)
    return status(platform)


# ---------- Instagram DMs (UNOFFICIAL: instagram.com's own web endpoints, with the saver session) ----------
IG = "https://www.instagram.com"
URLISH = re.compile(r"https?://[^\s<>\"'\\]+")
IG_CODE = re.compile(r"instagram\.com/(?:[\w.]+/)?(p|reels?|tv)/([\w-]{5,})")


def _ig_urls(item: dict) -> tuple[str, list]:
    """One DM item -> (kind, post links in it)."""
    t = item.get("item_type") or ""
    urls = []

    def code(media, kind=None):
        if isinstance(media, dict) and media.get("code"):
            k = kind or ("reel" if media.get("product_type") == "clips" else "p")
            urls.append(f"https://www.instagram.com/{k}/{media['code']}/")
    if t == "clip":
        code((item.get("clip") or {}).get("clip"), "reel")
    elif t in ("media_share", "felix_share"):
        code(item.get("media_share") or (item.get("felix_share") or {}).get("video"))
    elif t.startswith("xma"):
        for x in item.get(t) or item.get("xma_media_share") or []:
            if isinstance(x, dict) and x.get("target_url"):
                urls.append(x["target_url"])
    elif t == "link":
        lk = item.get("link") or {}
        u = (lk.get("link_context") or {}).get("link_url")
        urls += [u] if u else URLISH.findall(lk.get("text") or "")
    elif t == "text":
        urls += URLISH.findall(item.get("text") or "")
    elif t in ("story_share", "reel_share"):
        pass                                                   # stories vanish in 24 h: not saved (told in the reply)
    if not urls and t not in ("text", "story_share", "reel_share", "action_log", "like", "placeholder"):
        blob = json.dumps(item)
        urls += [f"https://www.instagram.com/{'p' if m[0] == 'p' else 'reel'}/{m[1]}/" for m in IG_CODE.findall(blob)]
    out = []
    for u in urls:
        u = u.rstrip(").,!?")
        if u not in out:
            out.append(u)
    return t, out[:5]


def ig_parse(inbox: dict, pending: bool = False) -> tuple[dict, list]:
    """instagram.com /api/v1/direct_v2/inbox/ JSON -> (viewer, [items from OTHER people, newest first])."""
    viewer = inbox.get("viewer") or {}
    me = str(viewer.get("pk") or viewer.get("pk_id") or "")
    out = []
    for th in (inbox.get("inbox") or {}).get("threads") or []:
        users = {str(u.get("pk") or u.get("pk_id")): u for u in th.get("users") or []}
        for it in th.get("items") or []:
            sender = str(it.get("user_id") or "")
            if not sender or sender == me:
                continue
            kind, urls = _ig_urls(it)
            u = users.get(sender, {})
            ts = int(it.get("timestamp") or 0)
            out.append({"item_id": str(it.get("item_id")), "thread_id": str(th.get("thread_id")), "sender_pk": sender,
                        "sender": u.get("username", ""), "sender_name": u.get("full_name", ""),
                        "ts": ts / 1e6 if ts > 1e13 else ts, "type": kind, "urls": urls,
                        "text": (it.get("text") or "")[:300] if kind == "text" else "", "pending": pending})
    return {"pk": me, "username": viewer.get("username", "")}, out


IG_RETRY_WAIT = 3.0                                # tests set 0


def _ig_call(method: str, path: str, slot: str = "instagram", **kw) -> dict:
    """One call to instagram.com as the saver account. Network blips / Instagram 5xx are tried once more after a short
    pause (only GETs: a repeated POST could send a reply twice). Always raises RuntimeError / PermissionError with a
    plain-English reason, never a raw httpx error."""
    cookies = load(slot)
    if not cookies:
        raise PermissionError("the Instagram saver account isn't connected")
    tries = 2 if method == "GET" else 1
    for k in range(tries):
        try:
            with _client(slot, cookies) as c:
                r = c.request(method, IG + path, **kw)
        except httpx.HTTPError as e:
            if k + 1 < tries:
                time.sleep(IG_RETRY_WAIT)
                continue
            raise RuntimeError(f"couldn't reach Instagram ({type(e).__name__})") from None
        if r.status_code >= 500 and k + 1 < tries:
            time.sleep(IG_RETRY_WAIT)
            continue
        break
    if r.status_code != 200 or r.headers.get("location"):
        state, detail = classify("instagram", r.status_code, r.headers.get("location", ""), r.text)
        if state not in ("ok", "error"):           # login wall / checkpoint / rate limit -> the account rests
            mark(slot, state, detail)
        raise RuntimeError(detail if state != "error" else f"Instagram answered {r.status_code}")
    try:
        return r.json()
    except ValueError:
        raise RuntimeError("unexpected answer from Instagram")  # noqa: B904


def active_ig() -> str:
    """Which Instagram saver account is doing the work right now: the main one while it is usable, else the backup,
    else "" (none). The main takes over again automatically once its status is ok / unchecked."""
    for sl in IG_SLOTS:
        if usable(sl):
            return sl
    return ""


def active(platform: str) -> str:
    """The account slot to use for a download of this platform ("instagram" -> main or backup)."""
    if platform == "instagram":
        return active_ig() or "instagram"
    return platform


_last_slot = {"slot": "instagram"}                 # the account the last inbox read came from: replies go out as it


def ig_inbox(limit: int = 20) -> dict:
    """The saver account's DMs (main inbox + message requests) -> {"viewer", "items", "warnings", "slot"} or {"busy": s}.
    The main inbox must work; a failing message-request list only adds a warning (its DMs are read on the next pass).
    If the main account fails during the read, the backup (if connected and usable) is tried in the same pass."""
    cands = [sl for sl in IG_SLOTS if usable(sl)]
    if not cands:
        return {"error": "the Instagram saver account isn't connected or needs attention", "items": []}
    last_err = None
    for sl in cands:
        w = slot_wait(sl)
        if w > 0:                                  # resting between uses is not a failure: never switch account for it
            return {"busy": round(w), "items": []}
        try:
            out = _ig_inbox_one(sl, limit)
        except (RuntimeError, PermissionError) as e:
            last_err = e
            continue
        _last_slot["slot"] = sl
        return out
    raise last_err


def _ig_inbox_one(slot: str, limit: int) -> dict:
    slot_take(slot)
    items, viewer, warnings = [], {}, []
    for path, pend in (("/api/v1/direct_v2/inbox/", False), ("/api/v1/direct_v2/pending_inbox/", True)):
        try:
            j = _ig_call("GET", path, slot, params={"persistentBadging": "true", "limit": str(limit), "thread_message_limit": "10"})
        except RuntimeError as e:
            if not pend:
                raise
            warnings.append(f"message requests not read: {e}")
            continue
        v, its = ig_parse(j, pend)
        viewer = viewer or v
        items += its
    if viewer.get("username"):
        _set_meta(slot, handle=viewer["username"])
    return {"viewer": viewer, "items": sorted(items, key=lambda x: x["ts"]), "warnings": warnings, "slot": slot}


def ig_reply(thread_id: str, text: str, pending: bool = False, slot: str = "") -> bool:
    if not re.fullmatch(r"\d{5,40}", thread_id or ""):
        return False
    slot = slot if slot in IG_SLOTS else _last_slot["slot"]      # a conversation belongs to the account that read it
    if pending:                                    # a message request: accept it first, or the reply can't be sent
        _ig_call("POST", f"/api/v1/direct_v2/threads/{thread_id}/approve/", slot)
    _ig_call("POST", "/api/v1/direct_v2/threads/broadcast/text/", slot,
             data={"thread_ids": f"[{thread_id}]", "text": text[:500], "client_context": str(uuid.uuid4().int)[:19],
                   "action": "send_item"})
    return True
