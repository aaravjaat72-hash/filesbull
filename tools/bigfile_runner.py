#!/usr/bin/env python3
"""filesbull big-file rename runner (GitHub Action .github/workflows/bigfile-rename.yml).

Files over 20 MB can't be downloaded by the Bot HTTP API, so their FILE name can't change there. Here the same bot logs
in over MTProto (Telethon, in-memory session) where bots may download/upload up to 2 GB:
  1. get GitHub's OIDC token for this run and ask the bot for work: POST {BOT_URL}/api/bigfile-job
     (the bot checks the token is from this repo's bigfile workflow on main, then returns the jobs + credentials)
  2. per job: download the file from its Backup channel post (or source post), upload it as a document named
     "<clean name>.<ext>" with the caption "📄 name / 🗂️ subject" to the Backup channel, delete the temp file
  3. report to POST {BOT_URL}/api/bigfile-done, signed HMAC-SHA256 with the one-job key the bot gave.
Also 🌐 big web links (kind "web", > 49 MB): download the URL (public addresses only) and upload it into the 📥 Inbox.
Secrets are never printed (and are masked in the log).
"""
import asyncio, hashlib, hmac, json, os, shutil, subprocess, sys, tempfile, time, urllib.request, urllib.error

BOT_URL = os.environ.get("BOT_URL", "https://filesbull.vercel.app").rstrip("/")
AUD = "filesbull-bigfile"


def mask(v):
    if v:
        print(f"::add-mask::{v}", flush=True)


def http(url, payload=None, headers=None, timeout=60):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET",
                                 headers={"Content-Type": "application/json", "User-Agent": "filesbull-bigfile/1.0", **(headers or {})})
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


def sign_and_report(job, result):
    body = json.dumps(dict(result, j=job["j"]), ensure_ascii=False, separators=(",", ":")).encode()
    sig = hmac.new(job["k"].encode(), body, hashlib.sha256).hexdigest()
    for i in range(3):
        req = urllib.request.Request(BOT_URL + "/api/bigfile-done", data=body, method="POST",
                                     headers={"Content-Type": "application/json", "X-Filesbull-Signature": sig,
                                              "User-Agent": "filesbull-bigfile/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                out = json.loads(r.read().decode() or "{}")
                print("report:", json.dumps({k: out.get(k) for k in ("ok", "state", "file_name", "retry")}, ensure_ascii=False))
                return out
        except urllib.error.HTTPError as e:
            print("report HTTP", e.code)
            if e.code < 500:
                return {}
        except Exception as e:
            print("report error", type(e).__name__)
        time.sleep(5 * (i + 1))
    return {}


async def resolve(client, chat_id):
    """Bots can't look channels up by name/history; channels.getChannels with access_hash=0 works for bots."""
    from telethon import functions, types, utils
    real_id, kind = utils.resolve_id(int(chat_id))
    if kind is types.PeerChannel:
        res = await client(functions.channels.GetChannelsRequest([types.InputChannel(real_id, 0)]))
        return res.chats[0]
    return await client.get_input_entity(int(chat_id))


def _ip_ok(ip):
    import ipaddress
    try:
        a = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    if getattr(a, "ipv4_mapped", None):
        a = a.ipv4_mapped
    return a.is_global and not (a.is_private or a.is_loopback or a.is_link_local or a.is_multicast or a.is_reserved)


def web_download(job, path):
    """🌐 big web link (bot gave the already-resolved file URL): stream it to disk. Every redirect hop must resolve to
    public addresses only; size capped at job['max']."""
    import socket, urllib.parse
    url, cap = job["url"], int(job.get("max") or 1900 * 1048576)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(NoRedirect)
    for _ in range(8):
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise RuntimeError("link http/https nahi")
        ips = [i[4][0] for i in socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80), 0, socket.SOCK_STREAM)]
        if not ips or not all(_ip_ok(ip) for ip in ips):
            raise RuntimeError("private/local address")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36", "Accept": "*/*"})
        try:
            r = opener.open(req, timeout=60)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                url = urllib.parse.urljoin(url, e.headers["Location"])
                continue
            raise RuntimeError(f"website ne HTTP {e.code} diya")
        break
    else:
        raise RuntimeError("bahut redirect")
    ctype = (r.headers.get("Content-Type") or "").split(";")[0].lower()
    if ctype in ("text/html", "application/xhtml+xml"):
        raise RuntimeError("link file nahi, webpage hai")
    got = 0
    t0 = time.time()
    with open(path, "wb") as f:
        while True:
            piece = r.read(1 << 20)
            if not piece:
                break
            got += len(piece)
            if got > cap:
                raise RuntimeError("file limit se badi")
            f.write(piece)
    if got == 0:
        raise RuntimeError("khali file")
    with open(path, "rb") as f:
        head = f.read(600).lstrip().lower()
    if head.startswith((b"<!doctype html", b"<html")):
        raise RuntimeError("link file nahi, webpage hai")
    print(f"web download {got / 1048576:.1f} MB in {time.time() - t0:.1f}s")
    return got


async def do_web_job(client, job, tmp):
    from telethon.tl.types import DocumentAttributeFilename
    name = job["file_name"]
    work = os.path.join(tmp, job["j"])
    os.makedirs(work, exist_ok=True)
    path = os.path.join(work, "dl.bin")
    size = await asyncio.get_running_loop().run_in_executor(None, web_download, job, path)
    target = await resolve(client, job["to"])
    t1 = time.time()
    sent = await client.send_file(target, path, caption=job.get("caption") or "", force_document=True,
                                  attributes=[DocumentAttributeFilename(name)], parse_mode=None)
    print(f"uploaded in {time.time() - t1:.1f}s")
    try:
        os.remove(path)
    except OSError:
        pass
    return {"ok": True, "chat_id": int(job["to"]), "message_id": int(sent.id), "file_name": name, "size": size}


async def do_job(client, job, tmp):
    if job.get("kind") == "web":
        return await do_web_job(client, job, tmp)
    from telethon.tl.types import DocumentAttributeFilename
    src_msg = None
    last_err = "file post nahi mila"
    for chat, mid in job.get("from") or []:
        try:
            ent = await resolve(client, chat)
            m = await client.get_messages(ent, ids=int(mid))
            if m and m.media and m.file:
                src_msg = m
                break
            last_err = "post me file nahi hai"
        except Exception as e:
            last_err = f"post nahi khula: {type(e).__name__}"
    if not src_msg:
        raise RuntimeError(last_err)
    name = job["file_name"]
    work = os.path.join(tmp, job["j"])
    os.makedirs(work, exist_ok=True)
    path = os.path.join(work, name)
    t0 = time.time()
    got = await client.download_media(src_msg, file=path)
    if not got or not os.path.exists(path) or os.path.getsize(path) == 0:
        raise RuntimeError("download khali aaya")
    size = os.path.getsize(path)
    print(f"downloaded {size / 1048576:.1f} MB in {time.time() - t0:.1f}s")
    target = await resolve(client, job["to"])
    t1 = time.time()
    sent = await client.send_file(target, path, caption=job.get("caption") or "", force_document=True,
                                  attributes=[DocumentAttributeFilename(name)], parse_mode=None)
    print(f"uploaded in {time.time() - t1:.1f}s")
    try:
        os.remove(path)
    except OSError:
        pass
    fname = None
    try:
        fname = sent.file.name
    except Exception:
        pass
    return {"ok": True, "chat_id": int(job["to"]), "message_id": int(sent.id), "file_name": fname or name, "size": size}


async def main_async(jobs, creds):
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    client = TelegramClient(StringSession(), int(creds["api_id"]), creds["api_hash"])
    await client.start(bot_token=creds["bot_token"])
    tmp = tempfile.mkdtemp(prefix="bigfile-")
    try:
        for job in jobs:
            print(f"job {job['j']} try {job.get('try')}: {job['file_name']} ({job.get('size', 0) / 1048576:.1f} MB)")
            try:
                res = await asyncio.wait_for(do_job(client, job, tmp), timeout=20 * 60)
            except Exception as e:
                msg = str(e) or type(e).__name__
                for s in creds.values():
                    msg = msg.replace(str(s), "***")
                print("job failed:", type(e).__name__)
                res = {"ok": False, "error": msg[:200]}
            sign_and_report(job, res)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        await client.disconnect()


def main():
    env = {"bot_token": os.environ.get("BOT_TOKEN", "").strip(), "api_id": os.environ.get("TG_API_ID", "").strip(),
           "api_hash": os.environ.get("TG_API_HASH", "").strip()}
    for v in env.values():
        mask(v)
    status, out = http(BOT_URL + "/api/bigfile-job", {"oidc": oidc_token(), "job": os.environ.get("JOB", ""),
                                                      "has": {"bot_token": bool(env["bot_token"]),
                                                              "api": bool(env["api_id"] and env["api_hash"])}})
    if status != 200 or not out.get("ok"):
        sys.exit(f"bot said HTTP {status}: {out.get('error')} {out.get('why', '')}")
    jobs = out.get("jobs") or []
    print(f"{len(jobs)} job(s)")
    if not jobs:
        return
    creds = dict(env)
    for k, v in (out.get("creds") or {}).items():
        mask(str(v))
        if not creds.get(k):
            creds[k] = str(v)
    missing = [k for k in ("bot_token", "api_id", "api_hash") if not creds.get(k)]
    if missing:
        for job in jobs:
            sign_and_report(job, {"ok": False, "error": "TG_API_ID / TG_API_HASH nahi mile (Vercel env)"})
        sys.exit("missing credentials: " + ", ".join(missing))
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "telethon==1.*", "cryptg"], check=True)
    asyncio.run(main_async(jobs, creds))


if __name__ == "__main__":
    main()
