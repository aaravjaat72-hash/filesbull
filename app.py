import hashlib
import hmac
import html
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
AI_RANK = os.environ.get("AI_RANK", "").strip().lower() in {"1", "true", "on", "yes"}
# Owner IDs from Vercel env. Accept any separator/format people paste:
# "123,456", "123 456", '"123"', "[123]", "@123", "-123", newlines...
ADMIN_USER_IDS = {
    int(x) for x in re.findall(r"\d+", os.environ.get("ADMIN_USER_IDS", ""))
}

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
# Telegram sends this value in the X-Telegram-Bot-Api-Secret-Token header on every
# webhook call, so forged updates (fake "admin" messages) can be rejected.
# Set WEBHOOK_SECRET in Vercel to choose your own; otherwise it is derived from BOT_TOKEN.
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip() or (
    hashlib.sha256(f"filesbull-webhook:{BOT_TOKEN}".encode("utf-8")).hexdigest() if BOT_TOKEN else ""
)


def log_error(where: str) -> None:
    # Printed tracebacks show up in Vercel -> Project -> Logs. Never print the token.
    text = traceback.format_exc()
    for secret in (BOT_TOKEN, OPENAI_API_KEY, SUPABASE_KEY, *GROQ_KEYS):
        if secret:
            text = text.replace(secret, "***")
    print(f"[filesbull] error in {where}:\n{text}", flush=True)


def http_json(url: str, method: str = "GET", payload: Optional[Dict[str, Any]] = None,
              headers: Optional[Dict[str, str]] = None, timeout: float = 20) -> Dict[str, Any]:
    body = None
    final_headers = {"Content-Type": "application/json"}
    if headers:
        final_headers.update(headers)
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=final_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Surface the API's own reason (e.g. Telegram "Bad Request: chat not found",
        # Supabase "column ... does not exist") instead of a bare "HTTP Error 400".
        try:
            detail = json.loads(exc.read().decode("utf-8", "replace"))
            reason = detail.get("description") or detail.get("message") or detail.get("error") or str(detail)
        except Exception:
            reason = exc.reason
        raise RuntimeError(f"HTTP {exc.code}: {str(reason)[:300]}") from None


def telegram(method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not configured")
    return http_json(f"{TG_API}/{method}", "POST", payload)


def supabase(path: str, method: str = "GET", payload: Any = None,
             query: str = "") -> Any:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("Supabase environment variables are not configured")
    url = f"{SUPABASE_URL}/rest/v1/{path}{query}"
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


def get_setting(key: str) -> Optional[str]:
    try:
        rows = supabase("bot_settings", "GET", query=f"?key=eq.{urllib.parse.quote(key)}&select=value&limit=1")
        return rows[0].get("value") if rows else None
    except Exception:
        return None


def set_setting(key: str, value: str) -> None:
    supabase("bot_settings", "POST", {"key": key, "value": value}, "?on_conflict=key")


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
    lines = [x for x in _LOG_QUEUE if x]
    _LOG_QUEUE.clear()
    if not lines:
        return
    target = get_setting("log_chat_id")
    if not target:
        return
    chunk = ""
    chunks = []
    for line in lines:
        if len(chunk) + len(line) > 3500:
            chunks.append(chunk)
            chunk = ""
        chunk = (chunk + "\n\n" + line) if chunk else line
    if chunk:
        chunks.append(chunk)
    for part in chunks[:3]:
        try:
            telegram("sendMessage", {"chat_id": int(target), "text": part, "disable_web_page_preview": True,
                                     "disable_notification": True})
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


def admin_menu() -> Dict[str, Any]:
    # One clear button per row (labels <= 20 chars so they never cut off on a phone).
    return {"inline_keyboard": [
        [{"text": "➕ Join Channel Jodo", "callback_data": "ad:addreq"}],
        [{"text": "📋 Join Channels", "callback_data": "ad:reqlist"}],
        [{"text": "➕ Source Channel Jodo", "callback_data": "ad:addsrc"}],
        [{"text": "📋 Source Channels", "callback_data": "ad:srclist"}],
        [{"text": "💾 Backup Channel", "callback_data": "ad:backup"}, {"text": "📝 Log Chat", "callback_data": "ad:log"}],
        [{"text": "💾 Backup: purani files bhi", "callback_data": "ad:bkold"}],
        [{"text": "👥 Users", "callback_data": "ad:users"}, {"text": "👮 Admins", "callback_data": "ad:admins"}],
        [{"text": "✏️ File Rename", "callback_data": "ad:rename"}, {"text": "📊 Report", "callback_data": "ad:report"}],
        [{"text": "🩺 Status", "callback_data": "ad:status"}, {"text": "❓ Admin Help", "callback_data": "ad:help"}],
        [{"text": "🔍 Search Menu", "callback_data": "ad:usermenu"}],
    ]}


ADMIN_PANEL_TEXT = ("⚙️ <b>Admin Panel</b>\n\n"
                    "Niche se kaam chuno 👇\n"
                    "<i>Search ke liye seedha book ka naam bhej sakte ho.</i>")

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
               "(/cancel = band)", "Post forward karo ya me"),
    "addadmin": ("👮 Naye admin ka Telegram numeric ID bhejo (wo bot me /myid bhejkar dekh sakta hai), ya uska koi message forward karo.\n"
                 "(/cancel = band)", "123456789"),
    "rename": ("✏️ Kis file ka naam badalna hai? File ka naam/keyword bhejo (ya ID aur naya naam: 12 Lucent GK Hindi).\n"
               "(/cancel = band)", "lucent"),
    "rename_to": ("✏️ Is file ka naya naam bhejo. (/cancel = band)", "Naya naam"),
}


def show(chat_id: int, text: str, markup: Optional[Dict[str, Any]] = None, query: Optional[Dict[str, Any]] = None,
         parse_mode: Optional[str] = None) -> None:
    """Edit the button's own message (clean navigation); fall back to a new message."""
    message_id = ((query or {}).get("message") or {}).get("message_id")
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
    req = urllib.request.Request(f"{SUPABASE_URL}/rest/v1/{table}?select={col}{query}&limit=1", method="GET", headers={
        "apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Prefer": "count=exact"})
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            total = (response.headers.get("Content-Range") or "").split("/")[-1]
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} counting {table}") from None
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


def chat_title(chat_id: Any) -> str:
    try:
        chat = telegram("getChat", {"chat_id": int(chat_id)}).get("result", {}) or {}
        return chat.get("title") or chat.get("first_name") or str(chat_id)
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
        files = count_rows("books")
        logs = count_rows("delivery_logs")
        today = time.strftime("%Y-%m-%d", time.gmtime())
        today_logs = count_rows("delivery_logs", f"&created_at=gte.{today}")
        req = len(required_channels())
        src = len(source_channels())
        return (f"📊 Report\nIndexed files: {files}\nFiles bheji gayi (total): {logs}\nAaj bheji gayi: {today_logs}\n"
                f"Join channels: {req}\nSource channels: {src}\nSmart AI: {'ON (' + ai_provider() + ')' if ai_provider() else 'OFF (GROQ_API_KEY nahi)'}\n"
                f"Backup: {'ON' if get_setting('backup_chat_id') else 'OFF'}\nLog chat: {'ON' if get_setting('log_chat_id') else 'OFF'}")
    except Exception as exc:
        return f"Report nahi ban payi: {str(exc)[:200]}"


def status_text(user_id: Optional[int]) -> str:
    role = role_of(user_id)
    role_word = {"owner": "✅ Owner (ADMIN_USER_IDS me hai)", "admin": "✅ Sub-admin (admins table)", "user": "❌ Normal user (admin nahi)"}[role]
    lines = [f"🩺 Status", f"Aapka Telegram ID: {user_id}", f"Role: {role_word}", f"Owner IDs set: {len(ADMIN_USER_IDS)}"]
    try:
        lines.append(f"Bot: {bot_mention()}")
    except Exception:
        pass
    reqs = required_channels()
    lines.append(f"\nJoin gate: {'ON' if reqs else 'OFF (koi join channel nahi)'}")
    for r in reqs:
        lines.append(f"• {r.get('title')}: {chat_check(r['chat_id'])}, link {'✅' if (r.get('invite_link') or r.get('username')) else '❌'}")
    try:
        srcs = source_channels()
        lines.append(f"\nSource channels: {len(srcs)}")
        checks = [source_check(r["chat_id"]) for r in srcs]
        for r, ch in zip(srcs, checks):
            lines.append(f"• {r.get('title')}: {ch}")
        if any(ch == AUTO_SAVE_OFF for ch in checks):
            lines.append("ℹ️ Auto-save off wale channels ki files forward karke save karo, ya bot ko admin banao. Purani files chalti rahengi.")
    except Exception as exc:
        lines.append(f"\nSource channels: error {str(exc)[:80]}")
    lines.append(f"Backup: {'ON' if get_setting('backup_chat_id') else 'OFF'} | Log chat: {'ON' if get_setting('log_chat_id') else 'OFF'}")
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
    "📝 Log Chat — naya user, kisne kaunsi file li, nayi file (user track)\n"
    "👥 Users — kitne users, aaj active, top 10; /user ID se details\n"
    "👮 Admins — sub-admin jodo/hatao (sirf owner)\n"
    "✏️ File Rename — file ka search naam badlo\n"
    "📊 Report — files aur delivery count\n"
    "🩺 Status — aapka ID, role, channels me bot admin hai ya nahi\n\n"
    "Commands bhi chalti hain: /admin, /requirechannel @ch, /removerequired @ch, /addchannel @ch, /removechannel @ch, "
    "/channels, /requiredchannels, /setbackupchannel, /setlogchat, /addadmin ID, /removeadmin ID, /rename ID naam, "
    "/report, /stats, /status, /user ID, /backupold, /myid, /cancel\n"
    "Kisi channel ki post bot ko forward karoge to wo source channel ban jayega."
)


def do_rename(chat_id: int, book_id: Any, new_title: str) -> None:
    new_title = new_title.strip()[:500]
    if not new_title:
        raise InputError("Naya naam khali hai.")
    rows = supabase("books", "GET", query=f"?id=eq.{int(book_id)}&select=id,title&limit=1")
    if not rows:
        raise InputError(f"File ID {book_id} nahi mili.")
    supabase("books", "PATCH", {"title": new_title, "keywords": new_title[:4000]}, f"?id=eq.{int(book_id)}")
    send_text(chat_id, f"✅ Naam badal diya:\n{rows[0].get('title')}\n→ {new_title}", {"inline_keyboard": [BACK_ROW]})


def handle_admin_input(message: Dict[str, Any], action: str, extra: Any) -> None:
    """The admin answered a prompt (ForceReply / pending)."""
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]
    text = (message.get("text") or message.get("caption") or "").strip()
    retry = {"inline_keyboard": [[{"text": "🔁 Dobara try", "callback_data": f"ad:{action if action != 'rename_to' else 'rename'}"}], BACK_ROW]}
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
            send_text(chat_id, f"✅ Log chat set: {chat.get('title')}", {"inline_keyboard": [BACK_ROW]})
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
                do_rename(chat_id, m.group(1), m.group(2))
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
            do_rename(chat_id, extra, text)
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
    if cmd == "panel":
        clear_pending(user_id)
        show(chat_id, ADMIN_PANEL_TEXT, admin_menu(), query, "HTML")
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
        try:
            telegram("answerCallbackQuery", {"callback_query_id": query.get("id"), "text": "💾 Backup chal raha hai..."})
        except Exception:
            pass
        show(chat_id, *backup_old_view(), query=query)
    elif cmd == "users":
        show(chat_id, users_view(), {"inline_keyboard": [[{"text": "🔄 Refresh", "callback_data": "ad:users"}], BACK_ROW]}, query)
    elif cmd == "log":
        show(chat_id, *setting_view("log_chat_id", "📝 Log Chat (User Track)", "ad:setlog", "ad:logoff",
                                    "Naya user aaya, kisne kaunsi file li, nayi file save hui — sab yahan aayega."), query=query)
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
        show(chat_id, status_text(user_id), {"inline_keyboard": [BACK_ROW]}, query)
    elif cmd == "help":
        show(chat_id, ADMIN_HELP, {"inline_keyboard": [BACK_ROW]}, query)
    else:
        show(chat_id, ADMIN_PANEL_TEXT, admin_menu(), query, "HTML")


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
    {"gk", "gs", "सामान्य ज्ञान", "samanya gyan"},
    {"computer", "कंप्यूटर", "कम्प्यूटर"},
    {"railway", "rrb", "रेलवे"},
    {"police", "पुलिस", "constable"},
    {"army", "agniveer", "अग्निवीर", "सेना"},
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
    if not vocab_hit and (any(p in low for p in OFF_PHRASES) or any(t in OFF_WORDS for t in tokens)):
        result["intent"] = "off_topic"
        return result
    if all(t in GREET_WORDS or t in FILLER for t in tokens) and any(t in GREET_WORDS for t in tokens):
        result["intent"] = "greeting"
        return result
    if len(tokens) <= 2 and not vocab_hit and any(_close_greet(t) for t in tokens) and \
            all(_close_greet(t) or t in GREET_WORDS or t in FILLER for t in tokens):
        result["intent"] = "greeting"     # "Jello", "helo bhai", "hlo"
        return result
    if all(t in THANK_WORDS or t in FILLER for t in tokens) and any(t in THANK_WORDS for t in tokens):
        result["intent"] = "thanks"
        return result
    more = any(t in MORE_WORDS for t in tokens)
    keywords = []
    for t in tokens:
        if t in FILLER or t in MORE_WORDS or t in REF_WORDS or t in GREET_WORDS and t not in EXAM_WORDS:
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

def ai_provider() -> str:
    return "groq" if GROQ_KEYS else ("openai" if OPENAI_API_KEY else "")


_KEY_COOL: Dict[int, float] = {}
KEY_COOLDOWN = 300   # seconds a rate-limited / failing key is skipped


def key_cooling(index: int) -> bool:
    until = _KEY_COOL.get(index)
    if until is None:
        raw = get_setting(f"aikey_cool:{index}")
        until = float(raw) if raw and raw.replace(".", "", 1).isdigit() else 0.0
        _KEY_COOL[index] = until
    return until > time.time()


def cool_key(index: int, seconds: int = KEY_COOLDOWN) -> None:
    until = time.time() + seconds
    _KEY_COOL[index] = until
    try:
        set_setting(f"aikey_cool:{index}", str(int(until)))
    except Exception:
        log_error("cool_key")


def ai_complete(system: str, user_text: str, max_tokens: int = 150) -> Optional[str]:
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user_text}]
    models = [m for m in dict.fromkeys([GROQ_MODEL, "llama-3.1-8b-instant", "llama-3.3-70b-versatile"]) if m]
    usable = [i for i in range(len(GROQ_KEYS)) if not key_cooling(i)] or list(range(len(GROQ_KEYS)))
    for index in usable:
        key = GROQ_KEYS[index]
        for model in models:
            try:
                res = http_json("https://api.groq.com/openai/v1/chat/completions", "POST", {
                    "model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.1,
                    "response_format": {"type": "json_object"}}, {"Authorization": f"Bearer {key}"}, timeout=6)
                return res["choices"][0]["message"]["content"]
            except Exception as exc:
                low = str(exc).lower()
                if "model" in low and any(x in low for x in ("not found", "decommission", "does not exist", "not supported")):
                    continue   # same key, next model
                print(f"[filesbull] groq key {index + 1} error: {str(exc)[:160].replace(key, '***')}", flush=True)
                # 429 limit / 401-403 bad key / 5xx / timeout: skip this key for a while, try the next one now.
                m = re.search(r"try again in (\d+)m", low)
                cool_key(index, max(KEY_COOLDOWN, int(m.group(1)) * 60) if m else KEY_COOLDOWN)
                break
    if OPENAI_API_KEY:
        body: Dict[str, Any] = {"model": OPENAI_MODEL, "messages": messages, "response_format": {"type": "json_object"}}
        if OPENAI_MODEL.startswith(("gpt-5", "o1", "o3", "o4")):
            body["reasoning_effort"] = "minimal" if OPENAI_MODEL.startswith("gpt-5") else "low"
            body["max_completion_tokens"] = 800
        else:
            body["max_completion_tokens"] = max_tokens
            body["temperature"] = 0.1
        try:
            res = http_json("https://api.openai.com/v1/chat/completions", "POST", body,
                            {"Authorization": f"Bearer {OPENAI_API_KEY}"}, timeout=8)
            return res["choices"][0]["message"]["content"]
        except Exception as exc:
            print(f"[filesbull] openai error: {str(exc)[:200]}", flush=True)
    return None


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

def search_rows(keywords: List[str], lang: Optional[str] = None, types: Optional[List[str]] = None,
                strict: bool = True) -> List[Dict[str, Any]]:
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
                       for col in ("title", "caption", "keywords", "content_text", "source_chat_title"))
    rows = supabase("books", "GET", query=(
        "?select=id,title,caption,keywords,content_text,source_chat_title,language"
        "&status=eq.indexed&limit=300&or=" + urllib.parse.quote(f"({filters})", safe="(),.*")
    ))
    scored = []
    for row in rows:
        head = normalize(" ".join(str(row.get(k) or "") for k in ("title", "caption", "keywords", "source_chat_title")))
        body = normalize(str(row.get("content_text") or ""))
        score = 0.0
        hits = 0
        for g in groups:
            if any(term in head for term in g):
                score += 3
                hits += 1
            elif any(term in body for term in g):
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
                 lang: Optional[str], types: Optional[List[str]], raw: str, note: str = "") -> bool:
    """Search + show page 1. Returns False when nothing was found (nothing sent)."""
    rows = search_rows(keywords, lang, types)
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


def smart_reply(chat_id: int, user: Dict[str, Any], text: str) -> None:
    user_id = user.get("id")
    u = understand(text)
    intent = u["intent"]
    if intent == "greeting":
        send_text(chat_id, greeting_msg(user), None, "HTML")
        return
    if intent == "thanks":
        send_text(chat_id, THANKS_MSG)
        return
    if intent in {"off_topic", "empty"}:
        send_text(chat_id, OFFTOPIC_MSG, None, "HTML")
        return
    ctx = load_ctx(user_id)
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
        ask_question(chat_id, user_id, ctx, "subject", [], None)
        return
    if send_results(chat_id, user_id, ctx, keywords, lang, types, text):
        return
    # Nothing found locally: ask the AI once (typos, long sentences), within the limits.
    if ai_budget_ok(user_id, ctx):
        ans = ai_interpret(text, ctx)
        ai_spend(user_id, ctx)
        save_ctx(user_id, ctx)
        if ans:
            if ans["intent"] in {"off_topic", "greeting"}:
                send_text(chat_id, OFFTOPIC_MSG, None, "HTML")
                return
            if ans["intent"] == "more" and ctx.get("q"):
                show_more(chat_id, user_id, ctx)
                return
            if ans["keywords"] and ans["keywords"] != keywords and \
                    send_results(chat_id, user_id, ctx, ans["keywords"], ans["lang"] or lang, types, text):
                return
            if ans["ask"]:
                ask_question(chat_id, user_id, ctx, "exam" if keywords else "subject", keywords, lang,
                             "🤔 " + esc(ans["ask"]))
                return
    send_no_results(chat_id, " ".join(keywords) or text)


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
            send_text(chat_id, OFFTOPIC_MSG, None, "HTML")
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


def flush_user() -> None:
    user = _REQ.get("user")
    if not user:
        return
    uid = int(user["id"])
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


def flush_request() -> None:
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
    files_today = count_rows("delivery_logs", f"&created_at=gte.{today_iso}")
    if users_table_ok():
        total = count_rows("bot_users", col="user_id")
        active = count_rows("bot_users", f"&last_seen=gte.{today_iso}", col="user_id")
        new = count_rows("bot_users", f"&first_seen=gte.{today_iso}", col="user_id")
        recent = supabase("bot_users", "GET", query="?select=*&order=last_seen.desc&limit=10")
        note = ""
    else:
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
            res = telegram("copyMessage", {"chat_id": target, "from_chat_id": from_chat, "message_id": message_id,
                                           "disable_notification": True}).get("result") or {}
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
                                "caption": (row.get("title") or "")[:1000], "disable_notification": True}).get("result") or {}
        if res.get("message_id"):
            return int(res["message_id"])
    return None


def ensure_backup(record: Dict[str, Any], from_chat: Any, message_id: Any) -> str:
    """Back up a just-indexed file once. 'saved' | 'dup' | 'off' | 'failed'."""
    target = get_setting("backup_chat_id")
    uniq = record.get("telegram_file_unique_id")
    if not target or not uniq:
        return "off"
    rows = supabase("books", "GET", query=f"?telegram_file_unique_id=eq.{urllib.parse.quote(str(uniq))}"
                                          "&select=id,backup_chat_id,backup_message_id&limit=1")
    if rows and rows[0].get("backup_message_id") and str(rows[0].get("backup_chat_id")) == str(target):
        return "dup"
    try:
        mid = _send_file_copy(int(target), from_chat, message_id, record)
    except Exception:
        log_error("backup copy")
        mid = None
    if not mid:
        return "failed"
    supabase("books", "PATCH", {"backup_chat_id": int(target), "backup_message_id": mid},
             f"?telegram_file_unique_id=eq.{urllib.parse.quote(str(uniq))}")
    return "saved"


def index_file(record: Dict[str, Any], from_chat: Any, message_id: Any, where: str) -> str:
    """Save a file to the catalog, back it up once, and log it."""
    upsert_item(record)
    try:
        backup = ensure_backup(record, from_chat, message_id)
    except Exception:
        log_error("ensure_backup")
        backup = "failed"
    mark = {"saved": " · 💾 backup ✅", "dup": " · 💾 pehle se backup", "failed": " · 💾 backup ❌"}.get(backup, "")
    audit_log(f"📥 Nayi file save: {str(record.get('title') or '')[:80]} — {where}{mark}")
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
            mid = _send_file_copy(int(target), row.get("source_chat_id"), row.get("source_message_id"), row)
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
    content_text = extract_pdf_text(info.get("file_id"), raw_name, info.get("file_size"))
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


def upsert_item(record: Dict[str, Any]) -> None:
    # The unique constraint on telegram_file_unique_id prevents duplicate indexing.
    supabase("books", "POST", record, "?on_conflict=telegram_file_unique_id")


def send_text(chat_id: int, text: str, reply_markup: Optional[Dict[str, Any]] = None,
              parse_mode: Optional[str] = None) -> None:
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
    {"command": "requirechannel", "description": "Admin: join channel jodo"},
    {"command": "requiredchannels", "description": "Admin: join channels list"},
    {"command": "removerequired", "description": "Admin: join channel hatao"},
    {"command": "addchannel", "description": "Admin: source channel jodo"},
    {"command": "channels", "description": "Admin: source channels list"},
    {"command": "removechannel", "description": "Admin: source channel hatao"},
    {"command": "setbackupchannel", "description": "Admin: backup channel set karein"},
    {"command": "setlogchat", "description": "Admin: log chat set karein"},
    {"command": "report", "description": "Admin: report"},
    {"command": "rename", "description": "Admin: file ka naam badlo"},
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
    audit_log(f"📄 {label} ({uid}) ne li: {str(row.get('title') or '')[:80]}" + (f" — query: {q[:60]}" if q else ""))


def send_by_file_id(chat_id: int, row: Dict[str, Any]) -> None:
    # Works even if the source post was deleted or the bot lost access to the channel.
    mime = (row.get("mime_type") or "").lower()
    method, field = "sendDocument", "document"
    if mime.startswith("video/"):
        method, field = "sendVideo", "video"
    elif mime.startswith("audio/"):
        method, field = "sendAudio", "audio"
    telegram(method, {"chat_id": chat_id, field: row["telegram_file_id"], "caption": (row.get("title") or "")[:1000]})


def deliver_result(chat_id: int, row: Dict[str, Any], user: Optional[Dict[str, Any]] = None) -> None:
    try:
        # copyMessage works for both public and private source channels when permitted.
        telegram("copyMessage", {
            "chat_id": chat_id,
            "from_chat_id": row["source_chat_id"],
            "message_id": row["source_message_id"],
        })
        log_delivery(chat_id, row, user)
        return
    except Exception:
        log_error("copyMessage")
    try:
        # Public source channel the bot is not a member of: a plain forward can still work.
        if row.get("source_chat_id") and row.get("source_message_id"):
            telegram("forwardMessage", {"chat_id": chat_id, "from_chat_id": row["source_chat_id"],
                                        "message_id": row["source_message_id"]})
            log_delivery(chat_id, row, user)
            return
    except Exception:
        log_error("forwardMessage")
    try:
        if row.get("backup_chat_id") and row.get("backup_message_id"):
            telegram("copyMessage", {"chat_id": chat_id, "from_chat_id": row["backup_chat_id"], "message_id": row["backup_message_id"]})
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
        send_text(chat_id, ADMIN_PANEL_TEXT, admin_menu(), "HTML")
    elif command == "/cancel":
        send_text(chat_id, "✅ Cancel ho gaya.", admin_menu())
    elif command in {"/status", "/debug"}:
        send_text(chat_id, status_text(user_id), {"inline_keyboard": [BACK_ROW]})
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
            supabase("admins", "DELETE", query=f"?user_id=eq.{arg}")
            _SUBADMIN_CACHE.pop(int(arg), None)
            try:
                telegram("deleteMyCommands", {"scope": {"type": "chat", "chat_id": int(arg)}})
            except Exception:
                log_error("remove admin menu")
            send_text(chat_id, f"✅ Sub-admin {arg} hata diya.")
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
                supabase("required_channels", "DELETE", query=f"?chat_id=eq.{cid}")
                send_text(chat_id, "✅ Join channel hata diya.")
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
                supabase("channels", "PATCH", {"active": False}, f"?chat_id=eq.{cid}")
                send_text(chat_id, "✅ Source channel hata diya (purani files search me rahengi).")
            except Exception as exc:
                fail(InputError(friendly_tg_error(exc)) if not isinstance(exc, InputError) else exc)
    elif command == "/setbackupchannel":
        if not arg:
            send_text(chat_id, *setting_view("backup_chat_id", "💾 Backup Channel", "ad:setbackup", "ad:backupoff", "Nayi files ki copy is channel me."))
        else:
            handle_admin_input(dict(message, text=arg), "setbackup", None)
    elif command == "/setlogchat":
        if not arg:
            send_text(chat_id, *setting_view("log_chat_id", "📝 Log Chat", "ad:setlog", "ad:logoff", "Index/delivery logs yahan."))
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
    elif command == "/rename":
        parts = text.split(maxsplit=2)
        if len(parts) >= 3 and parts[1].isdigit():
            try:
                do_rename(chat_id, parts[1], parts[2])
            except Exception as exc:
                fail(exc)
        else:
            ask_input(chat_id, user_id, "rename")
    else:
        send_text(chat_id, "Ye command nahi pehchani. Admin Panel:", admin_menu())


def handle_channel_post(message: Dict[str, Any]) -> None:
    channel = message.get("chat", {})
    try:
        allowed = supabase("channels", "GET", query=f"?chat_id=eq.{channel.get('id')}&active=eq.true&select=chat_id")
        if not allowed:
            return
        if file_info(message):
            record = build_metadata(message, channel)
            index_file(record, channel.get("id"), message.get("message_id"), str(channel.get("title") or "channel"))
    except Exception:
        log_error("channel_post")


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


def handle_callback(query: Dict[str, Any]) -> None:
    data = query.get("data", "")
    chat_id = query.get("message", {}).get("chat", {}).get("id")
    user_id = query.get("from", {}).get("id")
    note_user(query.get("from"))
    if data != "verify_join":
        try:
            telegram("answerCallbackQuery", {"callback_query_id": query.get("id")})
        except Exception:
            # An expired callback must not stop the button from working.
            log_error("answerCallbackQuery")
    if data == "verify_join":
        admin = is_subadmin(user_id)
        reqs = [] if admin else required_channels()
        missing = missing_required(user_id, reqs) if reqs else []
        if not missing:
            try:
                telegram("answerCallbackQuery", {"callback_query_id": query.get("id"), "text": "✅ Verified!"})
            except Exception:
                pass
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
            try:
                telegram("answerCallbackQuery", {"callback_query_id": query.get("id"), "text": alert[:200], "show_alert": True})
            except Exception:
                pass
            # Refresh the same gate message in place (no new message spam on every tap).
            show(chat_id, gate_text(missing, was_verified(user_id), len(reqs)), join_markup(missing, len(reqs)), query, "HTML")
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
    if data == "user_categories":
        rows = supabase("books", "GET", query="?select=title,language&status=eq.indexed&limit=500")
        langs = sorted({r.get("language") for r in rows if r.get("language")})
        send_text(chat_id, "🗂️ <b>Categories</b>\n\n" + ("\n".join(f"▸ {esc(x)}" for x in langs) if langs else "Abhi koi category nahi hai.") +
                  "\n\n<i>Book ka naam likhkar search karo 🔍</i>", None, "HTML")
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
    if rows and chat_id:
        deliver_result(chat_id, rows[0], query.get("from"))


def webhook_url() -> str:
    # Always register the production domain over https, even if a preview URL was opened.
    host = os.environ.get("PUBLIC_URL") or os.environ.get("VERCEL_PROJECT_PRODUCTION_URL") or request.host
    host = host.replace("https://", "").replace("http://", "").strip("/")
    return f"https://{host}/api/webhook"


@app.get("/")
def health() -> Any:
    # Visiting the deployed URL once is enough to register the webhook.
    # This keeps first-time setup simple for non-technical admins.
    registered = None
    if BOT_TOKEN:
        try:
            result = telegram("setWebhook", {"url": webhook_url(), "secret_token": WEBHOOK_SECRET,
                                             "allowed_updates": ["message", "channel_post", "callback_query"]})
            registered = bool(result.get("ok"))
            set_command_menu()
        except Exception:
            log_error("setup")
    status = setup_status()
    status["webhook_registered"] = registered
    return jsonify({"ok": True, "service": "telegram-book-bot", "status": status})


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
        },
        "ai": ("on (" + ai_provider() + ")") if ai_provider() else "off",
    }
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


@app.post("/api/webhook")
def webhook() -> Any:
    received = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not WEBHOOK_SECRET or not hmac.compare_digest(received, WEBHOOK_SECRET):
        # Not from Telegram, or the webhook was set without the secret:
        # open the site's home URL once and it re-registers itself.
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    update = request.get_json(silent=True) or {}
    _REQ.clear()
    _LOG_QUEUE.clear()
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
