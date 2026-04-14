"""
cold_email_agent.py
-------------------
Premium cold-email feature for Alfred.

Each user has:
  - A config doc   → stored in Alfred's user_documents as "cold_email_config.json"
  - A tracker DB   → stored at users/{storage_id}/cold_email.db

Public API used by main.py:
    get_config(storage_id)
    save_config(storage_id, cfg: dict)
    run_campaign(storage_id, leads: list[dict], groq_api_key: str) -> dict
    get_stats(storage_id) -> dict
    get_history(storage_id) -> list
    get_replies(storage_id) -> list
"""

import csv
import io
import json
import os
import re
import smtplib
import sqlite3
import uuid
from datetime import datetime, timezone
from email.header import decode_header as _decode_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, parseaddr
from pathlib import Path
from typing import Any

import httpx
from security_utils import get_safe_user_path

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _db_path(storage_id: str) -> Path:
    base = Path(get_safe_user_path(storage_id, "cold_email.db"))
    base.parent.mkdir(parents=True, exist_ok=True)
    return base


def _connect(storage_id: str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path(storage_id)))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_db(storage_id: str) -> None:
    conn = _connect(storage_id)
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
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                from_email    TEXT NOT NULL,
                subject       TEXT,
                snippet       TEXT,
                received_at   TEXT NOT NULL,
                sent_email_id INTEGER REFERENCES sent_emails(id)
            )
        """)
    conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Config (stored as Alfred user document)
# ---------------------------------------------------------------------------

def _config_path(storage_id: str) -> Path:
    path = Path(get_safe_user_path(storage_id, "cold_email_config.json"))
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def get_config(storage_id: str) -> dict:
    path = _config_path(storage_id)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {}


def save_config(storage_id: str, cfg: dict) -> None:
    _config_path(storage_id).write_text(json.dumps(cfg, indent=2))


# ---------------------------------------------------------------------------
# Email generation via Groq
# ---------------------------------------------------------------------------

def _build_prompt(lead: dict, cfg: dict) -> str:
    name       = lead.get("name", "there")
    company    = lead.get("company", "your company")
    extra      = {k: v for k, v in lead.items() if k not in ("name", "company", "email") and v}
    industry   = extra.get("industry", "their field")
    pain_point = extra.get("pain_point", "")

    extra_context = ""
    if extra:
        lines = [f"  - {k}: {v}" for k, v in extra.items()]
        extra_context = "Additional context about this lead:\n" + "\n".join(lines) + "\n"

    sender_name    = cfg.get("sender_name", "the team")
    sender_role    = cfg.get("sender_role", "Partner")
    company_name   = cfg.get("company_name", "our company")
    value_prop     = cfg.get("value_prop", "We help businesses grow with AI.")

    return f"""
You are {sender_name}, {sender_role} at {company_name}.

Write a cold outreach email to {name} at {company}.

What we do:
{value_prop}

{extra_context}
Rules:
- Write 200-250 words.
- Open with a very specific observation about {company} — something that shows you actually looked them up.
- Do NOT open with their name. Start with the observation.
- Use their exact pain point naturally — {"'" + pain_point + "'" if pain_point else "find one relevant to their industry"}.
- Make them feel like this email was written only for them.
- Tell a mini story or use an analogy relevant to {industry}.
- Reveal the solution only after building curiosity.
- End with one soft CTA like "Worth a quick call this week?"
- Sound like a human founder, not a marketer.
- No clichés, no "Hope this finds you well", no "I came across your profile".
- Do NOT add a signature block.

IMPORTANT: Return ONLY a JSON object on a single line. Use \\n for line breaks inside strings.
Example: {{"subject": "Your subject", "body": "Line one.\\nLine two."}}
""".strip()


def _call_groq(prompt: str, groq_api_key: str) -> str:
    resp = httpx.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {groq_api_key}", "Content-Type": "application/json"},
        json={
            "model": "llama-3.3-70b-versatile",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.9,
            "max_tokens": 800,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def generate_email(lead: dict, cfg: dict, groq_api_key: str) -> dict:
    prompt  = _build_prompt(lead, cfg)
    raw     = _call_groq(prompt, groq_api_key)
    cleaned = re.sub(r"^```[a-z]*\n?", "", raw, flags=re.IGNORECASE)
    cleaned = re.sub(r"\n?```$", "", cleaned).strip()
    cleaned = re.sub(r'(?<!\\)\n\s*(?=")', '', cleaned)
    cleaned = re.sub(r'\n', '\\n', cleaned)

    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError:
        sm = re.search(r'"subject"\s*:\s*"([^"]+)"', cleaned)
        bm = re.search(r'"body"\s*:\s*"(.*?)"(?:\s*})', cleaned, re.DOTALL)
        if sm and bm:
            result = {"subject": sm.group(1), "body": bm.group(1).replace('\\n', '\n')}
        else:
            raise ValueError(f"Model returned invalid JSON.\nRaw:\n{raw}")

    if "subject" not in result or "body" not in result:
        raise ValueError(f"Missing subject or body: {result}")
    return result


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_PORT = 587


def _validate_email(email: str) -> bool:
    """Basic email format validation."""
    if not email or "@" not in email:
        return False
    # Simple regex for basic validation
    return bool(re.match(r"[^@]+@[^@]+\.[^@]+", email))


def _send_email(lead: dict, subject: str, body: str, cfg: dict, storage_id: str) -> bool:
    sender_email    = cfg.get("sender_email", "").strip()
    sender_name     = cfg.get("sender_name", "").strip()
    app_password    = cfg.get("gmail_app_password", "").strip()
    signature_name  = cfg.get("signature_name", sender_name)
    company_name    = cfg.get("company_name", "").strip()

    if not sender_email or not app_password:
        raise ValueError("Sender email and Gmail app password are required in cold email config.")

    email_to = lead.get("email", "").strip()
    if not _validate_email(email_to):
        raise ValueError(f"Invalid lead email address: {email_to}")

    role = cfg.get('sender_role', '')
    signature = f"--\n{signature_name}\n{role + ', ' if role else ''}{company_name}".strip()
    full_body  = f"{body}\n\n{signature}"

    msg = MIMEMultipart("alternative")
    # Generate a proper Message-ID
    domain = sender_email.split('@')[1] if '@' in sender_email else 'alfred.ai'
    message_id = f"<{uuid.uuid4()}@{domain}>"
    
    msg["From"]       = formataddr((sender_name, sender_email))
    msg["To"]         = formataddr((lead.get("name", ""), email_to))
    msg["Subject"]    = subject
    msg["Date"]       = formatdate(localtime=True)
    msg["Message-ID"] = message_id
    
    msg.attach(MIMEText(full_body, "plain", "utf-8"))

    conn = _connect(storage_id)
    try:
        with smtplib.SMTP(GMAIL_SMTP_HOST, GMAIL_SMTP_PORT, timeout=20) as server:
            server.ehlo()
            server.starttls()
            server.login(sender_email, app_password)
            server.sendmail(sender_email, [email_to], msg.as_string())

        with conn:
            conn.execute(
                "INSERT INTO sent_emails (email, name, company, subject, body, message_id, sent_at, status) VALUES (?,?,?,?,?,?,?,?)",
                (email_to, lead.get("name",""), lead.get("company",""), subject, body, message_id, _now(), 'sent'),
            )
        return True

    except smtplib.SMTPAuthenticationError:
        error_msg = "Gmail authentication failed. Please verify your app password."
        with conn:
            conn.execute(
                "INSERT INTO sent_emails (email, name, company, subject, body, message_id, sent_at, status) VALUES (?,?,?,?,?,?,?,'failed')",
                (email_to, lead.get("name",""), lead.get("company",""), subject, body, f"AUTH_ERROR", _now()),
            )
        raise ValueError(error_msg)
    except Exception as exc:
        with conn:
            conn.execute(
                "INSERT INTO sent_emails (email, name, company, subject, body, message_id, sent_at, status) VALUES (?,?,?,?,?,?,?,'failed')",
                (email_to, lead.get("name",""), lead.get("company",""), subject, body, f"ERROR:{exc}", _now()),
            )
        return False
    finally:
        conn.close()


def _already_sent(storage_id: str, email: str) -> bool:
    _init_db(storage_id)
    conn = _connect(storage_id)
    row = conn.execute(
        "SELECT id FROM sent_emails WHERE email = ? AND status != 'failed' LIMIT 1",
        (email,),
    ).fetchone()
    conn.close()
    return row is not None


# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------

def parse_leads_csv(csv_text: str) -> list[dict]:
    """Parse a CSV string into a list of lead dicts. Required column: email."""
    reader = csv.DictReader(io.StringIO(csv_text))
    leads  = []
    for row in reader:
        clean = {k.strip().lower(): (v.strip() if v else "") for k, v in row.items() if k}
        if clean.get("email"):
            leads.append(clean)
    return leads


# ---------------------------------------------------------------------------
# Run campaign
# ---------------------------------------------------------------------------

def run_campaign(storage_id: str, leads: list[dict], groq_api_key: str) -> dict:
    """
    Send personalised cold emails to all leads.
    Returns a summary dict: {sent, skipped, failed, errors: [str]}.
    """
    if not leads:
        return {"sent": 0, "skipped": 0, "failed": 0, "errors": ["No leads provided."]}

    cfg = get_config(storage_id)
    if not cfg.get("sender_email") or not cfg.get("gmail_app_password"):
        return {"sent": 0, "skipped": 0, "failed": 0,
                "errors": ["Cold email is not configured. Please fill in your settings first."]}

    _init_db(storage_id)
    sent = skipped = failed = 0
    errors: list[str] = []

    for lead in leads:
        email_addr = lead.get("email", "").strip()
        if not email_addr:
            skipped += 1
            continue

        if _already_sent(storage_id, email_addr):
            skipped += 1
            continue

        try:
            generated = generate_email(lead, cfg, groq_api_key)
            ok = _send_email(lead, generated["subject"], generated["body"], cfg, storage_id)
            if ok:
                sent += 1
            else:
                failed += 1
        except Exception as exc:
            failed += 1
            errors.append(f"{email_addr}: {exc}")

    return {"sent": sent, "skipped": skipped, "failed": failed, "errors": errors}


# ---------------------------------------------------------------------------
# Stats / history / replies
# ---------------------------------------------------------------------------

def get_stats(storage_id: str) -> dict:
    _init_db(storage_id)
    conn = _connect(storage_id)
    try:
        total   = conn.execute("SELECT COUNT(*) FROM sent_emails").fetchone()[0]
        sent_ok = conn.execute("SELECT COUNT(*) FROM sent_emails WHERE status = 'sent'").fetchone()[0]
        failed  = conn.execute("SELECT COUNT(*) FROM sent_emails WHERE status = 'failed'").fetchone()[0]
        replied = conn.execute("SELECT COUNT(*) FROM sent_emails WHERE status = 'replied'").fetchone()[0]
        replies = conn.execute("SELECT COUNT(*) FROM replies").fetchone()[0]
    finally:
        conn.close()
    return {
        "total": total,
        "sent": sent_ok,
        "failed": failed,
        "replied": replied,
        "reply_count": replies,
        "reply_rate": round(replied / sent_ok * 100, 1) if sent_ok else 0,
    }


def get_history(storage_id: str, limit: int = 50) -> list[dict]:
    _init_db(storage_id)
    conn = _connect(storage_id)
    try:
        rows = conn.execute(
            "SELECT id, email, name, company, subject, sent_at, status FROM sent_emails ORDER BY sent_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def get_replies(storage_id: str, limit: int = 50) -> list[dict]:
    _init_db(storage_id)
    conn = _connect(storage_id)
    try:
        rows = conn.execute(
            "SELECT r.id, r.from_email, r.subject, r.snippet, r.received_at, s.name, s.company "
            "FROM replies r LEFT JOIN sent_emails s ON s.id = r.sent_email_id "
            "ORDER BY r.received_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Reply listener (run in background thread if wanted)
# ---------------------------------------------------------------------------

def check_replies_once(storage_id: str) -> int:
    """
    Poll Gmail IMAP for unseen messages that are replies to sent emails.
    Returns number of new replies found.
    Requires cfg keys: sender_email, gmail_app_password.
    """
    import imaplib
    import email as email_lib

    cfg = get_config(storage_id)
    sender_email = cfg.get("sender_email", "")
    app_password = cfg.get("gmail_app_password", "")
    if not sender_email or not app_password:
        return 0

    _init_db(storage_id)
    conn = _connect(storage_id)
    new_replies = 0

    def _decode(raw):
        parts = _decode_header(raw or "")
        out = []
        for chunk, charset in parts:
            if isinstance(chunk, bytes):
                out.append(chunk.decode(charset or "utf-8", errors="replace"))
            else:
                out.append(chunk)
        return " ".join(out)

    def _get_sent_by_email(from_email: str):
        row = conn.execute(
            "SELECT * FROM sent_emails WHERE email = ? AND status = 'sent' ORDER BY sent_at DESC LIMIT 1",
            (from_email,),
        ).fetchone()
        return dict(row) if row else None

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(sender_email, app_password)
        mail.select("INBOX")
        # Search for all messages since we might want to check against our DB even if seen
        # But for performance let's stick to UNSEEN for now, or just search for messages from people we emailed
        status, data = mail.search(None, "UNSEEN")
        if status != "OK" or not data[0]:
            mail.logout()
            return 0

        for uid in data[0].split():
            st, msg_data = mail.fetch(uid, "(RFC822)")
            if st != "OK":
                continue
            msg     = email_lib.message_from_bytes(msg_data[0][1])
            from_r  = _decode(msg.get("From", ""))
            subject = _decode(msg.get("Subject", "(no subject)"))
            
            # Extract email cleanly
            from_e = parseaddr(from_r)[1].lower()
            if not from_e:
                continue

            # Check if this is a reply to something we sent
            in_reply_to = msg.get("In-Reply-To", "")
            references = msg.get("References", "")
            
            sent_row = None
            if in_reply_to:
                # Try locating by Message-ID match
                sent_row = conn.execute(
                    "SELECT * FROM sent_emails WHERE message_id = ? LIMIT 1",
                    (in_reply_to,),
                ).fetchone()
            
            if not sent_row and references:
                ref_list = references.split()
                for ref in reversed(ref_list):
                    sent_row = conn.execute(
                        "SELECT * FROM sent_emails WHERE message_id = ? LIMIT 1",
                        (ref,),
                    ).fetchone()
                    if sent_row: break

            if not sent_row:
                # Fallback to email matching
                sent_row = _get_sent_by_email(from_e)
            
            if not sent_row:
                continue

            # Check if we already recorded this reply
            existing_reply = conn.execute(
                "SELECT id FROM replies WHERE from_email = ? AND received_at > ? LIMIT 1",
                (from_e, sent_row["sent_at"]),
            ).fetchone()
            if existing_reply:
                continue

            snippet = ""
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        snippet = payload.decode("utf-8", errors="replace")[:200]
                    break

            with conn:
                conn.execute(
                    "INSERT INTO replies (from_email, subject, snippet, received_at, sent_email_id) VALUES (?,?,?,?,?)",
                    (from_e, subject, snippet, _now(), sent_row["id"]),
                )
                conn.execute(
                    "UPDATE sent_emails SET status = 'replied' WHERE id = ?",
                    (sent_row["id"],),
                )
            new_replies += 1

        mail.logout()
    except Exception as e:
        print(f"IMAP Error: {e}")
    finally:
        conn.close()

    return new_replies
