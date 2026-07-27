"""
db_pg.py — слой совместимости SQLite → PostgreSQL для bot.py.

Задача: бот написан под модуль sqlite3 (158 мест `sqlite3.connect`, 306 запросов
с плейсхолдерами `?`). Переписывать всё вручную — гарантированно что-то пропустить.
Вместо этого здесь эмулируется API sqlite3 поверх psycopg3, с автоматическим
переводом SQL-диалекта.

Что делает слой:
  • `?`  → `%s`                      (плейсхолдеры)
  • `"строка"` → `'строка'`          (в SQLite двойные кавычки = литерал,
                                      в PostgreSQL = имя колонки → ошибка)
  • `INTEGER PRIMARY KEY AUTOINCREMENT` → `BIGSERIAL PRIMARY KEY`
  • `INTEGER` → `BIGINT`             (Telegram ID не влезает в INTEGER PG)
  • `ADD COLUMN` → `ADD COLUMN IF NOT EXISTS`
  • `cursor.lastrowid`               (в PG нет — добавляется `RETURNING id`)
  • `sqlite3.Row`                    (гибридные строки: row[0] И row['col'])
  • каждый запрос в SAVEPOINT        (в PG упавший запрос «отравляет» транзакцию,
                                      в SQLite — нет; savepoint выравнивает поведение)
  • ошибки psycopg → sqlite3.OperationalError / sqlite3.IntegrityError,
    чтобы существующие блоки `except` в боте продолжали работать.

Подключение берётся из переменной окружения DATABASE_URL.
"""

import os
import re
import logging
import sqlite3 as _sqlite3  # только ради классов исключений

import psycopg
from psycopg import sql as _pgsql
from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

# Пул соединений. Соединение до удалённого хоста дорогое (~20-50 мс на handshake),
# поэтому переиспользуем. max_size с запасом: бот однопоточный, но JobQueue
# может выполняться параллельно с хэндлерами.
_pool: ConnectionPool | None = None

# Кэш: у каких таблиц есть колонка `id` — нужно, чтобы решать,
# можно ли дописывать `RETURNING id` ради эмуляции lastrowid.
_tables_with_id: set[str] | None = None


def init_pool(dsn: str = None, min_size: int = 1, max_size: int = 10):
    """Создаёт пул соединений. Вызывается один раз при старте бота."""
    global _pool
    if _pool is not None:
        return _pool
    dsn = dsn or DATABASE_URL
    if not dsn:
        raise RuntimeError(
            "Не задана строка подключения. Установите переменную окружения DATABASE_URL, "
            "например: export DATABASE_URL='postgresql://user:pass@host:port/dbname'"
        )
    _pool = ConnectionPool(
        dsn,
        min_size=min_size,
        max_size=max_size,
        max_idle=300,
        timeout=30,
        kwargs={"autocommit": False},
    )
    _pool.wait(timeout=30)
    logger.info("PostgreSQL: пул соединений готов")
    return _pool


def close_pool():
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


# ─────────────────────────── Перевод SQL ───────────────────────────

def _split_sql(sql: str):
    """
    Разбивает SQL на куски: (текст_вне_строк, строковый_литерал_или_None).
    Нужно, чтобы не трогать `?` и кавычки внутри литералов вроде 'Что?'.
    """
    out = []
    buf = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            out.append(("".join(buf), None))
            buf = []
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":  # экранированная ''
                        j += 2
                        continue
                    break
                j += 1
            out.append((None, sql[i:j + 1]))
            i = j + 1
            continue
        if ch == '"':
            # В этом проекте двойные кавычки в SQL всегда означают строковый
            # литерал (наследие SQLite), идентификаторы в кавычки не берутся.
            j = sql.find('"', i + 1)
            if j == -1:
                buf.append(ch)
                i += 1
                continue
            inner = sql[i + 1:j]
            out.append(("".join(buf), None))
            buf = []
            out.append((None, "'" + inner.replace("'", "''") + "'"))
            i = j + 1
            continue
        buf.append(ch)
        i += 1
    if buf:
        out.append(("".join(buf), None))
    return out


_DDL_TYPE_RE = re.compile(
    r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b", re.IGNORECASE
)


def _translate_ddl(code: str) -> str:
    """Переводит типы SQLite → PostgreSQL в CREATE TABLE / ALTER TABLE."""
    # INTEGER PRIMARY KEY AUTOINCREMENT → BIGSERIAL PRIMARY KEY
    code = _DDL_TYPE_RE.sub("BIGSERIAL PRIMARY KEY", code)
    # Остальные INTEGER → BIGINT (Telegram user_id/chat_id не влезают в INTEGER)
    code = re.sub(r"\bINTEGER\b", "BIGINT", code, flags=re.IGNORECASE)
    #
    # ВАЖНО: `ADD COLUMN` НЕ переписывается в `ADD COLUMN IF NOT EXISTS`.
    # Бот использует идиому:
    #     try:
    #         c.execute('ALTER TABLE ... ADD COLUMN ...')
    #         ...однократный бэкфилл данных...
    #     except sqlite3.OperationalError:
    #         pass
    # Здесь важно, чтобы повторный ALTER именно ПАДАЛ — иначе бэкфилл
    # выполнялся бы при каждом запуске и затирал пользовательские настройки
    # (например видимость отделов в /find). Savepoint вокруг каждого запроса
    # делает такое «ожидаемое падение» безопасным для транзакции.
    return code


def translate_sql(sql: str) -> str:
    """Полный перевод одного SQL-запроса из диалекта SQLite в PostgreSQL."""
    parts = _split_sql(sql)
    is_ddl = bool(re.match(r"\s*(CREATE|ALTER|DROP)\b", sql, re.IGNORECASE))
    res = []
    for code, literal in parts:
        if literal is not None:
            res.append(literal)
            continue
        if code is None:
            continue
        # плейсхолдеры
        code = code.replace("?", "%s")
        if is_ddl:
            code = _translate_ddl(code)
        res.append(code)
    return "".join(res)


# ─────────────────────────── Строки результата ───────────────────────────

class Row(dict):
    """
    Гибридная строка: ведёт себя и как dict (row['nick'], dict(row)),
    и как кортеж (row[0], распаковка) — то же, что делал sqlite3.Row.
    Это важно, потому что в боте встречаются оба стиля доступа.
    """
    __slots__ = ("_order",)

    def __init__(self, mapping, order):
        super().__init__(mapping)
        self._order = order

    def __getitem__(self, key):
        if isinstance(key, int):
            return super().__getitem__(self._order[key])
        if isinstance(key, slice):
            return tuple(super(Row, self).__getitem__(k) for k in self._order[key])
        return super().__getitem__(key)

    def keys(self):
        return list(self._order)

    def __iter__(self):
        # Итерация по значениям — как у кортежа (sqlite3.Row ведёт себя так же)
        return iter(super().__getitem__(k) for k in self._order)


def _row_factory(cursor):
    cols = [d.name for d in (cursor.description or [])]

    def make(values):
        return Row(dict(zip(cols, values)), cols)

    return make


# ─────────────────────────── Курсор ───────────────────────────

_INSERT_RE = re.compile(r"\s*INSERT\s+INTO\s+[\"']?(\w+)", re.IGNORECASE)
_RETURNING_RE = re.compile(r"\bRETURNING\b", re.IGNORECASE)


def _load_tables_with_id(conn) -> set:
    global _tables_with_id
    if _tables_with_id is not None:
        return _tables_with_id
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND column_name = 'id'"
        )
        _tables_with_id = {r[0] for r in cur.fetchall()}
    return _tables_with_id


class Cursor:
    """Эмуляция sqlite3.Cursor поверх psycopg."""

    def __init__(self, conn_wrapper):
        self._cw = conn_wrapper
        self._cur = conn_wrapper._raw.cursor()
        self.lastrowid = None

    # --- основное ---
    def execute(self, sql, params=()):
        pg_sql = translate_sql(sql)
        self.lastrowid = None

        want_lastrowid = False
        m = _INSERT_RE.match(pg_sql)
        if m and not _RETURNING_RE.search(pg_sql):
            table = m.group(1).lower()
            if table in _load_tables_with_id(self._cw._raw):
                pg_sql = pg_sql.rstrip().rstrip(";") + " RETURNING id"
                want_lastrowid = True

        # SAVEPOINT вокруг каждого запроса.
        #
        # Зачем: в PostgreSQL упавший запрос переводит всю транзакцию в состояние
        # aborted — любые следующие запросы падают до rollback. В SQLite такого
        # нет, и бот на это опирается (например `try: ALTER TABLE ... except: pass`
        # в init_database). Savepoint позволяет откатить только упавший запрос.
        #
        # Важно: НЕ используем psycopg-шный conn.transaction() — как внешний блок
        # он делает COMMIT на выходе, из-за чего каждый запрос коммитился бы сам
        # и явный conn.rollback() переставал работать. Поэтому пишем SAVEPOINT
        # вручную: psycopg сам откроет транзакцию перед первым запросом, а мы
        # внутри неё ставим точку сохранения.
        sp = "sp_bot"
        self._sp(f"SAVEPOINT {sp}")
        try:
            self._cur.execute(pg_sql, tuple(params) if params else None)
            if want_lastrowid:
                row = self._cur.fetchone()
                if row:
                    self.lastrowid = row[0]
        except psycopg.Error as e:
            self._sp(f"ROLLBACK TO SAVEPOINT {sp}")
            if isinstance(e, (psycopg.errors.UniqueViolation,
                              psycopg.errors.IntegrityError)):
                raise _sqlite3.IntegrityError(str(e)) from e
            raise _sqlite3.OperationalError(f"{e}\nЗапрос: {pg_sql}") from e
        else:
            self._sp(f"RELEASE SAVEPOINT {sp}")
        return self

    def _sp(self, cmd):
        """
        Выполняет служебную команду SAVEPOINT/RELEASE/ROLLBACK TO на ОТДЕЛЬНОМ
        курсоре. Если делать это на основном, он потеряет результат запроса —
        например SELECT вернул бы пустоту.
        """
        try:
            with self._cw._raw.cursor() as helper:
                helper.execute(cmd)
        except psycopg.Error:
            pass

    def executemany(self, sql, seq):
        pg_sql = translate_sql(sql)
        sp = "sp_bot_many"
        self._sp(f"SAVEPOINT {sp}")
        try:
            self._cur.executemany(pg_sql, [tuple(p) for p in seq])
        except psycopg.Error as e:
            self._sp(f"ROLLBACK TO SAVEPOINT {sp}")
            if isinstance(e, (psycopg.errors.UniqueViolation,
                              psycopg.errors.IntegrityError)):
                raise _sqlite3.IntegrityError(str(e)) from e
            raise _sqlite3.OperationalError(f"{e}\nЗапрос: {pg_sql}") from e
        else:
            self._sp(f"RELEASE SAVEPOINT {sp}")
        return self

    # --- выборка ---
    def fetchone(self):
        try:
            row = self._cur.fetchone()
        except psycopg.ProgrammingError:
            return None
        return self._wrap(row)

    def fetchall(self):
        try:
            rows = self._cur.fetchall()
        except psycopg.ProgrammingError:
            return []
        return [self._wrap(r) for r in rows]

    def fetchmany(self, size=1):
        try:
            rows = self._cur.fetchmany(size)
        except psycopg.ProgrammingError:
            return []
        return [self._wrap(r) for r in rows]

    def _wrap(self, row):
        if row is None:
            return None
        if self._cw._use_rows:
            cols = [d.name for d in (self._cur.description or [])]
            return Row(dict(zip(cols, row)), cols)
        return row

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def description(self):
        return self._cur.description

    def close(self):
        try:
            self._cur.close()
        except Exception:
            pass

    def __iter__(self):
        for row in self._cur:
            yield self._wrap(row)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


# ─────────────────────────── Соединение ───────────────────────────

class Connection:
    """Эмуляция sqlite3.Connection поверх соединения из пула psycopg."""

    def __init__(self):
        if _pool is None:
            init_pool()
        self._raw = _pool.getconn()
        self._closed = False
        self._use_rows = False   # включается через .row_factory
        self._row_factory = None

    # bot.py делает: conn.row_factory = sqlite3.Row
    @property
    def row_factory(self):
        return self._row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._row_factory = value
        self._use_rows = value is not None

    def cursor(self):
        return Cursor(self)

    def execute(self, sql, params=()):
        cur = self.cursor()
        cur.execute(sql, params)
        return cur

    def commit(self):
        if not self._closed:
            try:
                self._raw.commit()
            except psycopg.Error as e:
                raise _sqlite3.OperationalError(str(e)) from e

    def rollback(self):
        if not self._closed:
            try:
                self._raw.rollback()
            except psycopg.Error:
                pass

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            # Незакоммиченные изменения откатываем — как делает sqlite3 при close()
            self._raw.rollback()
        except Exception:
            pass
        try:
            _pool.putconn(self._raw)
        except Exception:
            pass
        self._raw = None

    def __del__(self):
        # Страховка: если где-то в коде соединение не закрыли из-за исключения,
        # оно всё равно вернётся в пул, а не «утечёт».
        try:
            if not self._closed:
                self.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *a):
        if exc_type:
            self.rollback()
        else:
            self.commit()


def connect(*args, **kwargs):
    """
    Замена sqlite3.connect(DB_FILE). Аргументы игнорируются — путь к файлу
    больше не нужен, соединение берётся из пула PostgreSQL.
    """
    return Connection()


# Классы исключений — чтобы `except sqlite3.OperationalError` в боте работал
OperationalError = _sqlite3.OperationalError
IntegrityError = _sqlite3.IntegrityError
DatabaseError = _sqlite3.DatabaseError
Error = _sqlite3.Error
Row_ = Row
