"""SQLite 存储层。

选择标准库 sqlite3：单一文件、事务语义清晰、便于在工单现场部署，也不需要外部服务。
所有时间都以带时区的 ISO 8601 文本保存，读取时统一转成 UTC 感知时间。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inventory_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    partner     TEXT NOT NULL,
    item        TEXT NOT NULL,
    quantity    INTEGER NOT NULL CHECK (quantity >= 0),
    checked_at  TEXT NOT NULL,
    source      TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    run_id      TEXT NOT NULL,
    raw         TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_inventory_item
    ON inventory_snapshots(item, checked_at DESC);
CREATE INDEX IF NOT EXISTS idx_inventory_partner_item
    ON inventory_snapshots(partner, item, checked_at DESC);

-- 目录修订逐条保留；valid_to 为空表示当前有效版本。
CREATE TABLE IF NOT EXISTS catalog_revisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    part          TEXT NOT NULL,
    model         TEXT NOT NULL,
    firmware_from TEXT,
    firmware_to   TEXT,
    replacement   TEXT,
    revision      TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active',  -- active | discontinued
    source        TEXT NOT NULL,
    ingested_at   TEXT NOT NULL,
    valid_from    TEXT NOT NULL,
    valid_to      TEXT,
    retracted     INTEGER NOT NULL DEFAULT 0,
    retracted_at  TEXT,
    retraction_reason TEXT,
    run_id        TEXT NOT NULL,
    raw           TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_catalog_lookup
    ON catalog_revisions(part, model, valid_to);
CREATE INDEX IF NOT EXISTS idx_catalog_replacement
    ON catalog_revisions(replacement, valid_to);
CREATE INDEX IF NOT EXISTS idx_catalog_revision_key
    ON catalog_revisions(part, model, revision, valid_to);

CREATE TABLE IF NOT EXISTS catalog_ingest_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    received_at TEXT NOT NULL,
    digest      TEXT NOT NULL,
    row_count   INTEGER NOT NULL,
    changed     INTEGER NOT NULL DEFAULT 0,
    detail      TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS tickets (
    ticket      TEXT PRIMARY KEY,
    asset       TEXT,
    model       TEXT,
    part        TEXT,
    firmware    TEXT,
    fault       TEXT,
    warranty    TEXT,
    -- 服务工单回执里的原始状态，只用于展示与对照，不直接驱动内部状态机
    source_reservation TEXT,
    source_dispatch    TEXT,
    -- 本系统维护的状态
    dispatch_state TEXT NOT NULL DEFAULT 'draft',
    reserved_part TEXT,
    status_note TEXT,
    site_config TEXT NOT NULL DEFAULT '{}',
    config_source TEXT,
    config_revision INTEGER NOT NULL DEFAULT 0,
    -- 资产管理员确认时冻结的配置版本号；当前版本更大即视为确认已过期
    confirmed_config_revision INTEGER,
    confirmed_config_at TEXT,
    appointment_start TEXT,
    appointment_end   TEXT,
    appointment_source TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS confirmations (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket    TEXT NOT NULL REFERENCES tickets(ticket),
    kind      TEXT NOT NULL,       -- asset | warranty | part
    status    TEXT NOT NULL,       -- confirmed | rejected | review
    actor     TEXT NOT NULL,
    role      TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    details   TEXT NOT NULL DEFAULT '{}',
    note      TEXT
);
CREATE INDEX IF NOT EXISTS idx_confirmations_ticket
    ON confirmations(ticket, kind, decided_at DESC);

CREATE TABLE IF NOT EXISTS reservations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket       TEXT NOT NULL REFERENCES tickets(ticket),
    partner      TEXT NOT NULL,
    item         TEXT NOT NULL,
    request_key  TEXT NOT NULL,
    state        TEXT NOT NULL,
    portal_ref   TEXT,
    requested_at TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    quantity     INTEGER NOT NULL DEFAULT 1,
    response     TEXT NOT NULL DEFAULT '{}',
    UNIQUE (ticket, request_key)
);

CREATE TABLE IF NOT EXISTS reservation_attempts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id INTEGER NOT NULL REFERENCES reservations(id),
    attempt_no     INTEGER NOT NULL,
    request_key    TEXT NOT NULL,
    state_before   TEXT,
    sent_at        TEXT NOT NULL,
    outcome        TEXT NOT NULL,  -- held | conflict | failed | pending | reconciled
    portal_ref     TEXT,
    response       TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS dispatch_checks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket     TEXT NOT NULL REFERENCES tickets(ticket),
    checked_at TEXT NOT NULL,
    actor      TEXT NOT NULL,
    verdict    TEXT NOT NULL,       -- ready | pending | blocked | stale | conflict
    result     TEXT NOT NULL,
    snapshot_ids TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_dispatch_checks_ticket
    ON dispatch_checks(ticket, checked_at DESC);

CREATE TABLE IF NOT EXISTS events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket TEXT,
    at     TEXT NOT NULL,
    kind   TEXT NOT NULL,
    actor  TEXT,
    role   TEXT,
    message TEXT NOT NULL,
    data   TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_ticket ON events(ticket, at);

CREATE TABLE IF NOT EXISTS ingest_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job         TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    ok          INTEGER,
    detail      TEXT NOT NULL DEFAULT '{}'
);
"""


def __getattr__(name):
    # 兼容旧的导入路径 db.FixedClock
    if name == "FixedClock":
        from .clock import FixedClock

        return FixedClock
    raise AttributeError(name)


def utcnow():
    return datetime.now(timezone.utc)


def to_iso(dt):
    if dt.tzinfo is None:
        raise ValueError("时间必须带时区")
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_time(value):
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # 允许只有日期
    if len(text) == 10:
        text += "T00:00:00+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError(f"缺少时区：{value!r}")
    return parsed


class Clock:
    def now(self):
        return utcnow()


class Database:
    """每个线程一条连接。

    SQLite 连接不能在多个线程间共享事务状态；客服台又是多窗口并发的。
    用 thread-local 连接 + WAL + busy_timeout，让并发写排队而不是报
    "database is locked"。内存库无法跨线程共享，仍用单连接并串行化。
    """

    def __init__(self, path=":memory:"):
        self.path = str(path)
        self._local = threading.local()
        self._shared = None
        self._lock = threading.RLock()
        self._memory = self.path in (":memory:", "") or "mode=memory" in self.path
        if not self._memory and not self.path.startswith("file:"):
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        if self._memory:
            self._shared = self._connect()
        else:
            self._connect()

    def _open(self):
        if self.path.startswith("file:") or self._memory:
            conn = sqlite3.connect(self.path, uri=True, isolation_level=None,
                                   check_same_thread=False, timeout=10)
        else:
            conn = sqlite3.connect(self.path, isolation_level=None,
                                   check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def _connect(self):
        conn = self._open()
        if not self._memory:
            conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        if not self._memory:
            self._local.conn = conn
        return conn

    @property
    def conn(self):
        if self._memory:
            return self._shared
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
        return conn

    def close(self):
        if self._memory:
            if self._shared is not None:
                self._shared.close()
                self._shared = None
            return
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @contextmanager
    def tx(self):
        """立即取写锁，避免两个客服同时为同一张工单预留或派单。"""
        lock = self._lock if self._memory else nullcontext()
        with lock:
            conn = self.conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def query(self, sql, params=()):
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()

    def insert(self, table, values):
        columns = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        cur = self.conn.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(values.values())
        )
        return cur.lastrowid

    def update(self, table, key_column, key_value, values):
        if not values:
            return
        assignments = ", ".join(f"{col} = ?" for col in values)
        self.conn.execute(
            f"UPDATE {table} SET {assignments} WHERE {key_column} = ?",
            (*values.values(), key_value),
        )

    def event(self, *, ticket, at, kind, message, actor=None, role=None, data=None):
        self.insert(
            "events",
            {
                "ticket": ticket,
                "at": to_iso(at),
                "kind": kind,
                "actor": actor,
                "role": role,
                "message": message,
                "data": json.dumps(data or {}, ensure_ascii=False, sort_keys=True),
            },
        )

    def set_meta(self, key, value):
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value),
        )

    def get_meta(self, key, default=None):
        row = self.one("SELECT value FROM meta WHERE key = ?", (key,))
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return row["value"]
