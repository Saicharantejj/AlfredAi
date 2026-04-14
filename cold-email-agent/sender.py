import smtplib
import time
import uuid
from email.mime.multipart import MIMEMultipart
from email.mime.text      import MIMEText
from email.utils          import formataddr, formatdate

import config
import tracker


GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_PORT = 587

SIGNATURE = """--
Saicharan Tej
Senior Partner, The Solstice Tech"""


def _build_message(lead, subject, body):
    msg = MIMEMultipart("alternative")
    message_id = f"<{uuid.uuid4()}@{config.SENDER_EMAIL.split('@')[1]}>"

    msg["From"]       = formataddr((config.SENDER_NAME, config.SENDER_EMAIL))
    msg["To"]         = formataddr((lead.get("name", ""), lead["email"]))
    msg["Subject"]    = subject
    msg["Date"]       = formatdate(localtime=True)
    msg["Message-ID"] = message_id

    full_body = f"{body}\n\n{SIGNATURE}"
    msg.attach(MIMEText(full_body, "plain", "utf-8"))

    return msg, message_id


def send_email(lead, subject, body):
    try:
        msg, message_id = _build_message(lead, subject, body)

        with smtplib.SMTP(GMAIL_SMTP_HOST, GMAIL_SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(config.SENDER_EMAIL, config.GMAIL_APP_PASSWORD)
            server.sendmail(config.SENDER_EMAIL, [lead["email"]], msg.as_string())

        tracker.record_sent(lead, subject, body, message_id)
        print(f"  ✅  Sent → {lead['email']}  |  {subject}")
        return True

    except smtplib.SMTPAuthenticationError:
        err = "Gmail authentication failed. Check SENDER_EMAIL and GMAIL_APP_PASSWORD in config.py."
        print(f"  ❌  {err}")
        tracker.record_failed(lead, subject, body, err)
        return False

    except Exception as exc:
        err = str(exc)
        print(f"  ❌  Failed to send to {lead['email']}: {err}")
        tracker.record_failed(lead, subject, body, err)
        return False


def send_with_delay(lead, subject, body, delay=None):
    result = send_email(lead, subject, body)
    sleep_for = delay if delay is not None else config.DELAY_BETWEEN_EMAILS_SECONDS
    if sleep_for > 0:
        print(f"     ⏱  Waiting {sleep_for}s before next send…")
        time.sleep(sleep_for)
    return result


if __name__ == "__main__":
    test_lead = {
        "name":    "Test Person",
        "company": "Test Co",
        "email":   config.SENDER_EMAIL,
    }
    send_email(
        test_lead,
        subject="[TEST] Cold-email agent test",
        body="This is a test message. If you see this, SMTP is working.",
    )