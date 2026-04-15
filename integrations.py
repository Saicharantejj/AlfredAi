import base64
import hashlib
import os
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from security_utils import SECRET_KEY
from storage import (
    BASE_DIR, 
    load_user_document, 
    save_user_document,
    load_user_document_async,
    save_user_document_async
)

INTEGRATIONS_KEY_PATH = os.getenv(
    "ALFRED_INTEGRATIONS_KEY_PATH",
    str(BASE_DIR / "users" / ".integrations.key"),
)
DEFAULT_EMAIL_SMTP_HOST = os.getenv("ALFRED_DEFAULT_SMTP_HOST", "smtp.gmail.com").strip().lower()
DEFAULT_EMAIL_SMTP_PORT = int(os.getenv("ALFRED_DEFAULT_SMTP_PORT", "587"))
DEFAULT_EMAIL_IMAP_HOST = os.getenv("ALFRED_DEFAULT_IMAP_HOST", "imap.gmail.com").strip().lower()
DEFAULT_EMAIL_IMAP_PORT = int(os.getenv("ALFRED_DEFAULT_IMAP_PORT", "993"))


def _load_or_create_local_secret() -> str:
    secret_from_env = os.getenv("ALFRED_INTEGRATIONS_SECRET")
    if secret_from_env:
        return secret_from_env

    os.makedirs(os.path.dirname(INTEGRATIONS_KEY_PATH), exist_ok=True)
    if os.path.exists(INTEGRATIONS_KEY_PATH):
        with open(INTEGRATIONS_KEY_PATH, "r", encoding="utf-8") as key_file:
            persisted = key_file.read().strip()
            if persisted:
                return persisted

    persisted = base64.urlsafe_b64encode(os.urandom(32)).decode("utf-8")
    with open(INTEGRATIONS_KEY_PATH, "w", encoding="utf-8") as key_file:
        key_file.write(persisted)
    return persisted


def _build_cipher(secret: str) -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


def _get_cipher() -> Fernet:
    return _build_cipher(_load_or_create_local_secret())


def _get_legacy_ciphers() -> list[Fernet]:
    secrets: list[str] = []
    legacy_env_secret = os.getenv("ALFRED_INTEGRATIONS_SECRET")
    if legacy_env_secret:
        secrets.append(legacy_env_secret)
    if SECRET_KEY not in secrets:
        secrets.append(SECRET_KEY)
    return [_build_cipher(secret) for secret in secrets]


def _default_state(user_id: str) -> dict:
    return {
        "email": {
            "connected": False,
            "address": "",
            "encrypted_password": "",
            "smtp_host": "",
            "smtp_port": None,
            "imap_host": "",
            "imap_port": None,
            "last_error": "",
        },
        "whatsapp": {
            "connected": False,
            "session_id": user_id,
            "last_error": "",
        },
    }


def load_integrations(user_id: str) -> dict:
    try:
        data = load_user_document(user_id, "integrations.json", {})
        if data:
            default = _default_state(user_id)
            default.update(data)
            default["email"] = {**_default_state(user_id)["email"], **data.get("email", {})}
            default["whatsapp"] = {**_default_state(user_id)["whatsapp"], **data.get("whatsapp", {})}
            default["whatsapp"]["session_id"] = user_id
            return default
    except Exception:
        pass
    return _default_state(user_id)


async def load_integrations_async(user_id: str) -> dict:
    try:
        data = await load_user_document_async(user_id, "integrations.json", {})
        if data:
            default = _default_state(user_id)
            default.update(data)
            default["email"] = {**_default_state(user_id)["email"], **data.get("email", {})}
            default["whatsapp"] = {**_default_state(user_id)["whatsapp"], **data.get("whatsapp", {})}
            default["whatsapp"]["session_id"] = user_id
            return default
    except Exception:
        pass
    return _default_state(user_id)


def save_integrations(user_id: str, state: dict):
    save_user_document(user_id, "integrations.json", state)


async def save_integrations_async(user_id: str, state: dict):
    await save_user_document_async(user_id, "integrations.json", state)


def build_email_connection_settings(
    address: str,
    password: str,
    *,
    smtp_host: Optional[str] = "",
    smtp_port: Optional[int] = None,
    imap_host: Optional[str] = "",
    imap_port: Optional[int] = None,
) -> dict:
    normalized_smtp_host = (smtp_host or "").strip().lower()
    normalized_imap_host = (imap_host or "").strip().lower()
    smtp_default_port = DEFAULT_EMAIL_SMTP_PORT if not normalized_smtp_host else 587
    imap_default_port = DEFAULT_EMAIL_IMAP_PORT if not normalized_imap_host else 993
    return {
        "address": address.strip(),
        "password": password,
        "smtp_host": normalized_smtp_host or DEFAULT_EMAIL_SMTP_HOST,
        "smtp_port": int(smtp_port or smtp_default_port),
        "imap_host": normalized_imap_host or DEFAULT_EMAIL_IMAP_HOST,
        "imap_port": int(imap_port or imap_default_port),
    }


def set_email_credentials(
    user_id: str,
    address: str,
    app_password: str,
    *,
    smtp_host: Optional[str] = "",
    smtp_port: Optional[int] = None,
    imap_host: Optional[str] = "",
    imap_port: Optional[int] = None,
):
    settings = build_email_connection_settings(
        address,
        app_password,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        imap_host=imap_host,
        imap_port=imap_port,
    )
    state = load_integrations(user_id)
    state["email"] = {
        "connected": True,
        "address": settings["address"],
        "encrypted_password": _get_cipher().encrypt(app_password.encode("utf-8")).decode("utf-8"),
        "smtp_host": settings["smtp_host"],
        "smtp_port": settings["smtp_port"],
        "imap_host": settings["imap_host"],
        "imap_port": settings["imap_port"],
        "last_error": "",
    }
    save_integrations(user_id, state)


def clear_email_credentials(user_id: str):
    state = load_integrations(user_id)
    state["email"] = {
        "connected": False,
        "address": "",
        "encrypted_password": "",
        "smtp_host": "",
        "smtp_port": None,
        "imap_host": "",
        "imap_port": None,
        "last_error": "",
    }
    save_integrations(user_id, state)


def update_email_state(
    user_id: str,
    *,
    connected: Optional[bool] = None,
    last_error: Optional[str] = None,
    address: Optional[str] = None,
):
    state = load_integrations(user_id)
    email_state = state.get("email", {})
    if connected is not None:
        email_state["connected"] = connected
    if last_error is not None:
        email_state["last_error"] = last_error
    if address is not None:
        email_state["address"] = address.strip()
    state["email"] = {**_default_state(user_id)["email"], **email_state}
    save_integrations(user_id, state)


async def update_email_state_async(
    user_id: str,
    *,
    connected: Optional[bool] = None,
    last_error: Optional[str] = None,
    address: Optional[str] = None,
):
    state = await load_integrations_async(user_id)
    email_state = state.get("email", {})
    if connected is not None:
        email_state["connected"] = connected
    if last_error is not None:
        email_state["last_error"] = last_error
    if address is not None:
        email_state["address"] = address.strip()
    state["email"] = {**_default_state(user_id)["email"], **email_state}
    await save_integrations_async(user_id, state)


def get_email_connection_settings(
    user_id: Optional[str] = None,
    *,
    allow_fallback: Optional[bool] = None,
) -> Optional[dict]:
    if allow_fallback is None:
        allow_fallback = user_id is None

    if user_id:
        state = load_integrations(user_id)
        email_state = state.get("email", {})
        encrypted = email_state.get("encrypted_password")
        if email_state.get("connected") and email_state.get("address") and encrypted:
            primary_cipher = _get_cipher()
            for cipher in [primary_cipher, *_get_legacy_ciphers()]:
                try:
                    password = cipher.decrypt(encrypted.encode("utf-8")).decode("utf-8")
                    if cipher is not primary_cipher:
                        set_email_credentials(
                            user_id,
                            email_state["address"],
                            password,
                            smtp_host=email_state.get("smtp_host", ""),
                            smtp_port=email_state.get("smtp_port"),
                            imap_host=email_state.get("imap_host", ""),
                            imap_port=email_state.get("imap_port"),
                        )
                    return build_email_connection_settings(
                        email_state["address"],
                        password,
                        smtp_host=email_state.get("smtp_host", ""),
                        smtp_port=email_state.get("smtp_port"),
                        imap_host=email_state.get("imap_host", ""),
                        imap_port=email_state.get("imap_port"),
                    )
                except (InvalidToken, ValueError):
                    continue
            update_email_state(
                user_id,
                connected=False,
                last_error="Saved email credentials could not be decrypted. Reconnect the email account to restore access.",
            )
            return None

        if not allow_fallback:
            return None

    fallback_address = os.getenv("GMAIL_ADDRESS")
    fallback_password = os.getenv("GMAIL_APP_PASSWORD")
    if allow_fallback and fallback_address and fallback_password:
        return build_email_connection_settings(fallback_address, fallback_password)
    return None


async def get_email_connection_settings_async(
    user_id: Optional[str] = None,
    *,
    allow_fallback: Optional[bool] = None,
) -> Optional[dict]:
    if allow_fallback is None:
        allow_fallback = user_id is None

    if user_id:
        state = await load_integrations_async(user_id)
        email_state = state.get("email", {})
        encrypted = email_state.get("encrypted_password")
        if email_state.get("connected") and email_state.get("address") and encrypted:
            primary_cipher = _get_cipher()
            for cipher in [primary_cipher, *_get_legacy_ciphers()]:
                try:
                    password = cipher.decrypt(encrypted.encode("utf-8")).decode("utf-8")
                    return build_email_connection_settings(
                        email_state["address"],
                        password,
                        smtp_host=email_state.get("smtp_host", ""),
                        smtp_port=email_state.get("smtp_port"),
                        imap_host=email_state.get("imap_host", ""),
                        imap_port=email_state.get("imap_port"),
                    )
                except (InvalidToken, ValueError):
                    continue
            await update_email_state_async(
                user_id,
                connected=False,
                last_error="Saved email credentials could not be decrypted. Reconnect the email account to restore access.",
            )
            return None

        if not allow_fallback:
            return None

    fallback_address = os.getenv("GMAIL_ADDRESS")
    fallback_password = os.getenv("GMAIL_APP_PASSWORD")
    if allow_fallback and fallback_address and fallback_password:
        return build_email_connection_settings(fallback_address, fallback_password)
    return None


def get_email_credentials(
    user_id: Optional[str] = None,
    *,
    allow_fallback: Optional[bool] = None,
) -> tuple[Optional[str], Optional[str]]:
    settings = get_email_connection_settings(user_id, allow_fallback=allow_fallback)
    if not settings:
        return None, None
    return settings["address"], settings["password"]


async def get_whatsapp_session_id_async(user_id: str) -> str:
    state = await load_integrations_async(user_id)
    return state.get("whatsapp", {}).get("session_id") or user_id


async def update_whatsapp_state_async(user_id: str, *, connected: Optional[bool] = None, last_error: Optional[str] = None):
    state = await load_integrations_async(user_id)
    whatsapp_state = state.get("whatsapp", {})
    whatsapp_state["session_id"] = user_id
    if connected is not None:
        whatsapp_state["connected"] = connected
    if last_error is not None:
        whatsapp_state["last_error"] = last_error
    state["whatsapp"] = whatsapp_state
    await save_integrations_async(user_id, state)
