import time as _t0mod
_BOOT_T0 = _t0mod.time()
import base64
import hashlib
import hmac
import html
import http.client
import threading
import json
import io
import os
import re
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, request
from pypdf import PdfReader

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5-mini")
# Smart chat AI: Groq (free, OpenAI-compatible) first, OpenAI as fallback. The AI only turns a
# user's message into search keywords/filters; every result still comes from the database.
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_API_KEY_2 = os.environ.get("GROQ_API_KEY_2", "").strip()
GROQ_API_KEY_3 = os.environ.get("GROQ_API_KEY_3", "").strip()
# Backup keys: if key 1 is rate-limited / invalid / down, key 2 (then 3) takes over at once.
GROQ_KEYS = [k for k in dict.fromkeys([GROQ_API_KEY, GROQ_API_KEY_2, GROQ_API_KEY_3]) if k]
GROQ_MODEL = os.environ.get("GROQ_MODEL", "").strip()
AI_TURNS_PER_CHAT = 4          # AI calls per user per conversation (conversation = 30 min context)
AI_DAILY_PER_USER = int(os.environ.get("AI_DAILY_PER_USER", "12") or 12)
AI_DAILY_TOTAL = int(os.environ.get("AI_DAILY_LIMIT", "2000") or 2000)
AI_UNDERSTAND = os.environ.get("AI_UNDERSTAND", "1").strip().lower() not in {"0", "off", "false", "no"}
AI_UNDERSTAND_PER_USER = int(os.environ.get("AI_UNDERSTAND_DAILY", "40") or 40)   # fresh AI readings per user per IST day
AI_UNDERSTAND_CAP = 4.0          # seconds; then the local parser answers
UNDERSTAND_CACHE_TTL = 24 * 3600
AI_RANK = os.environ.get("AI_RANK", "").strip().lower() in {"1", "true", "on", "yes"}
# Owner IDs from Vercel env. Accept any separator/format people paste:
# "123,456", "123 456", '"123"', "[123]", "@123", "-123", newlines...
ADMIN_USER_IDS = {
    int(x) for x in re.findall(r"\d+", os.environ.get("ADMIN_USER_IDS", ""))
}

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
# AI keys added from inside the bot (Admin Panel -> 🔑 AI Keys), stored in bot_settings "ai_keys".
_DBKEYS: Dict[str, Any] = {"t": 0.0, "v": []}
# Telegram sends this value in the X-Telegram-Bot-Api-Secret-Token header on every
# webhook call, so forged updates (fake "admin" messages) can be rejected.
# Set WEBHOOK_SECRET in Vercel to choose your own; otherwise it is derived from BOT_TOKEN.
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip() or (
    hashlib.sha256(f"filesbull-webhook:{BOT_TOKEN}".encode("utf-8")).hexdigest() if BOT_TOKEN else ""
)


def log_error(where: str) -> None:
    # Printed tracebacks show up in Vercel -> Project -> Logs. Never print the token.
    text = traceback.format_exc()
    for secret in (BOT_TOKEN, OPENAI_API_KEY, SUPABASE_KEY, *GROQ_KEYS, *[x.get("k", "") for x in _DBKEYS["v"]],
                   *[k for v in globals().get("ENV_AI_KEYS", {}).values() for k in v],
                   *globals().get("_BIG_SECRETS", [])):
        if secret:
            text = text.replace(secret, "***")
    print(f"[filesbull] error in {where}:\n{text}", flush=True)


# ---- diagnostics: per-call timing trace + dry run (used only by /api/cbdiag; off for real updates)
_DIAG: Dict[str, Any] = {"on": False, "dry": False, "t0": 0.0, "calls": [], "sent": []}
_TG_READ = {"getMe", "getChat", "getChatMember", "getFile", "getWebhookInfo", "getChatMemberCount", "getMyCommands",
            "getChatAdministrators"}


def diag_label(url: str, method: str) -> str:
    if "api.telegram.org" in url:
        return "tg:" + url.rsplit("/", 1)[-1].split("?")[0]
    if SUPABASE_URL and url.startswith(SUPABASE_URL):
        rest = url[len(SUPABASE_URL) + 9:]
        tbl = rest.split("?")[0]
        m = re.search(r"key=eq\.([^&]+)", rest)
        return f"db:{method} {tbl}" + (f" {urllib.parse.unquote(m.group(1)).split(':')[0]}" if m else "")
    return "ext:" + urllib.parse.urlparse(url).netloc


# Keep-alive HTTPS connections per host, reused across calls (and across updates on a warm instance):
# a fresh TLS handshake to Supabase/Telegram costs ~200 ms per call, a reused connection one round trip.
_POOL: Dict[str, List[Any]] = {}
_POOL_LOCK = threading.Lock()
_POOL_STATS = {"new": 0, "reused": 0}


def _pool_get(host: str, timeout: float) -> Any:
    with _POOL_LOCK:
        free = _POOL.setdefault(host, [])
        conn = free.pop() if free else None
    if conn is None:
        _POOL_STATS["new"] += 1
        conn = http.client.HTTPSConnection(host, timeout=timeout)
    else:
        _POOL_STATS["reused"] += 1
    conn.timeout = timeout
    if conn.sock is not None:
        try:
            conn.sock.settimeout(timeout)
        except Exception:
            pass
    return conn


def _pool_put(host: str, conn: Any) -> None:
    with _POOL_LOCK:
        free = _POOL.setdefault(host, [])
        if len(free) < 8:
            free.append(conn)
            return
    conn.close()


def http_raw(url: str, method: str = "GET", body: Optional[bytes] = None, headers: Optional[Dict[str, str]] = None,
             timeout: float = 20) -> Any:
    """One HTTPS request on a pooled keep-alive connection -> (status, headers, body bytes).
    HTTP errors raise urllib.error.HTTPError (same as urllib) so callers keep their error handling."""
    u = urllib.parse.urlsplit(url)
    if u.scheme != "https":
        req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, response.headers, response.read()
    path = u.path + ("?" + u.query if u.query else "")
    for attempt in (1, 2):
        conn = _pool_get(u.netloc, timeout)
        reused = conn.sock is not None
        try:
            conn.request(method, path or "/", body=body, headers=headers or {})
            resp = conn.getresponse()
            data = resp.read()
        except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError, http.client.CannotSendRequest,
                http.client.BadStatusLine) as exc:
            conn.close()
            if attempt == 1 and reused:
                continue      # the server closed an idle keep-alive connection: once more on a fresh one
            raise urllib.error.URLError(exc) from None
        except Exception:
            conn.close()
            raise
        if resp.getheader("Connection", "").lower() == "close" or resp.will_close:
            conn.close()
        else:
            _pool_put(u.netloc, conn)
        if resp.status >= 400:
            raise urllib.error.HTTPError(url, resp.status, resp.reason, resp.headers, io.BytesIO(data))
        return resp.status, resp.headers, data
    raise urllib.error.URLError("unreachable")


def http_send(url: str, method: str, body: Optional[bytes], headers: Dict[str, str], timeout: float) -> Dict[str, Any]:
    _, _, data = http_raw(url, method, body, headers, timeout)
    return json.loads(data.decode("utf-8")) if data else {}


def http_json(url: str, method: str = "GET", payload: Optional[Dict[str, Any]] = None,
              headers: Optional[Dict[str, str]] = None, timeout: float = 20) -> Dict[str, Any]:
    body = None
    # A browser-like/app User-Agent: Cloudflare-fronted APIs (Groq) answer 403 "error code 1010"
    # to Python's default "Python-urllib" agent.
    final_headers = {"Content-Type": "application/json", "User-Agent": "filesbull-bot/1.0 (+https://filesbull.vercel.app)"}
    if headers:
        final_headers.update(headers)
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
    t_call = time.monotonic()
    try:
        return http_send(url, method, body, final_headers, timeout)
    except urllib.error.HTTPError as exc:
        # Surface the API's own reason (e.g. Telegram "Bad Request: chat not found",
        # Supabase "column ... does not exist") instead of a bare "HTTP Error 400".
        try:
            detail = json.loads(exc.read().decode("utf-8", "replace"))
            if isinstance(detail, list) and detail and isinstance(detail[0], dict):
                # Gemini's OpenAI-compatible errors come as [{"error": {"code", "message", "status"}}]
                detail = detail[0]
                if isinstance(detail.get("error"), dict) and detail["error"].get("message"):
                    detail = {"error": str(detail["error"].get("status") or "") + " " + str(detail["error"]["message"])}
            reason = detail.get("description") or detail.get("message") or detail.get("error") or str(detail)
            params = detail.get("parameters") if isinstance(detail, dict) else None
            if isinstance(params, dict):
                if params.get("migrate_to_chat_id"):
                    reason = f"{reason} (migrate_to_chat_id={params['migrate_to_chat_id']})"
                if params.get("retry_after") and "retry after" not in str(reason).lower():
                    reason = f"{reason} (retry after {params['retry_after']})"
        except Exception:
            reason = exc.reason
        raise RuntimeError(f"HTTP {exc.code}: {str(reason)[:300]}") from None
    finally:
        if _DIAG["on"]:
            _DIAG["calls"].append([int((t_call - _DIAG["t0"]) * 1000), int((time.monotonic() - t_call) * 1000), diag_label(url, method)])


def telegram(method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not configured")
    if _DIAG["dry"] and method not in _TG_READ:
        _DIAG["sent"].append({"ms": int((time.monotonic() - _DIAG["t0"]) * 1000), "m": method,
                              "text": str(payload.get("text") or payload.get("caption") or "")[:160],
                              "alert": bool(payload.get("show_alert")),
                              "cb": [b.get("callback_data") for row in ((payload.get("reply_markup") or {}).get("inline_keyboard") or [])
                                     for b in row if b.get("callback_data")]})
        return {"ok": True, "result": {"message_id": 1, "chat": {"id": payload.get("chat_id")}}}
    return http_json(f"{TG_API}/{method}", "POST", payload)


def supabase(path: str, method: str = "GET", payload: Any = None,
             query: str = "") -> Any:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("Supabase environment variables are not configured")
    url = f"{SUPABASE_URL}/rest/v1/{path}{query}"
    if method != "GET" and (path == "books" or (path == "bot_settings" and "catmap" in json.dumps(payload or query, default=str))):
        books_changed()
    if path == "bot_settings" and method != "GET":
        if method == "POST" and isinstance(payload, dict) and payload.get("key"):
            settings_forget(f"key=eq.{urllib.parse.quote(str(payload['key']))}")
        else:
            settings_forget(query)
    if _DIAG["dry"] and method != "GET":
        _DIAG["sent"].append({"ms": int((time.monotonic() - _DIAG["t0"]) * 1000), "m": f"db:{method} {path}"})
        return []
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Prefer": "return=representation",
    }
    if method == "POST" and "on_conflict=" in query:
        headers["Prefer"] = "return=representation,resolution=merge-duplicates"
    return http_json(url, method, payload, headers)


def is_admin(user_id: Optional[int]) -> bool:
    return bool(user_id and user_id in ADMIN_USER_IDS)


def is_owner(user_id: Optional[int]) -> bool:
    return is_admin(user_id)


_SUBADMIN_CACHE: Dict[int, Any] = {}


def is_subadmin(user_id: Optional[int]) -> bool:
    if is_admin(user_id):
        return True
    if not user_id:
        return False
    hit = _SUBADMIN_CACHE.get(user_id)
    if hit and time.time() - hit[0] < 20:
        return hit[1]
    try:
        value = bool(supabase("admins", "GET", query=f"?user_id=eq.{user_id}&select=user_id&limit=1"))
    except Exception:
        log_error("is_subadmin")
        return False
    _SUBADMIN_CACHE[user_id] = (time.time(), value)
    return value


def required_channels() -> List[Dict[str, Any]]:
    try:
        return supabase("required_channels", "GET", query="?active=eq.true&select=*&order=title")
    except Exception:
        # Logged, not silent: a broken read here would quietly switch the join gate off.
        log_error("required_channels")
        return []


# Settings cache: every read is remembered for the rest of the update (one Telegram update = one DB read per key), and
# config keys that change rarely are also kept per warm instance for SETTINGS_TTL seconds. Every write/delete through
# set_setting()/supabase("bot_settings", ...) updates/drops the cached copy, so this instance never serves its own stale value.
SETTINGS_TTL = 30.0
_SET_TTL_KEYS = ("log_chat_id", "backup_chat_id", "ai_naming", "cat_registry", "catmap")
_SET_CACHE: Dict[str, Any] = {}


def _set_memo() -> Dict[str, Any]:
    m = _REQ.get("_set")
    if m is None:
        m = _REQ["_set"] = {}
    return m


def _set_ttl_ok(key: str) -> bool:
    return key in _SET_TTL_KEYS or key.startswith(("aimodels", "admsearch:"))


SETTINGS_MEMO = [True]


def get_setting(key: str) -> Optional[str]:
    if not SETTINGS_MEMO[0]:
        _REQ.pop("_set", None)
    memo = _set_memo()
    if key in memo:
        return memo[key]
    hit = _SET_CACHE.get(key)
    if hit and time.monotonic() - hit[0] < SETTINGS_TTL:
        memo[key] = hit[1]
        return hit[1]
    try:
        rows = supabase("bot_settings", "GET", query=f"?key=eq.{urllib.parse.quote(key)}&select=value&limit=1")
        val = rows[0].get("value") if rows else None
    except Exception:
        return None
    memo[key] = val
    if _set_ttl_ok(key):
        _SET_CACHE[key] = (time.monotonic(), val)
    return val


def prefetch_settings(keys: List[str]) -> None:
    """One DB read for several settings (instead of one round trip each)."""
    if not SETTINGS_MEMO[0]:
        return
    memo = _set_memo()
    need = [k for k in dict.fromkeys(keys) if k not in memo and
            not (_SET_CACHE.get(k) and time.monotonic() - _SET_CACHE[k][0] < SETTINGS_TTL)]
    if not need:
        return
    try:
        q = ",".join(('"' + k.replace('"', "") + '"') if re.search(r'[,()" .]', k) else k for k in need)
        rows = supabase("bot_settings", "GET", query=f"?key=in.({urllib.parse.quote(q, safe=',')})&select=key,value")
    except Exception:
        log_error("prefetch_settings")
        return
    got = {r.get("key"): r.get("value") for r in rows or []}
    for k in need:
        memo[k] = got.get(k)
        if _set_ttl_ok(k):
            _SET_CACHE[k] = (time.monotonic(), got.get(k))


def settings_forget(query: str = "") -> None:
    """A write/delete on bot_settings: drop the cached copies it may touch."""
    m = re.search(r"key=eq\.([^&]+)", query or "")
    memo = _set_memo()
    if m:
        k = urllib.parse.unquote(m.group(1))
        memo.pop(k, None)
        _SET_CACHE.pop(k, None)
    else:
        memo.clear()
        _SET_CACHE.clear()


def set_setting(key: str, value: str) -> None:
    supabase("bot_settings", "POST", {"key": key, "value": value}, "?on_conflict=key")
    _set_memo()[key] = value
    if _set_ttl_ok(key):
        _SET_CACHE[key] = (time.monotonic(), value)


_EXEC: List[Any] = []


def par(*fns: Any) -> List[Any]:
    """Run independent lookups at the same time (each DB/Telegram call is a network round trip). Returns results in
    order; an exception is returned in its slot (callers decide)."""
    if len(fns) <= 1:
        out = []
        for fn in fns:
            try:
                out.append(fn())
            except Exception as exc:
                out.append(exc)
        return out
    if not _EXEC:
        from concurrent.futures import ThreadPoolExecutor
        _EXEC.append(ThreadPoolExecutor(max_workers=12))
    memo = _set_memo()
    futs = [_EXEC[0].submit(fn) for fn in fns]
    out = []
    for f in futs:
        try:
            out.append(f.result(timeout=20))
        except Exception as exc:
            out.append(exc)
    _REQ["_set"] = memo
    return out


# Short per-instance cache for list screens (📂 Files pages, 🗂️ Categories): paging back and forth is instant.
# Any write to the books table or catmap through this instance clears it; other instances see changes within VIEW_TTL.
VIEW_TTL = [20.0]
_VIEW_CACHE: Dict[str, Any] = {}
_BOOKS_GEN = [0]


def view_cached(key: str, fn: Any) -> Any:
    hit = _VIEW_CACHE.get(key)
    if hit and hit[1] == _BOOKS_GEN[0] and time.monotonic() - hit[0] < VIEW_TTL[0]:
        return hit[2]
    val = fn()
    _VIEW_CACHE[key] = (time.monotonic(), _BOOKS_GEN[0], val)
    return val


def books_changed() -> None:
    _BOOKS_GEN[0] += 1
    _VIEW_CACHE.clear()


def _ok(v: Any, default: Any) -> Any:
    return default if isinstance(v, Exception) else v


# ---- per-request buffers: user tracking + log-chat lines are written ONCE at the end of
# each webhook call (one read + one write per user, one log message per update) so a busy
# bot does not hammer Supabase or hit Telegram's per-chat rate limit.
_LOG_QUEUE: List[str] = []
_REQ: Dict[str, Any] = {}


def audit_log(text: str) -> None:
    """Queue a line for the 📝 Log Chat; sent batched at the end of the update."""
    if text:
        _LOG_QUEUE.append(text)


def flush_logs() -> None:
    """📝 Log Chat: one short message per event (🆕 / ✏️ / 📥); the last 50 are always kept for /logs."""
    lines = [x for x in _LOG_QUEUE if x]
    _LOG_QUEUE.clear()
    if not lines:
        return
    try:
        remember_logs(lines)
    except Exception:
        log_error("log_recent")
    target = get_setting("log_chat_id")
    if not target:
        return
    # one message per event; a burst bigger than 6 sends the rest joined (Telegram limits ~20 msgs/min per chat)
    parts = lines[:6]
    rest = lines[6:]
    chunk = ""
    for line in rest:
        if len(chunk) + len(line) > 3500:
            parts.append(chunk)
            chunk = ""
        chunk = (chunk + "\n\n" + line) if chunk else line
    if chunk:
        parts.append(chunk)
    for part in parts[:9]:
        try:
            log_chat_send(part)
        except Exception:
            log_error("log chat")
            return


def backup_message(source_chat_id: int, source_message_id: int) -> Optional[Dict[str, Any]]:
    target = get_setting("backup_chat_id")
    if not target:
        return None
    try:
        return telegram("copyMessage", {"chat_id": int(target), "from_chat_id": source_chat_id, "message_id": source_message_id}).get("result")
    except Exception:
        return None


_BOT_INFO: Dict[str, Any] = {}


def bot_info() -> Dict[str, Any]:
    if not _BOT_INFO:
        _BOT_INFO.update(telegram("getMe", {}).get("result", {}) or {})
    return _BOT_INFO


def bot_mention() -> str:
    try:
        name = bot_info().get("username")
        return f"@{name}" if name else "bot"
    except Exception:
        return "bot"


def role_of(user_id: Optional[int]) -> str:
    if is_admin(user_id):
        return "owner"
    if is_subadmin(user_id):
        return "admin"
    return "user"


class InputError(ValueError):
    """A problem the admin can fix; the message is shown to them as-is (Hinglish)."""


PRIVATE_LINK_MSG = ("Ye private invite link (t.me/+...) hai, isse bot channel pehchan nahi sakta.\n"
                    "Iske bajaye:\n1. Bot ko us channel me admin banao\n2. Phir channel ki koi bhi post yahan forward karo "
                    "(ya channel ka -100... ID bhejo).")


def parse_chat_ref(text: str) -> Any:
    """@username, username, t.me/username, https://t.me/username/123, t.me/c/123/4, -100123, 123 -> getChat id."""
    t = (text or "").strip().strip("\"'<>`").strip()
    if not t:
        raise InputError("Channel ka @username, t.me link ya -100... ID bhejo.")
    m = re.match(r"^(?:https?://)?(?:www\.)?(?:t|telegram)\.(?:me|dog)/(.+)$", t, re.I)
    if m:
        parts = [p for p in m.group(1).split("?")[0].split("/") if p]
        if not parts or parts[0].startswith("+") or parts[0].lower() == "joinchat":
            raise InputError(PRIVATE_LINK_MSG)
        if parts[0].lower() == "c" and len(parts) > 1 and parts[1].isdigit():
            return int("-100" + parts[1])
        if parts[0].lower() == "s" and len(parts) > 1:
            parts = parts[1:]
        t = parts[0]
    if re.fullmatch(r"-?\d+", t):
        # A channel ID copied without its -100 prefix still works.
        return int(t) if t.startswith("-") else int("-100" + t)
    t = t.lstrip("@")
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,}", t):
        return "@" + t
    raise InputError("Ye channel pehchana nahi gaya. Channel ka @username, t.me/... link ya -100... ID bhejo, "
                     "ya us channel ki koi post yahan forward karo.")


def friendly_tg_error(exc: Exception) -> str:
    msg = str(exc)
    low = msg.lower()
    if "chat not found" in low or "username" in low and "invalid" in low:
        return ("Channel nahi mila. Username/ID check karo. Private channel hai to pehle bot ko admin banao, "
                "phir channel ki koi post yahan forward karo.")
    if "member list is inaccessible" in low or "not enough rights" in low or "need administrator" in low:
        return f"Bot is channel me admin nahi hai. Pehle {bot_mention()} ko channel ka admin banao."
    if "bot was kicked" in low or "bot is not a member" in low:
        return f"Bot is channel me nahi hai. {bot_mention()} ko channel me add karke admin banao."
    return "Telegram error: " + msg[:200]


def resolve_chat(message: Optional[Dict[str, Any]], text: str) -> Dict[str, Any]:
    """Find the chat an admin means: a forwarded channel post, or typed @username / link / ID."""
    source = forwarded_source(message or {}) if message else None
    ref: Any = source.get("id") if source and source.get("id") else parse_chat_ref(text)
    try:
        chat = telegram("getChat", {"chat_id": ref}).get("result", {}) or {}
    except Exception as exc:
        if source and source.get("id"):
            chat = dict(source)
        else:
            raise InputError(friendly_tg_error(exc)) from None
    if not chat.get("id"):
        raise InputError("Channel nahi mila.")
    return chat


def bot_member(chat_id: Any) -> Dict[str, Any]:
    return telegram("getChatMember", {"chat_id": chat_id, "user_id": bot_info().get("id")}).get("result", {}) or {}


def ensure_bot_admin(chat: Dict[str, Any]) -> Dict[str, Any]:
    try:
        member = bot_member(chat["id"])
    except Exception as exc:
        raise InputError(friendly_tg_error(exc)) from None
    if member.get("status") not in {"administrator", "creator"}:
        raise InputError(f"Bot \"{chat.get('title', 'channel')}\" me admin nahi hai.\n"
                         f"Channel → Administrators → Add Admin → {bot_mention()} → Save, phir dobara bhejo.")
    return member


def make_join_link(chat: Dict[str, Any]) -> Optional[str]:
    """Public channel: t.me/username. Private: the bot's invite link (bot must be admin with invite rights)."""
    if chat.get("username"):
        return f"https://t.me/{chat['username']}"
    if chat.get("invite_link"):
        return chat["invite_link"]
    try:
        return telegram("createChatInviteLink", {"chat_id": chat["id"], "name": "filesbull join"}).get("result", {}).get("invite_link")
    except Exception:
        log_error("createChatInviteLink")
    try:
        return telegram("exportChatInviteLink", {"chat_id": chat["id"]}).get("result")
    except Exception:
        log_error("exportChatInviteLink")
    return None


def save_required_channel(chat: Dict[str, Any]) -> Dict[str, Any]:
    ensure_bot_admin(chat)
    link = make_join_link(chat)
    if not link:
        raise InputError("Is private channel ka join link nahi ban paya. Bot ko admin rights me "
                         "\"Invite users via link\" permission do, phir dobara bhejo.")
    row = {"chat_id": chat["id"], "title": chat.get("title") or str(chat["id"]), "username": chat.get("username"),
           "invite_link": link, "active": True}
    supabase("required_channels", "POST", row, "?on_conflict=chat_id")
    return row


def add_required_channel(target: str, message: Optional[Dict[str, Any]] = None) -> str:
    return save_required_channel(resolve_chat(message, target))["title"]


AUTO_SAVE_OFF = "⚠️ auto-save off (bot admin nahi)"
AUTO_SAVE_OFF_NOTE = "ℹ️ Bot admin nahi hai — purani/forward ki gayi files chalengi, nayi post apne-aap save nahi hongi"


def bot_admin_in(chat_id: Any) -> bool:
    try:
        return bot_member(chat_id).get("status") in {"administrator", "creator"}
    except Exception:
        return False


def add_source_channel(target: str, message: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Save a source channel. Never refused because the bot is not admin: files forwarded from it
    (and already indexed ones) keep working; only auto-save of NEW posts needs bot admin,
    because Telegram sends channel posts only to a bot that is admin there."""
    chat = resolve_chat(message, target)
    register_channel(chat)
    chat = dict(chat)
    chat["_bot_admin"] = bot_admin_in(chat["id"])
    return chat


def missing_required(user_id: Optional[int], channels: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Required channels this user has NOT joined. A channel the bot cannot check
    (bot not admin / removed) is skipped and logged instead of locking everyone out."""
    missing = []
    if not user_id:
        return missing
    for channel in (required_channels() if channels is None else channels):
        try:
            member = telegram("getChatMember", {"chat_id": channel["chat_id"], "user_id": user_id}).get("result", {}) or {}
        except Exception:
            log_error(f"gate check {channel.get('chat_id')}")
            continue
        status = member.get("status")
        if status in {"left", "kicked"} or (status == "restricted" and not member.get("is_member", True)):
            missing.append(channel)
    return missing


def user_has_required_access(user_id: Optional[int]) -> bool:
    if is_subadmin(user_id):
        return True
    return not missing_required(user_id)


def join_url(channel: Dict[str, Any]) -> Optional[str]:
    url = channel.get("invite_link") or (f"https://t.me/{channel['username']}" if channel.get("username") else None)
    if url:
        return url
    try:
        url = make_join_link({"id": channel["chat_id"]})
        if url:
            supabase("required_channels", "PATCH", {"invite_link": url}, f"?chat_id=eq.{channel['chat_id']}")
    except Exception:
        log_error("join link")
    return url


def esc(value: Any) -> str:
    """Escape user/channel text for parse_mode HTML (names like <Raj> or A&B stay safe)."""
    return html.escape(str(value if value is not None else ""), quote=False)


def first_name(user: Optional[Dict[str, Any]]) -> str:
    name = str((user or {}).get("first_name") or "").strip() or "dost"
    return esc(name[:40])


VERIFY_BUTTON = "✅ Joined / Verify"


def join_markup(channels: Optional[List[Dict[str, Any]]] = None, total: int = 0) -> Dict[str, Any]:
    """Join buttons for the channels still missing. With more than one join channel in total,
    each button carries the channel's name so the user knows which one is left."""
    chans = required_channels() if channels is None else channels
    named = max(total, len(chans)) > 1
    rows = []
    for channel in chans:
        url = join_url(channel)
        if url:
            label = f"📢 Join: {str(channel.get('title') or 'Channel')[:28]}" if named else "📢 Join Channel"
            rows.append([{"text": label, "url": url}])
    rows.append([{"text": VERIFY_BUTTON, "callback_data": "verify_join"}])
    return {"inline_keyboard": rows}


def gate_text(missing: List[Dict[str, Any]], left: bool = False, total: int = 0) -> str:
    many = max(total, len(missing)) > 1
    names = "\n".join(f"• {esc(c.get('title') or 'Channel')}" for c in missing) if many else ""
    if left:
        head = "⁉️ <b>Aapne hamara channel chhod diya!</b>"
        body = ("Bot ka access rok diya gaya hai 🔒\n"
                + ("Ye channels dobara join karo:\n" + names + "\n\nPhir ✅ Joined dabao." if many
                   else "Dobara channel join karo aur ✅ Joined dabao."))
        return f"{head}\n\n{body}"
    if many:
        return ("⁉️ <b>Bot use karne ke liye pehle hamare channels join karo!</b>\n\n" + names +
                "\n\nNiche har 📢 Join button dabao, join karo, phir ✅ Joined / Verify dabao.")
    return ("⁉️ <b>Bot use karne ke liye pehle hamara channel join karo!</b>\n\n"
            "Niche 📢 Join Channel dabao, join karo, phir ✅ Joined / Verify dabao.")


# ---- verified memory: who has passed the gate before (to tell "left" apart from "new").
# Stored in bot_settings as key "verified:<user_id>" (no schema change needed).
_VERIFIED_CACHE: Dict[int, bool] = {}


def was_verified(user_id: Optional[int]) -> bool:
    if not user_id:
        return False
    if user_id in _VERIFIED_CACHE:
        return _VERIFIED_CACHE[user_id]
    value = bool(get_setting(f"verified:{user_id}"))
    _VERIFIED_CACHE[user_id] = value
    return value


def mark_verified(user_id: Optional[int]) -> None:
    if not user_id or _VERIFIED_CACHE.get(user_id):
        return
    if not was_verified(user_id):
        try:
            set_setting(f"verified:{user_id}", str(int(time.time())))
        except Exception:
            log_error("mark_verified")
            return
    _VERIFIED_CACHE[user_id] = True


def send_join_gate(chat_id: int, missing: List[Dict[str, Any]], left: bool = False, total: int = 0) -> None:
    send_text(chat_id, gate_text(missing, left, total), join_markup(missing, total), "HTML")


def gate_blocks(user_id: Optional[int], chat_id: int) -> bool:
    """True (and the join prompt is sent) when a normal user still has to join channels.
    A user who passed before and is now missing gets the "you left our channel" message."""
    if is_subadmin(user_id):
        return False
    reqs = required_channels()
    missing = missing_required(user_id, reqs)
    if missing:
        note_user(None, verified=False)
        send_join_gate(chat_id, missing, left=was_verified(user_id), total=len(reqs))
        return True
    if reqs:
        mark_verified(user_id)
        note_user(None, verified=True)
    return False


# ---------------------------------------------------------------- admin panel

USER_KEYBOARD_ROWS = [
    [{"text": "🔍 Book Search Karo", "callback_data": "user_search"}],
    [{"text": "📚 PDF List", "callback_data": "user_list"}],
    [{"text": "🗂️ Categories", "callback_data": "user_categories"}],
    [{"text": "❓ Help", "callback_data": "user_help"}],
]

ADMIN_PANEL_BUTTON = [{"text": "⚙️ Admin Panel", "callback_data": "ad:panel"}]


def welcome_text(user: Optional[Dict[str, Any]], admin: bool = False) -> str:
    if admin:
        return (f"👋 <b>Namaste Admin, {first_name(user)}!</b>\n\n"
                "Book / PDF ka naam likho — ya ⚙️ Admin Panel kholo.")
    return (f"📚 <b>Files Bull me swagat hai, {first_name(user)}!</b>\n\n"
            "Book / PDF ka naam, subject ya exam likho — main turant dhoondh dunga 🔍")


def admin_start_menu() -> Dict[str, Any]:
    return {"inline_keyboard": [list(r) for r in USER_KEYBOARD_ROWS] + [list(ADMIN_PANEL_BUTTON)]}

BACK_ROW = [{"text": "🔙 Admin Panel", "callback_data": "ad:panel"}]


def user_menu() -> Dict[str, Any]:
    return {"inline_keyboard": [list(r) for r in USER_KEYBOARD_ROWS]}


def admin_menu(user_id: Optional[int] = None) -> Dict[str, Any]:
    # One clear button per row (labels <= 20 chars so they never cut off on a phone).
    # 🔑 AI Keys is owner-only, so sub-admins don't see it.
    keys_row = [{"text": "🔑 AI Keys", "callback_data": "ad:keys"}] if (user_id is None or is_owner(user_id)) else []
    return {"inline_keyboard": [
        [{"text": "➕ Join Channel Jodo", "callback_data": "ad:addreq"}],
        [{"text": "📋 Join Channels", "callback_data": "ad:reqlist"}],
        [{"text": "➕ Source Channel Jodo", "callback_data": "ad:addsrc"}],
        [{"text": "📋 Source Channels", "callback_data": "ad:srclist"}],
        [{"text": "💾 Backup Channel", "callback_data": "ad:backup"}, {"text": "📝 Log Chat", "callback_data": "ad:log"}],
        [{"text": "💾 Backup: purani files bhi", "callback_data": "ad:bkold"}],
        [{"text": "👥 Users", "callback_data": "ad:users"}, {"text": "👮 Admins", "callback_data": "ad:admins"}],
        [{"text": "📂 All Files", "callback_data": "ad:files:1"}, {"text": "🤖 AI Naming", "callback_data": "ad:ainame"}],
        [{"text": "✏️ File Rename", "callback_data": "ad:rename"}, {"text": "📊 Report", "callback_data": "ad:report"}],
        keys_row + [{"text": "🔄 Bot Restart", "callback_data": "ad:restart"}],
    ] + ([[{"text": "👤 Userbot", "callback_data": "ad:ub"}]] if (user_id is None or is_owner(user_id)) else []) + [
        [{"text": "🩺 Status", "callback_data": "ad:status"}, {"text": "❓ Admin Help", "callback_data": "ad:help"}],
        [{"text": search_mode_label(user_id), "callback_data": "ad:smode"}],
        [{"text": "🔍 Search Menu", "callback_data": "ad:usermenu"}],
    ]}


ADMIN_PANEL_TEXT = ("⚙️ <b>Admin Panel</b>\n\n"
                    "Niche se kaam chuno 👇\n"
                    "🤖 <i>Bolke bhi kaam karwa sakte ho, jaise: \"polity ka naam politics kr do\", "
                    "\"sari files backup kro\". Search ke liye book ka naam likho (ya shuru me ? lagao).</i>")

# Prompts for buttons that need input. The admin answers with a reply (ForceReply) or just
# sends the next message within 15 minutes (pending state in bot_settings).
PROMPTS = {
    "addreq": ("➕ Join channel: channel ka @username, t.me/... link ya -100... ID bhejo, ya us channel ki koi post forward karo.\n"
               "(Pehle bot ko us channel me admin banao. /cancel = band)", "@channelusername"),
    "addsrc": ("➕ Source channel: channel ka @username, t.me/... link ya -100... ID bhejo, ya us channel ki koi post forward karo.\n"
               "(Bot us channel me admin hona chahiye. /cancel = band)", "@channelusername"),
    "setbackup": ("💾 Backup channel set karo:\n"
                  "1️⃣ Telegram me naya PRIVATE channel banao (jaise \"Files Bull Backup\")\n"
                  "2️⃣ Channel → Administrators → Add Admin → bot chuno → \"Post Messages\" on → Save\n"
                  "3️⃣ Us channel ki koi bhi post yahan FORWARD karo (ya -100... ID bhejo)\n"
                  "(/cancel = band)", "Post forward karo ya -100..."),
    "setlog": ("📝 Log Chat (user track) set karo:\n"
               "1️⃣ Naya private channel/group banao (jaise \"Files Bull Users\")\n"
               "2️⃣ Bot ko wahan admin banao (Post Messages on)\n"
               "3️⃣ Uski koi post yahan FORWARD karo (ya -100... ID bhejo). \"me\" bhejo to logs isi chat me aayenge.\n"
               "👥 GROUP hai to aasaan: bot ko group me add karke admin banao, phir us group me /setlogchat bhejo.\n"
               "(/cancel = band)", "Post forward karo ya me"),
    "addadmin": ("👮 Naye admin ka Telegram numeric ID bhejo (wo bot me /myid bhejkar dekh sakta hai), ya uska koi message forward karo.\n"
                 "(/cancel = band)", "123456789"),
    "rename": ("✏️ Kis file ka naam badalna hai? File ka naam/keyword bhejo (ya ID aur naya naam: 12 Lucent GK Hindi).\n"
               "(/cancel = band)", "lucent"),
    "rename_to": ("✏️ Is file ka naya naam bhejo. (/cancel = band)", "Naya naam"),
    "addkey": ("🔑 AI ki API key yahan paste karke bhejo.\n"
               "Key wala message main turant chat se delete kar dunga 🔒\n"
               "(Groq / Gemini / Mistral / OpenRouter / NVIDIA. /cancel = band)", "API key"),
}


def show(chat_id: int, text: str, markup: Optional[Dict[str, Any]] = None, query: Optional[Dict[str, Any]] = None,
         parse_mode: Optional[str] = None) -> None:
    """Edit the button's own message (clean navigation); fall back to a new message."""
    message_id = ((query or {}).get("message") or {}).get("message_id")
    markup = safe_markup(markup)
    if message_id:
        payload: Dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text, "disable_web_page_preview": True}
        if markup:
            payload["reply_markup"] = markup
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            telegram("editMessageText", payload)
            return
        except Exception as exc:
            if "not modified" in str(exc).lower():
                return
    send_text(chat_id, text, markup, parse_mode)


def set_pending(user_id: int, action: str, extra: Any = None) -> None:
    try:
        set_setting(f"pending:{user_id}", json.dumps({"a": action, "x": extra, "ts": int(time.time())}))
    except Exception:
        log_error("set_pending")


def get_pending(user_id: int) -> Optional[Dict[str, Any]]:
    raw = get_setting(f"pending:{user_id}")
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if time.time() - int(data.get("ts", 0)) < 900 else None
    except Exception:
        return None


def clear_pending(user_id: int) -> None:
    try:
        supabase("bot_settings", "DELETE", query=f"?key=eq.{urllib.parse.quote(f'pending:{user_id}')}")
    except Exception:
        log_error("clear_pending")


def ask_input(chat_id: int, user_id: int, action: str, extra: Any = None, prefix: str = "") -> None:
    text, placeholder = PROMPTS[action]
    set_pending(user_id, action, extra)
    send_text(chat_id, prefix + text, {"force_reply": True, "input_field_placeholder": placeholder})


def prompt_action(message: Dict[str, Any]) -> Optional[str]:
    replied = ((message.get("reply_to_message") or {}).get("text") or "")
    for action, (text, _) in PROMPTS.items():
        if replied and replied.startswith(text[:30]):
            return action
    if replied.startswith("Channel ka @username"):  # prompt sent by the old version
        return "addreq"
    return None


def count_rows(table: str, query: str = "", col: str = "id") -> int:
    """Exact row count (Supabase returns at most 1000 rows per read, so don't count a list)."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("Supabase environment variables are not configured")
    url = f"{SUPABASE_URL}/rest/v1/{table}?select={col}{query}&limit=1"
    t_call = time.monotonic()
    try:
        _, hdrs, _ = http_raw(url, "GET", None, {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                                                 "Prefer": "count=exact"}, 15)
        total = (hdrs.get("Content-Range") or "").split("/")[-1]
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} counting {table}") from None
    finally:
        if _DIAG["on"]:
            _DIAG["calls"].append([int((t_call - _DIAG["t0"]) * 1000), int((time.monotonic() - t_call) * 1000), f"db:COUNT {table}"])
    return int(total) if total.isdigit() else 0


def chat_check(chat_id: Any) -> str:
    try:
        st = bot_member(chat_id).get("status")
        return "✅ bot admin" if st in {"administrator", "creator"} else f"❌ bot admin nahi ({st})"
    except Exception as exc:
        return "❌ " + friendly_tg_error(exc)[:60]


def source_check(chat_id: Any) -> str:
    """Source channels work without bot admin (forwarded/indexed files); only auto-save needs it."""
    return "✅ auto-save on" if bot_admin_in(chat_id) else AUTO_SAVE_OFF


def source_channels() -> List[Dict[str, Any]]:
    return supabase("channels", "GET", query="?select=*&active=eq.true&order=title")


def required_list_view() -> Any:
    rows = required_channels()
    lines = ["📋 Join Channels (naye users ko ye join karne padenge):"]
    kb = []
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. {r.get('title')} — {chat_check(r['chat_id'])}" + ("" if (r.get("invite_link") or r.get("username")) else " — ⚠️ join link nahi"))
        kb.append([{"text": f"❌ Hatao: {str(r.get('title'))[:28]}", "callback_data": f"ad:rmreq:{r['chat_id']}"}])
    if not rows:
        lines.append("Koi join channel nahi hai, isliye abhi gate band hai (sab users bina join kiye use kar sakte hain).")
    kb.append([{"text": "➕ Join Channel Jodo", "callback_data": "ad:addreq"}])
    kb.append(BACK_ROW)
    return "\n".join(lines), {"inline_keyboard": kb}


def source_list_view() -> Any:
    rows = source_channels()
    lines = ["📋 Source Channels (inki nayi files index hoti hain):"]
    kb = []
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. {r.get('title')} ({'@' + r['username'] if r.get('username') else r.get('chat_id')}) — {source_check(r['chat_id'])}")
        kb.append([{"text": f"❌ Hatao: {str(r.get('title'))[:28]}", "callback_data": f"ad:rmsrc:{r['chat_id']}"}])
    if not rows:
        lines.append("Koi source channel nahi hai.")
    kb.append([{"text": "➕ Source Channel Jodo", "callback_data": "ad:addsrc"}])
    kb.append(BACK_ROW)
    return "\n".join(lines), {"inline_keyboard": kb}


_TITLE_CACHE: Dict[str, Any] = {}


def chat_title(chat_id: Any) -> str:
    hit = _TITLE_CACHE.get(str(chat_id))
    if hit and time.monotonic() - hit[0] < 300:
        return hit[1]
    try:
        chat = telegram("getChat", {"chat_id": int(chat_id)}).get("result", {}) or {}
        title = chat.get("title") or chat.get("first_name") or str(chat_id)
        _TITLE_CACHE[str(chat_id)] = (time.monotonic(), title)
        return title
    except Exception:
        return f"{chat_id} (bot ko access nahi)"


def setting_view(key: str, label: str, set_cb: str, off_cb: str, about: str) -> Any:
    current = get_setting(key)
    text = f"{label}\n{about}\n\nAbhi: " + (f"✅ {chat_title(current)}" if current else "❌ OFF")
    kb = [[{"text": "✏️ Set / Badlo", "callback_data": set_cb}]]
    if current:
        kb.append([{"text": "🚫 Band karo", "callback_data": off_cb}])
    kb.append(BACK_ROW)
    return text, {"inline_keyboard": kb}


def admins_view(user_id: int) -> Any:
    try:
        subs = supabase("admins", "GET", query="?select=user_id,role&limit=100")
    except Exception:
        subs = []
    lines = ["👮 Admins", f"Owner (Vercel ADMIN_USER_IDS): {', '.join(str(x) for x in sorted(ADMIN_USER_IDS)) or 'koi nahi'}"]
    lines.append("Sub-admins: " + (", ".join(str(r.get("user_id")) for r in subs) if subs else "koi nahi"))
    kb = []
    if is_owner(user_id):
        kb.append([{"text": "➕ Admin Jodo", "callback_data": "ad:addadm"}])
        for r in subs:
            kb.append([{"text": f"❌ Hatao: {r.get('user_id')}", "callback_data": f"ad:rmadm:{r.get('user_id')}"}])
    else:
        lines.append("(Admin jodna/hatana sirf owner kar sakta hai.)")
    kb.append(BACK_ROW)
    return "\n".join(lines), {"inline_keyboard": kb}


def report_text() -> str:
    try:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        prefetch_settings(["backup_chat_id", "log_chat_id", "log_chat_status"])
        files, logs, today_logs, req, src = par(lambda: count_rows("books"), lambda: count_rows("delivery_logs"),
                                                lambda: count_rows("delivery_logs", f"&created_at=gte.{today}"),
                                                required_channels, source_channels)
        for x in (files, logs, today_logs):
            if isinstance(x, Exception):
                raise x
        req, src = len(_ok(req, [])), len(_ok(src, []))
        return (f"📊 Report\nIndexed files: {files}\nFiles bheji gayi (total): {logs}\nAaj bheji gayi: {today_logs}\n"
                f"Join channels: {req}\nSource channels: {src}\nSmart AI: {'ON (' + pname(ai_provider()) + ')' if ai_provider() else 'OFF (koi AI key nahi)'}\n"
                f"Backup: {'ON' if get_setting('backup_chat_id') else 'OFF'}\n{log_chat_line()}")
    except Exception as exc:
        return f"Report nahi ban payi: {str(exc)[:200]}"


def status_text(user_id: Optional[int]) -> str:
    role = role_of(user_id)
    role_word = {"owner": "✅ Owner (ADMIN_USER_IDS me hai)", "admin": "✅ Sub-admin (admins table)", "user": "❌ Normal user (admin nahi)"}[role]
    lines = [f"🩺 Status", f"Aapka Telegram ID: {user_id}", f"Role: {role_word}", f"Owner IDs set: {len(ADMIN_USER_IDS)}"]
    prefetch_settings(["backup_chat_id", "log_chat_id", "named_by", "understood_by", "log_chat_status"])
    mention, reqs, srcs = par(bot_mention, required_channels, source_channels)
    if not isinstance(mention, Exception):
        lines.append(f"Bot: {mention}")
    reqs = _ok(reqs, [])
    # every channel check at the same time (was one Telegram call after another)
    checks = par(*([(lambda c=r["chat_id"]: chat_check(c)) for r in reqs] +
                   ([(lambda c=r["chat_id"]: source_check(c)) for r in srcs] if not isinstance(srcs, Exception) else [])))
    lines.append(f"\nJoin gate: {'ON' if reqs else 'OFF (koi join channel nahi)'}")
    for r, ch in zip(reqs, checks[:len(reqs)]):
        lines.append(f"• {r.get('title')}: {_ok(ch, '❓')}, link {'✅' if (r.get('invite_link') or r.get('username')) else '❌'}")
    if isinstance(srcs, Exception):
        lines.append(f"\nSource channels: error {str(srcs)[:80]}")
    else:
        schecks = [_ok(c, "❓") for c in checks[len(reqs):]]
        lines.append(f"\nSource channels: {len(srcs)}")
        for r, ch in zip(srcs, schecks):
            lines.append(f"• {r.get('title')}: {ch}")
        if any(ch == AUTO_SAVE_OFF for ch in schecks):
            lines.append("ℹ️ Auto-save off wale channels ki files forward karke save karo, ya bot ko admin banao. Purani files chalti rahengi.")
    lines.append(f"Backup: {'ON' if get_setting('backup_chat_id') else 'OFF'} | {log_chat_line()}")
    try:
        nb = named_by_counts()
        lines.append(f"🏷️ Naam kaise bane: saaf naam {nb['clean']} · page padhke {nb['local_text']} (dono 0 token) · "
                     f"AI {nb['ai']} · fallback {nb['fallback']}")
    except Exception:
        pass
    try:
        ub = understood_counts()
        lines.append(f"🧠 Msg kaise samjhe: AI {ub['ai']} · cache {ub['cache']} (0 token) · bina AI (fallback) {ub['local_fallback']}"
                     f" · seedha naam {ub['exact']} (0 token)")
    except Exception:
        pass
    lines.append("\nNote: admins par join gate lagu nahi hota. Gate test karne ke liye dusre (normal) account se /start karo.")
    return "\n".join(lines)


ADMIN_HELP = (
    "❓ Admin Help\n"
    "➕ Join Channel Jodo — naye users ko ye channel join karna padega (bot wahan admin ho)\n"
    "📋 Join Channels — list, ❌ se hatao\n"
    "➕ Source Channel Jodo — is channel ki nayi files apne-aap index hongi\n"
    "📋 Source Channels — list, ❌ se hatao\n"
    "💾 Backup Channel — har nayi file ki copy yahan, source fail ho to yahan se bhejega\n"
    "💾 Backup: purani files bhi — pehle ki files bhi backup (20-20 karke)\n"
    "📝 Log Chat — naya user, kisne kaunsi file li, nayi file (user track); 📜 Purani updates Log me bhejo = ab tak ki saari files ki details\n"
    "👥 Users — kitne users, aaj active, top 10; /user ID se details\n"
    "👮 Admins — sub-admin jodo/hatao (sirf owner)\n"
    "✏️ File Rename — file ka search naam badlo\n"
    "📊 Report — files aur delivery count\n"
    "🩺 Status — aapka ID, role, channels me bot admin hai ya nahi\n"
    "🔑 AI Keys — Groq/OpenAI key bot me hi jodo/hatao, Vercel deploy nahi (sirf owner)\n"
    "🔄 Bot Restart — bot ruk jaye to webhook + menu dobara set\n"
    "📂 All Files — saari files 10-10 karke, ⬅️ / ➡️, file pe tap: 📤 Bhejo / ✏️ Rename / ❌ Hatao\n"
    "🤖 AI Naming — har nayi file ka naam file ke andar padhke apne-aap (hamesha chalu) + purane naam ek baar theek karo\n"
    "🔍 Search Mode — ON pe aapka har msg search hoga; OFF pe bot samjhega kaam hai ya search\n\n"
    "🤖 Bolke kaam (sirf admin): \"polity ka naam politics kr do\", \"sab files me Hindi ko हिंदी kr do\", "
    "\"lucent wali file hatao\", \"sari ek baar me upload kro\" (backup), \"users dikhao\", \"report\", "
    "\"bot restart kro\", \"sari files dikhao\", \"only polity ki sari pdf dikhao\", \"aaj ki files\", \"last 5 files\", "
    "\"10 MB se badi files\", \"GK channel ki files\", \"X file ko history category me daal do\", \"search mode off\", "
    "\"ai test kro\", \"ai naming band kro\", \"purane naam theek kro\". "
    "Har badlav se pehle dikhega kya badlega + ✅ Accept / ❌ Deny. Search karna ho to shuru me ? lagao (?polity)\n"
    "Hatane wale har button pe pehle ✅ Accept / ❌ Deny aayega\n\n"
    "Commands bhi chalti hain: /admin, /requirechannel @ch, /removerequired @ch, /addchannel @ch, /removechannel @ch, "
    "/channels, /requiredchannels, /setbackupchannel, /setlogchat, /addadmin ID, /removeadmin ID, /rename ID naam, "
    "/report, /stats, /status, /restart, /keys, /user ID, /backupold, /files, /myid, /cancel\n"
    "Kisi channel ki post bot ko forward karoge to wo source channel ban jayega."
)


def do_rename(chat_id: int, book_id: Any, new_title: str, user_id: Optional[int] = None,
              query: Optional[Dict[str, Any]] = None) -> None:
    """Rename one file: show old → new first, apply only on ✅ Accept (Aarav's rule)."""
    new_title = re.sub(r"\s+", " ", new_title or "").strip()[:500]
    if not new_title:
        raise InputError("Naya naam khali hai.")
    rows = supabase("books", "GET", query=f"?id=eq.{int(book_id)}&select=id,title&limit=1")
    if not rows:
        raise InputError(f"File ID {book_id} nahi mili.")
    preview_rename(chat_id, user_id or chat_id, [(int(rows[0]["id"]), str(rows[0].get("title") or ""), new_title)], query)


def handle_admin_input(message: Dict[str, Any], action: str, extra: Any) -> None:
    """The admin answered a prompt (ForceReply / pending)."""
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]
    text = (message.get("text") or message.get("caption") or "").strip()
    retry = {"inline_keyboard": [[{"text": "🔁 Dobara try", "callback_data": (f"ad:keyadd:{extra}" if action == "addkey" and extra in PROVIDERS else f"ad:{ {'rename_to': 'rename', 'addkey': 'keyadd'}.get(action, action)}")}], BACK_ROW]}
    if action == "addkey":
        # The message carries a secret: never echo it, never log it, delete it from the chat.
        clear_pending(user_id)
        if not is_owner(user_id):
            try:
                telegram("deleteMessage", {"chat_id": chat_id, "message_id": message.get("message_id")})
            except Exception:
                pass
            send_text(chat_id, "🔑 AI key sirf owner jod sakta hai. Maine message hata diya 🔒", {"inline_keyboard": [BACK_ROW]})
            return
        try:
            add_ai_key(message, chat_id, text, extra if extra in PROVIDERS else None)
        except InputError as exc:
            set_pending(user_id, action, extra)
            send_text(chat_id, f"❌ Nahi hua: {exc}\n\nSahi key dobara bhejo, ya /cancel.", retry)
        except Exception:
            print("[filesbull] error in add_ai_key (details hidden: message held a key)", flush=True)
            send_text(chat_id, "❌ Key save nahi hui, thodi der baad dobara try karo.", retry)
        return
    try:
        if action == "addreq":
            title = add_required_channel(text, message)
            clear_pending(user_id)
            send_text(chat_id, f"✅ Join channel jud gaya: {title}\nAb naye users ko /start par ye channel join karna padega.\n"
                               "(Aap admin ho, aap par gate nahi lagega — dusre account se test karo.)",
                      {"inline_keyboard": [[{"text": "📋 Join Channels", "callback_data": "ad:reqlist"}], BACK_ROW]})
        elif action == "addsrc":
            chat = add_source_channel(text, message)
            clear_pending(user_id)
            if chat.get("_bot_admin"):
                note = "Ab is channel me aane wali nayi files apne-aap save hongi.\nPurani files ke liye unhe bot ko forward karo."
            else:
                note = AUTO_SAVE_OFF_NOTE + ".\nNayi files bhi bot ko forward kar doge to save ho jayengi."
            info = file_info(message)
            src = forwarded_source(message)
            if info and src:
                # The admin forwarded a file post as the answer: index that file too.
                record = build_metadata(message, src)
                if src.get("message_id"):
                    record["source_message_id"] = src["message_id"]
                    record["source_link"] = message_link(src, src["message_id"])
                index_file(record, chat_id, message.get("message_id"), str(src.get("title") or "forward"))
                note += f"\n📄 Ye file bhi save ho gayi: {clean_text(info.get('file_name', 'file'))}"
            send_text(chat_id, f"✅ Source channel jud gaya: {chat.get('title')}\n{note}",
                      {"inline_keyboard": [[{"text": "📋 Source Channels", "callback_data": "ad:srclist"}], BACK_ROW]})
        elif action == "setbackup":
            chat = resolve_chat(message, text)
            member = ensure_bot_admin(chat)
            if chat.get("type") == "channel" and member.get("status") == "administrator" and member.get("can_post_messages") is False:
                raise InputError(f"Bot \"{chat.get('title', 'channel')}\" me admin hai par \"Post Messages\" permission off hai. Use on karke dobara bhejo.")
            try:
                telegram("sendMessage", {"chat_id": chat["id"], "text": "✅ Files Bull backup channel set ho gaya. Bot ki saari files yahan ek-ek baar (bina duplicate) save hongi.",
                                         "disable_notification": True})
            except Exception as exc:
                raise InputError("Bot is channel me message nahi bhej pa raha.\n" + friendly_tg_error(exc)) from None
            set_setting("backup_chat_id", str(chat["id"]))
            clear_pending(user_id)
            try:
                pending_n = count_rows("books", backup_pending_query(chat["id"]))
            except Exception:
                pending_n = 0
            kb = [[{"text": f"💾 Backup: purani files bhi ({pending_n})", "callback_data": "ad:bkold"}]] if pending_n else []
            send_text(chat_id, f"✅ Backup channel set: {chat.get('title')}\nAb har nayi file ek baar yahan save hogi (duplicate nahi)."
                               + (f"\n\nPurani {pending_n} files bhi save karni hain? Niche dabao 👇" if pending_n else ""),
                      {"inline_keyboard": kb + [BACK_ROW]})
        elif action == "setlog":
            if text.lower() in {"me", "yahan", "yaha", "here"}:
                chat = {"id": chat_id, "title": "Ye chat"}
            else:
                chat = resolve_chat(message, text)
            try:
                telegram("sendMessage", {"chat_id": chat["id"], "text": "✅ filesbull log chat set ho gaya. Yahan logs aayenge."})
            except Exception as exc:
                raise InputError("Bot is chat me message nahi bhej pa raha. Bot ko wahan add/admin karo.\n" + friendly_tg_error(exc)) from None
            set_setting("log_chat_id", str(chat["id"]))
            clear_pending(user_id)
            send_text(chat_id, f"✅ Log chat set: {chat.get('title')}\n\nAb tak ki saari files ki details bhi wahan bhejni hain? Niche dabao 👇",
                      {"inline_keyboard": [[{"text": "📜 Purani updates Log me bhejo", "callback_data": "ad:logfill"}], BACK_ROW]})
        elif action == "addadmin":
            if not is_owner(user_id):
                raise InputError("Admin sirf owner jod sakta hai.")
            origin = message.get("forward_origin") or {}
            target = (origin.get("sender_user") or {}).get("id") or (message.get("forward_from") or {}).get("id")
            if not target:
                digits = re.findall(r"\d{5,}", text)
                if not digits:
                    if origin.get("type") == "hidden_user":
                        raise InputError("Us user ki privacy settings ki wajah se ID nahi mili. Use bot me /myid bhejne ko kaho aur ID yahan bhejo.")
                    raise InputError("Numeric Telegram ID bhejo (jaise 123456789). Wo bot me /myid bhejkar dekh sakta hai.")
                target = int(digits[0])
            supabase("admins", "POST", {"user_id": int(target), "role": "subadmin"}, "?on_conflict=user_id")
            _SUBADMIN_CACHE.pop(int(target), None)
            set_admin_menu(int(target))
            clear_pending(user_id)
            send_text(chat_id, f"✅ Sub-admin jud gaya: {target}\nWo bot me /start karke ⚙️ Admin Panel dekh sakta hai.", {"inline_keyboard": [BACK_ROW]})
        elif action == "rename":
            m = re.match(r"^(\d+)\s+(.+)$", text, re.S)
            if m:
                clear_pending(user_id)
                do_rename(chat_id, m.group(1), m.group(2), user_id)
                return
            results = search_books(text)[:8] if text else []
            if not results:
                raise InputError("Is naam se koi file nahi mili. Dusra naam/keyword bhejo.")
            kb = [[{"text": f"{r['id']}. {str(r.get('title', ''))[:45]}", "callback_data": f"ad:rn:{r['id']}"}] for r in results]
            kb.append(BACK_ROW)
            clear_pending(user_id)
            send_text(chat_id, "Kaunsi file? Chuno:", {"inline_keyboard": kb})
        elif action == "rename_to":
            clear_pending(user_id)
            do_rename(chat_id, extra, text, user_id)
    except InputError as exc:
        set_pending(user_id, action, extra)  # keep waiting for a corrected answer
        send_text(chat_id, f"❌ Nahi hua: {exc}\n\nSahi value dobara bhejo, ya /cancel.", retry)
    except Exception as exc:
        log_error(f"admin input {action}")
        send_text(chat_id, f"❌ Nahi hua: {str(exc)[:250]}", retry)


def handle_admin_callback(query: Dict[str, Any], data: str, chat_id: int, user_id: int) -> None:
    legacy = {"admin_add_required": "ad:addreq", "admin_required_list": "ad:reqlist",
              "admin_add_source": "ad:addsrc", "admin_report": "ad:report"}
    data = legacy.get(data, data)
    cmd = data[3:]
    # the settings most admin screens read, fetched in ONE round trip (was one DB call each)
    if cmd in ("smode", "usermenu", "backup", "log", "logtest", "ainame", "cmdq") or cmd.startswith("cmdq:"):
        prefetch_settings([f"admsearch:{user_id}", f"pending:{user_id}", "log_chat_id", "backup_chat_id", "ai_naming",
                           LOGCHAT_STATUS_KEY] + (["ainame_cursor", "ainame_queue"] if cmd == "ainame" else []))
    _conf = cmd.startswith("y:")
    _rc = cmd[2:] if _conf else cmd
    if _rc.startswith(("rt", "keyadd")) and handle_router_callback(query, _rc, _conf, chat_id, user_id):
        return                 # ⭐ Live / 🔁 Backup order / 🧠 Model / ➕ Key Jodo (provider first)
    if cmd.startswith("y:"):
        cmd = cmd[2:]          # ✅ Accept on a confirm prompt: run the remove now
    elif is_destructive(cmd):
        if cmd.startswith(("keyrm:", "rmadm:")) and not is_owner(user_id):
            send_text(chat_id, "Ye sirf owner hata sakta hai.", {"inline_keyboard": [BACK_ROW]})
            return
        show(chat_id, *confirm_view(cmd), query=query)
        return
    if cmd.startswith("ub") and handle_ub_callback(query, cmd, chat_id, user_id):
        return                 # 👤 Userbot screen / login link / check now / pause / logout (after Accept)
    if handle_admin_callback_v6(query, cmd, chat_id, user_id):
        return
    if cmd == "panel":
        show(chat_id, ADMIN_PANEL_TEXT, admin_menu(user_id), query, "HTML")
        if get_setting(f"pending:{user_id}"):      # after the screen is up
            clear_pending(user_id)
    elif cmd == "usermenu":
        show(chat_id, welcome_text(query.get("from"), admin=True), admin_start_menu(), query, "HTML")
    elif cmd in {"addreq", "addsrc", "rename"}:
        ask_input(chat_id, user_id, cmd)
    elif cmd == "reqlist":
        show(chat_id, *required_list_view(), query=query)
    elif cmd == "srclist":
        show(chat_id, *source_list_view(), query=query)
    elif cmd.startswith("rmreq:"):
        cid = cmd.split(":", 1)[1]
        supabase("required_channels", "DELETE", query=f"?chat_id=eq.{urllib.parse.quote(cid)}")
        text, kb = required_list_view()
        show(chat_id, "✅ Join channel hata diya.\n\n" + text, kb, query)
    elif cmd.startswith("rmsrc:"):
        cid = cmd.split(":", 1)[1]
        supabase("channels", "PATCH", {"active": False}, f"?chat_id=eq.{urllib.parse.quote(cid)}")
        text, kb = source_list_view()
        show(chat_id, "✅ Source channel hata diya (purani files search me rahengi).\n\n" + text, kb, query)
    elif cmd.startswith("mkreq:"):
        cid = int(cmd.split(":", 1)[1])
        try:
            row = save_required_channel(resolve_chat(None, str(cid)))
            send_text(chat_id, f"✅ Join channel bhi bana diya: {row['title']}", {"inline_keyboard": [BACK_ROW]})
        except InputError as exc:
            send_text(chat_id, f"❌ Nahi hua: {exc}", {"inline_keyboard": [BACK_ROW]})
    elif cmd == "backup":
        text, kb = setting_view("backup_chat_id", "💾 Backup Channel", "ad:setbackup", "ad:backupoff",
                                "Bot ki har file ki ek copy is private channel me (bina duplicate). Source se file na jaye to yahan se bheji jayegi.")
        if get_setting("backup_chat_id"):
            kb["inline_keyboard"].insert(1, [{"text": "💾 Backup: purani files bhi", "callback_data": "ad:bkold"}])
        show(chat_id, text, kb, query=query)
    elif cmd == "bkold":
        cb_answer(query, "💾 Backup chal raha hai...")
        show(chat_id, *backup_old_view(), query=query)
    elif cmd == "users":
        show(chat_id, users_view(), {"inline_keyboard": [[{"text": "🔄 Refresh", "callback_data": "ad:users"}], BACK_ROW]}, query)
    elif cmd == "log":
        show(chat_id, *log_setting_view(), query=query)
    elif cmd in {"setbackup", "setlog"}:
        ask_input(chat_id, user_id, cmd)
    elif cmd in {"backupoff", "logoff"}:
        key = "backup_chat_id" if cmd == "backupoff" else "log_chat_id"
        supabase("bot_settings", "DELETE", query=f"?key=eq.{key}")
        show(chat_id, ("💾 Backup" if key == "backup_chat_id" else "📝 Log chat") + " band kar diya ✅", {"inline_keyboard": [BACK_ROW]}, query)
    elif cmd == "admins":
        show(chat_id, *admins_view(user_id), query=query)
    elif cmd == "addadm":
        if is_owner(user_id):
            ask_input(chat_id, user_id, "addadmin")
        else:
            send_text(chat_id, "Admin sirf owner jod sakta hai.")
    elif cmd.startswith("rmadm:"):
        if not is_owner(user_id):
            send_text(chat_id, "Admin sirf owner hata sakta hai.")
            return
        target = cmd.split(":", 1)[1]
        supabase("admins", "DELETE", query=f"?user_id=eq.{urllib.parse.quote(target)}")
        _SUBADMIN_CACHE.pop(int(target), None)
        try:
            telegram("deleteMyCommands", {"scope": {"type": "chat", "chat_id": int(target)}})
        except Exception:
            log_error("remove admin menu")
        text, kb = admins_view(user_id)
        show(chat_id, f"✅ Sub-admin {target} hata diya.\n\n" + text, kb, query)
    elif cmd.startswith("rn:"):
        ask_input(chat_id, user_id, "rename_to", int(cmd.split(":", 1)[1]))
    elif cmd == "report":
        show(chat_id, report_text(), {"inline_keyboard": [BACK_ROW]}, query)
    elif cmd == "status":
        show(chat_id, status_text(user_id), status_markup(), query)
    elif cmd == "help":
        show(chat_id, ADMIN_HELP, {"inline_keyboard": [BACK_ROW]}, query)
    elif cmd == "restart":
        cb_answer(query, "🔄 Restart ho raha hai...")
        show(chat_id, restart_text(), status_markup(), query)
    elif cmd == "keys" or cmd.startswith("key"):
        if not is_owner(user_id):
            send_text(chat_id, "🔑 AI Keys sirf owner badal sakta hai.", {"inline_keyboard": [BACK_ROW]})
            return
        if cmd == "keyadd":
            ask_input(chat_id, user_id, "addkey")
        elif cmd == "keychk":
            cb_answer(query, "🔄 Keys check ho rahi hain...")
            for key in all_ai_keys():
                st = check_ai_key(key)
                if st == "ok":
                    uncool_key(key)
                elif st in {"invalid", "limit"}:
                    cool_key(key, 3600 if st == "invalid" else KEY_COOLDOWN, st)
            show(chat_id, *keys_view(), query=query)
        elif cmd.startswith("keyrm:"):
            kid = cmd.split(":", 1)[1]
            keys = db_ai_keys(fresh=True)
            gone = [x for x in keys if key_id(x["k"]) == kid]
            save_db_ai_keys([x for x in keys if key_id(x["k"]) != kid])
            text, kb = keys_view()
            show(chat_id, (f"✅ Key hata di ({mask_key(gone[0]['k'])})\n\n" if gone else "") + text, kb, query)
        else:
            show(chat_id, *keys_view(), query=query)
    else:
        show(chat_id, ADMIN_PANEL_TEXT, admin_menu(user_id), query, "HTML")


# ---- confirm before any remove (Aarav 5:08/5:11 PM: one confirm, ✅ Accept / ❌ Deny)

DESTRUCTIVE_PREFIXES = ("rmreq:", "rmsrc:", "rmadm:", "keyrm:", "fdel:")
DESTRUCTIVE_CMDS = {"backupoff", "logoff", "ublogout"}


def is_destructive(cmd: str) -> bool:
    return cmd in DESTRUCTIVE_CMDS or cmd.startswith(DESTRUCTIVE_PREFIXES)


def confirm_view(cmd: str) -> Any:
    """'⚠️ <item> hatana hai?' with ✅ Accept (runs ad:y:<cmd>) / ❌ Deny (back to the list)."""
    arg = cmd.split(":", 1)[1] if ":" in cmd else ""
    name, back = cmd, "ad:panel"
    try:
        if cmd.startswith("rmreq:"):
            rows = supabase("required_channels", "GET", query=f"?chat_id=eq.{urllib.parse.quote(arg)}&select=title&limit=1")
            name, back = f"Join channel \"{(rows[0].get('title') if rows else None) or arg}\"", "ad:reqlist"
        elif cmd.startswith("rmsrc:"):
            rows = supabase("channels", "GET", query=f"?chat_id=eq.{urllib.parse.quote(arg)}&select=title&limit=1")
            name, back = f"Source channel \"{(rows[0].get('title') if rows else None) or arg}\"", "ad:srclist"
        elif cmd.startswith("rmadm:"):
            name, back = f"Sub-admin {arg}", "ad:admins"
        elif cmd.startswith("keyrm:"):
            found = [x["k"] for x in db_ai_keys() if key_id(x["k"]) == arg]
            name, back = f"AI key {mask_key(found[0]) if found else ''}".strip(), "ad:keys"
        elif cmd.startswith("fdel:"):
            rows = supabase("books", "GET", query=f"?id=eq.{int(arg)}&select=title&limit=1")
            name, back = f"File \"{(rows[0].get('title') if rows else None) or arg}\"", "ad:files:1"
        elif cmd == "backupoff":
            name, back = "Backup channel", "ad:backup"
        elif cmd == "logoff":
            name, back = "Log chat", "ad:log"
        elif cmd == "ublogout":
            name, back = "Userbot login (aapke Telegram account ka session)", "ad:ub"
    except Exception:
        log_error("confirm_view")
    if cmd.startswith("fdel:"):
        name += " (search me nahi aayegi)"
    return (f"⚠️ {name} hatana hai?",
            {"inline_keyboard": [[{"text": "✅ Accept", "callback_data": f"ad:y:{cmd}"[:64]},
                                  {"text": "❌ Deny", "callback_data": back}]]})


# ---- 🔑 AI Keys (owner only): add/remove Groq / OpenAI keys without a Vercel redeploy

def all_ai_keys() -> List[str]:
    return list(dict.fromkeys(k for p in PROVIDER_ORDER for k in provider_keys(p)))


def key_status_word(key: str) -> str:
    st = key_state(key)
    return {"": "✅ ok", "limit": "⏳ limit", "invalid": "❌ invalid"}.get(st, "⏳ " + st)


def prefetch_ai() -> None:
    """Everything the 🔑 AI Keys screens read, in two DB round trips instead of one per key/setting."""
    try:
        prefetch_settings(["ai_keys", ROUTER_KEY, f"aicalls:{ist_day()}"] +
                          [f"aikey_cool:{key_id(k)}" for v in ENV_AI_KEYS.values() for k in v] +
                          [f"aikey_cool:{key_id(x['k'])}" for x in _DBKEYS["v"]])
        prefetch_settings([f"aikey_cool:{key_id(x['k'])}" for x in db_ai_keys(fresh=True)])
    except Exception:
        log_error("prefetch_ai")


def keys_view() -> Any:
    """🔑 AI Keys: ⭐ LIVE provider, BACKUP ORDER, each provider's keys (masked), status and today's calls."""
    prefetch_ai()
    cfg = router_cfg(fresh=True)
    db = db_ai_keys(fresh=True)
    env = env_key_rows()
    chain = [p for p in cfg["order"] if provider_keys(p)]
    today = calls_today()
    lines = ["🔑 AI Keys — ⭐ Live + 🔁 Backup", ""]

    def pline(p: str) -> str:
        n = len(provider_keys(p))
        if not n:
            return f"{pname(p)} — ➖ key nahi"
        st = STATE_WORD[provider_state(p)]
        ok, bad = (today.get(p) or [0, 0])[:2]
        model = cfg["models"].get(p) or "auto"
        return f"{pname(p)} — {st} · {n} key · model: {model} · aaj {ok} calls" + (f" ({bad} fail)" if bad else "")

    if chain:
        lines.append("⭐ LIVE: " + pline(chain[0]))
    else:
        lines.append("⭐ LIVE: koi nahi — AI OFF ❌ (➕ Key Jodo se key jodo)")
    backups = [p for p in cfg["order"] if p != (chain[0] if chain else "") and (p in MAIN_PROVIDERS or provider_keys(p))]
    lines.append("🔁 Backup order:")
    for i, p in enumerate(backups, 1):
        lines.append(f"  {i}. " + pline(p))
    lines.append("Live fail ho (limit / error / slow) → wahi sawaal turant agli wali pe ✅")
    lines.append("")
    lines.append("Vercel wali keys:" if env else "Vercel wali keys: koi nahi")
    for n, p, k in env:
        lines.append(f"• {n}: {mask_key(k)} — {key_status_word(k)}")
    lines.append("")
    lines.append("Bot me judi keys:" if db else "Bot me judi keys: abhi koi nahi")
    kb = []
    for i, x in enumerate(db, 1):
        k = x["k"]
        lines.append(f"{i}. {mask_key(k)} — {pname(key_prov(x))} — {key_status_word(k)}")
        kb.append([{"text": f"❌ Hatao {mask_key(k)}", "callback_data": f"ad:keyrm:{key_id(k)}"}])
    lines.append("")
    lines.append(f"AI: {'ON ✅ (' + pname(chain[0]) + ')' if chain else 'OFF ❌'}")
    lines.append("✅ ok = chal rahi · ⏳ limit = thodi der ruki · ❌ invalid = galat/band key")
    kb = [[{"text": "➕ Key Jodo", "callback_data": "ad:keyadd"}, {"text": "⭐ Live badlo", "callback_data": "ad:rtlive"}],
          [{"text": "🔁 Backup order", "callback_data": "ad:rtord"}, {"text": "🧠 Model chuno", "callback_data": "ad:rtmod"}]] + kb + \
         [[{"text": "🔄 Keys Check", "callback_data": "ad:keychk"}, {"text": "🤖 AI test", "callback_data": "ad:aitest"}], BACK_ROW]
    return "\n".join(lines), {"inline_keyboard": kb}


KEYS_BACK = [{"text": "⬅️ AI Keys", "callback_data": "ad:keys"}]


def key_provider_picker() -> Any:
    rows = [[{"text": f"{pname(p)} ({len(provider_keys(p))})", "callback_data": f"ad:keyadd:{p}"}] for p in MAIN_PROVIDERS]
    lines = ["➕ Kis company ki key jodni hai?", ""]
    for p in MAIN_PROVIDERS:
        lines.append(f"• {pname(p)}: {PROVIDERS[p]['get']}")
    lines.append("\nSab free keys hain. Key bana ke copy karo, button dabao, phir key paste karo.")
    return "\n".join(lines), {"inline_keyboard": rows + [KEYS_BACK]}


def ask_key(chat_id: int, user_id: int, p: str) -> None:
    set_pending(user_id, "addkey", p)
    send_text(chat_id, PROMPTS["addkey"][0] + f"\n\n🏷️ {pname(p)} key — yahan se banti hai: {PROVIDERS[p]['get']}",
              {"force_reply": True, "input_field_placeholder": PROMPTS["addkey"][1]})


def _order_code(order: List[str]) -> str:
    return "".join(PROVIDERS[p]["c"] for p in order if p in PROVIDERS)


def _order_from(code: str) -> List[str]:
    return [CODE_P[c] for c in dict.fromkeys(code) if c in CODE_P]


def rt_live_view() -> Any:
    prefetch_ai()
    cfg = router_cfg(fresh=True)
    live = live_provider()
    rows = []
    for p in cfg["order"]:
        if p not in MAIN_PROVIDERS and not provider_keys(p):
            continue
        if provider_keys(p):
            rows.append([{"text": ("⭐ " if p == live else "") + f"{pname(p)} {STATE_WORD[provider_state(p)]}"[:40],
                          "callback_data": f"ad:rtl:{PROVIDERS[p]['c']}"}])
    none = [pname(p) for p in MAIN_PROVIDERS if not provider_keys(p)]
    text = (f"⭐ Live AI chuno (abhi: {pname(live) if live else 'koi nahi'})\n\nSirf jinki key judi hai wahi dikh rahe hain."
            + (f"\nKey nahi: {', '.join(none)} → ➕ Key Jodo" if none else ""))
    return text, {"inline_keyboard": rows + [[{"text": "➕ Key Jodo", "callback_data": "ad:keyadd"}], KEYS_BACK]}


def rt_order_view(code: str = "") -> Any:
    """Backup order editor. ⬆️/⬇️ only change this screen; 💾 Save → one ✅ Accept / ❌ Deny."""
    cfg = router_cfg(fresh=True)
    live = cfg["order"][0]
    cur = [p for p in cfg["order"][1:] if p in MAIN_PROVIDERS or provider_keys(p)]
    draft = [p for p in _order_from(code) if p in cur] if code else list(cur)
    draft += [p for p in cur if p not in draft]
    lines = [f"🔁 Backup order (⭐ Live: {pname(live)})", "", "Live fail ho to isi line se agli AI chalegi:"]
    rows = []
    for i, p in enumerate(draft):
        lines.append(f"{i + 1}. {pname(p)} — {STATE_WORD[provider_state(p)]}")
        up = draft[:]
        dn = draft[:]
        if i > 0:
            up[i - 1], up[i] = up[i], up[i - 1]
        if i < len(draft) - 1:
            dn[i + 1], dn[i] = dn[i], dn[i + 1]
        rows.append([{"text": f"{i + 1}. {pname(p)}", "callback_data": f"ad:rtord:{_order_code(draft)}"},
                     {"text": "⬆️", "callback_data": f"ad:rtord:{_order_code(up)}"},
                     {"text": "⬇️", "callback_data": f"ad:rtord:{_order_code(dn)}"}])
    changed = draft != cur
    if changed:
        lines.append("\n✏️ Badla hua order — 💾 Save dabao (phir ✅ Accept)")
    rows.append([{"text": "💾 Save order", "callback_data": f"ad:rtos:{_order_code(draft)}"}] if changed else [])
    return "\n".join(lines), {"inline_keyboard": [r for r in rows if r] + [KEYS_BACK]}


def rt_model_providers() -> Any:
    rows = [[{"text": f"🧠 {pname(p)}: {router_cfg()['models'].get(p) or 'auto'}"[:60], "callback_data": f"ad:rtm:{PROVIDERS[p]['c']}"}]
            for p in router_cfg(fresh=True)["order"] if provider_keys(p)]
    if not rows:
        return "🧠 Pehle koi AI key jodo.", {"inline_keyboard": [[{"text": "➕ Key Jodo", "callback_data": "ad:keyadd"}], KEYS_BACK]}
    return "🧠 Kis AI ka model chunna hai? (auto = bot khud sabse sasta/tez chunta hai)", {"inline_keyboard": rows + [KEYS_BACK]}


def rt_model_view(p: str) -> Any:
    keys = provider_keys(p)
    if not keys:
        return f"{pname(p)} ki key nahi hai.", {"inline_keyboard": [KEYS_BACK]}
    pinned = router_cfg(fresh=True)["models"].get(p, "")
    ids = [m for m in provider_models(p, keys[0]) if m][:8]
    if pinned and pinned not in ids:
        ids = [pinned] + ids[:7]
    try:
        set_setting(f"aimodel_list:{p}", json.dumps(ids))
    except Exception:
        log_error("model list")
    c = PROVIDERS[p]["c"]
    rows = [[{"text": ("✅ " if (m == pinned) else "") + m[:48], "callback_data": f"ad:rtms:{c}:{i}"}] for i, m in enumerate(ids)]
    rows.append([{"text": ("✅ " if not pinned else "") + "🔄 Auto (default)", "callback_data": f"ad:rtms:{c}:a"}])
    return (f"🧠 {pname(p)} ka model chuno (abhi: {pinned or 'auto → ' + (ids[0] if ids else '?')})\n"
            "Ye list is key ke apne models se bani hai."), {"inline_keyboard": rows + [KEYS_BACK]}


def rt_confirm(chat_id: int, query: Dict[str, Any], text: str, cmd: str, back: str) -> None:
    show(chat_id, text, {"inline_keyboard": [[{"text": "✅ Accept", "callback_data": f"ad:y:{cmd}"[:64]},
                                             {"text": "❌ Deny", "callback_data": back}]]}, query)


def handle_router_callback(query: Dict[str, Any], cmd: str, confirmed: bool, chat_id: int, user_id: int) -> bool:
    """⭐ Live / 🔁 Backup order / 🧠 Model. Every change = one ✅ Accept / ❌ Deny (Aarav's settings rule)."""
    if not cmd.startswith(("rt", "keyadd:")) and cmd != "keyadd":
        return False
    if not is_owner(user_id):
        send_text(chat_id, "🔑 AI Keys sirf owner badal sakta hai.", {"inline_keyboard": [BACK_ROW]})
        return True
    cfg = router_cfg(fresh=True)
    order, models = list(cfg["order"]), dict(cfg["models"])
    if cmd == "keyadd":
        show(chat_id, *key_provider_picker(), query=query)
    elif cmd.startswith("keyadd:"):
        p = cmd.split(":", 1)[1]
        if p in PROVIDERS:
            ask_key(chat_id, user_id, p)
        else:
            show(chat_id, *key_provider_picker(), query=query)
    elif cmd == "rtlive":
        show(chat_id, *rt_live_view(), query=query)
    elif cmd.startswith("rtl:"):
        p = CODE_P.get(cmd[4:], "")
        old = live_provider()
        if not p or not provider_keys(p):
            show(chat_id, *rt_live_view(), query=query)
        elif not confirmed:
            if p == old:
                show(chat_id, f"⭐ {pname(p)} pehle se Live hai.\n\n" + rt_live_view()[0], rt_live_view()[1], query)
            else:
                rt_confirm(chat_id, query, f"⭐ Live AI badalna hai?\n\n{pname(old) or 'koi nahi'} → {pname(p)}\n"
                                           f"({pname(old) or '—'} backup me no. 1 pe chali jayegi)", cmd, "ad:rtlive")
        else:
            new = [p] + [x for x in order if x != p]
            if old and old != p:
                new = [p, old] + [x for x in order if x not in (p, old)]
            save_router_cfg(new, models)
            audit_log(f"⚙️ AI Live badla: {pname(old) or '—'} → {pname(p)}")
            text, kb = keys_view()
            show(chat_id, f"✅ Live AI ab {pname(p)} hai\n\n" + text, kb, query)
    elif cmd == "rtord" or cmd.startswith("rtord:"):
        show(chat_id, *rt_order_view(cmd[6:] if ":" in cmd else ""), query=query)
    elif cmd.startswith("rtos:"):
        live = order[0]
        draft = [x for x in _order_from(cmd[5:]) if x != live]
        new = [live] + draft + [x for x in order if x != live and x not in draft]
        cur_names = " → ".join(pname(x) for x in order[1:] if x in MAIN_PROVIDERS or provider_keys(x))
        new_names = " → ".join(pname(x) for x in new[1:] if x in MAIN_PROVIDERS or provider_keys(x))
        if not confirmed:
            rt_confirm(chat_id, query, f"🔁 Backup order badalna hai?\n\nPehle: {cur_names}\nAb: {new_names}", cmd, "ad:rtord")
        else:
            save_router_cfg(new, models)
            audit_log(f"⚙️ AI backup order: {new_names}")
            text, kb = keys_view()
            show(chat_id, "✅ Backup order save ho gaya\n\n" + text, kb, query)
    elif cmd == "rtmod":
        show(chat_id, *rt_model_providers(), query=query)
    elif cmd.startswith("rtm:"):
        p = CODE_P.get(cmd[4:], "")
        show(chat_id, *(rt_model_view(p) if p else rt_model_providers()), query=query)
    elif cmd.startswith("rtms:"):
        _, c, idx = (cmd.split(":") + ["", ""])[:3]
        p = CODE_P.get(c, "")
        if not p:
            show(chat_id, *rt_model_providers(), query=query)
            return True
        try:
            ids = json.loads(get_setting(f"aimodel_list:{p}") or "[]")
        except Exception:
            ids = []
        model = "" if idx == "a" else (ids[int(idx)] if idx.isdigit() and int(idx) < len(ids) else None)
        if model is None:
            show(chat_id, *rt_model_view(p), query=query)
            return True
        old = models.get(p) or "auto"
        if not confirmed:
            rt_confirm(chat_id, query, f"🧠 {pname(p)} ka model badalna hai?\n\n{old} → {model or 'auto'}", cmd, f"ad:rtm:{c}")
        else:
            if model:
                models[p] = model
            else:
                models.pop(p, None)
            save_router_cfg(order, models)
            audit_log(f"⚙️ AI model: {pname(p)} {old} → {model or 'auto'}")
            text, kb = keys_view()
            show(chat_id, f"✅ {pname(p)} model: {model or 'auto'}\n\n" + text, kb, query)
    else:
        return False
    return True


def add_ai_key(message: Dict[str, Any], chat_id: int, text: str, provider: Optional[str] = None) -> None:
    """Owner pasted a key: delete their message first (always), check it with one tiny call, save it masked."""
    try:
        telegram("deleteMessage", {"chat_id": chat_id, "message_id": message.get("message_id")})
        deleted = True
    except Exception:
        deleted = False
    provider = provider if provider in PROVIDERS else None
    found = KEY_RE.search(text or "")
    note = "" if deleted else "\n⚠️ Key wala message khud delete kar do."
    key = found.group(1) if found else ""
    if not key and not provider and MISTRAL_KEY_RE.match(text or ""):
        key, provider = MISTRAL_KEY_RE.match(text or "").group(1), "mistral"
    if not key and provider:
        m = ANY_KEY_RE.match(text or "")
        key = m.group(1) if m else ""
    if not key:
        raise InputError("Ye API key jaisi nahi lag rahi. Key poori copy karke bhejo (Groq \"gsk_\", Gemini \"AIza\", "
                         "OpenRouter \"sk-or-\", NVIDIA \"nvapi-\" se shuru hoti hai; Mistral ki key bina prefix)." + note)
    p = key_provider(key) or provider
    if not p:
        raise InputError("Ye kis company ki key hai? 🔑 AI Keys → ➕ Key Jodo → company chuno, phir key bhejo." + note)
    keys = db_ai_keys(fresh=True)
    if key in all_ai_keys() or key in GROQ_KEYS or key == OPENAI_API_KEY or any(x["k"] == key for x in keys):
        raise InputError(f"Ye key ({mask_key(key)}) pehle se judi hai." + note)
    chk = validate_key(p, key)
    st = chk["st"]
    if st == "invalid":
        raise InputError(f"Ye {pname(p)} key ({mask_key(key)}) kaam nahi kar rahi (invalid). Nayi key banao aur dobara bhejo." + note)
    if st == "error" and not chk.get("model"):
        raise InputError("Key check nahi ho payi (network). Thodi der baad dobara bhejo." + note)
    keys.append({"k": key, "p": p, "ts": int(time.time())})
    save_db_ai_keys(keys)
    if st == "limit":
        cool_key(key, KEY_COOLDOWN, "limit")
    else:
        uncool_key(key)
    if chk.get("ms") is not None:
        test = f"\n🤖 Test call ✅ {chk['ms']} ms ({chk.get('model')})"
    elif chk.get("err"):
        test = f"\n⚠️ Test call: {chk['err'][:120]}"
    else:
        test = ""
    switched = f"\n(Ye {pname(p)} ki key thi, {pname(p)} me jodi)" if provider and p != provider else ""
    chain = ai_chain()
    role = ("⭐ Live" if chain and chain[0] == p else f"🔁 backup no. {chain.index(p)}") if p in chain else ""
    send_text(chat_id, f"✅ Key jud gayi ({mask_key(key)}) — {pname(p)}" + (" — abhi limit pe hai, thodi der me chalu" if st == "limit" else "")
              + test + switched + f"\n{pname(p)} keys: {len(provider_keys(p))} · {role} · kul AI keys: {len(all_ai_keys())}" + note,
              {"inline_keyboard": [[{"text": "🔑 AI Keys", "callback_data": "ad:keys"}], BACK_ROW]})


# ---- 🔄 Bot Restart / webhook self-heal

def status_markup() -> Dict[str, Any]:
    return {"inline_keyboard": [
        [{"text": "🔄 Bot Restart", "callback_data": "ad:restart"}, {"text": "🤖 AI test", "callback_data": "ad:aitest"}],
        [{"text": "🌐 Web kholo (restart)", "url": PUBLIC_HOME + "?restart=1"}],
        BACK_ROW]}


def restart_text() -> str:
    try:
        ensure_webhook(force=True)
        info = telegram("getWebhookInfo", {}).get("result", {}) or {}
    except Exception as exc:
        return ("❌ Restart nahi hua: " + friendly_tg_error(exc)[:150] +
                "\n\nNiche 🌐 Web kholo dabao (site khulte hi bot dobara chalu hota hai).")
    ok_url = info.get("url") == webhook_url()
    err = info.get("last_error_message")
    lines = ["🔄 Bot Restart ho gaya ✅" if ok_url else "⚠️ Restart kiya, par webhook sahi nahi laga",
             "", f"Webhook: {'✅ sahi' if ok_url else '❌ galat'}",
             f"Pending updates: {info.get('pending_update_count', 0)}",
             "Commands menu: ✅ set"]
    if err:
        when = info.get("last_error_date")
        ago = f" ({max(0, int(time.time()) - int(when)) // 60} min pehle)" if when else ""
        lines.append(f"Last error{ago}: {str(err)[:120]}")
    else:
        lines.append("Last error: koi nahi ✅")
    lines.append("\nDeploy ke baad bot apne-aap chalu rehta hai; phir bhi ruke to yahi button ya 🌐 Web kholo.")
    return "\n".join(lines)


# ================================================================ smart chat (cheap first, AI second)
# Every private text goes through understand() first (free, local). Only when that cannot
# decide does the bot ask the AI — at most AI_TURNS_PER_CHAT times per conversation and
# AI_DAILY_PER_USER times a day per user. Chit-chat never reaches the AI.

IST_OFFSET = 19800
CTX_TTL = 1800
PAGE = 8

HINT_LINE = ("Book ke baare me batao: 📚 kaunsi book, 🎯 kis exam ke liye, 🌐 Hindi ya English?\n"
             "Jaise: <b>SSC GD GK Hindi</b>")
OFFTOPIC_MSG = "🙏 Main sirf books / PDF dhoondhne me madad karta hu.\n\n" + HINT_LINE
THANKS_MSG = "🙏 Khushi hui! Aur koi book chahiye to bas naam likh do 📚"
NO_CONTEXT_MORE_MSG = "🤔 Pehle batao kaunsi book chahiye.\n\n" + HINT_LINE
ALL_SHOWN_MSG = ("✅ <b>Is search ki saari files dikha di</b>\n\n"
                 "Kuch aur chahiye? Book ka naam / exam / subject likho 📚")
ASK_SUBJECT_MSG = ("📚 <b>Kis subject ki book chahiye?</b>\n"
                   "<i>Button dabao ya naam likho (jaise: Lucent GK, SSC GD Maths)</i>")


def greeting_msg(user: Optional[Dict[str, Any]]) -> str:
    return f"👋 <b>Namaste {first_name(user)}!</b>\n\n" + HINT_LINE


def ask_exam_msg(label: str) -> str:
    return (f"📚 <b>{esc(label.title())} ki books</b> — kis exam ke liye chahiye? 🎯\n"
            "<i>Button dabao ya exam ka naam likho</i>")


FILLER = set("""
mujhe muje mjhe mujhko mujko hume humein hamein hame mere mera meri mai main mein me ko ki ka ke kaa kii se par pe
chahiye chaiye chahie chahiya chaahiye chahye chiye cahiye chahta chahti chahte chahiyee
ek 1 h hai hain he hu hun hoon ho hoga hogi tha thi hy hn hi
do de dedo dijiye dijie dena dedena dedijiye diya dijiyega dedijie bhejo bhejdo bhejiye bhej send give
milegi milega mil milta milti sakti sakta sakte skti skta skte kar karo kare karna kardo krdo kro kr
kya kia kiya koi kuch kuchh kuj kuchch bhi
please plz pls plzz plss bhai bro sir ji yaar yar bhaiya bhaiyya dost madam mam
book books kitab kitaab kitabe kitaben pustak pdf pdfs file files link
wali wala vala vali wale vale waali waala
ye yeh is iss isko iski iska iske isme ise isse usse uski uska uske usme wo woh vo voh
the a an of for to and i my need want any some please
raha rahi rahe rha rhi rhe padhu padhna padhne padh liye lie batao bata btao bataiye btaiye
suggest best achi acchi achhi sabse konsi kaunsi kounsi konsa kaunsa kon kaun si sa se
which what taiyari tayari taiyaari preparation prepare exam exams paper ke liye
मुझे मुझको चाहिए चाहिये एक की का के को दो दे दीजिए है हैं हूँ कोई कुछ भी क्या बुक किताब पुस्तक पीडीएफ फाइल में से और या
""".split())
# Generic praise / filler (Aarav 6:40 PM: "Mujhe mere liye ek bdhiya si book do" searched "bdhiya")
GENERIC_WORDS = set("""
badhiya badhia bdhiya bdiya badiya badhiyaa bdhia acha accha achha acchha achhi acchi achi ache acche achhe
best top good nice mast zabardast jabardast sahi shandar important imp useful kaam kam aayi aaye wali wala wale
vali vala vale liye lie liya si sa se recommend suggest suggestion bata batao btao dikhao dikha koi kuch kuchh
book books pdf pdfs file files notes kitab kitaab kitabe kitaben padhne padhai study
""".split())
BOOK_ASK_WORDS = {"book", "books", "kitab", "kitaab", "kitabe", "kitaben", "pdf", "pdfs", "notes", "recommend", "suggest",
                  "suggestion", "badhiya", "badhia", "bdhiya", "bdiya", "badiya", "best", "top", "important", "imp", "useful"}
PRAISE_WORDS = {"acha", "accha", "achha", "acchha", "achhi", "acchi", "achi", "ache", "acche", "achhe", "good", "nice", "mast",
                "zabardast", "jabardast", "sahi", "shandar"}
GIVE_WORDS = {"do", "de", "dedo", "dena", "dijiye", "bhejo", "bhej", "bhejdo", "chahiye", "chaiye", "chahie", "send", "give", "batao", "bata"}
FILLER = FILLER | {w for w in GENERIC_WORDS if w not in {"notes", "book", "books", "pdf", "pdfs", "file", "files"}} | \
    {"liye", "lie", "si", "sa", "wali", "wala", "wale", "vali", "vala", "dedo", "de", "do", "bhejo", "ek", "mere", "mujhe", "please", "bhai", "sir"}


MORE_WORDS = {"aur", "or", "hor", "dusri", "dusra", "dusre", "doosri", "doosra", "doosre", "alag", "alg", "anya",
              "more", "next", "another", "other", "others", "baki", "baaki", "bachi", "aage", "agla", "agli",
              "agle", "aur bhi", "dikhao", "dikhaiye", "dikha", "dusari", "दूसरी", "दूसरा", "अलग", "और"}
REF_WORDS = {"isse", "iski", "iska", "iske", "isme", "ise", "ye", "yeh", "wahi", "usi", "same", "wali", "wala",
             "vala", "vali", "wale", "vale"}
STRONG_GREET = ["hi", "hii", "hiii", "hello", "helo", "hlo", "hey", "heyy", "namaste", "namaskar", "pranam",
                "salam", "gm", "hlw", "hellow", "namastey"]
GREET_WORDS = set(STRONG_GREET) | {"ram", "radhe", "jai", "hind", "good", "morning", "evening", "night", "afternoon",
                                   "gn", "sup", "yo", "assalam", "alaikum", "walekum", "kaise", "kese", "kaisa",
                                   "haal", "chaal", "sab", "badhiya", "theek", "start", "there", "bot", "jii"}
THANK_WORDS = {"thanks", "thank", "thanku", "thankyou", "thnx", "thx", "ty", "shukriya", "dhanyawad", "dhanyavad",
               "ok", "okay", "okk", "okey", "thik", "theek", "accha", "acha", "achha", "nice", "great", "done", "hmm",
               "hm", "k", "mast", "badhiya", "super", "awesome", "you", "u", "so", "much", "very", "bahut", "dil",
               "se", "👍", "🙏", "❤️", "got", "it", "mil", "gaya", "gayi"}
OFF_WORDS = {"joke", "jokes", "chutkula", "chutkule", "shayari", "shayri", "gana", "gaana", "song", "songs", "movie",
             "movies", "film", "picture", "game", "games", "love", "pyar", "pyaar", "gf", "bf", "girlfriend",
             "boyfriend", "shadi", "shaadi", "timepass", "bore", "boring", "mazak", "mazaak", "weather", "mausam",
             "cricket", "ipl", "recharge", "paisa", "paise", "money", "earn", "kamai", "hack", "khana", "khaya",
             "neend", "instagram", "insta", "facebook", "dating", "flirt", "age", "umar", "single", "crush", "sexy"}
OFF_PHRASES = ("kaun ho", "kon ho", "tum kaun", "tum kon", "aap kaun", "aap kon", "kya kar rahe", "kya kr rhe",
               "kya kar rhe", "kya haal", "kaisa hai", "naam kya", "tumhara naam", "aapka naam", "who are you",
               "what is your name", "kha se ho", "kahan se ho", "kaha se ho")

EXAM_WORDS = {"ssc", "gd", "cgl", "chsl", "mts", "cpo", "steno", "je", "railway", "rrb", "ntpc", "alp", "rpf",
              "group", "police", "constable", "si", "daroga", "army", "agniveer", "airforce", "navy", "upsc", "ias",
              "ips", "pcs", "uppsc", "bpsc", "rpsc", "mppsc", "bank", "banking", "ibps", "sbi", "po", "clerk",
              "rbi", "ctet", "tet", "reet", "uptet", "teacher", "teaching", "nda", "cds", "afcat", "crpf", "bsf",
              "cisf", "itbp", "ssb", "patwari", "lekhpal", "vdo", "neet", "jee", "board", "class", "rajasthan",
              "bihar", "up", "mp", "haryana", "delhi", "punjab", "gujarat", "jharkhand", "uttarakhand"}
SUBJECT_WORDS = {"gk", "gs", "history", "itihas", "polity", "rajniti", "geography", "bhugol", "economy", "economics",
                 "eco", "arthshastra", "science", "vigyan", "physics", "chemistry", "biology", "maths", "math",
                 "ganit", "reasoning", "tark", "english", "hindi", "computer", "current", "affairs", "static",
                 "lucent", "ncert", "arihant", "samanya", "gyan", "grammar", "vocab", "constitution"}
TYPE_WORDS = {"notes", "note", "mock", "test", "pyq", "question", "questions", "solved", "previous", "year", "syllabus",
              "practice", "set", "oneliner", "one", "liner", "handwritten", "magazine"}
LANG_WORDS = {"hindi": "hi", "हिंदी": "hi", "हिन्दी": "hi", "english": "en", "angreji": "en", "angrezi": "en",
              "अंग्रेजी": "en", "eng": "en"}
SPELL = {"alg": "alag", "hor": "aur", "skti": "sakti", "fil": "file", "buk": "book", "bok": "book", "englis": "english",
         "inglish": "english", "hindii": "hindi", "maths": "maths", "mathematics": "maths", "resoning": "reasoning",
         "reasning": "reasoning", "geograpy": "geography", "histroy": "history", "railways": "railway",
         "polic": "police", "आर्मी": "army", "पुलिस": "police", "रेलवे": "railway"}
ALIAS_GROUPS = [
    {"eco", "economy", "economic", "economics", "arthshastra", "arthvyavastha", "अर्थशास्त्र"},
    {"history", "itihas", "इतिहास"},
    {"polity", "rajniti", "राजनीति", "samvidhan", "constitution", "संविधान"},
    {"geography", "bhugol", "भूगोल"},
    {"science", "vigyan", "विज्ञान"},
    {"maths", "math", "ganit", "गणित"},
    {"reasoning", "tark", "तर्कशक्ति", "रीजनिंग"},
    {"english", "angreji", "angrezi", "अंग्रेजी"},
    {"hindi", "हिंदी", "हिन्दी"},
    {"gk", "gs", "सामान्य ज्ञान", "samanya gyan", "general knowledge", "general studies", "static gk", "सामान्य अध्ययन"},
    {"gd", "ssc gd", "जीडी", "constable gd"},
    {"ntpc", "rrb ntpc", "एनटीपीसी"},
    {"cgl", "ssc cgl"},
    {"chsl", "ssc chsl"},
    {"computer", "कंप्यूटर", "कम्प्यूटर"},
    {"railway", "rrb", "रेलवे"},
    {"police", "पुलिस", "constable"},
    {"army", "agniveer", "agnipath", "अग्निवीर", "अग्निपथ", "सेना"},
    {"current", "करंट"},
]
ALIASES: Dict[str, set] = {}
for _g in ALIAS_GROUPS:
    for _w in _g:
        ALIASES[_w] = _g

EXAM_BUTTONS = [("SSC", "ssc"), ("Railway", "railway"), ("Police", "police"), ("Army / Agniveer", "army"),
                ("Teaching", "teacher"), ("📚 Sab dikhao", "all")]
SUBJECT_BUTTONS = [("GK / GS", "gk"), ("Maths", "maths"), ("Reasoning", "reasoning"), ("Hindi", "hindi"),
                   ("English", "english"), ("Science", "science"), ("History", "history"), ("Polity", "polity")]


def ist_day(ts: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime((ts if ts is not None else time.time()) + IST_OFFSET))


def ist_midnight_utc_iso() -> str:
    now = time.time()
    midnight = now - ((now + IST_OFFSET) % 86400)
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(midnight))


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def ist_time(iso: Any) -> str:
    try:
        s = str(iso)[:19].replace("T", " ")
        ts = time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S")) - time.timezone
        return time.strftime("%d %b %I:%M %p", time.gmtime(ts + IST_OFFSET))
    except Exception:
        return str(iso or "")[:16]


def tokenize(text: str) -> List[str]:
    t = (text or "").lower().replace("’", "'")
    words = re.findall(r"[a-z0-9]+|[\u0900-\u097F]+", t)
    out = []
    for w in words:
        w = SPELL.get(w, w)
        # "hiii", "hellooo", "plzzz": squeeze 3+ repeated letters
        w = re.sub(r"([a-z])\1{2,}", r"\1\1", w)
        out.append(w)
    return out


def _close_greet(word: str) -> bool:
    import difflib
    if len(word) < 2 or word in EXAM_WORDS or word in SUBJECT_WORDS:
        return False
    return bool(difflib.get_close_matches(word, STRONG_GREET, n=1, cutoff=0.78))


def understand(text: str) -> Dict[str, Any]:
    """Free, local reading of one message: intent + search keywords + filters."""
    tokens = tokenize(text)
    low = " ".join(tokens)
    result: Dict[str, Any] = {"tokens": tokens, "keywords": [], "lang": None, "types": [], "exam": [],
                              "subject": [], "ref": any(t in REF_WORDS for t in tokens), "intent": "book"}
    if not tokens:
        result["intent"] = "empty"
        return result
    vocab_hit = any(t in EXAM_WORDS or t in SUBJECT_WORDS for t in tokens)
    # "kuch accha sa do", "recommend karo koi badhiya kitab": a request for a book, not a thanks / greeting
    book_ask = any(t in BOOK_ASK_WORDS for t in tokens) or \
        (any(t in PRAISE_WORDS for t in tokens) and any(t in GIVE_WORDS for t in tokens))
    if not vocab_hit and (any(p in low for p in OFF_PHRASES) or any(t in OFF_WORDS for t in tokens)):
        result["intent"] = "off_topic"
        return result
    if not book_ask and all(t in GREET_WORDS or t in FILLER for t in tokens) and any(t in GREET_WORDS for t in tokens):
        result["intent"] = "greeting"
        return result
    if len(tokens) <= 2 and not vocab_hit and any(_close_greet(t) for t in tokens) and \
            all(_close_greet(t) or t in GREET_WORDS or t in FILLER for t in tokens):
        result["intent"] = "greeting"     # "Jello", "helo bhai", "hlo"
        return result
    if not book_ask and all(t in THANK_WORDS or t in FILLER for t in tokens) and any(t in THANK_WORDS for t in tokens):
        result["intent"] = "thanks"
        return result
    more = any(t in MORE_WORDS for t in tokens)
    keywords = []
    for t in tokens:
        if t in FILLER or t in MORE_WORDS or t in REF_WORDS or t in GREET_WORDS and t not in EXAM_WORDS:
            continue
        if t in GENERIC_WORDS and t not in TYPE_WORDS and t not in EXAM_WORDS and t not in SUBJECT_WORDS:
            continue
        if t in LANG_WORDS:
            result["lang"] = LANG_WORDS[t]
            continue
        if t in TYPE_WORDS:
            result["types"].append(t)
            continue
        if len(t) < 2 and not t.isdigit():
            continue
        keywords.append(t)
    result["exam"] = [k for k in keywords if k in EXAM_WORDS]
    result["subject"] = [k for k in keywords if k in SUBJECT_WORDS]
    result["keywords"] = keywords
    if more and not keywords:
        result["intent"] = "more"
    elif not keywords:
        result["intent"] = "vague"          # only filler / language / type words
    return result


# ---- short per-user conversation context (bot_settings "ctx:<user_id>", 30 min)

def load_ctx(user_id: Optional[int]) -> Dict[str, Any]:
    if not user_id:
        return {}
    raw = get_setting(f"ctx:{user_id}")
    try:
        data = json.loads(raw) if raw else {}
    except Exception:
        data = {}
    if not data or time.time() - float(data.get("ts", 0)) > CTX_TTL:
        return {}
    return data


def save_ctx(user_id: Optional[int], ctx: Dict[str, Any]) -> None:
    if not user_id:
        return
    ctx["ts"] = int(time.time())
    ctx["shown"] = [int(x) for x in (ctx.get("shown") or [])][-60:]
    try:
        set_setting(f"ctx:{user_id}", json.dumps(ctx, ensure_ascii=False)[:3000])
    except Exception:
        log_error("save_ctx")


# ---- AI (only when the local step cannot decide)

KEY_RE = re.compile(r"\b(gsk_[A-Za-z0-9]{20,}|sk-or-v1-[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_\-]{20,}|AIza[0-9A-Za-z_\-]{30,}|nvapi-[A-Za-z0-9_\-]{20,})")
MISTRAL_KEY_RE = re.compile(r"^\s*([A-Za-z0-9]{32})\s*$")   # Mistral keys: 32 letters/digits, no prefix
ANY_KEY_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9_\-\.]{19,199})\s*$")   # prefix-less keys (Mistral) after a provider pick


def key_provider(key: str) -> str:
    """Provider from the key's own prefix ("" = no known prefix, e.g. Mistral keys)."""
    if key.startswith("gsk_"):
        return "groq"
    if key.startswith("AIza"):
        return "gemini"
    if key.startswith("sk-or-"):
        return "openrouter"
    if key.startswith("nvapi-"):
        return "nvidia"
    if key.startswith("sk-"):
        return "openai"
    return ""


def key_id(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def mask_key(key: str) -> str:
    for pre in ("gsk_", "sk-or-v1-", "sk-or-", "nvapi-", "AIza", "sk-"):
        if key.startswith(pre):
            return pre + "…" + key[-4:]
    return "…" + key[-4:]


def db_ai_keys(fresh: bool = False) -> List[Dict[str, Any]]:
    """Keys the owner pasted in the bot: [{"k", "p": groq|openai, "ts"}]. Cached 60 s per instance."""
    if not fresh and time.time() - _DBKEYS["t"] < 60:
        return _DBKEYS["v"]
    keys = _DBKEYS["v"]
    try:
        raw = get_setting("ai_keys") if (SUPABASE_URL and SUPABASE_KEY) else None
        data = json.loads(raw) if raw else []
        keys = [x for x in data if isinstance(x, dict) and isinstance(x.get("k"), str) and x["k"]]
    except Exception:
        log_error("ai_keys")
    _DBKEYS.update(t=time.time(), v=keys)
    return keys


def save_db_ai_keys(keys: List[Dict[str, Any]]) -> None:
    set_setting("ai_keys", json.dumps(keys))
    _DBKEYS.update(t=time.time(), v=keys)


def groq_pool() -> List[str]:
    """Env keys first (GROQ_API_KEY, _2, _3), then keys added in the bot. Same failover for all."""
    return [k for k in dict.fromkeys(GROQ_KEYS + [x["k"] for x in db_ai_keys() if key_prov(x) == "groq"]) if k]


def openai_pool() -> List[str]:
    return [k for k in dict.fromkeys([OPENAI_API_KEY] + [x["k"] for x in db_ai_keys() if key_prov(x) == "openai"]) if k]


def ai_provider() -> str:
    """The ⭐ LIVE provider that has a key ("" = AI off)."""
    return live_provider()


_KEY_COOL: Dict[str, float] = {}
KEY_COOLDOWN = 300   # seconds a rate-limited / failing key is skipped


def key_state(key: str) -> str:
    """"" (usable) | "limit" | "invalid" — from the cooling record in bot_settings."""
    kid = key_id(key)
    raw = get_setting(f"aikey_cool:{kid}") or ""
    until, _, reason = raw.partition("|")
    try:
        cooling = float(until) > time.time()
    except ValueError:
        cooling = False
    return (reason or "limit") if cooling else ""


def key_cooling(key: str) -> bool:
    kid = key_id(key)
    until = _KEY_COOL.get(kid)
    if until is None:
        raw = (get_setting(f"aikey_cool:{kid}") or "").partition("|")[0]
        until = float(raw) if raw and raw.replace(".", "", 1).isdigit() else 0.0
        _KEY_COOL[kid] = until
    return until > time.time()


def cool_key(key: str, seconds: int = KEY_COOLDOWN, reason: str = "limit") -> None:
    until = time.time() + seconds
    _KEY_COOL[key_id(key)] = until
    try:
        set_setting(f"aikey_cool:{key_id(key)}", f"{int(until)}|{reason}")
    except Exception:
        log_error("cool_key")


def uncool_key(key: str) -> None:
    _KEY_COOL[key_id(key)] = 0.0
    try:
        supabase("bot_settings", "DELETE", query=f"?key=eq.{urllib.parse.quote('aikey_cool:' + key_id(key))}")
    except Exception:
        log_error("uncool_key")


def check_ai_key(key: str) -> str:
    """Keys Check (no tokens where the provider has a free auth check): "ok" | "invalid" | "limit" | "error"."""
    p = next((x for x in PROVIDER_ORDER if key in provider_keys(x)), "") or key_provider(key) or "openai"
    if p == "nvidia":
        # NVIDIA has no free auth check (its model list is public): a 1-token call; a model that answers 404
        # "Function … Not found for account" is skipped (key stays fine), the next model is tried
        last = "error"
        for model in (provider_models(p, key) or PROVIDERS[p]["prefer"])[:4]:
            try:
                http_json(PROVIDERS[p]["base"] + "/chat/completions", "POST",
                          {"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1},
                          _auth(p, key), timeout=8)
                promote_model(p, key, model)
                return "ok"
            except Exception as exc:
                cls = ai_err_class(exc)
                if cls == "model":
                    drop_model(p, key, model)
                    continue
                return cls if cls in ("invalid", "limit") else "error"
        return last
    url = PROVIDERS[p]["base"] + ("/key" if p == "openrouter" else "/models")
    try:
        http_json(url, "GET", None, _auth(p, key), timeout=6)
        return "ok"
    except Exception as exc:
        low = str(exc).lower()
        if "http 401" in low or "http 403" in low or "invalid" in low or ai_err_class(exc) == "invalid" or (p == "gemini" and "http 400" in low):
            return "invalid"
        if "http 429" in low:
            return "limit"
        return "error"


def fail_reason(exc: Exception) -> str:
    low = str(exc).lower()
    return "invalid" if ("http 401" in low or "http 403" in low or "invalid api key" in low) else "limit"


# Which chat models a Groq key may use. Groq retires / gates models over time (Oct 26 2026 live: the
# llama-3.1-8b-instant and llama-3.3-70b-versatile ids answered 404 model_not_found for Aarav's key
# while /models said the key is fine), so the list comes from the key's own free /models answer.
GROQ_PREFER = ["llama-3.1-8b-instant", "openai/gpt-oss-20b", "qwen/qwen3.8-27b", "qwen/qwen3-32b",
               "meta-llama/llama-4-scout-17b-16e-instruct", "llama-3.3-70b-versatile", "openai/gpt-oss-120b",
               "meta-llama/llama-4-maverick-17b-128e-instruct", "moonshotai/kimi-k2-instruct", "gemma2-9b-it"]
_GMODELS: Dict[str, Any] = {}
NON_CHAT = ("whisper", "tts", "guard", "embed", "playai", "orpheus", "distil", "compound", "allam", "prompt", "safeguard")


def _model_rank(mid: str) -> tuple:
    low = mid.lower()
    if mid in GROQ_PREFER:
        return (0, GROQ_PREFER.index(mid))
    size = re.search(r"(\d+)b", low)
    return (1, int(size.group(1)) if size else 999, low)


def groq_models(key: str) -> List[str]:
    """Usable chat model ids for this key, best first (GROQ_MODEL env first). Cached 6 h in memory + bot_settings."""
    kid = key_id(key)
    hit = _GMODELS.get(kid)
    if hit and time.time() - hit[0] < 6 * 3600:
        ids = hit[1]
    else:
        ids = []
        raw = get_setting(f"aimodels2:{kid}") or ""
        ts, _, lst = raw.partition("|")
        if lst and ts.isdigit() and time.time() - int(ts) < 6 * 3600:
            ids = [x for x in lst.split(",") if x]
        else:
            try:
                res = http_json("https://api.groq.com/openai/v1/models", "GET", None,
                                {"Authorization": f"Bearer {key}"}, timeout=4)
                ids = [str(m.get("id")) for m in (res.get("data") or [])
                       if m.get("id") and m.get("active", True) is not False
                       and not any(x in str(m.get("id")).lower() for x in NON_CHAT)]
                ids = sorted(dict.fromkeys(ids), key=_model_rank)[:6]
            except Exception:
                ids = []
            if ids:
                try:
                    set_setting(f"aimodels2:{kid}", f"{int(time.time())}|{','.join(ids)}")
                except Exception:
                    log_error("aimodels cache")
        if not ids:
            ids = ["openai/gpt-oss-20b", "qwen/qwen3.8-27b", "llama-3.1-8b-instant"]
        _GMODELS[kid] = (time.time(), ids)
    return [m for m in dict.fromkeys([GROQ_MODEL] + ids) if m][:4]


def drop_groq_model(key: str, model: str) -> None:
    """A model id this key cannot use: forget it so the next call goes straight to a working one."""
    kid = key_id(key)
    ids = [m for m in groq_models(key) if m != model and m != GROQ_MODEL]
    _GMODELS[kid] = (time.time(), ids or [m for m in GROQ_PREFER if m != model][:3])
    try:
        set_setting(f"aimodels2:{kid}", f"{int(time.time())}|{','.join(_GMODELS[kid][1])}")
    except Exception:
        log_error("drop_groq_model")


def groq_body(model: str, messages: List[Dict[str, Any]], max_tokens: int, fast: bool = False) -> Dict[str, Any]:
    """Request body per model family. gpt-oss models think before answering: with a small max_tokens the
    thinking eats the budget, the JSON comes out empty and Groq answers 400 json_validate_failed (seen live).
    fast=True (the per-message understanding call): the shortest think the model allows, a small extra budget."""
    body: Dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.1,
                            "response_format": {"type": "json_object"}}
    if "gpt-oss" in model:
        body["reasoning_effort"] = "low"
        body["max_tokens"] = max_tokens + (250 if fast else 600)
    elif fast and "qwen3" in model:
        body["reasoning_effort"] = "none"
    return body


def strip_think(text: Any) -> str:
    t = re.sub(r"<think>.*?</think>", "", str(text or ""), flags=re.S).strip()
    return t


_AI_LAST: Dict[str, Any] = {}   # model / ms / usage of the last successful call (diag only, never keys)


# ================================================================ multi-provider AI router (Aarav 7:03 / 7:04 PM Oct 26 2026)
# Groq, Gemini, Mistral, OpenRouter, NVIDIA (+ OpenAI if a key exists). The owner picks the ⭐ LIVE provider and the
# BACKUP ORDER in 🔑 AI Keys; every AI call goes LIVE first and, on a limit / bad key / server error / timeout /
# empty or broken JSON, the SAME request goes to the next backup at once. Keys are never shown, logged or sent.
PROVIDERS: Dict[str, Dict[str, Any]] = {
    "groq": {"name": "Groq", "c": "g", "base": "https://api.groq.com/openai/v1", "json": True,
             "env": ["GROQ_API_KEY", "GROQ_API_KEY_2", "GROQ_API_KEY_3"], "get": "console.groq.com/keys", "prefer": []},
    "gemini": {"name": "Gemini", "c": "e", "base": "https://generativelanguage.googleapis.com/v1beta/openai", "json": True,
               "env": ["GEMINI_API_KEY", "GEMINI_API_KEY_2", "GOOGLE_API_KEY"], "get": "aistudio.google.com/apikey",
               "prefer": ["gemini-2.5-flash-lite", "gemini-flash-lite-latest", "gemini-2.0-flash-lite", "gemini-2.5-flash",
                          "gemini-flash-latest", "gemini-2.0-flash"]},
    "mistral": {"name": "Mistral", "c": "m", "base": "https://api.mistral.ai/v1", "json": True,
                "env": ["MISTRAL_API_KEY", "MISTRAL_API_KEY_2"], "get": "console.mistral.ai → API Keys",
                "prefer": ["mistral-small-latest", "open-mistral-nemo", "ministral-8b-latest", "ministral-3b-latest",
                           "mistral-medium-latest"]},
    "openrouter": {"name": "OpenRouter", "c": "o", "base": "https://openrouter.ai/api/v1", "json": False,
                   "env": ["OPENROUTER_API_KEY", "OPENROUTER_API_KEY_2"], "get": "openrouter.ai/keys",
                   "prefer": ["meta-llama/llama-3.3-8b-instruct:free", "mistralai/mistral-small-3.2-24b-instruct:free",
                              "google/gemma-3-12b-it:free", "qwen/qwen3-8b:free", "meta-llama/llama-3.2-3b-instruct:free",
                              "mistralai/mistral-7b-instruct:free", "google/gemma-3-27b-it:free",
                              "meta-llama/llama-3.3-70b-instruct:free", "openai/gpt-oss-20b:free"]},
    "nvidia": {"name": "NVIDIA", "c": "n", "base": "https://integrate.api.nvidia.com/v1", "json": False,
               "env": ["NVIDIA_API_KEY", "NVIDIA_API_KEY_2"], "get": "build.nvidia.com → Get API Key",
               # Oct 26 2026: meta/llama-3.1-8b-instruct etc. answer 404 "Function …: Not found for account" (retired /
               # not on the key's account) — fast models first, every 404 model is skipped and remembered per key
               "prefer": ["openai/gpt-oss-20b", "google/gemma-3-12b-it", "nv-mistralai/mistral-nemo-12b-instruct",
                          "mistralai/mistral-7b-instruct-v0.3", "z-ai/glm-5.3-flash", "deepseek-ai/deepseek-v4.1-flash",
                          "meta/llama-3.3-70b-instruct", "meta/llama-3.1-8b-instruct", "meta/llama-3.2-3b-instruct",
                          "google/gemma-3-4b-it", "ibm/granite-3.0-8b-instruct", "minimaxai/minimax-m3.1-flash"]},
    "openai": {"name": "OpenAI", "c": "a", "base": "https://api.openai.com/v1", "json": True,
               "env": ["OPENAI_API_KEY"], "get": "platform.openai.com/api-keys", "prefer": [], "hidden": True},
}
PROVIDER_ORDER = ["groq", "gemini", "mistral", "openrouter", "nvidia", "openai"]
MAIN_PROVIDERS = PROVIDER_ORDER[:5]
CODE_P = {v["c"]: k for k, v in PROVIDERS.items()}
ENV_AI_KEYS: Dict[str, List[str]] = {p: [k for k in dict.fromkeys(os.environ.get(n, "").strip() for n in PROVIDERS[p]["env"]) if k]
                                     for p in ("gemini", "mistral", "openrouter", "nvidia")}
ROUTER_KEY = "ai_router"
_RCFG: Dict[str, Any] = {"t": 0.0, "v": None}
_PMODELS: Dict[str, Any] = {}
_NOJSON: Dict[str, bool] = {}
_AI_STATS: Dict[str, Any] = {}
_SWITCH_LOGGED: Dict[str, float] = {}
SWITCH_LOG_EVERY = 600


def pname(p: str) -> str:
    return PROVIDERS.get(p, {}).get("name", p)


def key_prov(x: Dict[str, Any]) -> str:
    """Provider of a key saved in the bot (stored "p"; old rows: from the key's prefix)."""
    p = str(x.get("p") or "")
    return p if p in PROVIDERS else (key_provider(str(x.get("k") or "")) or "openai")


def provider_keys(p: str) -> List[str]:
    if p == "groq":
        return groq_pool()
    if p == "openai":
        return openai_pool()
    return [k for k in dict.fromkeys(ENV_AI_KEYS.get(p, []) + [x["k"] for x in db_ai_keys() if key_prov(x) == p]) if k]


def env_key_rows() -> List[tuple]:
    """(env name, provider, key) for keys set in Vercel."""
    out = [(n, "groq", k) for n, k in (("GROQ_API_KEY", GROQ_API_KEY), ("GROQ_API_KEY_2", GROQ_API_KEY_2),
                                         ("GROQ_API_KEY_3", GROQ_API_KEY_3)) if k]
    for p in ("gemini", "mistral", "openrouter", "nvidia"):
        for n in PROVIDERS[p]["env"]:
            v = os.environ.get(n, "").strip()
            if v and v in ENV_AI_KEYS.get(p, []):
                out.append((n, p, v))
    if OPENAI_API_KEY:
        out.append(("OPENAI_API_KEY", "openai", OPENAI_API_KEY))
    return out


def router_cfg(fresh: bool = False, raw: Optional[str] = None) -> Dict[str, Any]:
    """{"order": [providers, LIVE first], "models": {provider: pinned model}} from bot_settings (cached 60 s)."""
    if raw is None and not fresh and _RCFG["v"] is not None and time.time() - _RCFG["t"] < 60:
        return _RCFG["v"]
    cfg: Dict[str, Any] = {}
    try:
        if raw is None:
            raw = get_setting(ROUTER_KEY) if (SUPABASE_URL and SUPABASE_KEY) else None
        cfg = json.loads(raw) if raw else {}
        if not isinstance(cfg, dict):
            cfg = {}
    except Exception:
        log_error("router_cfg")
        cfg = dict(_RCFG["v"] or {})
    order = [p for p in dict.fromkeys(cfg.get("order") or []) if p in PROVIDERS]
    order += [p for p in PROVIDER_ORDER if p not in order]
    models = {p: str(m) for p, m in (cfg.get("models") or {}).items() if p in PROVIDERS and m}
    v = {"order": order, "models": models}
    _RCFG.update(t=time.time(), v=v)
    return v


def save_router_cfg(order: List[str], models: Dict[str, str]) -> None:
    order = [p for p in dict.fromkeys(order) if p in PROVIDERS]
    order += [p for p in PROVIDER_ORDER if p not in order]
    set_setting(ROUTER_KEY, json.dumps({"order": order, "models": models}))
    _RCFG.update(t=time.time(), v={"order": order, "models": dict(models)})


def ai_chain() -> List[str]:
    """Providers that have at least one key, in the owner's order (LIVE first)."""
    return [p for p in router_cfg()["order"] if provider_keys(p)]


def live_provider() -> str:
    ch = ai_chain()
    return ch[0] if ch else ""


PROBE_NO_SLEEP = [False]
_KEY_OK: Dict[str, float] = {}


def mark_key_ok(key: str, now: bool = False) -> None:
    """Remember that this key has had at least one successful call (health shows 'untested' until then)."""
    kid = key_id(key)
    if _KEY_OK.get(kid):
        return
    _KEY_OK[kid] = time.time()

    def write() -> None:
        try:
            if not get_setting(f"aikey_ok:{kid}"):
                set_setting(f"aikey_ok:{kid}", str(int(time.time())))
        except Exception:
            log_error("mark_key_ok")
    write() if now else defer(write)


def key_tested(key: str) -> bool:
    kid = key_id(key)
    return bool(_KEY_OK.get(kid) or get_setting(f"aikey_ok:{kid}"))


def provider_state(p: str) -> str:
    """"none" (no key) | "ok" | "limit" | "invalid" — the best of its keys."""
    keys = provider_keys(p)
    if not keys:
        return "none"
    sts = [key_state(k) for k in keys]
    if "" in sts:
        return "ok"
    return "limit" if any(s != "invalid" for s in sts) else "invalid"


STATE_WORD = {"none": "➖ key nahi", "ok": "✅", "limit": "⏳ limit", "invalid": "❌ key kharab"}


def _is_reasoning(model: str) -> bool:
    low = model.lower()
    return any(x in low for x in ("gpt-oss", "deepseek-r1", "-r1", "qwen3", "qwq", "thinking", "magistral", "nemotron", "reason"))


def _model_ok(p: str, mid: str) -> bool:
    low = mid.lower()
    bad = ("embed", "whisper", "tts", "guard", "rerank", "moderation", "ocr", "transcribe", "voxtral", "imagen", "veo",
           "image", "audio", "vision", "-vl", "aqa", "learnlm", "live", "parse", "retriever", "reward", "safety", "clip",
           "deplot", "kosmos", "fuyu", "neva", "paligemma", "computer-use", "robotics", "native", "codestral", "devstral")
    if any(x in low for x in bad):
        return False
    if p == "gemini":
        return "gemini" in low and "pro" not in low.split("/")[-1].split("-")
    if p == "nvidia":
        return mid in PROVIDERS["nvidia"]["prefer"] or any(x in low for x in ("instruct", "-it", "chat"))
    return True


def _rank(p: str, mid: str) -> tuple:
    pref = PROVIDERS[p]["prefer"]
    if mid in pref:
        return (0, pref.index(mid), mid)
    low = mid.lower()
    size = re.search(r"(\d+)b\b", low)
    lite = 0 if ("lite" in low or "small" in low or "mini" in low or "nano" in low) else 1
    preview = 1 if ("preview" in low or "exp" in low) else 0
    return (1, preview, lite, int(size.group(1)) if size else 50, low)


def provider_models(p: str, key: str, fresh: bool = False) -> List[str]:
    """Chat models this key can use, best first (pinned model first). From the provider's own /models, cached 6 h."""
    pinned = router_cfg()["models"].get(p, "")
    if p == "groq":
        ids = groq_models(key)
    elif p == "openai":
        ids = [OPENAI_MODEL]
    else:
        ck = f"{p}:{key_id(key)}"
        hit = _PMODELS.get(ck)
        ids = []
        if hit and time.time() - hit[0] < 6 * 3600 and not fresh:
            ids = hit[1]
        else:
            raw = "" if fresh else (get_setting(f"aimodels3:{ck}") or "")
            ts, _, lst = raw.partition("|")
            if lst and ts.isdigit() and time.time() - int(ts) < 6 * 3600:
                ids = [x for x in lst.split(",") if x]
            else:
                try:
                    res = http_json(PROVIDERS[p]["base"] + "/models", "GET", None, _auth(p, key), timeout=5)
                    data = res.get("data") if isinstance(res, dict) else None
                    if data is None and isinstance(res, dict):
                        data = res.get("models") or []
                    got = []
                    for m in data or []:
                        mid = str(m.get("id") or m.get("name") or "").replace("models/", "")
                        if not mid or not _model_ok(p, mid):
                            continue
                        if p == "openrouter":
                            pr = m.get("pricing") or {}
                            if not (mid.endswith(":free") or (str(pr.get("prompt")) in ("0", "0.0") and str(pr.get("completion")) in ("0", "0.0"))):
                                continue
                        if p == "mistral" and isinstance(m.get("capabilities"), dict) and m["capabilities"].get("completion_chat") is False:
                            continue
                        got.append(mid)
                    _bad = set(bad_models(p, key))
                    ids = sorted(dict.fromkeys(g for g in got if g not in _bad), key=lambda x: _rank(p, x))[:8]
                except Exception:
                    ids = []
                if ids:
                    try:
                        set_setting(f"aimodels3:{ck}", f"{int(time.time())}|{','.join(ids)}")
                    except Exception:
                        log_error("aimodels3 cache")
            if not ids:
                _bad = set(bad_models(p, key))
                ids = [m for m in PROVIDERS[p]["prefer"] if m not in _bad][:4]
            _PMODELS[ck] = (time.time(), ids)
    return [m for m in dict.fromkeys([pinned] + list(ids)) if m]


def bad_models(p: str, key: str) -> List[str]:
    """Models that answered 'not found' for this key (kept 24 h, so a /models refresh can't bring them back)."""
    raw = get_setting(f"aimbad:{p}:{key_id(key)}") or ""
    ts, _, lst = raw.partition("|")
    if lst and ts.isdigit() and time.time() - int(ts) < 86400:
        return [x for x in lst.split(",") if x]
    return []


def promote_model(p: str, key: str, model: str) -> None:
    """The model that just worked goes first for this key (cached like the list)."""
    if p in ("groq", "openai"):
        return
    ck = f"{p}:{key_id(key)}"
    ids = provider_models(p, key)
    if not ids or ids[0] == model or router_cfg()["models"].get(p):
        return
    ids = [model] + [m for m in ids if m != model]
    _PMODELS[ck] = (time.time(), ids)
    try:
        set_setting(f"aimodels3:{ck}", f"{int(time.time())}|{','.join(ids[:8])}")
    except Exception:
        log_error("promote_model")


def drop_model(p: str, key: str, model: str) -> None:
    if p == "groq":
        drop_groq_model(key, model)
        return
    ck = f"{p}:{key_id(key)}"
    try:
        bad = list(dict.fromkeys(bad_models(p, key) + [model]))[-30:]
        set_setting(f"aimbad:{ck}", f"{int(time.time())}|{','.join(bad)}")
    except Exception:
        log_error("bad_models")
    ids = [m for m in provider_models(p, key) if m != model]
    _PMODELS[ck] = (time.time(), ids or [m for m in PROVIDERS[p]["prefer"] if m != model][:3])
    try:
        set_setting(f"aimodels3:{ck}", f"{int(time.time())}|{','.join(_PMODELS[ck][1])}")
    except Exception:
        log_error("drop_model")


def _auth(p: str, key: str) -> Dict[str, str]:
    h = {"Authorization": f"Bearer {key}"}
    if p == "openrouter":
        h.update({"HTTP-Referer": PUBLIC_HOME, "X-Title": "filesbull"})
    return h


def provider_body(p: str, model: str, messages: List[Dict[str, Any]], max_tokens: int, fast: bool = False,
                  json_mode: bool = True) -> Dict[str, Any]:
    """The request each provider expects (all speak the OpenAI chat shape)."""
    if p == "groq":
        body = groq_body(model, messages, max_tokens, fast)
        if not json_mode or _NOJSON.get(p + ":" + model):
            body.pop("response_format", None)
        return body
    if p == "openai":
        body = {"model": model, "messages": messages}
        if model.startswith(("gpt-5", "o1", "o3", "o4")):
            body["reasoning_effort"] = "minimal" if model.startswith("gpt-5") else "low"
            body["max_completion_tokens"] = 800
        else:
            body["max_completion_tokens"] = max_tokens
            body["temperature"] = 0.1
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        return body
    low = model.lower()
    body = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.1}
    if json_mode and PROVIDERS[p]["json"] and not _NOJSON.get(p + ":" + model):
        body["response_format"] = {"type": "json_object"}
    if p == "gemini" and "2.5-flash" in low and "lite" not in low:
        body["reasoning_effort"] = "none"          # 2.5 Flash thinks by default: off = faster, fewer tokens
    if _is_reasoning(model):
        body["max_tokens"] = max_tokens + (250 if fast else 600)
        if "gpt-oss" in low:
            body["reasoning_effort"] = "low"
        if p == "openrouter":
            body["reasoning"] = {"effort": "low", "exclude": True}
    return body


def strip_reasoning(text: Any) -> str:
    """Remove a reasoning model's thinking (<think>…</think>, an unclosed <think>, ```json fences)."""
    if isinstance(text, list):
        text = "".join(str(x.get("text") or "") if isinstance(x, dict) else str(x) for x in text)
    t = re.sub(r"<think>.*?</think>", "", str(text or ""), flags=re.S | re.I)
    if re.search(r"<think>", t, re.I):
        t = re.split(r"<think>", t, flags=re.I)[0]
    t = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", t.strip(), flags=re.I)
    return t.strip()


def first_json(text: str) -> Optional[str]:
    """The JSON in a reply: the whole text when it is JSON, else the first balanced {...} object that parses."""
    t = (text or "").strip()
    if not t:
        return None
    try:
        json.loads(t)
        return t
    except Exception:
        pass
    start = t.find("{")
    while start != -1:
        depth, in_str, esc_ = 0, False, False
        for i in range(start, len(t)):
            ch = t[i]
            if in_str:
                if esc_:
                    esc_ = False
                elif ch == "\\":
                    esc_ = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    cand = t[start:i + 1]
                    try:
                        json.loads(cand)
                        return cand
                    except Exception:
                        break
        start = t.find("{", start + 1)
    return None


def ai_err_class(exc: Exception) -> str:
    """limit | invalid | model | badjson | server | timeout | bad_request | other"""
    low = str(exc).lower()
    if "http 429" in low or "rate limit" in low or "quota" in low or "resource_exhausted" in low or "http 402" in low:
        return "limit"
    if ("http 401" in low or "http 403" in low or "invalid api key" in low or "api key not valid" in low
            or "api_key_invalid" in low or "unauthorized" in low or "authentication" in low):
        return "invalid"
    if "json_validate_failed" in low or "failed_generation" in low or "response_format" in low or "json mode" in low:
        return "badjson"
    if "function" in low and "not found" in low or "not found for account" in low:
        return "model"          # NVIDIA: the model's function is gone / not on this account → next model, key is fine
    if "model" in low and any(x in low for x in ("not found", "decommission", "does not exist", "not supported",
                                                 "no access", "no endpoints", "is not a valid model", "unknown model")):
        return "model"
    if re.search(r"http 5\d\d", low) or "overloaded" in low or "unavailable" in low:
        return "server"
    if "timed out" in low or "timeout" in low or "time out" in low:
        return "timeout"
    if "http 400" in low or "http 404" in low or "http 422" in low:
        return "bad_request"
    return "other"


def provider_call(p: str, key: str, model: str, messages: List[Dict[str, Any]], max_tokens: int, timeout: float,
                  fast: bool = False, json_mode: bool = True) -> Dict[str, Any]:
    """One HTTP call. Returns {"text", "usage"}; raises with the provider's own error text (key scrubbed)."""
    body = provider_body(p, model, messages, max_tokens, fast, json_mode)
    try:
        res = http_json(PROVIDERS[p]["base"] + "/chat/completions", "POST", body, _auth(p, key), timeout=timeout)
    except Exception as exc:
        raise RuntimeError(scrub_keys(str(exc), key)) from None
    if isinstance(res, dict) and res.get("error") and not res.get("choices"):
        err = res["error"]
        code = err.get("code") if isinstance(err, dict) else None
        msg_ = err.get("message") if isinstance(err, dict) else err
        raise RuntimeError(scrub_keys(f"HTTP {code or 500}: {msg_}", key))
    try:
        msg_ = res["choices"][0]["message"]
    except Exception:
        raise RuntimeError("empty reply (no choices)") from None
    usage = res.get("usage") if isinstance(res, dict) else None
    return {"text": strip_reasoning(msg_.get("content")),
            "usage": {k: usage.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens") if k in usage}
            if isinstance(usage, dict) else None}


def scrub_keys(s: str, key: str = "") -> str:
    if key:
        s = s.replace(key, "***")
    return re.sub(r"gsk_[A-Za-z0-9]+|sk-[A-Za-z0-9_\-]+|AIza[0-9A-Za-z_\-]+|nvapi-[A-Za-z0-9_\-]+", "***", s)


def _stat(p: str, ok: bool) -> None:
    day = _AI_STATS.setdefault("day", {})
    rec = day.setdefault(p, [0, 0])
    rec[0 if ok else 1] += 1
    if not _AI_STATS.get("queued"):
        _AI_STATS["queued"] = True
        defer(flush_ai_stats)


def flush_ai_stats() -> None:
    """Add this update's per-provider call counts to today's row (bot_settings aicalls:<IST day>)."""
    _AI_STATS["queued"] = False
    add = _AI_STATS.pop("day", {}) or {}
    if not add:
        return
    k = f"aicalls:{ist_day()}"
    try:
        cur = json.loads(get_setting(k) or "{}")
    except Exception:
        cur = {}
    for p, (ok, bad) in add.items():
        prev = cur.get(p) if isinstance(cur.get(p), list) else [0, 0]
        cur[p] = [int(prev[0]) + ok, int(prev[1]) + bad]
    set_setting(k, json.dumps(cur))


def calls_today() -> Dict[str, List[int]]:
    try:
        cur = json.loads(get_setting(f"aicalls:{ist_day()}") or "{}")
    except Exception:
        cur = {}
    for p, (ok, bad) in (_AI_STATS.get("day") or {}).items():
        prev = cur.get(p) if isinstance(cur.get(p), list) else [0, 0]
        cur[p] = [int(prev[0]) + ok, int(prev[1]) + bad]
    return {p: v for p, v in cur.items() if isinstance(v, list)}


SWITCH_WORD = {"limit": "limit", "invalid": "key kharab", "server": "server error", "timeout": "slow/timeout",
               "badjson": "galat jawab", "empty": "khali jawab", "model": "model nahi mila", "bad_request": "request error",
               "other": "error", "cooling": "ruki hui"}


def note_switch(frm: str, to: str, why: str) -> None:
    """"🔁 AI switch: Groq → Gemini (limit)" in the Log Chat, at most once per 10 min per failing provider."""
    now = time.time()
    if now - _SWITCH_LOGGED.get(frm, 0) < SWITCH_LOG_EVERY:
        return
    _SWITCH_LOGGED[frm] = now
    line = f"🔁 AI switch: {pname(frm)} → {pname(to) if to else 'koi backup nahi chala'} ({SWITCH_WORD.get(why, why)})"

    def send() -> None:
        try:
            last = float((get_setting(f"aiswitch:{frm}") or "0").split("|")[0] or 0)
        except Exception:
            last = 0.0
        if time.time() - last < SWITCH_LOG_EVERY:
            return
        set_setting(f"aiswitch:{frm}", f"{int(time.time())}|{line}")
        try:
            if get_setting("log_chat_id"):
                log_chat_send(line)
        except Exception:
            log_error("ai switch log")
    defer(send)


def ai_complete(system: str, user_text: str, max_tokens: int = 150, cap: float = 9.0, fast: bool = False,
                want_json: bool = True, only: Optional[str] = None) -> Optional[str]:
    """THE router: ⭐ LIVE provider first, then the backups in the owner's order, same request each time.
    One overall time cap (the webhook must answer Telegram fast). Returns the reply text (the JSON object when
    want_json) or None when every provider failed — callers then use their local fallback."""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user_text}]
    _AI_LAST.clear()
    chain = [only] if only else ai_chain()
    deadline = time.time() + max(2.0, min(9.0, cap))
    first_fail: Optional[tuple] = None
    for pi, p in enumerate(chain):
        keys = provider_keys(p)
        usable = [k for k in keys if not key_cooling(k)]
        if not usable:
            # every key of this provider is resting: the last provider still gets one try (better than nothing)
            if pi < len(chain) - 1 or not keys:
                first_fail = first_fail or (p, "cooling")
                continue
            usable = keys
        why = ""
        prov_done = False
        for n, key in enumerate(usable):
            if prov_done:
                break
            models = provider_models(p, key)[:4 if p == "groq" else (6 if p == "nvidia" else 3)]
            key_done = False
            for model in models:
                left = deadline - time.time()
                if left < 1:
                    return None
                more = pi < len(chain) - 1
                # leave time for a backup: the live provider gets ~65 % of what is left when a backup exists
                tmo = max(1.0, min(7.0, left * 0.65 if more else left - 0.2))
                t_call = time.time()
                for attempt in range(2):
                    try:
                        r = provider_call(p, key, model, messages, max_tokens, tmo, fast, want_json)
                        text = r["text"]
                        if want_json:
                            text = first_json(text) or ""
                        if not text:
                            # empty / not JSON: the same request goes to the next provider (no extra calls here)
                            why = "empty" if not r["text"] else "badjson"
                            _stat(p, False)
                            key_done = prov_done = True
                            break
                        _stat(p, True)
                        mark_key_ok(key)
                        _AI_LAST.update(provider=p, model=model, ms=int((time.time() - t_call) * 1000), usage=r["usage"])
                        if model != models[0]:
                            promote_model(p, key, model)
                        if first_fail and first_fail[0] != p:
                            _AI_LAST["switched_from"] = pname(first_fail[0])
                            note_switch(first_fail[0], p, first_fail[1])
                        return text
                    except Exception as exc:
                        _stat(p, False)
                        cls = ai_err_class(exc)
                        low = str(exc).lower()
                        why = cls
                        if cls in {"badjson", "bad_request"} and attempt == 0 and want_json and \
                                ("response_format" in low or "json" in low) and p != "groq":
                            _NOJSON[p + ":" + model] = True    # this model has no JSON mode: ask plainly, parse the JSON
                            continue
                        if cls == "model":
                            drop_model(p, key, model)
                        elif cls in {"badjson", "bad_request"}:
                            pass
                        else:
                            print(f"[filesbull] {p} key {n + 1} ({mask_key(key)}) error: {scrub_keys(str(exc), key)[:160]}", flush=True)
                            m = re.search(r"try again in (\d+)m", low)
                            if cls == "invalid":
                                cool_key(key, 3600, "invalid")
                            else:
                                cool_key(key, max(KEY_COOLDOWN, int(m.group(1)) * 60) if m else KEY_COOLDOWN, "limit")
                            key_done = True
                        break
                if key_done:
                    break
        first_fail = first_fail or (p, why or "other")
    if first_fail and len(chain) > 1:
        note_switch(first_fail[0], "", first_fail[1])
    return None


def validate_key(p: str, key: str) -> Dict[str, Any]:
    """New key check: a free auth check, then ONE tiny real call. {"st": ok|invalid|limit|error, "ms", "model", "err"}"""
    free_url = {"openrouter": "/key", "nvidia": ""}.get(p, "/models")
    if free_url:
        try:
            http_json(PROVIDERS[p]["base"] + free_url, "GET", None, _auth(p, key), timeout=6)
        except Exception as exc:
            cls = ai_err_class(exc)
            if cls == "invalid" or "http 400" in str(exc).lower() and p == "gemini":
                return {"st": "invalid", "err": scrub_keys(str(exc), key)[:160]}
            if cls == "limit":
                return {"st": "limit", "err": scrub_keys(str(exc), key)[:160]}
            return {"st": "error", "err": scrub_keys(str(exc), key)[:160]}
    out: Dict[str, Any] = {"st": "ok"}
    _PMODELS.pop(f"{p}:{key_id(key)}", None)
    models = provider_models(p, key, fresh=True) if p != "groq" else groq_models(key)
    for model in models[:6 if p == "nvidia" else 2]:
        t0 = time.monotonic()
        try:
            r = provider_call(p, key, model, [{"role": "system", "content": "Reply ONLY with JSON {\"ok\":true}."},
                                              {"role": "user", "content": "ping"}], 20, 7, True, True)
            out.update(ms=int((time.monotonic() - t0) * 1000), model=model, reply=r["text"][:30], err=None)
            mark_key_ok(key, now=True)
            if model != models[0]:
                promote_model(p, key, model)
            return out
        except Exception as exc:
            cls = ai_err_class(exc)
            out.update(model=model, err=scrub_keys(str(exc), key)[:160])
            if cls == "model":
                drop_model(p, key, model)
                continue
            if cls == "invalid":
                out["st"] = "invalid"
                return out
            if cls == "limit":
                out["st"] = "limit"
                return out
    return out


AI_SYSTEM = ("You help a Telegram bot that searches a catalog of study PDFs for Indian govt exams. "
             "Read the user's message (Hindi/Hinglish/English) and reply ONLY with JSON: "
             '{"intent":"book|more|greeting|off_topic","keywords":[],"exam":null,"subject":null,'
             '"language":null,"ask":null}. keywords = 1-4 short words likely in a file name '
             "(e.g. ssc, gd, gk, polity, lucent, maths, class 10). Fix spelling. Never invent book titles. "
             "language = hindi|english|null. intent more = user wants other/different files than last time. "
             "off_topic = not about books/study. ask = ONE short Hinglish question only if too vague to search, else null.")


def ai_budget_ok(user_id: Optional[int], ctx: Dict[str, Any]) -> bool:
    if not ai_provider() or not user_id:
        return False
    if int(ctx.get("ai", 0)) >= AI_TURNS_PER_CHAT:
        return False
    today = ist_day()
    for key, cap in ((f"aiday:{user_id}", AI_DAILY_PER_USER), ("aiday:all", AI_DAILY_TOTAL)):
        raw = get_setting(key) or ""
        day, _, n = raw.partition(":")
        if day == today and n.isdigit() and int(n) >= cap:
            return False
    return True


def ai_spend(user_id: int, ctx: Dict[str, Any]) -> None:
    ctx["ai"] = int(ctx.get("ai", 0)) + 1
    today = ist_day()
    for key in (f"aiday:{user_id}", "aiday:all"):
        try:
            raw = get_setting(key) or ""
            day, _, n = raw.partition(":")
            count = int(n) + 1 if day == today and n.isdigit() else 1
            set_setting(key, f"{today}:{count}")
        except Exception:
            log_error("ai_spend")


def ai_interpret(text: str, ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    prev = " ".join(ctx.get("q") or [])
    content = ai_complete(AI_SYSTEM, (f"Last search: {prev}\n" if prev else "") + "User: " + text[:300])
    if not content:
        return None
    try:
        data = json.loads(content[content.find("{"): content.rfind("}") + 1])
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    kws = []
    for k in (data.get("keywords") or [])[:4] + [data.get("exam"), data.get("subject")]:
        if isinstance(k, str):
            for w in tokenize(k)[:3]:
                if w not in FILLER and w not in kws and len(w) <= 30 and (len(w) > 1 or w.isdigit()):
                    kws.append(w)
    lang = str(data.get("language") or "").lower()
    ask = data.get("ask")
    return {"intent": str(data.get("intent") or "book").lower(), "keywords": kws[:5],
            "lang": "hi" if lang.startswith("hi") else ("en" if lang.startswith("en") else None),
            "ask": re.sub(r"\s+", " ", ask).strip()[:150] if isinstance(ask, str) and ask.strip() else None}


# ---- search (database only)

def _term_in(term: str, text: str) -> bool:
    """Short latin terms (gd, gk, gs, ca) must be whole words — "gs" inside "things" is not GS (Aarav 6:40 PM:
    "Gd gk dedo" returned "Physics all Formulas X grp NDA")."""
    if len(term) <= 3 and term.isascii():
        return re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", text) is not None
    return term in text


def search_rows(keywords: List[str], lang: Optional[str] = None, types: Optional[List[str]] = None,
                strict: bool = True, require_all: bool = False) -> List[Dict[str, Any]]:
    """Matching rows, best first. Each keyword counts once even with its aliases (itihas = history).
    strict: when some files match EVERY keyword ("ssc gd gk hindi"), only those are returned;
    otherwise files matching any keyword, best first."""
    groups = []
    for k in keywords:
        k = normalize(k)
        if not k:
            continue
        g = set(ALIASES.get(k, {k})) | {k}
        if g not in groups:
            groups.append(g)
    if not groups:
        return []
    # Typed keywords first, then their aliases (the pre-filter has a size cap).
    primary = [normalize(k) for k in keywords if normalize(k)]
    words = list(dict.fromkeys(primary + sorted(t for g in groups for t in g if len(t) > 1 or t.isdigit())))[:20]
    filters = ",".join(f"{col}.ilike.*{word}*" for word in words
                       for col in ("title", "caption", "keywords", "content_text", "source_chat_title", "file_name"))
    rows = supabase("books", "GET", query=(
        "?select=id,title,caption,keywords,content_text,source_chat_title,language,file_name"
        "&status=eq.indexed&limit=300&or=" + urllib.parse.quote(f"({filters})", safe="(),.*")
    ))
    scored = []
    for row in rows:
        head = normalize(" ".join(str(row.get(k) or "") for k in ("title", "caption", "keywords", "source_chat_title", "file_name")))
        body = normalize(str(row.get("content_text") or ""))
        score = 0.0
        hits = 0
        for g in groups:
            if any(_term_in(term, head) for term in g):
                score += 3
                hits += 1
            elif any(_term_in(term, body) for term in g):
                score += 1
                hits += 1
        if not score:
            continue
        row["_all"] = hits == len(groups)
        if lang:
            words_lang = {"hi": ("hindi", "हिंदी", "हिन्दी"), "en": ("english",)}[lang]
            if row.get("language") in ({"hi", "mixed"} if lang == "hi" else {"en", "mixed"}) or any(w in head for w in words_lang):
                score += 1
        for t in types or []:
            if t in head:
                score += 0.5
        scored.append((score, row))
    scored.sort(key=lambda x: (-x[0], str(x[1].get("title") or "")))
    out = [row for _, row in scored]
    if strict and len(groups) > 1 and any(r.get("_all") for r in out):
        out = [r for r in out if r.get("_all")]
    if require_all:
        out = [r for r in out if r.get("_all")]      # weak partial matches are not results
    return out


def has_full_match(keywords: List[str], lang: Optional[str] = None) -> bool:
    rows = search_rows(keywords, lang)
    return bool(rows) and bool(rows[0].get("_all"))


def search_books(query: str) -> List[Dict[str, Any]]:
    """Keyword search used by /search, admin rename and the plain fallback (top 10)."""
    u = understand(query)
    kws = u["keywords"] or [t for t in u["tokens"] if len(t) > 1 and t not in FILLER]
    if not kws and u["lang"]:
        kws = ["hindi" if u["lang"] == "hi" else "english"]
    rows = search_rows(kws, u["lang"], u["types"])[:10]
    return ai_rank(query, rows) if AI_RANK else rows


def results_markup(rows: List[Dict[str, Any]], more_left: int) -> Dict[str, Any]:
    kb = [[{"text": f"📄 {str(r.get('title') or 'Untitled')[:45]}", "callback_data": f"book:{r['id']}"}] for r in rows]
    if more_left > 0:
        kb.append([{"text": f"➡️ Aur dikhao ({more_left})", "callback_data": "more"}])
    return {"inline_keyboard": kb}


def send_results(chat_id: int, user_id: Optional[int], ctx: Dict[str, Any], keywords: List[str],
                 lang: Optional[str], types: Optional[List[str]], raw: str, note: str = "",
                 require_all: bool = False, rows: Optional[List[Dict[str, Any]]] = None) -> bool:
    """Search + show page 1. Returns False when nothing was found (nothing sent)."""
    if rows is None:
        rows = search_rows(keywords, lang, types, require_all=require_all)
    note_user(None, searches=1)
    if not rows:
        return False
    page = rows[:PAGE]
    ctx.update({"q": keywords, "lang": lang, "types": types or [], "raw": raw[:120], "shown": [r["id"] for r in page],
                "ask": None, "pending": None})
    save_ctx(user_id, ctx)
    send_text(chat_id, note + f"🔎 <b>{len(rows)} file mili</b> — jo chahiye us par tap karo 👇",
              results_markup(page, len(rows) - len(page)), "HTML")
    return True


def send_no_results(chat_id: int, label: str) -> None:
    send_text(chat_id, f"😕 <b>\"{esc(label[:60])}\" ke liye koi file nahi mili</b>\n\n"
                       "Dusra naam, chhota keyword ya sahi spelling try karo.\n"
                       "Jaise: <i>lucent</i>, <i>ssc gd gk</i>, <i>polity</i>", None, "HTML")


def show_more(chat_id: int, user_id: Optional[int], ctx: Dict[str, Any]) -> None:
    if not ctx.get("q"):
        send_text(chat_id, NO_CONTEXT_MORE_MSG, None, "HTML")
        return
    shown = set(int(x) for x in ctx.get("shown") or [])
    rows = [r for r in search_rows(ctx["q"], ctx.get("lang"), ctx.get("types")) if int(r["id"]) not in shown]
    broadened = False
    if not rows and len(ctx["q"]) > 1:
        # Nothing left for the exact search: try each word on its own (e.g. "ssc gd gk" -> "gk").
        seen = set(shown)
        for k in ctx["q"]:
            for r in search_rows([k], ctx.get("lang"), ctx.get("types")):
                if int(r["id"]) not in seen:
                    seen.add(int(r["id"]))
                    rows.append(r)
        broadened = bool(rows)
    note_user(None, searches=1)
    if not rows:
        send_text(chat_id, ALL_SHOWN_MSG, {"inline_keyboard": [[{"text": "📚 PDF List", "callback_data": "user_list"}]]}, "HTML")
        return
    page = rows[:PAGE]
    ctx["shown"] = list(shown) + [r["id"] for r in page]
    save_ctx(user_id, ctx)
    head = "🔎 <b>" + (f"Milti-julti {len(rows)} aur file mili" if broadened else f"{len(rows)} aur file mili") + \
           "</b> — tap karo 👇"
    send_text(chat_id, head, results_markup(page, len(rows) - len(page)), "HTML")


def ask_question(chat_id: int, user_id: Optional[int], ctx: Dict[str, Any], kind: str, pending: List[str],
                 lang: Optional[str], text: Optional[str] = None) -> None:
    buttons = EXAM_BUTTONS if kind == "exam" else SUBJECT_BUTTONS
    kb, row = [], []
    for label, val in buttons:
        row.append({"text": label, "callback_data": f"g:{'e' if kind == 'exam' else 's'}:{val}"})
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    if kind == "exam" and pending:
        n = len(search_rows(pending, lang))
        kb[-1][-1]["text"] = f"📚 Sab dikhao ({n})"
    ctx.update({"ask": kind, "pending": pending, "lang": lang})
    save_ctx(user_id, ctx)
    msg = text or (ask_exam_msg(" ".join(pending)) if kind == "exam" else ASK_SUBJECT_MSG)
    send_text(chat_id, msg, {"inline_keyboard": kb}, "HTML")


def smart_reply(chat_id: int, user: Dict[str, Any], text: str, searched: bool = False) -> None:
    """Every free-text search message (users, and admins' searches). Aarav 6:44 PM Oct 26: "ai phle understand
    krega fir uske liye book search hogi" — one small AI reading first (cached 24 h), then the search with what it
    understood; the free local parser is the fallback whenever the AI is not available, slow or limited."""
    user_id = user.get("id")
    u = understand(text)
    ctx = load_ctx(user_id)
    if not is_follow_up(u, ctx):
        try:
            if ai_first_reply(chat_id, user, text, u, ctx, searched):
                return
        except Exception:
            log_error("ai_first")
            count_understood("local_fallback")
    smart_reply_local(chat_id, user, text, searched, u=u, allow_ai=not AI_UNDERSTAND, ctx=ctx)


def smart_reply_local(chat_id: int, user: Dict[str, Any], text: str, searched: bool = False,
                      u: Optional[Dict[str, Any]] = None, allow_ai: bool = False,
                      ctx: Optional[Dict[str, Any]] = None) -> None:
    """The free local parser (e497af1 rules). searched=True: an admin already tapped 🔍 Search — never ask again."""
    user_id = user.get("id")
    u = u or understand(text)
    intent = u["intent"]
    if intent == "greeting":
        send_text(chat_id, greeting_msg(user), None, "HTML")
        return
    if intent == "thanks":
        send_text(chat_id, THANKS_MSG)
        return
    if intent in {"off_topic", "empty"}:
        offtopic_reply(chat_id, user, text, searched)
        return
    ctx = ctx if ctx is not None else load_ctx(user_id)
    if intent == "more":
        show_more(chat_id, user_id, ctx)
        return
    keywords, lang, types = list(u["keywords"]), u["lang"], u["types"]
    # Answer to our own question ("kis exam ke liye?") typed instead of tapped.
    if ctx.get("ask") and ctx.get("pending") and keywords and len(keywords) <= 3 and \
            all(k in EXAM_WORDS or k in SUBJECT_WORDS for k in keywords):
        merged = list(dict.fromkeys(list(ctx["pending"]) + keywords))
        if not has_full_match(merged, lang or ctx.get("lang")):
            merged = keywords
        if send_results(chat_id, user_id, ctx, merged, lang or ctx.get("lang"), types, text):
            return
    # "iski english wali" -> same search, new language/type
    if not keywords and u["ref"] and ctx.get("q") and (lang or types):
        if send_results(chat_id, user_id, ctx, ctx["q"], lang or ctx.get("lang"), types or ctx.get("types"), text):
            return
    if not keywords:
        if lang:
            # "Mujhe ek hindi ki book chahiye": search the language as a subject; if many, ask the exam.
            kw = ["hindi" if lang == "hi" else "english"]
            rows = search_rows(kw, lang, types)
            if len(rows) > 6:
                ask_question(chat_id, user_id, ctx, "exam", kw, lang)
                return
            if send_results(chat_id, user_id, ctx, kw, lang, types, text):
                return
            send_no_results(chat_id, kw[0])
            return
        generic_reply(chat_id, user_id)          # "ek badhiya si book do" → subjects + most-taken files, no search
        return
    if send_results(chat_id, user_id, ctx, keywords, lang, types, text, require_all=True):
        return
    fuzzy = fuzzy_rows(keywords)
    if fuzzy and send_results(chat_id, user_id, ctx, keywords, lang, types, text, rows=fuzzy):
        return
    key_terms = [k for k in keywords if is_key_term(k)]
    if key_terms:
        # Local parsing understood it (lucent, gd, gk…) and it is truly not there → no AI tokens.
        not_found_reply(chat_id, user, keywords)
        return
    # No known word at all (typos, long sentences): ask the AI once, within the limits — only when the
    # AI-first reading is switched off (AI_UNDERSTAND=0); otherwise the AI was already tried for this message.
    if allow_ai and ai_budget_ok(user_id, ctx):
        ans = ai_interpret(text, ctx)
        ai_spend(user_id, ctx)
        save_ctx(user_id, ctx)
        if ans:
            if ans["intent"] in {"off_topic", "greeting"}:
                offtopic_reply(chat_id, user, text, searched)
                return
            if ans["intent"] == "more" and ctx.get("q"):
                show_more(chat_id, user_id, ctx)
                return
            if ans["keywords"] and ans["keywords"] != keywords and \
                    send_results(chat_id, user_id, ctx, ans["keywords"], ans["lang"] or lang, types, text, require_all=True):
                return
            if ans["ask"]:
                ask_question(chat_id, user_id, ctx, "exam" if keywords else "subject", keywords, lang,
                             "🤔 " + esc(ans["ask"]))
                return
    not_found_reply(chat_id, user, keywords or [text])


def handle_guided_callback(query: Dict[str, Any], data: str, chat_id: int) -> None:
    user = query.get("from") or {}
    user_id = user.get("id")
    ctx = load_ctx(user_id)
    if data == "more":
        show_more(chat_id, user_id, ctx)
        return
    _, kind, val = (data.split(":", 2) + ["", ""])[:3]
    pending = list(ctx.get("pending") or [])
    lang = ctx.get("lang")
    if kind == "s":
        if not send_results(chat_id, user_id, ctx, [val], lang, None, val):
            send_no_results(chat_id, val)
        return
    if val == "all" or not val:
        if not pending or not send_results(chat_id, user_id, ctx, pending, lang, None, " ".join(pending)):
            offtopic_reply(chat_id, user, "", True)
        return
    label = dict((v, l) for l, v in EXAM_BUTTONS).get(val, val)
    if has_full_match(pending + [val], lang) and send_results(chat_id, user_id, ctx, pending + [val], lang, None, " ".join(pending + [val])):
        return
    if pending and send_results(chat_id, user_id, ctx, pending, lang, None, " ".join(pending),
                                note=f"ℹ️ {esc(label)} ke liye alag file nahi mili, ye saari {esc(' '.join(pending))} files hain:\n\n"):
        return
    send_no_results(chat_id, " ".join(pending + [val]))


# ================================================================ user tracking
# Table bot_users (files/filesbull_supabase.sql). Until it exists, the same record is kept in
# bot_settings key "user:<id>" as JSON, so nothing breaks and nothing is lost.

_USERS_TABLE: Dict[str, Any] = {}


def users_table_ok() -> bool:
    hit = _USERS_TABLE.get("ok")
    if hit is not None and time.time() - _USERS_TABLE.get("ts", 0) < 300:
        return hit
    try:
        supabase("bot_users", "GET", query="?select=user_id&limit=1")
        ok = True
    except Exception:
        ok = False
    _USERS_TABLE.update({"ok": ok, "ts": time.time()})
    return ok


def get_user_rec(user_id: int) -> Optional[Dict[str, Any]]:
    if users_table_ok():
        rows = supabase("bot_users", "GET", query=f"?user_id=eq.{int(user_id)}&select=*&limit=1")
        if rows:
            return rows[0]
    raw = get_setting(f"user:{user_id}")
    try:
        return json.loads(raw) if raw else None
    except Exception:
        return None


def save_user_rec(rec: Dict[str, Any]) -> None:
    if users_table_ok():
        try:
            supabase("bot_users", "POST", rec, "?on_conflict=user_id")
            return
        except Exception:
            log_error("bot_users save")
    set_setting(f"user:{rec['user_id']}", json.dumps(rec, ensure_ascii=False))


def note_user(user: Optional[Dict[str, Any]], **changes: Any) -> None:
    """Remember who this update is from and what happened; written once by flush_user()."""
    if user and user.get("id") and not user.get("is_bot"):
        _REQ.setdefault("user", user)
    for key, value in changes.items():
        if key == "verified":
            _REQ["verified"] = value
        else:
            _REQ[key] = _REQ.get(key, 0) + int(value)


def user_label(rec: Dict[str, Any]) -> str:
    name = " ".join(x for x in [rec.get("first_name"), rec.get("last_name")] if x).strip() or "User"
    uname = f" @{rec['username']}" if rec.get("username") else ""
    return f"{name[:40]}{uname}"


_USER_SEEN: Dict[int, float] = {}


def flush_user() -> None:
    user = _REQ.get("user")
    if not user:
        return
    uid = int(user["id"])
    # nothing to count and this instance wrote this user < 2 min ago: skip the read + write (2 DB round trips per tap)
    if not any(_REQ.get(k) for k in ("searches", "files")) and _REQ.get("verified") is None and \
            time.monotonic() - _USER_SEEN.get(uid, -1e9) < 110:
        return
    _USER_SEEN[uid] = time.monotonic()
    rec = get_user_rec(uid)
    new = rec is None
    if new:
        rec = {"user_id": uid, "first_seen": now_iso(), "verified": False, "searches": 0, "files_received": 0}
    before = json.dumps(rec, sort_keys=True, default=str)
    old_seen = str(rec.get("last_seen") or "")
    rec.update({"first_name": (user.get("first_name") or "")[:64], "last_name": (user.get("last_name") or "")[:64] or None,
                "username": user.get("username")})
    if _REQ.get("verified") is not None:
        rec["verified"] = bool(_REQ["verified"])
    rec["searches"] = int(rec.get("searches") or 0) + int(_REQ.get("searches", 0))
    rec["files_received"] = int(rec.get("files_received") or 0) + int(_REQ.get("files", 0))
    changed = new or json.dumps(dict(rec, last_seen=old_seen), sort_keys=True, default=str) != before
    stale = old_seen[:16] < time.strftime("%Y-%m-%dT%H:%M", time.gmtime(time.time() - 120))
    rec["last_seen"] = now_iso()
    if changed or stale:
        save_user_rec({k: rec.get(k) for k in ("user_id", "first_name", "last_name", "username", "first_seen",
                                               "last_seen", "verified", "searches", "files_received")})
    if new and not is_subadmin(uid):
        audit_log(f"🆕 Naya user: {user_label(rec)} ({uid})")


def defer(fn: Any) -> None:
    """Bookkeeping that the user's reply does not need (counters, caches): run after the reply, at the end of
    the update (flush_request)."""
    _REQ.setdefault("_defer", []).append(fn)


def run_deferred() -> None:
    for fn in _REQ.pop("_defer", []) or []:
        try:
            fn()
        except Exception:
            log_error("deferred")


def flush_request() -> None:
    run_deferred()
    try:
        flush_user()
    except Exception:
        log_error("flush_user")
    try:
        flush_logs()
    except Exception:
        log_error("flush_logs")
    _REQ.clear()


def all_user_recs() -> List[Dict[str, Any]]:
    """Fallback (no bot_users table): every user:<id> record from bot_settings."""
    rows = supabase("bot_settings", "GET", query="?key=like.user:*&select=value&limit=5000")
    out = []
    for r in rows:
        try:
            out.append(json.loads(r.get("value") or "{}"))
        except Exception:
            pass
    return out


def users_view() -> str:
    today_iso = ist_midnight_utc_iso()
    if users_table_ok():
        files_today, total, active, new, recent = par(
            lambda: count_rows("delivery_logs", f"&created_at=gte.{today_iso}"), lambda: count_rows("bot_users", col="user_id"),
            lambda: count_rows("bot_users", f"&last_seen=gte.{today_iso}", col="user_id"),
            lambda: count_rows("bot_users", f"&first_seen=gte.{today_iso}", col="user_id"),
            lambda: supabase("bot_users", "GET", query="?select=*&order=last_seen.desc&limit=10"))
        for x in (files_today, total, active, new, recent):
            if isinstance(x, Exception):
                raise x
        note = ""
    else:
        files_today = count_rows("delivery_logs", f"&created_at=gte.{today_iso}")
        recs = all_user_recs()
        total = len(recs)
        active = sum(1 for r in recs if str(r.get("last_seen") or "") >= today_iso)
        new = sum(1 for r in recs if str(r.get("first_seen") or "") >= today_iso)
        recent = sorted(recs, key=lambda r: str(r.get("last_seen") or ""), reverse=True)[:10]
        note = "\n\nℹ️ bot_users table abhi nahi bani — data bot_settings me safe hai. SQL chalane ke baad table me aayega."
    lines = ["👥 Users", f"Kul users: {total}", f"Aaj active: {active}", f"Aaj naye: {new}",
             f"Aaj files bheji: {files_today}", "", "🕑 Recent 10:"]
    for i, r in enumerate(recent, 1):
        lines.append(f"{i}. {user_label(r)} ({r.get('user_id')}) — 🔍 {r.get('searches') or 0} · 📄 {r.get('files_received') or 0}"
                     f" · {ist_time(r.get('last_seen'))}")
    if not recent:
        lines.append("Abhi koi user record nahi hai.")
    lines.append("\nKisi user ki details: /user <id>")
    return "\n".join(lines) + note


def user_detail(user_id: int) -> str:
    rec = get_user_rec(user_id)
    try:
        logs = supabase("delivery_logs", "GET", query=f"?user_id=eq.{int(user_id)}&select=book_title,query,created_at"
                                                      "&order=created_at.desc&limit=15")
    except Exception:
        logs = []
    if not rec and not logs:
        return f"User {user_id} ka koi record nahi mila."
    rec = rec or {"user_id": user_id}
    lines = [f"👤 {user_label(rec)}", f"ID: {user_id}",
             f"Pehli baar: {ist_time(rec.get('first_seen'))}", f"Last seen: {ist_time(rec.get('last_seen'))}",
             f"Join verified: {'✅' if rec.get('verified') else '❌'}",
             f"Searches: {rec.get('searches') or 0}", f"Files li: {rec.get('files_received') or 0}", ""]
    lines.append("📄 Aakhri files:" if logs else "📄 Abhi tak koi file nahi li.")
    for lg in logs:
        q = f" — query: {lg.get('query')}" if lg.get("query") else ""
        lines.append(f"• {str(lg.get('book_title') or '')[:60]}{q} ({ist_time(lg.get('created_at'))})")
    return "\n".join(lines)


# ================================================================ backup channel (no duplicates)

def _send_file_copy(target: int, from_chat: Any, message_id: Any, row: Dict[str, Any]) -> Optional[int]:
    """Copy a file into the backup chat: copy the post, else re-send by file_id. Returns message id."""
    if from_chat and message_id:
        try:
            payload: Dict[str, Any] = {"chat_id": target, "from_chat_id": from_chat, "message_id": message_id,
                                       "disable_notification": True}
            if row.get("_caption"):
                payload["caption"] = row["_caption"]
            res = telegram("copyMessage", payload).get("result") or {}
            if res.get("message_id"):
                return int(res["message_id"])
        except Exception as exc:
            if "too many requests" in str(exc).lower():
                raise
    if row.get("telegram_file_id"):
        mime = (row.get("mime_type") or "").lower()
        method, field = ("sendVideo", "video") if mime.startswith("video/") else \
            (("sendAudio", "audio") if mime.startswith("audio/") else ("sendDocument", "document"))
        res = telegram(method, {"chat_id": target, field: row["telegram_file_id"],
                                "caption": (row.get("_caption") or row.get("title") or "")[:1000], "disable_notification": True}).get("result") or {}
        if res.get("message_id"):
            return int(res["message_id"])
    return None


def ensure_backup(record: Dict[str, Any], from_chat: Any, message_id: Any, data: Optional[bytes] = None) -> str:
    """Back up a just-indexed file once, under its clean name. 'saved' | 'dup' | 'off' | 'failed'.
    With the file's bytes in hand (≤15 MB) the backup is a fresh upload whose FILE NAME is the clean name;
    otherwise a copy of the post whose caption is the clean name (Telegram can't rename a copied file)."""
    target = get_setting("backup_chat_id")
    uniq = record.get("telegram_file_unique_id")
    if not target or not uniq:
        return "off"
    rows = supabase("books", "GET", query=f"?telegram_file_unique_id=eq.{urllib.parse.quote(str(uniq))}"
                                          "&select=id,backup_chat_id,backup_message_id&limit=1")
    if rows and rows[0].get("backup_message_id") and str(rows[0].get("backup_chat_id")) == str(target):
        return "dup"
    mid = None
    caption = backup_caption(record)
    mime = str(record.get("mime_type") or "").lower()
    if data and not mime.startswith(("video/", "audio/")) and req_elapsed() < 6.0:
        try:
            res = telegram_upload("sendDocument", {"chat_id": int(target), "caption": caption, "disable_notification": "true"},
                                  "document", safe_filename(str(record.get("title") or ""), str(record.get("file_name") or "")), data)
            mid = int(((res or {}).get("result") or {}).get("message_id") or 0) or None
        except Exception:
            log_error("backup upload")
            mid = None
    if not mid:
        try:
            mid = _send_file_copy(int(target), from_chat, message_id, dict(record, _caption=caption))
        except Exception:
            log_error("backup copy")
            mid = None
    if not mid:
        return "failed"
    supabase("books", "PATCH", {"backup_chat_id": int(target), "backup_message_id": mid},
             f"?telegram_file_unique_id=eq.{urllib.parse.quote(str(uniq))}")
    return "saved"


def index_file(record: Dict[str, Any], from_chat: Any, message_id: Any, where: str) -> str:
    """Save a file to the catalog: name it the cheapest way (clear name → no reading; unclear → page 1..3 locally;
    AI only as last resort), put it in a category, THEN back it up once (clean name), and log it."""
    record.pop("_text", None)
    record.pop("_bytes", None)
    orig_title = str(record.get("file_name") or "") or str(record.get("title") or "")
    try:
        how = name_new_file(record)
    except Exception:
        log_error("name_new_file")
        how = "fallback"
    data = record.pop("_bytes", None)
    slug = record.pop("_cat", None) or "other"
    record["keywords"] = with_cat_tag(clean_text(" ".join(str(x or "") for x in (
        record.get("title"), record.get("keywords"))))[:3990], slug)
    if has_category_col():
        record["category"] = slug
    saved = upsert_item(record)
    book_id = (saved[0].get("id") if isinstance(saved, list) and saved and isinstance(saved[0], dict) else None)
    if not book_id and record.get("telegram_file_unique_id"):
        try:
            got_id = supabase("books", "GET", query="?telegram_file_unique_id=eq." +
                              urllib.parse.quote(str(record["telegram_file_unique_id"])) + "&select=id&limit=1")
            book_id = got_id[0].get("id") if got_id else None
        except Exception:
            book_id = None
    if book_id and not has_category_col():
        try:
            save_categories({int(book_id): slug})
        except Exception:
            log_error("catmap")
    if record.pop("_retry", None):
        queue_name_retry(book_id)
    if how != "kept":
        try:
            count_named_by(how)
        except Exception:
            log_error("named_by")
    record["_cat"] = slug
    # name changed but the file was never downloaded (clear name): fetch it only to store the backup under the
    # clean FILE name (no reading, no AI). Bigger than 15 MB → copy of the post with the clean caption.
    try:
        if not data and not record.get("backup_message_id") and get_setting("backup_chat_id") and how == "clean" and record.get("file_name") and \
                safe_filename(str(record.get("title") or ""), str(record["file_name"])) != record["file_name"] and \
                int(record.get("file_size") or 0) <= REUPLOAD_LIMIT and req_elapsed() < 4.0:
            got = download_file(record.get("telegram_file_id"), record.get("file_size"))
            data = got if got and len(got) <= REUPLOAD_LIMIT else None
    except Exception:
        data = None
    try:
        backup = ensure_backup(record, from_chat, message_id, data)
    except Exception:
        log_error("ensure_backup")
        backup = "failed"
    record.pop("_cat", None)
    if how != "kept":
        audit_log(file_added_log(record, orig_title, slug, how, where, backup))
    record.pop("_pages", None)
    return backup


def backup_pending_query(target: Any) -> str:
    return f"&or=(backup_message_id.is.null,backup_chat_id.neq.{int(target)})"


def backup_old_batch(limit: int = 20, budget: float = 7.0) -> Dict[str, Any]:
    target = get_setting("backup_chat_id")
    if not target:
        return {"error": "Pehle 💾 Backup Channel set karo."}
    started = time.monotonic()
    rows = supabase("books", "GET", query="?select=id,title,source_chat_id,source_message_id,telegram_file_id,mime_type"
                                          + backup_pending_query(target) + f"&order=id&limit={limit}")
    done = failed = 0
    wait = 0
    for row in rows:
        if time.monotonic() - started > budget:
            break
        try:
            mid = _send_file_copy(int(target), row.get("source_chat_id"), row.get("source_message_id"),
                                  dict(row, _caption=f"📄 {row.get('title') or ''}"[:1000]))
        except Exception as exc:
            m = re.search(r"retry after (\d+)", str(exc).lower())
            if m or "too many requests" in str(exc).lower():
                wait = int(m.group(1)) if m else 30
                break
            log_error("backup old")
            mid = None
        # backup_message_id 0 = tried and failed (source deleted, no file_id); not retried every run.
        supabase("books", "PATCH", {"backup_chat_id": int(target), "backup_message_id": mid or 0}, f"?id=eq.{int(row['id'])}")
        if mid:
            done += 1
        else:
            failed += 1
        time.sleep(0.3)
    remaining = count_rows("books", backup_pending_query(target))
    return {"done": done, "failed": failed, "remaining": remaining, "wait": wait}


def backup_old_view() -> Any:
    res = backup_old_batch()
    if res.get("error"):
        return "❌ " + res["error"], {"inline_keyboard": [[{"text": "💾 Backup Channel", "callback_data": "ad:backup"}], BACK_ROW]}
    lines = ["💾 Purani files ka backup", f"✅ Is baar save hui: {res['done']}"]
    if res["failed"]:
        lines.append(f"❌ Nahi ho payi: {res['failed']} (source post delete / bot ko access nahi)")
    lines.append(f"⏳ Baaki: {res['remaining']}")
    if res["wait"]:
        lines.append(f"\nTelegram ne {res['wait']} sec rukne ko kaha. Thodi der baad ▶️ dabao.")
    elif not res["remaining"]:
        lines.append("\n🎉 Saari files backup ho gayi! Nayi files apne-aap (bina duplicate) save hoti rahengi.")
    kb = []
    if res["remaining"]:
        kb.append([{"text": f"▶️ Aur backup karo ({res['remaining']})", "callback_data": "ad:bkold"}])
    kb.append(BACK_ROW)
    return "\n".join(lines), {"inline_keyboard": kb}



def ai_rank(query: str, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not OPENAI_API_KEY or len(rows) < 2:
        return rows
    # Old rows can have NULL keywords/content_text; `or ""` avoids a crash that made every
    # search reply "Database abhi available nahi hai" once OPENAI_API_KEY is set.
    compact = [{"id": r.get("id"), "title": r.get("title"), "keywords": (r.get("keywords") or "")[:500], "content": (r.get("content_text") or "")[:700]} for r in rows[:10]]
    prompt = "Select only the IDs of files that best satisfy the user's request. Return JSON with an ids array of numbers. Never invent IDs. User: " + query + "\nFiles: " + json.dumps(compact, ensure_ascii=False)
    body: Dict[str, Any] = {
        "model": OPENAI_MODEL,
        "messages": [{"role": "system", "content": "You rank a private PDF catalog. Output valid JSON only."}, {"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
        "max_completion_tokens": 300,
    }
    if OPENAI_MODEL.startswith(("gpt-5", "o1", "o3", "o4")):
        # Reasoning models reject temperature=0 and spend tokens on reasoning first.
        body["reasoning_effort"] = "minimal" if OPENAI_MODEL.startswith("gpt-5") else "low"
        body["max_completion_tokens"] = 2000
    else:
        body["temperature"] = 0
    try:
        result = http_json("https://api.openai.com/v1/chat/completions", "POST", body,
                           {"Authorization": f"Bearer {OPENAI_API_KEY}"})
        content = result["choices"][0]["message"]["content"]
        ids = json.loads(content).get("ids", [])
        by_id = {str(r.get("id")): r for r in rows}
        ranked = [by_id[str(i)] for i in ids if str(i) in by_id]
        return ranked or rows
    except Exception:
        log_error("ai_rank")
        return rows


def clean_text(value: str) -> str:
    value = value or ""
    value = re.sub(r"[_./\\-]+", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def normalize(value: str) -> str:
    return clean_text(value).lower()


def message_link(chat: Dict[str, Any], message_id: int) -> Optional[str]:
    username = chat.get("username")
    if username:
        return f"https://t.me/{username}/{message_id}"
    chat_id = str(chat.get("id", ""))
    if chat_id.startswith("-100"):
        return f"https://t.me/c/{chat_id[4:]}/{message_id}"
    return None


def file_info(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for kind in ("document", "video", "audio"):
        obj = message.get(kind)
        if obj:
            return {
                "kind": kind,
                "file_id": obj.get("file_id"),
                "file_unique_id": obj.get("file_unique_id"),
                "file_name": obj.get("file_name") or f"telegram_{obj.get('file_unique_id', 'file')}",
                "mime_type": obj.get("mime_type"),
                "file_size": obj.get("file_size"),
            }
    return None


def extract_pdf_text(file_id: Optional[str], file_name: str = "", file_size: Optional[int] = None) -> str:
    """Best-effort extraction for normal text PDFs; scanned PDFs need OCR."""
    if not file_id or not file_name.lower().endswith(".pdf") or not BOT_TOKEN:
        return ""
    # Telegram's getFile only serves files up to 20 MB, and only the first 8 MB are read
    # below (a cut-off PDF usually can't be parsed), so skip big files instead of
    # spending the webhook's time on a download that cannot work.
    if file_size and file_size > 8 * 1024 * 1024:
        return ""
    started = time.monotonic()
    try:
        file_result = telegram("getFile", {"file_id": file_id}).get("result", {})
        file_path = file_result.get("file_path")
        if not file_path:
            return ""
        url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        with urllib.request.urlopen(url, timeout=15) as response:
            data = response.read(8 * 1024 * 1024)
        reader = PdfReader(io.BytesIO(data))
        chunks = []
        for page in reader.pages[:25]:
            chunks.append(page.extract_text() or "")
            # Keep the webhook fast: stop after ~8 s so Telegram/Vercel don't time out.
            if sum(len(x) for x in chunks) >= 120000 or time.monotonic() - started > 8:
                break
        return clean_text(" ".join(chunks))[:120000]
    except Exception:
        return ""


def build_metadata(message: Dict[str, Any], channel: Dict[str, Any]) -> Dict[str, Any]:
    info = file_info(message) or {}
    caption = message.get("caption", "") or ""
    raw_name = info.get("file_name", "")
    title = clean_text(caption) or clean_text(os.path.splitext(raw_name)[0])
    # The file is NOT read here: name_new_file reads it only when the name is unclear (page by page, max 3).
    content_text = ""
    searchable = " ".join([title, caption, raw_name, channel.get("title", ""), content_text])
    # Captions can contain explicit tags such as #ssc #english; retain them for search.
    keywords = clean_text(searchable)
    return {
        "source_chat_id": channel.get("id"),
        "source_chat_title": channel.get("title", ""),
        "source_chat_username": channel.get("username"),
        "source_message_id": message.get("message_id"),
        "source_link": message_link(channel, message.get("message_id", 0)),
        "telegram_file_id": info.get("file_id"),
        "telegram_file_unique_id": info.get("file_unique_id"),
        "file_name": raw_name,
        "mime_type": info.get("mime_type"),
        "file_size": info.get("file_size"),
        "title": title[:500],
        "caption": caption[:4000],
        "keywords": keywords[:4000],
        "content_text": content_text,
        "language": detect_language(searchable),
        "status": "indexed",
    }


def detect_language(text: str) -> str:
    devanagari = len(re.findall(r"[\u0900-\u097F]", text or ""))
    latin = len(re.findall(r"[A-Za-z]", text or ""))
    if devanagari > 2 and latin >= 3:
        return "mixed"
    if devanagari > latin and devanagari > 2:
        return "hi"
    if latin > 5 and devanagari == 0:
        return "en"
    return "unknown"


def upsert_item(record: Dict[str, Any]) -> Any:
    # The unique constraint on telegram_file_unique_id prevents duplicate indexing.
    clean = {k: v for k, v in record.items() if not str(k).startswith("_")}
    return supabase("books", "POST", clean, "?on_conflict=telegram_file_unique_id")


def safe_markup(markup: Any) -> Any:
    """Telegram refuses the WHOLE message if one button's callback_data is over 64 bytes: such a button becomes a
    harmless no-op (and is logged) so the screen still opens."""
    if not isinstance(markup, dict) or not markup.get("inline_keyboard"):
        return markup
    for row in markup["inline_keyboard"]:
        for b in row:
            cd = b.get("callback_data")
            if isinstance(cd, str) and len(cd.encode("utf-8")) > 64:
                print(f"[filesbull] callback_data over 64 bytes dropped: {cd[:30]}...", flush=True)
                b["callback_data"] = "ad:noop" if cd.startswith("ad:") else "noop"
    return markup


def send_text(chat_id: int, text: str, reply_markup: Optional[Dict[str, Any]] = None,
              parse_mode: Optional[str] = None) -> None:
    reply_markup = safe_markup(reply_markup)
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        telegram("sendMessage", payload)
    except Exception as exc:
        # A formatting slip must never cost the user the message: resend as plain text.
        if parse_mode and "parse" in str(exc).lower():
            payload.pop("parse_mode", None)
            payload["text"] = re.sub(r"</?(b|i|u|code)>", "", text)
            payload["text"] = html.unescape(payload["text"])
            telegram("sendMessage", payload)
        else:
            raise


USER_MENU = [
    {"command": "start", "description": "Bot start karein"},
    {"command": "search", "description": "Book/file search karein"},
    {"command": "help", "description": "Help aur instructions"},
    {"command": "myid", "description": "Apna Telegram ID dekhein"},
]
ADMIN_MENU = USER_MENU + [
    {"command": "admin", "description": "Admin Panel (saare buttons)"},
    {"command": "status", "description": "Admin: ID, role, channels status"},
    {"command": "restart", "description": "Admin: bot restart (webhook dobara set)"},
    {"command": "keys", "description": "Owner: AI keys jodo/hatao"},
    {"command": "requirechannel", "description": "Admin: join channel jodo"},
    {"command": "requiredchannels", "description": "Admin: join channels list"},
    {"command": "removerequired", "description": "Admin: join channel hatao"},
    {"command": "addchannel", "description": "Admin: source channel jodo"},
    {"command": "channels", "description": "Admin: source channels list"},
    {"command": "removechannel", "description": "Admin: source channel hatao"},
    {"command": "setbackupchannel", "description": "Admin: backup channel set karein"},
    {"command": "setlogchat", "description": "Admin: log chat set karein"},
    {"command": "logtest", "description": "Owner: log chat me test message"},
    {"command": "report", "description": "Admin: report"},
    {"command": "rename", "description": "Admin: file ka naam badlo"},
    {"command": "files", "description": "Admin: saari files (10-10 page)"},
    {"command": "logs", "description": "Owner: update log (nayi file, naam badla, kisne li)"},
    {"command": "addadmin", "description": "Owner: sub-admin jodo"},
    {"command": "removeadmin", "description": "Owner: sub-admin hatao"},
    {"command": "cancel", "description": "Admin: chal raha kaam cancel"},
]


def set_admin_menu(user_id: int) -> None:
    try:
        telegram("setMyCommands", {"commands": ADMIN_MENU, "scope": {"type": "chat", "chat_id": user_id}})
    except Exception:
        # Fails with "chat not found" until that admin has pressed Start once.
        log_error(f"admin menu for {user_id}")


def set_command_menu() -> None:
    # Normal users only see their own commands; admin commands are shown to admins only
    # (as /help promises: "Admin commands menu se alag dikhengi").
    telegram("setMyCommands", {"commands": USER_MENU})
    admin_ids = set(ADMIN_USER_IDS)
    try:
        admin_ids.update(int(r["user_id"]) for r in supabase("admins", "GET", query="?select=user_id&limit=100"))
    except Exception:
        log_error("load sub-admins for menu")
    for user_id in admin_ids:
        set_admin_menu(user_id)


def register_channel(channel: Dict[str, Any]) -> Dict[str, Any]:
    record = {
        "chat_id": channel.get("id"),
        "title": channel.get("title") or channel.get("first_name") or "Telegram source",
        "username": channel.get("username"),
        "active": True,
    }
    if not record["chat_id"]:
        raise ValueError("chat id missing")
    rows = supabase("channels", "POST", record, "?on_conflict=chat_id")
    return rows[0] if isinstance(rows, list) and rows else record


def forwarded_source(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    origin = message.get("forward_origin") or {}
    if origin.get("type") == "channel":
        source = dict(origin.get("chat") or {})
        source["message_id"] = origin.get("message_id")
        return source
    if origin.get("type") == "chat" and origin.get("sender_chat"):
        return dict(origin.get("sender_chat") or {})
    return message.get("forward_from_chat") or None


def log_delivery(chat_id: int, row: Dict[str, Any], user: Optional[Dict[str, Any]] = None) -> None:
    user = user or _REQ.get("user") or {"id": chat_id}
    uid = user.get("id") or chat_id
    q = ""
    try:
        ctx = load_ctx(uid)
        q = " ".join(ctx.get("q") or []) or str(ctx.get("raw") or "")
    except Exception:
        pass
    entry = {"user_id": uid, "username": user.get("username") or user.get("first_name"), "query": q[:200] or None,
             "book_id": row.get("id"), "book_title": row.get("title")}
    try:
        supabase("delivery_logs", "POST", entry)
    except Exception:
        log_error("delivery_log")
        try:  # older table without username/query columns
            supabase("delivery_logs", "POST", {k: entry[k] for k in ("user_id", "book_id", "book_title")})
        except Exception:
            log_error("delivery_log basic")
    note_user(user if user.get("first_name") else None, files=1)
    label = user_label({"first_name": user.get("first_name"), "last_name": user.get("last_name"),
                        "username": user.get("username")})
    audit_log(f"📥 File li · {ist_now()}\n👤 {label} ({uid})\n📄 {str(row.get('title') or '')[:80]}" +
              (f"\n🔍 {q[:60]}" if q else ""))


def send_by_file_id(chat_id: int, row: Dict[str, Any]) -> None:
    # Works even if the source post was deleted or the bot lost access to the channel.
    mime = (row.get("mime_type") or "").lower()
    method, field = "sendDocument", "document"
    if mime.startswith("video/"):
        method, field = "sendVideo", "video"
    elif mime.startswith("audio/"):
        method, field = "sendAudio", "audio"
    telegram(method, {"chat_id": chat_id, field: row["telegram_file_id"], "caption": delivery_caption(row)})


# ---- delivery under the CURRENT name (Aarav 7:32 PM Oct 26: "list me to name change h lekin jbb file received hoti h
# to purana name hi rhta"). Caption is always the display name; files ≤20 MB are re-uploaded once under the clean
# file name and that copy's file_id is reused (bot_settings "dlf:<id>" = {f: file_id, n: name}); a rename makes a new copy.

def delivery_caption(row: Dict[str, Any]) -> str:
    slug = ""
    try:
        slug = book_category(row, {} if has_category_col() else catmap())
    except Exception:
        slug = ""
    cat = f"\n🗂️ {cat_label(slug)}" if slug and slug != "other" and known_category(slug) else ""
    return (f"📄 {str(row.get('title') or row.get('file_name') or 'File')}{cat}")[:1000]


def renamed_copy(row: Dict[str, Any]) -> Optional[str]:
    try:
        v = json.loads(get_setting(f"dlf:{row.get('id')}") or "null")
    except Exception:
        return None
    if isinstance(v, dict) and v.get("f") and v.get("n") == str(row.get("title") or ""):
        return str(v["f"])
    return None


def can_rename_file(row: Dict[str, Any]) -> bool:
    mime = str(row.get("mime_type") or "").lower()
    try:
        size = int(row.get("file_size") or 0)
    except (TypeError, ValueError):
        size = 0
    return bool(row.get("telegram_file_id")) and not mime.startswith(("video/", "audio/")) and 0 < size <= TG_FILE_LIMIT


def deliver_renamed(chat_id: int, row: Dict[str, Any], caption: str) -> bool:
    """Send the file with its clean FILE NAME. A renamed copy's file_id works at any size (>20 MB copies come from the
    GitHub Action, see BIG-FILE RENAME); new copies are made here only for ≤20 MB documents.
    False → caller sends a copy with the clean caption."""
    if not row.get("id"):
        return False
    title = str(row.get("title") or "").strip()
    if not title:
        return False
    cached = renamed_copy(row)
    if cached:
        try:
            res = telegram("sendDocument", {"chat_id": chat_id, "document": cached, "caption": caption})
            doc = ((res or {}).get("result") or {}).get("document") or {}
            _REQ["_dl"] = {"how": "renamed_file_id", "file_name": doc.get("file_name"),
                           "message_id": ((res or {}).get("result") or {}).get("message_id")}
            return True
        except Exception:
            log_error("renamed copy send")
        try:
            v = json.loads(get_setting(f"dlf:{row.get('id')}") or "null")
            if isinstance(v, dict) and v.get("c") and v.get("m"):
                res = telegram("copyMessage", {"chat_id": chat_id, "from_chat_id": int(v["c"]), "message_id": int(v["m"]),
                                               "caption": caption})
                _REQ["_dl"] = {"how": "renamed_post_copy", "message_id": ((res or {}).get("result") or {}).get("message_id")}
                return True
        except Exception:
            log_error("renamed post copy")
    if not can_rename_file(row):
        return False
    name = safe_filename(title, str(row.get("file_name") or ""))
    if cached:
        pass
    elif str(row.get("file_name") or "") == name:
        return False                       # the file already carries this name: a copy is enough
    if req_elapsed() > 8.0:
        return False
    data = download_file(row.get("telegram_file_id"), row.get("file_size"))
    if not data:
        return False
    try:
        res = telegram_upload("sendDocument", {"chat_id": chat_id, "caption": caption}, "document", name, data)
        doc = ((res or {}).get("result") or {}).get("document") or {}
        _REQ["_dl"] = {"how": "renamed_upload", "file_name": doc.get("file_name"), "message_id": ((res or {}).get("result") or {}).get("message_id")}
        if not (res or {}).get("ok", True) or not ((res or {}).get("result") or {}).get("message_id"):
            return False
        if doc.get("file_id"):
            try:
                set_setting(f"dlf:{row['id']}", json.dumps({"f": doc["file_id"], "n": title}, ensure_ascii=False))
            except Exception:
                log_error("renamed copy save")
        return True
    except Exception:
        log_error("renamed upload")
        return False


def deliver_result(chat_id: int, row: Dict[str, Any], user: Optional[Dict[str, Any]] = None) -> None:
    caption = delivery_caption(row)
    try:
        if deliver_renamed(chat_id, row, caption):
            log_delivery(chat_id, row, user)
            return
    except Exception:
        log_error("deliver_renamed")
    try:
        # copyMessage works for both public and private source channels when permitted; caption = current name.
        telegram("copyMessage", {
            "chat_id": chat_id,
            "from_chat_id": row["source_chat_id"],
            "message_id": row["source_message_id"],
            "caption": caption,
        })
        log_delivery(chat_id, row, user)
        return
    except Exception:
        log_error("copyMessage")
    try:
        if row.get("backup_chat_id") and row.get("backup_message_id"):
            telegram("copyMessage", {"chat_id": chat_id, "from_chat_id": row["backup_chat_id"], "message_id": row["backup_message_id"],
                                     "caption": caption})
            log_delivery(chat_id, row, user)
            return
    except Exception:
        log_error("backup copyMessage")
    try:
        if row.get("telegram_file_id"):
            send_by_file_id(chat_id, row)
            log_delivery(chat_id, row, user)
            return
    except Exception:
        log_error("send by file_id")
    try:
        # Public source channel the bot is not a member of: a plain forward (old caption) is the last way.
        if row.get("source_chat_id") and row.get("source_message_id"):
            telegram("forwardMessage", {"chat_id": chat_id, "from_chat_id": row["source_chat_id"],
                                        "message_id": row["source_message_id"]})
            log_delivery(chat_id, row, user)
            return
    except Exception:
        log_error("forwardMessage")
    link = row.get("source_link")
    if link:
        send_text(chat_id, f"File direct send nahi ho payi. Source channel link:\n{link}")
    else:
        send_text(chat_id, "File direct send nahi ho payi aur is message ka public link available nahi hai.")


def handle_private_message(message: Dict[str, Any]) -> None:
    user = message.get("from", {})
    chat_id = message.get("chat", {}).get("id")
    text = (message.get("text") or "").strip()
    if not chat_id:
        return

    user_id = user.get("id")
    note_user(user)
    command = command_name(text)
    if command == "/myid":
        role = {"owner": "Owner ✅", "admin": "Sub-admin ✅", "user": "Normal user"}[role_of(user_id)]
        send_text(chat_id, f"Aapka Telegram ID: {user_id}\nRole: {role}")
        return
    if command and command not in USER_COMMANDS and not is_subadmin(user_id):
        send_text(chat_id, f"⛔ Ye command sirf admin ke liye hai.\nAapka Telegram ID: {user_id}")
        return

    if not command and message.get("chat", {}).get("type") == "private" and (
            KEY_RE.search(text) or (is_subadmin(user_id) and MISTRAL_KEY_RE.match(text) and re.search(r"\d", text)
                                    and re.search(r"[A-Za-z]", text) and not get_pending(user_id))):
        # An API key must never be searched, sent to the AI or written to logs.
        if is_subadmin(user_id):
            handle_admin_input(message, "addkey", None)   # owner: saved; sub-admin: deleted + refused
            return
        try:
            telegram("deleteMessage", {"chat_id": chat_id, "message_id": message.get("message_id")})
        except Exception:
            pass
        send_text(chat_id, "🔒 API key chat me mat bhejo — maine message hata diya.")
        return

    if is_subadmin(user_id) and message.get("chat", {}).get("type") == "private" and not command:
        # Answer to an admin-panel prompt (reply to the prompt, or the next message after a button).
        action = prompt_action(message)
        extra = None
        pending = get_pending(user_id)
        if pending and (action is None or action == pending.get("a")):
            action, extra = pending.get("a"), pending.get("x")
        if action in PROMPTS:
            handle_admin_input(message, action, extra)
            return

    if gate_blocks(user_id, chat_id):
        return

    info = file_info(message)
    if is_admin(user.get("id")) and forwarded_source(message):
        source = forwarded_source(message)
        try:
            register_channel(source)
            if text == "/requirechannel" or text.startswith("/requirechannel"):
                invite = f"https://t.me/{source['username']}" if source.get("username") else None
                supabase("required_channels", "POST", {"chat_id": source["id"], "title": source.get("title", "Required channel"), "username": source.get("username"), "invite_link": invite, "active": True}, "?on_conflict=chat_id")
                send_text(chat_id, f"Required join channel add ho gaya: {source.get('title', 'channel')}")
                return
            if info:
                record = build_metadata(message, source)
                # A forwarded message has a new message_id in the bot chat. Delivery
                # must use the original message_id from the source channel.
                if source.get("message_id"):
                    record["source_message_id"] = source["message_id"]
                    record["source_link"] = message_link(source, source["message_id"])
                index_file(record, chat_id, message.get("message_id"), str(source.get("title") or "forward"))
                send_text(chat_id, f"Channel auto-add aur file index ho gayi: {source.get('title', 'source')}\nFile: {clean_text(info.get('file_name', 'file'))}")
            else:
                send_text(chat_id, f"✅ Source channel jud gaya: {source.get('title', 'source')}\nAb is channel ki nayi files index hongi.\n"
                                   "Isse join channel (naye users ke liye zaroori) bhi banana hai to neeche dabao.",
                          {"inline_keyboard": [[{"text": "➕ Ise Join Channel bhi banao", "callback_data": f"ad:mkreq:{source['id']}"}], BACK_ROW]})
        except Exception as exc:
            send_text(chat_id, f"Forwarded channel add nahi hua: {str(exc)[:250]}")
        return
    if info and is_admin(user.get("id")):
        # Files sent directly by an admin are indexed under the bot chat as a local source.
        pseudo_channel = {"id": chat_id, "title": "Admin uploads", "username": None}
        record = build_metadata(message, pseudo_channel)
        record["source_link"] = None
        index_file(record, chat_id, message.get("message_id"), "Admin upload")
        send_text(chat_id, f"Indexed: {record['title']}\nLanguage: {record['language']}\nAb admin is file ko search se dhoondh sakte hain.")
        return

    if command == "/start":
        if is_subadmin(user_id):
            clear_pending(user_id)
            send_text(chat_id, welcome_text(user, admin=True), admin_start_menu(), "HTML")
            return
        send_text(chat_id, welcome_text(user), user_menu(), "HTML")
        return
    if command == "/help":
        extra = "\n\n⚙️ Admin: /admin se Admin Panel khulega." if is_subadmin(user_id) else ""
        send_text(chat_id, USER_HELP + extra, None, "HTML")
        return
    is_private = message.get("chat", {}).get("type") == "private"
    if command_name(text) == "/search":
        query = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""
    else:
        query = text
    if not query:
        send_text(chat_id, SEARCH_PROMPT, None, "HTML")
        return
    if is_private and not command and is_subadmin(user_id):
        # Admin free text: a whitelisted admin action (preview + ✅ Accept) or a search.
        try:
            routed = admin_text(chat_id, user, query)
        except InputError as exc:
            send_text(chat_id, f"❌ Nahi hua: {exc}", {"inline_keyboard": [BACK_ROW]})
            return
        except Exception:
            log_error("admin_text")
            routed = query
        if routed is None:
            return
        query = routed
    if is_private and not command:
        # Understand first (free), search second, AI only if needed.
        try:
            smart_reply(chat_id, user, query)
        except Exception:
            log_error("smart_reply")
            send_text(chat_id, "Database abhi available nahi hai. Admin se setup check karne ko kahen.")
        return

    try:
        results = search_books(query)
        note_user(None, searches=1)
    except Exception:
        log_error("search")
        send_text(chat_id, "Database abhi available nahi hai. Admin se setup check karne ko kahen.")
        return
    if not results:
        send_text(chat_id, f"😕 <b>\"{esc(query[:60])}\" ke liye koi file nahi mili</b>\n\n"
                           "Dusra naam, chhota keyword ya sahi spelling try karo.\n"
                           "Jaise: <i>lucent</i>, <i>ssc gd gk</i>, <i>polity</i>", None, "HTML")
        return
    buttons = []
    for index, row in enumerate(results):
        label = f"📄 {str(row.get('title') or 'Untitled')[:45]}"
        buttons.append([{ "text": label, "callback_data": f"book:{row['id']}" }])
    send_text(chat_id, f"🔎 <b>{len(results)} file mili</b> — jo chahiye us par tap karo 👇", {"inline_keyboard": buttons}, "HTML")


USER_COMMANDS = {"/start", "/help", "/search", "/myid"}

SEARCH_PROMPT = ("🔍 <b>Kya dhoondhna hai?</b>\n\n"
                 "Book / PDF ka naam, subject ya exam likhkar bhejo.\n"
                 "Jaise: <i>lucent gk</i>, <i>ssc gd</i>, <i>polity notes</i>")
USER_HELP = ("❓ <b>Bot kaise use kare</b>\n\n"
             "1️⃣ Book / PDF ka naam, subject ya exam likho\n"
             "2️⃣ List me se file par tap karo\n"
             "3️⃣ File seedha chat me aa jayegi 📥\n\n"
             "/search &lt;naam&gt; — search\n"
             "/myid — aapka Telegram ID")


def command_name(text: str) -> str:
    # "/search@FilesBullBot lucent" -> "/search"
    if not text.startswith("/"):
        return ""
    return text.split()[0].split("@")[0].lower()


def handle_admin_command(message: Dict[str, Any]) -> None:
    text = (message.get("text") or "").strip()
    chat_id = message.get("chat", {}).get("id")
    user_id = message.get("from", {}).get("id")
    if not is_subadmin(user_id):
        send_text(chat_id, f"⛔ Ye admin command hai.\nAapka Telegram ID: {user_id}")
        return
    command = command_name(text)
    arg = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""
    clear_pending(user_id) if get_pending(user_id) else None

    def fail(exc: Exception) -> None:
        if not isinstance(exc, InputError):
            log_error(f"admin command {command}")
        send_text(chat_id, f"❌ Nahi hua: {str(exc)[:300]}")

    if command in {"/admin", "/panel", "/menu"}:
        send_text(chat_id, ADMIN_PANEL_TEXT, admin_menu(user_id), "HTML")
    elif command == "/cancel":
        send_text(chat_id, "✅ Cancel ho gaya.", admin_menu(user_id))
    elif command in {"/status", "/debug"}:
        send_text(chat_id, status_text(user_id), status_markup())
    elif command == "/restart":
        send_text(chat_id, restart_text(), status_markup())
    elif command == "/logs":
        if not is_owner(user_id):
            send_text(chat_id, "Ye sirf owner ke liye hai.")
        else:
            n = int(arg) if arg.strip().isdigit() else 15
            for part in logs_text(n):
                send_text(chat_id, part)
    elif command == "/userbot":
        if not is_owner(user_id):
            send_text(chat_id, "Ye sirf owner ke liye hai.")
        else:
            send_text(chat_id, *ub_view(), parse_mode="HTML")
    elif command == "/bigfix":
        if not is_owner(user_id):
            send_text(chat_id, "Ye sirf owner ke liye hai.")
        else:
            bigfix_view(chat_id, user_id)
    elif command == "/aitest":
        send_text(chat_id, ai_test_text(), {"inline_keyboard": [[{"text": "🔁 Dobara test", "callback_data": "ad:aitest"}], BACK_ROW]}, "HTML")
    elif command == "/ainaming":
        # Hidden owner-only emergency switch (AI naming is always on otherwise).
        if not is_owner(user_id):
            send_text(chat_id, "Ye sirf owner ke liye hai.")
        elif arg.lower() in {"off", "band"}:
            set_setting("ai_naming", "off")
            send_text(chat_id, "⛔ AI naming emergency band. Chalu: /ainaming on")
        else:
            set_setting("ai_naming", "on")
            send_text(chat_id, "✅ AI naming chalu (apne-aap har nayi file).")
    elif command in {"/keys", "/aikeys"}:
        if is_owner(user_id):
            send_text(chat_id, *keys_view())
        else:
            send_text(chat_id, "🔑 AI Keys sirf owner badal sakta hai.")
    elif command == "/addadmin":
        if not is_owner(user_id):
            send_text(chat_id, "Sirf primary owner sub-admin add kar sakta hai.")
        elif not arg:
            ask_input(chat_id, user_id, "addadmin")
        else:
            handle_admin_input(dict(message, text=arg), "addadmin", None)
    elif command == "/removeadmin":
        if not is_owner(user_id):
            send_text(chat_id, "Sirf primary owner sub-admin remove kar sakta hai.")
        elif not arg.isdigit():
            send_text(chat_id, *admins_view(user_id))
        else:
            send_text(chat_id, *confirm_view(f"rmadm:{arg}"))
    elif command == "/requirechannel":
        if not arg and not forwarded_source(message):
            ask_input(chat_id, user_id, "addreq")
        else:
            handle_admin_input(dict(message, text=arg), "addreq", None)
    elif command == "/requiredchannels":
        send_text(chat_id, *required_list_view())
    elif command in {"/removerequired", "/removerequirechannel"}:
        if not arg:
            send_text(chat_id, *required_list_view())
        else:
            try:
                ref = parse_chat_ref(arg)
                cid = ref if isinstance(ref, int) else telegram("getChat", {"chat_id": ref}).get("result", {}).get("id")
                send_text(chat_id, *confirm_view(f"rmreq:{cid}"))
            except Exception as exc:
                fail(InputError(friendly_tg_error(exc)) if not isinstance(exc, InputError) else exc)
    elif command == "/addchannel":
        if not arg and not forwarded_source(message):
            ask_input(chat_id, user_id, "addsrc")
        else:
            handle_admin_input(dict(message, text=arg), "addsrc", None)
    elif command == "/channels":
        send_text(chat_id, *source_list_view())
    elif command == "/removechannel":
        if not arg:
            send_text(chat_id, *source_list_view())
        else:
            try:
                ref = parse_chat_ref(arg)
                cid = ref if isinstance(ref, int) else telegram("getChat", {"chat_id": ref}).get("result", {}).get("id")
                send_text(chat_id, *confirm_view(f"rmsrc:{cid}"))
            except Exception as exc:
                fail(InputError(friendly_tg_error(exc)) if not isinstance(exc, InputError) else exc)
    elif command == "/setbackupchannel":
        if not arg:
            send_text(chat_id, *setting_view("backup_chat_id", "💾 Backup Channel", "ad:setbackup", "ad:backupoff", "Nayi files ki copy is channel me."))
        else:
            handle_admin_input(dict(message, text=arg), "setbackup", None)
    elif command == "/logtest":
        if not is_owner(user_id):
            send_text(chat_id, "Ye sirf owner ke liye hai.")
        else:
            send_text(chat_id, log_test_text(log_test()), {"inline_keyboard": [[{"text": "📜 Purani updates Log me bhejo", "callback_data": "ad:logfill"}]]}
                      if message.get("chat", {}).get("type") == "private" else None)
    elif command == "/setlogchat" and not arg and message.get("chat", {}).get("type") in {"group", "supergroup", "channel"}:
        # Sent inside the group itself → this group becomes the Log Chat (no forwarding needed for groups).
        try:
            telegram("sendMessage", {"chat_id": chat_id, "text": "✅ Ye group ab filesbull ka 📝 Log Chat hai. Saari updates yahan aayengi."})
            set_setting("log_chat_id", str(chat_id))
            _log_chat_mark(True)
        except Exception as exc:
            log_error("setlogchat in group")
            try:
                telegram("sendMessage", {"chat_id": user_id, "text": "❌ Group Log Chat set nahi hua: " + log_chat_reason(exc)})
            except Exception:
                pass
    elif command == "/setlogchat":
        if not arg:
            send_text(chat_id, *log_setting_view())
        else:
            handle_admin_input(dict(message, text=arg), "setlog", None)
    elif command in {"/report", "/stats"}:
        send_text(chat_id, report_text())
    elif command in {"/user", "/users"}:
        if arg and re.fullmatch(r"\d{3,}", arg):
            send_text(chat_id, user_detail(int(arg)), {"inline_keyboard": [[{"text": "👥 Users", "callback_data": "ad:users"}], BACK_ROW]})
        else:
            send_text(chat_id, users_view(), {"inline_keyboard": [BACK_ROW]})
    elif command == "/backupold":
        send_text(chat_id, *backup_old_view())
    elif command in {"/files", "/allfiles"}:
        page = int(arg) if arg.isdigit() else 1
        send_text(chat_id, *files_view(page))
    elif command == "/rename":
        parts = text.split(maxsplit=2)
        if len(parts) >= 3 and parts[1].isdigit():
            try:
                do_rename(chat_id, parts[1], parts[2], user_id)
            except Exception as exc:
                fail(exc)
        else:
            ask_input(chat_id, user_id, "rename")
    else:
        send_text(chat_id, "Ye command nahi pehchani. Admin Panel:", admin_menu(user_id))


def handle_channel_post(message: Dict[str, Any]) -> None:
    channel = message.get("chat", {})
    try:
        if userbot_post_in_backup(message):
            return          # the userbot's forward/re-upload: saved through its own report (source = original channel)
        allowed = supabase("channels", "GET", query=f"?chat_id=eq.{channel.get('id')}&active=eq.true&select=chat_id")
        if not allowed:
            return
        if file_info(message):
            record = build_metadata(message, channel)
            index_file(record, channel.get("id"), message.get("message_id"), str(channel.get("title") or "channel"))
    except Exception:
        log_error("channel_post")


def userbot_post_in_backup(message: Dict[str, Any]) -> bool:
    """A post in the Backup channel that the userbot made (a forward from another channel, or any post while a
    userbot run is going) is never indexed from the channel_post — no loop, no double save."""
    try:
        backup = get_setting("backup_chat_id")
        if not backup or str((message.get("chat") or {}).get("id")) != str(backup):
            return False
        origin = message.get("forward_origin") or {}
        if origin.get("type") == "channel" or message.get("forward_from_chat"):
            return True
        st = json.loads(get_setting("ub:state") or "{}")
        if int(message.get("message_id") or 0) in (st.get("recent_backup") or []):
            return True      # protected-content re-upload delivered after its report
        return bool(st.get("run")) and time.time() - float(st.get("run_t") or 0) < 1800
    except Exception:
        return False


def handle_group_file(message: Dict[str, Any]) -> None:
    chat = message.get("chat", {})
    if chat.get("type") not in {"group", "supergroup"} or not file_info(message):
        return
    if not is_admin(message.get("from", {}).get("id")):
        return
    try:
        register_channel(chat)
        index_file(build_metadata(message, chat), chat.get("id"), message.get("message_id"), str(chat.get("title") or "group"))
    except Exception:
        log_error("group_file")


# ---- every inline button the bot makes, as patterns. A tap whose data matches none of these came from an old
# message (a button that no longer exists) and gets "🔄 Ye button purana hai…" instead of silence.
CALLBACK_PATTERNS = [
    r"verify_join", r"noop", r"more", r"user_help", r"user_categories", r"user_list", r"user_search",
    r"book:\d+", r"cat:[A-Za-z0-9_\-]+(:\d+)?", r"g:[es]:[^:]{0,58}",
    r"admin_add_required", r"admin_required_list", r"admin_add_source", r"admin_report",
    r"ad:(panel|usermenu|addreq|addsrc|rename|reqlist|srclist|backup|bkold|users|log|setbackup|setlog|backupoff|logoff|"
    r"admins|addadm|report|status|help|restart|keys|keyadd|keychk|noop|smode|aitest|ainame|ainame:on|ainame:off|bigfix|"
    r"fixn|logfill|logfill:go|logtest|rtlive|rtord|rtmod)",
    r"ad:files(:\d+)?", r"ad:fl:[0-9a-f]{4,16}:\d+", r"ad:f:\d+(:\d+)?(:[0-9a-f]{0,16})?", r"ad:fs:\d+",
    r"ad:fixn:\d+", r"ad:cmdq:[0-9a-f]{4,16}", r"ad:op:[nyspca]:[0-9a-f]{4,16}(:\d+)?",
    r"ad:rn:\d+", r"ad:mkreq:-?\d+", r"ad:keyadd:[a-z]+", r"ad:rtl:[a-z]", r"ad:rtord:[a-z]*", r"ad:rtos:[a-z]*",
    r"ad:rtm:[a-z]", r"ad:rtms:[a-z]:(a|\d+)",
    r"ad:(y:)?(rmreq:-?\d+|rmsrc:-?\d+|rmadm:\d+|keyrm:[0-9a-f]{4,32}|fdel:\d+|backupoff|logoff|ublogout)",
    r"ad:(ub|ublogin|ubrun|ubpause)",
    r"ad:y:(rtl:[a-z]|rtos:[a-z]*|rtms:[a-z]:(a|\d+))",
]
_CB_RE = re.compile("(?:" + ")|(?:".join(CALLBACK_PATTERNS) + ")")
STALE_ADMIN = "🔄 Ye button purana hai, /admin dobara kholo"
STALE_USER = "🔄 Ye button purana hai, /start dobara karo"
CONFIRM_TTL = 3600      # an ✅ Accept older than this (message date) asks to start again
OWNER_ONLY_CMDS = ("keys", "key", "rt", "bigfix", "addadm", "rmadm:", "y:keyrm:", "y:rmadm:", "y:rt", "ub", "y:ublogout")
# toast shown at once for buttons whose screen needs a few lookups; quick screens just stop the spinner
CB_TOASTS = [("ad:status", "⏳ Status check ho raha hai..."), ("ad:keychk", "🔄 Keys check ho rahi hain..."),
             ("ad:aitest", "⏳ AI test ho raha hai..."), ("ad:report", "⏳"), ("ad:users", "⏳"), ("ad:bkold", "💾 Backup chal raha hai..."),
             ("ad:restart", "🔄 Restart ho raha hai..."), ("ad:fixn", "🤖 AI naam soch raha hai..."),
             ("ad:bigfix", "🔁 Badi files dekh raha hu..."), ("ad:logfill", "📜 Log Chat me bhej raha hu..."),
             ("ad:fs:", "📤 File bhej raha hu..."), ("book:", "📤 File bhej raha hu..."), ("ad:y:", "⏳"), ("ad:op:y:", "⏳"),
             ("ad:keys", "⏳"), ("ad:rtm:", "⏳"), ("ad:ub", "⏳"), ("ad:ubrun", "🔄 Check shuru kar raha hu..."),
             ("ad:ublogin", "🔑 Login link bana raha hu...")]


def callback_known(data: str) -> bool:
    return bool(data) and len(data.encode("utf-8")) <= 64 and bool(_CB_RE.fullmatch(data))


def cb_toast(data: str) -> str:
    for prefix, text in CB_TOASTS:
        if prefix.endswith(":") and data.startswith(prefix) or data == prefix or data.startswith(prefix + ":"):
            return text
    return ""


def cb_answer(query: Optional[Dict[str, Any]], text: str = "", alert: bool = False) -> None:
    """answerCallbackQuery once per tap (Telegram shows only the first). Never raises."""
    if not query or not query.get("id"):
        return
    done = _REQ.setdefault("_cb_done", set())
    if query["id"] in done:
        return
    done.add(query["id"])
    payload: Dict[str, Any] = {"callback_query_id": query["id"]}
    if text:
        payload["text"] = text[:200]
    if alert:
        payload["show_alert"] = True
    try:
        telegram("answerCallbackQuery", payload)
    except Exception as exc:
        if "query is too old" not in str(exc).lower() and "query id is invalid" not in str(exc).lower():
            log_error("answerCallbackQuery")


def handle_callback(query: Dict[str, Any]) -> None:
    """Every tap: answer FIRST (spinner stops at once; old/foreign buttons get an alert), then do the work and edit
    the message. Any error after that shows a short ❌ line instead of a hanging/silent button."""
    data = str(query.get("data") or "")
    user_id = (query.get("from") or {}).get("id")
    chat_id = ((query.get("message") or {}).get("chat") or {}).get("id")
    is_adm_btn = data.startswith(("ad:", "admin_"))
    if not callback_known(data):
        cb_answer(query, STALE_ADMIN if (is_adm_btn or is_admin(user_id)) else STALE_USER, True)
        note_user(query.get("from"))
        return
    if is_adm_btn:
        if not is_subadmin(user_id):
            cb_answer(query, f"⛔ Ye button sirf admin ke liye hai.\nAapka Telegram ID: {user_id}", True)
            return
        cmd = data[3:] if data.startswith("ad:") else ""
        if cmd.startswith(OWNER_ONLY_CMDS) and not is_owner(user_id):
            cb_answer(query, "🔑 Ye button sirf owner ke liye hai.", True)
            return
        msg_date = int((query.get("message") or {}).get("date") or 0)
        if cmd.startswith(("y:", "op:y:")) and msg_date and time.time() - msg_date > CONFIRM_TTL:
            cb_answer(query, "⌛ Ye confirm purana ho gaya (1 ghante se zyada). Dobara karo, phir ✅ Accept dabao.", True)
            return
    cb_answer(query, "⏳ Join check ho raha hai..." if data == "verify_join" else cb_toast(data))
    note_user(query.get("from"))
    try:
        _handle_callback_body(query, data, chat_id, user_id)
    except Exception as exc:
        log_error(f"callback {data[:30]}")
        if not chat_id:
            return
        back = {"inline_keyboard": [BACK_ROW]} if is_adm_btn else None
        why = str(exc) if isinstance(exc, InputError) else "kuch gadbad hui"
        try:
            show(chat_id, f"❌ Ye button abhi nahi chala ({why[:120]}).\nDobara dabao — phir bhi na chale to /admin kholo."
                 if is_adm_btn else "❌ Abhi nahi hua, dobara dabao.", back, query)
        except Exception:
            log_error("callback error reply")


def _handle_callback_body(query: Dict[str, Any], data: str, chat_id: Any, user_id: Any) -> None:
    if data == "verify_join":
        admin = is_subadmin(user_id)
        reqs = [] if admin else required_channels()
        missing = missing_required(user_id, reqs) if reqs else []
        if not missing:
            if reqs:
                mark_verified(user_id)
                note_user(None, verified=True)
            # The gate message turns into a thank-you (buttons gone), then the welcome + menu.
            show(chat_id, "✅ <b>Shukriya!</b> Ab aap bot use kar sakte ho 🎉", None, query, "HTML")
            if chat_id:
                send_text(chat_id, welcome_text(query.get("from"), admin=admin),
                          admin_start_menu() if admin else user_menu(), "HTML")
        else:
            names = ", ".join(str(c.get("title") or "Channel") for c in missing)
            alert = "❌ Abhi join nahi kiya! Pehle channel join karo, phir Verify dabao."
            alert += f"\n\nBaaki: {names}"
            # Already answered immediately; Telegram allows only one callback answer.
            # Put the failure in the message itself rather than a second (ignored) alert.
            show(chat_id, esc(alert[:160]) + "\n\n" + gate_text(missing, was_verified(user_id), len(reqs)),
                 join_markup(missing, len(reqs)), query, "HTML")
        return
    if (data.startswith("ad:") or data.startswith("admin_")) and chat_id:
        if not is_subadmin(user_id):
            send_text(chat_id, f"⛔ Ye button sirf admin ke liye hai.\nAapka Telegram ID: {user_id}")
            return
        try:
            handle_admin_callback(query, data, chat_id, user_id)
        except InputError as exc:
            send_text(chat_id, f"❌ Nahi hua: {exc}", {"inline_keyboard": [BACK_ROW]})
        return
    if chat_id and gate_blocks(user_id, chat_id):
        return
    if data == "user_help":
        send_text(chat_id, USER_HELP, None, "HTML")
        return
    if data == "noop":
        return
    if data == "user_categories":
        text, kb = categories_view()
        show(chat_id, text, kb, query, "HTML")
        return
    if data.startswith("cat:"):
        parts = data.split(":")
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
        text, kb = category_list_view(parts[1] if len(parts) > 1 else "other", page)
        show(chat_id, text, kb, query)
        return
    if data == "user_list":
        rows = supabase("books", "GET", query="?select=id,title,source_chat_title&status=eq.indexed&order=title&limit=30")
        send_text(chat_id, "📚 <b>PDF List</b>\n\n" + ("\n".join(f"📄 {esc(r.get('title'))}" for r in rows) if rows else "Abhi koi file nahi hai.") +
                  "\n\n<i>Jo chahiye uska naam likhkar bhejo 🔍</i>", None, "HTML")
        return
    if data == "user_search":
        send_text(chat_id, SEARCH_PROMPT, None, "HTML")
        return
    if (data == "more" or data.startswith("g:")) and chat_id:
        handle_guided_callback(query, data, chat_id)
        return
    if not data.startswith("book:"):
        return
    book_id = data.split(":", 1)[1]
    rows = supabase("books", "GET", query=f"?id=eq.{urllib.parse.quote(book_id)}&select=*&limit=1")
    if rows and str(rows[0].get("status") or "indexed") == "removed":
        rows = []
    if rows and chat_id:
        deliver_result(chat_id, rows[0], query.get("from"))
    elif chat_id:
        send_text(chat_id, "😕 Ye file ab bot me nahi hai. Naam likhkar dobara search karo 🔍")


# ================================================================ v6: admin AI commands, 📂 All Files, 🤖 AI naming
# Admins can type what they want ("polity ka naam politics kr do", "sari ek baar me upload kro").
# Cheap local rules first; the AI is asked only when the rules can't tell, and it may only answer
# with one of ADMIN_ACTIONS + params — it never runs anything itself. Every change is shown first
# and applied only on ✅ Accept (Aarav, 5:08/5:11 PM Oct 26).

ADMIN_AI_DAILY = int(os.environ.get("ADMIN_AI_DAILY", "60") or 60)
AI_NAME_DAILY = int(os.environ.get("AI_NAME_DAILY", "600") or 600)
FILES_PAGE = 10
FIX_BATCH = 20
_REQ_T0 = [time.monotonic()]

ADMIN_ACTIONS = {"search", "rename", "bulk_rename", "delete_file", "backup_old", "list_files", "users", "report",
                 "status", "restart", "add_channel", "remove_channel", "list_channels", "add_admin", "ai_keys",
                 "ai_naming", "fix_names", "panel", "help", "log_backfill", "categories", "move_category", "search_mode",
                 "ai_test", "log_test", "logs", "unknown"}
OWNER_ONLY_ACTIONS = {"add_admin", "ai_keys", "logs"}
SEARCH_PREFIXES = ("?", "？", "🔍", "search ", "dhoondo ", "dhundo ", "khojo ", "find ")
CMD_VERBS = {"kr", "kro", "krdo", "krde", "krna", "krne", "krke", "karo", "kardo", "karde", "kar", "karna", "karne",
             "karke", "kijiye", "kijie", "badlo", "badal", "badl", "badalna", "change", "rename", "hatao", "hata",
             "hatado", "hatana", "mitao", "mita", "delete", "remove", "set", "jodo", "jod", "add", "upload", "backup",
             "restart", "chalu", "band", "on", "off", "naam", "name", "rakho", "rakh", "theek", "thik", "sahi",
             "bana", "banao", "bnao", "bnado", "banado",
             # Aarav 6:14 PM Oct 26: "Abhi tkk ki sari updates log chat me dal do" was missed — these words mean "a job"
             "dal", "daal", "dalo", "daalo", "daldo", "daaldo", "dalna", "bhej", "bhejo", "bhejdo", "bhejna", "send",
             "dikhao", "dikha", "dikhado", "hatao", "jodo", "lagao", "log", "logs", "update", "updates", "record",
             "backup", "users", "report", "status", "restart", "rename", "naam", "settings", "setting"}
NAME_WORDS = r"(?:name|naam|nam|title|named)"
DO_TAIL = r"(?:kr|kar|rakh|krdo|kardo|karo|kro|krde|karde|krna|karna|likh|bana)\s*(?:do|de|dena|dijiye|dijie|o)?"


def req_elapsed() -> float:
    return time.monotonic() - _REQ_T0[0]


def search_mode_label(user_id: Optional[int]) -> str:
    on = False
    if user_id:
        try:
            on = get_setting(f"admsearch:{user_id}") == "1"
        except Exception:
            on = False
    return "🔍 Search Mode: ON" if on else "🔍 Search Mode: OFF"


def ai_naming_on() -> bool:
    try:
        return (get_setting("ai_naming") or "on") != "off"
    except Exception:
        return False


def human_size(n: Any) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{max(1, n // 1024)} KB"


# ---- pending admin operation: preview → ✅ Accept / ❌ Deny

def set_op(user_id: int, op: Dict[str, Any]) -> str:
    token = hashlib.sha256(f"{user_id}:{time.time()}:{os.urandom(4).hex()}".encode()).hexdigest()[:8]
    set_setting(f"adop:{user_id}", json.dumps(dict(op, tok=token, ts=int(time.time())), ensure_ascii=False))
    return token


def get_op(user_id: int, token: str) -> Optional[Dict[str, Any]]:
    raw = get_setting(f"adop:{user_id}")
    try:
        op = json.loads(raw) if raw else None
    except Exception:
        return None
    if not isinstance(op, dict) or op.get("tok") != token or time.time() - float(op.get("ts", 0)) > 3600:
        return None
    return op


def clear_op(user_id: int) -> None:
    try:
        supabase("bot_settings", "DELETE", query=f"?key=eq.{urllib.parse.quote(f'adop:{user_id}')}")
    except Exception:
        log_error("clear_op")


def accept_kb(token: str) -> Dict[str, Any]:
    return {"inline_keyboard": [[{"text": "✅ Accept", "callback_data": f"ad:op:y:{token}"},
                                 {"text": "❌ Deny", "callback_data": f"ad:op:n:{token}"}]]}


def ask_confirm(chat_id: int, user_id: int, text: str, op: Dict[str, Any], query: Optional[Dict[str, Any]] = None) -> None:
    show(chat_id, text, accept_kb(set_op(user_id, op)), query)


# ---- understanding an admin's message (local rules first)

def _strip_q(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().strip("\"'“”‘’`")).strip()


def _find_clean(s: str) -> str:
    s = _strip_q(s)
    s = re.sub(r"^(?:is|us|ye|yeh|jo|wo|vo|file|files|book|pdf)\s+", "", s, flags=re.I)
    s = re.sub(r"\s+(?:wali|wala|wale|file|files|book|pdf|ko|ki|ka|ke|vali|vala)$", "", s, flags=re.I)
    s = re.sub(r"\s+(?:wali|wala|wale|file|files|book|pdf|vali|vala)$", "", s, flags=re.I)
    return _strip_q(s)


def local_admin_intent(text: str) -> Optional[Dict[str, Any]]:
    """Free rules for the common admin commands. None = not sure (maybe search, maybe AI).
    List / delete carry "filters" (subject, type, source, size, date, count, words) — parse_filters()."""
    t = _strip_q(text)
    low = t.lower()
    words = set(re.findall(r"[a-z0-9]+|[\u0900-\u097F]+", low))
    # bulk rename: "sab files me Hindi ko हिंदी kr do" / "sab naamo me X ki jagah Y"
    m = re.match(r"^(?:sab|sabhi|sari|saari|all|har)\s+(?:files?|naam\w*|names?|books?|pdfs?)?\s*(?:ke\s+naam\w*\s+)?(?:me|mein|mai|m)\s+"
                 r"(?P<find>.+?)\s+(?:ko|ki\s+jagah|ke\s+badle|se)\s+(?P<to>.+?)\s+" + DO_TAIL + r"\s*$", t, re.I)
    if m:
        return {"action": "bulk_rename", "find": _strip_q(m.group("find")), "to": _strip_q(m.group("to"))}
    # move category: "Parmar sir polity pdf ko history category me daal do", "physics wali file ki category science kr do"
    m = re.match(r"^(?P<find>.+?)\s+(?:ki|ka|ke)\s+(?:category|catagory|catgory|categry|cat|section|folder)\s+(?P<to>.+?)\s+"
                 r"(?:kr|kar|kro|karo|krdo|kardo|rakh|rakho|rkh|rkho|bana|banao|set|badal|badlo|change)\w*\s*(?:do|de|dena|dijiye|dijie)?\s*$", t, re.I) or \
        re.match(r"^(?P<find>.+?)\s+(?:ko|kô)\s+(?P<to>[^\s]+(?:\s+[^\s]+)?)\s+(?:category|catagory|catgory|categry|section|folder)\s*(?:me|mein|mai|m)?\s+"
                 r"(?:daal|dal|dalo|daalo|daldo|daaldo|bhej|bhejo|bhejdo|move|shift|rakh|rakho|rkh|rkho|kr|kro|kar|karo|krdo|kardo|daal\s+do|dal\s+do)\w*(?:\s*(?:do|de|dena|dijiye))?\s*$", t, re.I) or \
        re.match(r"^(?:move|shift)\s+(?P<find>.+?)\s+(?:to|in|into|me|mein)\s+(?P<to>.+?)(?:\s+(?:category|section|folder))?\s*$", t, re.I)
    if m:
        return {"action": "move_category", "find": _find_clean(m.group("find")), "to": _strip_q(m.group("to"))}
    # rename: "polity ka name politics kr do", "polity ka name change krne politics kr do", "rename polity to politics"
    m = re.match(r"^(?:rename\s+)?(?P<find>.+?)\s+(?:ka|ki|ke|kaa)\s+" + NAME_WORDS + r"\s+"
                 r"(?:(?:change|badal|badl|chng|chnge)\w*\s+(?:(?:kr|kar|krke|karke|krne|karne|ke)\w*\s+)?)?"
                 r"(?P<to>.+?)\s+" + DO_TAIL + r"\s*$", t, re.I)
    if m and _strip_q(m.group("to")).lower() not in {"change", "badal", "theek", "thik", "sahi"}:
        return {"action": "rename", "find": _find_clean(m.group("find")), "to": _strip_q(m.group("to"))}
    m = re.match(r"^rename\s+(?P<find>.+?)\s+(?:to|->|→|=)\s+(?P<to>.+)$", t, re.I) or \
        re.match(r"^(?P<find>.+?)\s+(?:ka|ki|ke)\s+" + NAME_WORDS + r"\s+(?P<to>.+?)\s+(?:se\s+)?(?:badlo|badal\s*do|change\s*(?:kr|kar)\w*(?:\s*do)?)\s*$", t, re.I)
    if m:
        return {"action": "rename", "find": _find_clean(m.group("find")), "to": _strip_q(m.group("to"))}
    # AI naming / fix old names
    if ("ai" in words and words & {"naming", "naam", "name", "nam"}) and words & {"on", "off", "chalu", "band", "start", "stop", "bnd"}:
        return {"action": "ai_naming", "on": not (words & {"off", "band", "stop", "bnd"})}
    if words & {"naam", "name", "names", "naamo", "nam"} and words & {"theek", "thik", "sahi", "fix", "clean", "saaf"} and \
            words & {"purane", "purani", "old", "sab", "sabhi", "sare", "saare", "sari", "saari", "all", "ai"}:
        return {"action": "fix_names"}
    # 📜 old updates → Log Chat ("Abhi tkk ki sari updates log chat me dal do", "purani updates bhejo")
    if is_log_backfill(low, words):
        return {"action": "log_backfill"}
    # 🔑 AI keys / live provider / backup order (before "backup" — "backup ai ka order badlo" is about AI keys)
    provs = [p for p in ("groq", "gemini", "mistral", "openrouter", "nvidia") if p in words or (p == "openrouter" and "open router" in low)]
    if (words & {"key", "keys", "api", "apikey"} and (provs or words & {"ai", "add", "jodo", "dikhao", "list", "backup", "live", "nayi", "new"})) or \
            (provs and words & {"live", "backup", "order", "key", "keys", "model", "switch"}) or \
            ("ai" in words and words & {"live", "provider", "providers", "backup"} and
             words & {"order", "badlo", "change", "kr", "kro", "karo", "set", "dikhao", "switch", "bdlo"}):
        intent = {"action": "ai_keys"}
        if provs and words & {"live"} and not words & {"backup", "order"}:
            intent["live"] = provs[0]
        return intent
    # 🤖 AI test ("ai test kro", "ai chal raha hai kya")
    if "aitest" in words or ("ai" in words and (words & {"test", "check", "tst", "chek"} or re.search(r"\bai\s+(?:chal|kaam|work)", low))):
        return {"action": "ai_test"}
    # 📝 Log Chat test ("log test kro", "log chat check kro", "log channel chal raha hai?")
    if words & {"log", "logs", "logchat"} and (words & {"test", "check", "chek", "tst"} or re.search(r"\blog\s+(?:chat|channel)\s+(?:chal|kaam)", low)) \
            and not words & {"set", "lagao", "badlo"}:
        return {"action": "log_test"}
    # 🔍 Search Mode on / off
    if words & {"search", "serch", "sarch", "seach", "srch", "searh"} and words & {"mode", "mod", "moad", "mood"}:
        on = True if words & {"on", "chalu", "start", "kholo"} else (False if words & {"off", "band", "bnd", "stop"} else None)
        return {"action": "search_mode", "on": on}
    # backup of old files ("Sari ek baar me upload kro", "sab files backup kro")
    if words & {"backup", "bkp", "upload", "uplod", "uplode"}:
        if words & {"channel", "chat"} and words & {"set", "badlo", "change", "lagao", "jodo"}:
            return {"action": "panel_button", "button": "setbackup"}
        return {"action": "backup_old"}
    # 📂 file lists with filters / delete with filters ("Mujhe only polity ki sari pdf files dikhao", "aaj ki files", "last 5 files")
    del_words = {"delete", "hatao", "hata", "hatado", "remove", "mitao", "mita", "nikalo", "hide", "chupao", "chhupao", "chupa",
                 "chhupa", "chupado", "chhupado", "hatana", "deleet", "dlt", "htao", "hta"}
    job_words = {"rename", "badlo", "change", "upload", "backup", "admin", "key", "keys", "restart"}
    has_noun = bool(words & FILE_NOUNS) or bool(re.search(r"फाइल|पीडीएफ|किताब", low))
    if words & del_words and not words & {"channel", "channels", "chanel", "admin", "key", "keys", "group"}:
        f = parse_filters(t, known_sources() if has_noun else None)
        find = re.sub(r"\b(?:delete|hatao|hata|hatado|htao|hta|remove|mitao|mita|nikalo|hide|chupao|chhupao|chupa|chhupa|chupado|chhupado|"
                      r"hatana|dlt|kr|kro|karo|kar|do|de|dena|file|files|wali|wala|vali|ko|ki|ka|ye|yeh|is|please|plz|bhai|sari|saari|"
                      r"sab|sabhi|all|only|sirf|bas)\b", " ", t, flags=re.I)
        intent = {"action": "delete_file", "find": _strip_q(find)}
        sel = {k: v for k, v in f.items() if k in ("subject", "type", "source", "min_size", "max_size", "date_from", "date_to",
                                                   "date_label", "limit")}
        if sel:
            intent["filters"] = dict(sel, **({"query": f["query"]} if f.get("query") else {}))
        return intent
    if words & {"category", "categories", "catagory", "catgory", "categry", "catagories", "श्रेणी"} and \
            (words & SHOW_VERBS or len(words) <= 3) and not has_noun and not parse_filters(t).get("subject"):
        return {"action": "categories"}
    if has_noun and not words & job_words:
        f = parse_filters(t, known_sources())
        listy = bool(words & SHOW_VERBS) or bool(words & ALL_WORDS) or \
            any(f.get(k) for k in ("source", "min_size", "max_size", "date_from", "limit")) or \
            (f.get("subject") and words & {"category", "categories", "catagory", "section"})
        if listy:
            f.pop("only", None)
            return {"action": "list_files", "filters": f}
    # channels
    chat_ref = re.search(r"(@[A-Za-z0-9_]{4,}|https?://t\.me/\S+|t\.me/\S+|-100\d{6,})", t)
    if words & {"channel", "channels", "chanel", "group"}:
        kind = "join" if words & {"join", "required", "zaroori", "jaruri"} else ("source" if words & {"source", "src"} else None)
        if words & {"hatao", "hata", "remove", "delete", "hatado", "nikalo", "mitao"}:
            return {"action": "remove_channel", "kind": kind}
        if words & {"add", "jodo", "jod", "lagao", "dalo", "daalo", "joro", "bana", "banao", "bnao"}:
            return {"action": "add_channel", "kind": kind, "target": chat_ref.group(1) if chat_ref else None}
        if words & {"list", "dikhao", "dikha", "batao", "show", "kitne", "btao"}:
            return {"action": "list_channels", "kind": kind}
    if "admin" in words and words & {"add", "jodo", "bana", "banao", "jod", "bnao", "bnado"}:
        return {"action": "add_admin"}
    if words & {"restart", "reboot", "reset"} or re.search(r"\bbot\s+(?:ko\s+)?(?:chalu|start)\b", low):
        return {"action": "restart"}
    if re.search(r"\b(?:all|sari|saari|sab|sabhi|puri|poori)\s+(?:ki\s+)?(?:files?|pdfs?|books?)\b", low) and \
            (len(words) <= 3 or words & {"list", "dikhao", "dikha", "batao", "show"}) or \
            re.search(r"\b(?:files?|pdfs?)\s+(?:ki\s+)?list\b|\ball\s+files\b", low):
        return {"action": "list_files", "filters": {}}
    if words & {"log"} and words & {"chat", "channel"} and words & {"set", "lagao", "jodo", "badlo"}:
        return {"action": "panel_button", "button": "setlog"}
    # 📜 recent log lines (/logs): "logs dikhao", "last 20 logs", "aaj ki updates dikhao"
    if "logs" in words or (words & {"updates", "update", "log"} and words & (SHOW_VERBS | {"last", "recent", "latest"}) and
                           not words & {"user", "users", "kitne"}):
        m = re.search(r"\b(\d{1,3})\b", low)
        return {"action": "logs", "n": int(m.group(1)) if m and 0 < int(m.group(1)) <= 100 else None}
    if words & {"user", "users", "yuzar", "yuzer", "usr", "members"} and (len(words) <= 5 or words & {"dikhao", "batao", "kitne", "list", "btao"}) \
            or "kitne log" in low:
        return {"action": "users"}
    if words & {"report", "stats", "statistics", "hisab", "hisaab", "riport", "stat"} and len(words) <= 6:
        return {"action": "report"}
    if words & {"status", "debug", "health"} and len(words) <= 6 or \
            re.search(r"\bbot\s+(?:theek|thik|sahi|shi)?\s*(?:se\s+)?(?:chal|kaam)\s*(?:raha|rha|rhi|kr)", low):
        return {"action": "status"}
    if low in {"admin", "admin panel", "panel", "menu"}:
        return {"action": "panel"}
    if low in {"help", "admin help", "madad"} or (words & {"help", "madad"} and len(words) <= 3):
        return {"action": "help"}
    if words & {"rename", "naam", "name", "nam"} and words & {"badlo", "badalna", "badalni", "change", "rename", "badal", "chng", "badlna",
                                                          "badlni", "bdlo", "bdlna", "bdalna", "badalwana"} and len(words) <= 6:
        return {"action": "rename"}          # no file / new name given → ask which file
    return None


def looks_like_command(text: str) -> bool:
    """Admin text that might be a command the rules missed (then the AI decides)."""
    toks = set(re.findall(r"[a-z0-9]+|[\u0900-\u097F]+", (text or "").lower()))
    return bool(toks & CMD_VERBS)


ADMIN_AI_SYSTEM = (
    "You route a message from the ADMIN of a Telegram bot that stores study PDF files. "
    "Decide if it is an admin command or a file search. Reply ONLY with JSON: "
    '{"action":"...","find":null,"to":null,"kind":null,"target":null,"on":null,"query":null,"ask":null,'
    '"filters":{"subject":null,"type":null,"source":null,"min_size":null,"max_size":null,"date_from":null,"date_to":null,'
    '"limit":null,"query":null}}. '
    "action is one of: search (admin is looking for a file to get it; query = search words), "
    "list_files (SHOW / LIST files, optionally filtered — 'X ki sari files dikhao', 'aaj ki files', 'last 5 files'), "
    "rename (change ONE file's name: find = words of the current name, to = new name), bulk_rename (replace text in MANY "
    "file names: find, to), delete_file (remove/hide a file: find = its name words; or filters), move_category (put a file "
    "in another category: find = file name words, to = category), categories (show the category list), backup_old "
    "(copy/upload all old files to the backup channel), users, report, status, restart, add_channel (kind join|source, "
    "target @name/link or null), remove_channel (kind), list_channels (kind), add_admin, ai_keys (AI API keys, live/backup "
    "AI provider), ai_test (test if AI works), ai_naming (on true/false), fix_names (let AI clean old file names), "
    "log_backfill (send details of ALL files added so far to the log chat), log_test (test the log chat), logs (show recent "
    "log lines), search_mode (on true/false), panel, help, unknown. "
    "filters (for list_files / delete_file): subject = study subject in English (polity, history, geography, economics, "
    "science, gk, current affairs, maths, reasoning, hindi, english, computer, pyq, mock — राजव्यवस्था/samvidhan/constitution "
    "= polity, itihas = history, bhugol = geography, ganit = maths), type = pdf|video|audio|image|doc, source = source channel "
    "name, min_size/max_size in MB (number), date_from/date_to as YYYY-MM-DD (today is {today}, IST), limit = how many newest "
    "files, query = other title words. Words like only/sirf/bas/sari/saari mean 'just these'. Unused filters = null. "
    "Message may be Hindi/Hinglish with typos. If unclear, action unknown and ask = ONE short Hinglish question. "
    "Never invent file names; copy find/to from the message.")


def admin_ai_budget_ok(user_id: int) -> bool:
    if not ai_provider():
        return False
    raw = get_setting(f"aiday_admin:{user_id}") or ""
    day, _, n = raw.partition(":")
    return not (day == ist_day() and n.isdigit() and int(n) >= ADMIN_AI_DAILY)


def _count_day(key: str) -> None:
    try:
        raw = get_setting(key) or ""
        day, _, n = raw.partition(":")
        set_setting(key, f"{ist_day()}:{int(n) + 1 if day == ist_day() and n.isdigit() else 1}")
    except Exception:
        log_error("count_day")


def ai_admin_intent(user_id: int, text: str) -> Optional[Dict[str, Any]]:
    if not admin_ai_budget_ok(user_id):
        return None
    _count_day(f"aiday_admin:{user_id}")
    system = ADMIN_AI_SYSTEM.replace("{today}", time.strftime("%Y-%m-%d", time.gmtime(time.time() + IST_OFFSET)))
    content = ai_complete(system, text[:400], max_tokens=220, cap=8)
    if not content:
        return None
    return validate_admin_ai(content, text)


def validate_admin_ai(content: str, text: str) -> Optional[Dict[str, Any]]:
    """Parse + whitelist the AI's JSON; the locally read filters fill every gap (Aarav 7:25 PM: a subject /
    type / source word in the text is applied even if the AI said "list everything")."""
    try:
        data = json.loads(content[content.find("{"): content.rfind("}") + 1])
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    action = str(data.get("action") or "")
    action = {"list_all": "list_files", "list": "list_files", "show_files": "list_files", "files": "list_files",
              "hide_file": "delete_file", "hide": "delete_file", "delete": "delete_file", "category": "move_category",
              "set_category": "move_category", "aitest": "ai_test", "keys": "ai_keys"}.get(action, action)
    if action not in ADMIN_ACTIONS:
        return None   # anything outside the whitelist is ignored

    def s(v: Any) -> Optional[str]:
        return _strip_q(str(v))[:200] if isinstance(v, (str, int)) and not isinstance(v, bool) and str(v).strip() and \
            str(v).strip().lower() not in {"null", "none"} else None
    on = data.get("on")
    out = {"action": action, "find": s(data.get("find")), "to": s(data.get("to")),
           "kind": s(data.get("kind")) if s(data.get("kind")) in {"join", "source"} else None,
           "target": s(data.get("target")), "on": on if isinstance(on, bool) else None,
           "query": s(data.get("query")), "ask": s(data.get("ask")), "ai": True}
    if action in {"list_files", "delete_file", "search"}:
        local_f = parse_filters(text, known_sources())
        ai_f = clean_ai_filters(data.get("filters"))
        f = merge_filters(ai_f, local_f)
        if action == "search" and any(f.get(k) for k in ("subject", "source", "min_size", "max_size", "date_from", "limit")) and \
                re.search(r"\b(?:dikhao|dikha|list|show|batao|btao|sari|saari|sab|all|only|sirf)\b", text.lower()):
            action = out["action"] = "list_files"     # "polity ki sari files dikhao" is a list, not a single search
        if action != "search":
            f.pop("only", None)
            out["filters"] = f
    return out


BIGFIX_RE = re.compile(r"\b(badi|bdi|bari|bado|big|20\s*mb\s*se\s*(?:badi|bdi|jyada|zyada))\b.{0,20}\b(files?|pdfs?)\b.{0,25}\b(naam|nam|name)\b")


def admin_text(chat_id: int, user: Dict[str, Any], text: str) -> Optional[str]:
    """Admin free text. Returns None when handled here, else the text to search."""
    user_id = int(user.get("id"))
    raw = (text or "").strip()
    low = raw.lower()
    intent = local_admin_intent(raw)
    if intent and intent.get("action") == "search_mode":      # "search mode on/off" — never itself a search
        run_admin_action(chat_id, user, intent, raw)
        return None
    if BIGFIX_RE.search(low) and is_owner(user_id) and get_setting(f"admsearch:{user_id}") != "1":
        bigfix_view(chat_id, user_id)        # "badi files ka naam theek karo"
        return None
    for p in SEARCH_PREFIXES:
        if low.startswith(p):
            rest = raw[len(p):].strip()
            if not rest:
                send_text(chat_id, SEARCH_PROMPT, None, "HTML")
                return None
            return rest
    if get_setting(f"admsearch:{user_id}") == "1":
        return raw                           # Search Mode ON: everything is a search, except "search mode off"
    if intent is None:
        if not looks_like_command(raw):
            return raw                       # plain words ("lucent", "ssc gd gk hindi") → search, no AI
        intent = ai_admin_intent(user_id, raw)
        if intent is None or intent.get("action") == "unknown":
            # Sounds like a job but neither the rules nor the AI could tell → ask once (never the user off-topic reply).
            ask_cmd_or_search(chat_id, user_id, raw, (intent or {}).get("ask"))
            return None
    if intent["action"] == "search":
        return intent.get("query") or raw
    run_admin_action(chat_id, user, intent, raw)
    return None


def run_admin_action(chat_id: int, user: Dict[str, Any], intent: Dict[str, Any], raw: str,
                     query: Optional[Dict[str, Any]] = None) -> None:
    user_id = int(user.get("id"))
    a = intent.get("action")
    if a not in ADMIN_ACTIONS and a != "panel_button":
        return
    if a in OWNER_ONLY_ACTIONS and not is_owner(user_id):
        send_text(chat_id, "⛔ Ye kaam sirf owner kar sakta hai.", {"inline_keyboard": [BACK_ROW]})
        return
    find, to = intent.get("find") or "", intent.get("to") or ""
    if a == "rename":
        if not find or not to:
            ask_input(chat_id, user_id, "rename", prefix="✏️ Samajh gaya, naam badalna hai.\n")
            return
        rename_flow(chat_id, user_id, find, to)
    elif a == "bulk_rename":
        if not find or not to:
            send_text(chat_id, "✏️ Kya badalna hai? Aise likho: \"sab files me Hindi ko हिंदी kr do\"", {"inline_keyboard": [BACK_ROW]})
            return
        bulk_rename_flow(chat_id, user_id, find, to)
    elif a == "delete_file":
        flt = intent.get("filters") or {}
        if has_filters(flt) and (not find or not find_books(find, 2) or any(flt.get(k) for k in ("subject", "type", "date_from", "source", "min_size", "max_size", "limit"))):
            delete_filtered_flow(chat_id, user_id, flt, find)
        else:
            delete_flow(chat_id, user_id, find)
    elif a == "backup_old":
        backup_flow(chat_id, user_id)
    elif a == "list_files":
        send_text(chat_id, *filtered_files_view(intent.get("filters") or {}, 1))
    elif a == "categories":
        text, kb = categories_view()
        send_text(chat_id, text, kb or {"inline_keyboard": [BACK_ROW]}, "HTML")
    elif a == "move_category":
        if not to:
            send_text(chat_id, "🗂️ Kis category me daalni hai? Aise likho: \"Parmar sir polity pdf ko polity category me daal do\"",
                      {"inline_keyboard": [[{"text": "🗂️ Categories", "callback_data": "user_categories"}], BACK_ROW]})
            return
        move_category_flow(chat_id, user_id, find, to)
    elif a == "search_mode":
        on = intent.get("on")
        cur = get_setting(f"admsearch:{user_id}") == "1"
        if on is None:
            send_text(chat_id, ("🔍 Search Mode abhi ON hai — har msg seedha search hota hai." if cur else
                                "🔍 Search Mode abhi OFF hai — bot samajhta hai msg kaam hai ya search."),
                      {"inline_keyboard": [[{"text": "🔍 Search Mode OFF karo" if cur else "🔍 Search Mode ON karo", "callback_data": "ad:smode"}], BACK_ROW]})
            return
        set_setting(f"admsearch:{user_id}", "1" if on else "0")
        send_text(chat_id, "🔍 Search Mode ON — ab aapka har msg seedha search hoga.\n(Bolke kaam ke liye ise OFF karo: \"search mode off\")"
                  if on else "🔍 Search Mode OFF — ab bot samjhega aapka msg kaam hai ya search 🤖", {"inline_keyboard": [BACK_ROW]})
    elif a == "ai_test":
        send_text(chat_id, ai_test_text(), {"inline_keyboard": [[{"text": "🔁 Dobara test", "callback_data": "ad:aitest"}], BACK_ROW]}, "HTML")
    elif a == "log_test":
        send_text(chat_id, log_test_text(log_test()), {"inline_keyboard": [[{"text": "📜 Purani updates Log me bhejo", "callback_data": "ad:logfill"}], BACK_ROW]})
    elif a == "logs":
        n = intent.get("n") if isinstance(intent.get("n"), int) else 15
        for part in logs_text(n):
            send_text(chat_id, part)
    elif a == "users":
        send_text(chat_id, users_view(), {"inline_keyboard": [[{"text": "🔄 Refresh", "callback_data": "ad:users"}], BACK_ROW]})
    elif a == "report":
        send_text(chat_id, report_text(), {"inline_keyboard": [BACK_ROW]})
    elif a == "status":
        send_text(chat_id, status_text(user_id), status_markup())
    elif a == "restart":
        ask_confirm(chat_id, user_id, "🔄 Bot restart karu?\n\nWebhook aur commands menu dobara set honge (files/settings pe koi asar nahi).",
                    {"kind": "restart"})
    elif a == "add_channel":
        kind = intent.get("kind")
        target = intent.get("target")
        if not kind:
            send_text(chat_id, "➕ Kaunsa channel jodna hai?\n\n📢 Join = naye users ko join karna padega\n📥 Source = wahan ki files bot me aayengi",
                      {"inline_keyboard": [[{"text": "📢 Join Channel Jodo", "callback_data": "ad:addreq"}],
                                           [{"text": "📥 Source Channel Jodo", "callback_data": "ad:addsrc"}], BACK_ROW]})
        elif not target:
            ask_input(chat_id, user_id, "addreq" if kind == "join" else "addsrc")
        else:
            label = "📢 Join channel" if kind == "join" else "📥 Source channel"
            ask_confirm(chat_id, user_id, f"➕ {label} jodna hai: {target}\n\nPakka?", {"kind": "addch", "ch": kind, "target": target})
    elif a in {"remove_channel", "list_channels"}:
        kind = intent.get("kind")
        note = "Hatane ke liye list me ❌ dabao (pehle ✅ Accept puchega).\n\n" if a == "remove_channel" else ""
        if kind == "join":
            text, kb = required_list_view()
            send_text(chat_id, note + text, kb)
        elif kind == "source":
            text, kb = source_list_view()
            send_text(chat_id, note + text, kb)
        else:
            send_text(chat_id, note + "Kaunse channels?", {"inline_keyboard": [
                [{"text": "📋 Join Channels", "callback_data": "ad:reqlist"}],
                [{"text": "📋 Source Channels", "callback_data": "ad:srclist"}], BACK_ROW]})
    elif a == "add_admin":
        ask_input(chat_id, user_id, "addadmin")
    elif a == "ai_keys":
        live = intent.get("live")
        if live in PROVIDERS and is_owner(user_id):
            handle_router_callback({}, f"rtl:{PROVIDERS[live]['c']}", False, chat_id, user_id)   # one ✅ Accept / ❌ Deny
            return
        send_text(chat_id, *keys_view())
    elif a == "ai_naming":
        on = intent.get("on")
        if on is None:
            send_text(chat_id, *ainame_view())
            return
        ask_confirm(chat_id, user_id, ("🤖 AI Naming chalu karu?\nNayi files ka naam AI caption + file naam padhke rakhega."
                                       if on else "🤖 AI Naming band karu?\nNayi files ka naam purane tareeke se (caption / file naam) banega."),
                    {"kind": "ainame", "on": bool(on)})
    elif a == "fix_names":
        fixnames_view(chat_id, user_id, None)
    elif a == "panel_button" and intent.get("button") in {"setbackup", "setlog"}:
        ask_input(chat_id, user_id, intent["button"])
    elif a == "panel":
        send_text(chat_id, ADMIN_PANEL_TEXT, admin_menu(user_id), "HTML")
    elif a == "help":
        send_text(chat_id, ADMIN_HELP, {"inline_keyboard": [BACK_ROW]})
    elif a == "log_backfill":
        logfill_preview(chat_id, user_id, query)
    else:   # unknown
        ask_cmd_or_search(chat_id, user_id, raw, intent.get("ask"))


# ================================================================ v12: admin filters (Aarav 7:25 PM Oct 26)
# "Mujhe only polity ki sari pdf files dikhao" showed all 27 files: the list action dropped every filter.
# parse_filters() reads subject / type / source / size / date / count / words from the admin's text (no AI needed);
# the AI router returns the same {action, filters{...}} and is validated + merged with the local reading.

FILTER_SUBJECTS = {
    "polity": ["polity", "polty", "pality", "politics", "rajniti", "rajneeti", "rajvyavastha", "rajvyavstha", "rajvyavasta",
               "राजव्यवस्था", "राजनीति", "constitution", "samvidhan", "sanvidhan", "samvidhaan", "संविधान", "civics",
               "laxmikanth", "indian polity"],
    "history": ["history", "histroy", "histry", "itihas", "itihaas", "इतिहास", "indian history"],
    "geography": ["geography", "geo", "bhugol", "bhoogol", "भूगोल", "geogrphy"],
    "economics": ["economics", "economy", "arthvyavastha", "arthshastra", "अर्थव्यवस्था", "अर्थशास्त्र"],
    "science": ["science", "vigyan", "विज्ञान", "general science"],
    "static_gk": ["gk", "gs", "g.k", "g.s", "static gk", "general knowledge", "general studies", "samanya gyan",
                  "सामान्य ज्ञान", "सामान्य अध्ययन", "gk gs"],
    "current_affairs": ["ca", "current affairs", "current affair", "currentaffairs", "करंट अफेयर्स", "samsamayiki",
                        "सामयिकी", "karent affairs"],
    "maths": ["maths", "math", "mathematics", "ganit", "गणित", "quant", "arithmetic"],
    "reasoning": ["reasoning", "resoning", "reasning", "tark", "तर्कशक्ति", "रीजनिंग"],
    "hindi": ["hindi", "हिंदी", "हिन्दी", "hindi vyakaran"],
    "english": ["english", "angrezi", "angreji", "अंग्रेजी", "english grammar"],
    "computer": ["computer", "कंप्यूटर", "computer awareness"],
    "pyq": ["pyq", "previous year", "previous year paper", "purane paper", "solved paper"],
    "mock": ["mock", "mock test", "practice set", "test series"],
}
_FSUB_WORD = {w: s for s, ws in FILTER_SUBJECTS.items() for w in ws}
FILE_TYPES = {
    "pdf": ["pdf", "pdfs", "पीडीएफ"], "video": ["video", "videos", "vdo", "vedio", "वीडियो", "lecture", "lectures"],
    "audio": ["audio", "audios", "mp3", "ऑडियो"], "image": ["image", "images", "photo", "photos", "pic", "pics", "jpg", "png", "फोटो"],
    "doc": ["doc", "docx", "word", "ppt", "pptx", "excel"],
}
_FTYPE_WORD = {w: t for t, ws in FILE_TYPES.items() for w in ws}
TYPE_LABEL = {"pdf": "PDF", "video": "Video", "audio": "Audio", "image": "Photo", "doc": "Doc"}
FILE_NOUNS = {"file", "files", "fil", "fils", "filez", "fiels", "pdf", "pdfs", "book", "books", "kitab", "kitabe", "kitaben",
              "notes", "video", "videos", "audio", "audios", "image", "images", "photo", "photos", "documents", "docs",
              "फाइल", "फाइलें", "पीडीएफ", "किताब", "किताबें"}
SHOW_VERBS = {"dikhao", "dikha", "dikhado", "dikhaao", "dikhana", "dikhaye", "dikhaiye", "dikhayo", "dikhau", "dekhni",
              "dekhna", "dekhao", "dekhe", "batao", "bata", "btao", "bta", "btado", "list", "show", "display", "kitni",
              "kitne", "count", "dikhai", "dikhaa", "dikao", "dikho", "दिखाओ", "बताओ"}
ALL_WORDS = {"sari", "saari", "sare", "saare", "sab", "sabhi", "all", "only", "sirf", "bas", "puri", "poori", "total", "srf", "sirif"}
_FILTER_STOP = set("""mujhe muje mujhko mereko mere meri mera hame hume humko hamko please plz pls bhai bro yaar sir ji
    ki ka ke ko kya me mein mai m se sy aur or bhi wali wala wale vali vala vale jo jisme jinme jiska hai hain h he ho hoga
    the thi tha karo kro kr kar krdo kardo krde karde do de dena dijiye dijie chahiye chaiye chahie lao la laao bhejo bhej
    send ab abhi tak tk tkk naya nayi naye new latest recent last pichli pichle akhri aakhri first pehli top hi to bhai
    category categories catagory catgory section folder subject topic type size mb kb gb din days day hafte week mahine
    month ghante hours se badi bade bada chhoti choti chhote kam zyada jyada upar niche neeche above below more less than
    channel chanel group source src aayi aai aaye aye hui hue added add upload uploaded the a an of in for from is are
    in wo vo ye yeh is us un unki uski iski inki apni apne apna mere sirf only sari saari sare saare sab sabhi all bas puri
    poori total list show display dikhao dikha dikhado batao btao kitni kitne count aaj aj kal parso today yesterday this
    hafte की का के को से में मे और भी वाली वाला वाले सारी सारे सब सभी सिर्फ केवल मुझे मेरे दिखाओ दिखा बताओ दो दे भेजो
    फाइल फाइलें फ़ाइल फ़ाइलें पीडीएफ है हैं""".split()) | FILE_NOUNS | SHOW_VERBS | ALL_WORDS
_DATE_UNITS = {"din": 1, "dino": 1, "dinon": 1, "day": 1, "days": 1, "d": 1, "hafte": 7, "hafta": 7, "hafton": 7, "week": 7,
               "weeks": 7, "mahine": 30, "mahina": 30, "mahino": 30, "month": 30, "months": 30}


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _ist_midnight(days_ago: int = 0) -> float:
    now = time.time() + IST_OFFSET
    return (now - now % 86400) - IST_OFFSET - days_ago * 86400


def _size_bytes(num: str, unit: str) -> int:
    return int(float(num) * {"kb": 1024, "mb": 1048576, "gb": 1073741824}[unit.lower()])


def subject_from_word(w: str, fuzzy: bool = True) -> str:
    w = (w or "").strip().lower()
    if not w:
        return ""
    if w in _FSUB_WORD:
        return _FSUB_WORD[w]
    if " " in w or len(w) >= 4:
        s = norm_category(w)
        if s and s != "other" and known_category(s) and (w in CAT_SYNONYMS or w == cat_label(s).lower() or
                                                          w == s.replace("_", " ")):
            return s
    if fuzzy and len(w) >= 5 and re.fullmatch(r"[a-z]+", w):
        import difflib
        close = difflib.get_close_matches(w, [k for k in _FSUB_WORD if len(k) >= 5 and " " not in k], n=1, cutoff=0.8)
        if close and not difflib.get_close_matches(w, list(_FILTER_STOP), n=1, cutoff=0.86):
            return _FSUB_WORD[close[0]]
    return ""


def parse_filters(text: str, sources: Optional[List[str]] = None) -> Dict[str, Any]:
    """Filters in an admin's words. Returns only the keys found (subject, type, source, min_size, max_size,
    date_from, date_to, date_label, limit, query, only)."""
    low = " " + re.sub(r"\s+", " ", (text or "").lower()) + " "
    f: Dict[str, Any] = {}
    used: List[str] = []

    def cut(m: Any) -> None:
        used.append(m.group(0))

    # size: "10 MB se badi", "5mb se chhoti", "above 10 mb", "> 2mb"
    big = r"(?:jyada|zyada|jada|zada|jiyada|badi|bade|bada|bdi|bde|upar|uper|above|more|bigger|greater|large|larger|plus|\+)"
    small = r"(?:kam|chhoti|choti|chhote|chote|chhota|chota|niche|neeche|nichay|below|less|smaller|under)"
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(kb|mb|gb)\s*(?:se|than|sy|ke|ki|ka)?\s*(" + big + "|" + small + r")\b", low):
        key = "min_size" if re.fullmatch(big, m.group(3)) else "max_size"
        f[key] = _size_bytes(m.group(1), m.group(2)); cut(m)
    for m in re.finditer(r"(above|over|more than|greater than|bigger than|>=?|below|under|less than|smaller than|<=?)\s*"
                         r"(\d+(?:\.\d+)?)\s*(kb|mb|gb)\b", low):
        key = "min_size" if re.match(r"above|over|more|greater|bigger|>", m.group(1)) else "max_size"
        f[key] = _size_bytes(m.group(2), m.group(3)); cut(m)
    # date: aaj / kal / parso / last 7 din / is hafte / is mahine
    m = re.search(r"\b(?:last|pichle|pichhle|picchle|pichhe|past|previous|purane|pichli)?\s*(\d{1,3})\s*"
                  r"(din|dino|dinon|days?|d|hafte|hafta|hafton|weeks?|mahine|mahina|mahino|months?)\b", low)
    if m and (re.search(r"last|pichl|pichhl|picchl|past|previous|purane", m.group(0)) or
              re.search(re.escape(m.group(0).strip()) + r"\s*(?:ki|ke|ka|me|mein|se|wali|wale|purani)?\b", low)):
        days = int(m.group(1)) * _DATE_UNITS.get(m.group(2), 1)
        if 0 < days <= 3650:
            f["date_from"] = _iso(time.time() - days * 86400)
            f["date_label"] = f"last {m.group(1)} {'din' if _DATE_UNITS.get(m.group(2), 1) == 1 else ('hafte' if _DATE_UNITS.get(m.group(2)) == 7 else 'mahine')}"
            cut(m)
    if "date_from" not in f:
        if re.search(r"(?:\b(?:aaj|aj|aajki|today|todays)\b|आज)(?!\s*(?:tak|tk|tkk|takk|तक))", low):
            f["date_from"], f["date_label"] = _iso(_ist_midnight(0)), "aaj"
        elif re.search(r"\b(?:kal|kall|yesterday)\b|कल", low):
            f["date_from"], f["date_to"], f["date_label"] = _iso(_ist_midnight(1)), _iso(_ist_midnight(0)), "kal"
        elif re.search(r"\b(?:parso|parson|परसों)\b", low):
            f["date_from"], f["date_to"], f["date_label"] = _iso(_ist_midnight(2)), _iso(_ist_midnight(1)), "parso"
        elif re.search(r"\b(?:is|iss|this)\s+(?:hafte|hafta|week)\b", low):
            f["date_from"], f["date_label"] = _iso(time.time() - 7 * 86400), "is hafte"
        elif re.search(r"\b(?:is|iss|this)\s+(?:mahine|mahina|month)\b", low):
            f["date_from"], f["date_label"] = _iso(time.time() - 30 * 86400), "is mahine"
    # count: "last 5", "latest 10 files", "5 nayi files"
    m = re.search(r"\b(?:last|latest|recent|nayi|naye|nai|new|top|pichli|pichhli|akhri|aakhri|aakhiri|akhiri)\s+(\d{1,3})\b"
                  r"(?!\s*(?:din|dino|days?|d\b|hafte|weeks?|mahine|months?|mb|kb|gb))", low) or \
        re.search(r"\b(\d{1,3})\s+(?:nayi|naye|nai|latest|new|recent|last|aakhri|akhri)?\s*(?:files?|pdfs?|books?|videos?)\b", low)
    if m and 0 < int(m.group(1)) <= 500:
        f["limit"] = int(m.group(1)); cut(m)
    toks = re.findall(r"[a-z0-9.]+|[\u0900-\u097F]+", low)
    # source: "GK channel ki files", "channel Test se", "@mychannel", or a known source title in the text
    src = ""
    m = re.search(r"(@[a-z0-9_]{4,})", low)
    if m:
        src = m.group(1)
    if not src:
        m = re.search(r"(?:^|\s)((?:[\w\u0900-\u097F.&-]+\s+){0,3}?[\w\u0900-\u097F.&-]+)\s+(?:channel|chanel|chnl|group)\s+"
                      r"(?:ki|ke|ka|se|wali|wale|vali|vale|me|mein|mai|m)\b", low)
        if m:
            words = [w for w in m.group(1).split() if w not in _FILTER_STOP and not w.isdigit()]
            src = " ".join(words[-3:])
    if not src:
        m = re.search(r"\b(?:source|channel|chanel)\s+[\"“']?([\w\u0900-\u097F .&-]{2,40}?)[\"”']?\s+(?:ki|ke|ka|se|wali|wale|me)\b", low)
        if m:
            words = [w for w in m.group(1).split() if w not in _FILTER_STOP]
            src = " ".join(words[:3])
    if not src and sources:
        for title in sorted((s for s in sources if s), key=len, reverse=True):
            tl = title.lower().strip()
            if len(tl) >= 4 and not subject_from_word(tl, fuzzy=False) and re.search(r"(?<![\w])" + re.escape(tl) + r"(?![\w])", low):
                src = tl
                break
    if src:
        f["source"] = src[:60]
    src_words = set(src.lstrip("@").split()) if src else set()
    # subject: two-word phrases first, then words (whole words; Devanagari too)
    subj = ""
    for i in range(len(toks) - 1):
        if toks[i] in src_words or toks[i + 1] in src_words:
            continue
        s = subject_from_word(toks[i] + " " + toks[i + 1], fuzzy=False)
        if s:
            subj, _pair = s, (toks[i], toks[i + 1])
            used.extend(_pair)
            break
    if not subj:
        for t in toks:
            if t in src_words or t in _FTYPE_WORD:
                continue
            s = subject_from_word(t, fuzzy=t not in _FILTER_STOP)
            if s:
                subj = s
                used.append(t)
                break
    if subj:
        f["subject"] = subj
    for t in toks:
        if t in _FTYPE_WORD:
            f["type"] = _FTYPE_WORD[t]
            break
    if set(toks) & {"only", "sirf", "bas", "srf", "sirif", "keval", "sirph"}:
        f["only"] = True
    # leftover words → title words ("parmar sir wali files dikhao")
    import difflib
    used_words = set(" ".join(used).split()) | src_words
    left = []
    for t in toks:
        if t in used_words or t in _FILTER_STOP or t in _FTYPE_WORD or t.isdigit() or len(t) <= 2 or subject_from_word(t, fuzzy=False):
            continue
        if re.fullmatch(r"[a-z]+", t) and difflib.get_close_matches(t, [w for w in _FILTER_STOP if len(w) >= 3], n=1, cutoff=0.8):
            continue          # typo of a filler word ("dikhaoo", "fils")
        if subject_from_word(t):
            continue
        left.append(t)
    if left:
        f["query"] = " ".join(left[:4])
    return f


FILTER_KEYS = ("subject", "type", "source", "min_size", "max_size", "date_from", "date_to", "limit", "query")


def has_filters(f: Optional[Dict[str, Any]]) -> bool:
    return bool(f) and any(f.get(k) for k in FILTER_KEYS)


def clean_ai_filters(raw: Any) -> Dict[str, Any]:
    """Validate the AI's filters (whitelisted keys, normalised values)."""
    out: Dict[str, Any] = {}
    if not isinstance(raw, dict):
        return out
    s = raw.get("subject") or raw.get("category")
    if isinstance(s, str) and s.strip():
        slug = subject_from_word(s) or norm_category(s)
        if slug and slug != "other" and known_category(slug):
            out["subject"] = slug
    t = raw.get("type")
    if isinstance(t, str) and t.strip().lower() in _FTYPE_WORD:
        out["type"] = _FTYPE_WORD[t.strip().lower()]
    elif isinstance(t, str) and t.strip().lower() in FILE_TYPES:
        out["type"] = t.strip().lower()
    src = raw.get("source")
    if isinstance(src, str) and 1 < len(src.strip()) <= 60:
        out["source"] = src.strip().lower()
    for k in ("min_size", "max_size"):
        v = raw.get(k) if raw.get(k) is not None else raw.get(k + "_mb")
        try:
            if v is not None and str(v).strip() != "":
                mv = re.match(r"^\s*(\d+(?:\.\d+)?)\s*(kb|mb|gb)?\s*$", str(v).lower())
                if mv:
                    val = _size_bytes(mv.group(1), mv.group(2) or "mb")
                    if 0 < val < 5 * 1073741824:
                        out[k] = val
        except Exception:
            pass
    for k in ("date_from", "date_to"):
        v = raw.get(k)
        if isinstance(v, str) and re.match(r"^\d{4}-\d{2}-\d{2}", v.strip()):
            d = v.strip()[:10]
            try:
                import calendar
                ts = calendar.timegm(time.strptime(d, "%Y-%m-%d")) - IST_OFFSET  # IST midnight of that day
                out[k] = _iso(ts)
                out["date_label"] = (out.get("date_label", "") + (" " if out.get("date_label") else "") + ("se " if k == "date_from" else "tak ") + d).strip()
            except Exception:
                pass
    try:
        lim = int(raw.get("limit")) if raw.get("limit") not in (None, "", False) else 0
        if 0 < lim <= 500:
            out["limit"] = lim
    except (TypeError, ValueError):
        pass
    q = raw.get("query")
    if isinstance(q, str) and q.strip() and len(q.strip()) <= 60:
        qs = " ".join(w for w in re.findall(r"[a-z0-9]+|[\u0900-\u097F]+", q.lower()) if w not in _FILTER_STOP and not subject_from_word(w, fuzzy=False))
        if qs:
            out["query"] = qs
    return out


def merge_filters(ai_f: Dict[str, Any], local_f: Dict[str, Any]) -> Dict[str, Any]:
    """AI's filters, with the locally read ones filling every gap (a subject/type/source word in the text always counts)."""
    out = dict(ai_f or {})
    for k, v in (local_f or {}).items():
        if k == "query":
            continue
        if not out.get(k) and v:
            out[k] = v
    if "date_from" in (local_f or {}) and "date_label" in local_f and out.get("date_from") == local_f.get("date_from"):
        out["date_label"] = local_f["date_label"]
    if not out.get("query") and (local_f or {}).get("query") and not ai_f:
        out["query"] = local_f["query"]
    return out


def filter_label(f: Dict[str, Any]) -> str:
    bits = []
    if f.get("subject"):
        bits.append(cat_label(f["subject"]))
    if f.get("query"):
        bits.append(f"\"{f['query']}\"")
    if f.get("type"):
        bits.append(TYPE_LABEL.get(f["type"], f["type"]))
    if f.get("source"):
        bits.append(f"📥 {f['source']}")
    if f.get("min_size"):
        bits.append(f"{human_size(f['min_size'])}+")
    if f.get("max_size"):
        bits.append(f"< {human_size(f['max_size'])}")
    if f.get("date_from") or f.get("date_to"):
        bits.append(f.get("date_label") or "date")
    if f.get("limit"):
        bits.append(f"last {f['limit']}")
    return " · ".join(bits) or "Saari Files"


def public_filters(f: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    f = f or {}
    out = {k: f.get(k) for k in FILTER_KEYS}
    out["subject"] = f.get("subject")
    for k in ("min_size", "max_size"):
        out[k] = human_size(f[k]) if f.get(k) else None
    return out


def _row_type(r: Dict[str, Any]) -> str:
    mime = str(r.get("mime_type") or "").lower()
    fn = str(r.get("file_name") or "").lower()
    ext = fn.rsplit(".", 1)[-1] if "." in fn else ""
    if mime == "application/pdf" or ext == "pdf":
        return "pdf"
    if mime.startswith("video/") or ext in {"mp4", "mkv", "avi", "mov", "webm", "3gp"}:
        return "video"
    if mime.startswith("audio/") or ext in {"mp3", "m4a", "ogg", "wav", "aac", "opus"}:
        return "audio"
    if mime.startswith("image/") or ext in {"jpg", "jpeg", "png", "webp", "gif"}:
        return "image"
    if ext in {"doc", "docx", "ppt", "pptx", "xls", "xlsx", "txt"} or "word" in mime or "presentation" in mime:
        return "doc"
    return "pdf" if not mime and not ext else "other"


def _has_word(hay: str, w: str) -> bool:
    if re.search(r"[\u0900-\u097F]", w):
        return w in hay
    return re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", hay) is not None


def subject_words(slug: str) -> List[str]:
    ws = list(FILTER_SUBJECTS.get(slug, []))
    ws += [w for s, _, _, words in SUBJECTS if s == slug for w in words]
    ws += NAME_SUBJECT_WORDS.get(slug, [])
    ws += [w for w, s in CAT_SYNONYMS.items() if s == slug and w]
    ws += [w for s, _, _, words in EXTRA_KINDS if s == slug for w in words]
    lab = cat_label(slug).lower()
    if lab and lab != "other":
        ws.append(lab)
    return sorted({w.lower() for w in ws if w and len(w) >= 2}, key=len, reverse=True)


def row_matches(r: Dict[str, Any], f: Dict[str, Any], cmap: Dict[str, str]) -> bool:
    name = " ".join(str(r.get(k) or "") for k in ("title", "file_name", "caption")).lower()
    if f.get("subject"):
        slug = f["subject"]
        if r.get("_cat") != slug and not any(_has_word(name, w) for w in subject_words(slug)):
            return False
    if f.get("type") and _row_type(r) != f["type"]:
        return False
    if f.get("source"):
        s = f["source"].lstrip("@").lower()
        hay = " ".join(str(r.get(k) or "") for k in ("source_chat_title", "source_chat_username")).lower()
        if s not in hay and not all(w in hay for w in s.split()):
            return False
    size = r.get("file_size")
    try:
        size = int(size) if size is not None else None
    except (TypeError, ValueError):
        size = None
    if f.get("min_size") and (size is None or size < int(f["min_size"])):
        return False
    if f.get("max_size") and (size is None or size > int(f["max_size"])):
        return False
    created = str(r.get("created_at") or "")[:19].replace(" ", "T")
    if f.get("date_from") and (not created or created < f["date_from"][:19]):
        return False
    if f.get("date_to") and (not created or created >= f["date_to"][:19]):
        return False
    if f.get("query"):
        hay = name + " " + str(r.get("keywords") or "").lower()
        if not all(_has_word(hay, w) or w in hay for w in f["query"].split()):
            return False
    return True


def filtered_rows(f: Dict[str, Any]) -> List[Dict[str, Any]]:
    cols = ("id,title,file_name,file_size,mime_type,language,source_chat_title,source_chat_username,created_at,caption,keywords"
            + (",category" if has_category_col() else ""))
    rows = supabase("books", "GET", query=f"?select={cols}&status=eq.indexed&order=id.desc&limit=5000")
    cmap = {} if has_category_col() else catmap()
    out = []
    for r in rows:
        if f.get("subject"):
            r["_cat"] = book_category(r, cmap)
        if row_matches(r, f, cmap):
            out.append(r)
    out.sort(key=lambda r: int(r.get("id") or 0), reverse=True)
    if f.get("limit"):
        out = out[:int(f["limit"])]
    return out


def save_filter(f: Dict[str, Any]) -> str:
    body = json.dumps({k: f[k] for k in sorted(f) if f.get(k) not in (None, "", False)}, ensure_ascii=False, separators=(",", ":"))
    tok = hashlib.sha256(body.encode()).hexdigest()[:8]
    try:
        set_setting(f"flt:{tok}", body)
    except Exception:
        log_error("save_filter")
    return tok


def load_filter(tok: str) -> Optional[Dict[str, Any]]:
    try:
        v = json.loads(get_setting(f"flt:{tok}") or "null")
        return v if isinstance(v, dict) else None
    except Exception:
        return None


def top_categories_kb(n: int = 6) -> List[Any]:
    try:
        rows = category_rows()
    except Exception:
        rows = []
    counts: Dict[str, int] = {}
    for r in rows:
        counts[r["_cat"]] = counts.get(r["_cat"], 0) + 1
    kb: List[Any] = []
    row: List[Any] = []
    for slug in sorted(counts, key=lambda s: (s == "other", -counts[s]))[:n]:
        row.append({"text": f"{cat_emoji(slug)} {cat_label(slug)} ({counts[slug]})", "callback_data": f"cat:{slug}:1"})
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    return kb


def filtered_files_view(f: Dict[str, Any], page: int = 1, tok: str = "") -> Any:
    if not has_filters(f):
        return files_view(page)
    rows = filtered_rows(f)
    label = filter_label(f)
    if not rows:
        lines = [f"😕 {label} ki koi file nahi mili."]
        kb = top_categories_kb()
        if kb:
            lines.append("\n🗂️ Ye categories hain 👇")
        return "\n".join(lines), {"inline_keyboard": kb + [[{"text": "📂 All Files", "callback_data": "ad:files:1"}], BACK_ROW]}
    tok = tok or save_filter(f)
    total = len(rows)
    pages = max(1, -(-total // FILES_PAGE))
    page = min(max(1, int(page)), pages)
    part = rows[(page - 1) * FILES_PAGE: page * FILES_PAGE]
    lines = [f"📂 {label} — {total} file{'s' if total != 1 else ''}", f"Page {page}/{pages} · nayi files upar", ""]
    nums: List[Any] = []
    for n, r in enumerate(part, (page - 1) * FILES_PAGE + 1):
        bits = [x for x in (human_size(r.get("file_size")), str(r.get("source_chat_title") or "")[:20]) if x]
        lines.append(f"{n}. {str(r.get('title') or 'Untitled')[:70]}" + (f"\n    {' · '.join(bits)}" if bits else ""))
        nums.append({"text": str(n), "callback_data": f"ad:f:{r['id']}:{page}:{tok}"})
    lines.append("\nFile ka number dabao → 📤 Bhejo / ✏️ Rename / ❌ Hatao")
    kb = [nums[i:i + 5] for i in range(0, len(nums), 5)]
    nav = []
    if page > 1:
        nav.append({"text": "⬅️ Previous", "callback_data": f"ad:fl:{tok}:{page - 1}"})
    nav.append({"text": f"{page}/{pages}", "callback_data": "ad:noop"})
    if page < pages:
        nav.append({"text": "Next ➡️", "callback_data": f"ad:fl:{tok}:{page + 1}"})
    kb.append(nav)
    kb.append([{"text": "📂 Saari Files", "callback_data": "ad:files:1"}])
    kb.append(BACK_ROW)
    return "\n".join(lines)[:4000], {"inline_keyboard": kb}


def known_sources() -> List[str]:
    v = _REQ.get("_srcnames")
    if v is None:
        v = []
        try:
            for r in supabase("channels", "GET", query="?select=title,username&limit=200"):
                v += [str(r.get("title") or ""), str(r.get("username") or "")]
        except Exception:
            pass
        _REQ["_srcnames"] = v = [x for x in v if x]
    return v


# ---- move a file to another category ("Parmar sir polity pdf ko history category me daal do")

def move_category_flow(chat_id: int, user_id: int, find: str, to: str, query: Optional[Dict[str, Any]] = None) -> None:
    slug = subject_from_word(to) or norm_category(to)
    if not slug or not known_category(slug):
        names = ", ".join(cat_label(s) for s, _, _, _ in SUBJECTS if s != "other")
        send_text(chat_id, f"🤔 \"{to[:30]}\" naam ki category nahi hai.\nInme se likho: {names}",
                  {"inline_keyboard": [[{"text": "🗂️ Categories", "callback_data": "user_categories"}], BACK_ROW]})
        return
    if not find:
        send_text(chat_id, f"🗂️ Kaunsi file {cat_label(slug)} me daalni hai? File ka naam likho, jaise:\n"
                           f"\"Parmar sir polity pdf ko {cat_label(slug).lower()} category me daal do\"", {"inline_keyboard": [BACK_ROW]})
        return
    rows = find_books(find, 8)
    if not rows:
        send_text(chat_id, f"😕 \"{find[:60]}\" naam ki koi file nahi mili.", {"inline_keyboard": [[{"text": "📂 All Files", "callback_data": "ad:files:1"}], BACK_ROW]})
        return
    if len(rows) == 1:
        r = rows[0]
        ask_confirm(chat_id, user_id, f"🗂️ Category badalni hai?\n\n📄 {str(r.get('title') or '')[:70]}\n→ {cat_emoji(slug)} {cat_label(slug)}",
                    {"kind": "setcat", "ids": [int(r["id"])], "cat": slug}, query)
        return
    token = set_op(user_id, {"kind": "setcat_pick", "cat": slug, "ids": [int(r["id"]) for r in rows]})
    kb = [[{"text": f"🗂️ {str(r.get('title') or 'Untitled')[:40]}", "callback_data": f"ad:op:c:{token}:{r['id']}"}] for r in rows]
    kb.append([{"text": "❌ Deny", "callback_data": f"ad:op:n:{token}"}])
    send_text(chat_id, f"🔎 \"{find[:60]}\" se {len(rows)} files mili. Kaunsi {cat_label(slug)} me daalni hai?", {"inline_keyboard": kb})


def apply_setcat(user_id: int, ids: List[int], slug: str) -> List[str]:
    idl = ",".join(str(int(x)) for x in ids)
    rows = supabase("books", "GET", query=f"?id=in.({idl})&select=id,title,keywords") if idl else []
    save_categories({int(r["id"]): slug for r in rows})
    for r in rows:
        try:
            supabase("books", "PATCH", {"keywords": with_cat_tag(str(r.get("keywords") or ""), slug)}, f"?id=eq.{int(r['id'])}")
        except Exception:
            log_error("setcat keywords")
    try:
        audit_log(f"🗂️ Category badli ({actor_label(user_id)}): " + "; ".join(str(r.get("title") or "")[:50] for r in rows[:5])
                  + f" → {cat_label(slug)}\n🕒 {ist_now()}")
    except Exception:
        log_error("setcat log")
    return [str(r.get("title") or "") for r in rows]


def delete_filtered_flow(chat_id: int, user_id: int, f: Dict[str, Any], find: str = "") -> None:
    """Delete with a filter never deletes in bulk: it lists the matches, each one asks ✅ Accept / ❌ Deny."""
    rows = filtered_rows(f)
    label = filter_label(f)
    if not rows:
        send_text(chat_id, f"😕 {label} ki koi file nahi mili — kuch nahi hataya.", {"inline_keyboard": [[{"text": "📂 All Files", "callback_data": "ad:files:1"}], BACK_ROW]})
        return
    if len(rows) == 1:
        send_text(chat_id, *confirm_view(f"fdel:{rows[0]['id']}"))
        return
    kb = [[{"text": f"❌ {str(r.get('title') or 'Untitled')[:42]}", "callback_data": f"ad:fdel:{r['id']}"}] for r in rows[:10]]
    more = f"\n(pehli 10 dikha raha hu, kul {len(rows)})" if len(rows) > 10 else ""
    send_text(chat_id, f"🔎 {label}: {len(rows)} files mili. Kaunsi hatani hai?\nEk-ek karke — har file pe ✅ Accept / ❌ Deny puchega.{more}",
              {"inline_keyboard": kb + [BACK_ROW]})


def admin_parse(text: str, use_ai: bool = True, user_id: int = 0) -> Dict[str, Any]:
    """The whole reading of an admin message (diag + tests): local → AI → merged result."""
    local = local_admin_intent(text)
    res: Dict[str, Any] = {"text": text, "local": local, "ai": None, "source": "local" if local else None}
    final = local
    if local is None and use_ai and looks_like_command(text):
        ai = ai_admin_intent(user_id, text)
        res["ai"] = ai
        final = ai
        res["source"] = "ai" if ai else "none"
    res["final"] = final
    return res


# ---- rename / bulk rename / delete flows

def new_name_for(old: str, find: str, to: str) -> str:
    """Replace the matched words inside the title ("Polity Notes" → "Politics Notes"); else the whole name."""
    old = old or ""
    m = re.search(re.escape(find), old, re.I) if find else None
    if not m:
        return re.sub(r"\s+", " ", to).strip()[:500]
    seg = m.group(0)
    rep_to = to
    if to.islower() and seg[:1].isupper():
        rep_to = to.upper() if seg.isupper() and len(seg) > 1 else to[:1].upper() + to[1:]
    return re.sub(r"\s+", " ", old[:m.start()] + rep_to + old[m.end():]).strip()[:500]


def find_books(find: str, limit: int = 10) -> List[Dict[str, Any]]:
    find = (find or "").strip()
    if not find:
        return []
    if find.isdigit():
        rows = supabase("books", "GET", query=f"?id=eq.{int(find)}&status=eq.indexed&select=id,title,file_size&limit=1")
        if rows:
            return rows
    rows = search_books(find)
    lowf = find.lower()
    in_title = [r for r in rows if lowf in str(r.get("title") or "").lower()]
    if not in_title:
        # search_books caps at 10: look straight in the titles too.
        in_title = supabase("books", "GET", query="?select=id,title,file_size&status=eq.indexed&order=id.desc&limit=" +
                            f"{limit}&title=ilike." + urllib.parse.quote(f"*{find}*", safe="*"))
    return (in_title or rows)[:limit]


def preview_rename(chat_id: int, user_id: int, items: List[Any], query: Optional[Dict[str, Any]] = None,
                   extra: Optional[Dict[str, Any]] = None, head: str = "✏️ Ye naam badlega — pakka?") -> None:
    items = [(int(i), str(o or ""), str(n or "")) for i, o, n in items if n and str(n).strip() and str(n) != str(o)]
    if not items:
        show(chat_id, "✅ Kuch badalne ko nahi hai — naam pehle se waise hi hai.", {"inline_keyboard": [BACK_ROW]}, query)
        return
    lines = [head + (f" ({len(items)} files)" if len(items) > 1 else ""), ""]
    for n, (_, old, new) in enumerate(items[:12], 1):
        lines.append(f"{n}. {old[:70]}\n   → {new[:70]}")
    if len(items) > 12:
        lines.append(f"\n…aur {len(items) - 12} files")
    try:
        ids = ",".join(str(i) for i, _, _ in items[:50])
        big = [r for r in supabase("books", "GET", query=f"?id=in.({ids})&select=id,file_size,mime_type") if
               int(r.get("file_size") or 0) > TG_FILE_LIMIT] if ids else []
    except Exception:
        big = []
    big = [r for r in big if not str(r.get("mime_type") or "").lower().startswith(("video/", "audio/"))] \
        if bigfile_ready() else big
    if big and bigfile_ready():
        lines.append("\n🔁 20 MB se badi file" + (f" ({len(big)})" if len(big) > 1 else "") +
                     ": Accept ke baad bot use naye naam se dobara upload karega (2–4 min). Tab tak caption me naya naam dikhega.")
    elif big:
        lines.append("\n⚠️ 20 MB se badi file ka asli naam Telegram badalne nahi deta, caption me naya naam dikhega"
                     + (f" ({len(big)} files)" if len(big) > 1 else ""))
    op = {"kind": "rename", "items": [[i, o[:300], n[:300]] for i, o, n in items]}
    op.update(extra or {})
    ask_confirm(chat_id, user_id, "\n".join(lines)[:3900], op, query)


def rename_flow(chat_id: int, user_id: int, find: str, to: str) -> None:
    rows = find_books(find)
    if not rows:
        send_text(chat_id, f"😕 \"{find[:60]}\" naam ki koi file nahi mili.\nSahi naam likho ya 📂 All Files me dekho.",
                  {"inline_keyboard": [[{"text": "📂 All Files", "callback_data": "ad:files:1"}], BACK_ROW]})
        return
    if len(rows) == 1:
        r = rows[0]
        preview_rename(chat_id, user_id, [(r["id"], r.get("title"), new_name_for(str(r.get("title") or ""), find, to))])
        return
    token = set_op(user_id, {"kind": "rename_pick", "find": find, "to": to, "ids": [int(r["id"]) for r in rows]})
    kb = [[{"text": f"✏️ {str(r.get('title') or 'Untitled')[:40]}", "callback_data": f"ad:op:p:{token}:{r['id']}"}] for r in rows]
    with_word = [r for r in rows if find.lower() in str(r.get("title") or "").lower()]
    if len(with_word) > 1:
        kb.append([{"text": f"✏️ Sab {len(with_word)} me \"{find[:12]}\" badlo", "callback_data": f"ad:op:a:{token}"}])
    kb.append([{"text": "❌ Deny", "callback_data": f"ad:op:n:{token}"}])
    send_text(chat_id, f"🔎 \"{find[:60]}\" se {len(rows)} files mili. Kiska naam \"{to[:60]}\" karna hai?", {"inline_keyboard": kb})


def bulk_rename_flow(chat_id: int, user_id: int, find: str, to: str) -> None:
    rows = supabase("books", "GET", query="?select=id,title&status=eq.indexed&order=id.desc&limit=300&title=ilike." +
                    urllib.parse.quote(f"*{find}*", safe="*"))
    items = [(r["id"], r.get("title"), new_name_for(str(r.get("title") or ""), find, to)) for r in rows
             if find.lower() in str(r.get("title") or "").lower()]
    if not items:
        send_text(chat_id, f"😕 Kisi file ke naam me \"{find[:60]}\" nahi mila.", {"inline_keyboard": [BACK_ROW]})
        return
    preview_rename(chat_id, user_id, items, head=f"✏️ \"{find[:30]}\" → \"{to[:30]}\" — ye naam badlenge, pakka?")


def delete_flow(chat_id: int, user_id: int, find: str) -> None:
    if not find:
        send_text(chat_id, "❌ Kaunsi file hatani hai? Naam likho, jaise: \"lucent wali file hatao\" — ya 📂 All Files me tap karo.",
                  {"inline_keyboard": [[{"text": "📂 All Files", "callback_data": "ad:files:1"}], BACK_ROW]})
        return
    rows = find_books(find, 8)
    if not rows:
        send_text(chat_id, f"😕 \"{find[:60]}\" naam ki koi file nahi mili.", {"inline_keyboard": [[{"text": "📂 All Files", "callback_data": "ad:files:1"}], BACK_ROW]})
        return
    if len(rows) == 1:
        send_text(chat_id, *confirm_view(f"fdel:{rows[0]['id']}"))
        return
    kb = [[{"text": f"❌ {str(r.get('title') or 'Untitled')[:42]}", "callback_data": f"ad:fdel:{r['id']}"}] for r in rows]
    send_text(chat_id, f"🔎 \"{find[:60]}\" se {len(rows)} files mili. Kaunsi hatani hai? (agle step me ✅ Accept puchega)",
              {"inline_keyboard": kb + [BACK_ROW]})


def backup_flow(chat_id: int, user_id: int) -> None:
    target = get_setting("backup_chat_id")
    if not target:
        send_text(chat_id, "💾 Pehle Backup Channel set karo, phir purani files wahan upload hongi.",
                  {"inline_keyboard": [[{"text": "💾 Backup Channel", "callback_data": "ad:backup"}], BACK_ROW]})
        return
    try:
        n = count_rows("books", backup_pending_query(target))
    except Exception:
        n = 0
    if not n:
        send_text(chat_id, "✅ Saari files pehle se backup channel me hain. Nayi files apne-aap jaati rahengi.", {"inline_keyboard": [BACK_ROW]})
        return
    ask_confirm(chat_id, user_id, f"💾 {n} purani files backup channel \"{chat_title(target)}\" me upload hongi\n"
                                  f"(20-20 karke, koi duplicate nahi).\n\nShuru karu?", {"kind": "backup"})


def apply_renames(items: List[Any], cats: Optional[Dict[str, str]] = None, budget: float = 6.5,
                  who: str = "admin") -> List[Any]:
    """Apply [[id, old, new], ...]; the original file name / caption stay searchable. Returns items not done (time budget)."""
    started = time.monotonic()
    ids = [int(x[0]) for x in items]
    have: Dict[int, Dict[str, Any]] = {}
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        for r in supabase("books", "GET", query=f"?id=in.({','.join(str(x) for x in chunk)})&select=id,caption,file_name,source_chat_title"):
            have[int(r["id"])] = r
    left = []
    for n, (bid, old, new) in enumerate(items):
        if time.monotonic() - started > budget:
            left = items[n:]
            break
        row = have.get(int(bid)) or {}
        # Search words = new name + the file's own caption / original file name / channel (not older names).
        keywords = clean_text(" ".join(str(x or "") for x in (new, row.get("caption"), row.get("file_name"),
                                                                row.get("source_chat_title"))))
        slug = (cats or {}).get(str(bid)) or ""
        supabase("books", "PATCH", {"title": new[:500], "keywords": with_cat_tag(keywords[:3990], slug)}, f"?id=eq.{int(bid)}")
    if cats:
        try:
            save_categories({int(k): v for k, v in cats.items()})
        except Exception:
            log_error("save_categories")
    try:
        rename_log(items[:len(items) - len(left)], who)
    except Exception:
        log_error("rename_log")
    return left


def run_op(query: Dict[str, Any], chat_id: int, user_id: int, op: Dict[str, Any]) -> None:
    kind = op.get("kind")
    if kind == "rename":
        items = op.get("items") or []
        who = actor_label(user_id) + (" · AI se purane naam theek" if op.get("next_after") is not None else "")
        left = apply_renames(items, op.get("cats"), who=who)
        done = items[:len(items) - len(left)]
        lines = [f"✅ {len(done)} file ka naam badal diya" if len(done) != 1 else "✅ Naam badal diya:"]
        for _, old, new in done[:5]:
            lines.append(f"• {str(old)[:50]} → {str(new)[:50]}")
        try:
            big_line = bigfile_after_rename(chat_id, [int(x[0]) for x in done], actor_label(user_id))
        except Exception:
            log_error("bigfile_after_rename")
            big_line = ""
        if big_line:
            lines.append("\n" + big_line)
        kb: List[Any] = []
        if left:
            token = set_op(user_id, dict(op, items=left))
            lines.append(f"\n⏳ Baaki {len(left)} — niche dabao")
            kb.append([{"text": f"▶️ Baaki {len(left)} bhi badlo", "callback_data": f"ad:op:y:{token}"}])
        else:
            clear_op(user_id)
        if op.get("next_after") is not None:
            set_setting("ainame_cursor", str(int(op["next_after"])))
            if not left:
                kb.append([{"text": f"▶️ Agle {FIX_BATCH} naam", "callback_data": f"ad:fixn:{int(op['next_after'])}"}])
        show(chat_id, "\n".join(lines), {"inline_keyboard": kb + [BACK_ROW]}, query)
    elif kind == "backup":
        clear_op(user_id)
        cb_answer(query, "💾 Backup chal raha hai...")
        show(chat_id, *backup_old_view(), query=query)
    elif kind == "restart":
        clear_op(user_id)
        show(chat_id, restart_text(), status_markup(), query)
    elif kind == "ainame":
        clear_op(user_id)
        set_setting("ai_naming", "on" if op.get("on") else "off")
        text, kb = ainame_view()
        show(chat_id, ("✅ AI Naming chalu ho gaya\n\n" if op.get("on") else "✅ AI Naming band ho gaya\n\n") + text, kb, query)
    elif kind == "logfill":
        clear_op(user_id)
        if not get_setting("log_chat_id"):
            show(chat_id, setlog_steps_text(), {"inline_keyboard": [[{"text": "📝 Log Chat set karo", "callback_data": "ad:setlog"}], BACK_ROW]}, query)
            return
        logfill_start(user_id, chat_id, int(op.get("n") or 0))
        logfill_run_from_button(query, chat_id)
    elif kind == "setcat":
        clear_op(user_id)
        slug = str(op.get("cat") or "")
        titles = apply_setcat(user_id, [int(x) for x in op.get("ids") or []], slug)
        show(chat_id, f"✅ Category badal di → {cat_emoji(slug)} {cat_label(slug)}\n" + "\n".join(f"• {t[:60]}" for t in titles[:5]),
             {"inline_keyboard": [[{"text": f"📂 {cat_label(slug)} ki files", "callback_data": f"cat:{slug}:1"}], BACK_ROW]}, query)
    elif kind == "addch":
        clear_op(user_id)
        if op.get("ch") == "join":
            title = add_required_channel(str(op.get("target")), None)
            show(chat_id, f"✅ Join channel jud gaya: {title}", {"inline_keyboard": [[{"text": "📋 Join Channels", "callback_data": "ad:reqlist"}], BACK_ROW]}, query)
        else:
            chat = add_source_channel(str(op.get("target")), None)
            note = "" if chat.get("_bot_admin") else "\n" + AUTO_SAVE_OFF_NOTE
            show(chat_id, f"✅ Source channel jud gaya: {chat.get('title')}" + note,
                 {"inline_keyboard": [[{"text": "📋 Source Channels", "callback_data": "ad:srclist"}], BACK_ROW]}, query)
    else:
        show(chat_id, "Ye kaam purana ho gaya. Dobara bolo.", {"inline_keyboard": [BACK_ROW]}, query)


# ---- 📂 All Files (10 per page, newest first)

def files_view(page: int = 1) -> Any:
    page = max(1, int(page))

    def _rows(p: int) -> Any:
        return supabase("books", "GET", query="?select=id,title,file_size,language,source_chat_title&status=eq.indexed"
                                              f"&order=id.desc&offset={(p - 1) * FILES_PAGE}&limit={FILES_PAGE}")
    total, rows = par(lambda: view_cached("files_total", lambda: count_rows("books", "&status=eq.indexed")),
                      lambda: view_cached(f"files_p{page}", lambda: _rows(page)))   # both at once
    if isinstance(total, Exception):
        raise total
    pages = max(1, -(-total // FILES_PAGE))
    if page > pages:              # an old ➡️ button after files were removed: show the last page that exists
        page = pages
        rows = _rows(page) if total else []
    rows = [] if isinstance(rows, Exception) or not total else rows
    lines = [f"📂 Saari Files — {total}", f"Page {page}/{pages} · nayi files upar", ""]
    nums: List[Any] = []
    for n, r in enumerate(rows, (page - 1) * FILES_PAGE + 1):
        bits = [x for x in (human_size(r.get("file_size")), str(r.get("source_chat_title") or "")[:20]) if x]
        lines.append(f"{n}. {str(r.get('title') or 'Untitled')[:70]}" + (f"\n    {' · '.join(bits)}" if bits else ""))
        nums.append({"text": str(n), "callback_data": f"ad:f:{r['id']}:{page}"})
    if not rows:
        lines.append("Abhi koi file nahi hai.")
    else:
        lines.append("\nFile ka number dabao → 📤 Bhejo / ✏️ Rename / ❌ Hatao")
    kb = [nums[i:i + 5] for i in range(0, len(nums), 5)]
    nav = []
    if page > 1:
        nav.append({"text": "⬅️ Previous", "callback_data": f"ad:files:{page - 1}"})
    nav.append({"text": f"{page}/{pages}", "callback_data": "ad:noop"})
    if page < pages:
        nav.append({"text": "Next ➡️", "callback_data": f"ad:files:{page + 1}"})
    kb.append(nav)
    kb.append(BACK_ROW)
    return "\n".join(lines)[:4000], {"inline_keyboard": kb}


def file_menu_view(book_id: int, page: int = 1) -> Any:
    rows = supabase("books", "GET", query=f"?id=eq.{int(book_id)}&select=id,title,file_name,file_size,language,source_chat_title,status&limit=1")
    if not rows or rows[0].get("status") not in (None, "indexed"):
        return "😕 Ye file ab nahi hai.", {"inline_keyboard": [[{"text": "📂 All Files", "callback_data": f"ad:files:{page}"}]]}
    r = rows[0]
    lines = [f"📄 {r.get('title') or 'Untitled'}", ""]
    if r.get("file_name"):
        lines.append(f"Asli file naam: {r['file_name']}")
    if human_size(r.get("file_size")):
        lines.append(f"Size: {human_size(r.get('file_size'))}")
    if r.get("source_chat_title"):
        lines.append(f"Source: {r['source_chat_title']}")
    lines.append(f"ID: {r['id']}")
    return "\n".join(lines)[:3900], {"inline_keyboard": [
        [{"text": "📤 Bhejo", "callback_data": f"ad:fs:{r['id']}"}, {"text": "✏️ Rename", "callback_data": f"ad:rn:{r['id']}"}],
        [{"text": "❌ Hatao", "callback_data": f"ad:fdel:{r['id']}"}],
        [{"text": "🔙 List", "callback_data": f"ad:files:{page}"}]]}


# ---- 🤖 AI naming of files

AI_NAME_SYSTEM = (
    "You clean file titles for an Indian govt-exam study PDF library. Each item has the post caption and the original "
    "file name. Reply ONLY with JSON {\"titles\":[{\"i\":1,\"title\":\"...\"}]}. Rules: a short clean title, max 70 "
    "characters. Use ONLY words found in the caption or file name (you may fix spelling, spacing and capital letters). "
    "Keep book/author/publisher, exam, subject, class, part/volume, year and language if present. Remove channel names, "
    "@usernames, links, emojis, hashtags, 'join', 'download', promo lines, file extensions, underscores and ID numbers. "
    "Never invent a book title, author or exam that is not written there. If nothing useful, return the cleaned file name.")
GENERIC_TITLE_WORDS = {"hindi", "english", "notes", "note", "book", "books", "pdf", "part", "vol", "volume", "class", "exam",
                       "gk", "gs", "and", "the", "of", "for", "in", "with", "by", "edition", "new", "complete", "general",
                       "knowledge", "test", "mock", "paper", "papers", "question", "questions", "answer", "answers",
                       "solved", "previous", "year", "set", "series", "chapter", "medium", "guide", "practice", "full",
                       "study", "material", "important", "se", "ki", "ka", "ke", "aur", "evam"}


def title_supported(title: str, source: str) -> bool:
    """Guard against made-up titles: every word must come from the caption / file name (spelling fixes allowed)."""
    import difflib
    src = normalize(source)
    src_words = re.findall(r"[a-z0-9]+|[\u0900-\u097F]+", src)
    for w in re.findall(r"[a-z0-9]+|[\u0900-\u097F]+", (title or "").lower()):
        if w in GENERIC_TITLE_WORDS or len(w) <= 2 or w in src:
            continue
        if w.isdigit():
            return False
        if difflib.get_close_matches(w, src_words, n=1, cutoff=0.75):
            continue
        return False
    return True


def clean_ai_title(t: Any) -> str:
    t = re.sub(r"\s+", " ", str(t or "")).strip().strip("\"'“”`")
    t = re.sub(r"\.(pdf|docx?|pptx?|zip|rar|mp4|epub)$", "", t, flags=re.I).strip()
    return t[:100]


def ai_titles(items: List[Dict[str, Any]], cap: float = 9.0) -> Dict[int, str]:
    """{i: title} for [{"i", "caption", "file", "old"}] — only titles that pass the no-invention guard."""
    payload = [{"i": x["i"], "caption": re.sub(r"\s+", " ", str(x.get("caption") or ""))[:300],
                "file": str(x.get("file") or "")[:150]} for x in items]
    content = ai_complete(AI_NAME_SYSTEM, json.dumps(payload, ensure_ascii=False), max_tokens=min(1800, 50 + 45 * len(items)), cap=cap)
    if not content:
        return {}
    try:
        data = json.loads(content[content.find("{"): content.rfind("}") + 1])
    except Exception:
        return {}
    got = data.get("titles") if isinstance(data, dict) else None
    out: Dict[int, str] = {}
    by_i = {x["i"]: x for x in items}
    for t in got if isinstance(got, list) else []:
        if not isinstance(t, dict) or t.get("i") not in by_i:
            continue
        x = by_i[t["i"]]
        title = clean_ai_title(t.get("title"))
        source = " ".join(str(x.get(k) or "") for k in ("caption", "file", "old"))
        if len(title) >= 3 and title_supported(title, source):
            out[int(t["i"])] = title
    return out


def ai_name_budget_ok() -> bool:
    raw = get_setting("ainame:all") or ""
    day, _, n = raw.partition(":")
    return not (day == ist_day() and n.isdigit() and int(n) >= AI_NAME_DAILY)


def maybe_ai_title(record: Dict[str, Any]) -> str:
    """New file: let the AI clean its name (caption + original file name). Returns the old title when renamed.
    Falls back silently to the normal cleanup when off / no key / limit / slow / unsure."""
    if not record.get("caption") and not record.get("file_name"):
        return ""
    if req_elapsed() > 5 or not ai_provider() or not ai_naming_on() or not ai_name_budget_ok():
        return ""
    uniq = record.get("telegram_file_unique_id")
    if uniq:
        # Already in the catalog (re-forward): keep its name, an admin may have renamed it.
        have = supabase("books", "GET", query=f"?telegram_file_unique_id=eq.{urllib.parse.quote(str(uniq))}&select=id,title&limit=1")
        if have:
            record["title"] = have[0].get("title") or record.get("title")
            return ""
    _count_day("ainame:all")
    old = str(record.get("title") or "")
    got = ai_titles([{"i": 1, "caption": record.get("caption"), "file": record.get("file_name"), "old": old}],
                    cap=max(2.0, min(5.0, 9.0 - req_elapsed())))
    new = got.get(1)
    if not new or new == old:
        return ""
    record["title"] = new[:500]
    record["keywords"] = (new + " " + str(record.get("keywords") or old))[:4000]
    return old or str(record.get("file_name") or "")


def ainame_view() -> Any:
    on = ai_naming_on()
    key = bool(ai_provider())
    cursor = get_setting("ainame_cursor") or "0"
    try:
        queued = len(json.loads(get_setting("ainame_queue") or "[]"))
    except Exception:
        queued = 0
    lines = ["🤖 AI Naming — hamesha chalu, apne-aap" if on else "🤖 AI Naming — ⛔ emergency band hai (/ainaming on se chalu)", "",
             "Har nayi file save hote hi bot file ke andar ke pehle 2-3 page padhta hai aur us hisaab se naam rakhta hai",
             "(jaise hjskjd.pdf me history notes → \"History Notes\"). Naam me se uploader/channel ka naam, @, link hata deta hai.",
             "Saath me subject category: History, Polity, Science... (📂 Categories me dikhegi).",
             "File ke andar kuch nahi badalta — sirf naam + category. Asli file naam bhi save rehta hai.",
             "Backup channel me file isi saaf naam se jaati hai.",
             "AI fail ho to file naam saaf karke rakh deta hai aur baad me 3 baar tak dobara try karta hai.",
             "", f"AI key: {'✅ hai' if key else '❌ nahi (🔑 AI Keys me jodo)'}"]
    if queued:
        lines.append(f"⏳ AI naam baaki: {queued} files (apne-aap ho jayenge)")
    if cursor not in ("", "0"):
        lines.append(f"Purane naam: ID {cursor} tak check ho chuke")
    kb = [[{"text": "🛠️ AI se purane naam theek karo (ek baar)", "callback_data": "ad:fixn"}],
          [{"text": "🔁 Badi files ka naam theek karo", "callback_data": "ad:bigfix"}],
          [{"text": "🤖 AI test", "callback_data": "ad:aitest"}], BACK_ROW]
    return "\n".join(lines), {"inline_keyboard": kb}


def fixnames_view(chat_id: int, user_id: int, after: Optional[int], query: Optional[Dict[str, Any]] = None) -> None:
    """Next FIX_BATCH old files → one AI call → preview old → new → ✅ Accept applies."""
    if after is None:
        try:
            after = int(get_setting("ainame_cursor") or 0)
        except ValueError:
            after = 0
    else:
        set_setting("ainame_cursor", str(int(after)))
    if not ai_provider():
        show(chat_id, "❌ AI key nahi hai. Pehle 🔑 AI Keys me key jodo.", {"inline_keyboard": [BACK_ROW]}, query)
        return
    rows = supabase("books", "GET", query=f"?select=id,title,caption,file_name,content_text&status=eq.indexed&id=gt.{int(after)}&order=id&limit={FIX_BATCH}")
    if not rows:
        show(chat_id, "🎉 Saare purane naam check ho gaye! Nayi files ka naam AI apne-aap rakhega.",
             {"inline_keyboard": [[{"text": "🔁 Shuru se dobara", "callback_data": "ad:fixn:0"}], BACK_ROW]}, query)
        return
    if not ai_name_budget_ok():
        show(chat_id, "⏳ Aaj ki AI naming limit poori ho gayi. Kal dobara try karo.", {"inline_keyboard": [BACK_ROW]}, query)
        return
    _count_day("ainame:all")
    last = int(rows[-1]["id"])
    got = ai_name_items([{"i": n, "caption": r.get("caption"), "file": r.get("file_name"), "old": r.get("title"),
                          "text": str(r.get("content_text") or "")[:400]} for n, r in enumerate(rows, 1)],
                        cap=max(3.0, 9.0 - req_elapsed()), excerpt_chars=400)
    if got is None:
        show(chat_id, "⏳ AI abhi busy hai (limit / network). Naam purane jaise hi rahenge, kuch kharab nahi hua.\n"
                      "Thodi der baad 🔁 dabao — ya 🩺 Status → 🤖 AI test se wajah dekho.",
             {"inline_keyboard": [[{"text": "🔁 Dobara try", "callback_data": f"ad:fixn:{int(after)}"}],
                                  [{"text": "🤖 AI test", "callback_data": "ad:aitest"}], BACK_ROW]}, query)
        return
    cats: Dict[str, str] = {}
    for n, r in enumerate(rows, 1):
        subj = (got.get(n) or {}).get("subject") or classify_local(
            " ".join(str(r.get(k) or "") for k in ("title", "caption", "file_name")), str(r.get("content_text") or ""))
        cats[str(r["id"])] = subj
    items = [(r["id"], r.get("title"), got[n]["title"]) for n, r in enumerate(rows, 1)
             if (got.get(n) or {}).get("title") and got[n]["title"].strip().lower() != str(r.get("title") or "").strip().lower()]
    if not items:
        save_categories({int(k): v for k, v in cats.items()})
        set_setting("ainame_cursor", str(last))
        show(chat_id, f"✅ Ye {len(rows)} naam pehle se theek hain (category laga di).",
             {"inline_keyboard": [[{"text": f"▶️ Agle {FIX_BATCH} naam", "callback_data": f"ad:fixn:{last}"}], BACK_ROW]}, query)
        return
    preview_rename(chat_id, user_id, items, query, {"next_after": last, "cats": cats},
                   head=f"🤖 AI ke naam sujhav — file ke andar padhke ({len(rows)} me se {len(items)} badlenge) — pakka?")


def handle_admin_callback_v6(query: Dict[str, Any], cmd: str, chat_id: int, user_id: int) -> bool:
    """New buttons (v6). True when handled."""
    if cmd == "noop":
        return True
    if cmd.startswith("files"):
        page = cmd.split(":", 1)[1] if ":" in cmd else "1"
        show(chat_id, *files_view(int(page) if page.isdigit() else 1), query=query)
        return True
    if cmd.startswith("fl:"):      # filtered list page: ad:fl:<filter token>:<page>
        parts = cmd.split(":")
        flt = load_filter(parts[1]) if len(parts) > 1 else None
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
        if flt is None:      # the filter behind this button is gone: say so, never fall back to ALL files silently
            show(chat_id, "🔄 Ye filter wali list purani ho gayi. Dobara bolo (jaise \"polity ki pdf dikhao\") ya saari files dekho:",
                 {"inline_keyboard": [[{"text": "📂 Saari Files", "callback_data": "ad:files:1"}], BACK_ROW]}, query)
        else:
            show(chat_id, *filtered_files_view(flt, page, parts[1]), query=query)
        return True
    if cmd.startswith("f:"):
        parts = cmd.split(":")
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
        text, kb = file_menu_view(int(parts[1]), page)
        if len(parts) > 3 and parts[3]:
            for row in kb.get("inline_keyboard", []):
                for b in row:
                    if b.get("callback_data") == f"ad:files:{page}":
                        b["callback_data"] = f"ad:fl:{parts[3]}:{page}"
        show(chat_id, text, kb, query=query)
        return True
    if cmd.startswith("fs:"):
        rows = supabase("books", "GET", query=f"?id=eq.{int(cmd[3:])}&select=*&limit=1")
        if rows:
            deliver_result(chat_id, rows[0], query.get("from"))
        else:
            send_text(chat_id, "😕 Ye file ab nahi hai.")
        return True
    if cmd.startswith("fdel:"):      # reached only after ✅ Accept (ad:y:fdel:<id>)
        bid = int(cmd.split(":", 1)[1])
        rows = supabase("books", "GET", query=f"?id=eq.{bid}&select=title&limit=1")
        supabase("books", "PATCH", {"status": "removed"}, f"?id=eq.{bid}")
        text, kb = files_view(1)
        show(chat_id, f"✅ File hata di: {(rows[0].get('title') if rows else bid)}\n\n" + text, kb, query)
        return True
    if cmd == "logfill":
        logfill_preview(chat_id, user_id, query)
        return True
    if cmd == "logtest":
        show(chat_id, log_test_text(log_test()), {"inline_keyboard": [[{"text": "📜 Purani updates Log me bhejo", "callback_data": "ad:logfill"}], BACK_ROW]}, query)
        return True
    if cmd == "logfill:go":
        st = logfill_state()
        if not logfill_active(st) and st.get("stopped") == "errors":
            st.pop("stopped", None)
            st["errors"] = 0
            logfill_save(st)
        if not logfill_active():
            show(chat_id, logfill_status_text(st) if st else "📜 Koi adhoora kaam nahi hai.", {"inline_keyboard": [BACK_ROW]}, query)
            return True
        logfill_run_from_button(query, chat_id)
        return True
    if cmd.startswith("cmdq:"):
        clear_op(user_id)
        show(chat_id, *cmd_choice_view(user_id), query=query)
        return True
    if cmd == "smode":
        on = get_setting(f"admsearch:{user_id}") == "1"
        set_setting(f"admsearch:{user_id}", "0" if on else "1")
        note = ("🔍 Search Mode OFF — ab bot samjhega aapka msg kaam hai ya search 🤖\n\n" if on else
                "🔍 Search Mode ON — ab aapka har msg seedha search hoga.\n(Bolke kaam ke liye ise OFF karo)\n\n")
        show(chat_id, note + ADMIN_PANEL_TEXT, admin_menu(user_id), query, "HTML")
        return True
    if cmd == "aitest":
        show(chat_id, ai_test_text(), {"inline_keyboard": [[{"text": "🔁 Dobara test", "callback_data": "ad:aitest"}], BACK_ROW]},
             query, "HTML")
        return True
    if cmd == "ainame":
        show(chat_id, *ainame_view(), query=query)
        return True
    if cmd in {"ainame:on", "ainame:off"}:
        run_admin_action(chat_id, query.get("from") or {"id": user_id}, {"action": "ai_naming", "on": cmd.endswith(":on")}, "", query)
        return True
    if cmd == "bigfix":
        if not is_owner(user_id):
            show(chat_id, "Ye sirf owner kar sakta hai.", {"inline_keyboard": [BACK_ROW]}, query)
            return True
        cb_answer(query, "🔁 Badi files dekh raha hu...")
        bigfix_view(chat_id, user_id, query)
        return True
    if cmd == "fixn" or cmd.startswith("fixn:"):
        cb_answer(query, "🤖 AI naam soch raha hai...")
        arg = cmd.split(":", 1)[1] if ":" in cmd else ""
        fixnames_view(chat_id, user_id, int(arg) if arg.isdigit() else None, query)
        return True
    if cmd.startswith("op:"):
        parts = cmd.split(":")
        verb, token = (parts + ["", ""])[1:3]
        op = get_op(user_id, token)
        if not op:
            show(chat_id, "⌛ Ye button purana ho gaya. Dobara bolo / dabao.", {"inline_keyboard": [BACK_ROW]}, query)
            return True
        if verb == "n":
            clear_op(user_id)
            kb = []
            if op.get("next_after") is not None:
                kb.append([{"text": f"⏭️ Inhe chhodo, agle {FIX_BATCH}", "callback_data": f"ad:fixn:{int(op['next_after'])}"}])
            show(chat_id, "❌ Theek hai, kuch nahi badla.", {"inline_keyboard": kb + [BACK_ROW]}, query)
        elif verb == "y":
            run_op(query, chat_id, user_id, op)
        elif verb == "s" and op.get("kind") == "search":
            clear_op(user_id)
            smart_reply(chat_id, query.get("from") or {"id": user_id}, str(op.get("q") or ""), searched=True)
        elif verb == "p" and op.get("kind") == "rename_pick" and len(parts) > 3 and parts[3].isdigit():
            rows = supabase("books", "GET", query=f"?id=eq.{int(parts[3])}&select=id,title&limit=1")
            if not rows:
                show(chat_id, "😕 Ye file ab nahi hai.", {"inline_keyboard": [BACK_ROW]}, query)
            else:
                preview_rename(chat_id, user_id, [(rows[0]["id"], rows[0].get("title"),
                                                   new_name_for(str(rows[0].get("title") or ""), op["find"], op["to"]))], query)
        elif verb == "c" and op.get("kind") == "setcat_pick" and len(parts) > 3 and parts[3].isdigit():
            rows = supabase("books", "GET", query=f"?id=eq.{int(parts[3])}&select=id,title&limit=1")
            if not rows:
                show(chat_id, "😕 Ye file ab nahi hai.", {"inline_keyboard": [BACK_ROW]}, query)
            else:
                slug = str(op.get("cat") or "")
                ask_confirm(chat_id, user_id, f"🗂️ Category badalni hai?\n\n📄 {str(rows[0].get('title') or '')[:70]}\n→ {cat_emoji(slug)} {cat_label(slug)}",
                            {"kind": "setcat", "ids": [int(rows[0]["id"])], "cat": slug}, query)
        elif verb == "a" and op.get("kind") == "rename_pick":
            ids = ",".join(str(int(x)) for x in op.get("ids") or [])
            rows = supabase("books", "GET", query=f"?id=in.({ids})&select=id,title") if ids else []
            items = [(r["id"], r.get("title"), new_name_for(str(r.get("title") or ""), op["find"], op["to"])) for r in rows
                     if str(op["find"]).lower() in str(r.get("title") or "").lower()]
            preview_rename(chat_id, user_id, items, query)
        else:
            show(chat_id, "⌛ Ye button purana ho gaya. Dobara bolo / dabao.", {"inline_keyboard": [BACK_ROW]}, query)
        return True
    return False



PUBLIC_HOME = "https://" + (os.environ.get("PUBLIC_URL") or os.environ.get("VERCEL_PROJECT_PRODUCTION_URL")
                            or "filesbull.vercel.app").replace("https://", "").replace("http://", "").strip("/") + "/"
ALLOWED_UPDATES = ["message", "channel_post", "callback_query"]


def webhook_url() -> str:
    # Always register the production domain over https, even if a preview URL was opened.
    try:
        req_host = request.host
    except RuntimeError:
        req_host = ""
    host = os.environ.get("PUBLIC_URL") or os.environ.get("VERCEL_PROJECT_PRODUCTION_URL") or req_host or "filesbull.vercel.app"
    host = host.replace("https://", "").replace("http://", "").strip("/")
    return f"https://{host}/api/webhook"


def webhook_fingerprint() -> str:
    # Changes when the URL, the secret (WEBHOOK_SECRET / BOT_TOKEN) or the update types change.
    return hashlib.sha256(f"{webhook_url()}|{WEBHOOK_SECRET}|{','.join(ALLOWED_UPDATES)}".encode("utf-8")).hexdigest()[:16]


def commands_fingerprint() -> str:
    return hashlib.sha256(json.dumps([USER_MENU, ADMIN_MENU, sorted(ADMIN_USER_IDS)], ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def _setting_or_none(key: str) -> Optional[str]:
    try:
        return get_setting(key) if (SUPABASE_URL and SUPABASE_KEY) else None
    except Exception:
        return None


def ensure_webhook(force: bool = False) -> Dict[str, Any]:
    """Idempotent and cheap: one getWebhookInfo; setWebhook only when something is wrong.
    Called by GET / (GitHub Actions keepalive after every push + every 10 min) and 🔄 Bot Restart."""
    info = telegram("getWebhookInfo", {}).get("result", {}) or {}
    want = webhook_url()
    now = int(time.time())
    pending = int(info.get("pending_update_count") or 0)
    err_date = int(info.get("last_error_date") or 0)
    before = {"url_ok": info.get("url") == want, "pending_updates": pending,
              "last_error": info.get("last_error_message"),
              "last_error_min_ago": (now - err_date) // 60 if err_date else None}
    reasons = []
    if force:
        reasons.append("manual restart")
    if info.get("url") != want:
        reasons.append("webhook url missing/wrong")
    fp = webhook_fingerprint()
    if _setting_or_none("webhook_fp") != fp:
        reasons.append("secret/settings changed or not recorded")
    if sorted(info.get("allowed_updates") or []) != sorted(ALLOWED_UPDATES):
        reasons.append("update types")
    if pending and err_date and now - err_date < 3600:
        # Telegram backs off after failed deliveries and holds every later message behind them;
        # setWebhook makes it retry right away.
        reasons.append("stuck: delivery errors with pending updates")
    action = "none"
    if reasons:
        telegram("setWebhook", {"url": want, "secret_token": WEBHOOK_SECRET, "allowed_updates": ALLOWED_UPDATES})
        action = "re-set (" + ", ".join(reasons) + ")"
        try:
            set_setting("webhook_fp", fp)
        except Exception:
            log_error("save webhook_fp")
    cfp = commands_fingerprint()
    commands = "unchanged"
    if reasons or _setting_or_none("commands_fp") != cfp:
        set_command_menu()
        commands = "set"
        try:
            set_setting("commands_fp", cfp)
        except Exception:
            log_error("save commands_fp")
    return {"action": action, "commands": commands, "before": before}


@app.get("/")
def health() -> Any:
    # Opening the site (or the GitHub Actions keepalive) checks the webhook and fixes it only if
    # it is wrong — safe to call any number of times.
    registered = None
    heal: Dict[str, Any] = {}
    t_health = time.monotonic()
    _REQ.clear()
    try:
        # the settings the keepalive work below reads, in one round trip
        prefetch_settings(["webhook_fp", "commands_fp", "ainame_queue", "logfill_state", BIG_QUEUE_KEY, ROUTER_KEY, "ai_keys"])
    except Exception:
        pass
    if BOT_TOKEN:
        try:
            heal = ensure_webhook(force=request.args.get("restart") == "1")
            registered = True
        except Exception as exc:
            log_error("setup")
            registered = False
            heal = {"error": type(exc).__name__ + ": " + str(exc)[:120].replace(BOT_TOKEN, "***")}
    _REQ_T0[0] = time.monotonic()
    try:
        names = process_name_queue(budget=6.0)   # keepalive (every 10 min) retries AI names that failed at save time
    except Exception as exc:
        log_error("name_queue")
        names = {"error": type(exc).__name__}
    try:
        flush_logs()        # ✏️ renames done by the retry go to the log too
    except Exception:
        log_error("flush_logs")
    try:
        # 📜 a log backfill that stopped half way (function time over / lost kick) continues on the keepalive
        lf = logfill_state()
        if logfill_active(lf) and time.time() - float(lf.get("last_ts") or 0) > 60:
            lf = logfill_step(budget=max(2.5, 8.0 - req_elapsed()))
            logfill_notify_done(lf)
            if logfill_active(lf) and not lf.get("_busy"):
                logfill_kick()
    except Exception:
        log_error("logfill keepalive")
    _REQ.clear()
    if request.args.get("full") == "1":
        status = setup_status()     # every table, channel, AI key and diagnostics check (slow: ~15–20 s)
        try:
            status["category_column"] = "ok" if has_category_col() else "missing (catmap fallback)"
        except Exception:
            pass
    else:
        status = light_status()     # quick: keepalive + the essentials; full report at /?full=1
    status["webhook_registered"] = registered
    status["self_heal"] = heal
    status["ai_name_retry"] = names
    status["ms"] = int((time.monotonic() - t_health) * 1000)
    return jsonify({"ok": True, "service": "telegram-book-bot", "status": status})


def light_status() -> Dict[str, Any]:
    """The quick health page (GET /): env names, database reachable, AI on/off, big-file rename, keep-warm.
    Never secrets. Everything else (tables, channels, per-key checks, counts) is at /?full=1."""
    st: Dict[str, Any] = {"mode": "light (full report: /?full=1)",
                          "build": os.environ.get("VERCEL_GIT_COMMIT_SHA", "")[:7] or "local",
                          "env": {n: bool(v) for n, v in (("BOT_TOKEN", BOT_TOKEN), ("SUPABASE_URL", SUPABASE_URL),
                                                          ("SUPABASE_SERVICE_ROLE_KEY", SUPABASE_KEY),
                                                          ("ADMIN_USER_IDS", ADMIN_USER_IDS), ("GROQ_API_KEY", GROQ_API_KEY),
                                                          ("TG_API_ID", TG_API_ID), ("TG_API_HASH", TG_API_HASH),
                                                          ("GH_TOKEN", GH_DISPATCH_TOKEN))},
                          "instance": {"age_s": int(time.time() - _INST["born"]), "requests": _INST["reqs"],
                                       "boot_ms": _BOOT_MS[0], "http_conns": dict(_POOL_STATS)}}
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            prefetch_settings([ROUTER_KEY, "ai_keys", BIG_QUEUE_KEY, "ai_naming", "ub:sess", "ub:state", "ub:paused", "ub:login"])
            supabase("bot_settings", "GET", query="?select=key&limit=1")
            st["database"] = "ok"
        except Exception as exc:
            st["database"] = "error: " + str(exc)[:120]
    else:
        st["database"] = "not configured"
    try:
        st["ai"] = ("on (" + ai_provider() + ")") if ai_provider() else "off"
        st["ai_naming"] = "on" if ai_naming_on() else "off"
    except Exception:
        log_error("light ai")
    try:
        if bigfile_ready():
            bigfile_sweep()
        bst = bigfile_status()
        st["bigfile_rename"] = bst["state"]
        st["bigfile"] = bst
    except Exception:
        log_error("bigfile status")
    try:
        _ubi = ub_health_info()
        st["userbot"] = _ubi["state"]            # not_logged_in | ready | paused | error
        st["userbot_info"] = _ubi
    except Exception:
        log_error("userbot status")
    return st


def diagnostics() -> Dict[str, Any]:
    """Counts and per-channel bot-admin checks for the health page. No IDs of people, no secrets."""
    d: Dict[str, Any] = {"owner_ids_in_env": len(ADMIN_USER_IDS)}
    try:
        d["username"] = bot_info().get("username")
    except Exception:
        pass
    try:
        d["subadmins"] = count_rows("admins", col="user_id")
    except Exception as exc:
        d["subadmins"] = "error: " + str(exc)[:60]
    try:
        reqs = required_channels()
        d["join_gate"] = "on" if reqs else "off (no required channels saved)"
        d["required_channels"] = []
        for r in reqs:
            try:
                ok = bot_member(r["chat_id"]).get("status") in {"administrator", "creator"}
            except Exception:
                ok = False
            d["required_channels"].append({"title": r.get("title"), "bot_is_admin": ok,
                                           "has_join_link": bool(r.get("invite_link") or r.get("username")),
                                           "added_at": r.get("created_at")})
    except Exception as exc:
        d["required_channels"] = "error: " + str(exc)[:60]
    try:
        srcs = source_channels()
        d["source_channels"] = len(srcs)
        not_admin = [r.get("title") for r in srcs if not bot_admin_in(r["chat_id"])]
        d["source_channels_bot_admin"] = len(srcs) - len(not_admin)
        # Not an error: these work for forwarded/already-indexed files; only auto-save of new
        # posts needs the bot to be admin (Telegram sends channel posts only to admins).
        d["source_channels_bot_not_admin"] = not_admin
        d["source_channels_status"] = [{"title": r.get("title"),
                                        "auto_save": "on" if r.get("title") not in not_admin else AUTO_SAVE_OFF}
                                       for r in srcs]
    except Exception as exc:
        d["source_channels"] = "error: " + str(exc)[:60]
    d["backup_channel_set"] = bool(get_setting("backup_chat_id"))
    try:
        if d["backup_channel_set"]:
            d["files_not_backed_up"] = count_rows("books", backup_pending_query(get_setting("backup_chat_id")))
    except Exception:
        pass
    d["log_chat_set"] = bool(get_setting("log_chat_id"))
    try:
        _ls = log_chat_status()
        d["log_chat_ok"] = _ls.get("ok") if _ls else None
        d["log_chat_last_error"] = None if not _ls or _ls.get("ok") else _ls.get("reason")
    except Exception:
        pass
    try:
        _lf = logfill_state()
        d["log_backfill"] = ({"active": logfill_active(_lf), "sent": _lf.get("sent_files", 0), "total": _lf.get("total", 0),
                              "done": bool(_lf.get("done"))} if _lf else "never")
    except Exception:
        pass
    try:
        d["named_by"] = named_by_counts()          # clean / local_text = 0 tokens; ai = one small call; fallback
        d["understood_by"] = understood_counts()   # user msgs: ai = one small call; cache / exact = 0 tokens; local_fallback
        d["categories_auto"] = sorted(cat_registry())
        _REQ.pop("_catreg", None)
    except Exception:
        pass
    return d


def setup_status() -> Dict[str, Any]:
    """Names-only setup check, so a missing Vercel variable or a broken webhook is
    visible without opening logs. Never returns secret values."""
    status: Dict[str, Any] = {
        "env": {
            "BOT_TOKEN": bool(BOT_TOKEN),
            "SUPABASE_URL": bool(SUPABASE_URL),
            "SUPABASE_SERVICE_ROLE_KEY": bool(SUPABASE_KEY),
            "ADMIN_USER_IDS": bool(ADMIN_USER_IDS),
            "OPENAI_API_KEY": bool(OPENAI_API_KEY),
            "GROQ_API_KEY": bool(GROQ_API_KEY),
            "GROQ_API_KEY_2": bool(GROQ_API_KEY_2),
            "GROQ_API_KEY_3": bool(GROQ_API_KEY_3),
            "GEMINI_API_KEY": bool(ENV_AI_KEYS.get("gemini")),
            "MISTRAL_API_KEY": bool(ENV_AI_KEYS.get("mistral")),
            "OPENROUTER_API_KEY": bool(ENV_AI_KEYS.get("openrouter")),
            "NVIDIA_API_KEY": bool(ENV_AI_KEYS.get("nvidia")),
            "TG_API_ID": bool(TG_API_ID),
            "TG_API_HASH": bool(TG_API_HASH),
            "GH_TOKEN": bool(GH_DISPATCH_TOKEN),
        },
        "ai": ("on (" + ai_provider() + ")") if ai_provider() else "off",
        "ai_keys_in_bot": len(db_ai_keys()),
        "ai_naming": "on" if ai_naming_on() else "off",
        "admin_commands": "on",
    }
    try:  # how new files got their names: clean + local_text cost 0 tokens, ai = one small call each
        status["named_by"] = named_by_counts()
    except Exception:
        pass
    status["ai_understand"] = "on (AI first, cache 24 h, local fallback)" if AI_UNDERSTAND else "off (local parser)"
    try:  # >20 MB file names through the GitHub Action: ready / missing_secrets, trigger, queue counts
        bq = bigfile_sweep() if bigfile_ready() else None
        bst = bigfile_status()
        status["bigfile_rename"] = bst["state"]
        status["bigfile"] = bst
    except Exception:
        log_error("bigfile status")
    try:  # multi-provider router: LIVE + backup order, per provider key count / status / calls today (never keys)
        status["ai_providers"] = ai_providers_summary()
    except Exception:
        log_error("ai_providers")
    try:  # how user messages were understood: ai = one small call; cache + exact = 0 tokens; local_fallback = no AI
        status["understood_by"] = understood_counts()
        prune_understand_cache()
    except Exception:
        pass
    if GROQ_KEYS:
        # Key check without spending tokens (lists models). Never shows a key.
        checks_ai = {}
        for i, key in enumerate(GROQ_KEYS):
            name = "GROQ_API_KEY" if i == 0 else f"GROQ_API_KEY_{i + 1}"
            try:
                http_json("https://api.groq.com/openai/v1/models", "GET", None, {"Authorization": f"Bearer {key}"}, timeout=5)
                checks_ai[name] = "ok"
            except Exception as exc:
                checks_ai[name] = "error: " + str(exc)[:120].replace(key, "***")
        status["ai_key_check"] = checks_ai
    if BOT_TOKEN:
        try:
            info = telegram("getWebhookInfo", {}).get("result", {})
            status["webhook"] = {
                "url": info.get("url"),
                "pending_updates": info.get("pending_update_count"),
                "last_error": info.get("last_error_message"),
                "last_error_date": info.get("last_error_date"),
            }
        except Exception as exc:
            status["webhook"] = {"error": type(exc).__name__ + ": " + str(exc)[:120].replace(BOT_TOKEN, "***")}
    if SUPABASE_URL and SUPABASE_KEY:
        # One cheap read per table/column the code uses; "missing" means schema.sql /
        # migrate_advanced.sql has not been run in the Supabase SQL Editor yet.
        checks = {
            "books": "id,content_text",
            "books.backup_columns": "backup_chat_id,backup_message_id",
            "channels": "chat_id",
            "required_channels": "chat_id",
            "admins": "user_id",
            "bot_settings": "key",
            "delivery_logs": "id",
        }
        tables: Dict[str, str] = {}
        for name, cols in checks.items():
            try:
                supabase(name.split(".")[0], "GET", query=f"?select={cols}&limit=1")
                tables[name] = "ok"
            except Exception as exc:
                tables[name] = "missing/error: " + str(exc)[:80]
        status["database"] = "ok" if all(v == "ok" for v in tables.values()) else "incomplete"
        status["tables"] = tables
        try:
            supabase("bot_users", "GET", query="?select=user_id&limit=1")
            status["optional_tables"] = {"bot_users": "ok"}
        except Exception:
            status["optional_tables"] = {"bot_users": "missing (users saved in bot_settings until the SQL is run)"}
        status["bot"] = diagnostics()
    else:
        status["database"] = "not configured (set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY)"
    return status


# ================================================================ content-based naming + subject categories (Oct 26 2026)
# Every new file: read the first pages (PDF text layer / .txt / .docx), one AI call names it from the CONTENT
# and picks a subject. Only the stored name + category change — never the file itself. Original file name and
# caption stay stored (and searchable). AI fail → cleaned file name + local subject rules, retried later.

SUBJECTS = [  # slug, label, emoji, strong words (title / file name / caption / content)
    ("history", "History", "🏛️", ["history", "itihas", "इतिहास", "mughal", "मुगल", "मुग़ल", "akbar", "अकबर", "harappa",
                                  "हड़प्पा", "maurya", "मौर्य", "gupta", "गुप्त", "sultanate", "सल्तनत", "vedic", "वैदिक",
                                  "1857", "freedom struggle", "स्वतंत्रता संग्राम", "ancient india", "प्राचीन भारत",
                                  "medieval", "मध्यकालीन", "आधुनिक भारत", "modern india", "gandhi", "गांधी", "babur", "बाबर",
                                  "aurangzeb", "औरंगजेब", "shivaji", "शिवाजी", "buddha", "बुद्ध", "mahavir", "महावीर",
                                  "british rule", "east india company", "ईस्ट इंडिया कंपनी"]),
    ("polity", "Polity", "⚖️", ["polity", "politics", "राजव्यवस्था", "राजनीति", "संविधान", "constitution", "संसद",
                               "parliament", "अनुच्छेद", "fundamental rights", "मौलिक अधिकार", "राष्ट्रपति", "president",
                               "governor", "राज्यपाल", "panchayat", "पंचायत", "judiciary", "न्यायपालिका", "लोकसभा",
                               "lok sabha", "राज्यसभा", "rajya sabha", "preamble", "प्रस्तावना", "laxmikanth"]),
    ("geography", "Geography", "🌍", ["geography", "bhugol", "भूगोल", "नदी", "नदियों", "नदियाँ", "river", "rivers",
                                     "mountain", "पर्वत", "climate", "जलवायु", "soil", "मिट्टी", "मृदा", "ocean", "महासागर",
                                     "continent", "महाद्वीप", "monsoon", "मानसून", "solar system", "सौरमंडल", "latitude",
                                     "अक्षांश", "volcano", "ज्वालामुखी", "earth", "पृथ्वी", "map", "मानचित्र"]),
    ("economics", "Economics", "💰", ["economics", "economy", "अर्थव्यवस्था", "अर्थशास्त्र", "budget", "बजट", "gdp",
                                     "inflation", "मुद्रास्फीति", "महंगाई", "five year plan", "पंचवर्षीय", "rbi",
                                     "reserve bank", "रिजर्व बैंक", "gst", "banking", "बैंकिंग", "niti aayog", "नीति आयोग"]),
    ("science", "Science", "🔬", ["science", "vigyan", "विज्ञान", "physics", "भौतिकी", "chemistry", "रसायन", "biology",
                                 "जीव विज्ञान", "vitamin", "विटामिन", "कोशिका", "atom", "परमाणु", "acid", "अम्ल",
                                 "human body", "मानव शरीर", "disease", "रोग", "newton", "न्यूटन", "electricity", "विद्युत"]),
    ("static_gk", "Static GK", "📌", ["static gk", "स्थैतिक", "books and authors", "पुस्तकें और लेखक", "पुस्तके और लेखक",
                                     "important days", "महत्वपूर्ण दिवस", "awards", "पुरस्कार", "first in india",
                                     "national symbols", "राष्ट्रीय प्रतीक", "dance", "नृत्य", "sports", "खेल",
                                     "general knowledge", "सामान्य ज्ञान", "lucent", "ल्यूसेंट", "gk", "जीके",
                                     "one liner", "वन लाइनर"]),
    ("current_affairs", "Current Affairs", "📰", ["current affairs", "करंट अफेयर्स", "समसामयिकी", "सम सामयिकी",
                                                 "current affair", "monthly ca", "daily ca", "करेंट अफेयर्स"]),
    ("maths", "Maths", "➗", ["maths", "math", "mathematics", "ganit", "गणित", "arithmetic", "अंकगणित", "percentage",
                             "प्रतिशत", "profit and loss", "लाभ और हानि", "लाभ हानि", "ratio", "अनुपात", "algebra",
                             "बीजगणित", "geometry", "ज्यामिति", "simplification", "सरलीकरण", "number system",
                             "संख्या पद्धति", "time and work", "समय और कार्य", "simple interest", "साधारण ब्याज",
                             "mensuration", "क्षेत्रमिति", "trigonometry", "त्रिकोणमिति", "rs aggarwal", "rakesh yadav"]),
    ("reasoning", "Reasoning", "🧩", ["reasoning", "रीजनिंग", "तर्कशक्ति", "तार्किक", "analogy", "सादृश्यता",
                                     "coding decoding", "कोडिंग डिकोडिंग", "blood relation", "रक्त संबंध", "direction sense",
                                     "दिशा ज्ञान", "syllogism", "न्याय निगमन", "puzzle", "पहेली", "non verbal", "अशाब्दिक",
                                     "series", "श्रृंखला"]),
    ("hindi", "Hindi", "🇮🇳", ["hindi grammar", "हिंदी व्याकरण", "हिन्दी व्याकरण", "व्याकरण", "vyakaran", "संधि", "समास",
                              "अलंकार", "मुहावरे", "लोकोक्ति", "पर्यायवाची", "विलोम", "हिंदी साहित्य", "हिन्दी साहित्य",
                              "तत्सम", "छंद", "कारक", "सर्वनाम"]),
    ("english", "English", "🔤", ["english grammar", "vocabulary", "synonyms", "antonyms", "idioms", "phrases", "tense",
                                 "tenses", "active passive", "narration", "preposition", "one word substitution",
                                 "spelling", "comprehension", "articles", "modals", "noun", "pronoun", "adjective"]),
    ("computer", "Computer", "💻", ["computer", "कंप्यूटर", "कम्प्यूटर", "ms office", "excel", "internet", "इंटरनेट",
                                   "ccc", "o level", "software", "hardware", "operating system", "keyboard shortcut"]),
    ("pyq", "PYQ / Previous Papers", "📑", ["pyq", "previous year", "previous years", "पिछले वर्ष", "पिछले वर्षों",
                                           "solved paper", "solved papers", "question paper", "प्रश्न पत्र", "हल प्रश्न पत्र",
                                           "पूर्व वर्ष", "papers"]),
    ("mock", "Mock / Practice", "📝", ["mock", "mock test", "मॉक", "practice set", "प्रैक्टिस सेट", "प्रैक्टिस",
                                      "test series", "टेस्ट सीरीज", "model paper", "मॉडल पेपर", "मॉडल पेपर्स",
                                      "practice book", "अभ्यास"]),
    ("other", "Other", "📁", []),
]
SUBJECT_LABEL = {s: l for s, l, _, _ in SUBJECTS}
SUBJECT_EMOJI = {s: e for s, _, e, _ in SUBJECTS}
SUBJECT_ALIASES = {
    "history": "history", "itihas": "history", "polity": "polity", "politics": "polity", "constitution": "polity",
    "civics": "polity", "geography": "geography", "economics": "economics", "economy": "economics",
    "science": "science", "physics": "science", "chemistry": "science", "biology": "science",
    "static gk": "static_gk", "gk": "static_gk", "general knowledge": "static_gk", "general awareness": "static_gk",
    "gk/gs": "static_gk", "general studies": "static_gk", "current affairs": "current_affairs",
    "maths": "maths", "math": "maths", "mathematics": "maths", "quant": "maths", "quantitative aptitude": "maths",
    "reasoning": "reasoning", "hindi": "hindi", "english": "english", "computer": "computer",
    "pyq": "pyq", "previous papers": "pyq", "previous papers/pyq": "pyq", "previous year papers": "pyq",
    "pyq / previous papers": "pyq", "mock": "mock", "mock/practice": "mock", "practice": "mock",
    "mock / practice": "mock", "other": "other", "unknown": "other",
}
CAT_TAG = "catg_"
NAME_JUNK_WORDS = {"join", "joinus", "telegram", "channel", "official", "download", "free", "subscribe", "follow",
                   "whatsapp", "youtube", "share", "link", "group", "admin", "pdf", "file", "copy", "final", "new"}


def norm_subject(value: Any) -> str:
    v = re.sub(r"\s+", " ", str(value or "").strip().lower())
    if v in SUBJECT_ALIASES:
        return SUBJECT_ALIASES[v]
    for slug, label, _, _ in SUBJECTS:
        if v == label.lower() or v == slug:
            return slug
    return ""


def _hits(words: List[str], text: str) -> int:
    n = 0
    for w in words:
        if re.search(r"[\u0900-\u097F]", w):
            if w in text:
                n += 1
        elif re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", text):
            n += 1
    return n


def classify_local(name_text: str, content: str = "") -> str:
    """Free subject guess: name words count 3x, content words 1x. PYQ / Mock win only when the NAME says so."""
    name = (name_text or "").lower().replace("_", " ")
    body = (content or "")[:6000].lower()
    for slug in ("pyq", "mock"):
        words = [w for s, _, _, ws in SUBJECTS if s == slug for w in ws]
        if _hits(words, name):
            return slug
    best, score = "other", 0
    for slug, _, _, words in SUBJECTS:
        if slug in ("pyq", "mock", "other"):
            continue
        s = 3 * _hits(words, name) + min(6, _hits(words, body))
        if s > score:
            best, score = slug, s
    return best


def is_junk_name(stem: str) -> bool:
    """Random / meaningless file names like hjskjd, IMG_2023, telegram_AgAD.., 12345."""
    s = re.sub(r"[_\-.\s]+", " ", stem or "").strip()
    if not s or re.search(r"[\u0900-\u097F]", s):
        return not s
    words = re.findall(r"[a-z]+", s.lower())
    if not words or re.fullmatch(r"[\d\s]+", s):
        return True
    if s.lower().startswith(("telegram ", "img ", "doc ", "document", "file ", "scan", "cam scanner", "camscanner")):
        return True
    real = [w for w in words if len(w) >= 3 and re.search(r"[aeiou]", w) and not re.search(r"[^aeiou]{5,}", w)]
    if not real:
        return True
    # one short made-up token like "hjskjd" (few vowels)
    if len(words) == 1 and len(re.findall(r"[aeiou]", words[0])) / max(1, len(words[0])) < 0.2:
        return True
    return False


def strip_people(title: str) -> str:
    """Remove uploader / channel / person names and junk from a name."""
    t = str(title or "")
    t = re.sub(r"https?://\S+|t\.me/\S+|www\.\S+", " ", t, flags=re.I)
    t = re.sub(r"@\w+", " ", t)
    t = re.sub(r"#\w+", " ", t)
    t = re.sub(r"\s+(?:by|from|uploaded by|shared by|credit|credits|cr)\s*[:\-]?\s+[^\n|,()\[\]]+$", " ", t, flags=re.I)
    t = re.sub(r"\s+द्वारा\s+[^\n|,()\[\]]+$", " ", t)
    t = re.sub(r"[\U0001F000-\U0001FFFF\u2600-\u27BF\uFE0F\u200d❤]+", " ", t)
    t = re.sub(r"\.(pdf|docx?|txt|pptx?|zip|rar|mp4|epub)\b", " ", t, flags=re.I)
    t = re.sub(r"[_|]+", " ", t)
    words = [w for w in t.split() if w.lower().strip(".,:;!-") not in NAME_JUNK_WORDS]
    t = " ".join(words)
    t = re.sub(r"(?<![\w])\d{5,}(?![\w])", " ", t)          # long number-only codes (not years)
    t = re.sub(r"\s+", " ", t).strip(" -–—:.,;|")
    return t[:100]


def local_clean_name(caption: str, file_name: str) -> str:
    stem = os.path.splitext(str(file_name or ""))[0]
    stem = re.sub(r"_(\d)$", "", stem)
    first = ""
    for line in str(caption or "").splitlines():
        c = strip_people(line)
        if len(re.findall(r"[A-Za-z\u0900-\u097F]", c)) >= 3:
            first = c
            break
    name = first or strip_people(stem.replace("_", " ").replace("-", " "))
    return name or clean_text(stem) or "Untitled"


# ---- reading the file (Telegram bots may download up to 20 MB)
TG_FILE_LIMIT = 20 * 1024 * 1024
REUPLOAD_LIMIT = 15 * 1024 * 1024


def download_file(file_id: Optional[str], file_size: Optional[int]) -> Optional[bytes]:
    """The file's bytes (≤20 MB, Telegram's limit for bots); None when bigger / unavailable."""
    if not file_id or not BOT_TOKEN or (file_size and int(file_size) > TG_FILE_LIMIT):
        return None
    try:
        path = (telegram("getFile", {"file_id": file_id}).get("result") or {}).get("file_path")
        if not path:
            return None
        with urllib.request.urlopen(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{path}", timeout=6) as resp:
            return resp.read(TG_FILE_LIMIT + 1)
    except Exception:
        return None


def text_from_bytes(data: Optional[bytes], file_name: str, mime: str = "", budget: float = 2.5) -> Dict[str, str]:
    """{"excerpt": first ~2-3 pages, "content": more pages for search}. Empty for scanned PDFs (no OCR)."""
    out = {"excerpt": "", "content": ""}
    if not data:
        return out
    low = (file_name or "").lower()
    started = time.monotonic()
    try:
        if low.endswith(".pdf") or (mime or "").endswith("pdf") or data[:5] == b"%PDF-":
            reader = PdfReader(io.BytesIO(data), strict=False)
            chunks: List[str] = []
            for n, page in enumerate(reader.pages[:20]):
                try:
                    chunks.append(page.extract_text() or "")
                except Exception:
                    chunks.append("")
                if n == 2 or (n < 2 and len(" ".join(chunks)) > 2500):
                    out["excerpt"] = clean_text(" ".join(chunks))
                if time.monotonic() - started > budget or sum(len(x) for x in chunks) > 60000:
                    break
            out["content"] = clean_text(" ".join(chunks))[:60000]
            out["excerpt"] = out["excerpt"] or out["content"][:3000]
        elif low.endswith(".txt") or (mime or "").startswith("text/"):
            t = data[:200000].decode("utf-8", "replace")
            out["content"] = clean_text(t)[:60000]
            out["excerpt"] = out["content"][:3000]
        elif low.endswith(".docx"):
            import zipfile
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                xml = z.read("word/document.xml").decode("utf-8", "replace")
            t = html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"</w:p>", "\n", xml)))
            out["content"] = clean_text(t)[:60000]
            out["excerpt"] = out["content"][:3000]
    except Exception:
        pass
    # a "text layer" of only a few letters (scanned PDF with page numbers) counts as no text
    if len(re.findall(r"[A-Za-z\u0900-\u097F]", out["excerpt"])) < 40:
        out["excerpt"] = ""
    return out


# ---- AI naming
AI_NAME2_SYSTEM = (
    "You name study files for an Indian govt-exam PDF library. Each item has the original file name, the post caption and "
    "an excerpt of the text INSIDE the file (may be empty). Reply ONLY with JSON "
    '{"items":[{"i":1,"title":"...","subject":"...","exam":null,"language":"hi|en|mixed"}]}. '
    "title: a short clean name (2-7 words, max 60 characters) saying what the file really is. The CONTENT wins: if the "
    "file name is random letters or does not match the text inside, name it from the text (e.g. history notes about the "
    "Mughals named hjskjd.pdf -> \"Mughal Empire History Notes\"). Use words that appear in the text, caption or file name "
    "(you may add a subject word like History/Polity/Notes/इतिहास). Write it in the language/script of the content "
    "(Hindi content -> Devanagari). Keep a real book/publisher name, exam, class, part or year only if it is written there. "
    "REMOVE person, uploader and channel names (\"History by Aarav\" -> \"History\"), @usernames, links, 'join', promo text, "
    "emojis, file extensions, codes and random letters. Never invent a book title or author. "
    "subject: exactly one of History, Polity, Geography, Economics, Science, Static GK, Current Affairs, Maths, Reasoning, "
    "Hindi, English, Computer, PYQ, Mock, Admit Card, Answer Key, Result, Cut Off, Syllabus, Notification, Typing, "
    "Data Analysis, Other (PYQ = previous year papers, Mock = mock tests / practice sets; Hindi / "
    "English only for language grammar/literature, not for a book merely written in Hindi). If none of these fits but "
    "the file is clearly a new kind of file, give a NEW short English category of 1-3 words (e.g. Physical Test, "
    "Interview); otherwise Other.")


def title_supported2(title: str, source: str) -> bool:
    """Relaxed guard: every real word must come from the caption, file name OR the text inside (spelling fixes allowed);
    subject words (History, इतिहास, Notes...) are always fine. Invented names fail."""
    extra = set()
    for slug, label, _, words in SUBJECTS:
        extra.update(re.findall(r"[a-z0-9]+|[\u0900-\u097F]+", (label + " " + slug).lower()))
    extra.update({"इतिहास", "राजव्यवस्था", "भूगोल", "अर्थव्यवस्था", "विज्ञान", "गणित", "रीजनिंग", "हिंदी", "अंग्रेजी",
                  "कंप्यूटर", "नोट्स", "प्रश्न", "उत्तर", "सामान्य", "ज्ञान", "notes", "empire", "practice", "papers",
                  "current", "affairs", "static", "previous", "mock", "pyq", "mcq", "mcqs"})
    import difflib
    src = normalize(source)
    src_words = set(re.findall(r"[a-z0-9]+|[\u0900-\u097F]+", src))
    for w in re.findall(r"[a-z0-9]+|[\u0900-\u097F]+", (title or "").lower()):
        if w in GENERIC_TITLE_WORDS or w in extra or len(w) <= 2 or w in src_words:
            continue
        if not w.isdigit() and w in src:
            continue
        if w.isdigit():
            return False
        if difflib.get_close_matches(w, list(src_words)[:4000], n=1, cutoff=0.8):
            continue
        return False
    return True


_LAST_NAME_RAW = [""]


def ai_name_items(items: List[Dict[str, Any]], cap: float = 9.0, excerpt_chars: int = 1500) -> Optional[Dict[int, Dict[str, str]]]:
    """items [{"i","caption","file","text"}] -> {i: {"title","subject"}} (guarded). None = the AI call itself failed."""
    payload = [{"i": x["i"], "file": str(x.get("file") or "")[:150],
                "caption": re.sub(r"\s+", " ", str(x.get("caption") or ""))[:300],
                "text": re.sub(r"\s+", " ", str(x.get("text") or ""))[:excerpt_chars]} for x in items]
    extra = [str(r.get("label")) for r in cat_registry().values() if r.get("label")]
    system = AI_NAME2_SYSTEM + (" Categories the library already has: " + ", ".join(extra[:30]) + "." if extra else "")
    content = ai_complete(system, json.dumps(payload, ensure_ascii=False),
                          max_tokens=min(2000, 80 + 60 * len(items)), cap=cap)
    _LAST_NAME_RAW[0] = (content or "")[:1500]
    if not content:
        return None
    try:
        data = json.loads(content[content.find("{"): content.rfind("}") + 1])
    except Exception:
        return None
    got = data.get("items") or data.get("titles") if isinstance(data, dict) else None
    by_i = {x["i"]: x for x in items}
    out: Dict[int, Dict[str, str]] = {}
    for t in got if isinstance(got, list) else []:
        if not isinstance(t, dict) or t.get("i") not in by_i:
            continue
        x = by_i[t["i"]]
        title = clean_ai_title(strip_people(str(t.get("title") or "")))
        source = " ".join(str(x.get(k) or "") for k in ("caption", "file", "old", "text"))
        subj = norm_category(t.get("subject"), create=True) if t.get("subject") else ""
        if len(title) >= 3 and title_supported2(title, source):
            out[int(t["i"])] = {"title": title, "subject": subj}
        elif subj:
            out[int(t["i"])] = {"title": "", "subject": subj}
    return out


# ---- category storage: books.category column when it exists, else bot_settings "catmap" {id: slug}
_CATCOL: Dict[str, Any] = {"t": 0.0, "v": None}


def has_category_col() -> bool:
    if _CATCOL["v"] is not None and time.time() - _CATCOL["t"] < 600:
        return bool(_CATCOL["v"])
    try:
        supabase("books", "GET", query="?select=category&limit=1")
        ok = True
    except Exception:
        ok = False
    _CATCOL.update(t=time.time(), v=ok)
    return ok


def catmap() -> Dict[str, str]:
    try:
        return json.loads(get_setting("catmap") or "{}")
    except Exception:
        return {}


def save_categories(cats: Dict[int, str]) -> None:
    cats = {int(k): v for k, v in cats.items() if v}
    if not cats:
        return
    if has_category_col():
        for bid, slug in cats.items():
            try:
                supabase("books", "PATCH", {"category": slug}, f"?id=eq.{int(bid)}")
            except Exception:
                log_error("save_category")
        return
    m = catmap()
    m.update({str(k): v for k, v in cats.items()})
    set_setting("catmap", json.dumps(m, separators=(",", ":")))


def with_cat_tag(keywords: str, slug: str) -> str:
    k = re.sub(r"\s*" + CAT_TAG + r"[a-z_]+", "", str(keywords or ""))
    return (k + (" " + CAT_TAG + slug if slug else ""))[:4000]


def backup_caption(record: Dict[str, Any]) -> str:
    slug = record.get("_cat") or record.get("category") or ""
    cat = f"\n🗂️ {cat_label(slug)}" if known_category(slug) else ""
    return (f"📄 {record.get('title') or ''}{cat}")[:1000]


def safe_filename(title: str, orig: str) -> str:
    ext = os.path.splitext(orig or "")[1].lower() or ".pdf"
    base = re.sub(r"[\\/:*?\"<>|\n\r\t]+", " ", str(title or "file")).strip()[:80] or "file"
    return base + ext


def telegram_upload(method: str, fields: Dict[str, Any], file_field: str, filename: str, data: bytes) -> Dict[str, Any]:
    """multipart/form-data upload (used to store the backup copy under its clean file name)."""
    boundary = "----filesbull" + hashlib.sha1(os.urandom(8)).hexdigest()
    parts: List[bytes] = []
    for k, v in fields.items():
        if v is None:
            continue
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode("utf-8"))
    if _DIAG["dry"]:
        _DIAG["sent"].append({"ms": int((time.monotonic() - _DIAG["t0"]) * 1000), "m": method + " (upload)"})
        return {"ok": True, "result": {"message_id": 1}}
    fn = filename.replace('"', "'")
    parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; filename=\"{fn}\"\r\n"
                  "Content-Type: application/octet-stream\r\n\r\n").encode("utf-8") + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    req = urllib.request.Request(f"{TG_API}/{method}", data=b"".join(parts), method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}: upload failed") from None


# ---- retry queue for files the AI could not name at save time (bot_settings "ainame_queue")
NAME_RETRIES = 3


def queue_name_retry(book_id: Any) -> None:
    if not book_id:
        return
    try:
        q = json.loads(get_setting("ainame_queue") or "[]")
        if not any(int(x.get("id")) == int(book_id) for x in q):
            q.append({"id": int(book_id), "n": 0})
        set_setting("ainame_queue", json.dumps(q[-200:], separators=(",", ":")))
    except Exception:
        log_error("queue_name_retry")


def process_name_queue(budget: float = 8.0, max_items: int = 10) -> Dict[str, int]:
    """Retry AI naming for queued files (keepalive every 10 min / site open). Uses stored text, no download."""
    stats = {"done": 0, "failed": 0, "left": 0}
    try:
        q = json.loads(get_setting("ainame_queue") or "[]")
    except Exception:
        q = []
    if not q or not ai_provider() or not ai_naming_on():
        stats["left"] = len(q)
        return stats
    started = time.monotonic()
    batch = q[:max_items]
    ids = ",".join(str(int(x["id"])) for x in batch)
    rows = {int(r["id"]): r for r in supabase("books", "GET", query=f"?id=in.({ids})&select=id,title,caption,file_name,"
                                                                     "content_text,keywords,backup_chat_id,backup_message_id")}
    items = [{"i": int(x["id"]), "caption": rows[int(x["id"])].get("caption"), "file": rows[int(x["id"])].get("file_name"),
              "text": str(rows[int(x["id"])].get("content_text") or "")[:1200]} for x in batch if int(x["id"]) in rows]
    got = ai_name_items(items, cap=max(2.0, budget - (time.monotonic() - started)), excerpt_chars=1200) if items else {}
    keep = []
    cats: Dict[int, str] = {}
    for x in q:
        bid = int(x["id"])
        if bid not in rows:
            if x in batch:
                continue
            keep.append(x)
            continue
        if x not in batch:
            keep.append(x)
            continue
        if got is None:
            x["n"] = int(x.get("n") or 0) + 1
            if x["n"] < NAME_RETRIES:
                keep.append(x)
            stats["failed"] += 1
            continue
        res = got.get(bid) or {}
        row = rows[bid]
        slug = res.get("subject") or classify_local(" ".join(str(row.get(k) or "") for k in ("title", "caption", "file_name")),
                                                    str(row.get("content_text") or ""))
        title = res.get("title") or row.get("title")
        cats[bid] = slug
        patch = {"title": str(title)[:500], "keywords": with_cat_tag(clean_text(" ".join(str(v or "") for v in (
            title, row.get("caption"), row.get("file_name")))), slug)}
        try:
            supabase("books", "PATCH", patch, f"?id=eq.{bid}")
            rename_log([[bid, row.get("title"), title]], "🤖 AI (auto, dobara try)")
            if row.get("backup_chat_id") and row.get("backup_message_id"):
                try:
                    telegram("editMessageCaption", {"chat_id": row["backup_chat_id"], "message_id": row["backup_message_id"],
                                                    "caption": backup_caption({"title": title, "_cat": slug})})
                except Exception:
                    pass
            stats["done"] += 1
        except Exception:
            log_error("name_queue_patch")
            keep.append(x)
    save_categories(cats)
    set_setting("ainame_queue", json.dumps(keep, separators=(",", ":")))
    stats["left"] = len(keep)
    return stats


# ---- 📂 Categories (subjects with counts → paginated file list)
CAT_PAGE = 10


def book_category(row: Dict[str, Any], cmap: Dict[str, str]) -> str:
    slug = row.get("category") or cmap.get(str(row.get("id")))
    if not slug:
        m = re.search(CAT_TAG + r"([a-z_]+)", str(row.get("keywords") or ""))
        slug = m.group(1) if m else ""
    if not known_category(slug):
        slug = name_category(" ".join(str(row.get(k) or "") for k in ("title", "file_name"))) or \
            classify_local(" ".join(str(row.get(k) or "") for k in ("title", "file_name")))
        if not known_category(slug):
            slug = "other"
    return slug


def category_rows() -> List[Dict[str, Any]]:
    return [dict(r) for r in view_cached("catrows", _category_rows)]


def _category_rows() -> List[Dict[str, Any]]:
    cols = "id,title,file_name" + (",category" if has_category_col() else "")
    rows = supabase("books", "GET", query=f"?select={cols}&status=eq.indexed&order=id.desc&limit=5000")
    cmap = {} if has_category_col() else catmap()
    for r in rows:
        r["_cat"] = book_category(r, cmap)
    return rows


def categories_view() -> Any:
    rows = category_rows()
    counts: Dict[str, int] = {}
    for r in rows:
        counts[r["_cat"]] = counts.get(r["_cat"], 0) + 1
    if not rows:
        return "🗂️ <b>Categories</b>\n\nAbhi koi file nahi hai.", None
    kb: List[Any] = []
    row: List[Any] = []
    # every category that has files, biggest first, Other last (new categories appear by themselves)
    for slug in sorted(counts, key=lambda s: (s == "other", -counts[s], cat_label(s))):
        row.append({"text": f"{cat_emoji(slug)} {cat_label(slug)} ({counts[slug]})", "callback_data": f"cat:{slug}:1"})
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    return (f"🗂️ <b>Categories</b> — {len(counts)} categories, {len(rows)} files\n\nCategory chuno 👇", {"inline_keyboard": kb})


def category_list_view(slug: str, page: int = 1) -> Any:
    rows = [r for r in category_rows() if r["_cat"] == slug]
    total = len(rows)
    pages = max(1, (total + CAT_PAGE - 1) // CAT_PAGE)
    page = min(max(1, page), pages)
    chunk = rows[(page - 1) * CAT_PAGE: page * CAT_PAGE]
    label = f"{cat_emoji(slug)} {cat_label(slug)}"
    if not chunk:
        return f"{label}\n\nIs category me abhi koi file nahi.", {"inline_keyboard": [[{"text": "🔙 Categories", "callback_data": "user_categories"}]]}
    kb = [[{"text": f"📄 {str(r.get('title') or 'Untitled')[:45]}", "callback_data": f"book:{r['id']}"}] for r in chunk]
    nav = []
    if page > 1:
        nav.append({"text": "⬅️ Previous", "callback_data": f"cat:{slug}:{page - 1}"})
    nav.append({"text": f"{page}/{pages}", "callback_data": "noop"})
    if page < pages:
        nav.append({"text": "Next ➡️", "callback_data": f"cat:{slug}:{page + 1}"})
    kb.append(nav)
    kb.append([{"text": "🔙 Categories", "callback_data": "user_categories"}])
    return f"{label} — {total} files (page {page}/{pages})\n\nJo chahiye us par tap karo 👇", {"inline_keyboard": kb}


# ================================================================ token-saving naming + dynamic categories + log (Oct 26 2026, 6:10 PM)
# Aarav 5:57 PM: a file whose name already says what it is (History, Geo, GS, Result, Data Analysis, SSC GD…) is NOT
# read and gets NO AI call — its name is only cleaned. Unclear names ("new pdf", "my pdf", "pdf by X", hjskjd,
# IMG_123) are read page by page (max 3); the bot stops at the first page where a subject/topic is found locally
# (Hindi + English keyword dictionary, 0 tokens). Only if 3 pages say nothing clear does ONE small AI call
# (≤1200 chars) name it. New kinds of files open new categories by themselves; nothing fits → Other.

EXTRA_KINDS = [  # slug, label, emoji, words — document kinds; their category opens itself on the first such file
    ("admit_card", "Admit Card", "🎫", ["admit card", "admitcard", "admit-card", "hall ticket", "call letter", "प्रवेश पत्र",
                                       "एडमिट कार्ड"]),
    ("answer_key", "Answer Key", "🔑", ["answer key", "answerkey", "answer-key", "answer keys", "उत्तर कुंजी", "आंसर की"]),
    ("result", "Result", "🏆", ["result", "results", "merit list", "रिजल्ट", "परिणाम", "मेरिट लिस्ट"]),
    ("cut_off", "Cut Off", "📉", ["cut off", "cutoff", "cut-off", "कट ऑफ", "कटऑफ"]),
    ("syllabus", "Syllabus", "📋", ["syllabus", "सिलेबस", "पाठ्यक्रम", "exam pattern", "परीक्षा पैटर्न"]),
    ("notification", "Notification", "📢", ["notification", "vacancy", "advertisement", "अधिसूचना", "नोटिफिकेशन",
                                           "विज्ञप्ति", "भर्ती विज्ञापन"]),
    ("typing", "Typing", "⌨️", ["typing", "टाइपिंग", "steno", "स्टेनो"]),
    ("data_analysis", "Data Analysis", "📊", ["data analysis", "data analytics", "data analyst", "डेटा विश्लेषण",
                                              "डाटा विश्लेषण", "डेटा एनालिसिस"]),
]
EXAMS = [  # label, words — an exam name alone is a clear name; with no subject it becomes its own category
    ("SSC GD", ["ssc gd", "sscgd", "एसएससी जीडी", "ssc जीडी"]), ("Agniveer", ["agniveer", "agnipath", "अग्निवीर", "अग्निपथ"]),
    ("NTPC", ["ntpc", "एनटीपीसी"]), ("Group D", ["group d", "ग्रुप डी", "rrc group d"]), ("SSC CGL", ["ssc cgl", "cgl"]),
    ("SSC CHSL", ["chsl"]), ("SSC MTS", ["ssc mts", "mts"]), ("RRB ALP", ["rrb alp", "alp"]),
    ("UP Police", ["up police", "यूपी पुलिस", "uppolice"]), ("Delhi Police", ["delhi police", "दिल्ली पुलिस"]),
    ("Police", ["police", "पुलिस", "constable", "कांस्टेबल"]), ("Army", ["army gd", "indian army", "आर्मी", "सेना भर्ती"]),
    ("CTET", ["ctet", "tet", "सीटेट"]), ("UPSC", ["upsc", "ias", "यूपीएससी"]), ("Bank", ["ibps", "sbi po", "sbi clerk", "bank po"]),
    ("NDA / CDS", ["nda", "cds", "afcat"]), ("Railway", ["railway", "rrb", "रेलवे"]), ("SSC", ["ssc", "एसएससी"]),
]
NAME_SUBJECT_WORDS = {  # extra NAME words (short forms) — matched as whole words in the file name / caption only
    "geography": ["geo", "bhugol", "geography"], "static_gk": ["gs", "gk", "g k", "g s", "gk gs", "general studies",
                                                               "सामान्य अध्ययन", "सामान्य ज्ञान", "samanya gyan", "lucent"],
    "maths": ["math", "maths", "ganit", "गणित", "quant", "arithmetic", "advance maths"], "history": ["itihas", "इतिहास"],
    "polity": ["rajniti", "rajvyavastha"], "science": ["vigyan", "विज्ञान", "physics", "chemistry", "biology", "gs science"],
    "economics": ["economy", "arthvyavastha", "अर्थशास्त्र"], "current_affairs": ["current affairs", "करंट अफेयर्स",
                                                                                    "samsamayiki", "सामयिकी", "ca"],
    "reasoning": ["reasoning", "tark", "रीजनिंग", "तर्कशक्ति"], "computer": ["computer", "कंप्यूटर"],
    "hindi": ["hindi vyakaran", "हिंदी व्याकरण", "हिन्दी व्याकरण"], "english": ["english grammar", "vocabulary"],
}
TOPICS = [  # subject, topic label, words (≥2 distinct hits, or 1 multi-word phrase, makes the topic)
    ("history", "Mughal Empire", ["mughal", "मुगल", "मुग़ल", "akbar", "अकबर", "babur", "बाबर", "aurangzeb", "औरंगजेब",
                                  "humayun", "हुमायूँ", "shah jahan", "शाहजहाँ", "jahangir", "जहाँगीर"]),
    ("history", "Harappan Civilization", ["harappa", "हड़प्पा", "सिंधु घाटी", "indus valley", "mohenjo", "मोहनजोदड़ो"]),
    ("history", "Maurya Empire", ["maurya", "मौर्य", "ashoka", "अशोक", "chandragupta", "चंद्रगुप्त", "bindusara"]),
    ("history", "Gupta Empire", ["gupta empire", "गुप्त वंश", "गुप्त साम्राज्य", "samudragupta", "समुद्रगुप्त"]),
    ("history", "Delhi Sultanate", ["sultanate", "सल्तनत", "khilji", "खिलजी", "tughlaq", "तुगलक", "lodi", "लोदी"]),
    ("history", "Vedic Age", ["vedic", "वैदिक", "rigveda", "ऋग्वेद", "आर्य"]),
    ("history", "Revolt of 1857", ["1857", "sepoy mutiny", "1857 की क्रांति", "मंगल पांडे", "rani lakshmibai"]),
    ("history", "Freedom Struggle", ["freedom struggle", "स्वतंत्रता संग्राम", "national movement", "राष्ट्रीय आंदोलन",
                                     "गांधी", "gandhi", "भारत छोड़ो", "quit india", "non cooperation", "असहयोग"]),
    ("history", "Buddhism and Jainism", ["बौद्ध", "buddhism", "जैन धर्म", "jainism", "mahavir", "महावीर", "गौतम बुद्ध"]),
    ("polity", "Constitution", ["constitution", "संविधान", "preamble", "प्रस्तावना", "संविधान सभा"]),
    ("polity", "Fundamental Rights", ["fundamental rights", "मौलिक अधिकार", "मूल अधिकार"]),
    ("polity", "Parliament", ["parliament", "संसद", "लोकसभा", "राज्यसभा", "lok sabha", "rajya sabha"]),
    ("polity", "President", ["राष्ट्रपति", "president of india", "उपराष्ट्रपति"]),
    ("polity", "Judiciary", ["supreme court", "सर्वोच्च न्यायालय", "उच्चतम न्यायालय", "judiciary", "न्यायपालिका", "high court"]),
    ("polity", "Panchayati Raj", ["panchayat", "पंचायत", "पंचायती राज", "73rd amendment"]),
    ("geography", "Solar System", ["solar system", "सौरमंडल", "सौर मंडल", "ग्रह", "planets", "पृथ्वी"]),
    ("geography", "Indian Rivers", ["rivers of india", "नदियाँ", "नदी तंत्र", "ganga", "गंगा", "ब्रह्मपुत्र", "drainage"]),
    ("geography", "Climate and Monsoon", ["climate", "जलवायु", "monsoon", "मानसून"]),
    ("geography", "Soils", ["soils", "मृदा", "मिट्टी", "black soil", "काली मिट्टी"]),
    ("economics", "Budget", ["budget", "बजट", "fiscal deficit", "राजकोषीय घाटा"]),
    ("economics", "Banking", ["reserve bank", "रिज़र्व बैंक", "रिजर्व बैंक", "banking", "बैंकिंग", "repo rate", "रेपो दर"]),
    ("economics", "Inflation", ["inflation", "मुद्रास्फीति", "महंगाई"]),
    ("economics", "National Income", ["gdp", "national income", "राष्ट्रीय आय", "सकल घरेलू उत्पाद"]),
    ("science", "Physics", ["physics", "भौतिक विज्ञान", "न्यूटन", "newton", "गुरुत्वाकर्षण", "gravitation", "प्रकाश का"]),
    ("science", "Chemistry", ["chemistry", "रसायन", "अम्ल", "acid", "क्षार", "periodic table", "आवर्त सारणी"]),
    ("science", "Biology", ["biology", "जीव विज्ञान", "कोशिका", "human body", "मानव शरीर", "विटामिन", "vitamin"]),
    ("maths", "Percentage", ["percentage", "प्रतिशत"]), ("maths", "Profit and Loss", ["profit and loss", "लाभ और हानि",
                                                                                     "लाभ-हानि", "लाभ हानि", "cost price", "क्रय मूल्य"]),
    ("maths", "Ratio and Proportion", ["ratio and proportion", "अनुपात", "समानुपात", "ratio"]),
    ("maths", "Average", ["average", "औसत"]), ("maths", "Simple and Compound Interest", ["simple interest", "साधारण ब्याज",
                                                                                         "compound interest", "चक्रवृद्धि ब्याज"]),
    ("maths", "Time and Work", ["time and work", "समय और कार्य", "समय एवं कार्य"]),
    ("maths", "Speed Time and Distance", ["speed", "चाल", "समय और दूरी", "time and distance", "रेलगाड़ी"]),
    ("maths", "Number System", ["number system", "संख्या पद्धति", "lcm", "hcf", "लघुत्तम", "महत्तम"]),
    ("maths", "Geometry", ["geometry", "ज्यामिति", "त्रिभुज", "triangle", "वृत्त", "circle"]),
    ("maths", "Mensuration", ["mensuration", "क्षेत्रमिति", "क्षेत्रफल", "आयतन", "surface area"]),
    ("reasoning", "Coding-Decoding", ["coding decoding", "coding-decoding", "कूट", "कोडिंग"]),
    ("reasoning", "Blood Relation", ["blood relation", "रक्त संबंध"]), ("reasoning", "Direction Sense", ["direction sense", "दिशा ज्ञान", "दिशा परीक्षण"]),
    ("reasoning", "Series", ["number series", "श्रृंखला", "श्रेणी", "letter series"]),
    ("reasoning", "Analogy", ["analogy", "सादृश्यता", "सादृश्य"]), ("reasoning", "Syllogism", ["syllogism", "न्याय निगमन"]),
    ("hindi", "संधि", ["संधि"]), ("hindi", "समास", ["समास"]), ("hindi", "पर्यायवाची शब्द", ["पर्यायवाची"]),
    ("hindi", "मुहावरे और लोकोक्तियाँ", ["मुहावरे", "लोकोक्ति", "लोकोक्तियाँ"]), ("hindi", "विलोम शब्द", ["विलोम"]),
    ("english", "Tenses", ["tenses", "present perfect", "past continuous"]), ("english", "Vocabulary",
                                                                            ["synonyms", "antonyms", "one word substitution", "idioms and phrases"]),
    ("english", "Voice and Narration", ["active voice", "passive voice", "narration", "direct and indirect"]),
    ("computer", "MS Office", ["ms word", "ms excel", "powerpoint", "ms office"]),
    ("computer", "Computer Basics", ["input device", "output device", "cpu", "operating system", "motherboard"]),
    ("current_affairs", "Current Affairs", ["current affairs", "करंट अफेयर्स", "समसामयिकी", "सामयिकी"]),
]
CAT_SYNONYMS = {  # normalised words → one category, so there are no near-duplicates
    "geo": "geography", "bhugol": "geography", "भूगोल": "geography", "world geography": "geography",
    "indian geography": "geography", "गणित": "maths", "ganit": "maths", "arithmetic": "maths", "quantitative": "maths",
    "इतिहास": "history", "indian history": "history", "राजव्यवस्था": "polity", "राजनीति": "polity", "indian polity": "polity",
    "अर्थव्यवस्था": "economics", "अर्थशास्त्र": "economics", "indian economy": "economics", "विज्ञान": "science",
    "general science": "science", "gs": "static_gk", "g.s.": "static_gk", "gk gs": "static_gk", "gk & gs": "static_gk",
    "सामान्य ज्ञान": "static_gk", "सामान्य अध्ययन": "static_gk", "करंट अफेयर्स": "current_affairs", "ca": "current_affairs",
    "current affairs monthly": "current_affairs", "monthly current affairs": "current_affairs", "रीजनिंग": "reasoning",
    "तर्कशक्ति": "reasoning", "mental ability": "reasoning", "हिंदी": "hindi", "हिन्दी": "hindi", "hindi grammar": "hindi",
    "अंग्रेजी": "english", "english grammar": "english", "कंप्यूटर": "computer", "computer awareness": "computer",
    "previous year": "pyq", "previous year paper": "pyq", "previous year questions": "pyq", "solved papers": "pyq",
    "mock test": "mock", "mock tests": "mock", "practice set": "mock", "practice sets": "mock", "test series": "mock",
    "admit card": "admit_card", "admit cards": "admit_card", "hall ticket": "admit_card", "प्रवेश पत्र": "admit_card",
    "answer key": "answer_key", "उत्तर कुंजी": "answer_key", "results": "result", "रिजल्ट": "result", "परिणाम": "result",
    "merit list": "result", "cutoff": "cut_off", "cut-off": "cut_off", "syllabus": "syllabus", "सिलेबस": "syllabus",
    "exam pattern": "syllabus", "notification": "notification", "notifications": "notification", "vacancy": "notification",
    "typing test": "typing", "टाइपिंग": "typing", "data analytics": "data_analysis", "data analyst": "data_analysis",
    "misc": "other", "miscellaneous": "other", "general": "other", "others": "other", "none": "other", "": "other",
}
EXTRA_LABEL = {s: l for s, l, _, _ in EXTRA_KINDS}
EXTRA_EMOJI = {s: e for s, _, e, _ in EXTRA_KINDS}
_PAGES_READ = [0]   # pages read for the last new file (status / tests)


def slugify(label: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", str(label or "").lower())).strip("_")[:30]


def cat_registry() -> Dict[str, Dict[str, Any]]:
    """Categories the bot opened by itself (bot_settings "cat_registry"): {slug: {label, emoji, t}}."""
    v = _REQ.get("_catreg")
    if v is None:
        try:
            v = json.loads(get_setting("cat_registry") or "{}")
        except Exception:
            v = {}
        _REQ["_catreg"] = v
    return v


def cat_label(slug: str) -> str:
    return SUBJECT_LABEL.get(slug) or EXTRA_LABEL.get(slug) or (cat_registry().get(slug) or {}).get("label") or \
        str(slug or "Other").replace("_", " ").title()


def cat_emoji(slug: str) -> str:
    return SUBJECT_EMOJI.get(slug) or EXTRA_EMOJI.get(slug) or (cat_registry().get(slug) or {}).get("emoji") or "📁"


def known_category(slug: str) -> bool:
    return bool(slug) and (slug in SUBJECT_LABEL or slug in EXTRA_LABEL or slug in cat_registry())


def ensure_category(slug: str, label: str = "", emoji: str = "") -> str:
    """Open a new category the first time a file needs it (logged). Base subjects always exist."""
    if not slug or slug in SUBJECT_LABEL:
        return slug or "other"
    reg = cat_registry()
    if slug in reg:
        return slug
    reg[slug] = {"label": (label or EXTRA_LABEL.get(slug) or slug.replace("_", " ").title())[:30],
                 "emoji": emoji or EXTRA_EMOJI.get(slug) or "📁", "t": int(time.time())}
    try:
        set_setting("cat_registry", json.dumps(reg, ensure_ascii=False, separators=(",", ":")))
        audit_log(f"🗂️ Nayi category bani: {reg[slug]['emoji']} {reg[slug]['label']}\n🕒 {ist_now()}")
    except Exception:
        log_error("cat_registry")
    return slug


def norm_category(value: Any, create: bool = False) -> str:
    """Any subject / category word → one slug (synonyms merged: Geo = Geography, Maths = Math = गणित).
    Unknown + create → a new category (short English words only); unknown otherwise → ""."""
    v = re.sub(r"\s+", " ", re.sub(r"[_|/]+", " ", str(value or "").strip().lower())).strip(" .-–:")
    v = re.sub(r"\s+(notes?|pdf|books?|files?|section|category)$", "", v).strip()
    if v in CAT_SYNONYMS:
        return CAT_SYNONYMS[v]
    s = norm_subject(v) or norm_subject(str(value or ""))
    if s:
        return s
    table = {l.lower(): s for s, l, _, _ in SUBJECTS}
    table.update({l.lower(): s for s, l, _, _ in EXTRA_KINDS})
    table.update({s.replace("_", " "): s for s, _, _, _ in EXTRA_KINDS})
    table.update({str(r.get("label") or "").lower(): s for s, r in cat_registry().items()})
    table.update({l.lower(): slugify(l) for l, _ in EXAMS})
    for cand in (v, v[:-1] if v.endswith("s") else v, v + "s"):
        if cand in table:
            s = table[cand]
            return ensure_category(s, next((l for l, _ in EXAMS if slugify(l) == s), "")) if create else s
    import difflib
    close = difflib.get_close_matches(v, list(table), n=1, cutoff=0.86) if v else []
    if close:
        return table[close[0]]
    if not create:
        return ""
    if not re.fullmatch(r"[a-z][a-z0-9 &.\-]{2,23}", v) or len(v.split()) > 3:
        return "other"
    label = " ".join(w if w.isupper() else w.capitalize() for w in str(value).strip().split())[:24]
    return ensure_category(slugify(label), label)


def _hit_words(words: List[str], text: str) -> List[str]:
    out = []
    for w in words:
        if re.search(r"[\u0900-\u097F]", w):
            if w in text:
                out.append(w)
        elif re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", text):
            out.append(w)
    return out


def _name_norm(s: str) -> str:
    s = os.path.splitext(str(s or ""))[0] if re.search(r"\.(pdf|docx?|txt|pptx?|epub)$", str(s or ""), re.I) else str(s or "")
    return re.sub(r"\s+", " ", re.sub(r"[_\-.()\[\]]+", " ", s.lower())).strip()


def exam_in(text: str) -> str:
    for label, words in EXAMS:
        if _hit_words(words, text):
            return label
    return ""


def name_category(name: str) -> str:
    """Category a NAME already says (kind > PYQ/Mock > subject > exam), "" when the name is unclear."""
    t = _name_norm(name)
    if not t:
        return ""
    for slug, _, _, words in EXTRA_KINDS:
        if _hit_words(words, t):
            return slug
    for slug in ("pyq", "mock"):
        if _hit_words([w for s, _, _, ws in SUBJECTS if s == slug for w in ws], t):
            return slug
    best, score = "", 0
    for slug, _, _, words in SUBJECTS:
        if slug in ("pyq", "mock", "other"):
            continue
        n = len(set(_hit_words(words + NAME_SUBJECT_WORDS.get(slug, []), t)))
        if n > score:
            best, score = slug, n
    if best:
        return best
    for slug, _, words in TOPICS:
        if _hit_words(words, t):
            return slug
    ex = exam_in(t)
    return slugify(ex) if ex else ""


def clear_name(caption: str, file_name: str) -> Optional[Dict[str, str]]:
    """{title, cat} when the caption / file name already says the subject or topic; None = unclear (read it)."""
    cands = [ln for ln in str(caption or "").splitlines() if ln.strip()][:4] + [os.path.splitext(str(file_name or ""))[0]]
    for c in cands:
        cleaned = strip_people(c.replace("_", " "))
        if len(re.findall(r"[A-Za-z\u0900-\u097F]", cleaned)) < 2:
            continue
        cat = name_category(cleaned)
        if cat:
            title = re.sub(r"\s+", " ", cleaned).strip()
            return {"title": title[:100], "cat": cat}
    return None


def iter_pages(data: Optional[bytes], file_name: str, mime: str = "", max_pages: int = 3):
    """Yield the text of page 1, 2, 3 one at a time (lazy: the caller stops early). txt/docx = 2000-char pages."""
    if not data:
        return
    low = (file_name or "").lower()
    try:
        if low.endswith(".pdf") or (mime or "").endswith("pdf") or data[:5] == b"%PDF-":
            reader = PdfReader(io.BytesIO(data), strict=False)
            for page in reader.pages[:max_pages]:
                try:
                    t = page.extract_text() or ""
                except Exception:
                    t = ""
                yield t
            return
        if low.endswith(".txt") or (mime or "").startswith("text/"):
            t = data[:200000].decode("utf-8", "replace")
        elif low.endswith(".docx"):
            import zipfile
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                xml = z.read("word/document.xml").decode("utf-8", "replace")
            t = html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"</w:p>", "\n", xml)))
        else:
            return
        for i in range(max_pages):
            chunk = t[i * 2000:(i + 1) * 2000]
            if not chunk.strip():
                return
            yield chunk
    except Exception:
        return


def _heading_lines(text: str) -> List[str]:
    out = []
    for ln in str(text or "").splitlines()[:25]:
        ln = strip_people(re.sub(r"\s+", " ", ln)).strip(" -–—:•*#=")
        letters = len(re.findall(r"[A-Za-z\u0900-\u097F]", ln))
        words = ln.split()
        if 2 <= len(words) <= 8 and letters >= 5 and letters >= 0.6 * len(ln.replace(" ", "")) and \
                not re.search(r"(page|पृष्ठ|www|http|join|telegram|whatsapp|copyright|index|contents|विषय सूची)", ln, re.I):
            out.append(ln[:60])
    return out


def detect_from_text(text: str) -> Optional[Dict[str, str]]:
    """Free (no AI) subject + topic from page text. None = not clear yet (read the next page)."""
    low = re.sub(r"[ \t]+", " ", str(text or "").lower())
    if len(re.findall(r"[a-z\u0900-\u097F]", low)) < 40:
        return None
    head = low[:500]
    exam = exam_in(head)
    for slug, label, _, words in EXTRA_KINDS:
        if _hit_words(words, head):
            year = re.search(r"\b(20[1-3]\d)\b", head)
            return {"cat": slug, "title": " ".join(x for x in (exam, label, year.group(1) if year else "") if x)}
    best, bscore = None, 0
    for slug, topic, words in TOPICS:
        hits = set(_hit_words(words, low[:6000]))
        score = len(hits) + (1 if any(" " in w for w in hits) else 0) + (1 if _hit_words(words, head) else 0)
        if score > bscore:
            best, bscore = (slug, topic), score
    subj, sscore = "", 0
    for slug, _, _, words in SUBJECTS:
        if slug in ("pyq", "mock", "other"):
            continue
        n = len(set(_hit_words(words, low[:6000])))
        if n > sscore:
            subj, sscore = slug, n
    kind = ""
    if _hit_words(["previous year", "पिछले वर्ष", "solved paper", "pyq", "पूर्व वर्ष"], head):
        kind = "PYQ"
    elif _hit_words(["practice set", "प्रैक्टिस सेट", "mock test", "मॉक टेस्ट", "अभ्यास प्रश्न", "practice questions"], head):
        kind = "Practice Set"
    elif _hit_words(["mcq", "objective", "वस्तुनिष्ठ", "प्रश्नोत्तरी", "quiz"], head):
        kind = "MCQ"
    if best and bscore >= 2:
        slug, topic = best
        return {"cat": slug, "title": f"{SUBJECT_LABEL[slug]} – {topic} {kind or 'Notes'}"}
    if subj and sscore >= 2:
        heads = [h for h in _heading_lines(text) if _hit_words([w for s, _, _, ws in SUBJECTS if s == subj for w in ws],
                                                                 h.lower())]
        mid = f" – {heads[0]}" if heads and heads[0].lower() != SUBJECT_LABEL[subj].lower() else ""
        return {"cat": subj, "title": f"{SUBJECT_LABEL[subj]}{mid} {kind or 'Notes'}".strip()}
    if exam and kind:
        return {"cat": "mock" if kind == "Practice Set" else ("pyq" if kind == "PYQ" else slugify(exam)),
                "title": f"{exam} {kind}"}
    return None


NAMED_BY = ("clean", "local_text", "ai", "fallback")
NAMED_BY_TEXT = {"clean": "naam already sahi tha → sirf saaf kiya (0 token, file nahi padhi)",
                 "local_text": "{p} page padhke, bina AI (0 token)", "ai": "AI se (1 chhoti call)",
                 "fallback": "saaf naam (AI fail / text nahi mila)"}


def count_named_by(how: str) -> None:
    if how not in NAMED_BY:
        return
    try:
        d = json.loads(get_setting("named_by") or "{}")
    except Exception:
        d = {}
    d[how] = int(d.get(how) or 0) + 1
    set_setting("named_by", json.dumps(d, separators=(",", ":")))


def named_by_counts() -> Dict[str, int]:
    try:
        d = json.loads(get_setting("named_by") or "{}")
    except Exception:
        d = {}
    return {k: int(d.get(k) or 0) for k in NAMED_BY}


def name_new_file(record: Dict[str, Any], text: Optional[Dict[str, str]] = None) -> str:
    """Name + category for a new file, cheapest first. Returns "kept" | "clean" | "local_text" | "ai" | "fallback".
    Sets record title / _cat / content_text / _bytes / _pages; "fallback" with readable text queues an AI retry."""
    caption, fname = str(record.get("caption") or ""), str(record.get("file_name") or "")
    _PAGES_READ[0] = 0
    record["_pages"] = 0
    uniq = record.get("telegram_file_unique_id")
    if uniq:
        # Already in the catalog (re-forward): keep its name + category, an admin may have renamed it.
        cols = "id,title,file_name,keywords" + (",category" if has_category_col() else "")
        have = supabase("books", "GET", query=f"?telegram_file_unique_id=eq.{urllib.parse.quote(str(uniq))}&select={cols}&limit=1")
        if have:
            record["title"] = have[0].get("title") or record.get("title")
            record["_cat"] = book_category(have[0], {} if has_category_col() else catmap())
            return "kept"
    base = local_clean_name(caption, fname)
    # 1) the name already says it → no download, no reading, no AI
    c = clear_name(caption, fname)
    if c:
        record["title"] = c["title"][:500]
        record["_cat"] = norm_category(c["cat"], create=True) if not known_category(c["cat"]) else c["cat"]
        if record["_cat"] not in SUBJECT_LABEL:
            ensure_category(record["_cat"], next((l for l, _ in EXAMS if slugify(l) == record["_cat"]), ""))
        return "clean"
    record["title"] = base[:500]
    record["_cat"] = "other"
    # 2) unclear name → read page 1, 2, 3; stop at the first page where the subject / topic is clear (0 tokens)
    data = download_file(record.get("telegram_file_id"), record.get("file_size"))
    record["_bytes"] = data if data and len(data) <= REUPLOAD_LIMIT else None
    pages: List[str] = []
    found = None
    for page in iter_pages(data, fname, str(record.get("mime_type") or "")):
        pages.append(page or "")
        _PAGES_READ[0] = len(pages)
        found = detect_from_text("\n".join(pages))
        if found:
            break
    record["_pages"] = len(pages)
    read = clean_text(" ".join(pages))[:60000]
    if read:
        record["content_text"] = read
        record["keywords"] = clean_text(" ".join([str(record.get("keywords") or ""), read[:3000]]))[:3990]
    if found:
        record["title"] = found["title"][:500]
        record["_cat"] = found["cat"] if found["cat"] in SUBJECT_LABEL else ensure_category(
            found["cat"], next((l for l, _ in EXAMS if slugify(l) == found["cat"]), ""))
        return "local_text"
    excerpt = re.sub(r"\s+", " ", " ".join(pages)).strip()[:1200]
    if len(re.findall(r"[A-Za-z\u0900-\u097F]", excerpt)) < 40:
        return "fallback"        # scanned / empty / too big: the cleaned name stays
    # 3) still unclear after 3 pages → ONE small AI call
    if not ai_naming_on() or not ai_provider() or not ai_name_budget_ok() or req_elapsed() > 6.5:
        record["_retry"] = True
        return "fallback"
    _count_day("ainame:all")
    got = ai_name_items([{"i": 1, "caption": caption, "file": fname, "text": excerpt}],
                        cap=max(2.0, min(7.0, 9.0 - req_elapsed())), excerpt_chars=1200)
    if got is None:
        record["_retry"] = True
        return "fallback"
    res = got.get(1) or {}
    if res.get("title"):
        record["title"] = res["title"][:500]
    if res.get("subject"):
        record["_cat"] = res["subject"]
    return "ai"


# ---- 📝 Log channel: one short message per event (🆕 file added, ✏️ renamed, 📥 file taken); last 50 kept for /logs
LOG_KEEP = 50


def ist_now() -> str:
    t = time.gmtime(time.time() + 19800)
    return time.strftime("%d %b, %I:%M %p", t)


def source_label(where: str) -> str:
    user = _REQ.get("user") or {}
    who = user_label({"first_name": user.get("first_name"), "last_name": user.get("last_name"),
                      "username": user.get("username")}) if user.get("id") else ""
    if where == "Admin upload":
        return f"👤 Admin upload ({who})" if who else "👤 Admin upload"
    if who and is_admin(user.get("id")):
        return f"↪️ Forward by {who} (from: {where})"
    return f"📢 Channel: {where}"


def file_added_log(record: Dict[str, Any], orig: str, slug: str, how: str, where: str, backup: str) -> str:
    pages = int(record.get("_pages") or 0)
    nb = NAMED_BY_TEXT.get(how, how).replace("{p}", str(pages or 1))
    if how == "fallback" and record.get("_retry"):
        nb += " · AI baad me dobara"
    new = str(record.get("title") or "")
    mark = {"saved": "💾 backup ✅", "dup": "💾 pehle se backup", "failed": "💾 backup ❌"}.get(backup, "")
    lines = [f"🆕 Nayi file · {ist_now()}", f"📍 {source_label(where)}",
             (f"📄 {orig[:70]} → {new[:70]}" if orig and orig != new else f"📄 {new[:80]}"),
             f"🗂️ {cat_label(slug)} · 🏷️ {nb}"]
    if record.get("telegram_file_id") is not None and record.get("file_size"):
        lines[-1] += f" · {human_size(record.get('file_size'))}"
    if mark:
        lines.append(mark)
    return "\n".join(lines)


def rename_log(items: List[Any], who: str) -> None:
    items = [x for x in items if str(x[1] or "") != str(x[2] or "")]
    if not items:
        return
    head = f"✏️ Naam badla · {ist_now()} · {who}"
    body = [f"• {str(o)[:60]} → {str(n)[:60]}" for _, o, n in items[:20]]
    if len(items) > 20:
        body.append(f"…aur {len(items) - 20}")
    audit_log(head + "\n" + "\n".join(body))


def actor_label(user_id: Optional[int] = None) -> str:
    user = _REQ.get("user") or {}
    if user.get("id") and (user_id is None or user.get("id") == user_id):
        return "admin " + user_label({"first_name": user.get("first_name"), "last_name": user.get("last_name"),
                                      "username": user.get("username")})
    return f"admin {user_id}" if user_id else "admin"


def remember_logs(lines: List[str]) -> None:
    try:
        old = json.loads(get_setting("log_recent") or "[]")
    except Exception:
        old = []
    old.extend(lines)
    set_setting("log_recent", json.dumps(old[-LOG_KEEP:], ensure_ascii=False, separators=(",", ":")))


def logs_text(n: int = 15) -> List[str]:
    try:
        rows = json.loads(get_setting("log_recent") or "[]")
    except Exception:
        rows = []
    if not rows:
        return ["📝 Abhi koi log nahi hai."]
    target = "✅ Log Chat set hai, sab wahan bhi jata hai." if get_setting("log_chat_id") else \
        "ℹ️ Log Chat set nahi hai. /admin → 📝 Log Chat se channel set karo, phir har update wahan aayega."
    rows = rows[-max(1, min(LOG_KEEP, n)):][::-1]
    out, cur = [], f"📝 Last {len(rows)} logs (naya upar)\n{target}"
    for r in rows:
        if len(cur) + len(r) > 3800:
            out.append(cur)
            cur = ""
        cur = (cur + "\n\n" + r) if cur else r
    out.append(cur)
    return out[:4]


# ---- 🤖 AI test (owner): one tiny real call, shows ✅ or the exact error (never the key)
def ai_providers_summary() -> Dict[str, Any]:
    cfg = router_cfg()
    chain = ai_chain()
    today = calls_today()
    out: Dict[str, Any] = {"live": chain[0] if chain else None, "backup_order": chain[1:], "providers": {}}
    for p in cfg["order"]:
        n = len(provider_keys(p))
        if p not in MAIN_PROVIDERS and not n:
            continue
        ok, bad = (today.get(p) or [0, 0])[:2]
        stt = provider_state(p) if n else "no key"
        if stt == "ok" and not ok and not any(key_tested(k) for k in provider_keys(p)):
            stt = "untested"           # never had a successful call yet (a fresh key isn't "ok" until it answers once)
        out["providers"][p] = {"keys": n, "status": stt,
                               "role": "live" if chain and chain[0] == p else (f"backup {chain.index(p)}" if p in chain else "off"),
                               "model": cfg["models"].get(p) or "auto", "calls_today": ok, "fails_today": bad}
    try:
        last = [get_setting(f"aiswitch:{p}") or "" for p in chain]
        sw = sorted((x for x in last if x), reverse=True)
        out["last_switch"] = sw[0].split("|", 1)[1] if sw else None
    except Exception:
        out["last_switch"] = None
    return out


def ai_test_text() -> str:
    """/aitest: one tiny real call to EVERY provider — ✅ + ms, or the exact error class. Never a key."""
    pr = ai_probe(True)
    live = pr.get("provider") or ""
    lines = ["🤖 <b>AI test</b> (har AI ki ek chhoti asli call)",
             f"⭐ Live: {esc(pname(live)) if live else 'koi nahi (AI OFF)'}", ""]
    order = router_cfg()["order"]
    for p in order:
        rows = [k for k in pr.get("keys", []) if k.get("p") == p]
        if not rows:
            if p in MAIN_PROVIDERS:
                lines.append(f"➖ {pname(p)}: key nahi (🔑 AI Keys → ➕ Key Jodo)")
            continue
        for k in rows:
            label = f"{pname(p)} #{k['key'].split('#')[-1]}"
            tries = k.get("tries") or []
            ok = [t for t in tries if t.get("ok")]
            if ok:
                lines.append(f"✅ {esc(label)}: chal raha hai — model {esc(ok[0]['model'])}, {ok[0]['ms']} ms")
            elif tries and tries[-1].get("cls") == "limit" and "429" in str(tries[-1].get("error") or ""):
                lines.append(f"⏳ {esc(label)}: limit (nayi free key? 1–2 min baad dobara test karo)")
            elif tries:
                lines.append(f"❌ {esc(label)}: " + esc(str(tries[-1].get('error') or 'khali jawab')[:160]))
            else:
                lines.append(f"• {esc(label)}: {esc(str(k.get('cooling')))}")
    lines.append("\nLive fail ho to upar wale order me agli AI apne-aap chalegi.")
    return "\n".join(lines)


# ================================================================ AI diagnostics (no keys ever shown)
DIAG_TOKEN_SHA256 = "4d4e81e91187974473b09468e5c04a92865425fe8b02960c1359de6abdb68f41"


def _err_class(exc: Exception, key: str = "") -> str:
    s = str(exc)
    if key:
        s = s.replace(key, "***")
    s = scrub_keys(s)
    m = re.match(r"HTTP (\d+)", s)
    if m:
        return s[:220]
    return (type(exc).__name__ + ": " + s)[:220]


def _probe_provider(p: str, json_mode: bool, all_models: bool) -> List[Dict[str, Any]]:
    out = []
    for n, key in enumerate(provider_keys(p)[:3], 1):
        row: Dict[str, Any] = {"key": f"{p} #{n}", "p": p, "cooling": key_state(key) or "no", "tries": []}
        if p == "groq":
            try:
                lst = http_json(PROVIDERS[p]["base"] + "/models", "GET", None, _auth(p, key), timeout=5)
                row["models_listed"] = sorted(str(m.get("id")) for m in (lst.get("data") or []))
            except Exception as exc:
                row["models_listed"] = _err_class(exc, key)
            _GMODELS.pop(key_id(key), None)
            row["models_picked"] = provider_models(p, key)
        else:
            _PMODELS.pop(f"{p}:{key_id(key)}", None)
            row["models_picked"] = provider_models(p, key, fresh=True)
        for model in row["models_picked"][:4 if all_models else (8 if p == "nvidia" else 3)]:
            t0 = time.monotonic()
            try:
                for attempt in range(2):        # a 429 (e.g. a brand-new free key) gets one retry after ~2 s
                    try:
                        r = provider_call(p, key, model, [{"role": "system", "content": "Reply ONLY with JSON {\"ok\":true}."},
                                                          {"role": "user", "content": "ping"}], 20, 8, True, json_mode)
                        break
                    except Exception as exc0:
                        if attempt == 0 and "429" in str(exc0):
                            if not PROBE_NO_SLEEP[0]:
                                time.sleep(2.0)
                            continue
                        raise
                txt = r["text"][:40]
                if txt:
                    mark_key_ok(key, now=True)
                row["tries"].append({"model": model, "ok": bool(txt), "ms": int((time.monotonic() - t0) * 1000), "reply": txt})
                if txt and model != row["models_picked"][0]:
                    promote_model(p, key, model)
                if txt and not all_models:
                    break
            except Exception as exc:
                row["tries"].append({"model": model, "ok": False, "ms": int((time.monotonic() - t0) * 1000),
                                     "error": _err_class(exc, key), "cls": ai_err_class(exc)})
                if ai_err_class(exc) == "model":
                    drop_model(p, key, model)       # a 404 model never marks the key dead: next model
                if ai_err_class(exc) in {"invalid", "limit"}:
                    break
        out.append(row)
    return out


def ai_probe(json_mode: bool = True, all_models: bool = False) -> Dict[str, Any]:
    """One tiny real AI call per key of every provider (in parallel). Status codes / model names only."""
    out: Dict[str, Any] = {"provider": ai_provider() or "none", "chain": ai_chain(), "keys": []}
    provs = [p for p in router_cfg()["order"] if provider_keys(p)]
    import concurrent.futures as _cf
    with _cf.ThreadPoolExecutor(max_workers=max(1, len(provs))) as ex:
        futs = {p: ex.submit(_probe_provider, p, json_mode, all_models) for p in provs}
        for p in provs:
            try:
                out["keys"].extend(futs[p].result(timeout=20))
            except Exception as exc:
                out["keys"].append({"key": f"{p} #1", "p": p, "tries": [{"model": "?", "ok": False, "ms": 0,
                                                                        "error": _err_class(exc)}]})
    return out


def naming_diag(ids: str = "") -> Dict[str, Any]:
    """Live check of content naming on fixed samples (no real files touched)."""
    samples = [
        {"i": 1, "file": "hjskjd.pdf", "caption": "", "text": "The Mughal Empire was founded by Babur after the first battle of "
         "Panipat in 1526. Akbar the Great ruled from 1556 to 1605 and introduced the Mansabdari system and Din-i-Ilahi."},
        {"i": 2, "file": "History by Aarav.pdf", "caption": "", "text": ""},
        {"i": 3, "file": "geography_book.pdf", "caption": "", "text": "भारतीय संविधान की प्रस्तावना, मौलिक अधिकार अनुच्छेद 12 से 35, "
         "राज्य के नीति निदेशक तत्व, संसद और राष्ट्रपति की शक्तियाँ"},
    ]
    t0 = time.monotonic()
    got = ai_name_items(samples, cap=9)
    return {"ms": int((time.monotonic() - t0) * 1000), "result": got if got is not None else "AI call failed",
            "raw": _LAST_NAME_RAW[0],
            "files": [s["file"] for s in samples]}


@app.get("/api/aidiag")
def aidiag() -> Any:
    tok = request.args.get("t", "")
    if not tok or not hmac.compare_digest(hashlib.sha256(tok.encode()).hexdigest(), DIAG_TOKEN_SHA256):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    _REQ_T0[0] = time.monotonic()
    res: Dict[str, Any] = {"ok": True, "build": os.environ.get("VERCEL_GIT_COMMIT_SHA", "")[:7]}
    res["probe_json"] = ai_probe(True, request.args.get("all") == "1")
    if request.args.get("plain") == "1":
        res["probe_plain"] = ai_probe(False)
    if request.args.get("q"):
        try:
            u = understand(request.args.get("q", ""))
            rows = search_rows(u["keywords"], u["lang"], u["types"], require_all=True) if u["keywords"] else []
            fz = fuzzy_rows(u["keywords"]) if u["keywords"] and not rows else []
            res["search"] = {"intent": u["intent"], "keywords": u["keywords"], "results": [str(r.get("title"))[:60] for r in rows[:8]],
                             "n": len(rows), "fuzzy": [str(r.get("title"))[:60] for r in fz[:5]],
                             "key_terms": [k for k in u["keywords"] if is_key_term(k)]}
        except Exception as exc:
            res["search"] = {"error": _err_class(exc)}
    if request.args.get("a"):      # admin command reading: local rules → AI → merged filters, + matching files in the live DB
        try:
            txt_a = request.args.get("a", "")[:300]
            p = admin_parse(txt_a, use_ai=request.args.get("noai") != "1", user_id=0)
            out_a: Dict[str, Any] = {"text": txt_a, "source": p["source"], "local": p["local"], "ai": p["ai"]}
            if request.args.get("ai") == "1":       # also ask the AI for the same text (shows the structured JSON)
                out_a["ai_forced"] = ai_admin_intent(0, txt_a)
            fin = p["final"] or {}
            out_a["action"] = fin.get("action")
            out_a["filters"] = public_filters(fin.get("filters")) if "filters" in fin else None
            if fin.get("action") in ("list_files", "delete_file") and has_filters(fin.get("filters")):
                rows_a = filtered_rows(fin["filters"])
                out_a["header"] = f"📂 {filter_label(fin['filters'])} — {len(rows_a)} files"
                out_a["count"] = len(rows_a)
                out_a["files"] = [str(r.get("title"))[:70] for r in rows_a[:30]]
            res["admin"] = out_a
        except Exception as exc:
            res["admin"] = {"error": _err_class(exc)}
    if request.args.get("big"):     # >20 MB rename: status, candidates, queue; &bigrun=<book id> queues + triggers (no owner msg)
        try:
            rows_b = supabase("books", "GET", query=f"?select=*&status=eq.indexed&file_size=gt.{TG_FILE_LIMIT}&order=id.desc&limit=200")
            res["bigfile"] = {"status": bigfile_status(),
                              "files": [{"id": r.get("id"), "title": str(r.get("title"))[:70], "size": human_size(r.get("file_size")),
                                         "file_name": str(r.get("file_name") or "")[:90], "delivered": delivered_name(r)[:90],
                                         "needs": big_needs_rename(r), "backup": bool(r.get("backup_message_id"))} for r in rows_b[:40]],
                              "queue": [{k: j.get(k) for k in ("j", "b", "fn", "st", "tries", "err", "file_name", "run")}
                                        for j in bigq_load()[-10:]]}
            if request.args.get("bigrun", "").isdigit():
                pick = [r for r in rows_b if str(r.get("id")) == request.args.get("bigrun")]
                res["bigfile"]["run"] = {k: v for k, v in bigfile_start(pick, None, "diag").items() if k != "jobs"}
                res["bigfile"]["run"]["queued"] = len(bigq_load()) and [j.get("j") for j in bigq_load() if j.get("st") == "pending"]
            flush_request()
        except Exception as exc:
            res["bigfile"] = {"error": _err_class(exc)}
    if request.args.get("dl"):      # live check of delivery under the clean name: send to the Log Chat, read the name, delete it
        try:
            target = get_setting("log_chat_id")
            rows_d = supabase("books", "GET", query="?select=*&status=eq.indexed&order=id.desc&limit=500")
            arg = request.args.get("dl", "")
            if arg.isdigit():
                rows_d = [r for r in rows_d if str(r.get("id")) == arg]
            else:
                rows_d = [r for r in rows_d if can_rename_file(r) and not renamed_copy(r) and str(r.get("file_name") or "") !=
                          safe_filename(str(r.get("title") or ""), str(r.get("file_name") or ""))]
            if not target or not rows_d:
                res["deliver"] = {"error": "no log chat" if not target else "no candidate"}
            else:
                r_d = rows_d[0]
                _REQ.pop("_dl", None)
                ok_d = deliver_renamed(int(target), r_d, delivery_caption(r_d))
                info = dict(_REQ.get("_dl") or {}, ok=ok_d, title=r_d.get("title"), original_file_name=r_d.get("file_name"),
                            size=human_size(r_d.get("file_size")), caption=delivery_caption(r_d), cached=bool(renamed_copy(r_d)))
                if info.get("message_id") and request.args.get("keep") != "1":
                    try:
                        telegram("deleteMessage", {"chat_id": int(target), "message_id": int(info["message_id"])})
                        info["test_message_deleted"] = True
                    except Exception:
                        info["test_message_deleted"] = False
                res["deliver"] = info
        except Exception as exc:
            res["deliver"] = {"error": _err_class(exc)}
    if request.args.get("u"):
        try:
            txt_u = request.args.get("u", "")[:200]
            t_u = time.time()
            d_u, src_u = ai_understand(txt_u, None, use_cache=request.args.get("nocache") != "1")
            run_deferred()
            res["understand"] = {"text": txt_u, "source": src_u, "json": d_u, "ms": int((time.time() - t_u) * 1000),
                                 "ai_call_ms": _AI_LAST.get("ms") if src_u == "ai" else None,
                                 "exact_skip": exact_name_hit(txt_u),
                                 "model": _AI_LAST.get("model") if src_u == "ai" else None,
                                 "usage": _AI_LAST.get("usage") if src_u == "ai" else None}
            if d_u:
                sets_u, _, lang_u = understanding_term_sets(d_u)
                res["understand"]["term_sets"] = sets_u
                hit = []
                for kws in sets_u:
                    rows = search_rows(kws, lang_u, None, require_all=True)
                    if rows:
                        hit = [str(r.get("title"))[:60] for r in rows[:5]]
                        res["understand"]["matched_set"] = kws
                        break
                res["understand"]["results"] = hit
                res["understand"]["label"] = understanding_label(d_u)
            res["understood_by"] = understood_counts()
        except Exception as exc:
            res["understand"] = {"error": _err_class(exc)}
    if request.args.get("logtest") == "1":
        try:
            res["logtest"] = log_test()
        except Exception as exc:
            res["logtest"] = {"error": _err_class(exc)}
    if request.args.get("name") == "1":
        try:
            res["naming"] = naming_diag(request.args.get("ids", ""))
        except Exception as exc:
            res["naming"] = {"error": _err_class(exc)}
    return jsonify(res)


_INST: Dict[str, Any] = {"born": time.time(), "reqs": 0}


@app.before_request
def _count_request() -> None:
    _INST["reqs"] += 1
    _REQ.pop("_set", None)      # the settings memo never outlives one HTTP request


def run_cb_diag(data: str, uid: int, msg_text: str = "") -> Dict[str, Any]:
    """Run one button tap through handle_callback with nothing sent (Telegram writes + DB writes recorded only)."""
    query = {"id": "diag", "data": data, "from": {"id": uid, "first_name": "Diag", "is_bot": False},
             "message": {"message_id": 1, "date": int(time.time()), "chat": {"id": uid, "type": "private"}, "text": msg_text}}
    _REQ.clear()
    _LOG_QUEUE.clear()
    _DIAG.update(on=True, dry=True, t0=time.monotonic(), calls=[], sent=[])
    _REQ_T0[0] = _DIAG["t0"]
    err = None
    try:
        handle_callback(query)
    except Exception as exc:
        err = _err_class(exc)
    t_view = int((time.monotonic() - _DIAG["t0"]) * 1000)
    try:
        flush_request()
    except Exception as exc:
        err = err or ("flush: " + _err_class(exc))
    total = int((time.monotonic() - _DIAG["t0"]) * 1000)
    sent, calls = list(_DIAG["sent"]), list(_DIAG["calls"])
    _DIAG.update(on=False, dry=False)
    ans = next((s for s in sent if s["m"] == "answerCallbackQuery"), None)
    view = next((s for s in sent if s["m"] in ("editMessageText", "sendMessage", "sendDocument", "copyMessage")), None)
    return {"cb": data, "answer_ms": ans["ms"] if ans else None, "answer_text": ans["text"] if ans else None,
            "answers": sum(1 for s in sent if s["m"] == "answerCallbackQuery"),
            "view_ms": view["ms"] if view else None, "view": (view["m"] + ": " + view["text"][:90]) if view else None,
            "buttons": (view or {}).get("cb", [])[:40], "handler_ms": t_view, "total_ms": total, "error": err,
            "db_calls": sum(1 for c in calls if c[2].startswith("db:")), "tg_calls": sum(1 for c in calls if c[2].startswith("tg:")),
            "ext_calls": [c for c in calls if c[2].startswith("ext:")], "slow": sorted(calls, key=lambda c: -c[1])[:6],
            "sent": [s["m"] for s in sent], "calls": calls if _DIAG.get("full") else None}


@app.get("/api/cbdiag")
def cbdiag() -> Any:
    """Owner-free live timing of button taps: GET /api/cbdiag?t=<diag token>&cb=ad:panel&cb=ad:files:1[&as=user]."""
    tok = request.args.get("t", "")
    if not tok or not hmac.compare_digest(hashlib.sha256(tok.encode()).hexdigest(), DIAG_TOKEN_SHA256):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    t_req = time.monotonic()
    uid = 1 if request.args.get("as") == "user" else (min(ADMIN_USER_IDS) if ADMIN_USER_IDS else 1)
    inst = {"first_request_on_instance": _INST["reqs"] <= 1, "instance_age_s": int(time.time() - _INST["born"]),
            "boot_ms": _BOOT_MS[0], "requests_on_instance": _INST["reqs"]}
    _DIAG["full"] = request.args.get("full") == "1"
    out = [run_cb_diag(cb[:64], uid, request.args.get("msg", "")) for cb in request.args.getlist("cb")[:25]]
    if request.args.get("net") == "1":       # raw round trips: first call opens the connection, the rest reuse it
        net: Dict[str, Any] = {"region": os.environ.get("VERCEL_REGION", ""), "db_ms": [], "tg_ms": []}
        for _ in range(3):
            t = time.monotonic()
            try:
                supabase("bot_settings", "GET", query="?select=key&limit=1")
            except Exception:
                pass
            net["db_ms"].append(int((time.monotonic() - t) * 1000))
            t = time.monotonic()
            try:
                telegram("getMe", {})
            except Exception:
                pass
            net["tg_ms"].append(int((time.monotonic() - t) * 1000))
        try:
            import socket
            net["db_ip"] = socket.gethostbyname(urllib.parse.urlsplit(SUPABASE_URL).hostname or "")
        except Exception:
            pass
        net["pool"] = dict(_POOL_STATS)
        try:   # response header names/values that hint where the database runs (no cookies/keys)
            _, hh, _ = http_raw(f"{SUPABASE_URL}/rest/v1/bot_settings?select=key&limit=1", "GET", None,
                                {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}, 10)
            net["db_headers"] = {k: v[:80] for k, v in hh.items() if k.lower() not in ("set-cookie", "authorization", "apikey")
                                 and (k.lower().startswith(("sb-", "x-", "cf-", "server", "via")))}
        except Exception as exc:
            net["db_headers"] = str(exc)[:80]
        inst["net"] = net
    return jsonify({"ok": True, "build": os.environ.get("VERCEL_GIT_COMMIT_SHA", "")[:7], "instance": inst, "taps": out,
                    "request_ms": int((time.monotonic() - t_req) * 1000)})


@app.after_request
def build_header(response: Any) -> Any:
    # Which commit is serving (lets a deploy be verified without opening the home page).
    response.headers["X-Filesbull-Build"] = os.environ.get("VERCEL_GIT_COMMIT_SHA", "")[:7] or "local"
    return response


# ---- 📝 Log Chat sending (channel, group or supergroup) with the exact reason kept when it fails (Aarav 6:34 PM)
LOGCHAT_STATUS_KEY = "log_chat_status"


def log_chat_reason(exc: Exception) -> str:
    low = str(exc).lower()
    if "upgraded to a supergroup" in low or "migrate_to_chat_id" in low:
        return "group supergroup ban gaya (ID badal gayi) — bot ne nayi ID le li"
    if "chat not found" in low:
        return "chat nahi mila — bot us group/channel me add nahi hai (ya ID galat)"
    if "bot was kicked" in low:
        return "bot ko group/channel se nikaal diya gaya"
    if "bot is not a member" in low or "not a member" in low:
        return "bot member nahi hai — bot ko group/channel me add karo"
    if "not enough rights" in low or "have no rights" in low or "need administrator" in low or "admin_required" in low:
        return "bot ko message bhejne ki permission nahi — bot ko admin banao (Post/Send Messages on)"
    if "topic" in low or "thread" in low:
        return "group me Topics on hain aur General topic band hai — General khol do ya Topics band karo"
    if "retry after" in low or "too many requests" in low:
        return "Telegram limit — thodi der baad apne-aap"
    if "forbidden" in low:
        return "bot ko yahan bhejne ki permission nahi (Forbidden)"
    if "blocked" in low:
        return "bot block hai"
    return "Telegram error: " + re.sub(r"-?\d{6,}", "<id>", str(exc))[:120].replace(BOT_TOKEN or "\0", "***")


def log_chat_status() -> Dict[str, Any]:
    try:
        d = json.loads(get_setting(LOGCHAT_STATUS_KEY) or "{}")
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _log_chat_mark(ok: bool, reason: str = "") -> None:
    try:
        old = log_chat_status()
        if ok and old.get("ok") and time.time() - float(old.get("t") or 0) < 3600:
            return                         # nothing new to store (saves a DB write per event)
        set_setting(LOGCHAT_STATUS_KEY, json.dumps({"ok": ok, "t": int(time.time()), "reason": reason[:200]},
                                                   ensure_ascii=False, separators=(",", ":")))
    except Exception:
        log_error("log_chat_status")


def log_chat_send(text: str, silent: bool = True) -> Dict[str, Any]:
    """Send one message to the 📝 Log Chat. A group that became a supergroup is followed to its new ID.
    Raises on failure (after storing the reason) so callers can wait on Telegram's retry-after."""
    target = get_setting("log_chat_id")
    if not target:
        raise RuntimeError("log chat not set")
    for attempt in range(2):
        try:
            res = telegram("sendMessage", {"chat_id": int(target), "text": text[:4000], "disable_web_page_preview": True,
                                           "disable_notification": silent})
            _log_chat_mark(True)
            return res
        except Exception as exc:
            m = re.search(r"migrate_to_chat_id=(-?\d+)", str(exc))
            if m and attempt == 0:
                target = m.group(1)
                set_setting("log_chat_id", target)
                continue
            if "retry after" not in str(exc).lower():
                _log_chat_mark(False, log_chat_reason(exc))
            raise
    raise RuntimeError("log chat send failed")


def log_chat_line() -> str:
    if not get_setting("log_chat_id"):
        return "Log chat: OFF (/logs se dekho)"
    st = log_chat_status()
    if st and not st.get("ok"):
        return f"Log chat: ❌ {st.get('reason') or 'bhej nahi pa raha'} (/logtest se check karo)"
    if st.get("ok"):
        return "Log chat: ✅ ON (bhej raha hai)"
    return "Log chat: ON (abhi tak kuch bheja nahi — /logtest dabao)"


def log_test() -> Dict[str, Any]:
    """Post a test line to the Log Chat; returns {ok, chat_type, title?, reason?} — never the chat ID."""
    out: Dict[str, Any] = {"set": bool(get_setting("log_chat_id"))}
    if not out["set"]:
        out.update(ok=False, reason="Log Chat set nahi hai")
        return out
    try:
        info = telegram("getChat", {"chat_id": int(get_setting("log_chat_id"))}).get("result") or {}
        out["chat_type"] = info.get("type")
        out["title"] = info.get("title") or info.get("first_name")
        out["topics"] = bool(info.get("is_forum"))
    except Exception as exc:
        m = re.search(r"migrate_to_chat_id=(-?\d+)", str(exc))
        if m:
            set_setting("log_chat_id", m.group(1))
            out["migrated"] = True
        else:
            out["chat_error"] = log_chat_reason(exc)
    try:
        log_chat_send("✅ Log Chat connected — filesbull yahan updates bhejega (" + ist_now() + " IST)", silent=False)
        out["ok"] = True
    except Exception as exc:
        out.update(ok=False, reason=log_chat_reason(exc))
    return out


def log_test_text(res: Dict[str, Any]) -> str:
    kind = {"channel": "channel", "supergroup": "group (supergroup)", "group": "group", "private": "private chat"}.get(
        str(res.get("chat_type") or ""), str(res.get("chat_type") or "?"))
    if res.get("ok"):
        return f"✅ Log Chat connected!\n{kind}: {res.get('title') or ''}\nTest message wahan chala gaya 👍"
    if not res.get("set"):
        return setlog_steps_text()
    return (f"❌ Log Chat me message nahi gaya.\nWajah: {res.get('reason')}\n" + (f"({kind}: {res.get('title')})\n" if res.get("title") else "") +
            "\nGroup ho to: bot ko group me add karo → admin banao (Send Messages on).\n"
            "Channel ho to: bot admin + Post Messages on.\nAasaan tarika: us group me /setlogchat bhejo — wahi group Log Chat ban jayega.")



# ---- user search understanding (Aarav 6:40 PM, a user "Yash"): generic asks, truly-missing books, relevance

def is_key_term(k: str) -> bool:
    k = normalize(k)
    return k in EXAM_WORDS or k in SUBJECT_WORDS or k in ALIASES


def fuzzy_rows(keywords: List[str]) -> List[Dict[str, Any]]:
    """Spelling-tolerant match on file names ("lusent" → Lucent): every keyword ≥4 letters must closely match a word
    of the title / original file name (short ones exactly). Cheap: one query, no AI."""
    import difflib
    kws = [normalize(k) for k in keywords if normalize(k)]
    if not kws or not any(len(k) >= 4 for k in kws):
        return []
    try:
        rows = supabase("books", "GET", query="?select=id,title,file_name,caption&status=eq.indexed&order=id.desc&limit=3000")
    except Exception:
        return []
    out = []
    for r in rows:
        words = set(re.findall(r"[a-z0-9]+|[\u0900-\u097F]+", normalize(" ".join(str(r.get(c) or "") for c in ("title", "file_name", "caption")))))
        if not words:
            continue
        ok = True
        for k in kws:
            g = set(ALIASES.get(k, {k})) | {k}
            if any(x in words for x in g):
                continue
            if len(k) >= 4 and difflib.get_close_matches(k, list(words), n=1, cutoff=0.8):
                continue
            ok = False
            break
        if ok:
            out.append(r)
    return out[:30]


def top_categories(n: int = 6) -> List[Any]:
    try:
        rows = category_rows()
    except Exception:
        return []
    counts: Dict[str, int] = {}
    for r in rows:
        counts[r["_cat"]] = counts.get(r["_cat"], 0) + 1
    order = sorted((s for s in counts if s != "other"), key=lambda s: (-counts[s], cat_label(s)))[:n]
    return [(s, counts[s]) for s in order]


def category_buttons(n: int = 6) -> List[Any]:
    kb: List[Any] = []
    row: List[Any] = []
    for slug, c in top_categories(n):
        row.append({"text": f"{cat_emoji(slug)} {cat_label(slug)} ({c})", "callback_data": f"cat:{slug}:1"})
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    return kb


def most_taken(n: int = 5) -> List[Dict[str, Any]]:
    try:
        logs = supabase("delivery_logs", "GET", query="?select=book_id&order=created_at.desc&limit=1000")
    except Exception:
        return []
    counts: Dict[int, int] = {}
    for x in logs:
        if x.get("book_id"):
            counts[int(x["book_id"])] = counts.get(int(x["book_id"]), 0) + 1
    ids = [i for i, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:n * 2]]
    if not ids:
        return []
    try:
        rows = supabase("books", "GET", query=f"?id=in.({','.join(str(i) for i in ids)})&status=eq.indexed&select=id,title")
    except Exception:
        return []
    by = {int(r["id"]): r for r in rows}
    return [by[i] for i in ids if i in by][:n]


def generic_reply(chat_id: int, user_id: Optional[int]) -> None:
    kb = category_buttons(6)
    top = most_taken(5)
    text = "📚 Kis subject/exam ki chahiye?\nNiche category chuno, ya naam likho (jaise: lucent, ssc gd gk)."
    if top:
        text += "\n\n🔥 Sabse zyada li gayi files:"
        kb += [[{"text": f"🔥 {str(r.get('title') or 'Untitled')[:42]}", "callback_data": f"book:{r['id']}"}] for r in top]
    if not kb:
        kb = [[{"text": label, "callback_data": f"g:s:{val}"}] for label, val in SUBJECT_BUTTONS[:6]]
    send_text(chat_id, text, {"inline_keyboard": kb})


def not_found_reply(chat_id: int, user: Dict[str, Any], keywords: List[str], understood: str = "") -> None:
    q = " ".join(k for k in keywords if k).strip()[:60] or "ye"
    uid = user.get("id")
    kb = category_buttons(6)
    if uid and is_subadmin(uid):
        send_no_results(chat_id, q)
        return
    try:
        who = user_label({"first_name": user.get("first_name"), "last_name": user.get("last_name"),
                          "username": user.get("username")}) if (user.get("first_name") or user.get("username")) else f"user {uid}"
        audit_log(f"🔎 Missing request: {q} ({who}) · {ist_now()} IST" + (f"\n🤖 {understood}" if understood else ""))
    except Exception:
        log_error("missing log")
    send_text(chat_id, f"😕 {q[:1].upper() + q[1:]} abhi nahi hai. Admin ko bata diya ✅\n\n"
                       + ("Ye categories dekh lo 👇" if kb else "Thodi der baad dobara try karna."),
              {"inline_keyboard": kb} if kb else None)



# ================================================================ v10: AI understands first, then the search (Aarav 6:44 PM Oct 26)
# "Bhai ai phle understand krega fir uske liye book search hogi." Every non-command free-text search message:
#   1) one small AI reading (cheapest model of the key's own list, tiny prompt, ≤150 answer tokens, 4 s cap)
#      → {intent, subject, exam, book, topic, keywords}; cached 24 h by normalised text (repeats cost 0 tokens);
#      a 1-word exact file/category name skips the AI;
#   2) the search with what it understood + the alias map + the e497af1 relevance rules (every term must match);
#   3) AI down / slow / limited → the free local parser (e497af1), never an AI error to users.

UNDERSTAND_SYSTEM = (
    "You read messages sent to a Telegram bot that searches study PDFs for Indian govt exams. Messages are "
    "Hindi/Hinglish/English with typos. Reply ONLY with JSON: "
    '{"intent":"search|recommend|greeting|other","subject":null,"exam":null,"book":null,"topic":null,"keywords":[]}. '
    "search = asks for a subject/exam/book/topic; recommend = wants some good book but names no subject/exam/book; "
    "greeting = hi/hello only; other = thanks, chat, not about study files. exam = full name (gd -> \"SSC GD\", "
    "ntpc -> \"RRB NTPC\"). subject like \"GK\", \"Maths\", \"History\". book = a title only if the user names one "
    "(lucent -> \"Lucent GK\"). keywords = 1-4 words likely in a file name, spelling fixed, no filler words "
    "(do, dedo, chahiye, book, pdf, bhai). Never invent books.")
UNDERSTOOD_BY = ("ai", "cache", "local_fallback", "exact")
_UCACHE: Dict[str, Any] = {}
_UNDERSTAND_INTENTS = {"search", "recommend", "greeting", "other"}
_THANK_STRONG = {"thanks", "thank", "thanku", "thankyou", "thnx", "thx", "ty", "shukriya", "dhanyawad", "dhanyavad",
                 "dhanyvad", "शुक्रिया", "धन्यवाद"}
_EXAM_PREFIX = {"ssc", "rrb", "upsc", "exam", "exams", "bharti", "recruitment"}


def is_follow_up(u: Dict[str, Any], ctx: Dict[str, Any]) -> bool:
    """Replies to the bot's own last answer ("aur dikhao", the exam it just asked for, "iski english wali") need
    the conversation, not a fresh reading — they stay on the local path that holds the context."""
    kws = u.get("keywords") or []
    if u.get("intent") == "more":
        return True
    if ctx.get("ask") and ctx.get("pending") and kws and len(kws) <= 3 and \
            all(k in EXAM_WORDS or k in SUBJECT_WORDS for k in kws):
        return True
    return bool(not kws and u.get("ref") and ctx.get("q") and (u.get("lang") or u.get("types")))


def understand_key(text: str) -> str:
    return " ".join(tokenize(text))[:200]


def count_understood(src: str) -> None:
    if src in UNDERSTOOD_BY:
        defer(lambda: _count_understood(src))


def _count_understood(src: str) -> None:
    try:
        d = json.loads(get_setting("understood_by") or "{}")
    except Exception:
        d = {}
    d[src] = int(d.get(src) or 0) + 1
    try:
        set_setting("understood_by", json.dumps(d, separators=(",", ":")))
    except Exception:
        log_error("understood_by")


def understood_counts() -> Dict[str, int]:
    try:
        d = json.loads(get_setting("understood_by") or "{}")
    except Exception:
        d = {}
    return {k: int(d.get(k) or 0) for k in UNDERSTOOD_BY}


def exact_name_hit(text: str) -> bool:
    """One word that already IS a file or category name ("lucent", "history", "polity") — the local search finds it
    exactly, so the AI call is skipped (pure cost saving)."""
    toks = [t for t in tokenize(text) if t]
    if len(toks) != 1:
        return False
    w = toks[0]
    if len(w) < 2 or w in FILLER or w in STRONG_GREET or w in THANK_WORDS or w in OFF_WORDS:
        return False
    if is_key_term(w) or w in CAT_SYNONYMS or known_category(w):
        return True
    try:
        rows = supabase("books", "GET", query="?select=id&status=eq.indexed&limit=1&title=ilike." + urllib.parse.quote(w))
        return bool(rows)
    except Exception:
        return False


def clean_understanding(data: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(data, dict):
        return None
    intent = str(data.get("intent") or "").strip().lower()
    intent = {"book": "search", "find": "search", "recommendation": "recommend", "greet": "greeting",
              "off_topic": "other", "offtopic": "other", "thanks": "other"}.get(intent, intent)
    if intent not in _UNDERSTAND_INTENTS:
        return None
    out: Dict[str, Any] = {"intent": intent}
    for f in ("subject", "exam", "book", "topic"):
        v = data.get(f)
        if isinstance(v, list):
            v = " ".join(str(x) for x in v if isinstance(x, (str, int)))
        v = re.sub(r"\s+", " ", str(v)).strip() if isinstance(v, (str, int)) and str(v).strip().lower() not in {"", "null", "none"} else None
        out[f] = v[:40] if v else None
    kws = []
    for k in (data.get("keywords") or [])[:6] if isinstance(data.get("keywords"), list) else []:
        if isinstance(k, (str, int)) and str(k).strip():
            kws.append(re.sub(r"\s+", " ", str(k)).strip()[:30])
    out["keywords"] = kws[:4]
    return out


def understanding_label(d: Dict[str, Any]) -> str:
    """What the AI understood, for logs / diag: "exam SSC GD · subject GK"."""
    parts = [f"{f} {d[f]}" for f in ("book", "exam", "subject", "topic") if d.get(f)]
    if d.get("keywords"):
        parts.append("words " + ", ".join(d["keywords"]))
    return f"{d.get('intent')}: " + (" · ".join(parts) if parts else "—")


def ai_understand(text: str, user_id: Optional[int], use_cache: bool = True) -> tuple:
    """(understanding, "ai" | "cache") or (None, reason). Never raises, never shows anything.
    One DB read before the AI call (cache + both daily counters together); every write happens after the reply."""
    key = understand_key(text)
    if not key:
        return None, "empty"
    ck = "aiu:" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    if use_cache:
        hit = _UCACHE.get(ck)
        if hit and time.time() - hit[0] < UNDERSTAND_CACHE_TTL:
            return dict(hit[1]), "cache"
    if not ai_provider():
        return None, "no_key"
    ukey = f"aiuday:{int(user_id)}" if user_id else ""
    vals: Dict[str, str] = {}
    try:
        keys = [k for k in (ck, ukey, "aiday:all") if k]
        rows = supabase("bot_settings", "GET", query=f"?key=in.({','.join(keys)})&select=key,value")
        vals = {str(r.get("key")): str(r.get("value") or "") for r in rows or []}
    except Exception:
        log_error("aiu read")
    if use_cache and vals.get(ck):
        try:
            c = json.loads(vals[ck])
            if time.time() - float(c.get("t") or 0) < UNDERSTAND_CACHE_TTL and isinstance(c.get("d"), dict):
                _UCACHE[ck] = (float(c["t"]), c["d"])
                return dict(c["d"]), "cache"
        except Exception:
            pass
    pool = all_ai_keys()
    if pool and all(key_cooling(k) for k in pool):
        return None, "keys_cooling"           # every key is resting after a limit/error: answer locally, at once
    today = ist_day()
    counts: Dict[str, int] = {}
    for k, cap in ((ukey, AI_UNDERSTAND_PER_USER), ("aiday:all", AI_DAILY_TOTAL)):
        if not k:
            continue
        day, _, n = vals.get(k, "").partition(":")
        counts[k] = int(n) if day == today and n.isdigit() else 0
        if counts[k] >= cap:
            return None, "daily_limit"
    content = ai_complete(UNDERSTAND_SYSTEM, "Message: " + text.strip()[:200], max_tokens=150,
                          cap=AI_UNDERSTAND_CAP, fast=True)

    def spend() -> None:
        for k, n in counts.items():
            set_setting(k, f"{today}:{n + 1}")
    defer(spend)
    if not content:
        return None, "ai_failed"
    try:
        d = clean_understanding(json.loads(content[content.find("{"): content.rfind("}") + 1]))
    except Exception:
        d = None
    if not d:
        return None, "bad_json"
    now = time.time()
    _UCACHE[ck] = (now, d)
    if len(_UCACHE) > 500:
        for old in sorted(_UCACHE, key=lambda x: _UCACHE[x][0])[:100]:
            _UCACHE.pop(old, None)

    def save() -> None:
        supabase("bot_settings", "POST", {"key": ck, "updated_at": now_iso(), "value": json.dumps(
            {"t": int(now), "q": key[:80], "d": d}, ensure_ascii=False, separators=(",", ":"))}, "?on_conflict=key")
    defer(save)
    return dict(d), "ai"


_UPRUNE = {"t": 0.0}


def prune_understand_cache() -> None:
    """Drop cached readings older than a day (runs at most hourly, from GET / — the keepalive)."""
    if time.time() - _UPRUNE["t"] < 3600:
        return
    _UPRUNE["t"] = time.time()
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - UNDERSTAND_CACHE_TTL - 3600))
    try:
        supabase("bot_settings", "DELETE", query=f"?key=like.aiu%3A*&updated_at=lt.{cutoff}")
    except Exception:
        log_error("aiu prune")


def _terms(value: Any) -> tuple:
    """Search words of one AI field through the same rules as the local parser: (keywords, lang)."""
    if not value:
        return [], None
    u = understand(" ".join(value) if isinstance(value, list) else str(value))
    return [k for k in u["keywords"] if k not in FILLER], u["lang"]


def understanding_term_sets(d: Dict[str, Any]) -> tuple:
    """Keyword sets to try, most specific first. Every set must match fully (e497af1: never unrelated files).
    Subject / topic / book words are never dropped; only an exam prefix like "ssc" in "ssc gd" may go."""
    lang = None
    structured: List[str] = []
    for f in ("book", "exam", "subject", "topic"):
        t, lg = _terms(d.get(f))
        lang = lang or lg
        structured += [x for x in t if x not in structured]
    kw, lg = _terms(d.get("keywords"))
    lang = lang or lg
    book, _ = _terms(d.get("book"))

    def no_prefix(ws: List[str]) -> List[str]:
        rest = [w for w in ws if w not in _EXAM_PREFIX]
        return rest if rest and any(w in EXAM_WORDS for w in rest) and len(rest) < len(ws) else ws

    sets: List[List[str]] = []
    for s in (structured, no_prefix(structured), book, book[:1] if len(book) > 1 and book[0] not in EXAM_WORDS else [],
              kw, no_prefix(kw)):
        s = s[:5]
        if s and s not in sets:
            sets.append(s)
    return sets, structured, lang


def ai_first_reply(chat_id: int, user: Dict[str, Any], text: str, u: Dict[str, Any], ctx: Dict[str, Any],
                   searched: bool = False) -> bool:
    """True when the message was answered from the AI's reading; False → the local parser answers."""
    if not AI_UNDERSTAND:
        return False
    if exact_name_hit(text):
        count_understood("exact")
        return False
    d, src = ai_understand(text, user.get("id"))
    if not d:
        count_understood("local_fallback")
        return False
    if act_on_understanding(chat_id, user, text, d, u, ctx, searched):
        count_understood(src)
        return True
    count_understood("local_fallback")
    return False


def act_on_understanding(chat_id: int, user: Dict[str, Any], text: str, d: Dict[str, Any], u: Dict[str, Any],
                         ctx: Dict[str, Any], searched: bool = False) -> bool:
    user_id = user.get("id")
    intent = d["intent"]
    local_key = [k for k in u.get("keywords") or [] if is_key_term(k)]
    sets, structured, ai_lang = understanding_term_sets(d)
    if intent == "greeting" and not local_key and not structured:
        send_text(chat_id, greeting_msg(user), None, "HTML")
        return True
    if intent == "other" and not local_key and not structured:
        if u.get("intent") == "thanks" or set(u.get("tokens") or tokenize(text)) & _THANK_STRONG:
            send_text(chat_id, THANKS_MSG)
        else:
            offtopic_reply(chat_id, user, text, searched)
        return True
    if intent == "recommend" and not structured:
        generic_reply(chat_id, user_id)          # "ek bdhiya si book do" → subjects + most-taken files
        return True
    if not sets:
        return False                              # nothing searchable understood ("hindi ki book") → local rules
    lang, types = u.get("lang") or ai_lang, u.get("types") or []
    for kws in sets:
        rows = search_rows(kws, lang, types, require_all=True)
        if rows:
            return send_results(chat_id, user_id, ctx, kws, lang, types, text, rows=rows)
    for kws in sets[:3]:
        fz = fuzzy_rows(kws)
        if fz:
            return send_results(chat_id, user_id, ctx, kws, lang, types, text, rows=fz)
    label = d.get("book") or " ".join(dict.fromkeys(x for x in (d.get("exam"), d.get("subject"), d.get("topic")) if x)) \
        or " ".join(sets[0])
    not_found_reply(chat_id, user, [label], understood=f"AI samjha ({understanding_label(d)}) · msg: “{text.strip()[:80]}”")
    return True



# ================================================================ v9: admin text never gets the user off-topic reply + 📜 log backfill
# Aarav 6:14 PM Oct 26: as owner he typed "Abhi tkk ki sari updates log chat me dal do" and got
# "🙏 Main sirf books / PDF dhoondhne me madad karta hu…". For admins that reply never fires now:
# rules → AI → "🤔 Ye command hai ya search?" [⚙️ Command] [🔍 Search].

_BF_LOG = {"log", "logs", "लॉग"}
_BF_REC = {"update", "updates", "updets", "updatess", "record", "records", "details", "detail", "history", "entries",
           "entry", "data", "jankari", "info"}
_BF_ALL = {"sari", "saari", "sare", "saare", "sab", "sabhi", "all", "purani", "purane", "purana", "old", "pichli",
           "pichle", "puri", "poori", "pura", "poora", "tak", "tk", "tkk", "takk"}
_BF_SEND = {"dal", "daal", "dalo", "daalo", "daldo", "daaldo", "dalna", "daalna", "dalde", "bhej", "bhejo", "bhejdo",
            "bhejde", "bhejna", "send", "post", "forward", "chadhao", "kr", "kro", "karo", "kar", "do", "de"}


def is_log_backfill(low: str, words: set) -> bool:
    """'sari updates log chat me dal do', 'purani updates bhejo', 'abhi tak ka record log me'."""
    if words & {"set", "lagao", "badlo", "hatao", "band", "off"} and not words & _BF_REC:
        return False                      # "log chat set kro" / "log band kro" are other jobs
    marker = bool(words & _BF_ALL) or bool(re.search(r"\b(?:abhi|ab|aaj)\s*(?:tak|tk|tkk|takk)\b|till\s+now|so\s+far", low))
    if not marker:
        return False
    has_log = bool(words & _BF_LOG)
    has_rec = bool(words & _BF_REC)
    has_send = bool(words & _BF_SEND)
    has_place = bool(words & {"chat", "channel", "chanel", "group", "me", "mein", "mai"})
    # "log" alone can mean people ("sab log kitne hai") — needs a record word, a send verb or "log chat/channel/me"
    if has_log and (has_rec or has_send or has_place):
        return True
    return has_rec and has_send and bool(words & {"purani", "purane", "old", "pichli", "pichle", "tak", "tk", "tkk", "takk",
                                                  "sari", "saari", "sab", "sabhi", "all"})


def ask_cmd_or_search(chat_id: int, user_id: int, raw: str, hint: Optional[str] = None,
                      query: Optional[Dict[str, Any]] = None) -> None:
    token = set_op(user_id, {"kind": "search", "q": (raw or "")[:300]})
    text = "🤔 Ye command hai ya search?"
    if raw:
        text += f"\n\n\"{raw[:120]}\""
    if hint and hint.strip() and "command" not in hint.lower():
        text += f"\n\n{hint.strip()[:200]}"
    show(chat_id, text, {"inline_keyboard": [[{"text": "⚙️ Command", "callback_data": f"ad:cmdq:{token}"},
                                              {"text": "🔍 Search", "callback_data": f"ad:op:s:{token}"}]]}, query)


def offtopic_reply(chat_id: int, user: Dict[str, Any], text: str, searched: bool = False) -> None:
    """Non-admins: the fixed off-topic reply. Admins: never — ask command/search, or after 🔍 Search say nothing found."""
    uid = user.get("id")
    if uid and is_subadmin(uid):
        if searched or not (text or "").strip():
            send_text(chat_id, f"😕 \"{(text or '')[:60]}\" ke liye koi file nahi mili.\n"
                               "Book ka naam / subject likho, ya 📂 All Files dekho.",
                      {"inline_keyboard": [[{"text": "📂 All Files", "callback_data": "ad:files:1"}],
                                           [{"text": "⚙️ Admin Panel", "callback_data": "ad:panel"}]]})
        else:
            ask_cmd_or_search(chat_id, int(uid), text)
        return
    send_text(chat_id, OFFTOPIC_MSG, None, "HTML")


CMD_EXAMPLES = ("⚙️ Bolke kaam — aise likho:\n"
                "▸ \"polity ka naam politics kr do\"\n"
                "▸ \"abhi tak ki sari updates log chat me dal do\"\n"
                "▸ \"sari ek baar me upload kro\" (backup)\n"
                "▸ \"lucent wali file hatao\"\n"
                "▸ \"only polity ki sari pdf dikhao\" / \"aaj ki files\" / \"last 5 files\"\n"
                "▸ \"Parmar sir pdf ko polity category me daal do\"\n"
                "▸ \"users dikhao\" / \"status\" / \"bot restart kro\"\n\n"
                "Ya niche se button chuno 👇")


def cmd_choice_view(user_id: int) -> Any:
    kb = [[{"text": "📜 Purani updates Log me bhejo", "callback_data": "ad:logfill"}]] + admin_menu(user_id)["inline_keyboard"]
    return CMD_EXAMPLES, {"inline_keyboard": kb}


# ---- 📜 log backfill: every file added so far → 📝 Log Chat (summary + ~20 files per message, ≥1.1 s apart, resumable)
LOGFILL_PER_MSG = 20
LOGFILL_GAP = 1.1          # seconds between two messages to the log chat (Telegram: ~20 msgs/min per chat)
LOGFILL_BUDGET = 7.0       # seconds per function run; the rest continues by itself / ▶️ Aage bhejo / keepalive
LOGFILL_SLEEP = [time.sleep]
LOGFILL_KEY = "logfill_state"


def logfill_cols() -> str:
    return "id,title,file_name,caption,keywords,source_chat_id,source_chat_title,created_at" + \
        (",category" if has_category_col() else "")


def logfill_state() -> Dict[str, Any]:
    try:
        d = json.loads(get_setting(LOGFILL_KEY) or "{}")
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def logfill_save(st: Dict[str, Any]) -> None:
    set_setting(LOGFILL_KEY, json.dumps(st, ensure_ascii=False, separators=(",", ":")))


def logfill_active(st: Optional[Dict[str, Any]] = None) -> bool:
    st = logfill_state() if st is None else st
    return bool(st) and not st.get("done") and not st.get("stopped")


def setlog_steps_text() -> str:
    return ("📝 Log Chat abhi set nahi hai, isliye purani updates kahin nahi bhej sakta.\n\n"
            "Set karne ke steps:\n"
            "1️⃣ Telegram me naya private channel banao (jaise \"Files Bull Logs\")\n"
            "2️⃣ Channel → Administrators → Add Admin → " + bot_mention() + " → \"Post Messages\" on → Save\n"
            "3️⃣ /admin → 📝 Log Chat → ✏️ Set / Badlo → us channel ki koi bhi post yahan FORWARD karo\n\n"
            "Phir dobara bolo: \"sari updates log chat me dal do\"")


def logfill_preview(chat_id: int, user_id: int, query: Optional[Dict[str, Any]] = None) -> None:
    if not get_setting("log_chat_id"):
        show(chat_id, setlog_steps_text(), {"inline_keyboard": [[{"text": "📝 Log Chat set karo", "callback_data": "ad:setlog"}], BACK_ROW]}, query)
        return
    n = count_rows("books", "&status=eq.indexed")
    if not n:
        show(chat_id, "📜 Abhi koi file save hi nahi hai — bhejne ko kuch nahi.", {"inline_keyboard": [BACK_ROW]}, query)
        return
    msgs = 1 + (n + LOGFILL_PER_MSG - 1) // LOGFILL_PER_MSG
    st = logfill_state()
    note = ""
    if logfill_active(st):
        note = (f"\n\nℹ️ Pichli baar {st.get('sent_files', 0)}/{st.get('total', 0)} bheji thi — "
                "Accept karne pe shuru se dobara jayegi (adhoori wali ke liye ▶️ Aage bhejo).")
    text = f"📜 Log Chat me ab tak ki {n} files ki details jayengi (~{msgs} messages). Bhejun?" + note
    kb_extra = [[{"text": f"▶️ Aage bhejo ({st.get('sent_files', 0)}/{st.get('total', 0)})", "callback_data": "ad:logfill:go"}]] \
        if logfill_active(st) else []
    kb = accept_kb(set_op(user_id, {"kind": "logfill", "n": n}))
    kb["inline_keyboard"] = kb["inline_keyboard"] + kb_extra
    show(chat_id, text, kb, query)


def _orig_name(row: Dict[str, Any]) -> str:
    fn = str(row.get("file_name") or "").strip()
    if fn:
        return fn
    cap = str(row.get("caption") or "").strip().split("\n")[0]
    return cap[:80]


def _norm_nm(s: str) -> str:
    return re.sub(r"[^a-z0-9\u0900-\u097F]+", "", os.path.splitext(str(s or ""))[0].lower())


def _was_renamed(row: Dict[str, Any]) -> bool:
    o = _orig_name(row)
    return bool(o) and _norm_nm(o) != _norm_nm(row.get("title") or "")


def _src_label(row: Dict[str, Any]) -> str:
    title = str(row.get("source_chat_title") or "").strip()
    try:
        sid = int(row.get("source_chat_id") or 0)
    except Exception:
        sid = 0
    if sid > 0 or title.lower() == "admin uploads":
        return "👤 Admin upload"
    return f"📢 {title[:40]}" if title else "📢 Channel"


def logfill_all_rows() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    after = 0
    for _ in range(50):
        rows = supabase("books", "GET", query=f"?select={logfill_cols()}&status=eq.indexed&id=gt.{after}&order=id.asc&limit=1000")
        out.extend(rows)
        if len(rows) < 1000:
            break
        after = int(rows[-1]["id"])
    return out


def logfill_total_users() -> int:
    try:
        if users_table_ok():
            return count_rows("bot_users", col="user_id")
        return len(all_user_recs())
    except Exception:
        return 0


def logfill_summary(rows: List[Dict[str, Any]]) -> str:
    cmap = {} if has_category_col() else catmap()
    counts: Dict[str, int] = {}
    for r in rows:
        slug = book_category(r, cmap)
        counts[slug] = counts.get(slug, 0) + 1
    renamed = sum(1 for r in rows if _was_renamed(r))
    srcs: Dict[str, int] = {}
    for r in rows:
        k = _src_label(r)
        srcs[k] = srcs.get(k, 0) + 1
    order = sorted(counts.items(), key=lambda kv: (kv[0] == "other", -kv[1], cat_label(kv[0])))
    lines = ["📜 Ab tak ka record (" + ist_now() + " IST)", "",
             f"📚 Kul files: {len(rows)}",
             f"👥 Kul users: {logfill_total_users()}",
             f"✏️ Naam badle ab tak: {renamed} (asli file naam se alag)", "",
             "🗂️ Category wise:"]
    for slug, c in order[:40]:
        lines.append(f"{cat_emoji(slug)} {cat_label(slug)}: {c}")
    lines.append("")
    lines.append("📍 Kahan se aayi:")
    for k, c in sorted(srcs.items(), key=lambda kv: -kv[1])[:12]:
        lines.append(f"{k}: {c}")
    lines.append("")
    lines.append(f"Niche har file ki line ({LOGFILL_PER_MSG}-{LOGFILL_PER_MSG} karke): tareekh · kahan se · asli naam → abhi ka naam · category")
    return "\n".join(lines)[:4000]


def logfill_line(n: int, row: Dict[str, Any], cmap: Dict[str, str]) -> str:
    orig = _orig_name(row)
    cur = str(row.get("title") or "Untitled")
    slug = book_category(row, cmap)
    name = f"{orig[:60]} → {cur[:70]}" if orig and _was_renamed(row) else cur[:90]
    when = ist_time(row.get("created_at")) if row.get("created_at") else ""
    return f"{n}. " + (f"{when} · " if when else "") + f"{_src_label(row)}\n   📄 {name} · {cat_emoji(slug)} {cat_label(slug)}"


def _tg_wait(exc: Exception) -> int:
    m = re.search(r"retry after (\d+)", str(exc), re.I)
    return int(m.group(1)) if m else 0


def logfill_send(target: str, text: str, st: Dict[str, Any]) -> None:
    gap = LOGFILL_GAP - (time.time() - float(st.get("last_ts") or 0))
    if gap > 0:
        LOGFILL_SLEEP[0](gap)
    log_chat_send(text)
    st["last_ts"] = time.time()
    st["msgs"] = int(st.get("msgs") or 0) + 1


def logfill_start(user_id: int, chat_id: int, n: int) -> Dict[str, Any]:
    st = {"after": 0, "sent_files": 0, "total": int(n), "msgs": 0, "summary": False, "by": int(user_id),
          "chat": int(chat_id), "t": int(time.time()), "done": False, "last_ts": 0, "lock": 0, "num": 0}
    logfill_save(st)
    return st


def logfill_step(budget: float = LOGFILL_BUDGET) -> Dict[str, Any]:
    """Send as much as fits in the time budget; the cursor is saved after every message (resumable)."""
    st = logfill_state()
    if not logfill_active(st):
        return st
    now = time.time()
    if float(st.get("lock") or 0) > now:
        st["_busy"] = True
        return st                          # another run is sending right now
    if float(st.get("wait_until") or 0) > now:
        st["_wait"] = int(float(st["wait_until"]) - now)
        return st
    target = get_setting("log_chat_id")
    if not target:
        st["stopped"] = "no_log_chat"
        logfill_save(st)
        return st
    started = time.monotonic()
    st["lock"] = now + budget + 5
    logfill_save(st)
    cmap = {} if has_category_col() else catmap()
    try:
        if not st.get("summary"):
            rows = logfill_all_rows()
            st["total"] = len(rows)
            logfill_send(target, logfill_summary(rows), st)
            st["summary"] = True
            logfill_save(st)
        while time.monotonic() - started + LOGFILL_GAP + 1.0 < budget:
            rows = supabase("books", "GET", query=f"?select={logfill_cols()}&status=eq.indexed&id=gt.{int(st['after'])}"
                                                  f"&order=id.asc&limit={LOGFILL_PER_MSG}")
            if not rows:
                st["done"] = True
                break
            num = int(st.get("num") or 0)
            first = num + 1
            lines = [logfill_line(num + i + 1, r, cmap) for i, r in enumerate(rows)]
            head = f"📜 Files {first}–{num + len(rows)} / {max(int(st.get('total') or 0), num + len(rows))}"
            body = head + "\n\n" + "\n".join(lines)
            if len(body) > 4000:
                body = body[:3990] + "…"
            logfill_send(target, body, st)
            st["num"] = num + len(rows)
            st["sent_files"] = st["num"]
            st["after"] = int(rows[-1]["id"])
            logfill_save(st)
            if len(rows) < LOGFILL_PER_MSG:
                st["done"] = True
                break
    except Exception as exc:
        wait = _tg_wait(exc)
        if wait:
            st["wait_until"] = time.time() + wait + 1
        else:
            st["errors"] = int(st.get("errors") or 0) + 1
            st["last_error"] = type(exc).__name__ + ": " + str(exc)[:120].replace(BOT_TOKEN or "\0", "***")
            if st["errors"] >= 5:
                st["stopped"] = "errors"
        log_error("logfill")
    st["lock"] = 0
    if st.get("done"):
        st["finished"] = int(time.time())
    logfill_save(st)
    return st


def logfill_status_text(st: Dict[str, Any]) -> str:
    total = int(st.get("total") or 0)
    sent = int(st.get("sent_files") or 0)
    if st.get("done"):
        return f"✅ Log Chat me ab tak ki {sent} files ki details bhej di ({int(st.get('msgs') or 0)} messages: summary + list)."
    if st.get("stopped") == "no_log_chat":
        return "❌ Log Chat hata diya gaya, isliye ruk gaya. Log Chat set karke dobara bolo."
    if st.get("stopped"):
        return f"❌ {sent}/{total} bhejne ke baad ruk gaya (Telegram error). ▶️ Aage bhejo dabao."
    extra = ""
    if st.get("_wait"):
        extra = f"\nTelegram ne {st['_wait']} sec rukne ko kaha — uske baad apne-aap aage."
    elif st.get("_busy"):
        extra = "\nAbhi bhej raha hai…"
    return f"⏳ {sent}/{total} files bhej di. Baaki apne-aap jaa rahi hain (atke to ▶️ Aage bhejo).{extra}"


def logfill_markup(st: Dict[str, Any]) -> Dict[str, Any]:
    kb: List[Any] = []
    if logfill_active(st) or st.get("stopped") == "errors":
        kb.append([{"text": "▶️ Aage bhejo", "callback_data": "ad:logfill:go"}])
    kb.append(BACK_ROW)
    return {"inline_keyboard": kb}


def logfill_kick_key() -> str:
    return hmac.new((WEBHOOK_SECRET or "x").encode(), b"logfill", hashlib.sha256).hexdigest()[:24]


def logfill_kick() -> None:
    """Start the next run in a fresh function call (the cursor is saved, so a lost kick only waits for keepalive)."""
    if _DIAG["dry"]:
        _DIAG["sent"].append({"ms": 0, "m": "logfill_kick"})
        return
    try:
        urllib.request.urlopen(urllib.request.Request(PUBLIC_HOME + "api/logfill?k=" + logfill_kick_key(),
                                                      headers={"User-Agent": "filesbull-bot/1.0"}), timeout=1.5).read()
    except Exception:
        pass


def logfill_notify_done(st: Dict[str, Any]) -> None:
    if st.get("done") and not st.get("notified") and st.get("chat"):
        st["notified"] = True
        logfill_save(st)
        try:
            send_text(int(st["chat"]), logfill_status_text(st), {"inline_keyboard": [BACK_ROW]})
        except Exception:
            log_error("logfill notify")


@app.get("/api/logfill")
def logfill_endpoint() -> Any:
    tok = request.args.get("k", "")
    diag = request.args.get("t", "")
    ok = bool(tok) and hmac.compare_digest(tok, logfill_kick_key())
    if not ok and diag:
        ok = hmac.compare_digest(hashlib.sha256(diag.encode()).hexdigest(), DIAG_TOKEN_SHA256)
    if not ok:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    _REQ_T0[0] = time.monotonic()
    st = logfill_state()
    if not logfill_active(st):            # only continues a run an admin already accepted
        return jsonify({"ok": True, "active": False, "done": bool(st.get("done")), "sent": st.get("sent_files", 0)})
    st = logfill_step()
    logfill_notify_done(st)
    if logfill_active(st) and not st.get("_busy"):
        if st.get("_wait"):
            LOGFILL_SLEEP[0](min(int(st["_wait"]), 6))
        logfill_kick()
    return jsonify({"ok": True, "active": logfill_active(st), "sent": st.get("sent_files", 0), "total": st.get("total", 0)})


def logfill_run_from_button(query: Optional[Dict[str, Any]], chat_id: int) -> None:
    cb_answer(query, "📜 Log Chat me bhej raha hu...")
    st = logfill_step(max(2.5, LOGFILL_BUDGET - req_elapsed()))
    show(chat_id, logfill_status_text(st), logfill_markup(st), query)
    if st.get("done"):
        st["notified"] = True
        logfill_save(st)
    elif logfill_active(st) and not st.get("_busy"):
        logfill_kick()


def log_setting_view() -> Any:
    text, kb = setting_view("log_chat_id", "📝 Log Chat (User Track)", "ad:setlog", "ad:logoff",
                            "Yahan aayega: 🆕 nayi file (kab, kahan se, purana naam → naya naam, category), ✏️ naam badla (kisne), "
                            "📥 kis user ne kaunsi file li, 🗂️ nayi category bani. Set na ho to /logs se last 50 dekho.")
    rows = kb["inline_keyboard"]
    rows.insert(len(rows) - 1, [{"text": "📜 Purani updates Log me bhejo", "callback_data": "ad:logfill"}])
    if get_setting("log_chat_id"):
        rows.insert(len(rows) - 1, [{"text": "🧪 Log Test", "callback_data": "ad:logtest"}])
        text += "\n" + log_chat_line()
    text += "\n\n👥 Group ko Log Chat banana ho: bot ko group me admin banao, phir us group me /setlogchat bhejo."
    return text, kb



# ---- BIG-FILE RENAME (>20 MB) through MTProto in a GitHub Action (Aarav 7:36 PM Oct 26: "koi hor treeka h kya")
# The Bot HTTP API cannot download files over 20 MB, so it cannot re-upload them under a new FILE name.
# MTProto (the full Telegram protocol) lets the same bot download/upload up to 2 GB. That runs in the GitHub Action
# .github/workflows/bigfile-rename.yml (tools/bigfile_runner.py):
#   1. The bot queues a job in bot_settings "bigq" and triggers the workflow (GitHub API, token GH_TOKEN in Vercel);
#      without that token the workflow's own 5-minute schedule picks the queue up.
#   2. The runner proves who it is with GitHub's OIDC token (signed by GitHub: this repo, main branch, this workflow)
#      at POST /api/bigfile-job and gets the job + the bot credentials (BOT_TOKEN, TG_API_ID, TG_API_HASH from Vercel;
#      GitHub Actions secrets with the same names are used instead when they exist). No secret is stored in the repo.
#   3. It downloads the file from the Backup channel post, uploads it as a document named "<clean name>.<ext>" with
#      caption "📄 name / 🗂️ subject" to the Backup channel, deletes the temp file and reports to POST /api/bigfile-done
#      signed with an HMAC key the bot gave for that one job.
#   4. The bot reads the new post's file_id (forward + delete, file name checked), stores it in "dlf:<id>" and every
#      later send uses it (the Bot API can send any size by file_id). Failure → one retry, then "❌" + reason to the owner.
TG_API_ID = os.environ.get("TG_API_ID", "").strip()
TG_API_HASH = os.environ.get("TG_API_HASH", "").strip()
GH_REPO = os.environ.get("GH_REPO", "aaravjaat72-hash/filesbull").strip()
GH_TOKEN_NAMES = ("GH_TOKEN", "GH_DISPATCH_TOKEN", "GITHUB_TOKEN", "GH_PAT", "GITHUB_PAT")
GH_DISPATCH_TOKEN = next((os.environ.get(n, "").strip() for n in GH_TOKEN_NAMES if os.environ.get(n, "").strip()), "")
BIG_WORKFLOW = "bigfile-rename.yml"
BIG_QUEUE_KEY = "bigq"
BIG_TRIES = 2                       # first try + one retry
BIG_STALE_SEC = 15 * 60            # a claimed job with no report after this counts as a failed try
OIDC_ISS = "https://token.actions.githubusercontent.com"
OIDC_AUD = "filesbull-bigfile"
BIG_STATUS_TEXT = "⏳ Badi file ka naam badal raha hai (2–4 min)…"   # measured: 34.7 MB took 3 min 40 s on Oct 26
BIG_STATUS_SLOW = "⏳ Badi file ka naam badal raha hai (5–15 min)…"
_BIG_SECRETS = [s for s in (TG_API_HASH, GH_DISPATCH_TOKEN) if s]


def bigfile_ready() -> bool:
    return bool(TG_API_ID and TG_API_HASH and BOT_TOKEN)


def bigfile_status() -> Dict[str, Any]:
    st: Dict[str, Any] = {"state": "ready" if bigfile_ready() else "missing_secrets",
                          "trigger": "instant" if GH_DISPATCH_TOKEN else "schedule_5min (GH_TOKEN missing)"}
    if not bigfile_ready():
        st["missing"] = [n for n, v in (("TG_API_ID", TG_API_ID), ("TG_API_HASH", TG_API_HASH), ("BOT_TOKEN", BOT_TOKEN)) if not v]
    try:
        q = bigq_load()
        cnt: Dict[str, int] = {}
        for j in q:
            cnt[j.get("st", "?")] = cnt.get(j.get("st", "?"), 0) + 1
        st["queue"] = cnt
        last = [j for j in q if j.get("st") in ("done", "failed")]
        if last:
            j = last[-1]
            st["last"] = {"st": j.get("st"), "name": str(j.get("fn") or "")[:80], "err": str(j.get("err") or "")[:120] or None}
    except Exception:
        pass
    return st


def _scrub(text: str) -> str:
    text = str(text or "")
    for s in (BOT_TOKEN, *_BIG_SECRETS):
        if s:
            text = text.replace(s, "***")
    return re.sub(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b", "***", text)


def bigq_load() -> List[Dict[str, Any]]:
    try:
        v = json.loads(get_setting(BIG_QUEUE_KEY) or "[]")
        return v if isinstance(v, list) else []
    except Exception:
        return []


def bigq_save(q: List[Dict[str, Any]]) -> None:
    # keep every open job + the last 40 finished ones
    open_ = [j for j in q if j.get("st") in ("pending", "running")]
    done = [j for j in q if j.get("st") not in ("pending", "running")][-40:]
    keep = sorted(open_ + done, key=lambda j: j.get("t", 0))
    set_setting(BIG_QUEUE_KEY, json.dumps(keep, ensure_ascii=False, separators=(",", ":")))


def is_big_doc(row: Dict[str, Any]) -> bool:
    mime = str(row.get("mime_type") or "").lower()
    try:
        size = int(row.get("file_size") or 0)
    except (TypeError, ValueError):
        size = 0
    return size > TG_FILE_LIMIT and not mime.startswith(("video/", "audio/")) and \
        bool(row.get("telegram_file_id") or (row.get("backup_chat_id") and row.get("backup_message_id")) or
             (row.get("source_chat_id") and row.get("source_message_id")))


def delivered_name(row: Dict[str, Any]) -> str:
    """The FILE name a user receives right now."""
    try:
        v = json.loads(get_setting(f"dlf:{row.get('id')}") or "null")
    except Exception:
        v = None
    if isinstance(v, dict) and v.get("f") and v.get("n") == str(row.get("title") or "") and v.get("fn"):
        return str(v["fn"])
    if renamed_copy(row):
        return safe_filename(str(row.get("title") or ""), str(row.get("file_name") or ""))
    return str(row.get("file_name") or "")


def big_needs_rename(row: Dict[str, Any]) -> bool:
    title = str(row.get("title") or "").strip()
    if not title or not row.get("id") or not is_big_doc(row):
        return False
    return delivered_name(row) != safe_filename(title, str(row.get("file_name") or ""))


def bigfile_enqueue(rows: List[Dict[str, Any]], chat_id: Optional[int], who: str = "") -> List[Dict[str, Any]]:
    """Queue >20 MB documents whose delivered name differs from the display name. Returns the jobs added."""
    q = bigq_load()
    added: List[Dict[str, Any]] = []
    for r in rows:
        if not big_needs_rename(r):
            continue
        title = str(r.get("title") or "").strip()
        fn = safe_filename(title, str(r.get("file_name") or ""))
        if any(j.get("b") == r["id"] and j.get("fn") == fn and j.get("st") in ("pending", "running") for j in q):
            continue
        for j in q:                     # an older open job for the same file (other name) is replaced
            if j.get("b") == r["id"] and j.get("st") in ("pending", "running"):
                j["st"], j["err"] = "replaced", "naya naam aa gaya"
        job = {"j": hashlib.sha1(os.urandom(12)).hexdigest()[:12], "b": int(r["id"]), "n": title[:300], "fn": fn,
               "sz": int(r.get("file_size") or 0), "st": "pending", "tries": 0, "chat": chat_id, "t": int(time.time()),
               "who": who[:60]}
        q.append(job)
        added.append(job)
    if added:
        bigq_save(q)
    return added


def bigfile_dispatch(job_id: str = "") -> str:
    """Start the workflow now. 'dispatched' | 'no_token' | 'error: …'. Never raises."""
    if not GH_DISPATCH_TOKEN:
        return "no_token"
    if _DIAG["dry"]:
        _DIAG["sent"].append({"ms": 0, "m": "bigfile_dispatch"})
        return "dispatched"
    url = f"https://api.github.com/repos/{GH_REPO}/actions/workflows/{BIG_WORKFLOW}/dispatches"
    body = json.dumps({"ref": "main", "inputs": {"job": str(job_id or "queue")}}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {GH_DISPATCH_TOKEN}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "filesbull-bot", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return "dispatched" if resp.status in (200, 204) else f"error: HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        try:
            why = json.loads(exc.read().decode("utf-8", "replace")).get("message", "")
        except Exception:
            why = ""
        return _scrub(f"error: HTTP {exc.code} {why}")[:160]
    except Exception as exc:
        return _scrub(f"error: {type(exc).__name__}")[:160]


def bigfile_start(rows: List[Dict[str, Any]], chat_id: Optional[int], who: str = "") -> Dict[str, Any]:
    """Queue + trigger. Returns {jobs, dispatch, text} (text = owner status line, '' when nothing queued)."""
    if not bigfile_ready():
        return {"jobs": [], "dispatch": "missing_secrets", "text": ""}
    jobs = bigfile_enqueue(rows, chat_id, who)
    if not jobs:
        return {"jobs": [], "dispatch": "", "text": ""}
    d = bigfile_dispatch(jobs[0]["j"])
    for j in jobs:
        audit_log(f"⏳ Badi file rename queue · {ist_now()}\n📄 {j['fn'][:80]} ({human_size(j['sz'])})")
    text = BIG_STATUS_TEXT if d == "dispatched" else BIG_STATUS_SLOW
    if len(jobs) > 1:
        text += f" ({len(jobs)} files)"
    return {"jobs": jobs, "dispatch": d, "text": text}


def bigfile_after_rename(chat_id: int, ids: List[int], who: str = "") -> str:
    """After a rename was accepted: >20 MB documents get a real new file name. Returns a status line or ''."""
    if not ids or not bigfile_ready():
        return ""
    try:
        rows = supabase("books", "GET", query=f"?id=in.({','.join(str(int(i)) for i in ids[:50])})&select=*")
    except Exception:
        log_error("bigfile rows")
        return ""
    res = bigfile_start(rows, chat_id, who)
    return res["text"]


def big_candidates(limit: int = 200) -> List[Dict[str, Any]]:
    rows = supabase("books", "GET", query=f"?select=*&status=eq.indexed&file_size=gt.{TG_FILE_LIMIT}&order=id.desc&limit={limit}")
    return [r for r in rows if big_needs_rename(r)]


def bigfix_view(chat_id: int, user_id: int, query: Optional[Dict[str, Any]] = None) -> None:
    """🔁 Badi files ka naam theek karo: queue every >20 MB file whose delivered name ≠ display name."""
    back = {"inline_keyboard": [BACK_ROW]}
    if not bigfile_ready():
        show(chat_id, "❌ Badi file ka naam badalne ke liye Vercel me TG_API_ID aur TG_API_HASH chahiye "
                      "(Settings → Environment Variables), phir Redeploy.\nTab tak caption me naya naam dikhega.", back, query)
        return
    try:
        rows = big_candidates()
    except Exception:
        log_error("big_candidates")
        show(chat_id, "❌ Files padh nahi paya, thodi der baad dobara try karo.", back, query)
        return
    if not rows:
        open_ = [j for j in bigq_load() if j.get("st") in ("pending", "running")]
        show(chat_id, (f"⏳ {len(open_)} badi file ka naam abhi badal raha hai…" if open_ else
                       "✅ Saari 20 MB se badi files pehle se sahi naam se jaati hain."), back, query)
        return
    res = bigfile_start(rows, chat_id, actor_label(user_id))
    lines = [res["text"] or BIG_STATUS_TEXT, ""]
    for j in res["jobs"][:12]:
        lines.append(f"• {j['fn'][:70]} ({human_size(j['sz'])})")
    if len(res["jobs"]) > 12:
        lines.append(f"…aur {len(res['jobs']) - 12}")
    if not res["jobs"]:
        lines = ["⏳ Ye files pehle se queue me hain, thodi der me ho jayengi."]
    lines.append("\nHar file ke baad yahin \"✅ Ho gaya\" aayega. Tab tak caption me naya naam dikhega.")
    show(chat_id, "\n".join(lines)[:3900], back, query)


# -- runner authentication: GitHub's OIDC token (RS256, verified here with GitHub's published keys)
_JWKS: Dict[str, Any] = {"t": 0.0, "keys": {}}


def _b64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def oidc_keys(force: bool = False) -> Dict[str, Any]:
    if force or not _JWKS["keys"] or time.time() - _JWKS["t"] > 3600:
        with urllib.request.urlopen(OIDC_ISS + "/.well-known/jwks", timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        _JWKS["keys"] = {k["kid"]: (int.from_bytes(_b64u(k["n"]), "big"), int.from_bytes(_b64u(k["e"]), "big"))
                         for k in data.get("keys", []) if k.get("kty") == "RSA" and k.get("kid")}
        _JWKS["t"] = time.time()
    return _JWKS["keys"]


_SHA256_DI = bytes.fromhex("3031300d060960864801650304020105000420")


def rs256_ok(signing_input: bytes, sig: bytes, n: int, e: int) -> bool:
    k = (n.bit_length() + 7) // 8
    if len(sig) != k or k < 256:
        return False
    s = int.from_bytes(sig, "big")
    if s >= n:
        return False
    m = pow(s, e, n).to_bytes(k, "big")
    digest = _SHA256_DI + hashlib.sha256(signing_input).digest()
    expected = b"\x00\x01" + b"\xff" * (k - len(digest) - 3) + b"\x00" + digest
    return hmac.compare_digest(m, expected)


def verify_oidc(token: str, workflow: str = "", aud_want: str = "") -> Dict[str, Any]:
    """Claims of a valid GitHub Actions OIDC token from THIS repo's workflow (default: bigfile) on main. Raises InputError."""
    workflow = workflow or BIG_WORKFLOW
    aud_want = aud_want or OIDC_AUD
    try:
        h64, p64, s64 = str(token or "").split(".")
        header = json.loads(_b64u(h64))
        claims = json.loads(_b64u(p64))
        sig = _b64u(s64)
    except Exception:
        raise InputError("bad token")
    if header.get("alg") != "RS256":
        raise InputError("bad alg")
    keys = oidc_keys()
    if header.get("kid") not in keys:
        keys = oidc_keys(force=True)
    key = keys.get(header.get("kid"))
    if not key or not rs256_ok(f"{h64}.{p64}".encode(), sig, *key):
        raise InputError("bad signature")
    now = time.time()
    aud = claims.get("aud")
    if claims.get("iss") != OIDC_ISS or (aud != aud_want and not (isinstance(aud, list) and aud_want in aud)):
        raise InputError("bad issuer/audience")
    if not (float(claims.get("exp", 0)) > now - 30 and float(claims.get("nbf", claims.get("iat", 0)) or 0) <= now + 60):
        raise InputError("expired")
    wf = f"{GH_REPO}/.github/workflows/{workflow}@refs/heads/main"
    if (claims.get("repository") != GH_REPO or claims.get("ref") != "refs/heads/main" or
            claims.get("workflow_ref") != wf or
            claims.get("event_name") not in ("workflow_dispatch", "repository_dispatch", "schedule")):
        raise InputError("wrong repo/workflow")
    return claims


def big_job_key(job_id: str) -> str:
    master = os.environ.get("BIGFILE_SECRET", "").strip() or hashlib.sha256(f"filesbull-bigfile:{BOT_TOKEN}".encode()).hexdigest()
    return hmac.new(master.encode(), f"bigjob:{job_id}".encode(), hashlib.sha256).hexdigest()


def big_sign(job_id: str, body: bytes) -> str:
    return hmac.new(big_job_key(job_id).encode(), body, hashlib.sha256).hexdigest()


def bigfile_sweep(q: Optional[List[Dict[str, Any]]] = None, save: bool = True) -> List[Dict[str, Any]]:
    """Claimed jobs that never reported: count as a failed try (retry once, then ❌)."""
    q = bigq_load() if q is None else q
    changed = False
    now = time.time()
    for j in q:
        if j.get("st") == "running" and now - float(j.get("ct") or 0) > BIG_STALE_SEC:
            big_fail(j, "GitHub Action ne time pe jawab nahi diya")
            changed = True
    if changed and save:
        bigq_save(q)
        if any(j.get("st") == "pending" for j in q):
            bigfile_dispatch("retry")
    return q


def big_notify(job: Dict[str, Any], text: str) -> None:
    chat = job.get("chat")
    if chat:
        try:
            send_text(int(chat), text)
        except Exception:
            log_error("big notify")


def big_fail(job: Dict[str, Any], reason: str) -> None:
    reason = _scrub(reason)[:200] or "pata nahi"
    job["err"] = reason
    if int(job.get("tries") or 0) < BIG_TRIES:
        job["st"] = "pending"           # one more try
        audit_log(f"🔁 Badi file rename dobara try · {job.get('fn', '')[:70]}\nWajah: {reason[:120]}")
        return
    job["st"] = "failed"
    big_notify(job, f"❌ Badi file ka naam nahi badla: {job.get('fn', '')[:80]}\nWajah: {reason}\n"
                    "File pehle jaisi users ko ja rahi hai (caption me naya naam).")
    audit_log(f"❌ Badi file rename fail · {ist_now()}\n📄 {job.get('fn', '')[:80]}\nWajah: {reason[:120]}")


@app.post("/api/bigfile-job")
def bigfile_job_api() -> Any:
    """The GitHub Action asks for work. Auth: GitHub OIDC token of this repo's bigfile workflow on main."""
    body = request.get_json(silent=True) or {}
    try:
        claims = verify_oidc(str(body.get("oidc") or ""))
    except InputError as exc:
        return jsonify({"ok": False, "error": "unauthorized", "why": str(exc)}), 401
    except Exception as exc:
        return jsonify({"ok": False, "error": "unauthorized", "why": type(exc).__name__}), 401
    q = bigfile_sweep(save=False)
    runner_has = body.get("has") or {}
    jobs_out: List[Dict[str, Any]] = []
    claim = [j for j in q if j.get("st") == "pending"][:5]
    if claim:
        ids = ",".join(str(int(j["b"])) for j in claim)
        rows = {int(r["id"]): r for r in supabase("books", "GET", query=f"?id=in.({ids})&select=*")}
        for j in claim:
            r = rows.get(int(j["b"]))
            if not r:
                j["st"], j["err"] = "failed", "file DB me nahi mili"
                continue
            if str(r.get("title") or "").strip() != j.get("n"):
                j["st"], j["err"] = "replaced", "naam fir badal gaya"
                continue
            j["st"], j["ct"], j["tries"] = "running", int(time.time()), int(j.get("tries") or 0) + 1
            j["run"] = str(claims.get("run_id") or "")
            target = get_setting("backup_chat_id") or r.get("backup_chat_id")
            jobs_out.append({"j": j["j"], "k": big_job_key(j["j"]), "book": int(r["id"]), "file_name": j["fn"],
                             "caption": delivery_caption(r), "size": int(r.get("file_size") or 0),
                             "file_id": r.get("telegram_file_id"),
                             "from": [x for x in ([r.get("backup_chat_id"), r.get("backup_message_id")],
                                                  [r.get("source_chat_id"), r.get("source_message_id")]) if x[0] and x[1]],
                             "to": target, "try": j["tries"]})
        bigq_save(q)
    out: Dict[str, Any] = {"ok": True, "jobs": jobs_out}
    if jobs_out:
        creds: Dict[str, Any] = {}
        if not runner_has.get("bot_token") and BOT_TOKEN:
            creds["bot_token"] = BOT_TOKEN
        if not runner_has.get("api") and TG_API_ID and TG_API_HASH:
            creds["api_id"], creds["api_hash"] = TG_API_ID, TG_API_HASH
        out["creds"] = creds
    flush_request()
    return jsonify(out)


def big_read_file_id(job: Dict[str, Any], chat_id: Any, message_id: Any) -> Dict[str, Any]:
    """Forward the new post to a private chat, read document.file_id / file_name, delete the forward."""
    target = job.get("chat") or get_setting("log_chat_id") or (sorted(ADMIN_USER_IDS)[0] if ADMIN_USER_IDS else None)
    res = telegram("forwardMessage", {"chat_id": int(target), "from_chat_id": int(chat_id), "message_id": int(message_id),
                                      "disable_notification": True})
    msg = (res or {}).get("result") or {}
    doc = msg.get("document") or msg.get("video") or msg.get("audio") or {}
    try:
        telegram("deleteMessage", {"chat_id": int(target), "message_id": int(msg.get("message_id"))})
    except Exception:
        pass
    return {"file_id": doc.get("file_id"), "file_name": doc.get("file_name"), "file_unique_id": doc.get("file_unique_id")}


@app.post("/api/bigfile-done")
def bigfile_done_api() -> Any:
    """The GitHub Action reports a job. Auth: X-Filesbull-Signature = HMAC-SHA256(job key, raw body)."""
    raw = request.get_data() or b""
    try:
        body = json.loads(raw.decode("utf-8") or "{}")
    except Exception:
        body = {}
    jid = str(body.get("j") or "")
    sig = request.headers.get("X-Filesbull-Signature", "")
    if not jid or not sig or not BOT_TOKEN or not hmac.compare_digest(sig, big_sign(jid, raw)):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    _REQ.clear()
    _LOG_QUEUE.clear()
    q = bigq_load()
    job = next((j for j in q if j.get("j") == jid), None)
    if not job:
        return jsonify({"ok": False, "error": "unknown job"}), 404
    if job.get("st") != "running":
        return jsonify({"ok": True, "ignored": job.get("st")})
    result: Dict[str, Any] = {"ok": True}
    if body.get("ok") and body.get("chat_id") and body.get("message_id"):
        try:
            info = big_read_file_id(job, body["chat_id"], body["message_id"])
            if not info.get("file_id"):
                raise RuntimeError("nayi file ka file_id nahi mila")
            if info.get("file_name") and info["file_name"] != job.get("fn"):
                raise RuntimeError(f"file ka naam galat aaya: {str(info['file_name'])[:60]}")
            set_setting(f"dlf:{int(job['b'])}", json.dumps({"f": info["file_id"], "n": job.get("n"), "fn": job.get("fn"),
                                                          "c": int(body["chat_id"]), "m": int(body["message_id"])},
                                                         ensure_ascii=False))
            try:
                rows = supabase("books", "GET", query=f"?id=eq.{int(job['b'])}&select=id,backup_chat_id&limit=1")
                if rows and str(rows[0].get("backup_chat_id") or "") in ("", str(body["chat_id"])):
                    supabase("books", "PATCH", {"backup_chat_id": int(body["chat_id"]), "backup_message_id": int(body["message_id"])},
                             f"?id=eq.{int(job['b'])}")
            except Exception:
                log_error("big backup ids")
            job["st"], job["err"], job["done_at"] = "done", "", int(time.time())
            job["file_name"] = info.get("file_name") or job.get("fn")
            big_notify(job, f"✅ Ho gaya: {job.get('fn', '')[:80]}\nAb users ko file isi naam se jayegi.")
            audit_log(f"✅ Badi file ka naam badla · {ist_now()}\n📄 {job.get('fn', '')[:80]} ({human_size(job.get('sz'))})")
            result["file_name"] = job["file_name"]
        except Exception as exc:
            log_error("bigfile done")
            big_fail(job, str(exc))
    else:
        big_fail(job, str(body.get("error") or "GitHub Action fail hua"))
    bigq_save(q)
    if job.get("st") == "pending":
        result["retry"] = bigfile_dispatch(jid)
    result["state"] = job.get("st")
    flush_request()
    return jsonify(result)


# ================================================================ 👤 USERBOT: auto-forward from channels where the bot is NOT admin
# (Aarav, Oct 26 2026 8:04/8:24 PM). A bot can't read a channel it isn't admin in, so the owner logs in his OWN Telegram
# account once (web page below, phone → code → 2FA). The session string is stored ENCRYPTED in bot_settings
# (key derived from USERBOT_SECRET or BOT_TOKEN). A GitHub Action (.github/workflows/userbot-forward.yml, every 10 min
# + "🔄 Abhi check karo") proves itself with GitHub OIDC, gets the session, forwards NEW file posts of those channels to
# the Backup channel (re-uploads when the channel blocks forwarding), gently (≤25 files/run, 2–4 s apart, stops on
# FloodWait) and reports each one here; the bot then saves/names/categorises it like an admin-channel post.
UB_WORKFLOW = "userbot-forward.yml"
UB_AUD = "filesbull-userbot"
UB_SESS_KEY = "ub:sess"
UB_STATE_KEY = "ub:state"
UB_PAUSE_KEY = "ub:paused"
UB_LOGIN_KEY = "ub:login"
UB_LOGIN_TTL = 600                # a half-done login (phone/code step) is deleted after 10 min
UB_MAX_PER_RUN = 25
UB_FIRST_RUN_MAX = 20             # first look at a channel: only its last ~20 posts of the last 24 h, no history dump
UB_ALERT_EVERY = 3600             # the same "❌ Userbot: …" at most once per hour
UB_DEVICE = "Filesbull Userbot"
_UB_SECRETS: List[str] = []


def ub_master() -> bytes:
    raw = os.environ.get("USERBOT_SECRET", "").strip() or hashlib.sha256(f"filesbull-userbot:{BOT_TOKEN}".encode()).hexdigest()
    return raw.encode()


def _ub_keys() -> tuple:
    m = ub_master()
    return (hmac.new(m, b"filesbull-userbot-enc-v1", hashlib.sha256).digest(),
            hmac.new(m, b"filesbull-userbot-mac-v1", hashlib.sha256).digest())


def ub_encrypt(plain: str) -> str:
    """Encrypt-then-MAC with stdlib only: HMAC-SHA256 counter keystream + 16-byte HMAC tag."""
    ek, mk = _ub_keys()
    nonce = os.urandom(16)
    data = plain.encode("utf-8")
    ks = b"".join(hmac.new(ek, nonce + i.to_bytes(8, "big"), hashlib.sha256).digest() for i in range(len(data) // 32 + 1))
    ct = bytes(a ^ b for a, b in zip(data, ks))
    tag = hmac.new(mk, b"v1" + nonce + ct, hashlib.sha256).digest()[:16]
    return "v1." + base64.urlsafe_b64encode(nonce + ct + tag).decode().rstrip("=")


def ub_decrypt(blob: str) -> Optional[str]:
    """None when missing, tampered or encrypted under another key (e.g. BOT_TOKEN changed)."""
    try:
        if not blob or not str(blob).startswith("v1."):
            return None
        raw = _b64u(str(blob)[3:])
        nonce, ct, tag = raw[:16], raw[16:-16], raw[-16:]
        ek, mk = _ub_keys()
        if len(nonce) != 16 or not hmac.compare_digest(tag, hmac.new(mk, b"v1" + nonce + ct, hashlib.sha256).digest()[:16]):
            return None
        ks = b"".join(hmac.new(ek, nonce + i.to_bytes(8, "big"), hashlib.sha256).digest() for i in range(len(ct) // 32 + 1))
        return bytes(a ^ b for a, b in zip(ct, ks)).decode("utf-8")
    except Exception:
        return None


def ub_scrub(text: Any) -> str:
    text = _scrub(str(text or ""))
    for s in _UB_SECRETS:
        if s:
            text = text.replace(s, "***")
    text = re.sub(r"\+?\d[\d \-]{8,}\d", "***", text)          # phone numbers
    return re.sub(r"[A-Za-z0-9_\-+/]{100,}={0,2}", "***", text)  # session strings / long tokens


def ub_t(ts: Any) -> str:
    try:
        return time.strftime("%d %b %I:%M %p", time.gmtime(float(ts) + IST_OFFSET))
    except Exception:
        return ""


def ub_ready() -> bool:
    return bool(TG_API_ID and TG_API_HASH and BOT_TOKEN)


def ub_state() -> Dict[str, Any]:
    try:
        v = json.loads(get_setting(UB_STATE_KEY) or "{}")
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def ub_save_state(st: Dict[str, Any]) -> None:
    set_setting(UB_STATE_KEY, json.dumps(st, ensure_ascii=False, separators=(",", ":")))


def ub_del(key: str) -> None:
    try:
        supabase("bot_settings", "DELETE", query=f"?key=eq.{urllib.parse.quote(key)}")
    except Exception:
        log_error("ub del")
    _set_memo().pop(key, None)
    _SET_CACHE.pop(key, None)


def ub_session() -> Optional[str]:
    blob = get_setting(UB_SESS_KEY)
    return ub_decrypt(blob) if blob else None


def ub_paused() -> bool:
    return (get_setting(UB_PAUSE_KEY) or "") == "1"


def ub_login_state() -> Dict[str, Any]:
    """The half-done login, or {} (and deleted) once it is older than 10 min."""
    try:
        v = json.loads(get_setting(UB_LOGIN_KEY) or "{}")
    except Exception:
        v = {}
    if not isinstance(v, dict):
        v = {}
    if v and time.time() - float(v.get("t") or 0) > UB_LOGIN_TTL:
        ub_del(UB_LOGIN_KEY)
        return {}
    return v


def ub_health() -> str:
    """not_logged_in | ready | paused | error"""
    if not get_setting(UB_SESS_KEY):
        return "not_logged_in"
    if ub_paused():
        return "paused"
    st = ub_state()
    if st.get("err") and float(st.get("err_t") or 0) >= float(st.get("ok_t") or 0):
        return "error"
    return "ready"


def ub_owner_chat(st: Optional[Dict[str, Any]] = None) -> Optional[int]:
    st = st if st is not None else ub_state()
    o = st.get("owner")
    if o and int(o) in ADMIN_USER_IDS:
        return int(o)
    return sorted(ADMIN_USER_IDS)[0] if ADMIN_USER_IDS else None


def ub_alert(st: Dict[str, Any], key: str, text: str) -> bool:
    """One short '❌ Userbot: …' to the owner, at most once per hour per same error. Caller saves st."""
    al = st.setdefault("al", {})
    now = time.time()
    if now - float(al.get(key) or 0) < UB_ALERT_EVERY:
        return False
    al[key] = int(now)
    for k in list(al):
        if now - float(al[k] or 0) > 86400:
            al.pop(k, None)
    chat = ub_owner_chat(st)
    if chat:
        try:
            send_text(chat, ub_scrub(text)[:600], {"inline_keyboard": [[{"text": "👤 Userbot", "callback_data": "ad:ub"}]]})
        except Exception:
            log_error("ub alert")
    return True


def ub_dispatch(why: str = "") -> str:
    """Start the userbot workflow now (GH_TOKEN). 'dispatched' | 'no_token' | 'error: …'. Never raises."""
    if not GH_DISPATCH_TOKEN:
        return "no_token"
    if _DIAG["dry"]:
        _DIAG["sent"].append({"ms": 0, "m": "ub_dispatch"})
        return "dispatched"
    url = f"https://api.github.com/repos/{GH_REPO}/actions/workflows/{UB_WORKFLOW}/dispatches"
    body = json.dumps({"ref": "main", "inputs": {"why": str(why or "button")[:40]}}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {GH_DISPATCH_TOKEN}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "filesbull-bot", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return "dispatched" if resp.status in (200, 204) else f"error: HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        try:
            why_ = json.loads(exc.read().decode("utf-8", "replace")).get("message", "")
        except Exception:
            why_ = ""
        return _scrub(f"error: HTTP {exc.code} {why_}")[:160]
    except Exception as exc:
        return _scrub(f"error: {type(exc).__name__}")[:160]


def ub_sources() -> List[Dict[str, Any]]:
    """Active source channels where the bot is NOT admin (those the bot can't read itself). Never the Backup/Log chat."""
    skip = {str(get_setting("backup_chat_id") or ""), str(get_setting("log_chat_id") or "")}
    rows = [r for r in source_channels() if str(r.get("chat_id")) not in skip]
    admin = par(*[(lambda cid=r["chat_id"]: bot_admin_in(cid)) for r in rows]) if rows else []
    return [r for r, a in zip(rows, admin) if a is not True]


# ---- login: one-time signed link from /admin → 👤 Userbot → 🔑 Login (owner only), then a small web page
# (phone → code → 2FA password). The code is typed on the page, never sent in a Telegram chat (Telegram
# cancels a login code that is sent inside a message).

def _ub_sig(uid: int, nonce: str, exp: int) -> str:
    return hmac.new(ub_master(), f"ublogin:{uid}:{nonce}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]


def ub_login_link(uid: int) -> str:
    nonce = hashlib.sha1(os.urandom(16)).hexdigest()[:16]
    exp = int(time.time()) + UB_LOGIN_TTL
    set_setting(UB_LOGIN_KEY, json.dumps({"n": nonce, "u": int(uid), "t": int(time.time()), "step": "phone", "pt": 0}))
    return f"{PUBLIC_HOME}ub/login?t={uid}.{nonce}.{exp}.{_ub_sig(int(uid), nonce, exp)}"


def ub_check_token(tok: str) -> Dict[str, Any]:
    """The login state for a valid, unexpired, current link; raises InputError otherwise."""
    try:
        uid_s, nonce, exp_s, sig = str(tok or "").split(".")
        uid, exp = int(uid_s), int(exp_s)
    except Exception:
        raise InputError("Link galat hai")
    if not hmac.compare_digest(sig, _ub_sig(uid, nonce, exp)) or not is_owner(uid):
        raise InputError("Link galat hai")
    if exp < time.time():
        raise InputError("Link purana ho gaya (10 min)")
    st = ub_login_state()
    if not st or st.get("n") != nonce or int(st.get("u") or 0) != uid:
        raise InputError("Link purana ho gaya ya istemal ho chuka")
    return st


def ub_norm_phone(v: str) -> str:
    raw = str(v or "").strip()
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10 and not raw.startswith("+"):
        digits = "91" + digits                       # Indian number typed without +91
    elif len(digits) == 11 and digits.startswith("0"):
        digits = "91" + digits[1:]
    if not 8 <= len(digits) <= 15:
        raise InputError("Number sahi daalo, jaise +91 98765 43210")
    return "+" + digits


def ub_norm_code(v: str) -> str:
    code = re.sub(r"\D", "", str(v or ""))
    if not 4 <= len(code) <= 8:
        raise InputError("Code me sirf 5 number hote hain (jaise 12345)")
    return code


def ub_client(session: str = "") -> Any:
    from telethon import TelegramClient                      # lazy: only login/logout/ping load Telethon
    from telethon.sessions import StringSession
    return TelegramClient(StringSession(session or ""), int(TG_API_ID), TG_API_HASH, connection_retries=1, retry_delay=1,
                          timeout=8, request_retries=2, device_model=UB_DEVICE, system_version="filesbull",
                          app_version="1.0", lang_code="en", system_lang_code="en")


def ub_run(coro_fn: Any, *args: Any) -> Any:
    import asyncio
    return asyncio.run(asyncio.wait_for(coro_fn(*args), timeout=25))


def _ub_err_text(exc: Exception) -> str:
    """Telegram's error → a short Hinglish line (never the number/code itself)."""
    name = type(exc).__name__
    sec = getattr(exc, "seconds", None)
    table = {
        "PhoneNumberInvalidError": "Ye number Telegram me sahi nahi hai. +91 ke saath dobara daalo.",
        "PhoneNumberBannedError": "Telegram ne is number ko band kiya hua hai.",
        "PhoneNumberUnoccupiedError": "Is number pe Telegram account nahi hai.",
        "PhoneCodeInvalidError": "Code galat hai. Telegram app me aaya 5 number wala code dobara dekh ke daalo.",
        "PhoneCodeExpiredError": "Code expire ho gaya (ya kisi chat me bhej diya gaya tha). Upar 'Naya code' dabao.",
        "PhoneCodeEmptyError": "Code khali hai.",
        "PasswordHashInvalidError": "2-step password galat hai. Dobara daalo.",
        "SessionPasswordNeededError": "2-step password chahiye.",
        "ApiIdInvalidError": "TG_API_ID / TG_API_HASH galat hain (Vercel env).",
        "AuthRestartError": "Telegram ne login dobara shuru karne bola. 'Naya code' dabao.",
    }
    if name in ("FloodWaitError", "FloodError", "PhonePasswordFloodError") or sec:
        mins = max(1, int(sec or 60) // 60)
        return f"Telegram ne bahut baar try karne pe roka hai. {mins} min baad dobara karo."
    return table.get(name) or f"Telegram error: {name}"


async def _ub_send_code(phone: str) -> Dict[str, Any]:
    client = ub_client()
    await client.connect()
    try:
        sent = await client.send_code_request(phone)
        via = type(getattr(sent, "type", None)).__name__.replace("SentCodeType", "").lower() or "app"
        return {"h": sent.phone_code_hash, "s": client.session.save(), "via": via}
    finally:
        await client.disconnect()


async def _ub_sign_in(session: str, phone: str, code: str, hsh: str, password: str) -> Dict[str, Any]:
    from telethon import errors
    client = ub_client(session)
    await client.connect()
    try:
        try:
            if password:
                me = await client.sign_in(password=password)
            else:
                me = await client.sign_in(phone=phone, code=code, phone_code_hash=hsh)
        except errors.SessionPasswordNeededError:
            return {"need_password": True, "s": client.session.save()}
        return {"ok": True, "s": client.session.save(), "me": {"id": int(me.id), "username": me.username or "",
                                                               "name": " ".join(x for x in (me.first_name, me.last_name) if x)}}
    finally:
        await client.disconnect()


async def _ub_logout(session: str) -> bool:
    client = ub_client(session)
    await client.connect()
    try:
        return bool(await client.log_out())
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def _ub_ping() -> Dict[str, Any]:
    from telethon import functions
    t0 = time.monotonic()
    client = ub_client()
    await client.connect()
    try:
        r = await client(functions.help.GetNearestDcRequest())
        return {"ok": True, "this_dc": r.this_dc, "nearest_dc": r.nearest_dc, "country": r.country,
                "ms": int((time.monotonic() - t0) * 1000)}
    finally:
        await client.disconnect()


UB_FAKE: Dict[str, Any] = {}       # harness: replaces the Telethon calls (send_code/sign_in/logout/ping)


def ub_call(name: str, *args: Any) -> Any:
    fn = UB_FAKE.get(name)
    if fn:
        return fn(*args)
    return ub_run({"send_code": _ub_send_code, "sign_in": _ub_sign_in, "logout": _ub_logout, "ping": _ub_ping}[name], *args)


def ub_login_step(st: Dict[str, Any], step: str, value: str) -> Dict[str, Any]:
    """Advance the login. Returns {"st": state, "msg": error/info text, "done": me}. Saves the state."""
    if not ub_ready():
        raise InputError("Vercel me TG_API_ID / TG_API_HASH nahi hain")
    out: Dict[str, Any] = {"msg": ""}
    try:
        if step == "restart":
            st.update(step="phone", h=None, s=None, ph=None)
        elif step == "phone":
            if int(st.get("pt") or 0) >= 3:
                raise InputError("Is link se 3 baar code maang liya. /admin → 👤 Userbot → 🔑 Login se naya link lo.")
            phone = ub_norm_phone(value)
            r = ub_call("send_code", phone)
            st.update(step="code", h=r["h"], s=ub_encrypt(r["s"]), ph=ub_encrypt(phone), via=r.get("via") or "app",
                      pt=int(st.get("pt") or 0) + 1, ct=0)
        elif step == "code":
            if st.get("step") != "code":
                raise InputError("Pehle number daalo")
            code = ub_norm_code(value)
            st["ct"] = int(st.get("ct") or 0) + 1
            if st["ct"] > 5:
                raise InputError("Bahut baar galat code. 'Naya code' dabao.")
            r = ub_call("sign_in", ub_decrypt(st.get("s") or "") or "", ub_decrypt(st.get("ph") or "") or "", code,
                        st.get("h") or "", "")
            if r.get("need_password"):
                st.update(step="password", s=ub_encrypt(r["s"]), wt=0)
            elif r.get("ok"):
                out["done"] = ub_finish_login(st, r)
                return out
        elif step == "password":
            if st.get("step") != "password":
                raise InputError("Pehle code daalo")
            if not str(value or ""):
                raise InputError("Password daalo")
            st["wt"] = int(st.get("wt") or 0) + 1
            if st["wt"] > 5:
                raise InputError("Bahut baar galat password. Thodi der baad naya link lo.")
            r = ub_call("sign_in", ub_decrypt(st.get("s") or "") or "", "", "", "", str(value))
            if r.get("ok"):
                out["done"] = ub_finish_login(st, r)
                return out
        else:
            raise InputError("Galat step")
    except InputError as exc:
        out["msg"] = str(exc)
        if "owner wale account" in out["msg"]:
            st.update(step="phone", h=None, s=None, ph=None)
    except Exception as exc:
        name = type(exc).__name__
        out["msg"] = _ub_err_text(exc)
        print(f"[filesbull] userbot login step {step}: {name}", flush=True)
        if name in ("PhoneCodeExpiredError", "AuthRestartError"):
            st.update(step="phone", h=None, s=None)
    set_setting(UB_LOGIN_KEY, json.dumps(st))
    out["st"] = st
    return out


def ub_finish_login(st: Dict[str, Any], r: Dict[str, Any]) -> Dict[str, Any]:
    me = r.get("me") or {}
    # The page itself is owner-only (signed one-time link from the owner's /admin, 10 min). Aarav (Oct 26, 9:24 PM)
    # wants to log in with a DIFFERENT (second) Telegram account as the userbot, so any account is allowed here.
    if not int(me.get("id") or 0):
        raise InputError("Login poora nahi hua, dobara try karo")
    set_setting(UB_SESS_KEY, ub_encrypt(r["s"]))
    ub_del(UB_LOGIN_KEY)
    ust = ub_state()
    ust.update(me={"username": me.get("username") or "", "name": str(me.get("name") or "")[:60]}, owner=int(st.get("u") or 0),
               login_t=int(time.time()), err="", err_t=0)
    ub_save_state(ust)
    ub_del(UB_PAUSE_KEY)
    who = ("@" + me["username"]) if me.get("username") else (me.get("name") or "aapka account")
    d = ub_dispatch("login")
    chat = int(st.get("u") or 0) or ub_owner_chat(ust)
    try:
        send_text(chat, f"✅ Userbot login ho gaya: {who}\n\nAb har 10 min un channels ki nayi files (jahan bot admin nahi) "
                        f"Backup channel me aayengi, bot unhe save + naam + category karega.\n"
                        + ("Pehla check abhi shuru ho gaya." if d == "dispatched" else "Pehla check 10 min me."),
                  {"inline_keyboard": [[{"text": "👤 Userbot", "callback_data": "ad:ub"}]]})
    except Exception:
        log_error("ub login notify")
    audit_log(f"👤 Userbot login · {ist_now()} · {who}")
    flush_request()
    return {"who": who}


UB_PAGE_CSS = ("body{font-family:system-ui,-apple-system,sans-serif;max-width:420px;margin:24px auto;padding:0 16px;"
               "background:#0f172a;color:#e2e8f0}h2{font-size:20px}.box{background:#1e293b;border-radius:14px;padding:18px}"
               "input{width:100%;box-sizing:border-box;font-size:22px;padding:12px;border-radius:10px;border:1px solid #475569;"
               "background:#0f172a;color:#fff;margin:10px 0;letter-spacing:2px}button{width:100%;font-size:18px;padding:12px;"
               "border:0;border-radius:10px;background:#22c55e;color:#04120a;font-weight:700}.err{background:#7f1d1d;"
               "padding:10px;border-radius:10px;margin:10px 0}.mut{color:#94a3b8;font-size:14px}.link{background:#334155;"
               "color:#e2e8f0;margin-top:10px}")


def ub_page(st: Optional[Dict[str, Any]], tok: str, msg: str = "", done: Optional[Dict[str, Any]] = None, fatal: str = "") -> Any:
    def form(step: str, label: str, inp: str, btn: str) -> str:
        return (f'<form method="post" autocomplete="off"><input type="hidden" name="t" value="{esc(tok)}">'
                f'<input type="hidden" name="step" value="{step}"><label>{label}</label>{inp}<button>{btn}</button></form>')
    head = ('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="robots" content="noindex"><title>Filesbull Userbot Login</title>'
            f'<style>{UB_PAGE_CSS}</style></head><body><h2>👤 Filesbull Userbot Login</h2><div class="box">')
    body = ""
    if fatal:
        body = f'<div class="err">❌ {esc(fatal)}</div><p class="mut">Bot me /admin → 👤 Userbot → 🔑 Login dabao, naya link milega.</p>'
    elif done:
        body = (f'<p>✅ Login ho gaya: <b>{esc(done.get("who"))}</b></p><p>Ab ye page band kar do. Bot me message aa gaya hoga.</p>'
                '<p class="mut">Har 10 min un channels ki nayi files Backup channel me jayengi.</p>')
    else:
        step = (st or {}).get("step") or "phone"
        if msg:
            body += f'<div class="err">{esc(msg)}</div>'
        if step == "phone":
            body += form("phone", "Apna Telegram number (wahi jis account se channels joined hain):",
                         '<input name="v" type="tel" inputmode="tel" placeholder="+91 98765 43210" required autofocus>',
                         "Code bhejo")
            body += '<p class="mut">Telegram aapke Telegram app me (ya SMS se) ek login code bhejega.</p>'
        elif step == "code":
            via = "SMS" if "sms" in str((st or {}).get("via") or "") else "Telegram app (\"Telegram\" naam ki chat)"
            body += form("code", f"Code daalo jo {via} me aaya:",
                         '<input name="v" inputmode="numeric" autocomplete="one-time-code" placeholder="12345" required autofocus>',
                         "Login karo")
            body += ('<p class="mut">⚠️ Ye code kisi ko mat bhejna, kisi chat me forward bhi mat karna (Telegram code cancel kar deta hai). '
                     'Sirf yahan daalo.</p>')
            body += (f'<form method="post"><input type="hidden" name="t" value="{esc(tok)}"><input type="hidden" name="step" '
                     'value="restart"><button class="link">↩️ Naya code / number badlo</button></form>')
        elif step == "password":
            body += form("password", "Aapke account pe 2-step verification hai. Apna Telegram cloud password daalo:",
                         '<input name="v" type="password" autocomplete="current-password" required autofocus>', "Login karo")
            body += '<p class="mut">Ye password kahin save nahi hota, sirf Telegram ko jata hai.</p>'
    body += '<p class="mut">Link 10 min chalega. Session encrypt karke save hota hai. Logout: bot me 👤 Userbot → 🚪 Logout.</p>'
    resp = app.response_class(head + body + "</div></body></html>", mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Frame-Options"] = "DENY"
    return resp


@app.route("/ub/login", methods=["GET", "POST"])
def ub_login_page() -> Any:
    _REQ.clear()
    _LOG_QUEUE.clear()
    tok = (request.form.get("t") if request.method == "POST" else request.args.get("t")) or ""
    try:
        st = ub_check_token(tok)
    except InputError as exc:
        return ub_page(None, "", fatal=str(exc))
    if request.method == "GET":
        return ub_page(st, tok)
    try:
        res = ub_login_step(st, str(request.form.get("step") or ""), str(request.form.get("v") or "")[:200])
    except InputError as exc:
        return ub_page(st, tok, fatal=str(exc))
    if res.get("done"):
        return ub_page(None, tok, done=res["done"])
    return ub_page(res.get("st") or st, tok, res.get("msg") or "")


# ---- the GitHub Action: ask for work (OIDC), check which files are new, report each forward, report the run

def ub_run_key(run: str, nonce: str) -> str:
    return hmac.new(ub_master(), f"ubrun:{run}:{nonce}".encode(), hashlib.sha256).hexdigest()


def ub_verify_report() -> tuple:
    """(body, state) of a report signed with the current run's key; raises InputError."""
    raw = request.get_data() or b""
    try:
        body = json.loads(raw.decode("utf-8") or "{}")
    except Exception:
        raise InputError("bad body")
    st = ub_state()
    cur = st.get("run") or {}
    sig = request.headers.get("X-Filesbull-Signature", "")
    if not cur.get("id") or str(body.get("run") or "") != str(cur.get("id")) or not sig or \
            time.time() - float(cur.get("t") or 0) > 3600 or \
            not hmac.compare_digest(sig, hmac.new(ub_run_key(str(cur["id"]), str(cur.get("n") or "")).encode(), raw,
                                                  hashlib.sha256).hexdigest()):
        raise InputError("unauthorized")
    return body, st


@app.post("/api/userbot-job")
def ub_job_api() -> Any:
    body = request.get_json(silent=True) or {}
    try:
        claims = verify_oidc(str(body.get("oidc") or ""), UB_WORKFLOW, UB_AUD)
    except InputError as exc:
        return jsonify({"ok": False, "error": "unauthorized", "why": str(exc)}), 401
    except Exception as exc:
        return jsonify({"ok": False, "error": "unauthorized", "why": type(exc).__name__}), 401
    _REQ.clear()
    _LOG_QUEUE.clear()
    prefetch_settings([UB_SESS_KEY, UB_STATE_KEY, UB_PAUSE_KEY, UB_LOGIN_KEY, "backup_chat_id", "log_chat_id"])
    ub_login_state()                       # sweeps a stale half-done login
    if not ub_ready():
        return jsonify({"ok": True, "state": "missing_secrets"})
    blob = get_setting(UB_SESS_KEY)
    if not blob:
        return jsonify({"ok": True, "state": "not_logged_in"})
    sess = ub_decrypt(blob)
    st = ub_state()
    if not sess:
        ub_del(UB_SESS_KEY)
        st.update(err="session padha nahi gaya (BOT_TOKEN badla?)", err_t=int(time.time()))
        ub_alert(st, "decrypt", "❌ Userbot: login dobara karna hoga (purana session khul nahi raha). /admin → 👤 Userbot → 🔑 Login")
        ub_save_state(st)
        flush_request()
        return jsonify({"ok": True, "state": "not_logged_in"})
    if ub_paused():
        return jsonify({"ok": True, "state": "paused"})
    backup = get_setting("backup_chat_id")
    if not backup:
        st.update(err="Backup channel set nahi hai", err_t=int(time.time()))
        ub_alert(st, "nobackup", "❌ Userbot: Backup channel set nahi hai. /admin → 💾 Backup Channel")
        ub_save_state(st)
        flush_request()
        return jsonify({"ok": True, "state": "no_backup"})
    srcs = ub_sources()
    ch = st.get("ch") or {}
    run = f"{claims.get('run_id') or ''}.{claims.get('run_attempt') or ''}"
    nonce = hashlib.sha1(os.urandom(16)).hexdigest()[:16]
    st["run"] = {"id": run, "n": nonce, "t": int(time.time()), "ev": str(claims.get("event_name") or "")}
    st["run_t"] = int(time.time())
    ub_save_state(st)
    out = {"ok": True, "state": "ready", "session": sess, "api_id": TG_API_ID, "api_hash": TG_API_HASH,
           "backup": int(backup), "run": run, "k": ub_run_key(run, nonce), "max": UB_MAX_PER_RUN,
           "first_max": UB_FIRST_RUN_MAX, "first_hours": 24, "gap": [2.0, 4.0], "device": UB_DEVICE,
           "sources": [{"id": int(r["chat_id"]), "title": str(r.get("title") or ""), "username": r.get("username") or "",
                        "last": int((ch.get(str(r["chat_id"])) or {}).get("last") or 0)} for r in srcs]}
    flush_request()
    return jsonify(out)


def ub_known(items: List[Dict[str, Any]]) -> List[bool]:
    """For each {u: file_unique_id, n: file name, s: size}: already in the catalog?"""
    found = [False] * len(items)
    uniq = [str(i.get("u") or "") for i in items if i.get("u")]
    have_u = set()
    if uniq:
        rows = supabase("books", "GET", query="?select=telegram_file_unique_id&telegram_file_unique_id=in.(" +
                        ",".join(urllib.parse.quote(f'"{u}"') for u in uniq[:100]) + ")")
        have_u = {str(r.get("telegram_file_unique_id")) for r in rows}
    sizes = sorted({int(i.get("s") or 0) for i in items if int(i.get("s") or 0) > 0})
    have_ns = set()
    if sizes:
        rows = supabase("books", "GET", query=f"?select=file_name,file_size&file_size=in.({','.join(str(s) for s in sizes[:100])})")
        have_ns = {(_norm_nm(str(r.get("file_name") or "")), int(r.get("file_size") or 0)) for r in rows}
    for n, i in enumerate(items):
        if str(i.get("u") or "") in have_u or (_norm_nm(str(i.get("n") or "")), int(i.get("s") or 0)) in have_ns:
            found[n] = True
    return found


@app.post("/api/userbot-seen")
def ub_seen_api() -> Any:
    try:
        body, _ = ub_verify_report()
    except InputError:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    items = [i for i in (body.get("items") or []) if isinstance(i, dict)][:200]
    try:
        known = ub_known(items)
    except Exception:
        log_error("ub seen")
        return jsonify({"ok": False, "error": "db"}), 500
    return jsonify({"ok": True, "known": known})


def ub_read_post(backup: Any, mid: Any) -> Dict[str, Any]:
    """The Bot API view of a Backup-channel post (file_id etc.): forward it inside the same channel, read, delete."""
    res = telegram("forwardMessage", {"chat_id": int(backup), "from_chat_id": int(backup), "message_id": int(mid),
                                      "disable_notification": True})
    msg = (res or {}).get("result") or {}
    try:
        if msg.get("message_id"):
            telegram("deleteMessage", {"chat_id": int(backup), "message_id": int(msg["message_id"])})
    except Exception:
        pass
    return msg


def ub_ingest(body: Dict[str, Any], st: Dict[str, Any]) -> str:
    """One forwarded/re-uploaded file → catalog, exactly like a post from an admin source channel, with the ORIGINAL
    channel as its source. 'saved' | 'dup' | 'nofile'."""
    backup = get_setting("backup_chat_id")
    src = body.get("src") or {}
    msg = ub_read_post(backup, body["backup_msg"])
    info = file_info(msg)
    if not info:
        return "nofile"
    rows = supabase("books", "GET", query="?select=id&limit=1&telegram_file_unique_id=eq." +
                    urllib.parse.quote(str(info.get("file_unique_id") or "")))
    if rows or ub_known([{"n": info.get("file_name"), "s": info.get("file_size")}])[0]:
        return "dup"
    channel = {"id": int(src.get("id") or 0), "title": str(src.get("title") or "channel"), "username": src.get("username") or None,
               "type": "channel"}
    fake = dict(msg, message_id=int(src.get("msg") or 0), chat=channel)
    fake.pop("forward_origin", None)
    record = build_metadata(fake, channel)
    record["backup_chat_id"] = int(backup)
    record["backup_message_id"] = int(body["backup_msg"])
    index_file(record, int(backup), int(body["backup_msg"]), str(channel["title"]))
    return "saved"


@app.post("/api/userbot-done")
def ub_done_api() -> Any:
    """kind=file: one post was forwarded → save it now and move that channel's last_id.
    kind=final: the run's summary (last ids, counts, errors, fatal) → Log Chat line, owner alert, health state."""
    try:
        body, st = ub_verify_report()
    except InputError:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    _REQ.clear()
    _LOG_QUEUE.clear()
    ch = st.setdefault("ch", {})
    now = int(time.time())
    today = ist_day()
    if (st.get("today") or {}).get("d") != today:
        st["today"] = {"d": today, "n": 0}
    out: Dict[str, Any] = {"ok": True}
    if body.get("kind") == "file":
        src = body.get("src") or {}
        c = ch.setdefault(str(src.get("id")), {})
        try:
            res = ub_ingest(body, st)
        except Exception as exc:
            log_error("ub ingest")
            res = "error"
            c["err"] = ub_scrub(str(exc))[:120]
        if res == "saved":
            st["today"]["n"] = int(st["today"].get("n") or 0) + 1
            st["run_saved"] = int(st.get("run_saved") or 0) + 1
            rs = st.setdefault("run_by", {})
            rs[str(src.get("title") or "?")[:40]] = int(rs.get(str(src.get("title") or "?")[:40]) or 0) + 1
        if res in ("saved", "dup"):
            c["last"] = max(int(c.get("last") or 0), int(src.get("msg") or 0))
            recent = st.setdefault("recent_backup", [])
            recent.append(int(body["backup_msg"]))
            st["recent_backup"] = recent[-100:]
        c["title"] = str(src.get("title") or c.get("title") or "")[:60]
        c["n"] = int(c.get("n") or 0) + (1 if res == "saved" else 0)
        out["result"] = res
    elif body.get("kind") == "final":
        fatal = str(body.get("fatal") or "")
        for cid, last in (body.get("last") or {}).items():
            c = ch.setdefault(str(cid), {})
            c["last"] = max(int(c.get("last") or 0), int(last or 0))
            c["t"] = now
        for cid in (body.get("ok_ids") or []):
            ch.setdefault(str(cid), {})["err"] = ""
        for cid, err in (body.get("errors") or {}).items():
            ch.setdefault(str(cid), {})["err"] = ub_scrub(err)[:120]
        for cid, title in (body.get("titles") or {}).items():
            ch.setdefault(str(cid), {})["title"] = str(title)[:60]
        if body.get("me"):
            st["me"] = {"username": str(body["me"].get("username") or "")[:40], "name": str(body["me"].get("name") or "")[:60]}
        saved = int(st.pop("run_saved", 0) or 0)
        by = st.pop("run_by", {}) or {}
        st["last_run"] = {"t": now, "saved": saved, "fwd": int(body.get("sent") or 0), "seen": int(body.get("checked") or 0),
                          "stop": str(body.get("stop") or "")[:80]}
        if saved:
            parts = sorted(by.items(), key=lambda x: -x[1])
            where = parts[0][0] + " se" if len(parts) == 1 else ", ".join(f"{t} ({n})" for t, n in parts) + " se"
            audit_log(f"👤 Userbot: {saved} nayi file{'' if saved == 1 else 's'}, {where}")
        if fatal in ("revoked", "unauthorized"):
            ub_del(UB_SESS_KEY)
            st.update(err="login khatam (Telegram ne session band kiya)", err_t=now, me={})
            ub_alert(st, "revoked", "❌ Userbot: login khatam ho gaya (Telegram ne session band kar diya). "
                                    "/admin → 👤 Userbot → 🔑 Login dobara karo.")
        elif fatal or body.get("error"):
            reason = ub_scrub(fatal or body.get("error"))[:160]
            st.update(err=reason, err_t=now)
            key = re.sub(r"\d+", "#", reason)[:60]
            ub_alert(st, key, f"❌ Userbot: {reason}")
        else:
            st.update(ok_t=now, err="")
            if body.get("errors"):
                st["warn"] = "; ".join(f"{(ch.get(str(k)) or {}).get('title') or k}: {ub_scrub(v)[:60]}"
                                       for k, v in body["errors"].items())[:200]
            else:
                st["warn"] = ""
        st.pop("run", None)
    ub_save_state(st)
    flush_request()
    return jsonify(out)


# ---- 👤 Userbot screen (owner only)

def ub_view(note: str = "") -> Any:
    prefetch_settings([UB_SESS_KEY, UB_STATE_KEY, UB_PAUSE_KEY, UB_LOGIN_KEY, "backup_chat_id", "log_chat_id"])
    st = ub_state()
    hs = ub_health()
    me = st.get("me") or {}
    who = ("@" + me["username"]) if me.get("username") else (me.get("name") or "")
    word = {"not_logged_in": "❌ Login nahi hai", "ready": "✅ Chal raha hai", "paused": "⏸️ Ruka hua (Pause)",
            "error": "⚠️ Dikkat"}[hs]
    lines = ["👤 <b>Userbot</b> — jin channels me bot admin nahi, unki nayi files aapke account se Backup channel me", ""]
    if note:
        lines += [note, ""]
    lines.append(f"Status: {word}" + (f" — {esc(who)}" if who and hs != "not_logged_in" else ""))
    if not ub_ready():
        lines.append("⚠️ Vercel me TG_API_ID / TG_API_HASH nahi hain")
    if hs == "error" and st.get("err"):
        lines.append(f"Wajah: {esc(st.get('err'))}")
    lr = st.get("last_run") or {}
    if lr.get("t"):
        lines.append(f"Last check: {ub_t(lr['t'])} — {int(lr.get('saved') or 0)} nayi file"
                     + (f" ({esc(lr['stop'])})" if lr.get("stop") else ""))
    td = st.get("today") or {}
    lines.append(f"Aaj forward: {int(td.get('n') or 0) if td.get('d') == ist_day() else 0} files")
    try:
        srcs = ub_sources()
    except Exception:
        log_error("ub sources")
        srcs = []
    ch = st.get("ch") or {}
    lines += ["", f"📡 Channels (bot admin nahi): {len(srcs)}"]
    for r in srcs[:15]:
        c = ch.get(str(r["chat_id"])) or {}
        tail = f"last check {ub_t(c['t'])}" if c.get("t") else "abhi check nahi hua"
        if c.get("n"):
            tail += f", {int(c['n'])} files"
        if c.get("err"):
            tail += f" ⚠️ {esc(str(c['err'])[:60])}"
        lines.append(f"• {esc(str(r.get('title') or r['chat_id'])[:40])} — {tail}")
    if not srcs:
        lines.append("• koi nahi (sab source channels me bot admin hai, ya source channel hi nahi)")
    lines += ["", "<i>Har 10 min check · ek baar me max 25 files · har file ke beech 2–4 sec · Telegram roke to agli baar.</i>"]
    kb: List[List[Dict[str, str]]] = []
    if hs == "not_logged_in":
        kb.append([{"text": "🔑 Login", "callback_data": "ad:ublogin"}])
    else:
        kb.append([{"text": "🔄 Abhi check karo", "callback_data": "ad:ubrun"}])
        kb.append([{"text": "▶️ Resume" if hs == "paused" else "⏸️ Pause", "callback_data": "ad:ubpause"},
                   {"text": "🚪 Logout", "callback_data": "ad:ublogout"}])
    kb.append(BACK_ROW)
    return "\n".join(lines), {"inline_keyboard": kb}


UB_LOGIN_STEPS = ("🔑 <b>Userbot login</b> (sirf ek baar)\n\n"
                  "1️⃣ Niche 🔐 Login page kholo\n"
                  "2️⃣ Apna Telegram number daalo (+91 ke saath) → Code bhejo\n"
                  "3️⃣ Telegram app me \"Telegram\" naam ki chat me 5 number ka code aayega → wahi page pe daalo\n"
                  "4️⃣ 2-step password laga hai to wo bhi page pe daalo\n\n"
                  "⚠️ Code kisi chat me mat bhejna/forward karna (bot ko bhi nahi) — Telegram code cancel kar deta hai. Sirf page pe.\n"
                  "Link 10 min chalega, ek hi baar.")


def handle_ub_callback(query: Dict[str, Any], cmd: str, chat_id: int, user_id: int) -> bool:
    if cmd == "ub":
        show(chat_id, *ub_view(), query=query, parse_mode="HTML")
    elif cmd == "ublogin":
        if not ub_ready():
            show(chat_id, "❌ Pehle Vercel me TG_API_ID aur TG_API_HASH daalo, phir Redeploy.", {"inline_keyboard": [BACK_ROW]}, query)
            return True
        url = ub_login_link(user_id)
        show(chat_id, UB_LOGIN_STEPS, {"inline_keyboard": [[{"text": "🔐 Login page kholo", "url": url}],
                                                           [{"text": "🔙 Userbot", "callback_data": "ad:ub"}]]}, query, "HTML")
    elif cmd == "ubrun":
        if not get_setting(UB_SESS_KEY):
            show(chat_id, *ub_view("❌ Pehle 🔑 Login karo."), query=query, parse_mode="HTML")
        elif ub_paused():
            show(chat_id, *ub_view("⏸️ Userbot ruka hua hai. Pehle ▶️ Resume dabao."), query=query, parse_mode="HTML")
        else:
            d = ub_dispatch("button")
            note = ("🔄 Check shuru ho gaya — 1–2 min me nayi files Backup channel me aayengi." if d == "dispatched" else
                    "⏳ Turant start nahi ho saka (GH_TOKEN nahi) — agle 10 min wale check me hoga." if d == "no_token" else
                    f"❌ Start nahi hua: {esc(d)[:100]}")
            show(chat_id, *ub_view(note), query=query, parse_mode="HTML")
    elif cmd == "ubpause":
        if ub_paused():
            ub_del(UB_PAUSE_KEY)
            audit_log(f"▶️ Userbot resume · {ist_now()}")
            note = "▶️ Userbot phir chalu."
        else:
            set_setting(UB_PAUSE_KEY, "1")
            audit_log(f"⏸️ Userbot pause · {ist_now()}")
            note = "⏸️ Userbot ruk gaya. Koi forward nahi hoga jab tak ▶️ Resume na dabao."
        show(chat_id, *ub_view(note), query=query, parse_mode="HTML")
    elif cmd == "ublogout":                 # only reached after ✅ Accept (is_destructive → one confirm)
        sess = ub_session()
        out = False
        if sess:
            try:
                out = bool(ub_call("logout", sess))
            except Exception as exc:
                print(f"[filesbull] userbot logout: {type(exc).__name__}", flush=True)
        ub_del(UB_SESS_KEY)
        ub_del(UB_LOGIN_KEY)
        st = ub_state()
        st.update(me={}, err="", err_t=0)
        st.pop("run", None)
        ub_save_state(st)
        audit_log(f"🚪 Userbot logout · {ist_now()}")
        show(chat_id, *ub_view("🚪 Logout ho gaya. Session delete" + (" + Telegram se bhi hata diya." if out else
                                                                       ". (Telegram → Settings → Devices me 'Filesbull Userbot' dikhe to wahan se bhi hata sakte ho.)")),
             query=query, parse_mode="HTML")
    else:
        return False
    return True



def ub_health_info() -> Dict[str, Any]:
    """Health page: userbot state word + last run (never the session, number or account id)."""
    ub_login_state()                    # a half-done login older than 10 min is deleted here (keepalive every 5 min)
    st = ub_state()
    lr = st.get("last_run") or {}
    td = st.get("today") or {}
    return {"state": ub_health(), "secrets": "ok" if ub_ready() else "missing TG_API_ID/TG_API_HASH",
            "trigger": "instant" if GH_DISPATCH_TOKEN else "schedule_10min",
            "last_run": ub_t(lr["t"]) if lr.get("t") else None, "last_saved": lr.get("saved"),
            "today": int(td.get("n") or 0) if td.get("d") == ist_day() else 0,
            "error": (st.get("err") or None) if ub_health() == "error" else None}


@app.get("/api/ubdiag")
def ub_diag() -> Any:
    """Diag token only: &ping=1 = MTProto connect from Vercel (no login); state + sources (titles only)."""
    tok = request.args.get("t", "")
    if not tok or not hmac.compare_digest(hashlib.sha256(tok.encode()).hexdigest(), DIAG_TOKEN_SHA256):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    _REQ.clear()
    out: Dict[str, Any] = {"ok": True, "build": os.environ.get("VERCEL_GIT_COMMIT_SHA", "")[:7], "health": ub_health_info()}
    st = ub_state()
    out["channels"] = {(v.get("title") or k): {x: v.get(x) for x in ("last", "n", "err") if x in v} | ({"t": ub_t(v["t"])} if v.get("t") else {})
                       for k, v in (st.get("ch") or {}).items()}
    out["last_run"] = st.get("last_run")
    try:
        out["sources_not_admin"] = [str(r.get("title")) for r in ub_sources()]
    except Exception as exc:
        out["sources_not_admin"] = "error: " + type(exc).__name__
    if request.args.get("ping") == "1":
        try:
            out["mtproto"] = ub_call("ping")
        except Exception as exc:
            out["mtproto"] = {"ok": False, "error": type(exc).__name__}
    if request.args.get("view") == "1":
        out["view"] = ub_view()[0]
    return jsonify(out)


_SEEN_UPDATES: List[Any] = []


@app.post("/api/webhook")
def webhook() -> Any:
    received = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not WEBHOOK_SECRET or not hmac.compare_digest(received, WEBHOOK_SECRET):
        # Not from Telegram, or the webhook was set without the secret:
        # open the site's home URL once and it re-registers itself.
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    update = request.get_json(silent=True) or {}
    # Telegram re-sends an update it thinks timed out; never run the same update twice on a warm instance
    # (a slow old button press must not pop up again after a later /start).
    uid_ = update.get("update_id")
    if uid_ is not None:
        if uid_ in _SEEN_UPDATES:
            return jsonify({"ok": True, "dup": True})
        _SEEN_UPDATES.append(uid_)
        if len(_SEEN_UPDATES) > 500:
            del _SEEN_UPDATES[:250]
    _REQ.clear()
    _LOG_QUEUE.clear()
    _REQ_T0[0] = time.monotonic()
    reply_chat = ((update.get("message") or {}).get("chat") or {}).get("id") or \
        (((update.get("callback_query") or {}).get("message") or {}).get("chat") or {}).get("id")
    try:
        if update.get("callback_query"):
            handle_callback(update["callback_query"])
        elif update.get("channel_post"):
            handle_channel_post(update["channel_post"])
        elif update.get("message"):
            message = update["message"]
            text = (message.get("text") or "").strip()
            handle_group_file(message)
            if file_info(message) and message.get("chat", {}).get("type") in {"group", "supergroup"}:
                flush_request()
                return jsonify({"ok": True})
            command = command_name(text)
            is_private = message.get("chat", {}).get("type") == "private"
            if command and command not in USER_COMMANDS and is_subadmin(message.get("from", {}).get("id")):
                handle_admin_command(message)
            elif is_private or command in USER_COMMANDS:
                # In groups only answer explicit commands, not every chat message.
                handle_private_message(message)
    except Exception:
        # Always return 200 so Telegram does not retry a malformed/duplicate update forever,
        # but log the traceback so the error is visible in Vercel logs.
        log_error("webhook")
        if reply_chat and not update.get("channel_post"):
            try:
                hint = "" if (SUPABASE_URL and SUPABASE_KEY) else " (database setup missing)"
                send_text(reply_chat, "Abhi kuch gadbad hui" + hint + ", thodi der baad dobara try karein.")
            except Exception:
                pass
    flush_request()
    return jsonify({"ok": True})


_BOOT_MS = [int((time.time() - _BOOT_T0) * 1000)]

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
