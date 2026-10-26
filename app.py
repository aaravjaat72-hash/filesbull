import json
import os
import re
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, request

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
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


def build_metadata(message: Dict[str, Any], channel: Dict[str, Any]) -> Dict[str, Any]:
    info = file_info(message) or {}
    caption = message.get("caption", "") or ""
    raw_name = info.get("file_name", "")
    title = clean_text(caption) or clean_text(os.path.splitext(raw_name)[0])
    searchable = " ".join([title, caption, raw_name, channel.get("title", "")])
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
    terms = [normalize(x) for x in re.findall(r"[\w\u0900-\u097F]+", query) if len(x) > 1]
    scored = []
    for row in rows:
        haystack = normalize(" ".join(str(row.get(k, "")) for k in ("title", "caption", "keywords", "source_chat_title")))
        score = sum(1 for term in terms if term in haystack)
        if score:
            scored.append((score, row))
    scored.sort(key=lambda x: (-x[0], x[1].get("title", "")))
    return [row for _, row in scored[:10]]


def send_text(chat_id: int, text: str, reply_markup: Optional[Dict[str, Any]] = None) -> None:
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    telegram("sendMessage", payload)


def deliver_result(chat_id: int, row: Dict[str, Any]) -> None:
    try:
        # copyMessage works for both public and private source channels when permitted.
        telegram("copyMessage", {
            "chat_id": chat_id,
            "from_chat_id": row["source_chat_id"],
            "message_id": row["source_message_id"],
        })
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

    info = file_info(message)
    if info and is_admin(user.get("id")):
        # Files sent directly by an admin are indexed under the bot chat as a local source.
        pseudo_channel = {"id": chat_id, "title": "Admin uploads", "username": None}
        record = build_metadata(message, pseudo_channel)
        record["source_link"] = None
        upsert_item(record)
        send_text(chat_id, f"Indexed: {record['title']}\nLanguage: {record['language']}\nAb admin is file ko search se dhoondh sakte hain.")
        return

    if text.startswith("/start"):
        send_text(chat_id, "Namaste! Book ka naam, subject ya exam likhkar search karein.\n\nExample: SSC English grammar")
        return
    if text.startswith("/help"):
        send_text(chat_id, "/start - bot start\n/search <keywords> - book search\nAdmin: /addchannel @channelusername")
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
    if not is_admin(user_id):
        send_text(chat_id, "Ye admin command hai.")
        return
    if text.startswith("/addchannel"):
        target = text[len("/addchannel"):].strip()
        if not target:
            send_text(chat_id, "Use: /addchannel @channelusername\nBot ko pehle channel mein admin banayein.")
            return
        try:
            channel = telegram("getChat", {"chat_id": target}).get("result", {})
            telegram("getChatMember", {"chat_id": channel["id"], "user_id": telegram("getMe", {}).get("result", {}).get("id")})
            supabase("channels", "POST", {
                "chat_id": channel["id"],
                "title": channel.get("title", target),
                "username": channel.get("username"),
                "active": True,
            }, "?on_conflict=chat_id")
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
    if text.startswith("/stats"):
        rows = supabase("books", "GET", query="?select=id&limit=1000")
        send_text(chat_id, f"Indexed files: {len(rows)}")
        return
    send_text(chat_id, "Admin commands: /addchannel @username, /channels, /stats")


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


def handle_callback(query: Dict[str, Any]) -> None:
    data = query.get("data", "")
    chat_id = query.get("message", {}).get("chat", {}).get("id")
    telegram("answerCallbackQuery", {"callback_query_id": query.get("id")})
    if not data.startswith("book:"):
        return
    book_id = data.split(":", 1)[1]
    rows = supabase("books", "GET", query=f"?id=eq.{urllib.parse.quote(book_id)}&select=*&limit=1")
    if rows and chat_id:
        deliver_result(chat_id, rows[0])


@app.get("/")
def health() -> Any:
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
            if text.startswith("/") and is_admin(message.get("from", {}).get("id")):
                handle_admin_command(message)
            else:
                handle_private_message(message)
    except Exception:
        # Always return 200 so Telegram does not retry a malformed/duplicate update forever.
        pass
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
