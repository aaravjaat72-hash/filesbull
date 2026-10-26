#!/usr/bin/env python3
"""filesbull USERBOT forwarder (GitHub Action .github/workflows/userbot-forward.yml, every 10 min + "🔄 Abhi check karo").

Channels where the bot is NOT admin can't be read by the bot. The owner logged in his own Telegram account once (bot's
login page); the bot keeps that session encrypted and hands it only to this workflow after GitHub's OIDC token proves
the run is this repo's userbot workflow on main. Then, gently:
  * per source channel: posts newer than the stored last id (first look: only the last ~20 posts of the last 24 h)
  * only file posts (documents/PDF, video, audio; no text/photos/stickers/GIFs/voice), not already in the bot's DB
  * forward to the Backup channel (or download + re-upload with the original caption when the channel blocks
    forwarding), max 25 per run, 2–4 s random gap, stop at FloodWait (rest next run)
  * report each file to the bot right away (the bot saves, names and categorises it) and the run at the end.
Nothing secret is printed (session/api hash are masked; no phone numbers or ids of people).
"""
import asyncio, base64, hashlib, hmac, json, os, random, shutil, struct, subprocess, sys, tempfile, time, urllib.request, urllib.error

BOT_URL = os.environ.get("BOT_URL", "https://filesbull.vercel.app").rstrip("/")
AUD = "filesbull-userbot"


def mask(v):
    if v:
        print(f"::add-mask::{v}", flush=True)


def http(url, payload=None, headers=None, timeout=60):
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET",
                                 headers={"Content-Type": "application/json", "User-Agent": "filesbull-userbot/1.0", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}


def oidc_token():
    url, tok = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL"), os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    if not url or not tok:
        sys.exit("OIDC not available (workflow needs permissions: id-token: write)")
    req = urllib.request.Request(url + "&audience=" + AUD, headers={"Authorization": "bearer " + tok})
    with urllib.request.urlopen(req, timeout=30) as r:
        v = json.loads(r.read().decode())["value"]
    mask(v)
    return v


class Bot:
    def __init__(self, job):
        self.run, self.key = job["run"], job["k"]

    def post(self, path, payload, tries=3):
        body = json.dumps(dict(payload, run=self.run), ensure_ascii=False, separators=(",", ":")).encode()
        sig = hmac.new(self.key.encode(), body, hashlib.sha256).hexdigest()
        for i in range(tries):
            req = urllib.request.Request(BOT_URL + path, data=body, method="POST",
                                         headers={"Content-Type": "application/json", "X-Filesbull-Signature": sig,
                                                  "User-Agent": "filesbull-userbot/1.0"})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return json.loads(r.read().decode() or "{}")
            except urllib.error.HTTPError as e:
                print(f"bot {path} HTTP {e.code}")
                if e.code < 500:
                    return {}
            except Exception as e:
                print(f"bot {path} error {type(e).__name__}")
            time.sleep(4 * (i + 1))
        return {}


def file_unique_id(doc_id):
    """Bot API file_unique_id of a document (type 2 + media id, zero-run-length encoded, urlsafe base64)."""
    raw = struct.pack("<iq", 2, int(doc_id))
    out, n = bytearray(), 0
    for b in raw:
        if b == 0:
            n += 1
            continue
        if n:
            out += bytes((0, n))
            n = 0
        out.append(b)
    if n:
        out += bytes((0, n))
    return base64.urlsafe_b64encode(bytes(out)).decode().rstrip("=")


def is_file_post(m):
    """documents/PDF, video, audio — not stickers, GIFs, voice notes, round videos, photos or plain text."""
    if not getattr(m, "document", None):
        return False
    if getattr(m, "sticker", None) or getattr(m, "gif", None) or getattr(m, "voice", None) or getattr(m, "video_note", None):
        return False
    return True


def item_of(m):
    f = m.file
    return {"u": file_unique_id(m.document.id), "n": (f.name or "") if f else "", "s": int((f.size or 0) if f else 0)}


async def send_one(client, backup, ent, m, tmp, protected):
    """Forward; when the channel blocks forwarding, download + re-upload with the original caption. Returns (id, mode)."""
    from telethon import errors
    if not protected:
        try:
            res = await client.forward_messages(backup, m)
            res = res[0] if isinstance(res, list) else res
            return int(res.id), "forward"
        except errors.ChatForwardsRestrictedError:
            pass
    path = None
    try:
        work = tempfile.mkdtemp(dir=tmp)
        path = await client.download_media(m, file=work + os.sep)
        if not path or not os.path.exists(path) or os.path.getsize(path) == 0:
            raise RuntimeError("download khali aaya")
        attrs = list(getattr(m.document, "attributes", None) or [])
        mime = (getattr(m.document, "mime_type", "") or "").lower()
        sent = await client.send_file(backup, path, caption=m.message or "", formatting_entities=m.entities or None,
                                      attributes=attrs, force_document=not mime.startswith(("video/", "audio/")),
                                      supports_streaming=mime.startswith("video/"))
        return int(sent.id), "upload"
    finally:
        if path:
            shutil.rmtree(os.path.dirname(path), ignore_errors=True)


async def main_async(job):
    from telethon import TelegramClient, errors, utils
    from telethon.sessions import StringSession
    bot = Bot(job)
    summary = {"kind": "final", "last": {}, "errors": {}, "ok_ids": [], "titles": {}, "sent": 0, "checked": 0}
    client = TelegramClient(StringSession(job["session"]), int(job["api_id"]), job["api_hash"],
                            device_model=job.get("device") or "Filesbull Userbot", system_version="filesbull",
                            app_version="1.0", lang_code="en", system_lang_code="en", flood_sleep_threshold=0)
    tmp = tempfile.mkdtemp(prefix="userbot-")
    try:
        await client.connect()
        if not await client.is_user_authorized():
            summary["fatal"] = "revoked"
            print("session is not authorized any more (logged out / revoked)")
            return summary
        me = await client.get_me()
        summary["me"] = {"username": me.username or "", "name": " ".join(x for x in (me.first_name, me.last_name) if x)}
        want = {int(s["id"]): s for s in job["sources"]}
        ents, backup = {}, None
        async for d in client.iter_dialogs(limit=None):
            pid = utils.get_peer_id(d.entity)
            if pid in want:
                ents[pid] = d.entity
            if pid == int(job["backup"]):
                backup = d.entity
        if backup is None:
            summary["fatal"] = "aapka account Backup channel me nahi hai (ya post ka haq nahi)"
            return summary
        print(f"{len(want)} channel(s) to check, {len(ents)} joined")
        budget = int(job.get("max") or 25)
        gap = job.get("gap") or [2.0, 4.0]
        stop = ""
        for cid, src in want.items():
            if stop:
                break
            ent = ents.get(cid)
            if ent is None:
                summary["errors"][str(cid)] = "aapka account is channel me joined nahi"
                continue
            summary["titles"][str(cid)] = getattr(ent, "title", "") or src.get("title") or ""
            last = int(src.get("last") or 0)
            try:
                if last <= 0:
                    recent = await client.get_messages(ent, limit=int(job.get("first_max") or 20))
                    top = max([m.id for m in recent] or [0])
                    cutoff = time.time() - 3600 * float(job.get("first_hours") or 24)
                    msgs = sorted([m for m in recent if m.date and m.date.timestamp() >= cutoff], key=lambda m: m.id)
                else:
                    top = last
                    msgs = [m async for m in client.iter_messages(ent, min_id=last, reverse=True, limit=300)]
                summary["checked"] += len(msgs)
                files = [m for m in msgs if is_file_post(m)]
                known = []
                if files:
                    res = bot.post("/api/userbot-seen", {"items": [item_of(m) for m in files]})
                    known = res.get("known") or []
                    if len(known) != len(files):
                        raise RuntimeError("bot se duplicate check nahi hua")
                kmap = {m.id: bool(k) for m, k in zip(files, known)}
                protected = bool(getattr(ent, "noforwards", False))
                done_to = last
                n_new = 0
                for m in msgs:
                    if m.id in kmap and not kmap[m.id]:
                        if budget <= 0:
                            stop = f"limit {job.get('max')} files/run, baaki agli baar"
                            break
                        try:
                            bid, mode = await send_one(client, backup, ent, m, tmp, protected or bool(getattr(m, "noforwards", False)))
                        except (errors.FloodWaitError, errors.AuthKeyUnregisteredError, errors.SessionRevokedError,
                                errors.UserDeactivatedError, errors.UserDeactivatedBanError, errors.AuthKeyDuplicatedError,
                                errors.ChatAdminRequiredError, errors.ChatWriteForbiddenError):
                            raise
                        except Exception as e:      # one bad post must not block the channel forever: skip it
                            summary["errors"][str(cid)] = f"ek file nahi gayi ({type(e).__name__})"
                            print("one file failed:", type(e).__name__)
                            done_to = max(done_to, m.id)
                            continue
                        budget -= 1
                        n_new += 1
                        summary["sent"] += 1
                        print(f"sent 1 file ({mode})")
                        bot.post("/api/userbot-done", {"kind": "file", "backup_msg": bid, "mode": mode,
                                                       "src": {"id": cid, "title": summary["titles"][str(cid)],
                                                               "username": getattr(ent, "username", "") or "", "msg": m.id}})
                        await asyncio.sleep(random.uniform(float(gap[0]), float(gap[1])))
                    done_to = max(done_to, m.id)
                else:
                    done_to = max(done_to, top)
                summary["last"][str(cid)] = done_to
                summary["ok_ids"].append(cid)
                print(f"channel done: {n_new} new file(s)")
            except errors.FloodWaitError as e:
                stop = f"Telegram ne {int(e.seconds)} sec rukne bola, baaki agli baar"
                print(f"FloodWait {int(e.seconds)} s: stopping, the rest goes next run")
                break
            except (errors.AuthKeyUnregisteredError, errors.SessionRevokedError, errors.UserDeactivatedError,
                    errors.UserDeactivatedBanError, errors.AuthKeyDuplicatedError) as e:
                summary["fatal"] = "revoked"
                print("session revoked:", type(e).__name__)
                break
            except (errors.ChannelPrivateError, errors.ChatAdminRequiredError, errors.ChatWriteForbiddenError) as e:
                summary["errors"][str(cid)] = {"ChannelPrivateError": "channel private/ban",
                                               "ChatAdminRequiredError": "Backup channel me post ka haq nahi",
                                               "ChatWriteForbiddenError": "Backup channel me post ka haq nahi"}[type(e).__name__]
                if type(e).__name__ != "ChannelPrivateError":
                    summary["fatal"] = "aapka account Backup channel me post nahi kar sakta (admin banao)"
                    break
            except Exception as e:
                summary["errors"][str(cid)] = (type(e).__name__ + (": " + str(e)[:80] if isinstance(e, RuntimeError) else ""))
                print("channel failed:", type(e).__name__)
        summary["stop"] = stop
        return summary
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            await client.disconnect()
        except Exception:
            pass


def main():
    status, job = http(BOT_URL + "/api/userbot-job", {"oidc": oidc_token()})
    if status != 200 or not job.get("ok"):
        sys.exit(f"bot said HTTP {status}: {job.get('error')} {job.get('why', '')}")
    for k in ("session", "api_hash", "k"):
        mask(str(job.get(k) or ""))
    state = job.get("state")
    if state != "ready":
        print({"not_logged_in": "userbot: not logged in (owner hasn't logged in yet) — nothing to do",
               "paused": "userbot: paused by the owner — nothing to do",
               "missing_secrets": "userbot: TG_API_ID / TG_API_HASH missing in Vercel — nothing to do",
               "no_backup": "userbot: no Backup channel set — nothing to do"}.get(state, f"userbot: {state}"))
        return
    if not job.get("sources"):
        print("userbot: no source channel without bot admin — nothing to do")
        Bot(job).post("/api/userbot-done", {"kind": "final", "sent": 0, "checked": 0})
        return
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "telethon==1.42.0", "cryptg"], check=True)
    bot = Bot(job)
    try:
        summary = asyncio.run(asyncio.wait_for(main_async(job), timeout=35 * 60))
    except Exception as e:
        print("run failed:", type(e).__name__)
        summary = {"kind": "final", "error": f"run fail: {type(e).__name__}"}
    out = {k: v for k, v in summary.items()}
    bot.post("/api/userbot-done", out)
    print(json.dumps({"sent": summary.get("sent"), "checked": summary.get("checked"), "stop": summary.get("stop"),
                      "errors": len(summary.get("errors") or {}), "fatal": bool(summary.get("fatal"))}))


if __name__ == "__main__":
    main()
