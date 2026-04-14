"""
Gmail connector for Alfred.

Uses Google OAuth 2.0 + the Gmail API instead of SMTP/IMAP app passwords.
This is the correct long-term approach: no stored passwords, tokens auto-refresh,
works even when Google blocks "less secure app" access.

Setup (one-time, done in Google Cloud Console):
  1. Create a project → Enable the Gmail API.
  2. OAuth consent screen → External → add your Gmail as a test user.
  3. Credentials → Create OAuth client ID → Web application.
     Authorised redirect URI: http://localhost:8000/api/integrations/gmail/callback
     (or your production domain equivalent)
  4. Copy Client ID and Client Secret into .env:
       GOOGLE_CLIENT_ID=...
       GOOGLE_CLIENT_SECRET=...

Environment variables
---------------------
  GOOGLE_CLIENT_ID       Required — OAuth 2.0 client ID
  GOOGLE_CLIENT_SECRET   Required — OAuth 2.0 client secret
  ALFRED_BASE_URL        Base URL of the Alfred server (default: http://localhost:8000)
                         Used to build the redirect URI automatically.

Usage
-----
    from connectors.gmail import gmail

    # Kick off the OAuth flow (returns a URL to redirect the browser to)
    url = gmail.get_auth_url(user_id, redirect_uri)

    # Handle the redirect callback
    result = gmail.handle_callback(user_id, code=..., state=..., redirect_uri=...)

    # Send an email
    ok, error = gmail.send_email(user_id, to="alice@example.com", subject="Hey", body="...")

    # Read unread emails
    emails = gmail.get_unread_emails(user_id, max_emails=5)

    # Disconnect
    gmail.disconnect(user_id)
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from connectors.base import BaseConnector

logger = logging.getLogger("alfred.connectors.gmail")

SCOPES = ["https://mail.google.com/"]
ALFRED_BASE_URL = os.getenv("ALFRED_BASE_URL", "http://localhost:8000").rstrip("/")
DEFAULT_REDIRECT_URI = f"{ALFRED_BASE_URL}/api/integrations/gmail/callback"


# ---------------------------------------------------------------------------
# Encryption helpers (reuse the same key as integrations.py)
# ---------------------------------------------------------------------------

def _get_cipher():
    """Build a Fernet cipher using the same secret as integrations.py."""
    try:
        from integrations import _get_cipher as _integrations_cipher  # noqa: PLC0415
        return _integrations_cipher()
    except Exception:
        # Fallback: derive from ALFRED_JWT_SECRET
        from cryptography.fernet import Fernet  # noqa: PLC0415
        secret = os.getenv("ALFRED_JWT_SECRET", "alfred-default-secret")
        key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
        return Fernet(key)


def _encrypt(value: str) -> str:
    return _get_cipher().encrypt(value.encode()).decode()


def _decrypt(encrypted: str) -> str:
    return _get_cipher().decrypt(encrypted.encode()).decode()


# ---------------------------------------------------------------------------
# Gmail connector
# ---------------------------------------------------------------------------

class GmailConnector(BaseConnector):
    name = "gmail"

    # ------------------------------------------------------------------
    # BaseConnector interface
    # ------------------------------------------------------------------

    def get_status(self, user_id: str) -> dict:
        state = self.load_state(user_id)
        return {
            "connected": bool(state.get("connected")),
            "email": state.get("email", ""),
            "last_error": state.get("last_error", ""),
        }

    def is_configured(self) -> bool:
        """Return True if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are set."""
        return bool(os.getenv("GOOGLE_CLIENT_ID") and os.getenv("GOOGLE_CLIENT_SECRET"))

    # ------------------------------------------------------------------
    # Internal: Google client config dict
    # ------------------------------------------------------------------

    def _client_config(self) -> dict:
        client_id = os.getenv("GOOGLE_CLIENT_ID")
        client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
        if not client_id or not client_secret:
            raise RuntimeError(
                "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be set in .env "
                "before using Gmail OAuth. See connectors/gmail.py for setup instructions."
            )
        return {
            "web": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        }

    # ------------------------------------------------------------------
    # Internal: token persistence
    # ------------------------------------------------------------------

    def _save_tokens(self, user_id: str, creds, email: str) -> None:
        expiry_iso = ""
        if creds.expiry:
            expiry_iso = creds.expiry.replace(tzinfo=timezone.utc).isoformat() if creds.expiry.tzinfo is None else creds.expiry.isoformat()

        state = self.load_state(user_id)
        state.update({
            "connected": True,
            "email": email,
            "access_token": creds.token or "",
            "token_expiry": expiry_iso,
            "last_error": "",
        })
        # Only overwrite the encrypted refresh token if we have a new one.
        if creds.refresh_token:
            state["refresh_token_encrypted"] = _encrypt(creds.refresh_token)

        # Remove transient OAuth state fields.
        state.pop("_oauth_state", None)
        state.pop("_redirect_uri", None)
        self.save_state(user_id, state)

    def _load_credentials(self, user_id: str):
        """
        Load Google OAuth credentials for *user_id*, auto-refreshing if expired.
        Returns a google.oauth2.credentials.Credentials object, or None.
        """
        try:
            from google.oauth2.credentials import Credentials  # noqa: PLC0415
            from google.auth.transport.requests import Request  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError(
                "google-auth is not installed. Run: pip install google-auth google-auth-oauthlib google-api-python-client"
            ) from exc

        state = self.load_state(user_id)
        if not state.get("connected") or not state.get("refresh_token_encrypted"):
            return None

        try:
            refresh_token = _decrypt(state["refresh_token_encrypted"])
        except Exception:
            logger.exception("Could not decrypt Gmail refresh token for %s", user_id)
            self.update_state(user_id, connected=False, last_error="Saved Gmail credentials could not be decrypted. Reconnect your Gmail account.")
            return None

        expiry = None
        if state.get("token_expiry"):
            try:
                expiry = datetime.fromisoformat(state["token_expiry"])
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
            except ValueError:
                pass

        client_id = os.getenv("GOOGLE_CLIENT_ID")
        client_secret = os.getenv("GOOGLE_CLIENT_SECRET")

        creds = Credentials(
            token=state.get("access_token") or None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=SCOPES,
        )
        if expiry:
            creds.expiry = expiry

        # Auto-refresh if expired.
        if not creds.valid and creds.refresh_token:
            try:
                creds.refresh(Request())
                self._save_tokens(user_id, creds, state.get("email", ""))
            except Exception as exc:
                logger.exception("Could not refresh Gmail token for %s", user_id)
                self.update_state(user_id, connected=False, last_error=f"Gmail token refresh failed: {exc}. Reconnect your Gmail account.")
                return None

        return creds

    def _build_gmail_service(self, user_id: str):
        try:
            from googleapiclient.discovery import build as gbuild  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError("google-api-python-client is not installed.") from exc

        creds = self._load_credentials(user_id)
        if not creds:
            return None
        return gbuild("gmail", "v1", credentials=creds)

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------

    def update_state(
        self,
        user_id: str,
        *,
        connected: Optional[bool] = None,
        last_error: Optional[str] = None,
        email: Optional[str] = None,
    ) -> None:
        state = self.load_state(user_id)
        if connected is not None:
            state["connected"] = connected
        if last_error is not None:
            state["last_error"] = last_error
        if email is not None:
            state["email"] = email
        self.save_state(user_id, state)

    # ------------------------------------------------------------------
    # OAuth flow
    # ------------------------------------------------------------------

    def get_auth_url(
        self,
        user_id: str,
        redirect_uri: str = DEFAULT_REDIRECT_URI,
    ) -> str:
        """
        Generate the Google OAuth consent URL.
        The browser should be redirected here to start the auth flow.
        """
        try:
            from google_auth_oauthlib.flow import Flow  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError("google-auth-oauthlib is not installed.") from exc

        flow = Flow.from_client_config(
            self._client_config(),
            scopes=SCOPES,
            redirect_uri=redirect_uri,
        )
        auth_url, state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",  # Always ask for consent so we always get a refresh_token.
        )

        # Persist state so we can validate the callback.
        current_state = self.load_state(user_id)
        current_state["_oauth_state"] = state
        current_state["_redirect_uri"] = redirect_uri
        self.save_state(user_id, current_state)

        return auth_url

    def handle_callback(
        self,
        user_id: str,
        code: str,
        state: str,
        redirect_uri: str = DEFAULT_REDIRECT_URI,
    ) -> dict:
        """
        Exchange the OAuth authorization code for tokens.
        Returns { "connected": True, "email": "user@gmail.com" } on success.
        Raises RuntimeError on failure.
        """
        try:
            from google_auth_oauthlib.flow import Flow  # noqa: PLC0415
            from googleapiclient.discovery import build as gbuild  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError("Google client libraries not installed.") from exc

        saved = self.load_state(user_id)
        saved_state = saved.get("_oauth_state", "")

        flow = Flow.from_client_config(
            self._client_config(),
            scopes=SCOPES,
            state=saved_state or state,
            redirect_uri=redirect_uri,
        )
        flow.fetch_token(code=code)
        creds = flow.credentials

        # Fetch the user's Gmail address.
        try:
            from googleapiclient.discovery import build as _gbuild  # noqa: PLC0415
            oauth2_service = _gbuild("oauth2", "v2", credentials=creds)
            user_info = oauth2_service.userinfo().get().execute()
            email = user_info.get("email", "")
        except Exception:
            # Fallback: parse email from id_token if available.
            email = ""
            logger.warning("Could not fetch Gmail user info; email will be empty")

        self._save_tokens(user_id, creds, email)
        logger.info("Gmail OAuth connected for %s (%s)", user_id, email)
        return {"connected": True, "email": email}

    # ------------------------------------------------------------------
    # Send email
    # ------------------------------------------------------------------

    def send_email(
        self,
        user_id: str,
        to: str,
        subject: str,
        body: str,
    ) -> tuple[bool, str]:
        """
        Send an email via the Gmail API.
        Returns (success, error_message).
        """
        service = self._build_gmail_service(user_id)
        if not service:
            return False, "Gmail account is not connected. Go to Settings > Integrations and connect Gmail."

        try:
            state = self.load_state(user_id)
            sender = state.get("email", "me")

            message = MIMEMultipart()
            message["from"] = sender
            message["to"] = to
            message["subject"] = subject
            message.attach(MIMEText(body, "plain"))

            raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
            service.users().messages().send(userId="me", body={"raw": raw}).execute()
            self.update_state(user_id, connected=True, last_error="")
            return True, ""
        except Exception as exc:
            logger.exception("Gmail send failed for %s", user_id)
            error = str(exc)
            self.update_state(user_id, connected=True, last_error=error)
            return False, error

    # ------------------------------------------------------------------
    # Read emails
    # ------------------------------------------------------------------

    def get_unread_emails(
        self,
        user_id: str,
        max_emails: int = 5,
    ) -> list[dict]:
        """
        Return up to *max_emails* unread messages from the Gmail inbox.
        Each item: { "from", "subject", "body" (first 500 chars) }
        """
        import email as _email_lib  # noqa: PLC0415
        from email.header import decode_header  # noqa: PLC0415

        service = self._build_gmail_service(user_id)
        if not service:
            return []

        try:
            result = (
                service.users()
                .messages()
                .list(userId="me", labelIds=["INBOX", "UNREAD"], maxResults=max_emails)
                .execute()
            )
            messages = result.get("messages", [])
            emails = []

            for msg_ref in messages:
                msg = (
                    service.users()
                    .messages()
                    .get(userId="me", id=msg_ref["id"], format="raw")
                    .execute()
                )
                raw = base64.urlsafe_b64decode(msg["raw"].encode())
                parsed = _email_lib.message_from_bytes(raw)

                # Decode subject
                raw_subject = parsed.get("Subject", "")
                subject_parts = decode_header(raw_subject) if raw_subject else [("(no subject)", None)]
                subject_str, enc = subject_parts[0]
                if isinstance(subject_str, bytes):
                    subject_str = subject_str.decode(enc or "utf-8", errors="ignore")

                # Extract plain text body
                body = ""
                if parsed.is_multipart():
                    for part in parsed.walk():
                        if part.get_content_type() == "text/plain":
                            payload = part.get_payload(decode=True)
                            if payload:
                                body = payload.decode("utf-8", errors="ignore")
                                break
                else:
                    payload = parsed.get_payload(decode=True)
                    if payload:
                        body = payload.decode("utf-8", errors="ignore")

                emails.append({
                    "from": parsed.get("From", ""),
                    "subject": subject_str,
                    "body": body[:500],
                })

            self.update_state(user_id, connected=True, last_error="")
            return emails

        except Exception as exc:
            logger.exception("Gmail read failed for %s", user_id)
            self.update_state(user_id, last_error=str(exc))
            return []

    # ------------------------------------------------------------------
    # Disconnect
    # ------------------------------------------------------------------

    def disconnect(self, user_id: str) -> None:
        """Revoke tokens and clear Gmail state."""
        state = self.load_state(user_id)
        encrypted = state.get("refresh_token_encrypted", "")
        if encrypted:
            try:
                import requests as _requests  # noqa: PLC0415
                refresh_token = _decrypt(encrypted)
                _requests.post(
                    "https://oauth2.googleapis.com/revoke",
                    params={"token": refresh_token},
                    timeout=5,
                )
            except Exception:
                pass  # Best-effort revoke — clear locally regardless.

        self.save_state(user_id, {
            "connected": False,
            "email": "",
            "access_token": "",
            "refresh_token_encrypted": "",
            "token_expiry": "",
            "last_error": "",
        })


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

gmail = GmailConnector()
