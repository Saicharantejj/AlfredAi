import email
from typing import Optional, List, Dict, Any, Union
from email.header import decode_header
from dotenv import load_dotenv
from integrations import get_email_connection_settings, update_email_state
from mail_transport import open_imap_connection

load_dotenv()


def get_unread_emails(max_emails: int = 5, user_id: Optional[str] = None) -> List:
    # --- Gmail OAuth path (preferred when connected) ---
    if user_id:
        try:
            from connectors.gmail import gmail  # noqa: PLC0415
            gmail_state = gmail.load_state(user_id)
            if gmail_state.get("connected"):
                return gmail.get_unread_emails(user_id, max_emails=max_emails)
        except Exception as exc:
            print(f"Gmail connector error, falling back to IMAP: {exc}")

    # --- App-password IMAP fallback ---
    try:
        settings = get_email_connection_settings(user_id, allow_fallback=user_id is None)
        if not settings:
            print("Email read error: no connected email account")
            if user_id:
                update_email_state(user_id, connected=False, last_error="No connected email account.")
            return []

        mail = open_imap_connection(settings["imap_host"], settings["imap_port"])
        mail.login(settings["address"], settings["password"])
        mail.select("inbox")

        _, messages = mail.search(None, "UNSEEN")
        email_ids = messages[0].split()[-max_emails:]

        emails = []
        for eid in reversed(email_ids):
            _, msg_data = mail.fetch(eid, "(RFC822)")
            msg = email.message_from_bytes(msg_data[0][1])

            raw_subject = msg.get("Subject", "")
            subject, encoding = decode_header(raw_subject)[0] if raw_subject else ("(no subject)", None)
            if isinstance(subject, bytes):
                subject = subject.decode(encoding or "utf-8")

            sender = msg.get("From", "")

            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        body = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                        break
            else:
                body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")

            emails.append({"from": sender, "subject": subject, "body": body[:500]})

        mail.logout()
        if user_id:
            update_email_state(user_id, connected=True, last_error="")
        return emails

    except Exception as e:
        print(f"Email read error: {e}")
        if user_id:
            update_email_state(user_id, connected=False, last_error=str(e))
        return []
