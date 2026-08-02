"""
webapp.py — Telegram Mini App «Личное дело» поверх той же PostgreSQL, что и bot.py.

Работает как отдельный Flask-сервер в фоновом потоке того же процесса, что и бот
(см. запуск в bot.py: threading.Thread(target=run_webapp, daemon=True)). Все
константы (токен бота, RANKS, список отделов, слой БД) прокидываются снаружи
через create_app(...) — модуль не импортирует bot.py напрямую (у него нет
расширения .py, обычный import с ним не сработает).

Аутентификация — через Telegram initData: подпись, которую Web App SDK кладёт
в window.Telegram.WebApp.initData. Проверяем HMAC-подписью на BOT_TOKEN
(см. validate_init_data) — так бэкенд узнаёт user_id без отдельного логина.

Права:
  - Свою карточку (без пометок) видит любой зарегистрированный пользователь.
  - Полную карточку (с пометками) и чужие карточки видят и редактируют только
    ранг 5+, спецдопуск или главный админ — is_staff().
"""

import hashlib
import hmac
import json
import logging
from urllib.parse import parse_qsl

from flask import Flask, request, jsonify, Response

logger = logging.getLogger(__name__)


# ─────────────────────────── Проверка initData ───────────────────────────

def validate_init_data(init_data: str, bot_token: str):
    """
    Проверяет подпись initData по алгоритму Telegram (HMAC-SHA256 secret_key
    = HMAC-SHA256("WebAppData", bot_token)). Возвращает dict пользователя
    (id, first_name, username, ...) или None, если подпись неверна/данных нет.
    """
    if not init_data or not bot_token:
        return None
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None
    received_hash = pairs.pop('hash', None)
    if not received_hash:
        return None
    data_check_string = '\n'.join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed_hash, received_hash):
        return None
    user_json = pairs.get('user')
    if not user_json:
        return None
    try:
        return json.loads(user_json)
    except (json.JSONDecodeError, TypeError):
        return None


def create_app(*, bot_token, db_file, sqlite3_module, ranks, admin_ids, get_departments):
    """
    Собирает Flask-приложение. Все зависимости от bot.py — параметрами, чтобы
    этот модуль оставался самостоятельным.
    """
    app = Flask(__name__)
    admin_ids_set = set(admin_ids) if isinstance(admin_ids, (list, set, tuple)) else {admin_ids}

    # ── Утилиты БД (тот же паттерн, что и в bot.py: sqlite3_module — это db_pg) ──

    def _fetchone(query, params=()):
        conn = sqlite3_module.connect(db_file)
        conn.row_factory = sqlite3_module.Row
        c = conn.cursor()
        c.execute(query, params)
        row = c.fetchone()
        conn.close()
        return dict(row) if row else None

    def _fetchall(query, params=()):
        conn = sqlite3_module.connect(db_file)
        conn.row_factory = sqlite3_module.Row
        c = conn.cursor()
        c.execute(query, params)
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return rows

    def _execute(query, params=()):
        conn = sqlite3_module.connect(db_file)
        c = conn.cursor()
        c.execute(query, params)
        conn.commit()
        ok = c.rowcount > 0
        conn.close()
        return ok

    def _get_user_row(user_id):
        return _fetchone('SELECT * FROM users WHERE user_id = ?', (user_id,))

    def _is_staff(user_id):
        if user_id in admin_ids_set:
            return True
        row = _get_user_row(user_id)
        if not row:
            return False
        if row.get('special_access') == 'Да':
            return True
        return (row.get('rank') or 0) >= 5

    def _rank_name(rank):
        return ranks.get(rank, ranks.get(0, {})).get('name', str(rank))

    def _dossier_payload(user_id, include_notes: bool):
        row = _get_user_row(user_id)
        if not row:
            return None
        awards = _fetchall(
            'SELECT award_name, award_type, date FROM awards WHERE user_id = ? ORDER BY id DESC LIMIT 10',
            (user_id,),
        )
        payload = {
            "user_id": user_id,
            "nick": row.get('game_nick') or '',
            "callsign": row.get('callsign') or '',
            "token": row.get('token') or '',
            "rank": row.get('rank') or 0,
            "rank_name": _rank_name(row.get('rank') or 0),
            "department": row.get('department') or 'Не назначен',
            "warns": row.get('warns') or '0/0',
            "reg_date": row.get('created_at') or '',
            "blocked": row.get('bot_blocked') == 'Да',
            "awards": [
                {"name": a['award_name'], "type": a['award_type'], "date": a['date']}
                for a in awards
            ],
        }
        if include_notes:
            notes = _fetchall(
                '''SELECT n.id, n.text, n.pinned, n.created_at, n.author_id,
                          u.game_nick AS author_nick
                   FROM user_notes n
                   LEFT JOIN users u ON u.user_id = n.author_id
                   WHERE n.user_id = ?
                   ORDER BY n.pinned DESC, n.id DESC''',
                (user_id,),
            )
            payload["notes"] = [
                {
                    "id": n['id'],
                    "text": n['text'],
                    "pinned": bool(n['pinned']),
                    "created_at": n['created_at'],
                    "author_nick": n['author_nick'] or f"id {n['author_id']}",
                }
                for n in notes
            ]
        return payload

    # ── Аутентификация каждого запроса ──

    def _authed_user_id():
        """Достаёт initData из заголовка, проверяет подпись, возвращает user_id или None."""
        init_data = request.headers.get('Telegram-Init-Data', '')
        tg_user = validate_init_data(init_data, bot_token)
        if not tg_user or 'id' not in tg_user:
            return None
        return int(tg_user['id'])

    # ── Роуты API ──

    @app.get('/api/me')
    def api_me():
        user_id = _authed_user_id()
        if not user_id:
            return jsonify({"error": "unauthorized"}), 401
        staff = _is_staff(user_id)
        payload = _dossier_payload(user_id, include_notes=staff)
        if not payload:
            return jsonify({"error": "not_registered"}), 404
        payload["is_staff"] = staff
        payload["is_self"] = True
        if staff:
            payload["rank_choices"] = [{"id": rid, "name": r.get('name', str(rid))} for rid, r in sorted(ranks.items())]
            payload["dept_choices"] = get_departments() or []
        return jsonify(payload)

    @app.get('/api/dossier/<int:target_id>')
    def api_dossier(target_id):
        user_id = _authed_user_id()
        if not user_id:
            return jsonify({"error": "unauthorized"}), 401
        if target_id == user_id:
            staff = _is_staff(user_id)
            payload = _dossier_payload(target_id, include_notes=staff)
        elif _is_staff(user_id):
            payload = _dossier_payload(target_id, include_notes=True)
        else:
            return jsonify({"error": "forbidden"}), 403
        if not payload:
            return jsonify({"error": "not_registered"}), 404
        payload["is_staff"] = _is_staff(user_id)
        payload["is_self"] = target_id == user_id
        return jsonify(payload)

    @app.get('/api/search')
    def api_search():
        user_id = _authed_user_id()
        if not user_id or not _is_staff(user_id):
            return jsonify({"error": "forbidden"}), 403
        q = (request.args.get('q') or '').strip()
        if len(q) < 2:
            return jsonify({"results": []})
        rows = _fetchall(
            '''SELECT user_id, game_nick, callsign, rank FROM users
               WHERE game_nick LIKE ? OR callsign LIKE ?
               ORDER BY rank DESC LIMIT 20''',
            (f'%{q}%', f'%{q}%'),
        )
        return jsonify({"results": [
            {"user_id": r['user_id'], "nick": r['game_nick'], "callsign": r['callsign'],
             "rank_name": _rank_name(r['rank'] or 0)}
            for r in rows
        ]})

    @app.post('/api/dossier/<int:target_id>')
    def api_dossier_update(target_id):
        user_id = _authed_user_id()
        if not user_id or not _is_staff(user_id):
            return jsonify({"error": "forbidden"}), 403
        if not _get_user_row(target_id):
            return jsonify({"error": "not_registered"}), 404
        data = request.get_json(silent=True) or {}

        nick = (data.get('nick') or '').strip()
        callsign = (data.get('callsign') or '').strip()
        department = (data.get('department') or '').strip()
        rank = data.get('rank')

        if nick:
            _execute('UPDATE users SET game_nick = ? WHERE user_id = ?', (nick[:64], target_id))
        if callsign:
            _execute('UPDATE users SET callsign = ? WHERE user_id = ?', (callsign[:32], target_id))
        if department and department in (get_departments() or []):
            _execute('UPDATE users SET department = ? WHERE user_id = ?', (department, target_id))
        if isinstance(rank, int) and rank in ranks:
            _execute('UPDATE users SET rank = ? WHERE user_id = ?', (rank, target_id))

        payload = _dossier_payload(target_id, include_notes=True)
        payload["is_staff"] = True
        payload["is_self"] = target_id == user_id
        return jsonify(payload)

    @app.post('/api/dossier/<int:target_id>/notes')
    def api_dossier_add_note(target_id):
        user_id = _authed_user_id()
        if not user_id or not _is_staff(user_id):
            return jsonify({"error": "forbidden"}), 403
        if not _get_user_row(target_id):
            return jsonify({"error": "not_registered"}), 404
        data = request.get_json(silent=True) or {}
        text = (data.get('text') or '').strip()[:1000]
        if not text:
            return jsonify({"error": "empty_text"}), 400
        conn = sqlite3_module.connect(db_file)
        c = conn.cursor()
        c.execute(
            'INSERT INTO user_notes (user_id, author_id, text, created_at) VALUES (?, ?, ?, ?)',
            (target_id, user_id, text, _now_str()),
        )
        conn.commit()
        conn.close()
        payload = _dossier_payload(target_id, include_notes=True)
        payload["is_staff"] = True
        payload["is_self"] = target_id == user_id
        return jsonify(payload)

    def _now_str():
        import datetime
        try:
            from zoneinfo import ZoneInfo
            return datetime.datetime.now(ZoneInfo("Europe/Moscow")).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    # ── Health-check (нужен bothost, чтобы считать деплой живым) ──

    @app.get('/health')
    def health():
        return "ok"

    # ── Сама страница Mini App ──

    @app.get('/')
    def index():
        return Response(_INDEX_HTML, mimetype='text/html')

    return app


_INDEX_HTML = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Личное дело</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  :root {
    --bg: var(--tg-theme-bg-color, #0e1116);
    --card: var(--tg-theme-secondary-bg-color, #171b22);
    --text: var(--tg-theme-text-color, #e8e8e8);
    --hint: var(--tg-theme-hint-color, #8a8f98);
    --accent: #b9974a;
    --accent-dim: #6e5a2c;
    --danger: #b04a4a;
    --line: #2a2f38;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 16px 14px 32px;
    background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }
  .folder {
    background: var(--card);
    border: 1px solid var(--line);
    border-top: 3px solid var(--accent);
    border-radius: 6px;
    padding: 16px;
    margin-bottom: 14px;
  }
  .folder-title {
    text-align: center;
    letter-spacing: 1px;
    font-size: 13px;
    color: var(--accent);
    text-transform: uppercase;
    border-bottom: 1px dashed var(--accent-dim);
    padding-bottom: 10px;
    margin-bottom: 12px;
  }
  .folder-title b { display: block; font-size: 17px; color: var(--text); margin-top: 4px; letter-spacing: 0.5px; }
  .row { display: flex; justify-content: space-between; gap: 10px; padding: 6px 0; font-size: 14px; border-bottom: 1px solid var(--line); }
  .row:last-child { border-bottom: none; }
  .row .k { color: var(--hint); }
  .row .v { text-align: right; font-weight: 500; }
  .section-title { font-size: 12px; text-transform: uppercase; letter-spacing: 1px; color: var(--accent); margin: 4px 0 8px; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 12px; background: var(--accent-dim); color: #f2e6c9; }
  .warn-badge { background: #4a2a2a; color: #e6b0b0; }
  .award { padding: 6px 0; font-size: 13px; border-bottom: 1px solid var(--line); }
  .award:last-child { border-bottom: none; }
  .muted { color: var(--hint); font-size: 13px; }
  input, select, textarea {
    width: 100%; padding: 9px 10px; margin: 4px 0 10px;
    background: #0d1015; border: 1px solid var(--line); border-radius: 5px;
    color: var(--text); font-size: 14px;
  }
  textarea { min-height: 60px; resize: vertical; }
  button {
    width: 100%; padding: 11px; border: none; border-radius: 6px;
    background: var(--accent); color: #1a1400; font-weight: 600; font-size: 14px;
    cursor: pointer;
  }
  button.secondary { background: transparent; border: 1px solid var(--accent-dim); color: var(--accent); margin-top: 6px; }
  .note { padding: 8px 0; border-bottom: 1px solid var(--line); font-size: 13px; }
  .note:last-child { border-bottom: none; }
  .note .meta { color: var(--hint); font-size: 11px; margin-top: 2px; }
  .search-results { margin-top: 8px; }
  .search-item { padding: 8px; border: 1px solid var(--line); border-radius: 5px; margin-bottom: 6px; cursor: pointer; font-size: 13px; }
  .search-item:active { background: var(--accent-dim); }
  .hidden { display: none !important; }
  .error { color: var(--danger); font-size: 13px; padding: 10px; text-align: center; }
  .stamp { text-align: center; color: var(--hint); font-size: 11px; margin-top: 18px; letter-spacing: 1px; }
</style>
</head>
<body>
  <div id="root"><div class="muted" style="text-align:center;padding:40px 0;">Загрузка личного дела…</div></div>

<script>
const tg = window.Telegram && window.Telegram.WebApp;
if (tg) { tg.ready(); tg.expand(); }
const initData = tg ? tg.initData : "";

function api(path, opts) {
  opts = opts || {};
  opts.headers = Object.assign({'Content-Type': 'application/json', 'Telegram-Init-Data': initData}, opts.headers || {});
  return fetch(path, opts).then(async r => {
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || ('http_' + r.status));
    return data;
  });
}

const root = document.getElementById('root');

function renderError(msg) {
  const map = {
    unauthorized: 'Не удалось подтвердить личность через Telegram.',
    not_registered: 'Пользователь не зарегистрирован в боте.',
    forbidden: 'Недостаточно прав для просмотра этого личного дела.',
  };
  root.innerHTML = '<div class="folder"><div class="error">' + (map[msg] || 'Ошибка загрузки: ' + msg) + '</div></div>';
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s == null ? '' : String(s);
  return d.innerHTML;
}

function dossierHtml(d) {
  let html = '';
  html += '<div class="folder">';
  html += '  <div class="folder-title">Личное дело<b>№ ' + esc(d.token || '—') + '</b></div>';
  html += '  <div class="row"><span class="k">Ник</span><span class="v">' + esc(d.nick) + '</span></div>';
  html += '  <div class="row"><span class="k">Позывной</span><span class="v">' + esc(d.callsign) + '</span></div>';
  html += '  <div class="row"><span class="k">Ранг</span><span class="v">' + esc(d.rank_name) + '</span></div>';
  html += '  <div class="row"><span class="k">Отдел</span><span class="v">' + esc(d.department) + '</span></div>';
  html += '  <div class="row"><span class="k">В деле с</span><span class="v">' + esc(d.reg_date || '—') + '</span></div>';
  html += '  <div class="row"><span class="k">Взыскания</span><span class="v"><span class="badge warn-badge">' + esc(d.warns) + '</span></span></div>';
  if (d.blocked) {
    html += '  <div class="row"><span class="k">Статус</span><span class="v"><span class="badge warn-badge">отключён от бота</span></span></div>';
  }
  html += '</div>';

  html += '<div class="folder"><div class="section-title">Награды</div>';
  if (d.awards && d.awards.length) {
    d.awards.forEach(a => {
      html += '<div class="award">🏅 ' + esc(a.name) + ' <span class="muted">— ' + esc(a.type) + ', ' + esc(a.date) + '</span></div>';
    });
  } else {
    html += '<div class="muted">Наград нет.</div>';
  }
  html += '</div>';

  if (d.notes) {
    html += '<div class="folder"><div class="section-title">Особые отметки (видно только сотрудникам)</div>';
    html += '<div id="notes-list">' + notesHtml(d.notes) + '</div>';
    if (d.is_staff) {
      html += '<textarea id="note-text" placeholder="Новая отметка…"></textarea>';
      html += '<button id="note-add-btn">Добавить отметку</button>';
    }
    html += '</div>';
  }

  return html;
}

function notesHtml(notes) {
  if (!notes.length) return '<div class="muted">Отметок нет.</div>';
  return notes.map(n =>
    '<div class="note">' + (n.pinned ? '📌 ' : '') + esc(n.text) +
    '<div class="meta">' + esc(n.author_nick) + ' · ' + esc(n.created_at) + '</div></div>'
  ).join('');
}

function editFormHtml(d) {
  let opts = '';
  (window.__RANKS__ || []).forEach(r => {
    opts += '<option value="' + r.id + '"' + (r.id === d.rank ? ' selected' : '') + '>' + esc(r.name) + '</option>';
  });
  let deptOpts = '';
  (window.__DEPTS__ || []).forEach(dep => {
    deptOpts += '<option value="' + esc(dep) + '"' + (dep === d.department ? ' selected' : '') + '>' + esc(dep) + '</option>';
  });
  return '<div class="folder"><div class="section-title">Редактирование (сотрудник)</div>' +
    '<input id="edit-nick" value="' + esc(d.nick) + '" placeholder="Ник">' +
    '<input id="edit-callsign" value="' + esc(d.callsign) + '" placeholder="Позывной">' +
    '<select id="edit-dept">' + deptOpts + '</select>' +
    '<select id="edit-rank">' + opts + '</select>' +
    '<button id="save-btn">Сохранить изменения</button>' +
    '</div>';
}

let currentId = null;

function renderDossier(d) {
  currentId = d.user_id;
  let html = dossierHtml(d);
  if (d.is_staff) html += editFormHtml(d);
  html += searchBlockHtml(d.is_staff);
  root.innerHTML = html;

  const saveBtn = document.getElementById('save-btn');
  if (saveBtn) saveBtn.onclick = () => saveEdits(d.user_id);
  const noteBtn = document.getElementById('note-add-btn');
  if (noteBtn) noteBtn.onclick = () => addNote(d.user_id);
  const searchInput = document.getElementById('search-input');
  if (searchInput) searchInput.oninput = debounce(doSearch, 350);
}

function searchBlockHtml(isStaff) {
  if (!isStaff) return '';
  return '<div class="folder"><div class="section-title">Найти другое личное дело</div>' +
    '<input id="search-input" placeholder="Ник или позывной…">' +
    '<div id="search-results" class="search-results"></div></div>';
}

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

function doSearch() {
  const q = document.getElementById('search-input').value.trim();
  const box = document.getElementById('search-results');
  if (q.length < 2) { box.innerHTML = ''; return; }
  api('/api/search?q=' + encodeURIComponent(q)).then(res => {
    box.innerHTML = res.results.map(u =>
      '<div class="search-item" data-id="' + u.user_id + '">' + esc(u.nick) +
      ' <span class="muted">· ' + esc(u.callsign) + ' · ' + esc(u.rank_name) + '</span></div>'
    ).join('') || '<div class="muted">Ничего не найдено.</div>';
    box.querySelectorAll('.search-item').forEach(el => {
      el.onclick = () => loadDossier(parseInt(el.dataset.id, 10));
    });
  }).catch(e => { box.innerHTML = '<div class="error">' + esc(e.message) + '</div>'; });
}

function saveEdits(id) {
  const body = {
    nick: document.getElementById('edit-nick').value,
    callsign: document.getElementById('edit-callsign').value,
    department: document.getElementById('edit-dept').value,
    rank: parseInt(document.getElementById('edit-rank').value, 10),
  };
  api('/api/dossier/' + id, { method: 'POST', body: JSON.stringify(body) })
    .then(renderDossier)
    .catch(e => alert('Не сохранилось: ' + e.message));
}

function addNote(id) {
  const el = document.getElementById('note-text');
  const text = el.value.trim();
  if (!text) return;
  api('/api/dossier/' + id + '/notes', { method: 'POST', body: JSON.stringify({ text }) })
    .then(renderDossier)
    .catch(e => alert('Не удалось добавить: ' + e.message));
}

function loadDossier(id) {
  root.innerHTML = '<div class="muted" style="text-align:center;padding:40px 0;">Загрузка…</div>';
  api('/api/dossier/' + id).then(renderDossier).catch(e => renderError(e.message));
}

// Инициализация: подтягиваем справочники рангов/отделов из своей же карточки,
// затем рендерим личное дело текущего пользователя.
api('/api/me').then(d => {
  window.__RANKS__ = d.rank_choices || [];
  window.__DEPTS__ = d.dept_choices || [];
  renderDossier(d);
}).catch(e => renderError(e.message));
</script>
  <div class="stamp">ФСБ · РМ-РП 02</div>
</body>
</html>
"""
