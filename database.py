"""
SQLite schema initialisation — PRD §8.3.
Raw sqlite3, no ORM. PostgreSQL migration path: swap connection string only.
"""

import json
import logging
import os
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(os.environ.get("DB_PATH", "gaphunter.db"))

_CREATE_USERS = """
CREATE TABLE IF NOT EXISTS users (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    email            TEXT    UNIQUE NOT NULL,
    hashed_password  TEXT    NOT NULL,
    created_at       TEXT    DEFAULT (datetime('now'))
);
"""

_CREATE_USER_PROFILES = """
CREATE TABLE IF NOT EXISTS user_profiles (
    user_id          INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    skills           TEXT    DEFAULT '[]',
    seniority        TEXT    DEFAULT 'unknown',
    experience_years INTEGER DEFAULT 0,
    updated_at       TEXT    DEFAULT (datetime('now'))
);
"""

_CREATE_SEARCH_HISTORY = """
CREATE TABLE IF NOT EXISTS search_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    session_id TEXT    NOT NULL,
    role       TEXT,
    location   TEXT,
    gaps       TEXT    DEFAULT '[]',
    created_at TEXT    DEFAULT (datetime('now'))
);
"""

_CREATE_SESSION_IDX = """
CREATE INDEX IF NOT EXISTS idx_search_session ON search_history(session_id);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.execute(_CREATE_USERS)
        conn.execute(_CREATE_USER_PROFILES)
        conn.execute(_CREATE_SEARCH_HISTORY)
        conn.execute(_CREATE_SESSION_IDX)
        conn.commit()
    logger.info("SQLite initialised at %s", DB_PATH)


def create_user(email: str, hashed_password: str) -> int | None:
    """Returns new user_id or None if email already exists."""
    try:
        with get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO users (email, hashed_password) VALUES (?, ?)",
                (email.lower().strip(), hashed_password),
            )
            user_id = cur.lastrowid
            conn.execute(
                "INSERT INTO user_profiles (user_id) VALUES (?)", (user_id,)
            )
            conn.commit()
            return user_id
    except sqlite3.IntegrityError:
        return None


def get_user_by_email(email: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, email, hashed_password FROM users WHERE email = ?",
            (email.lower().strip(),),
        ).fetchone()
    return dict(row) if row else None


def save_search(
    session_id: str,
    role: str,
    location: str,
    gaps: list[dict],
    user_id: int | None = None,
) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO search_history (user_id, session_id, role, location, gaps) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, session_id, role, location, json.dumps(gaps)),
        )
        conn.commit()


def update_profile(user_id: int, skills: list[str], seniority: str, experience_years: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO user_profiles (user_id, skills, seniority, experience_years, updated_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            (user_id, json.dumps(skills), seniority, experience_years),
        )
        conn.commit()
