"""SQLite 连接与模式。"""
import sqlite3

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    uploaded_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS segment_rows (
    segment_id INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    page_id TEXT NOT NULL,
    PRIMARY KEY (segment_id, position)
);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY,
    page_a TEXT NOT NULL,
    page_b TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (page_a, page_b)
);
CREATE TABLE IF NOT EXISTS transcripts (
    page_id TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def get_db():
    """每请求一个连接。autocommit 模式，事务由路由显式 BEGIN IMMEDIATE 控制。"""
    if "db" not in g:
        conn = sqlite3.connect(
            current_app.config["DATABASE"],
            timeout=30,  # 等锁上限：并发写者在 BEGIN IMMEDIATE 处排队
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        g.db = conn
    return g.db


def close_db(exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_app(app):
    app.teardown_appcontext(close_db)
    with app.app_context():
        conn = get_db()
        conn.executescript(SCHEMA)
        conn.execute("PRAGMA journal_mode = WAL")
