import json
import os
import sqlite3
from pathlib import Path
from typing import Any


def get_db_path() -> Path:
    db_env = os.environ.get("DB_PATH", "./data/witnessed.db")
    path = Path(db_env)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def get_data_dir() -> Path:
    data_env = os.environ.get("DATA_DIR", "./data")
    path = Path(data_env)
    path.mkdir(parents=True, exist_ok=True)
    (path / "photos").mkdir(parents=True, exist_ok=True)
    return path


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(get_db_path(), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db() -> None:
    conn = get_connection()
    with conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS claims (
            id TEXT PRIMARY KEY,
            make TEXT,
            model TEXT,
            color TEXT,
            plate TEXT,
            state TEXT,
            claimant_name TEXT,
            claimant_statement TEXT,
            claimed_damage_area TEXT,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            package_bytes BLOB,
            signature TEXT,
            ai_summary TEXT
        );

        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            claim_id TEXT NOT NULL REFERENCES claims(id),
            session_code TEXT NOT NULL,
            status TEXT NOT NULL,
            retake_counts TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_token TEXT NOT NULL REFERENCES sessions(token),
            slot TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            path TEXT NOT NULL,
            reading_json TEXT,
            status TEXT NOT NULL,
            model TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS findings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id TEXT NOT NULL REFERENCES claims(id),
            check_name TEXT NOT NULL,
            status TEXT NOT NULL,
            reason TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id TEXT NOT NULL REFERENCES claims(id),
            action TEXT NOT NULL,
            note TEXT,
            timestamp TEXT NOT NULL
        );
        """)
        try:
            conn.execute("ALTER TABLE claims ADD COLUMN ai_summary TEXT;")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE photos ADD COLUMN model TEXT;")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE findings ADD COLUMN adjuster_label TEXT;")
        except Exception:
            pass
    conn.close()
