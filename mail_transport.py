import imaplib
import smtplib
from typing import Optional


def smtp_uses_ssl(port: int) -> bool:
    return int(port) == 465


def imap_uses_ssl(port: int) -> bool:
    return int(port) == 993


import logging
import socket

logger = logging.getLogger("alfred.mail_transport")

def open_smtp_connection(host: str, port: int, *, timeout: float = 30.0):
    try:
        return _connect_smtp(host, port, timeout=timeout)
    except (socket.error, OSError) as e:
        # 101 is ENETUNREACH on Linux, 51 on macOS. 
        # We also catch general socket errors that might indicate IPv6 issues.
        logger.warning("SMTP connection to %s:%d failed (%s). Attempting IPv4 fallback...", host, port, e)
        try:
            # Resolve to IPv4 specifically
            addr_info = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
            if addr_info:
                ipv4_address = addr_info[0][4][0]
                logger.info("Retrying SMTP connection with IPv4 address: %s", ipv4_address)
                return _connect_smtp(ipv4_address, port, timeout=timeout, original_host=host)
        except Exception as e2:
            logger.error("IPv4 fallback failed: %s", e2)
        
        # If fallback fails or doesn't apply, re-raise the original error
        raise


def _connect_smtp(host: str, port: int, *, timeout: float, original_host: Optional[str] = None):
    host_for_log = f"{original_host} ({host})" if original_host else host
    
    if smtp_uses_ssl(port):
        logger.debug("Connecting to SMTP_SSL %s:%d", host_for_log, port)
        return smtplib.SMTP_SSL(host, int(port), timeout=timeout)

    logger.debug("Connecting to SMTP %s:%d", host_for_log, port)
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
