import json
import io
import os
import re
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
ADMIN_USER_IDS = {
    int(x.strip()) for x in os.environ.get("ADMIN_USER_IDS", "").split(",") if x.strip().isdigit()
}

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"


def http_json(url: str, method: str = "GET", payload: Optional[Dict[str, Any]] = None,
              headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    body = None
    final_headers = {"Content-Type": "application/json"}
    if headers:
        final_headers.update(headers)
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=final_headers, method=method)
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


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


def is_subadmin(user_id: Optional[int]) -> bool:
    if is_admin(user_id):
        return True
    if not user_id:
        return False
    try:
        return bool(supabase("admins", "GET", query=f"?user_id=eq.{user_id}&select=user_id&limit=1"))
    except Exception:
        return False


def required_channels() -> List[Dict[str, Any]]:
    try:
        return supabase("required_channels", "GET", query="?active=eq.true&select=*&order=title")
    except Exception:
        return []


def user_has_required_access(user_id: Optional[int]) -> bool:
    if is_subadmin(user_id):
        return True
    channels = required_channels()
    if not channels or not user_id:
        return True
    for channel in channels:
        try:
            member = telegram("getChatMember", {"chat_id": channel["chat_id"], "user_id": user_id}).get("result", {})
            if member.get("status") in {"left", "kicked"}:
                return False
        except Exception:
            return False
    return True


def join_markup() -> Dict[str, Any]:
    rows = []
    for channel in required_channels():
        url = channel.get("invite_link") or (f"https://t.me/{channel['username']}" if channel.get("username") else None)
        if url:
            rows.append([{"text": f"Join: {channel.get('title', 'Channel')[:30]}", "url": url}])
    rows.append([{"text": "I Joined - Verify", "callback_data": "verify_join"}])
    return {"inline_keyboard": rows}


def ai_rank(query: str, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not OPENAI_API_KEY or len(rows) < 2:
        return rows
    compact = [{"id": r.get("id"), "title": r.get("title"), "keywords": r.get("keywords", "")[:500], "content": r.get("content_text", "")[:700]} for r in rows[:10]]
    prompt = "Select only the IDs of files that best satisfy the user's request. Return JSON with an ids array of numbers. Never invent IDs. User: " + query + "\nFiles: " + json.dumps(compact, ensure_ascii=False)
    try:
        result = http_json("https://api.openai.com/v1/chat/completions", "POST", {
            "model": OPENAI_MODEL,
            "messages": [{"role": "system", "content": "You rank a private PDF catalog. Output valid JSON only."}, {"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_completion_tokens": 300,
        }, {"Authorization": f"Bearer {OPENAI_API_KEY}"})
        content = result["choices"][0]["message"]["content"]
        ids = json.loads(content).get("ids", [])
        by_id = {str(r.get("id")): r for r in rows}
        ranked = [by_id[str(i)] for i in ids if str(i) in by_id]
        return ranked or rows
    except Exception:
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


def extract_pdf_text(file_id: Optional[str], file_name: str = "") -> str:
    """Best-effort extraction for normal text PDFs; scanned PDFs need OCR."""
    if not file_id or not file_name.lower().endswith(".pdf") or not BOT_TOKEN:
        return ""
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
            if sum(len(x) for x in chunks) >= 120000:
                break
        return clean_text(" ".join(chunks))[:120000]
    except Exception:
        return ""


def build_metadata(message: Dict[str, Any], channel: Dict[str, Any]) -> Dict[str, Any]:
    info = file_info(message) or {}
    caption = message.get("caption", "") or ""
    raw_name = info.get("file_name", "")
    title = clean_text(caption) or clean_text(os.path.splitext(raw_name)[0])
    content_text = extract_pdf_text(info.get("file_id"), raw_name)
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


def search_books(query: str) -> List[Dict[str, Any]]:
    rows = supabase("books", "GET", query="?select=*&status=eq.indexed&limit=500")
    raw_terms = [normalize(x) for x in re.findall(r"[\w\u0900-\u097F]+", query) if len(x) > 1]
    aliases = {
        "eco": {"eco", "economy", "economic", "economics"},
        "economy": {"eco", "economy", "economic", "economics"},
        "economic": {"eco", "economy", "economic", "economics"},
        "economics": {"eco", "economy", "economic", "economics"},
    }
    terms = set(raw_terms)
    for term in raw_terms:
        terms.update(aliases.get(term, set()))
    scored = []
    for row in rows:
        haystack = normalize(" ".join(str(row.get(k, "")) for k in ("title", "caption", "keywords", "content_text", "source_chat_title")))
        score = sum(1 for term in terms if term in haystack)
        if row.get("content_text") and any(term in normalize(row["content_text"]) for term in raw_terms):
            score += 3
        if score:
            scored.append((score, row))
    scored.sort(key=lambda x: (-x[0], x[1].get("title", "")))
    return ai_rank(query, [row for _, row in scored[:10]])


def send_text(chat_id: int, text: str, reply_markup: Optional[Dict[str, Any]] = None) -> None:
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    telegram("sendMessage", payload)


def set_command_menu() -> None:
    telegram("setMyCommands", {"commands": [
        {"command": "start", "description": "Bot start karein"},
        {"command": "search", "description": "Book/file search karein"},
        {"command": "help", "description": "Help aur instructions"},
        {"command": "addchannel", "description": "Admin: channel add karein"},
        {"command": "channels", "description": "Admin: active channels"},
        {"command": "removechannel", "description": "Admin: channel disable karein"},
        {"command": "stats", "description": "Admin: indexed files count"},
        {"command": "addadmin", "description": "Owner: sub-admin add karein"},
        {"command": "removeadmin", "description": "Owner: sub-admin remove karein"},
        {"command": "requirechannel", "description": "Admin: join channel set karein"},
        {"command": "requiredchannels", "description": "Admin: join channels list"},
    ]})


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


def deliver_result(chat_id: int, row: Dict[str, Any]) -> None:
    try:
        # copyMessage works for both public and private source channels when permitted.
        telegram("copyMessage", {
            "chat_id": chat_id,
            "from_chat_id": row["source_chat_id"],
            "message_id": row["source_message_id"],
        })
        try:
            supabase("delivery_logs", "POST", {"user_id": chat_id, "book_id": row.get("id"), "book_title": row.get("title")})
        except Exception:
            pass
        return
    except Exception:
        pass
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

    if not user_has_required_access(user.get("id")):
        send_text(chat_id, "Bot use karne ke liye pehle required channel join karein, phir Verify dabayein.", join_markup())
        return

    info = file_info(message)
    if is_admin(user.get("id")) and forwarded_source(message):
        source = forwarded_source(message)
        try:
            register_channel(source)
            if info:
                record = build_metadata(message, source)
                # A forwarded message has a new message_id in the bot chat. Delivery
                # must use the original message_id from the source channel.
                if source.get("message_id"):
                    record["source_message_id"] = source["message_id"]
                    record["source_link"] = message_link(source, source["message_id"])
                upsert_item(record)
                send_text(chat_id, f"Channel auto-add aur file index ho gayi: {source.get('title', 'source')}\nFile: {clean_text(info.get('file_name', 'file'))}")
            else:
                send_text(chat_id, f"Channel auto-add ho gaya: {source.get('title', 'source')}\nAb is channel ki nayi files index hongi.")
        except Exception as exc:
            send_text(chat_id, f"Forwarded channel add nahi hua: {str(exc)[:250]}")
        return
    if info and is_admin(user.get("id")):
        # Files sent directly by an admin are indexed under the bot chat as a local source.
        pseudo_channel = {"id": chat_id, "title": "Admin uploads", "username": None}
        record = build_metadata(message, pseudo_channel)
        record["source_link"] = None
        upsert_item(record)
        send_text(chat_id, f"Indexed: {record['title']}\nLanguage: {record['language']}\nAb admin is file ko search se dhoondh sakte hain.")
        return

    if text.startswith("/start"):
        menu = {"inline_keyboard": [[{"text": "Search PDF", "callback_data": "user_search"}], [{"text": "Categories", "callback_data": "user_categories"}, {"text": "PDF List", "callback_data": "user_list"}], [{"text": "Help", "callback_data": "user_help"}]]}
        send_text(chat_id, "Namaste! Book/file ka naam, subject ya exam likhkar search karein.", menu)
        return
    if text.startswith("/help"):
        send_text(chat_id, "/search <keywords> - book search\nCategories - category list\nPDF List - available files\nAdmin commands menu se alag dikhengi.")
        return
    if text.startswith("/search"):
        query = text[len("/search"):].strip()
    else:
        query = text
    if not query:
        send_text(chat_id, "Search ke liye book ka naam ya keywords bhejiye.")
        return

    try:
        results = search_books(query)
    except Exception:
        send_text(chat_id, "Database abhi available nahi hai. Admin se setup check karne ko kahen.")
        return
    if not results:
        send_text(chat_id, "Is search ke liye koi matching file nahi mili.")
        return
    buttons = []
    for index, row in enumerate(results):
        label = f"{index + 1}. {row.get('title', 'Untitled')[:45]}"
        buttons.append([{ "text": label, "callback_data": f"book:{row['id']}" }])
    send_text(chat_id, f"{len(results)} result mile. File select karein:", {"inline_keyboard": buttons})


def handle_admin_command(message: Dict[str, Any]) -> None:
    text = (message.get("text") or "").strip()
    chat_id = message.get("chat", {}).get("id")
    user_id = message.get("from", {}).get("id")
    if not is_subadmin(user_id):
        send_text(chat_id, "Ye admin command hai.")
        return
    if text.startswith("/addadmin"):
        if not is_owner(user_id):
            send_text(chat_id, "Sirf primary owner sub-admin add kar sakta hai.")
            return
        target = text[len("/addadmin"):].strip()
        if not target.isdigit():
            send_text(chat_id, "Use: /addadmin TELEGRAM_NUMERIC_ID")
            return
        supabase("admins", "POST", {"user_id": int(target), "role": "subadmin"}, "?on_conflict=user_id")
        send_text(chat_id, "Sub-admin add ho gaya.")
        return
    if text.startswith("/removeadmin"):
        if not is_owner(user_id):
            send_text(chat_id, "Sirf primary owner sub-admin remove kar sakta hai.")
            return
        target = text[len("/removeadmin"):].strip()
        if target.isdigit():
            supabase("admins", "DELETE", query=f"?user_id=eq.{target}")
            send_text(chat_id, "Sub-admin remove ho gaya.")
        return
    if text.startswith("/requirechannel"):
        target = text[len("/requirechannel"):].strip()
        try:
            channel = telegram("getChat", {"chat_id": target}).get("result", {})
            link = f"https://t.me/{channel['username']}" if channel.get("username") else None
            supabase("required_channels", "POST", {"chat_id": channel["id"], "title": channel.get("title", target), "username": channel.get("username"), "invite_link": link, "active": True}, "?on_conflict=chat_id")
            send_text(chat_id, "Required join channel save ho gaya.")
        except Exception as exc:
            send_text(chat_id, f"Required channel add nahi hua: {str(exc)[:250]}")
        return
    if text.startswith("/requiredchannels"):
        rows = required_channels()
        send_text(chat_id, "Required channels:\n" + ("\n".join(f"- {r.get('title')}" for r in rows) if rows else "Koi required channel nahi hai."))
        return
    if text.startswith("/addchannel"):
        target = text[len("/addchannel"):].strip()
        if not target:
            send_text(chat_id, "2 tareeqe:\n1. /addchannel @channelusername\n2. Channel ki file/post bot ko forward karein; channel automatic add ho jayega.")
            return
        try:
            if target.startswith("https://t.me/"):
                target = "@" + target.rstrip("/").split("/")[-1].split("?")[0]
            channel = telegram("getChat", {"chat_id": target}).get("result", {})
            bot_id = telegram("getMe", {}).get("result", {}).get("id")
            membership = telegram("getChatMember", {"chat_id": channel["id"], "user_id": bot_id}).get("result", {})
            if membership.get("status") not in {"administrator", "creator"}:
                raise ValueError("Bot channel ka administrator nahi hai")
            register_channel(channel)
            send_text(chat_id, f"Channel add ho gaya: {channel.get('title', target)}")
        except Exception as exc:
            send_text(chat_id, f"Channel add nahi hua. Bot admin hai aur username/ID sahi hai?\n{str(exc)[:300]}")
        return
    if text.startswith("/channels"):
        rows = supabase("channels", "GET", query="?select=*&active=eq.true&order=title")
        if not rows:
            send_text(chat_id, "Abhi koi active channel nahi hai.")
        else:
            send_text(chat_id, "Active channels:\n" + "\n".join(f"- {r.get('title')} ({r.get('username') or r.get('chat_id')})" for r in rows))
        return
    if text.startswith("/removechannel"):
        target = text[len("/removechannel"):].strip()
        if not target:
            send_text(chat_id, "Use: /removechannel @channelusername ya channel ID")
            return
        try:
            if target.startswith("@"):
                target = str(telegram("getChat", {"chat_id": target}).get("result", {}).get("id"))
            supabase("channels", "PATCH", {"active": False}, f"?chat_id=eq.{urllib.parse.quote(target)}")
            send_text(chat_id, "Channel disable kar diya gaya.")
        except Exception as exc:
            send_text(chat_id, f"Channel disable nahi hua: {str(exc)[:250]}")
        return
    if text.startswith("/stats"):
        rows = supabase("books", "GET", query="?select=id&limit=1000")
        send_text(chat_id, f"Indexed files: {len(rows)}")
        return
    send_text(chat_id, "Admin commands: /addchannel, /channels, /removechannel, /stats")


def handle_channel_post(message: Dict[str, Any]) -> None:
    channel = message.get("chat", {})
    try:
        allowed = supabase("channels", "GET", query=f"?chat_id=eq.{channel.get('id')}&active=eq.true&select=chat_id")
        if not allowed:
            return
        if file_info(message):
            upsert_item(build_metadata(message, channel))
    except Exception:
        return


def handle_group_file(message: Dict[str, Any]) -> None:
    chat = message.get("chat", {})
    if chat.get("type") not in {"group", "supergroup"} or not file_info(message):
        return
    if not is_admin(message.get("from", {}).get("id")):
        return
    try:
        register_channel(chat)
        upsert_item(build_metadata(message, chat))
    except Exception:
        return


def handle_callback(query: Dict[str, Any]) -> None:
    data = query.get("data", "")
    chat_id = query.get("message", {}).get("chat", {}).get("id")
    telegram("answerCallbackQuery", {"callback_query_id": query.get("id")})
    user_id = query.get("from", {}).get("id")
    if not is_subadmin(user_id) and not user_has_required_access(user_id):
        send_text(chat_id, "Pehle required channel join karein.", join_markup())
        return
    if data == "verify_join":
        if user_has_required_access(user_id):
            send_text(chat_id, "Verified. Ab aap bot use kar sakte hain. /start dabayein.")
        else:
            send_text(chat_id, "Abhi required channel join nahi mila. Pehle Join button dabayein, phir Verify karein.", join_markup())
        return
    if data == "user_help":
        send_text(chat_id, "Book, subject, exam ya keyword bhejiye. Bot available indexed files me se result dega.")
        return
    if data == "user_categories":
        rows = supabase("books", "GET", query="?select=title,language&status=eq.indexed&limit=500")
        langs = sorted({r.get("language") for r in rows if r.get("language")})
        send_text(chat_id, "Available language/categories metadata:\n" + ("\n".join(f"- {x}" for x in langs) if langs else "Abhi categories indexed nahi hain."))
        return
    if data == "user_list":
        rows = supabase("books", "GET", query="?select=id,title,source_chat_title&status=eq.indexed&order=title&limit=30")
        send_text(chat_id, "Available PDFs:\n" + ("\n".join(f"- {r.get('title')} ({r.get('source_chat_title', 'source')})" for r in rows) if rows else "Abhi koi file indexed nahi hai."))
        return
    if data == "user_search":
        send_text(chat_id, "Search ke liye book, subject ya exam ka naam bhejiye.")
        return
    if not data.startswith("book:"):
        return
    book_id = data.split(":", 1)[1]
    rows = supabase("books", "GET", query=f"?id=eq.{urllib.parse.quote(book_id)}&select=*&limit=1")
    if rows and chat_id:
        deliver_result(chat_id, rows[0])


@app.get("/")
def health() -> Any:
    # Visiting the deployed URL once is enough to register the webhook.
    # This keeps first-time setup simple for non-technical admins.
    if BOT_TOKEN:
        try:
            public_url = request.url_root.rstrip("/") + "/api/webhook"
            telegram("setWebhook", {"url": public_url})
            set_command_menu()
        except Exception:
            pass
    return jsonify({"ok": True, "service": "telegram-book-bot"})


@app.post("/api/webhook")
def webhook() -> Any:
    update = request.get_json(silent=True) or {}
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
                return jsonify({"ok": True})
            if text.startswith("/") and is_subadmin(message.get("from", {}).get("id")):
                handle_admin_command(message)
            else:
                handle_private_message(message)
    except Exception:
        # Always return 200 so Telegram does not retry a malformed/duplicate update forever.
        pass
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
