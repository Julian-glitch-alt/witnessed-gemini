from datetime import date, datetime, timedelta, timezone
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
            ai_summary TEXT,
            answers_json TEXT,
            signer_name TEXT,
            signature_image TEXT,
            signed_at TEXT
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

        CREATE TABLE IF NOT EXISTS claim_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id TEXT NOT NULL REFERENCES claims(id),
            author TEXT,
            note TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS claim_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id TEXT NOT NULL REFERENCES claims(id),
            event_type TEXT NOT NULL,
            detail TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS policies (
            policy_number TEXT PRIMARY KEY,
            insured_name TEXT NOT NULL,
            vehicle_year INTEGER NOT NULL,
            vehicle_make TEXT NOT NULL,
            vehicle_model TEXT NOT NULL,
            vehicle_color TEXT NOT NULL,
            vin TEXT,
            plate TEXT NOT NULL,
            state TEXT NOT NULL,
            collision INTEGER NOT NULL DEFAULT 0,
            comprehensive INTEGER NOT NULL DEFAULT 0,
            liability_only INTEGER NOT NULL DEFAULT 0,
            deductible INTEGER NOT NULL DEFAULT 500,
            lienholder TEXT,
            effective_date TEXT NOT NULL,
            expiry_date TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """)
        try:
            conn.execute("ALTER TABLE claims ADD COLUMN ai_summary TEXT;")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE claims ADD COLUMN policy_number TEXT;")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE claims ADD COLUMN policy_snapshot_json TEXT;")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE claims ADD COLUMN vehicle_year INTEGER;")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE claims ADD COLUMN vin TEXT;")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE photos ADD COLUMN model TEXT;")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE photos ADD COLUMN round INTEGER DEFAULT 1;")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE findings ADD COLUMN adjuster_label TEXT;")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE claims ADD COLUMN answers_json TEXT;")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE claims ADD COLUMN signer_name TEXT;")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE claims ADD COLUMN signature_image TEXT;")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE claims ADD COLUMN signed_at TEXT;")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE sessions ADD COLUMN requested_slots TEXT;")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE sessions ADD COLUMN round INTEGER DEFAULT 1;")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE claims ADD COLUMN round INTEGER DEFAULT 1;")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE claims ADD COLUMN previous_signature TEXT;")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE claims ADD COLUMN version INTEGER DEFAULT 1;")
        except Exception:
            pass
        seed_demo_policies(conn)
    conn.close()


def seed_demo_policies(conn: sqlite3.Connection) -> None:
    now = datetime.now(timezone.utc)
    today = now.date()

    eff_active = min(date(2026, 1, 1), today - timedelta(days=180)).isoformat()
    exp_active = max(date(2027, 1, 1), today + timedelta(days=185)).isoformat()

    first_of_month = today.replace(day=1)
    exp_1004_date = first_of_month - timedelta(days=15)
    exp_1004 = min(date(2026, 10, 15), exp_1004_date).isoformat()
    eff_1004 = (date.fromisoformat(exp_1004) - timedelta(days=365)).isoformat()

    demo_data = [
        (
            "DEMO-AUTO-1001",
            "Jane Doe",
            2020,
            "Toyota",
            "Camry",
            "blue",
            "1HGCM82633A004352",
            "7XYZ123",
            "CA",
            1,
            1,
            0,
            500,
            None,
            eff_active,
            exp_active,
            now.isoformat(),
        ),
        (
            "DEMO-AUTO-1002",
            "Bob Miller",
            2019,
            "Honda",
            "Civic",
            "white",
            None,
            "8KLM204",
            "CA",
            1,
            1,
            0,
            1000,
            None,
            eff_active,
            exp_active,
            now.isoformat(),
        ),
        (
            "DEMO-AUTO-1003",
            "Alice Brown",
            2018,
            "Ford",
            "F-150",
            "black",
            None,
            "6TRK889",
            "CA",
            0,
            0,
            1,
            0,
            None,
            eff_active,
            exp_active,
            now.isoformat(),
        ),
        (
            "DEMO-AUTO-1004",
            "David Clark",
            2017,
            "Nissan",
            "Altima",
            "silver",
            None,
            "5ALT321",
            "CA",
            1,
            0,
            0,
            500,
            None,
            eff_1004,
            exp_1004,
            now.isoformat(),
        ),
    ]
    for row in demo_data:
        conn.execute(
            """
            INSERT OR IGNORE INTO policies (
                policy_number, insured_name, vehicle_year, vehicle_make, vehicle_model,
                vehicle_color, vin, plate, state, collision, comprehensive, liability_only,
                deductible, lienholder, effective_date, expiry_date, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            row,
        )


def log_claim_event(claim_id: str, event_type: str, detail: str | None = None) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    with conn:
        conn.execute(
            """
            INSERT INTO claim_events (claim_id, event_type, detail, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (claim_id, event_type, detail, now_iso),
        )
    conn.close()
