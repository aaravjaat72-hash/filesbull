#!/usr/bin/env python3
"""filesbull USERBOT (GitHub Action .github/workflows/userbot-forward.yml, every 10 min + instant dispatch).

Channels where the bot is NOT admin can't be read by the bot. The owner logged in a Telegram account once (bot's
login page); the bot keeps that session encrypted and hands it only to this workflow after GitHub's OIDC token proves
the run is this repo's userbot workflow on main. Each run, gently:
  * setup: if no "filesbull Userbot Inbox" / "filesbull Inbox Alerts" channel is set, create it (private broadcast
    channel) and make the bot admin there; report the id to the bot (signed)
  * join new source channels the account is not in yet (public @username, t.me link, stored invite link), max 3/run
  * LIVE: posts newer than the stored last id (first look: only the last ~20 posts of the last 24 h)
  * HISTORY: per channel a cursor walking newest → oldest, with what is left of the shared cap (live first)
  * only file posts (documents/PDF, video, audio), never one the bot already has (file_unique_id / name+size /
    same name ±1 % size) or one already sent in this run → forward ONLY into the Inbox (download + re-upload when the
    channel blocks forwarding), max 25 per run (15 after a FloodWait > 10 min), 2–4 s apart, stop at FloodWait
  * report each file to the bot at once (the bot names + categorises it), then pump the bot's settle step
    (Inbox → Backup, one file per call), then the run's summary.
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


def item_key(it):
    return it["u"], (norm_name(it["n"]), int(it["s"]))


def norm_name(n):
    import re
    return re.sub(r"[^a-z0-9\u0900-\u097f]+", "", os.path.splitext(str(n or ""))[0].lower())


ERR_WORDS = {"UserRestrictedError": "Telegram ne is account ko naya channel banane se roka hai (spam limit)",
             "ChannelsTooMuchError": "account me bahut zyada channels hain",
             "ChannelsAdminPublicTooMuchError": "account me bahut zyada channels hain",
             "UserNotMutualContactError": "bot ko admin nahi bana paya",
             "BotGroupsBlockedError": "bot ko channel me add nahi kar paya",
             "UserBotError": "bot ko admin nahi bana paya"}


class Stop(Exception):
    pass


async def create_channel(client, title, about, bot_username):
    """A private broadcast channel owned by the userbot account, with the bot as admin (post/edit/delete)."""
    from telethon.tl.functions.channels import CreateChannelRequest
    r = await client(CreateChannelRequest(title=title, about=about, broadcast=True))
    ent = r.chats[0]
    await make_bot_admin(client, ent, bot_username)
    return ent


async def make_bot_admin(client, ent, bot_username):
    last = None
    for i in range(2):
        try:
            await client.edit_admin(ent, bot_username, post_messages=True, edit_messages=True, delete_messages=True,
                                    invite_users=True, title="filesbull")
            return
        except Exception as e:         # noqa: PERF203
            from telethon import errors
            if isinstance(e, errors.FloodWaitError):
                raise
            last = e
            await asyncio.sleep(2)
    raise last


def invite_hash(link):
    import re
    m = re.search(r"(?:t\.me|telegram\.me)/(?:\+|joinchat/)([A-Za-z0-9_\-]{8,})", str(link or ""))
    return m.group(1) if m else ""


async def join_one(client, username="", link=""):
    """→ the joined channel entity. Raises Telethon errors (FloodWait etc.)."""
    from telethon import errors
    from telethon.tl.functions.channels import JoinChannelRequest
    from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest
    if username:
        r = await client(JoinChannelRequest(username))
        return r.chats[0]
    h = invite_hash(link)
    if not h:
        raise ValueError("no link")
    try:
        r = await client(ImportChatInviteRequest(h))
        return r.chats[0]
    except errors.UserAlreadyParticipantError:
        r = await client(CheckChatInviteRequest(h))
        return r.chat


class Run:
    def __init__(self, client, bot, job, summary):
        self.client, self.bot, self.job, self.s = client, bot, job, summary
        self.budget = int(job.get("max") or 25)
        self.gap = job.get("gap") or [2.0, 4.0]
        self.seen_u, self.seen_ns = set(), set()
        self.live_first = {}
        self.stop = ""

    def fresh(self, files, title=""):
        """Which of these file posts are new: not known to the bot (any status) and not already sent this run."""
        if not files:
            return {}
        items = [item_of(m) for m in files]
        res = self.bot.post("/api/userbot-seen", {"items": items})
        known = res.get("known") or []
        if len(known) != len(files):
            raise RuntimeError("bot se duplicate check nahi hua")
        out = {}
        for m, it, k in zip(files, items, known):
            u, (nm, sz) = item_key(it)
            dup = bool(k) or u in self.seen_u or bool(nm and any(nm == a and abs(sz - b) <= max(1, b * 0.01) for a, b in self.seen_ns))
            self.seen_u.add(u)
            if nm:
                self.seen_ns.add((nm, sz))
            out[m.id] = not dup
            if dup and len(self.s["dup_items"]) < 150:
                self.s["dup_items"].append({"n": it["n"][:80], "ch": str(title)[:40]})
        return out

    async def send(self, ent, cid, m, lane, protected):
        """Forward one post into the Inbox and report it. False = stop this lane (not confirmed)."""
        from telethon import errors
        if self.budget <= 0:
            self.stop = self.stop or f"limit {self.job.get('max')} files/run, baaki agli baar"
            raise Stop()
        try:
            bid, mode = await send_one(self.client, self.inbox, ent, m, self.tmp, protected or bool(getattr(m, "noforwards", False)))
        except (errors.FloodWaitError, errors.AuthKeyUnregisteredError, errors.SessionRevokedError,
                errors.UserDeactivatedError, errors.UserDeactivatedBanError, errors.AuthKeyDuplicatedError,
                errors.ChatAdminRequiredError, errors.ChatWriteForbiddenError):
            raise
        except Exception as e:      # one bad post must not block the channel forever: skip it
            self.s["errors"][str(cid)] = f"ek file nahi gayi ({type(e).__name__})"
            print("one file failed:", type(e).__name__)
            return True
        self.budget -= 1
        self.s["sent"] += 1
        if lane == "bf":
            self.s["bf_sent"] += 1
        print(f"sent 1 file to the Inbox ({mode}, {lane})")
        reported = self.bot.post("/api/userbot-done", {
            "kind": "file", "lane": lane, "inbox": self.inbox_id, "inbox_msg": bid, "mode": mode,
            "src": {"id": cid, "title": self.s["titles"].get(str(cid)) or "", "username": getattr(ent, "username", "") or "",
                    "msg": m.id, "date": int(m.date.timestamp()) if getattr(m, "date", None) else 0}})
        if reported.get("result") not in ("saved", "dup"):
            # Never checkpoint an unindexed file: the Inbox post stays; next run tries this source post again and the
            # DB's file_unique_id catches it if the report did land.
            self.stop = "Inbox post ka bot me save confirm nahi hua, agli baar phir check"
            self.s["error"] = "Inbox me file aayi par bot me save confirm nahi hua; agle run me dobara check"
            raise Stop()
        await asyncio.sleep(random.uniform(float(self.gap[0]), float(self.gap[1])))
        return True

    async def live(self, ent, cid, src):
        client = self.client
        last = int(src.get("last") or 0)
        if last <= 0:
            recent = await client.get_messages(ent, limit=int(self.job.get("first_max") or 20))
            top = max([m.id for m in recent] or [0])
            cutoff = time.time() - 3600 * float(self.job.get("first_hours") or 24)
            msgs = sorted([m for m in recent if m.date and m.date.timestamp() >= cutoff], key=lambda m: m.id)
            self.live_first[cid] = msgs[0].id if msgs else top + 1      # history starts just below what live took
        else:
            top = last
            msgs = [m async for m in client.iter_messages(ent, min_id=last, reverse=True, limit=300)]
        self.s["checked"] += len(msgs)
        files = [m for m in msgs if is_file_post(m)]
        new = self.fresh(files, self.s["titles"].get(str(cid)) or "")
        self.s["dupes"] += sum(1 for v in new.values() if not v)
        protected = bool(getattr(ent, "noforwards", False))
        done_to = last
        n_new = 0
        try:
            for m in msgs:
                if new.get(m.id):
                    await self.send(ent, cid, m, "live", protected)
                    n_new += 1
                done_to = max(done_to, m.id)
            done_to = max(done_to, top)
        finally:
            self.s["last"][str(cid)] = done_to
        return n_new

    async def history(self, ent, cid, src, quota, scan_left):
        """Walk older posts newest → oldest from the cursor. Returns (sent, scanned)."""
        bf = src.get("bf") or {}
        cur = int(bf.get("cur") or 0) or self.live_first.get(cid) or (int(self.s["last"].get(str(cid)) or src.get("last") or 0) + 1)
        rec = self.s["bf"].setdefault(str(cid), {"cur": cur, "scanned": 0, "files": 0, "dup": 0})
        protected = bool(getattr(ent, "noforwards", False))
        sent = 0
        while scan_left > 0:
            chunk = await self.client.get_messages(ent, limit=min(100, scan_left), offset_id=cur)
            chunk = sorted([m for m in chunk if m.id < cur], key=lambda m: -m.id)
            if not chunk:
                rec["done"] = True
                break
            files = [m for m in chunk if is_file_post(m)]
            new = self.fresh(files, self.s["titles"].get(str(cid)) or "")
            for m in chunk:
                if new.get(m.id):
                    if sent >= quota or self.budget <= 0:
                        return sent, rec
                    await self.send(ent, cid, m, "bf", protected)
                    sent += 1
                elif m.id in new:
                    rec["dup"] += 1
                    self.s["dupes"] += 1
                if m.id in new:
                    rec["files"] += 1
                rec["scanned"] += 1
                scan_left -= 1
                cur = m.id
                rec["cur"] = cur
                if getattr(m, "date", None):
                    rec["date"] = int(m.date.timestamp())
            if scan_left <= 0:
                break
        return sent, rec


async def main_async(job):
    from telethon import TelegramClient, errors, utils
    from telethon.sessions import StringSession
    bot = Bot(job)
    summary = {"kind": "final", "last": {}, "errors": {}, "ok_ids": [], "titles": {}, "sent": 0, "checked": 0,
               "dupes": 0, "dup_items": [], "bf": {}, "bf_sent": 0, "flood": 0}
    client = TelegramClient(StringSession(job["session"]), int(job["api_id"]), job["api_hash"],
                            device_model=job.get("device") or "Filesbull Userbot", system_version="filesbull",
                            app_version="1.0", lang_code="en", system_lang_code="en", flood_sleep_threshold=0)
    tmp = tempfile.mkdtemp(prefix="userbot-")
    run = Run(client, bot, job, summary)
    run.tmp = tmp
    try:
        await client.connect()
        if not await client.is_user_authorized():
            summary["fatal"] = "revoked"
            print("session is not authorized any more (logged out / revoked)")
            return summary
        me = await client.get_me()
        summary["me"] = {"username": me.username or "", "name": " ".join(x for x in (me.first_name, me.last_name) if x)}
        PASS_UP = (Stop, errors.FloodWaitError, errors.AuthKeyUnregisteredError, errors.SessionRevokedError,
                   errors.UserDeactivatedError, errors.UserDeactivatedBanError, errors.AuthKeyDuplicatedError,
                   errors.ChatAdminRequiredError, errors.ChatWriteForbiddenError)
        ents = {}
        async for d in client.iter_dialogs(limit=None):
            ents[utils.get_peer_id(d.entity)] = d.entity
        try:
            # 1) setup: the Inbox + the Inbox alert chat (created here when the bot has none yet)
            setup = {}
            for kind, title in (job.get("create") or {}).items():
                about = ("filesbull: channels ki raw files. Bot inhe naam + category deke Backup me settle karta hai."
                         if kind == "inbox" else "filesbull: Inbox ke alerts (har run ka hisaab).")
                try:
                    ent = await create_channel(client, title, about, job.get("bot") or "")
                    pid = utils.get_peer_id(ent)
                    ents[pid] = ent
                    setup[kind] = {"id": pid, "title": title}
                    print(f"created the {kind} channel")
                except errors.FloodWaitError as e:
                    summary["flood"] = max(summary["flood"], int(e.seconds))
                    setup[kind] = {"error": f"Telegram ne {int(e.seconds)} sec rukne bola"}
                except Exception as e:
                    setup[kind] = {"error": ERR_WORDS.get(type(e).__name__, type(e).__name__)}
                    print(f"{kind} channel not created:", type(e).__name__)
            inbox_id = int(job.get("inbox") or 0)
            if setup:
                res = bot.post("/api/userbot-done", dict({"kind": "setup"}, **setup))
                inbox_id = int(res.get("inbox") or inbox_id or 0)
            run.inbox_id = inbox_id
            run.inbox = ents.get(inbox_id) if inbox_id else None
            if inbox_id and run.inbox is None:
                try:
                    run.inbox = await client.get_entity(inbox_id)
                except Exception:
                    run.inbox = None
            # 2) join new source channels (max 3 per run)
            want = {int(s["id"]): s for s in job["sources"]}
            joins = int(job.get("joins") or 3)
            jr = {"kind": "join", "joined": [], "need": [], "failed": {}}
            todo = [("", "", lk, None) for lk in (job.get("invites") or [])]
            todo += [(s.get("username") or "", s.get("title") or "", s.get("link") or "", cid) for cid, s in want.items() if cid not in ents]
            for username, title, link, cid in todo:
                if not username and not invite_hash(link):
                    jr["need"].append({"id": cid, "title": title})
                    continue
                if joins <= 0:
                    break
                joins -= 1
                try:
                    ent = await join_one(client, username, link)
                    pid = utils.get_peer_id(ent)
                    ents[pid] = ent
                    jr["joined"].append({"id": pid, "title": getattr(ent, "title", "") or title, "username": getattr(ent, "username", "") or "",
                                         "link": link, "new_source": cid is None})
                    print("joined 1 channel")
                except errors.FloodWaitError as e:
                    summary["flood"] = max(summary["flood"], int(e.seconds))
                    run.stop = f"Telegram ne {int(e.seconds)} sec rukne bola, baaki agli baar"
                    break
                except Exception as e:
                    if cid is not None:
                        jr["failed"][str(cid)] = type(e).__name__
                    print("join failed:", type(e).__name__)
                await asyncio.sleep(random.uniform(float(run.gap[0]), float(run.gap[1])))
            if jr["joined"] or jr["need"] or jr["failed"]:
                bot.post("/api/userbot-done", jr)
            if not inbox_id:
                summary["stop"] = "Inbox channel set nahi — koi file forward nahi hui (Backup me sirf settled files)"
                print("no Inbox channel: nothing is forwarded")
                return summary
            if run.inbox is None:
                summary["fatal"] = "aapka account Inbox channel me nahi hai (ya post ka haq nahi)"
                return summary
            # 3) live posts first (all channels), then history with what is left of the shared cap
            order = []
            for cid, src in want.items():
                ent = ents.get(cid)
                if ent is None:
                    summary["errors"][str(cid)] = "aapka account is channel me joined nahi"
                    continue
                summary["titles"][str(cid)] = getattr(ent, "title", "") or src.get("title") or ""
                order.append((cid, src, ent))
            if not run.stop:
                for cid, src, ent in order:
                    try:
                        n = await run.live(ent, cid, src)
                        summary["ok_ids"].append(cid)
                        print(f"live: {n} new file(s)")
                    except errors.ChannelPrivateError:
                        summary["errors"][str(cid)] = "channel private/ban"
                    except PASS_UP:
                        raise
                    except Exception as e:
                        summary["errors"][str(cid)] = type(e).__name__ + (": " + str(e)[:80] if isinstance(e, RuntimeError) else "")
                        print("channel failed:", type(e).__name__)
            if not run.stop and not job.get("bf_paused"):
                active = [(cid, src, ent) for cid, src, ent in order if (src.get("bf") or {}).get("on")]
                scan = int(job.get("bf_scan") or 600)
                for i, (cid, src, ent) in enumerate(active):
                    if run.budget <= 0:
                        run.stop = run.stop or f"limit {job.get('max')} files/run, purani files agli baar"
                        break
                    quota = max(1, run.budget // max(1, len(active) - i))
                    try:
                        sent, rec = await run.history(ent, cid, src, quota, scan)
                        print(f"history: {sent} file(s)" + (", channel finished" if rec.get("done") else ""))
                    except errors.ChannelPrivateError:
                        summary["errors"][str(cid)] = "channel private/ban"
                    except PASS_UP:
                        raise
                    except Exception as e:
                        summary["errors"][str(cid)] = "history: " + type(e).__name__ + (": " + str(e)[:80] if isinstance(e, RuntimeError) else "")
                        print("history failed:", type(e).__name__)
        except Stop:
            pass
        except errors.FloodWaitError as e:
            summary["flood"] = max(summary["flood"], int(e.seconds))
            run.stop = f"Telegram ne {int(e.seconds)} sec rukne bola, baaki agli baar"
            print(f"FloodWait {int(e.seconds)} s: stopping, the rest goes next run")
        except (errors.AuthKeyUnregisteredError, errors.SessionRevokedError, errors.UserDeactivatedError,
                errors.UserDeactivatedBanError, errors.AuthKeyDuplicatedError) as e:
            summary["fatal"] = "revoked"
            print("session revoked:", type(e).__name__)
        except (errors.ChatAdminRequiredError, errors.ChatWriteForbiddenError):
            summary["fatal"] = "aapka account Inbox channel me post nahi kar sakta (admin banao)"
        summary["stop"] = run.stop
        return summary
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            await client.disconnect()
        except Exception:
            pass


def settle_pump(bot, max_calls=60, max_secs=480):
    """Inbox → Backup: the bot settles one understood file per call (gently, 1 s apart)."""
    t0 = time.time()
    done = {"settled": 0, "big": 0, "review_fixed": 0}
    for _ in range(max_calls):
        if time.time() - t0 > max_secs:
            break
        r = bot.post("/api/userbot-settle", {})
        did = str((r or {}).get("did") or "none")
        if did in done:
            done[did] += 1
        if did in ("none", "review_none", "review_still", "no_backup", "error", "failed") and not r.get("left"):
            break
        time.sleep(1.0)
    print(f"settle: {done['settled']} into Backup, {done['big']} big file(s) queued, {done['review_fixed']} review fixed")
    return done


def started_by():
    try:
        ev = json.load(open(os.environ.get("GITHUB_EVENT_PATH") or ""))
        return str((ev.get("inputs") or {}).get("why") or "")
    except Exception:
        return ""


def chain_next(t0, why="chain"):
    """GitHub's 10-min schedule is best-effort (on a new repo it may not run at all): each finished run starts the next
    one itself ~10 min after it began (workflow_dispatch with the run's own GITHUB_TOKEN). The bot refuses a chained
    job that comes < 8 min after the last one, so runs never pile up."""
    tok, repo = os.environ.get("GH_CHAIN_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not tok or not repo:
        return "no_token"
    wait = max(0.0, min(600 - (time.time() - t0), 600))
    print(f"next run in {int(wait)} s")
    time.sleep(wait)
    st, _ = http(f"https://api.github.com/repos/{repo}/actions/workflows/userbot-forward.yml/dispatches",
                 {"ref": "main", "inputs": {"why": why}},
                 headers={"Authorization": "Bearer " + tok, "Accept": "application/vnd.github+json"})
    print("next run started" if st in (200, 204) else f"next run not started (HTTP {st})")
    return st


def main():
    t0 = time.time()
    status, job = http(BOT_URL + "/api/userbot-job", {"oidc": oidc_token(), "why": started_by()})
    if status != 200 or not job.get("ok"):
        sys.exit(f"bot said HTTP {status}: {job.get('error')} {job.get('why', '')}")
    for k in ("session", "api_hash", "k"):
        mask(str(job.get(k) or ""))
    state = job.get("state")
    if state != "ready":
        print({"not_logged_in": "userbot: not logged in (owner hasn't logged in yet) — nothing to do",
               "paused": "userbot: paused by the owner — nothing to do",
               "missing_secrets": "userbot: TG_API_ID / TG_API_HASH missing in Vercel — nothing to do",
               "no_backup": "userbot: no Backup channel set — nothing to do",
               "too_soon": "userbot: another run started < 8 min ago — it keeps the chain going"}.get(state, f"userbot: {state}"))
        return
    bot = Bot(job)
    if not job.get("sources") and not job.get("create") and not job.get("invites"):
        print("userbot: no source channel without bot admin — nothing to forward")
        summary = {"kind": "final", "sent": 0, "checked": 0}
    else:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "telethon==1.42.0", "cryptg"], check=True)
        try:
            summary = asyncio.run(asyncio.wait_for(main_async(job), timeout=25 * 60))
        except Exception as e:
            print("run failed:", type(e).__name__)
            summary = {"kind": "final", "error": f"run fail: {type(e).__name__}"}
    if summary.get("fatal") != "revoked":
        settle_pump(bot)
    out = {k: v for k, v in summary.items()}
    bot.post("/api/userbot-done", out)
    print(json.dumps({"sent": summary.get("sent"), "history": summary.get("bf_sent"), "checked": summary.get("checked"),
                      "dupes": summary.get("dupes"), "stop": summary.get("stop"), "errors": len(summary.get("errors") or {}),
                      "fatal": bool(summary.get("fatal"))}))
    if summary.get("fatal") != "revoked":
        chain_next(t0)


if __name__ == "__main__":
    main()
