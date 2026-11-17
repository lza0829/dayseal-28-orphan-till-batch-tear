from __future__ import annotations
from app.db import connect


SCHEMA = '''
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tills (
  id INTEGER PRIMARY KEY,
  till_code TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cash_lines (
  id INTEGER PRIMARY KEY,
  line_no TEXT NOT NULL UNIQUE,
  till_code TEXT NOT NULL,
  business_day TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  amount REAL NOT NULL,
  pay_method TEXT NOT NULL,
  note TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS close_batches (
  id INTEGER PRIMARY KEY,
  batch_no TEXT NOT NULL UNIQUE,
  business_day TEXT NOT NULL,
  scope TEXT NOT NULL DEFAULT 'day',
  till_code TEXT,
  system_total REAL NOT NULL,
  declared_cash REAL NOT NULL,
  variance REAL NOT NULL,
  line_count INTEGER NOT NULL,
  status TEXT NOT NULL,
  lock_state TEXT NOT NULL DEFAULT 'sealed',
  locked_day INTEGER NOT NULL DEFAULT 1,
  locked_till INTEGER NOT NULL DEFAULT 0,
  sealed_at TEXT NOT NULL,
  note TEXT
);
CREATE TABLE IF NOT EXISTS variance_rows (
  id INTEGER PRIMARY KEY,
  batch_id INTEGER NOT NULL,
  business_day TEXT NOT NULL,
  scope TEXT NOT NULL DEFAULT 'day',
  till_code TEXT,
  variance REAL NOT NULL,
  disposition TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS variance_holds (
  id INTEGER PRIMARY KEY,
  business_day TEXT NOT NULL,
  reason TEXT
);
CREATE TABLE IF NOT EXISTS reopen_requests (
  id INTEGER PRIMARY KEY,
  business_day TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'none'
);
-- 分机先封策略下，日封时对「当日无流水机具」的显式收口登记
CREATE TABLE IF NOT EXISTS close_waives (
  id INTEGER PRIMARY KEY,
  business_day TEXT NOT NULL,
  till_code TEXT NOT NULL,
  batch_id INTEGER,
  created_at TEXT NOT NULL,
  UNIQUE(business_day, till_code)
);
'''

# 索引统一由 migrate() 在列补齐之后创建（旧库直接建局部索引会因缺列失败）
INDEX_DDL = [
    "CREATE INDEX IF NOT EXISTS idx_cash_lines_day_till ON cash_lines(business_day, till_code)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_day_batch ON close_batches(business_day) WHERE scope='day'",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_till_batch ON close_batches(business_day, till_code) WHERE scope='till'",
]


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = _column_names(conn, table)
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def migrate(conn: sqlite3.Connection) -> None:
    """对既有库做幂等升级；新库由 SCHEMA 直接建出。"""
    _add_columns(conn, "close_batches", {
        "scope": "TEXT NOT NULL DEFAULT 'day'",
        "till_code": "TEXT",
        "lock_state": "TEXT NOT NULL DEFAULT 'sealed'",
        "locked_day": "INTEGER NOT NULL DEFAULT 1",
        "locked_till": "INTEGER NOT NULL DEFAULT 0",
    })
    _add_columns(conn, "variance_rows", {
        "scope": "TEXT NOT NULL DEFAULT 'day'",
        "till_code": "TEXT",
    })
    conn.execute(
        '''CREATE TABLE IF NOT EXISTS close_waives (
             id INTEGER PRIMARY KEY,
             business_day TEXT NOT NULL,
             till_code TEXT NOT NULL,
             batch_id INTEGER,
             created_at TEXT NOT NULL,
             UNIQUE(business_day, till_code)
           )'''
    )
    for ddl in INDEX_DDL:
        conn.execute(ddl)
    # 进程崩溃可能留下 sealing 行；sealing 不持锁，启动时清掉以便重试。
    conn.execute("DELETE FROM close_batches WHERE lock_state='sealing'")


def seed_if_empty() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        migrate(conn)
        n = conn.execute("SELECT COUNT(*) c FROM tills").fetchone()["c"]
        if n:
            conn.commit()
            return
        conn.execute(
            "INSERT INTO settings(key, value) VALUES ('business_day', ?)",
            ("2026-11-16",),
        )
        # 封账范围策略：store=整店日封（默认），till=分机先封
        conn.execute(
            "INSERT INTO settings(key, value) VALUES ('close_strategy', 'store')",
        )
        tills = [
            ("TILL-A", "一号收银"),
            ("TILL-B", "二号收银"),
            ("TILL-C", "三号收银"),
        ]
        for code, title in tills:
            conn.execute("INSERT INTO tills(till_code, title) VALUES (?,?)", (code, title))
        lines = [
            ("CL-0001", "TILL-A", "2026-11-16", "2026-11-16T09:12:00Z", 35.5, "现金", "早市"),
            ("CL-0002", "TILL-A", "2026-11-16", "2026-11-16T10:05:00Z", 128.0, "微信", None),
            ("CL-0003", "TILL-B", "2026-11-16", "2026-11-16T11:20:00Z", 66.0, "现金", None),
            ("CL-0004", "TILL-B", "2026-11-16", "2026-11-16T14:02:00Z", 210.0, "支付宝", None),
            ("CL-0005", "TILL-C", "2026-11-16", "2026-11-16T16:40:00Z", 49.9, "现金", None),
            ("CL-0006", "TILL-A", "2026-11-15", "2026-11-15T18:10:00Z", 88.0, "现金", "昨日"),
        ]
        for row in lines:
            conn.execute(
                '''INSERT INTO cash_lines(line_no, till_code, business_day, occurred_at, amount, pay_method, note, created_at)
                   VALUES (?,?,?,?,?,?,?,?)''',
                (*row, row[3]),
            )
        conn.commit()
    finally:
        conn.close()
