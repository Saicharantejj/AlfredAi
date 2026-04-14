import re
from typing import Union, Optional, List, Dict, Any, Tuple
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv
from contacts import find_contacts_by_query
from integrations import get_email_connection_settings, update_email_state
from mail_transport import open_smtp_connection

load_dotenv()
EMAIL_RE = re.compile(r"^[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,63}$", re.IGNORECASE)


def _resolve_recipient_email(to: str, user_id: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    recipient = str(to or "").strip()
    if not recipient:
        return None, "Email recipient is missing."
    if EMAIL_RE.fullmatch(recipient):
        return recipient, None
    if not user_id:
        return None, f'No email address found for "{recipient}".'

    matches = [contact for contact in find_contacts_by_query(user_id, recipient) if contact.get("email")]
    if len(matches) == 1:
        return matches[0]["email"], None
    if len(matches) > 1:
        return None, f'Multiple contacts match "{recipient}". Use the full email address.'
    return None, f'No saved contact with an email address matches "{recipient}".'


def send_email(to: str, subject: str, body: str, user_id: Optional[str] = None) -> bool:
    recipient, recipient_error = _resolve_recipient_email(to, user_id)
    if not recipient:
        print(f"Email error: {recipient_error}")
        if user_id:
            update_email_state(user_id, connected=True, last_error=recipient_error or "Invalid email recipient.")
        return False

    # --- Gmail OAuth path (preferred when connected) ---
    if user_id:
        try:
            from connectors.gmail import gmail  # noqa: PLC0415
            gmail_state = gmail.load_state(user_id)
            if gmail_state.get("connected"):
                ok, error = gmail.send_email(user_id, recipient, subject, body)
                if not ok:
                    print(f"Gmail API send error: {error}")
                return ok
        except Exception as exc:
            print(f"Gmail connector error, falling back to SMTP: {exc}")

    # --- App-password SMTP fallback ---
    try:
        settings = get_email_connection_settings(user_id, allow_fallback=user_id is None)
        if not settings:
            print("Email error: no connected email account")
            if user_id:
                update_email_state(user_id, connected=False, last_error="No connected email account.")
            return False

        msg = MIMEMultipart()
        msg['From'] = settings["address"]
        msg['To'] = recipient
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))

        with open_smtp_connection(settings["smtp_host"], settings["smtp_port"], timeout=20) as server:
            server.login(settings["address"], settings["password"])
            server.sendmail(settings["address"], recipient, msg.as_string())
        if user_id:
            update_email_state(user_id, connected=True, last_error="")
        return True
    except Exception as e:
        print(f"Email error: {e}")
        if user_id:
            update_email_state(user_id, connected=False, last_error=str(e))
        return False
