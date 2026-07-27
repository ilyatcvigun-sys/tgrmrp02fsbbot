#!/usr/bin/env python3
"""
migrate_to_pg.py — перенос базы бота из SQLite в PostgreSQL.

Запуск:
    export DATABASE_URL='postgresql://user:pass@host:port/dbname'
    python3 migrate_to_pg.py conference.db

Что делает:
  1. Читает схему из SQLite и создаёт эквивалентные таблицы в PostgreSQL
     (INTEGER → BIGINT, AUTOINCREMENT → BIGSERIAL, "литерал" → 'литерал').
  2. Переносит все строки.
  3. Выставляет счётчики последовательностей, чтобы новые записи не конфликтовали
     по id с уже перенесёнными.
  4. Сверяет количество строк в каждой таблице и печатает отчёт.

Скрипт идемпотентен по схеме (CREATE TABLE IF NOT EXISTS), но данные по умолчанию
не дублирует: если в целевой таблице уже есть строки, она пропускается.
Флаг --force очищает целевые таблицы перед переносом.
"""

import os
import sys
import sqlite3
import argparse

import psycopg

# Переиспользуем переводчик диалекта из слоя совместимости
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_pg import translate_sql  # noqa: E402


# Порядок важен только для читаемости отчёта — внешних ключей в схеме нет
def get_sqlite_tables(con):
    cur = con.cursor()
    cur.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    return cur.fetchall()


def get_columns(con, table):
    return [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]


def get_int_columns(con, table):
    """Индексы колонок, объявленных как INTEGER — их значения нужно проверять."""
    out = []
    for i, r in enumerate(con.execute(f'PRAGMA table_info("{table}")')):
        if "INT" in (r[2] or "").upper():
            out.append(i)
    return out


def sanitize_row(row, int_idx, table, cols, problems):
    """
    SQLite из-за динамической типизации допускает строки в INTEGER-колонках
    (например banned_by = '-'). PostgreSQL такое отвергает. Приводим такие
    значения к NULL и записываем в отчёт, чтобы ничего не потерялось молча.
    """
    row = list(row)
    for i in int_idx:
        v = row[i]
        if v is None or isinstance(v, (int, float)):
            continue
        s = str(v).strip()
        try:
            row[i] = int(s)
        except ValueError:
            problems.append(f"{table}.{cols[i]} = {v!r} → NULL")
            row[i] = None
    return tuple(row)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sqlite_path", help="путь к conference.db")
    ap.add_argument("--dsn", default=os.environ.get("DATABASE_URL", ""),
                    help="строка подключения PostgreSQL (или переменная DATABASE_URL)")
    ap.add_argument("--force", action="store_true",
                    help="очистить целевые таблицы перед переносом")
    ap.add_argument("--schema-only", action="store_true",
                    help="только создать таблицы, данные не переносить")
    args = ap.parse_args()

    if not args.dsn:
        print("ОШИБКА: не задана строка подключения.")
        print("Укажите --dsn или переменную окружения DATABASE_URL.")
        return 1
    if not os.path.exists(args.sqlite_path):
        print(f"ОШИБКА: файл {args.sqlite_path} не найден.")
        return 1

    slite = sqlite3.connect(args.sqlite_path)
    tables = get_sqlite_tables(slite)
    print(f"Найдено таблиц в SQLite: {len(tables)}\n")

    pg = psycopg.connect(args.dsn)
    pg.autocommit = False
    cur = pg.cursor()

    # ── 1. Схема ─────────────────────────────────────────────────────
    print("── Создание схемы ──")
    for name, ddl in tables:
        if not ddl:
            continue
        pg_ddl = translate_sql(ddl)
        # SQLite пишет "CREATE TABLE x", нам нужно IF NOT EXISTS
        if "IF NOT EXISTS" not in pg_ddl.upper():
            pg_ddl = pg_ddl.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS", 1)
        try:
            cur.execute(pg_ddl)
            print(f"  ok   {name}")
        except psycopg.Error as e:
            pg.rollback()
            print(f"  FAIL {name}: {e}")
            print(f"       DDL: {pg_ddl[:300]}")
            return 1
    pg.commit()

    # Индексы из SQLite (кроме автоматических)
    idx = slite.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' "
        "AND sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    if idx:
        print("\n── Индексы ──")
        for iname, isql in idx:
            pg_sql = translate_sql(isql)
            if "IF NOT EXISTS" not in pg_sql.upper():
                pg_sql = pg_sql.replace("CREATE INDEX", "CREATE INDEX IF NOT EXISTS", 1)
                pg_sql = pg_sql.replace("CREATE UNIQUE INDEX",
                                        "CREATE UNIQUE INDEX IF NOT EXISTS", 1)
            try:
                cur.execute(pg_sql)
                pg.commit()
                print(f"  ok   {iname}")
            except psycopg.Error as e:
                pg.rollback()
                print(f"  skip {iname}: {str(e)[:80]}")

    if args.schema_only:
        print("\nРежим --schema-only: данные не переносились.")
        pg.close()
        return 0

    # ── 2. Данные ────────────────────────────────────────────────────
    print("\n── Перенос данных ──")
    report = []
    problems = []
    for name, _ in tables:
        cols = get_columns(slite, name)
        int_idx = get_int_columns(slite, name)
        rows = slite.execute(f'SELECT * FROM "{name}"').fetchall()
        rows = [sanitize_row(r, int_idx, name, cols, problems) for r in rows]
        src_count = len(rows)

        cur.execute(f'SELECT COUNT(*) FROM "{name}"')
        dst_count = cur.fetchone()[0]

        if dst_count and not args.force:
            print(f"  skip {name:22} (в PG уже {dst_count} строк, --force чтобы перезаписать)")
            report.append((name, src_count, dst_count, "пропущено"))
            continue

        if args.force and dst_count:
            cur.execute(f'TRUNCATE TABLE "{name}" RESTART IDENTITY CASCADE')

        if src_count:
            collist = ", ".join(f'"{c}"' for c in cols)
            ph = ", ".join(["%s"] * len(cols))
            try:
                cur.executemany(
                    f'INSERT INTO "{name}" ({collist}) VALUES ({ph})',
                    [tuple(r) for r in rows],
                )
                pg.commit()
            except psycopg.Error as e:
                pg.rollback()
                print(f"  FAIL {name}: {e}")
                report.append((name, src_count, 0, "ОШИБКА"))
                continue

        cur.execute(f'SELECT COUNT(*) FROM "{name}"')
        final = cur.fetchone()[0]
        status = "ok" if final == src_count else "РАСХОЖДЕНИЕ"
        print(f"  {status:4} {name:22} {src_count:>5} → {final:>5}")
        report.append((name, src_count, final, status))

    # ── 3. Синхронизация последовательностей ─────────────────────────
    print("\n── Счётчики id (sequences) ──")
    for name, _ in tables:
        cols = get_columns(slite, name)
        if "id" not in cols:
            continue
        try:
            cur.execute(
                "SELECT pg_get_serial_sequence(%s, 'id')", (name,)
            )
            seq = cur.fetchone()[0]
            if not seq:
                continue
            cur.execute(f'SELECT COALESCE(MAX(id), 0) FROM "{name}"')
            mx = cur.fetchone()[0]
            cur.execute("SELECT setval(%s, %s, true)", (seq, max(mx, 1)))
            pg.commit()
            print(f"  ok   {name:22} следующий id = {max(mx, 1) + 1}")
        except psycopg.Error as e:
            pg.rollback()
            print(f"  skip {name}: {str(e)[:70]}")

    # ── 4. Отчёт ─────────────────────────────────────────────────────
    print("\n" + "=" * 58)
    print("ИТОГ")
    print("=" * 58)
    bad = [r for r in report if r[3] not in ("ok", "пропущено")]
    total_src = sum(r[1] for r in report)
    total_dst = sum(r[2] for r in report)
    print(f"Таблиц обработано : {len(report)}")
    print(f"Строк в SQLite    : {total_src}")
    print(f"Строк в PostgreSQL: {total_dst}")
    if problems:
        print(f"\nИсправлено значений с неверным типом: {len(problems)}")
        print("(SQLite допускал текст в числовых колонках, PostgreSQL — нет)")
        for p in problems[:20]:
            print(f"  • {p}")
        if len(problems) > 20:
            print(f"  …и ещё {len(problems) - 20}")
    if bad:
        print(f"\nПРОБЛЕМЫ в таблицах: {', '.join(r[0] for r in bad)}")
        return 1
    print("\nПеренос завершён без ошибок.")
    slite.close()
    pg.close()
    return 0


def auto_migrate_if_needed(sqlite_path: str = "conference.db", dsn: str = None) -> bool:
    """
    Автоматический перенос при первом запуске — для хостингов без консоли
    (bothost и подобные), где нельзя выполнить скрипт вручную.

    Логика:
      • если в PostgreSQL таблица users уже содержит строки — ничего не делаем;
      • если файла SQLite нет — ничего не делаем (значит база уже «родная» PG);
      • иначе переносим данные и пишем подробный отчёт в лог.

    Вызывается из bot.py при старте. Безопасно вызывать при каждом запуске:
    повторно данные не переносятся.
    """
    import logging
    log = logging.getLogger("migrate")

    dsn = dsn or os.environ.get("DATABASE_URL", "")
    if not dsn:
        log.error("DATABASE_URL не задан — автоперенос пропущен")
        return False

    if not os.path.exists(sqlite_path):
        log.info(f"Файл {sqlite_path} не найден — переносить нечего, работаем на PostgreSQL")
        return False

    # Уже переносили?
    try:
        with psycopg.connect(dsn) as probe:
            with probe.cursor() as pc:
                pc.execute(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = current_schema() AND table_name = 'users')"
                )
                if pc.fetchone()[0]:
                    pc.execute("SELECT COUNT(*) FROM users")
                    n = pc.fetchone()[0]
                    if n > 0:
                        log.info(f"PostgreSQL уже содержит данные ({n} пользователей) — перенос не требуется")
                        return False
    except psycopg.Error as e:
        log.error(f"Не удалось проверить состояние базы: {e}")
        return False

    log.warning("=" * 60)
    log.warning("ПЕРВЫЙ ЗАПУСК: переношу данные из SQLite в PostgreSQL")
    log.warning("=" * 60)

    slite = sqlite3.connect(sqlite_path)
    tables = get_sqlite_tables(slite)
    pg = psycopg.connect(dsn)
    cur = pg.cursor()

    # Схема
    for name, ddl in tables:
        if not ddl:
            continue
        pg_ddl = translate_sql(ddl)
        if "IF NOT EXISTS" not in pg_ddl.upper():
            pg_ddl = pg_ddl.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS", 1)
        try:
            cur.execute(pg_ddl)
        except psycopg.Error as e:
            pg.rollback()
            log.error(f"Ошибка создания таблицы {name}: {e}")
            return False
    pg.commit()

    # Данные
    problems = []
    total_src = total_dst = 0
    for name, _ in tables:
        cols = get_columns(slite, name)
        int_idx = get_int_columns(slite, name)
        rows = slite.execute(f'SELECT * FROM "{name}"').fetchall()
        rows = [sanitize_row(r, int_idx, name, cols, problems) for r in rows]
        if not rows:
            continue
        collist = ", ".join(f'"{c}"' for c in cols)
        ph = ", ".join(["%s"] * len(cols))
        try:
            cur.executemany(
                f'INSERT INTO "{name}" ({collist}) VALUES ({ph})',
                [tuple(r) for r in rows],
            )
            pg.commit()
        except psycopg.Error as e:
            pg.rollback()
            log.error(f"Ошибка переноса {name}: {e}")
            continue
        cur.execute(f'SELECT COUNT(*) FROM "{name}"')
        got = cur.fetchone()[0]
        total_src += len(rows)
        total_dst += got
        log.warning(f"  {name:22} {len(rows):>5} → {got:>5}")

    # Счётчики id
    for name, _ in tables:
        if "id" not in get_columns(slite, name):
            continue
        try:
            cur.execute("SELECT pg_get_serial_sequence(%s, 'id')", (name,))
            seq = cur.fetchone()[0]
            if not seq:
                continue
            cur.execute(f'SELECT COALESCE(MAX(id), 0) FROM "{name}"')
            mx = cur.fetchone()[0]
            cur.execute("SELECT setval(%s, %s, true)", (seq, max(mx, 1)))
            pg.commit()
        except psycopg.Error:
            pg.rollback()

    log.warning("-" * 60)
    log.warning(f"ПЕРЕНЕСЕНО: {total_dst} строк из {total_src}")
    if problems:
        log.warning(f"Исправлено значений с неверным типом: {len(problems)}")
        for p in problems[:10]:
            log.warning(f"  • {p}")
    log.warning("Перенос завершён. Файл conference.db можно удалить после проверки.")
    log.warning("=" * 60)

    slite.close()
    pg.close()
    return True


if __name__ == "__main__":
    sys.exit(main())
