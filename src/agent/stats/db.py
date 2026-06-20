"""SQLite stats sink for processed emails.

Schema is intentionally portable (single events table, ISO-8601 text
timestamps, parameterized `?` queries) so that a later migration to
Postgres on AWS is mostly a connection-string + autoincrement swap.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = Path(__file__).parent.parent.parent / "stats.db"


def _db_path() -> Path:
    override = os.getenv("STATS_DB_PATH")
    return Path(override) if override else _DEFAULT_DB_PATH


SCHEMA = """
CREATE TABLE IF NOT EXISTS email_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL,
    gmail_id        TEXT    NOT NULL,
    thread_id       TEXT,
    sender          TEXT,
    sender_domain   TEXT,
    subject         TEXT,
    category        TEXT,
    confidence      REAL,
    action_notes    TEXT,
    trust_phase     TEXT,
    draft_created   INTEGER NOT NULL DEFAULT 0,
    UNIQUE (ts, gmail_id)
);

CREATE INDEX IF NOT EXISTS idx_email_events_ts ON email_events(ts);
CREATE INDEX IF NOT EXISTS idx_email_events_category ON email_events(category);
CREATE INDEX IF NOT EXISTS idx_email_events_gmail_id ON email_events(gmail_id);

CREATE TABLE IF NOT EXISTS user_rules (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    ts   TEXT    NOT NULL,
    rule TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS kv_store (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_HISTORY_ID_KEY = "last_history_id"


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


def record_event(
    *,
    ts: str,
    gmail_id: str,
    thread_id: str | None,
    sender: str,
    sender_domain: str,
    subject: str,
    category: str,
    confidence: float,
    action_notes: str,
    trust_phase: str,
    draft_created: bool,
) -> None:
    try:
        with connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO email_events (
                    ts, gmail_id, thread_id, sender, sender_domain, subject,
                    category, confidence, action_notes, trust_phase, draft_created
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    gmail_id,
                    thread_id,
                    sender,
                    sender_domain,
                    subject,
                    category,
                    round(confidence, 2),
                    action_notes,
                    trust_phase,
                    1 if draft_created else 0,
                ),
            )
    except Exception:
        # Stats are best-effort — never break the agent because the DB hiccuped.
        logger.exception("[db] failed to record event for gmail_id=%s", gmail_id)


def get_totals() -> dict[str, int]:
    with connect() as conn:
        total_events = conn.execute("SELECT COUNT(*) FROM email_events").fetchone()[0]
        unique_emails = conn.execute(
            "SELECT COUNT(DISTINCT gmail_id) FROM email_events"
        ).fetchone()[0]
        drafts_created = conn.execute(
            "SELECT COUNT(*) FROM email_events WHERE draft_created = 1"
        ).fetchone()[0]
        return {
            "total_events": total_events,
            "unique_emails": unique_emails,
            "drafts_created": drafts_created,
        }


def get_category_breakdown() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT category, COUNT(*) AS n, AVG(confidence) AS avg_conf
            FROM email_events
            GROUP BY category
            ORDER BY n DESC
            """
        ).fetchall()
        return [
            {
                "category": r["category"],
                "count": r["n"],
                "avg_confidence": round(r["avg_conf"], 2) if r["avg_conf"] is not None else 0.0,
            }
            for r in rows
        ]


def get_daily_counts(days: int = 14) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT substr(ts, 1, 10) AS day, category, COUNT(*) AS n
            FROM email_events
            GROUP BY day, category
            ORDER BY day ASC
            """
        ).fetchall()
        return [{"day": r["day"], "category": r["category"], "count": r["n"]} for r in rows][
            -days * 7 :
        ]


def get_top_senders(limit: int = 10) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT sender, COUNT(*) AS n,
                   SUM(CASE WHEN category = 'junk' THEN 1 ELSE 0 END) AS junk_n
            FROM email_events
            GROUP BY sender
            ORDER BY n DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [{"sender": r["sender"], "count": r["n"], "junk_count": r["junk_n"]} for r in rows]


def get_events_for_date(date: str) -> list[dict[str, Any]]:
    """Return all events for a given date (YYYY-MM-DD), ordered by time."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT ts, sender, subject, category, confidence,
                   action_notes, draft_created
            FROM email_events
            WHERE substr(ts, 1, 10) = ?
            ORDER BY ts
            """,
            (date,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_category_for_gmail_id(gmail_id: str) -> str | None:
    """Return the most recent classified category for a gmail_id, or None."""
    with connect() as conn:
        row = conn.execute(
            "SELECT category FROM email_events WHERE gmail_id = ? ORDER BY ts DESC LIMIT 1",
            (gmail_id,),
        ).fetchone()
        return row["category"] if row else None


def get_event_for_gmail_id(gmail_id: str) -> dict[str, Any] | None:
    """Return the most recent classifier event for a gmail_id, or None.

    Includes what the agent did (category, action_notes, draft_created) so a
    digest can show the outcome alongside the live Gmail message.
    """
    with connect() as conn:
        row = conn.execute(
            """
            SELECT category, confidence, action_notes, draft_created
            FROM email_events
            WHERE gmail_id = ?
            ORDER BY ts DESC
            LIMIT 1
            """,
            (gmail_id,),
        ).fetchone()
        return dict(row) if row else None


def get_user_rules() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT id, ts, rule FROM user_rules ORDER BY id ASC").fetchall()
        return [dict(r) for r in rows]


def add_user_rule(rule: str) -> int:
    from datetime import UTC, datetime

    ts = datetime.now(UTC).isoformat()
    with connect() as conn:
        cur = conn.execute("INSERT INTO user_rules (ts, rule) VALUES (?, ?)", (ts, rule))
        return cur.lastrowid  # type: ignore[return-value]


def delete_user_rule(rule_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM user_rules WHERE id = ?", (rule_id,))
        return cur.rowcount > 0


def kv_get(key: str) -> str | None:
    """Read an arbitrary value from the kv_store, or None if absent."""
    with connect() as conn:
        row = conn.execute("SELECT value FROM kv_store WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def kv_set(key: str, value: str) -> None:
    """Upsert an arbitrary value into the kv_store."""
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO kv_store (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )


def get_cursor() -> str | None:
    """Return the persisted Gmail history cursor, or None if never set."""
    with connect() as conn:
        row = conn.execute(
            "SELECT value FROM kv_store WHERE key = ?",
            (_HISTORY_ID_KEY,),
        ).fetchone()
        return row["value"] if row else None


def set_cursor(history_id: str) -> None:
    """Persist the latest Gmail history cursor."""
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO kv_store (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (_HISTORY_ID_KEY, history_id),
        )


def message_already_processed(gmail_id: str) -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM email_events WHERE gmail_id = ? LIMIT 1",
            (gmail_id,),
        ).fetchone()
        return row is not None


def get_recent_events(limit: int = 50) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT ts, gmail_id, sender, subject, category, confidence,
                   action_notes, trust_phase, draft_created
            FROM email_events
            ORDER BY ts DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
