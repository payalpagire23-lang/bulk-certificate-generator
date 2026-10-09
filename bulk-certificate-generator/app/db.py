"""SQLite helpers. Every thread/request opens its own connection (sqlite3 connections are not thread-safe)."""
import os
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    event_name    TEXT NOT NULL,
    title         TEXT NOT NULL,
    issuer        TEXT NOT NULL,
    issue_date    TEXT NOT NULL,
    status        TEXT NOT NULL,          -- pending | processing | completed | completed_with_errors | failed
    total         INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    started_at    TEXT,
    completed_at  TEXT
);

CREATE TABLE IF NOT EXISTS certificates (
    id               TEXT PRIMARY KEY,
    job_id           TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    idx              INTEGER NOT NULL,    -- position of the recipient in the request (0-based)
    recipient_name   TEXT,
    recipient_email  TEXT,
    course           TEXT,
    status           TEXT NOT NULL,       -- pending | success | failed
    error            TEXT,
    file_path        TEXT,
    created_at       TEXT NOT NULL,
    generated_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_cert_job_status ON certificates(job_id, status);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")  # readers don't block the writer thread
    return conn


def init_db(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
