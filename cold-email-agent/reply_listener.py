"""
reply_listener.py
-----------------
Polls Gmail via IMAP and detects replies to emails we sent.
Runs in a background thread.
"""

import email
import imaplib
import threading
import time
from email.header import decode_header

import config
import tracker


GMAIL_IMAP_HOST = "imap.gmail.com"
GMAIL_IMAP_PORT = 993


def _notify_reply(from_email: str, subject: str, snippet: str) -> None:
    banner = (
        "\n"
        "╔══════════════════════════════════════════════════╗\n"
        "║  📬  REPLY RECEIVED                              ║\n"
        f"║  From    : {from_email:<38} ║\n"
        f"║  Subject : {subject[:38]:<38} ║\n"
        "╚══════════════════════════════════════════════════╝\n"
    )
    print(banner)

    try:
        import subprocess
        script = (
            f'display notification "{from_email}: {subject[:60]}" '
            f'with title "Cold Email Reply" sound name "Glass"'
        )
        subprocess.run(["osascript", "-e", script], check=False, capture_output=True)
    except Exception:
        pass


def _notify_complete(total: int) -> None:
    print(f"\n🎉  All {total} leads processed! Check data/tracker.db for the full log.\n")
    try:
        import subprocess
        script = (
            f'display notification "All {total} emails sent!" '
            'with title "Cold Email Agent — Done" sound name "Hero"'
        )
        subprocess.run(["osascript", "-e", script], check=False, capture_output=True)
    except Exception:
        pass


def _decode_header_value(raw) -> str:
    parts = decode_header(raw or "")
    decoded = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            decoded.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(chunk)
    return " ".join(decoded)


def _check_once(seen_uids: set) -> set:
    try:
        mail = imaplib.IMAP4_SSL(GMAIL_IMAP_HOST, GMAIL_IMAP_PORT)
        mail.login(config.SENDER_EMAIL, config.GMAIL_APP_PASSWORD)
        mail.select("INBOX")

        status, data = mail.search(None, "UNSEEN")
        if status != "OK" or not data[0]:
            mail.logout()
            return seen_uids

        uid_list = data[0].split()
        new_uids  = set(uid_list) - seen_uids

        for uid in new_uids:
            status, msg_data = mail.fetch(uid, "(RFC822)")
            if status != "OK":
                continue

            raw_email = msg_data[0][1]
            msg       = email.message_from_bytes(raw_email)

            from_raw   = _decode_header_value(msg.get("From", ""))
            subject    = _decode_header_value(msg.get("Subject", "(no subject)"))
            from_email = from_raw
            if "<" in from_raw:
                from_email = from_raw.split("<")[1].rstrip(">").strip()

            sent_row = tracker.get_sent_by_email(from_email)

            if sent_row:
                snippet = ""
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        payload = part.get_payload(decode=True)
                        if payload:
                            snippet = payload.decode("utf-8", errors="replace")[:200]
                        break

                tracker.record_reply(from_email, subject, snippet, sent_row["id"])
                _notify_reply(from_email, subject, snippet)

        mail.logout()
        seen_uids.update(new_uids)

    except imaplib.IMAP4.error as exc:
        print(f"  ⚠️  IMAP error: {exc}")
    except Exception as exc:
        print(f"  ⚠️  Reply-listener error: {exc}")

    return seen_uids


def start_listener(stop_event=None) -> None:
    print(f"👂  Reply listener started — polling every {config.REPLY_CHECK_INTERVAL_SECONDS}s…")
    seen_uids: set = set()
    while True:
        seen_uids = _check_once(seen_uids)
        for _ in range(config.REPLY_CHECK_INTERVAL_SECONDS):
            if stop_event and stop_event.is_set():
                print("👂  Reply listener stopped.")
                return
            time.sleep(1)


def start_listener_thread() -> threading.Event:
    stop_event = threading.Event()
    thread = threading.Thread(
        target=start_listener,
        args=(stop_event,),
        daemon=True,
        name="reply-listener",
    )
    thread.start()
    return stop_event