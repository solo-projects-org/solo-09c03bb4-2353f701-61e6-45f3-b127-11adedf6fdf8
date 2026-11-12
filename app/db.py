"""SQLite 持久化：书目记录、采集进度、采集运行历史。

所有写操作在单连接单事务内完成，保证“整批采完才提交”。
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

DB_PATH = os.environ.get("APP_DB_PATH", "/data/harvester.db")

# 支持撤销的采集批次格式版本；升级前写入的旧批次该字段为 NULL，不可撤销
SNAPSHOT_VERSION = 1

# bib_records 全列，用于快照与整行恢复
BIB_COLUMNS = (
    "identifier", "datestamp", "status", "title", "creator", "publisher",
    "subject", "description", "date", "type", "format", "identifier_dc",
    "source", "language", "relation", "coverage", "rights", "raw_dc",
    "updated_at",
)

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS bib_records (
    identifier      TEXT PRIMARY KEY,            -- OAI 记录唯一标识
    datestamp       TEXT NOT NULL,               -- OAI header datestamp
    status          TEXT NOT NULL,               -- active / withdrawn
    title           TEXT,
    creator         TEXT,
    publisher       TEXT,
    subject         TEXT,
    description     TEXT,
    date            TEXT,
    type            TEXT,
    format          TEXT,
    identifier_dc   TEXT,
    source          TEXT,
    language        TEXT,
    relation        TEXT,
    coverage        TEXT,
    rights          TEXT,
    raw_dc          TEXT NOT NULL,               -- 原始 Dublin Core JSON
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS harvest_progress (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    last_until      TEXT NOT NULL,               -- 上次成功采集的截止日（含）
    last_harvest_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS harvest_runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    from_date        TEXT NOT NULL,
    until_date       TEXT NOT NULL,
    status           TEXT NOT NULL,               -- running / success / failed / revoked
    pages            INTEGER NOT NULL DEFAULT 0,
    fetched          INTEGER NOT NULL DEFAULT 0,
    inserted         INTEGER NOT NULL DEFAULT 0,
    updated          INTEGER NOT NULL DEFAULT 0,
    withdrawn        INTEGER NOT NULL DEFAULT 0,
    retries          INTEGER NOT NULL DEFAULT 0,
    error            TEXT,
    snapshot_version INTEGER,                     -- 非 NULL 表示批次可撤销
    prev_last_until  TEXT,                        -- 批前续采进度 last_until
    prev_harvest_at  TEXT,                        -- 批前续采进度 last_harvest_at
    revoked_at       TEXT
);

-- 每批成功采集落库前的书目快照：pre_image 非空=批前已有该行（改写恢复用）；
-- pre_image 为 NULL=批前不存在、本批新增（撤销时删除）
CREATE TABLE IF NOT EXISTS bib_snapshots (
    run_id      INTEGER NOT NULL,
    identifier  TEXT NOT NULL,
    pre_image   TEXT,
    PRIMARY KEY (run_id, identifier)
);
"""

# 升级迁移：旧库只补列/补表，不动任何已有数据
_MIGRATION_COLUMNS = {
    "harvest_runs": (
        ("snapshot_version", "INTEGER"),
        ("prev_last_until", "TEXT"),
        ("prev_harvest_at", "TEXT"),
        ("revoked_at", "TEXT"),
    ),
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_connection(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(db_path: str | None = None) -> None:
    conn = get_connection(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
        # 升级已有库：补齐旧版本缺失的列（ADD COLUMN 不影响既有数据）
        for table, columns in _MIGRATION_COLUMNS.items():
            existing = {
                row["name"] for row in conn.execute(
                    f"PRAGMA table_info({table})"
                ).fetchall()
            }
            for name, decl in columns:
                if name not in existing:
                    conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {decl}"
                    )
        conn.commit()
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式事务上下文：异常回滚，正常时由调用方负责 commit。"""
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
    except Exception:
        conn.rollback()
        raise


def get_last_until(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT last_until FROM harvest_progress WHERE id = 1"
    ).fetchone()
    return row["last_until"] if row else None


def advance_progress(conn: sqlite3.Connection, until_date: str) -> None:
    """整批成功后推进截止日（单行 upsert）。"""
    conn.execute(
        """
        INSERT INTO harvest_progress (id, last_until, last_harvest_at)
        VALUES (1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            last_until = excluded.last_until,
            last_harvest_at = excluded.last_harvest_at
        """,
        (until_date, utc_now_iso()),
    )


def start_run(conn: sqlite3.Connection, from_date: str, until_date: str) -> int:
    cur = conn.execute(
        """
        INSERT INTO harvest_runs (started_at, from_date, until_date, status)
        VALUES (?, ?, ?, 'running')
        """,
        (utc_now_iso(), from_date, until_date),
    )
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, *, status: str,
               pages: int, fetched: int, inserted: int, updated: int,
               withdrawn: int, retries: int, error: str | None,
               snapshot_version: int | None = None,
               prev_last_until: str | None = None,
               prev_harvest_at: str | None = None) -> None:
    conn.execute(
        """
        UPDATE harvest_runs SET
            finished_at = ?, status = ?, pages = ?, fetched = ?,
            inserted = ?, updated = ?, withdrawn = ?, retries = ?, error = ?,
            snapshot_version = COALESCE(?, snapshot_version),
            prev_last_until = ?, prev_harvest_at = ?
        WHERE id = ?
        """,
        (utc_now_iso(), status, pages, fetched, inserted, updated,
         withdrawn, retries, error, snapshot_version,
         prev_last_until, prev_harvest_at, run_id),
    )


def get_progress_snapshot(conn: sqlite3.Connection) -> tuple[str | None, str | None]:
    """读取批前续采进度（不存在则两个值都为 None）。"""
    row = conn.execute(
        "SELECT last_until, last_harvest_at FROM harvest_progress WHERE id = 1"
    ).fetchone()
    if row is None:
        return None, None
    return row["last_until"], row["last_harvest_at"]


def snapshot_bib_record(conn: sqlite3.Connection, *, run_id: int,
                        identifier: str) -> bool:
    """落库前保存某条书目的批前整行；本批新增标识（无旧行）返回 False。

    同一 run 对同一标识只快照一次，保留最早的批前状态。
    """
    row = conn.execute(
        f"SELECT {', '.join(BIB_COLUMNS)} FROM bib_records WHERE identifier = ?",
        (identifier,),
    ).fetchone()
    if row is None:
        return False
    conn.execute(
        """
        INSERT OR IGNORE INTO bib_snapshots (run_id, identifier, pre_image)
        VALUES (?, ?, ?)
        """,
        (run_id, identifier, json.dumps(dict(row), ensure_ascii=False)),
    )
    return True


def get_run(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM harvest_runs WHERE id = ?", (run_id,)
    ).fetchone()


def get_latest_terminal_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """最近一条已结束的成功/已撤销批次（failed 批次不参与，不影响撤销）。"""
    return conn.execute(
        """
        SELECT * FROM harvest_runs
        WHERE status IN ('success', 'revoked')
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()


def restore_progress(conn: sqlite3.Connection,
                     prev_last_until: str | None,
                     prev_harvest_at: str | None) -> None:
    """把续采进度恢复到批前；批前没有进度则删除进度行（显示未采集）。"""
    if prev_last_until is None:
        conn.execute("DELETE FROM harvest_progress WHERE id = 1")
        return
    conn.execute(
        """
        INSERT INTO harvest_progress (id, last_until, last_harvest_at)
        VALUES (1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            last_until = excluded.last_until,
            last_harvest_at = excluded.last_harvest_at
        """,
        (prev_last_until, prev_harvest_at),
    )


def restore_bib_snapshot(conn: sqlite3.Connection, run_id: int) -> int:
    """按快照恢复该批改写的书目：整行写回批前内容与状态。

    返回恢复的书目数。本批新增行（pre_image 为 NULL）不在此处理。
    """
    rows = conn.execute(
        "SELECT identifier, pre_image FROM bib_snapshots "
        "WHERE run_id = ? AND pre_image IS NOT NULL",
        (run_id,),
    ).fetchall()
    placeholders = ", ".join("?" for _ in BIB_COLUMNS)
    for row in rows:
        pre = json.loads(row["pre_image"])
        conn.execute(
            f"INSERT OR REPLACE INTO bib_records "
            f"({', '.join(BIB_COLUMNS)}) VALUES ({placeholders})",
            tuple(pre[col] for col in BIB_COLUMNS),
        )
    return len(rows)


def insert_run_touched(conn: sqlite3.Connection, *, run_id: int,
                       identifier: str) -> None:
    """登记本批触碰过、但批前不存在的标识（本批新增），供撤销时删除。"""
    conn.execute(
        "INSERT OR IGNORE INTO bib_snapshots (run_id, identifier, pre_image) "
        "VALUES (?, ?, NULL)",
        (run_id, identifier),
    )


def delete_unsnapshotted_touched(conn: sqlite3.Connection, run_id: int) -> int:
    """删除本批新增的书目（快照 pre_image 为 NULL 的触碰标识）。"""
    rows = conn.execute(
        "SELECT identifier FROM bib_snapshots WHERE run_id = ? AND pre_image IS NULL",
        (run_id,),
    ).fetchall()
    for row in rows:
        conn.execute(
            "DELETE FROM bib_records WHERE identifier = ?", (row["identifier"],)
        )
    return len(rows)


def delete_snapshots(conn: sqlite3.Connection, run_id: int) -> None:
    conn.execute("DELETE FROM bib_snapshots WHERE run_id = ?", (run_id,))


def mark_run_revoked(conn: sqlite3.Connection, run_id: int,
                     revoked_at: str | None = None) -> None:
    conn.execute(
        "UPDATE harvest_runs SET status = 'revoked', revoked_at = ? WHERE id = ?",
        (revoked_at or utc_now_iso(), run_id),
    )


def upsert_bib_record(conn: sqlite3.Connection, *, identifier: str,
                      datestamp: str, fields: dict, raw_dc: str) -> str:
    """按标识 upsert 书目；返回 'inserted' 或 'updated'。"""
    existing = conn.execute(
        "SELECT identifier FROM bib_records WHERE identifier = ?",
        (identifier,),
    ).fetchone()
    now = utc_now_iso()
    params = (
        identifier, datestamp, "active",
        fields.get("title"), fields.get("creator"), fields.get("publisher"),
        fields.get("subject"), fields.get("description"), fields.get("date"),
        fields.get("type"), fields.get("format"), fields.get("identifier"),
        fields.get("source"), fields.get("language"), fields.get("relation"),
        fields.get("coverage"), fields.get("rights"), raw_dc, now,
    )
    if existing is None:
        conn.execute(
            """
            INSERT INTO bib_records (
                identifier, datestamp, status, title, creator, publisher,
                subject, description, date, type, format, identifier_dc,
                source, language, relation, coverage, rights, raw_dc, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            params,
        )
        return "inserted"
    conn.execute(
        """
        UPDATE bib_records SET
            datestamp = ?, status = ?, title = ?, creator = ?, publisher = ?,
            subject = ?, description = ?, date = ?, type = ?, format = ?,
            identifier_dc = ?, source = ?, language = ?, relation = ?,
            coverage = ?, rights = ?, raw_dc = ?, updated_at = ?
        WHERE identifier = ?
        """,
        params[1:] + (identifier,),
    )
    return "updated"


def withdraw_bib_record(conn: sqlite3.Connection, *, identifier: str,
                        datestamp: str) -> bool:
    """遇删除标记则撤下（标记 withdrawn）。不存在返回 False。"""
    cur = conn.execute(
        """
        UPDATE bib_records
        SET status = 'withdrawn', datestamp = ?, updated_at = ?
        WHERE identifier = ?
        """,
        (datestamp, utc_now_iso(), identifier),
    )
    return cur.rowcount > 0


def insert_withdrawn_tombstone(conn: sqlite3.Connection, *, identifier: str,
                               datestamp: str) -> None:
    """删除标记记录此前不在库中时，写入 withdrawn 墓碑，保留撤下痕迹。"""
    conn.execute(
        """
        INSERT INTO bib_records (
            identifier, datestamp, status, raw_dc, updated_at
        ) VALUES (?, ?, 'withdrawn', ?, ?)
        ON CONFLICT(identifier) DO NOTHING
        """,
        (identifier, datestamp, "{}", utc_now_iso()),
    )
