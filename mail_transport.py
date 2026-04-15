import imaplib
import smtplib


def smtp_uses_ssl(port: int) -> bool:
    return int(port) == 465


def imap_uses_ssl(port: int) -> bool:
    return int(port) == 993


def open_smtp_connection(host: str, port: int, *, timeout: float = 30.0):
    if smtp_uses_ssl(port):
        return smtplib.SMTP_SSL(host, int(port), timeout=timeout)

    server = smtplib.SMTP(host, int(port), timeout=timeout)
    server.ehlo()
    if server.has_extn("starttls"):
        server.starttls()
        server.ehlo()
    return server


def open_imap_connection(host: str, port: int):
    if imap_uses_ssl(port):
        return imaplib.IMAP4_SSL(host, int(port))

    mail = imaplib.IMAP4(host, int(port))
    capabilities = {
        cap.decode("utf-8", errors="ignore").upper() if isinstance(cap, bytes) else str(cap).upper()
        for cap in (getattr(mail, "capabilities", ()) or ())
    }
    if "STARTTLS" in capabilities:
        mail.starttls()
    return mail
