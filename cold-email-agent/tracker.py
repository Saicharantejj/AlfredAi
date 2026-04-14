import os
import sqlite3
from datetime import datetime, timezone
import config


def _now():
    return datetime.now(timezone.utc).isoformat()


def _connect():
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _connect()
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sent_emails (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                email       TEXT NOT NULL,
                name        TEXT,
                company     TEXT,
                subject     TEXT,
                body        TEXT,
                message_id  TEXT,
                sent_at     TEXT NOT NULL,
                status      TEXT DEFAULT 'sent'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS replies (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                from_email      TEXT NOT NULL,
                subject         TEXT,
                snippet         TEXT,
                received_at     TEXT NOT NULL,
                sent_email_id   INTEGER REFERENCES sent_emails(id)
            )
        """)
    conn.close()


def record_sent(lead, subject, body, message_id=""):
    conn = _connect()
    with conn:
        cursor = conn.execute(
            "INSERT INTO sent_emails (email, name, company, subject, body, message_id, sent_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (lead["email"], lead.get("name", ""), lead.get("company", ""), subject, body, message_id, _now()),
        )
        row_id = cursor.lastrowid
    conn.close()
    return row_id


def record_failed(lead, subject, body, error):
    conn = _connect()
    with conn:
        conn.execute(
            "INSERT INTO sent_emails (email, name, company, subject, body, message_id, sent_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'failed')",
            (lead["email"], lead.get("name", ""), lead.get("company", ""), subject, body, f"ERROR: {error}", _now()),
        )
    conn.close()


def record_reply(from_email, subject, snippet, sent_email_id):
    conn = _connect()
    with conn:
        conn.execute(
            "INSERT INTO replies (from_email, subject, snippet, received_at, sent_email_id) VALUES (?, ?, ?, ?, ?)",
            (from_email, subject, snippet, _now(), sent_email_id),
        )
        if sent_email_id:
            conn.execute(
                "UPDATE sent_emails SET status = 'replied' WHERE id = ?",
                (sent_email_id,)
            )
    conn.close()


def already_sent(email):
    conn = _connect()
    row = conn.execute(
        "SELECT id FROM sent_emails WHERE email = ? AND status != 'failed' LIMIT 1",
        (email,)
    ).fetchone()
    conn.close()
    return row is not None


def get_sent_by_email(email):
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM sent_emails WHERE email = ? AND status = 'sent' ORDER BY sent_at DESC LIMIT 1",
        (email,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_sent():
    conn = _connect()
    rows = conn.execute("SELECT * FROM sent_emails ORDER BY sent_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_replies():
    conn = _connect()
    rows = conn.execute("SELECT * FROM replies ORDER BY received_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def summary():
    conn = _connect()
    total   = conn.execute("SELECT COUNT(*) FROM sent_emails").fetchone()[0]
    sent_ok = conn.execute("SELECT COUNT(*) FROM sent_emails WHERE status = 'sent'").fetchone()[0]
    failed  = conn.execute("SELECT COUNT(*) FROM sent_emails WHERE status = 'failed'").fetchone()[0]
    replied = conn.execute("SELECT COUNT(*) FROM sent_emails WHERE status = 'replied'").fetchone()[0]
    conn.close()
    return {"total": total, "sent": sent_ok, "failed": failed, "replied": replied}


if __name__ == "__main__":
    init_db()
    print("DB summary:", summary())