from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "trips.db"


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS trips (
                chat_id           INTEGER PRIMARY KEY,
                destination       TEXT NOT NULL,
                currency          TEXT NOT NULL,
                joiners           TEXT NOT NULL,
                pinned_message_id INTEGER,
                created_at        TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS entries (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id            INTEGER NOT NULL,
                created_at         TEXT NOT NULL,
                payer              TEXT NOT NULL,
                debtors            TEXT NOT NULL,
                amount_per_debtor  REAL NOT NULL,
                raw_text           TEXT NOT NULL,
                FOREIGN KEY (chat_id) REFERENCES trips(chat_id)
            );
            CREATE INDEX IF NOT EXISTS idx_entries_chat ON entries(chat_id, id);
            """
        )


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def save_trip(
    chat_id: int,
    destination: str,
    currency: str,
    joiners: list[str],
    pinned_message_id: int | None = None,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO trips (chat_id, destination, currency, joiners, pinned_message_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                destination       = excluded.destination,
                currency          = excluded.currency,
                joiners           = excluded.joiners,
                pinned_message_id = excluded.pinned_message_id
            """,
            (chat_id, destination, currency, json.dumps(joiners), pinned_message_id, _now()),
        )


def get_trip(chat_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT destination, currency, joiners, pinned_message_id FROM trips WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "destination": row["destination"],
        "currency": row["currency"],
        "joiners": json.loads(row["joiners"]),
        "pinned_message_id": row["pinned_message_id"],
    }


def add_entry(
    chat_id: int,
    payer: str,
    debtors: list[str],
    amount_per_debtor: float,
    raw_text: str,
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO entries (chat_id, created_at, payer, debtors, amount_per_debtor, raw_text)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (chat_id, _now(), payer, json.dumps(debtors), amount_per_debtor, raw_text),
        )
        return cur.lastrowid


def list_entries(chat_id: int, limit: int = 100) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, created_at, payer, debtors, amount_per_debtor, raw_text "
            "FROM entries WHERE chat_id = ? ORDER BY id ASC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "created_at": r["created_at"],
            "payer": r["payer"],
            "debtors": json.loads(r["debtors"]),
            "amount_per_debtor": r["amount_per_debtor"],
            "raw_text": r["raw_text"],
        }
        for r in rows
    ]


def delete_last_entry(chat_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, raw_text FROM entries WHERE chat_id = ? ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM entries WHERE id = ?", (row["id"],))
        return {"id": row["id"], "raw_text": row["raw_text"]}
