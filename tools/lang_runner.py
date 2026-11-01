#!/usr/bin/env python3
"""filesbull 🏷️ auto-reader OCR runner (GitHub Action .github/workflows/langread.yml).

New files whose language the bot could not tell from the name, the caption or the PDF's text layer are read here:
  1. get GitHub's OIDC token for this run and ask the bot for work: POST {BOT_URL}/api/langread-job
     (the bot checks the token is from this repo's langread workflow on main, then returns the files + its bot token)
  2. per file (≤ 20 MB, Bot API getFile): the text layer of up to 10 pages; when that is too little, OCR of 2 pages with
     tesseract (Hindi + English). Only the text goes back — the bot decides the language itself.
  3. report to POST {BOT_URL}/api/langread-done, signed HMAC-SHA256 with the one-batch key the bot gave.
Nothing is uploaded, renamed in Telegram or deleted. Secrets are never printed (and are masked in the log).
"""
import hashlib, hmac, io, json, os, re, subprocess, sys, tempfile, time, urllib.request, urllib.error, zipfile

LEGACY_REPO, LEGACY_URL = "aaravjaat72-hash/filesbull", "https://filesbull.vercel.app"
AUD = "filesbull-langread"


def resolve_bot_url():
    for n in ("BOT_URL_INPUT", "BOT_URL"):
        v = (os.environ.get(n) or "").strip().rstrip("/")
        if v:
            v = v if v.startswith("http") else "https://" + v
            if v.startswith("https://"):
                return v
    repo = (os.environ.get("GITHUB_REPOSITORY") or LEGACY_REPO).strip().lower()
    return LEGACY_URL if repo == LEGACY_REPO else ""


BOT_URL = resolve_bot_url()


def mask(v):
    if v:
        print(f"::add-mask::{v}", flush=True)


def http(url, payload=None, headers=None, timeout=60, raw=None):
    data = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET",
                                 headers={"Content-Type": "application/json", "User-Agent": "filesbull-langread/1.0", **(headers or {})})
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


def letters(s):
    return len(re.findall(r"[A-Za-z\u0900-\u097F]", s or ""))


def tesseract(png):
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(png)
        p = f.name
    try:
        r = subprocess.run(["timeout", "-k", "5", "60", "tesseract", p, "-", "-l", "hin+eng", "--psm", "3"],
                           capture_output=True, text=True)
        return r.stdout
    finally:
        os.unlink(p)


def read(data, name, mime):
    """(text, how) — text layer first, OCR only when the first pages carry almost no text."""
    import pymupdf
    if data[:2] == b"PK":
        z = zipfile.ZipFile(io.BytesIO(data))
        parts = sorted([n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)],
                       key=lambda n: int(re.findall(r"\d+", n)[-1]))[:15] or [n for n in z.namelist() if n == "word/document.xml"]
        return re.sub(r"\s+", " ", " ".join(re.sub(r"<[^>]+>", " ", z.read(n).decode("utf8", "ignore")) for n in parts))[:6000], "text"
    if str(mime).startswith("image/"):
        return tesseract(data)[:6000], "ocr"
    if str(mime).startswith("text/") or name.lower().endswith(".txt"):
        return data[:12000].decode("utf8", "ignore"), "text"
    doc = pymupdf.open(stream=data, filetype="pdf")
    lines = []
    for i in range(min(doc.page_count, 10)):
        lines += [l.strip() for l in (doc[i].get_text() or "").splitlines() if l.strip()]
    seen = {}
    for l in lines:
        seen[l] = seen.get(l, 0) + 1
    txt = "\n".join(l for l in lines if seen[l] < 3)          # drop channel stamps repeated on every page
    if letters(txt) < 150 and letters(" ".join(lines)) >= 400 and len(seen) >= 3:
        txt = "\n".join(lines)                                   # same few lines on every page = still real text
    if letters(txt) >= 150:
        return txt[:6000], "text"
    n = doc.page_count
    pages = sorted({p for p in (min(1, n - 1), min(3, n - 1), n // 2) if p >= 0})[:2]
    ocr = ""
    for p in pages:
        pix = doc[p].get_pixmap(dpi=150, colorspace=pymupdf.csGRAY)
        ocr += tesseract(pix.tobytes("png")) + "\n"
    return (txt + "\n" + ocr)[:6000], "ocr"


def main():
    if not BOT_URL:
        print("No bot address (set the repo variable BOT_URL). Nothing to do.")
        return
    has_tok = bool(os.environ.get("BOT_TOKEN"))
    st, job = http(BOT_URL + "/api/langread-job", {"oidc": oidc_token(), "has": {"bot_token": has_tok}, "max": 25}, timeout=90)
    if st != 200 or not job.get("ok"):
        sys.exit(f"bot said {st}: {json.dumps({k: job.get(k) for k in ('error', 'why')})}")
    items = job.get("items") or []
    print(f"{len(items)} file(s) to read")
    if not items:
        return
    token = os.environ.get("BOT_TOKEN") or (job.get("creds") or {}).get("bot_token") or ""
    mask(token)
    mask(job.get("k"))
    subprocess.run("sudo apt-get -qq update >/dev/null && sudo apt-get -qq install -y tesseract-ocr tesseract-ocr-hin >/dev/null",
                   shell=True, check=False)
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "pymupdf"], check=False)
    out = []
    for it in items:
        rec = {"id": it["id"]}
        t0 = time.time()
        try:
            st, gf = http(f"https://api.telegram.org/bot{token}/getFile?file_id={urllib.parse.quote(str(it['file_id']))}", timeout=60)
            if not gf.get("ok"):
                raise RuntimeError(str(gf.get("description") or st)[:80])
            with urllib.request.urlopen(f"https://api.telegram.org/file/bot{token}/{gf['result']['file_path']}", timeout=120) as r:
                data = r.read(21 * 1024 * 1024)
            rec["txt"], rec["how"] = read(data, str(it.get("file_name") or ""), str(it.get("mime") or ""))
        except Exception as exc:
            rec["err"] = (type(exc).__name__ + ": " + str(exc)[:120]).replace(token or "\x00", "***")
        print(f"#{it['id']}: {rec.get('how') or 'error'} · {len(rec.get('txt') or '')} chars · {time.time() - t0:.1f}s")
        out.append(rec)
    body = json.dumps({"batch": job["batch"], "items": out}, ensure_ascii=False, separators=(",", ":")).encode()
    sig = hmac.new(job["k"].encode(), body, hashlib.sha256).hexdigest()
    for i in range(3):
        st, res = http(BOT_URL + "/api/langread-done", raw=body, headers={"X-Filesbull-Signature": sig}, timeout=90)
        print("report:", st, json.dumps({k: res.get(k) for k in ("ok", "found", "unknown", "error")}))
        if st < 500:
            break
        time.sleep(10)


if __name__ == "__main__":
    import urllib.parse
    main()
