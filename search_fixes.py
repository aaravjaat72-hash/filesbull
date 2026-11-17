"""Search and database-availability fixes.

Installed once by app.py. Function globals intentionally bind to the main bot
namespace so existing settings, per-request buffers and cache invalidation are
shared. No database schema, credentials or Telegram delivery changes.
"""
from typing import Any, Dict, List, Optional
from types import FunctionType

class DatabaseUnavailable(RuntimeError):
    """The catalog is unavailable, not empty."""

_DB_RESTRICTION = {"until": 0.0, "reason": ""}
DB_UNAVAILABLE_TEXT = (
    "⚠️ Abhi file database unavailable hai (provider ki free usage limit). "
    "Isliye search nahi chal rahi; iska matlab file missing nahi hai. "
    "Database access restore hone par dobara search karein."
)

def _db_check_available() -> None:
    if time.monotonic() < _DB_RESTRICTION['until']:
        raise DatabaseUnavailable(_DB_RESTRICTION['reason'])

def _db_note_error(exc: Exception) -> None:
    if re.search('HTTP 402\\b', str(exc)):
        reason = 'Database restricted by provider (HTTP 402); check Supabase usage and billing cycle.'
        _DB_RESTRICTION.update(until=time.monotonic() + 60.0, reason=reason)
        raise DatabaseUnavailable(reason) from None

def supabase(path: str, method: str='GET', payload: Any=None, query: str='') -> Any:
    _db_check_available()
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError('Supabase environment variables are not configured')
    url = f'{SUPABASE_URL}/rest/v1/{path}{query}'
    if method != 'GET' and (path == 'books' or (path == 'bot_settings' and 'catmap' in json.dumps(payload or query, default=str))):
        books_changed()
    if path == 'bot_settings' and method != 'GET':
        if method == 'POST' and isinstance(payload, dict) and payload.get('key'):
            settings_forget(f"key=eq.{urllib.parse.quote(str(payload['key']))}")
        else:
            settings_forget(query)
    if _DIAG['dry'] and method != 'GET':
        _DIAG['sent'].append({'ms': int((time.monotonic() - _DIAG['t0']) * 1000), 'm': f'db:{method} {path}'})
        return []
    headers = {'apikey': SUPABASE_KEY, 'Authorization': f'Bearer {SUPABASE_KEY}', 'Prefer': 'return=representation'}
    if method == 'POST' and 'on_conflict=' in query:
        headers['Prefer'] = 'return=representation,resolution=merge-duplicates'
    if payload is not None and method != 'GET':
        payload = _no_nul(payload)
    try:
        return http_json(url, method, payload, headers)
    except Exception as exc:
        _db_note_error(exc)
        raise

def count_rows(table: str, query: str='', col: str='id') -> int:
    """Exact row count (Supabase returns at most 1000 rows per read, so don't count a list)."""
    _db_check_available()
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError('Supabase environment variables are not configured')
    url = f'{SUPABASE_URL}/rest/v1/{table}?select={col}{query}&limit=1'
    t_call = time.monotonic()
    try:
        (_, hdrs, _) = http_raw(url, 'GET', None, {'apikey': SUPABASE_KEY, 'Authorization': f'Bearer {SUPABASE_KEY}', 'Prefer': 'count=exact'}, 15)
        total = (hdrs.get('Content-Range') or '').split('/')[-1]
    except urllib.error.HTTPError as exc:
        _db_note_error(RuntimeError(f'HTTP {exc.code} counting database rows'))
        raise RuntimeError(f'HTTP {exc.code} counting {table}') from None
    finally:
        if _DIAG['on']:
            _DIAG['calls'].append([int((t_call - _DIAG['t0']) * 1000), int((time.monotonic() - t_call) * 1000), f'db:COUNT {table}'])
    return int(total) if total.isdigit() else 0

def health() -> Any:
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            supabase('bot_settings', 'GET', query='?select=key&limit=1')
        except DatabaseUnavailable:
            return jsonify({'ok': False, 'service': 'telegram-book-bot', 'status': {'database': 'restricted: HTTP 402 (provider usage quota)', 'database_state': 'restricted', 'retry_after_s': 60, 'build': os.environ.get('VERCEL_GIT_COMMIT_SHA', '')[:7] or 'local', 'action': 'Check Supabase usage/billing cycle. No files have been deleted.'}})
        except Exception:
            pass
    registered = None
    heal: Dict[str, Any] = {}
    t_health = time.monotonic()
    _REQ.clear()
    try:
        prefetch_settings(['webhook_fp', 'commands_fp', 'ainame_queue', 'langq', 'langq_kick', 'logfill_state', BIG_QUEUE_KEY, ROUTER_KEY, 'ai_keys', 'webq', 'webbigq', 'upl:bucket', 'upl:sweep', 'upl:pw', 'webbig_kick', 'gh:botvar', 'diag_sha'])
    except Exception:
        pass
    if BOT_TOKEN:
        try:
            heal = ensure_webhook(force=request.args.get('restart') == '1')
            registered = True
        except Exception as exc:
            log_error('setup')
            registered = False
            heal = {'error': type(exc).__name__ + ': ' + str(exc)[:120].replace(BOT_TOKEN, '***')}
    _REQ_T0[0] = time.monotonic()
    try:
        names = process_name_queue(budget=6.0)
    except Exception as exc:
        log_error('name_queue')
        names = {'error': type(exc).__name__}
    try:
        if req_elapsed() < 5.0:
            langs = process_lang_queue(budget=max(1.0, min(4.0, 8.0 - req_elapsed())))
        else:
            langs = {'skipped': 'time'}
    except Exception as exc:
        log_error('lang_queue')
        langs = {'error': type(exc).__name__}
    try:
        flush_logs()
    except Exception:
        log_error('flush_logs')
    try:
        lf = logfill_state()
        if logfill_active(lf) and time.time() - float(lf.get('last_ts') or 0) > 60:
            lf = logfill_step(budget=max(2.5, 8.0 - req_elapsed()))
            logfill_notify_done(lf)
            if logfill_active(lf) and (not lf.get('_busy')):
                logfill_kick()
    except Exception:
        log_error('logfill keepalive')
    try:
        web_keepalive()
        try:
            bc_keepalive()
        except Exception:
            log_error('bc keepalive')
    except Exception:
        log_error('web keepalive')
    try:
        if GH_DISPATCH_TOKEN and SUPABASE_URL and SUPABASE_KEY and (req_elapsed() < 6.0):
            gh_sync_bot_url()
    except Exception:
        log_error('gh bot_url var')
    _REQ.clear()
    if request.args.get('full') == '1':
        status = setup_status()
        try:
            status['category_column'] = 'ok' if has_category_col() else 'missing (catmap fallback)'
        except Exception:
            pass
    else:
        status = light_status()
    status['webhook_registered'] = registered
    status['self_heal'] = heal
    status['ai_name_retry'] = names
    try:
        status['auto_reader'] = dict(langq_status(), last=langs)
    except Exception:
        status['auto_reader'] = {'last': langs}
    try:
        status['animations'] = dict({'mode': 'on' if fx_on() else 'off'}, **FX_STATS)
    except Exception:
        pass
    status['ms'] = int((time.monotonic() - t_health) * 1000)
    return jsonify({'ok': True, 'service': 'telegram-book-bot', 'status': status})

def fuzzy_rows(keywords: List[str]) -> List[Dict[str, Any]]:
    """Spelling-tolerant match on file names ("lusent" → Lucent): every keyword ≥4 letters must closely match a word
    of the title / original file name (short ones exactly). Cheap: one query, no AI."""
    import difflib
    kws = [normalize(k) for k in keywords if normalize(k)]
    if not kws or not any((len(k) >= 4 for k in kws)):
        return []

    def load() -> List[Dict[str, Any]]:
        cols = 'id,title,file_name,caption,language,keywords,status,file_size,telegram_file_unique_id'
        return supabase('books', 'GET', query=f'?select={cols}&status=eq.indexed&order=id.desc&limit=3000')
    try:
        rows = view_cached('fuzzy_catalog', load)
    except DatabaseUnavailable:
        raise
    except Exception:
        return []
    out = []
    for r in rows:
        words = set(re.findall('[a-z0-9]+|[\\u0900-\\u097F]+', normalize(' '.join((str(r.get(c) or '') for c in ('title', 'file_name', 'caption'))))))
        if not words:
            continue
        ok = True
        for k in kws:
            g = set(ALIASES.get(k, {k})) | {k}
            if any((x in words for x in g)):
                continue
            if len(k) >= 4 and difflib.get_close_matches(k, list(words), n=1, cutoff=0.8):
                continue
            ok = False
            break
        if ok:
            out.append(r)
    return out[:30]

def webhook() -> Any:
    received = request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
    if not WEBHOOK_SECRET or not hmac.compare_digest(received, WEBHOOK_SECRET):
        return (jsonify({'ok': False, 'error': 'unauthorized'}), 401)
    update = request.get_json(silent=True) or {}
    uid_ = update.get('update_id')
    if uid_ is not None:
        if uid_ in _SEEN_UPDATES:
            return jsonify({'ok': True, 'dup': True})
        _SEEN_UPDATES.append(uid_)
        if len(_SEEN_UPDATES) > 500:
            del _SEEN_UPDATES[:250]
    _REQ.clear()
    _LOG_QUEUE.clear()
    _REQ_T0[0] = time.monotonic()
    reply_chat = ((update.get('message') or {}).get('chat') or {}).get('id') or (((update.get('callback_query') or {}).get('message') or {}).get('chat') or {}).get('id')
    try:
        if update.get('callback_query'):
            handle_callback(update['callback_query'])
        elif update.get('chat_member'):
            handle_chat_member(update['chat_member'])
        elif update.get('chat_join_request'):
            handle_join_request(update['chat_join_request'])
        elif update.get('channel_post'):
            handle_channel_post(update['channel_post'])
        elif update.get('message'):
            message = update['message']
            text = (message.get('text') or '').strip()
            handle_group_file(message)
            if file_info(message) and message.get('chat', {}).get('type') in {'group', 'supergroup'}:
                flush_request()
                return jsonify({'ok': True})
            command = command_name(text)
            is_private = message.get('chat', {}).get('type') == 'private'
            if command and command not in USER_COMMANDS and is_subadmin(message.get('from', {}).get('id')):
                if is_private:
                    kb_clear(message['chat']['id'], message.get('from', {}).get('id'))
                handle_admin_command(message)
            elif is_private or command in USER_COMMANDS:
                handle_private_message(message)
    except DatabaseUnavailable:
        if reply_chat and (not update.get('channel_post')):
            try:
                send_text(reply_chat, DB_UNAVAILABLE_TEXT)
            except Exception:
                log_error('database unavailable notification')
    except Exception:
        log_error('webhook')
        if reply_chat and (not update.get('channel_post')):
            try:
                hint = '' if SUPABASE_URL and SUPABASE_KEY else ' (database setup missing)'
                send_text(reply_chat, 'Abhi kuch gadbad hui' + hint + ', thodi der baad dobara try karein.')
            except Exception:
                pass
    try:
        flush_request()
    except Exception:
        log_error('webhook flush')
    return jsonify({'ok': True})

def row_lang(row: Dict[str, Any]) -> str:
    v = str(row.get('language') or '').lower()
    if v == 'mixed':
        return 'both'
    if v in ('hi', 'en', 'both', 'unk'):
        return v
    m = re.search('\\blang_(hi|en|both|unk)\\b', str(row.get('keywords') or ''))
    if m:
        return m.group(1)
    return lang_from_meta(row.get('title'), row.get('file_name'), row.get('caption')) or 'unk'

def _db_or(group: List[str]) -> str:
    parts = []
    for term in group:
        t = re.sub('[,()*\\"\\\\]', ' ', term).strip()
        if not t:
            continue
        pattern = '*'.join(t.split())
        for col in ('title', 'file_name', 'caption'):
            parts.append(f'{col}.ilike.*{pattern}*')
    return 'or(' + ','.join(parts) + ')'

def s2_fetch(groups: List[List[str]]) -> List[Dict[str, Any]]:
    """Candidates from the DB: every group must appear in title / file name / caption (and=(or(..),or(..))).
    The Python check after it is the real rule (whole words, first caption lines only)."""
    if not groups:
        return []
    cond = 'and=(' + ','.join((_db_or(g) for g in groups)) + ')'
    q = f'?select={S2_COLS}&{S2_STATUSES}&order=id.desc&limit=1000&' + urllib.parse.quote(cond, safe='(),.*=&')
    key = 'search:' + hashlib.sha256(q.encode()).hexdigest()[:24]

    def load() -> List[Dict[str, Any]]:
        rows = supabase('books', 'GET', query=q)
        if isinstance(rows, list) and len(rows) >= 1000:
            for off in (1000, 2000, 3000, 4000):
                page = supabase('books', 'GET', query=q + f'&offset={off}')
                if not isinstance(page, list):
                    raise RuntimeError('Invalid catalog response')
                rows += page
                if len(page) < 1000:
                    break
        return rows if isinstance(rows, list) else []
    if key not in _VIEW_CACHE and len(_VIEW_CACHE) >= 8:
        _VIEW_CACHE.pop(next(iter(_VIEW_CACHE)))
    return [dict(r) for r in view_cached(key, load)]

def s2_search(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = s2_fetch(spec['req'])
    scored = []
    for r in rows:
        if str(r.get('status') or '') == 'removed':
            continue
        s = s2_score(r, spec)
        if s is not None:
            scored.append((s, r))
    scored.sort(key=lambda x: (-x[0], len(str(x[1].get('title') or '')), str(x[1].get('title') or '')))
    (out, seen_u) = ([], set())
    for (_, r) in scored:
        u_ = str(r.get('telegram_file_unique_id') or '')
        if u_ and u_ in seen_u:
            continue
        if u_:
            seen_u.add(u_)
        (r['_lang'], r['_kind']) = (row_lang(r), row_kind(r))
        out.append(r)
    return out

def s2_close_options(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Nothing matched every word: files matching all but the least specific word, or a close spelling of the
    title words — shown ONLY under a clear 'pura match nahi mila' line."""
    req = spec.get('req') or []
    out: List[Dict[str, Any]] = []
    if len(req) > 1:
        order = sorted(range(len(req)), key=lambda i: len(req[i][0]))
        for i in order[:2]:
            sub = dict(spec, req=[g for (j, g) in enumerate(req) if j != i], opt=[])
            out = s2_search(sub)
            if out:
                break
    if not out:
        try:
            fz = fuzzy_rows([g[0] for g in req]) if req else []
        except DatabaseUnavailable:
            raise
        except Exception:
            fz = []
        out = [dict(r, _lang=row_lang(r), _kind=row_kind(r)) for r in fz]
    return out[:6]

def s2_rows(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = s2_search(spec)
    if not rows and spec.get('fz'):
        try:
            rows = [dict(r, _lang=row_lang(r), _kind=row_kind(r)) for r in fuzzy_rows([g[0] for g in spec['req']])]
        except DatabaseUnavailable:
            raise
        except Exception:
            rows = []
    return rows

def s2_run(chat_id: int, user: Dict[str, Any], spec: Dict[str, Any], ctx: Dict[str, Any], note: str='', understood: str='', not_found: bool=True, raw: str='') -> bool:
    rows = s2_search(spec)
    if not rows:
        try:
            fz = fuzzy_rows([g[0] for g in spec['req']])
        except DatabaseUnavailable:
            raise
        except Exception:
            fz = []
        if fz:
            spec['fz'] = True
            rows = [dict(r, _lang=row_lang(r), _kind=row_kind(r)) for r in fz]
            note += '🔤 Spelling ke hisaab se milte-julte naam:\n'
    _known_w = lambda w: _s2_generic(w) or w in ALIASES or w in S2_BRANDS
    unknown_typed = any((not _known_w(g[0]) for g in spec.get('req') or []))
    if not rows and unknown_typed and spec.get('ai_req') and (spec['ai_req'] != spec['req']):
        typed = [g[0] for g in spec['req'] if not _known_w(g[0])]
        near = lambda a: any((difflib.SequenceMatcher(None, a, t).ratio() >= 0.75 for t in typed))
        ai_ok = [g for g in spec['ai_req'] if any((near(a) for a in g))]
        known = [g for g in spec['req'] if g[0] not in typed]
        sp_ai = dict(spec, req=known + [g for g in ai_ok if g not in known], opt=[], fz=False) if ai_ok else None
    else:
        sp_ai = None
    if not rows and (not sp_ai) and unknown_typed and spec.get('ai_book'):
        known = [g for g in spec['req'] if _known_w(g[0])]
        sp_ai = dict(spec, req=known + spec['ai_book'], opt=[], fz=False)
    if not rows and sp_ai:
        rows = s2_search(sp_ai)
        if rows:
            spec.update(sp_ai)
            note += f"🤖 Samjha: {' '.join((g[0] for g in spec['req']))}\n"
    note_user(None, searches=1)
    if not rows:
        if not not_found:
            return False
        s2_not_found(chat_id, user, spec, understood)
        return True
    if spec['lang'] and (not s2_filter(rows, spec['lang'], None)):
        c = _count_lang(rows)
        have = ', '.join((f'{LANG_LABEL[k]} ({c[k]})' for k in ('hi', 'en', 'both', 'unk') if c[k]))
        note += f"ℹ️ {LANG_LABEL.get(spec['lang'], spec['lang'])} me nahi mili — ye bhashayen hain: {have}\n"
        spec['lang'] = None
    if spec['kind'] and (not s2_filter(s2_filter(rows, spec['lang'], None) if spec['lang'] else rows, None, spec['kind'])):
        note += f"ℹ️ {KIND_LABEL.get(spec['kind'], spec['kind'])} nahi mili — baaki type dikha raha hu\n"
        spec['kind'] = spec['kind_q'] = None
    (txt, kb) = s2_screen(spec, rows, 1)
    ctx.update({'s2': spec, 'q': [g[0] for g in spec['req']], 'lang': None, 'types': [], 'raw': (raw or spec.get('raw') or '')[:120], 'shown': [], 'ask': None, 'pending': None, 's2p': 1})
    save_ctx(user.get('id'), ctx)
    send_text(chat_id, (esc(note) + '\n' if note else '') + txt, kb, 'HTML')
    _FX.found = True
    return True


def install(namespace):
    """Keep the legacy bot's shared state and Flask endpoint registrations intact."""
    namespace.update(DatabaseUnavailable=DatabaseUnavailable,
                     _DB_RESTRICTION=_DB_RESTRICTION,
                     DB_UNAVAILABLE_TEXT=DB_UNAVAILABLE_TEXT)
    names = ['_db_check_available', '_db_note_error', 'supabase', 'count_rows', 'health', 'webhook', 'fuzzy_rows', 'row_lang', '_db_or', 's2_fetch', 's2_search', 's2_close_options', 's2_rows', 's2_run']
    for name in names:
        template = globals()[name]
        namespace[name] = FunctionType(template.__code__, namespace, name,
                                       template.__defaults__, template.__closure__)
    original_ensure = namespace["ensure_webhook"]
    def controlled_webhook(force=False):
        # New deployments stay passive until explicitly activated. This also
        # stops an older deployment on the same repository stealing updates.
        if namespace["os"].environ.get("WEBHOOK_AUTO_SETUP", "0").lower() not in {"1", "true", "on"}:
            return {"action": "disabled", "commands": "unchanged", "before": {}}
        return original_ensure(force=force)
    namespace["ensure_webhook"] = controlled_webhook
    namespace["app"].view_functions["health"] = namespace["health"]
    namespace["app"].view_functions["webhook"] = namespace["webhook"]
