import asyncio
import os
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from threading import Lock
from typing import Literal, Union, Optional, List, Dict, Any, Tuple

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, UploadFile, File, Form, Request, Depends, HTTPException, Response
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse as HTMLRedirectResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field, field_validator
from alfred_core import chat
from connectors.whatsapp import whatsapp as whatsapp_connector
from connectors.gmail import gmail as gmail_connector
from integrations import (
    build_email_connection_settings,
    clear_email_credentials,
    get_email_connection_settings_async,
    get_whatsapp_session_id_async,
    load_integrations_async,
    set_email_credentials,
    update_email_state_async,
    update_whatsapp_state_async,
)
from mail_transport import open_smtp_connection
from contacts import (
    add_contact_async as add_contact,
    delete_contact_async as delete_stored_contact,
    import_recent_whatsapp_contacts_async as import_recent_whatsapp_contacts,
    load_contacts_async as load_contacts,
    update_contact_async as update_stored_contact,
)
from storage import (
    create_account_async as create_account,
    get_storage_backend,
    get_storage_health,
    init_storage_async as init_storage,
    load_account_async as load_account,
    get_account_async as get_account,
    load_accounts_async as load_accounts,
    load_user_document_async as load_user_document,
    save_account_async as save_account,
    save_user_document_async as save_user_document,
    ensure_user_storage_async as ensure_user_storage,
    close_async_pool,
    get_is_premium_async,
    set_is_premium_async,
)
import cold_email_agent
from security_utils import (
    get_password_hash, verify_password, create_access_token, 
    get_safe_user_path, sanitize_user_id, build_storage_user_id
)
from scheduler import schedule_morning_briefing, get_pending_updates, start_followup_scheduler
import jwt
import httpx
import base64
import os
import json
import hashlib
import io
import shutil
import logging
from datetime import datetime
import re
import shlex
import subprocess
import time
import uuid
import zipfile
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as xml_escape

app = FastAPI(title="Alfred")
logger = logging.getLogger("alfred")
if not logger.handlers:
    logging.basicConfig(level=os.getenv("ALFRED_LOG_LEVEL", "INFO"))

from starlette.middleware.base import BaseHTTPMiddleware

SESSION_COOKIE_NAME = "alfred_session"
CSRF_COOKIE_NAME = "alfred_csrf"
_WA_SESSION_INDEX: dict[str, str] = {}  # session_id → storage_id for fast webhook lookups
SESSION_COOKIE_MAX_AGE = int(os.getenv("ALFRED_SESSION_HOURS", "4320")) * 3600
COOKIE_SECURE = os.getenv("ALFRED_COOKIE_SECURE", "false").lower() == "true"
MAX_REQUEST_BODY_BYTES = int(os.getenv("ALFRED_MAX_REQUEST_BYTES", str(4 * 1024 * 1024)))
AUTH_RATE_LIMIT_ATTEMPTS = int(os.getenv("ALFRED_AUTH_RATE_LIMIT_ATTEMPTS", "50"))
AUTH_RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("ALFRED_AUTH_RATE_LIMIT_WINDOW_SECONDS", "900"))
GENERAL_READ_RATE_LIMIT_ATTEMPTS = int(os.getenv("ALFRED_GENERAL_READ_RATE_LIMIT_ATTEMPTS", "120"))
GENERAL_READ_RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("ALFRED_GENERAL_READ_RATE_LIMIT_WINDOW_SECONDS", "60"))
GENERAL_WRITE_RATE_LIMIT_ATTEMPTS = int(os.getenv("ALFRED_GENERAL_WRITE_RATE_LIMIT_ATTEMPTS", "30"))
GENERAL_WRITE_RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("ALFRED_GENERAL_WRITE_RATE_LIMIT_WINDOW_SECONDS", "60"))
from connectors.whatsapp import BRIDGE_URL as WHATSAPP_BRIDGE_URL
WHATSAPP_BRIDGE_CSP_ORIGIN = WHATSAPP_BRIDGE_URL
TERMINAL_COMMANDS_ENABLED = os.getenv("ALFRED_ENABLE_TERMINAL_COMMANDS", "false").lower() == "true"
BACKUP_DIR = os.getenv("ALFRED_BACKUP_DIR", os.path.join(os.path.dirname(__file__), "backups"))
CHAT_REQUEST_TIMEOUT_SECONDS = float(os.getenv("ALFRED_CHAT_TIMEOUT_SECONDS", "30"))
ADMIN_SECRET = os.getenv("ALFRED_ADMIN_SECRET", "alfred-admin-secret")
CHAT_WORKERS = max(2, int(os.getenv("ALFRED_CHAT_WORKERS", "4")))

MAX_CHAT_MESSAGES = 50
MAX_MESSAGE_LENGTH = 4000
MAX_NAME_LENGTH = 80
MAX_TASK_LENGTH = 500
MAX_NOTE_LENGTH = 10000
MAX_UPLOAD_PROMPT_LENGTH = 1000
MAX_EMAIL_PASSWORD_LENGTH = 128
MAX_EMAIL_HOST_LENGTH = 253
MAX_PHONE_LENGTH = 16
MAX_CONTACT_NOTES_LENGTH = 1000
CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
EMAIL_RE = re.compile(r"^[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,63}$", re.IGNORECASE)
EMAIL_HOST_RE = re.compile(r"^[A-Z0-9](?:[A-Z0-9.-]{0,251}[A-Z0-9])?$", re.IGNORECASE)
SAFE_COMMAND_ID_RE = re.compile(r"^[a-f0-9]{8}$")
SAFE_TASK_ID_RE = re.compile(r"^\d{1,10}$")
SAFE_CONTACT_ID_RE = re.compile(r"^[a-f0-9]{12}$")
TRUSTED_ORIGINS = {origin.strip() for origin in os.getenv("ALFRED_TRUSTED_ORIGINS", "").split(",") if origin.strip()}
SAFE_TERMINAL_COMMANDS = {"open", "mkdir", "touch", "ls", "pwd"}
SERVER_INSTANCE_TOKEN = os.getenv("ALFRED_SERVER_INSTANCE_TOKEN", uuid.uuid4().hex)
CHAT_EXECUTOR = ThreadPoolExecutor(max_workers=CHAT_WORKERS, thread_name_prefix="alfred-chat")
WHATSAPP_CONTACT_SYNC_TASKS: dict[str, asyncio.Task] = {}

class LimitUploadSize(BaseHTTPMiddleware):
    def __init__(self, app, max_upload_size: int):
        super().__init__(app)
        self.max_upload_size = max_upload_size

    async def dispatch(self, request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH"}:
            content_length = request.headers.get('content-length')
            if content_length:
                try:
                    if int(content_length) > self.max_upload_size:
                        return JSONResponse(status_code=413, content={"detail": "Payload Too Large"})
                except ValueError:
                    return JSONResponse(status_code=400, content={"detail": "Invalid Content-Length"})
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), geolocation=(), interest-cohort=()"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        if request.url.scheme == "https" or COOKIE_SECURE:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "img-src 'self' data: https:; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "script-src 'self' 'unsafe-inline'; "
            f"connect-src 'self' http://127.0.0.1:8000 http://localhost:8000 {WHATSAPP_BRIDGE_CSP_ORIGIN} https://ipapi.co https:; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        return response

class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def _get_client_key(self, request: Request) -> str:
        forwarded_for = request.headers.get("x-forwarded-for", "")
        if forwarded_for:
            return forwarded_for.split(",")[0].strip()
        if request.client and request.client.host:
            return request.client.host
        return "unknown"

    def _get_rule(self, request: Request) -> Optional[Tuple[str, int, int]]:
        if request.method == "OPTIONS":
            return None
        path = request.url.path
        if path.startswith("/static/") or path in {"/health/live", "/health/ready"}:
            return None
        if path in {"/api/auth/login", "/api/auth/register"}:
            return (f"auth:{path}", AUTH_RATE_LIMIT_ATTEMPTS, AUTH_RATE_LIMIT_WINDOW_SECONDS)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            return (f"write:{path}", GENERAL_WRITE_RATE_LIMIT_ATTEMPTS, GENERAL_WRITE_RATE_LIMIT_WINDOW_SECONDS)
        return (f"read:{path}", GENERAL_READ_RATE_LIMIT_ATTEMPTS, GENERAL_READ_RATE_LIMIT_WINDOW_SECONDS)

    async def dispatch(self, request: Request, call_next):
        rule = self._get_rule(request)
        if not rule:
            return await call_next(request)

        bucket, limit, window_seconds = rule
        client_key = f"{self._get_client_key(request)}:{bucket}"
        now = time.time()

        with self._lock:
            entries = self._requests[client_key]
            while entries and now - entries[0] >= window_seconds:
                entries.popleft()
            if len(entries) >= limit:
                retry_after = max(1, int(window_seconds - (now - entries[0])))
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Too many requests. Please try again later."},
                    headers={"Retry-After": str(retry_after)},
                )
            entries.append(now)
            remaining = max(0, limit - len(entries))

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Window"] = str(window_seconds)
        return response


class CSRFMiddleware(BaseHTTPMiddleware):
    def _is_exempt(self, request: Request) -> bool:
        if request.method in {"GET", "HEAD", "OPTIONS"}:
            return True
        path = request.url.path
        return path in {"/api/auth/login", "/api/auth/register", "/api/auth/csrf"} or path.startswith("/static/")

    async def dispatch(self, request: Request, call_next):
        if self._is_exempt(request):
            return await call_next(request)

        session_cookie = request.cookies.get(SESSION_COOKIE_NAME)
        if not session_cookie:
            return await call_next(request)

        origin = request.headers.get("origin")
        if origin:
            allowed_origins = set(TRUSTED_ORIGINS)
            allowed_origins.add(str(request.base_url).rstrip("/"))
            if origin.rstrip("/") not in allowed_origins:
                return JSONResponse(status_code=403, content={"detail": "Invalid request origin."})

        csrf_cookie = request.cookies.get(CSRF_COOKIE_NAME)
        csrf_header = request.headers.get("X-CSRF-Token", "")
        if not csrf_cookie or not csrf_header or csrf_cookie != csrf_header:
            return JSONResponse(status_code=403, content={"detail": "CSRF validation failed."})

        return await call_next(request)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith("/static/"):
            return await call_next(request)
        started_at = time.time()
        response = await call_next(request)
        duration_ms = (time.time() - started_at) * 1000
        logger.info("%s %s -> %s (%.1fms)", request.method, request.url.path, response.status_code, duration_ms)
        return response

app.add_middleware(LimitUploadSize, max_upload_size=MAX_REQUEST_BODY_BYTES)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(CSRFMiddleware)
app.add_middleware(RequestLoggingMiddleware)
app.add_middleware(SecurityHeadersMiddleware)

app.mount("/static", StaticFiles(directory="static"), name="static")
if os.path.exists("dashboard/dist"):
    app.mount("/premium", StaticFiles(directory="dashboard/dist", html=True), name="premium")


@app.on_event("startup")
async def startup_checks():
    await init_storage()
    bridge_ready = whatsapp_connector.ensure_bridge_running()
    if bridge_ready:
        try:
            accounts = await load_accounts()
            whatsapp_connector.restore_sessions(accounts)
            # Populate session index for fast webhook lookups
            for email, acct in accounts.items():
                sid = acct.get("storage_id", "")
                if sid:
                    _WA_SESSION_INDEX[sid] = sid
        except Exception:
            logger.exception("Could not restore WhatsApp sessions on startup")
    try:
        backup_path = maybe_create_backup()
        if backup_path:
            logger.info("Database backup available at %s", backup_path)
    except Exception:
        logger.exception("Could not create startup backup")

    # ── Auto-schedule morning briefings for all existing users ──────────────
    try:
        accounts = await load_accounts()
        for acct in accounts:
            uid = acct.get("storage_id") or acct.get("user_id")
            name = acct.get("display_name") or acct.get("name") or uid
            prefs_doc = await load_user_document(uid, "preferences") or {}
            bh = int(prefs_doc.get("briefing_hour", 8))
            bm = int(prefs_doc.get("briefing_minute", 0))
            if uid:
                schedule_morning_briefing(uid, display_name=name, hour=bh, minute=bm)
                logger.info("Scheduled morning briefing for %s at %02d:%02d", uid, bh, bm)
    except Exception:
        logger.exception("Could not schedule morning briefings on startup")

    # ── Start follow-up engine ────────────────────────────────────────────────
    try:
        start_followup_scheduler()
        logger.info("Follow-up engine started")
    except Exception:
        logger.exception("Could not start follow-up scheduler")


@app.on_event("shutdown")
async def shutdown_event():
    await close_async_pool()

def get_user_file(filename: str, user_id: str) -> str:
    path = get_safe_user_path(user_id, filename)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path

class Token(BaseModel):
    access_token: str
    token_type: str

class User(BaseModel):
    name: str
    email: str
    storage_id: str

security = HTTPBearer(auto_error=False)


def _ensure_no_control_chars(value: str, field_name: str) -> str:
    if CONTROL_CHARS_RE.search(value):
        raise ValueError(f"{field_name} contains unsupported control characters.")
    return value


def sanitize_single_line_text(value: str, field_name: str, *, max_length: int, min_length: int = 1) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string.")
    cleaned = re.sub(r"\s+", " ", value).strip()
    cleaned = _ensure_no_control_chars(cleaned, field_name)
    if len(cleaned) < min_length:
        raise ValueError(f"{field_name} is required.")
    if len(cleaned) > max_length:
        raise ValueError(f"{field_name} must be {max_length} characters or fewer.")
    return cleaned


def sanitize_multiline_text(value: str, field_name: str, *, max_length: int, min_length: int = 1) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string.")
    cleaned = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    cleaned = _ensure_no_control_chars(cleaned, field_name)
    if len(cleaned) < min_length:
        raise ValueError(f"{field_name} is required.")
    if len(cleaned) > max_length:
        raise ValueError(f"{field_name} must be {max_length} characters or fewer.")
    return cleaned


def sanitize_email(value: str) -> str:
    cleaned = sanitize_single_line_text(value, "email", max_length=254, min_length=3).lower()
    if not EMAIL_RE.fullmatch(cleaned):
        raise ValueError("Email address is invalid.")
    return cleaned


def sanitize_password(value: str, *, min_length: int) -> str:
    if not isinstance(value, str):
        raise ValueError("Password must be a string.")
    _ensure_no_control_chars(value, "password")
    if len(value) < min_length:
        raise ValueError(f"Password must be at least {min_length} characters long.")
    if len(value) > 128:
        raise ValueError("Password must be 128 characters or fewer.")
    return value


def sanitize_phone_number(value: str) -> str:
    cleaned = sanitize_single_line_text(value, "number", max_length=32, min_length=3)
    normalized = re.sub(r"[^\d+]", "", cleaned)
    if normalized.count("+") > 1 or ("+" in normalized[1:]):
        raise ValueError("Invalid phone number format.")
    digits = normalized[1:] if normalized.startswith("+") else normalized
    if not re.fullmatch(r"^[1-9]\d{7,14}$", digits):
        raise ValueError("Invalid phone number format.")
    return normalized


def sanitize_optional_phone_number(value: Optional[str]) -> str:
    if value is None:
        return ""
    trimmed = str(value).strip()
    if not trimmed:
        return ""
    return sanitize_phone_number(trimmed)


def sanitize_optional_email(value: Optional[str]) -> str:
    if value is None:
        return ""
    trimmed = str(value).strip()
    if not trimmed:
        return ""
    return sanitize_email(trimmed)


def sanitize_optional_email_host(value: Optional[str], field_name: str) -> str:
    if value is None:
        return ""
    trimmed = str(value).strip().lower()
    if not trimmed:
        return ""
    cleaned = sanitize_single_line_text(trimmed, field_name, max_length=MAX_EMAIL_HOST_LENGTH, min_length=1)
    if ".." in cleaned or not EMAIL_HOST_RE.fullmatch(cleaned):
        raise ValueError(f"{field_name} is invalid.")
    return cleaned


def sanitize_optional_multiline_text(value: Optional[str], field_name: str, *, max_length: int) -> str:
    if value is None:
        return ""
    normalized = str(value).replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return ""
    return sanitize_multiline_text(normalized, field_name, max_length=max_length)


def ensure_safe_id(value: str, pattern: re.Pattern[str], field_name: str) -> str:
    cleaned = sanitize_single_line_text(value, field_name, max_length=64, min_length=1)
    if not pattern.fullmatch(cleaned):
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}.")
    return cleaned


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def ensure_backup_dir() -> str:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    return BACKUP_DIR


def maybe_create_backup() -> Optional[str]:
    storage = get_storage_health()
    if storage["backend"] != "sqlite":
        return None
    db_path = storage["db_path"]
    if not storage["exists"] or not os.path.exists(db_path):
        return None

    backup_dir = ensure_backup_dir()
    today = datetime.utcnow().strftime("%Y%m%d")
    backup_path = os.path.join(backup_dir, f"alfred-{today}.db")
    if os.path.exists(backup_path):
        return backup_path
    shutil.copy2(db_path, backup_path)
    return backup_path


def ensure_whatsapp_bridge_running() -> bool:
    """Kept for backward compatibility; delegates to the WhatsApp connector."""
    return whatsapp_connector.ensure_bridge_running()


def is_safe_terminal_command(command: str) -> bool:
    try:
        args = shlex.split(command)
    except ValueError:
        return False
    if not args:
        return False
    binary = os.path.basename(args[0])
    if binary not in SAFE_TERMINAL_COMMANDS:
        return False
    for arg in args[1:]:
        if any(token in arg for token in ("..", ";", "&&", "||", "|", ">", "<", "`", "$(")):
            return False
    return True


def set_session_cookie(response: Response, token: str):
    csrf_token = uuid.uuid4().hex + uuid.uuid4().hex
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        max_age=SESSION_COOKIE_MAX_AGE,
        path="/",
    )
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        secure=COOKIE_SECURE,
        samesite="lax",
        max_age=SESSION_COOKIE_MAX_AGE,
        path="/",
    )
    return csrf_token


def clear_session_cookie(response: Response):
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    response.delete_cookie(key=CSRF_COOKIE_NAME, path="/")

async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
) -> User:
    from security_utils import SECRET_KEY, ALGORITHM
    token = credentials.credentials if credentials else request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        token_name: str = payload.get("name")
        if email is None or token_name is None:
            raise HTTPException(status_code=401, detail="Invalid token")
        user_data = await get_account(email)
        if not user_data:
            raise HTTPException(status_code=401, detail="Invalid token")
        name = user_data.get("name", token_name)
        storage_id = await ensure_user_storage(user_data, email, name)
        return User(name=name, email=email, storage_id=storage_id)
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

class Message(StrictModel):
    role: Literal["user", "assistant", "system"]
    content: str

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        return sanitize_multiline_text(value, "message", max_length=MAX_MESSAGE_LENGTH)

class ChatRequest(StrictModel):
    messages: list[Message]

    @field_validator("messages")
    @classmethod
    def validate_messages(cls, value: list[Message]) -> list[Message]:
        if not value:
            raise ValueError("At least one message is required.")
        if len(value) > MAX_CHAT_MESSAGES:
            raise ValueError(f"No more than {MAX_CHAT_MESSAGES} messages are allowed.")
        return value

class RegisterRequest(StrictModel):
    name: str
    email: str
    password: str
    accepts_terms: bool

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return sanitize_single_line_text(value, "name", max_length=MAX_NAME_LENGTH)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        return sanitize_email(value)

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        return sanitize_password(value, min_length=8)

class LoginRequest(StrictModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        return sanitize_email(value)

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        return sanitize_password(value, min_length=1)

class PreferencesUpdateRequest(StrictModel):
    widget_preferences: Optional[str] = None
    location_data: Optional[str] = None
    onboarding_complete: Optional[int] = None

class EmailIntegrationRequest(StrictModel):
    address: str
    app_password: str
    smtp_host: Optional[str] = ""
    smtp_port: Optional[int] = None
    imap_host: Optional[str] = ""
    imap_port: Optional[int] = None

    @field_validator("address")
    @classmethod
    def validate_address(cls, value: str) -> str:
        return sanitize_email(value)

    @field_validator("app_password")
    @classmethod
    def validate_app_password(cls, value: str) -> str:
        cleaned = re.sub(r"\s+", "", value or "")
        return sanitize_single_line_text(cleaned, "app_password", max_length=MAX_EMAIL_PASSWORD_LENGTH, min_length=8)

    @field_validator("smtp_host")
    @classmethod
    def validate_smtp_host(cls, value: Optional[str]) -> str:
        return sanitize_optional_email_host(value, "smtp_host")

    @field_validator("imap_host")
    @classmethod
    def validate_imap_host(cls, value: Optional[str]) -> str:
        return sanitize_optional_email_host(value, "imap_host")

    @field_validator("smtp_port", "imap_port")
    @classmethod
    def validate_mail_port(cls, value: Optional[int]) -> Optional[int]:
        if value is None:
            return None
        if value < 1 or value > 65535:
            raise ValueError("Mail server port must be between 1 and 65535.")
        return value


class TaskCreateRequest(StrictModel):
    text: str

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return sanitize_single_line_text(value, "task text", max_length=MAX_TASK_LENGTH)


class NoteCreateRequest(StrictModel):
    content: str

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        return sanitize_multiline_text(value, "note content", max_length=MAX_NOTE_LENGTH)


class PomodoroUpdateRequest(StrictModel):
    is_running: Optional[bool] = None
    seconds_left: Optional[int] = Field(default=None, ge=0, le=24 * 60 * 60)
    phase: Optional[Literal["work", "break", "longbreak"]] = None
    session: Optional[int] = Field(default=None, ge=0, le=100)


class WhatsAppSendRequest(StrictModel):
    number: str
    message: str

    @field_validator("number")
    @classmethod
    def validate_number(cls, value: str) -> str:
        return sanitize_phone_number(value)

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        return sanitize_multiline_text(value, "message", max_length=2000)


class OnboardingRequest(StrictModel):
    location: Optional[dict] = None
    widgets: Optional[List[str]] = None

class ShortcutSettingsRequest(StrictModel):
    mode: Literal["off", "voice", "clap", "both"]
    threshold: float = Field(ge=0.001, le=1.0)


class ContactCreateRequest(StrictModel):
    name: str
    phone: Optional[str] = ""
    email: Optional[str] = ""
    notes: Optional[str] = ""

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return sanitize_single_line_text(value, "name", max_length=MAX_NAME_LENGTH)

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: Optional[str]) -> str:
        return sanitize_optional_phone_number(value)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: Optional[str]) -> str:
        return sanitize_optional_email(value)

    @field_validator("notes")
    @classmethod
    def validate_notes(cls, value: Optional[str]) -> str:
        return sanitize_optional_multiline_text(value, "notes", max_length=MAX_CONTACT_NOTES_LENGTH)


class ContactUpdateRequest(StrictModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    notes: Optional[str] = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return sanitize_single_line_text(value, "name", max_length=MAX_NAME_LENGTH)

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return sanitize_optional_phone_number(value)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return sanitize_optional_email(value)

    @field_validator("notes")
    @classmethod
    def validate_notes(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return sanitize_optional_multiline_text(value, "notes", max_length=MAX_CONTACT_NOTES_LENGTH)

# ── Authentication ─────────────────────────────────────────────────────────────

def _resolve_legacy_user_dir(user_id: Optional[str]) -> Optional[str]:
    if not user_id:
        return None
    base_dir = os.path.abspath("users")
    candidate = os.path.abspath(os.path.join(base_dir, user_id))
    try:
        if os.path.commonpath([base_dir, candidate]) != base_dir:
            return None
    except ValueError:
        return None
    return candidate

async def ensure_personalized_memory(storage_id: str, display_name: str, email_key: str):
    from memory import load_memory_async, save_memory_async

    memory = await load_memory_async(storage_id)
    if memory.get("name") != display_name:
        memory["name"] = display_name
    if memory.get("email") != email_key:
        memory["email"] = email_key
    await save_memory_async(memory, storage_id)

async def ensure_user_storage(user_data: dict, email_key: str, display_name: str) -> str:
    storage_id = user_data.get("storage_id") or build_storage_user_id(email_key)
    changed = user_data.get("storage_id") != storage_id
    user_data["storage_id"] = storage_id

    if get_storage_backend() == "sqlite":
        target_dir = os.path.dirname(get_user_file("memory.json", storage_id))
        os.makedirs(target_dir, exist_ok=True)

        legacy_candidates = []
        for candidate in (
            display_name,
            sanitize_user_id(display_name),
            email_key,
            sanitize_user_id(email_key),
        ):
            if candidate and candidate not in legacy_candidates and candidate != storage_id:
                legacy_candidates.append(candidate)

        for legacy_id in legacy_candidates:
            legacy_dir = _resolve_legacy_user_dir(legacy_id)
            if not legacy_dir or not os.path.isdir(legacy_dir):
                continue
            if os.path.abspath(legacy_dir) == os.path.abspath(target_dir):
                continue
            for entry in os.listdir(legacy_dir):
                src = os.path.join(legacy_dir, entry)
                dst = os.path.join(target_dir, entry)
                if not os.path.exists(dst):
                    shutil.move(src, dst)
            if not os.listdir(legacy_dir):
                os.rmdir(legacy_dir)
            changed = True

    await ensure_personalized_memory(storage_id, display_name, email_key)

    if changed:
        await save_account(email_key, user_data)
    return storage_id


async def fetch_whatsapp_bridge_status(session_id: str) -> dict:
    """Kept for backward compatibility; delegates to connector."""
    return await whatsapp_connector._bridge_status_async(session_id)


async def ensure_whatsapp_session_ready(session_id: str, *, wait_seconds: int = 8) -> dict:
    """Kept for backward compatibility; delegates to connector."""
    return await whatsapp_connector.ensure_ready_async(session_id, wait_seconds=wait_seconds)


async def sync_whatsapp_contacts_from_bridge(user_id: str, session_id: str) -> dict[str, int]:
    """Kept for backward compatibility; delegates to connector."""
    return await whatsapp_connector.sync_contacts_async(user_id, session_id)


def schedule_whatsapp_contact_sync(user_id: str, session_id: str) -> None:
    existing_task = WHATSAPP_CONTACT_SYNC_TASKS.get(user_id)
    if existing_task and not existing_task.done():
        return

    async def _runner() -> None:
        try:
            await sync_whatsapp_contacts_from_bridge(user_id, session_id)
        except Exception:
            logger.exception("Could not auto-sync WhatsApp contacts for %s", user_id)
        finally:
            current = WHATSAPP_CONTACT_SYNC_TASKS.get(user_id)
            if current is task:
                WHATSAPP_CONTACT_SYNC_TASKS.pop(user_id, None)

    task = asyncio.create_task(_runner())
    WHATSAPP_CONTACT_SYNC_TASKS[user_id] = task


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    details = []
    for error in exc.errors()[:5]:
        loc = ".".join(str(part) for part in error.get("loc", []) if part != "body")
        message = error.get("msg", "Invalid value")
        details.append(f"{loc}: {message}" if loc else message)
    return JSONResponse(
        status_code=422,
        content={"ok": False, "error": "Invalid request payload.", "details": details},
    )

@app.post("/api/auth/register")
async def register_user(request: Request, req: RegisterRequest, response: Response):
    if not req.accepts_terms:
        return {"ok": False, "error": "You must accept the terms and conditions."}

    email_key = req.email
    hashed_pw = get_password_hash(req.password)
    new_user_name = req.name
    storage_id = build_storage_user_id(email_key)
    account_data = {
        "name": new_user_name,
        "password_hash": hashed_pw,
        "created_at": datetime.now().isoformat(),
        "storage_id": storage_id,
    }
    if not await create_account(email_key, account_data):
        return {"ok": False, "error": "An account with this email already exists."}

    await ensure_personalized_memory(storage_id, new_user_name, email_key)

    token = create_access_token(data={"sub": email_key, "name": new_user_name, "storage_id": storage_id})
    set_session_cookie(response, token)
    return {"ok": True, "name": new_user_name, "access_token": token}


@app.post("/api/auth/login")
async def login_user(request: Request, req: LoginRequest, response: Response):
    print(f"DEBUG: Login attempt for email: {req.email}")
    email_key = req.email
    user_data = await get_account(email_key)
    if not user_data:
        return {"ok": False, "error": "Invalid email or password."}

    if not verify_password(req.password, user_data["password_hash"]):
        # Fallback for old SHA256 passwords (security upgrade)
        old_hashed_pw = hashlib.sha256(req.password.encode()).hexdigest()
        if user_data["password_hash"] == old_hashed_pw:
            # Upgrade to bcrypt
            user_data["password_hash"] = get_password_hash(req.password)
            await save_account(email_key, user_data)
        else:
            return {"ok": False, "error": "Invalid email or password."}

    storage_id = await ensure_user_storage(user_data, email_key, user_data["name"])
    token = create_access_token(data={"sub": email_key, "name": user_data["name"], "storage_id": storage_id})
    set_session_cookie(response, token)
    return {"ok": True, "name": user_data["name"], "access_token": token}


@app.post("/api/auth/logout")
def logout_user(response: Response):
    clear_session_cookie(response)
    response.delete_cookie(key=CSRF_COOKIE_NAME, path="/")
    return {"ok": True}


@app.post("/api/auth/csrf")
async def get_csrf_token(response: Response):
    csrf_token = uuid.uuid4().hex + uuid.uuid4().hex
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        secure=COOKIE_SECURE,
        samesite="lax",
        max_age=SESSION_COOKIE_MAX_AGE,
        path="/",
    )
    return {"csrf_token": csrf_token}


@app.get("/api/auth/me")
async def auth_me(current_user: User = Depends(get_current_user)):
    user_data = await get_account(current_user.email)
    needs_onboarding = not user_data.get("onboarding_complete", False) if user_data else True
    return {
        "ok": True,
        "name": current_user.name,
        "email": current_user.email,
        "needs_onboarding": needs_onboarding,
        "location": user_data.get("location_data") if user_data else None,
        "widgets": user_data.get("widget_preferences") if user_data else None
    }

@app.post("/api/user/onboarding")
async def post_onboarding(req: OnboardingRequest, current_user: User = Depends(get_current_user)):
    user_data = await get_account(current_user.email)
    if not user_data:
        raise HTTPException(status_code=404, detail="User not found")
    
    user_data["onboarding_complete"] = 1
    user_data["location_data"] = json.dumps(req.location) if req.location else None
    user_data["widget_preferences"] = json.dumps(req.widgets) if req.widgets else None
    
    await save_account(current_user.email, user_data)
    return {"ok": True}


@app.get("/api/integrations")
async def get_integrations(current_user: User = Depends(get_current_user)):
    state = await load_integrations_async(current_user.storage_id)
    email_state = state.get("email", {})
    if email_state.get("connected") and email_state.get("address"):
        if not await get_email_connection_settings_async(current_user.storage_id, allow_fallback=False):
            state = await load_integrations_async(current_user.storage_id)
            email_state = state.get("email", {})
    whatsapp_session_id = await get_whatsapp_session_id_async(current_user.storage_id)
    whatsapp_info = {
        "connected": state.get("whatsapp", {}).get("connected", False),
        "session_id": whatsapp_session_id,
        "last_error": state.get("whatsapp", {}).get("last_error", ""),
        "qr": None,
    }

    was_connected = state.get("whatsapp", {}).get("connected", False)

    try:
        bridge_status = await fetch_whatsapp_bridge_status(whatsapp_session_id)
        # If the user had WhatsApp connected but the bridge session isn't active
        # (e.g. after Alfred or bridge restart), auto-resume using saved LocalAuth data.
        if was_connected and not bridge_status.get("ready") and not bridge_status.get("qr") and not bridge_status.get("initializing"):
            try:
                async with httpx.AsyncClient() as client:
                    await client.post(
                        f"{WHATSAPP_BRIDGE_URL}/session/start",
                        json={"sessionId": whatsapp_session_id},
                        timeout=10,
                    )
                bridge_status = await fetch_whatsapp_bridge_status(whatsapp_session_id)
            except Exception:
                pass
        whatsapp_info["connected"] = bool(bridge_status.get("ready"))
        whatsapp_info["last_error"] = bridge_status.get("lastError", whatsapp_info["last_error"])
        whatsapp_info["qr"] = bridge_status.get("qr")
        await update_whatsapp_state_async(
            current_user.storage_id,
            connected=whatsapp_info["connected"],
            last_error=whatsapp_info["last_error"] or "",
        )
        if whatsapp_info["connected"]:
            schedule_whatsapp_contact_sync(current_user.storage_id, whatsapp_session_id)
    except Exception as e:
        whatsapp_info["last_error"] = "WhatsApp bridge unavailable"
        await update_whatsapp_state_async(current_user.storage_id, connected=False, last_error=str(e))

    return {
        "email": {
            "connected": bool(email_state.get("connected")),
            "address": email_state.get("address", ""),
            "smtp_host": email_state.get("smtp_host", ""),
            "smtp_port": email_state.get("smtp_port"),
            "imap_host": email_state.get("imap_host", ""),
            "imap_port": email_state.get("imap_port"),
            "last_error": email_state.get("last_error", ""),
        },
        "whatsapp": whatsapp_info,
    }


@app.post("/api/integrations/email")
async def connect_email(req: EmailIntegrationRequest, current_user: User = Depends(get_current_user)):
    import smtplib
    if req.smtp_port and not req.smtp_host:
        raise HTTPException(status_code=400, detail="Enter an SMTP host when using a custom SMTP port.")
    if req.imap_port and not req.imap_host:
        raise HTTPException(status_code=400, detail="Enter an IMAP host when using a custom IMAP port.")

    settings = build_email_connection_settings(
        req.address,
        req.app_password,
        smtp_host=req.smtp_host,
        smtp_port=req.smtp_port,
        imap_host=req.imap_host,
        imap_port=req.imap_port,
    )
    try:
        with open_smtp_connection(settings["smtp_host"], settings["smtp_port"], timeout=10) as server:
            server.login(settings["address"], settings["password"])
    except smtplib.SMTPAuthenticationError:
        await update_email_state_async(
            current_user.storage_id,
            connected=False,
            last_error="Email authentication failed. Use the mailbox password, or an app password if your provider requires one.",
            address=req.address,
        )
        raise HTTPException(
            status_code=400,
            detail="Email authentication failed. Use the mailbox password, or an app password if your provider requires one.",
        )
    except Exception as e:
        await update_email_state_async(
            current_user.storage_id,
            connected=False,
            last_error=f"Could not connect to the outgoing mail server: {e}",
            address=req.address,
        )
        raise HTTPException(status_code=400, detail=f"Could not connect to the outgoing mail server: {e}")
    set_email_credentials(
        current_user.storage_id,
        req.address,
        req.app_password,
        smtp_host=req.smtp_host,
        smtp_port=req.smtp_port,
        imap_host=req.imap_host,
        imap_port=req.imap_port,
    )
    await update_email_state_async(current_user.storage_id, connected=True, last_error="", address=req.address)
    return {
        "ok": True,
        "connected": True,
        "address": req.address,
        "smtp_host": settings["smtp_host"],
        "smtp_port": settings["smtp_port"],
        "imap_host": settings["imap_host"],
        "imap_port": settings["imap_port"],
    }


@app.delete("/api/integrations/email")
def disconnect_email(current_user: User = Depends(get_current_user)):
    clear_email_credentials(current_user.storage_id)
    return {"ok": True}


# ── Gmail OAuth routes ────────────────────────────────────────────────────────

@app.get("/api/integrations/gmail/auth-url")
def gmail_auth_url(current_user: User = Depends(get_current_user), request: Request = None):
    """
    Returns the Google OAuth consent URL.
    The frontend should open this URL (new tab or redirect) to start the Gmail
    connection flow.
    """
    if not gmail_connector.is_configured():
        raise HTTPException(
            status_code=501,
            detail="Gmail OAuth is not configured. Add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET to .env.",
        )
    redirect_uri = str(request.base_url).rstrip("/") + "/api/integrations/gmail/callback"
    url = gmail_connector.get_auth_url(current_user.storage_id, redirect_uri=redirect_uri)
    return {"url": url}


@app.get("/api/integrations/gmail/callback")
async def gmail_oauth_callback(
    code: str = "",
    state: str = "",
    error: str = "",
    request: Request = None,
):
    """
    Google redirects here after the user approves (or denies) access.
    Exchanges the code for tokens, then redirects to the settings page.

    Note: this endpoint does NOT use get_current_user — Google's redirect doesn't
    carry Alfred's session cookie.  We identify the user via the OAuth state token
    that was saved in their integrations record at auth-url time.
    """
    if error:
        return HTMLRedirectResponse(f"/chat-ui?gmail_error={error}")

    if not code:
        raise HTTPException(status_code=400, detail="Missing OAuth code.")

    # Find user by matching saved _oauth_state against the incoming state.
    try:
        accounts = await load_accounts()
    except Exception:
        raise HTTPException(status_code=500, detail="Could not load accounts.")

    storage_id = None
    for _email, account_data in accounts.items():
        sid = account_data.get("storage_id", "")
        if not sid:
            continue
        saved = gmail_connector.load_state(sid)
        if saved.get("_oauth_state") == state:
            storage_id = sid
            break

    if not storage_id:
        raise HTTPException(status_code=400, detail="OAuth state mismatch. Try connecting again.")

    redirect_uri = str(request.base_url).rstrip("/") + "/api/integrations/gmail/callback"
    try:
        result = gmail_connector.handle_callback(
            storage_id,
            code=code,
            state=state,
            redirect_uri=redirect_uri,
        )
    except Exception as exc:
        logger.exception("Gmail OAuth callback failed for %s", storage_id)
        return HTMLRedirectResponse(f"/chat-ui?gmail_error={str(exc)[:120]}")

    email_addr = result.get("email", "")
    logger.info("Gmail connected for %s (%s)", storage_id, email_addr)
    return HTMLRedirectResponse(f"/chat-ui?gmail_connected=1&gmail_email={email_addr}")


@app.delete("/api/integrations/gmail")
def disconnect_gmail(current_user: User = Depends(get_current_user)):
    """Revoke Gmail OAuth tokens and clear the connection."""
    gmail_connector.disconnect(current_user.storage_id)
    return {"ok": True}


@app.get("/api/integrations/gmail/status")
def gmail_status(current_user: User = Depends(get_current_user)):
    """Return the current Gmail connection status for this user."""
    return gmail_connector.get_status(current_user.storage_id)


class WhatsAppReadyWebhook(BaseModel):
    sessionId: str


@app.post("/api/integrations/whatsapp/on-ready")
async def whatsapp_on_ready(req: WhatsAppReadyWebhook, request: Request):
    """
    Internal webhook called by the Node bridge the moment a WhatsApp session
    becomes ready. Triggers an immediate contact sync and updates connected state
    so Alfred knows contacts are available without waiting for a UI action.

    Only accepted from localhost — the bridge and Python always run on the same machine.
    """
    client_host = request.client.host if request.client else ""
    if client_host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403, detail="Forbidden")

    session_id = req.sessionId
    if not session_id:
        raise HTTPException(status_code=400, detail="sessionId is required")

    # Fast path: look up via in-memory session index (populated on WhatsApp connect)
    storage_id = _WA_SESSION_INDEX.get(session_id)
    if not storage_id:
        # Fallback: query integrations doc for this session_id
        # The WhatsApp session_id is always the user's storage_id
        storage_id = session_id if (await load_account(session_id)) else None
        if storage_id:
            _WA_SESSION_INDEX[session_id] = storage_id

    if not storage_id:
        logger.warning("on-ready webhook: no user found for session %s", session_id)
        return {"ok": False, "error": "No user found for session"}

    # Mark as connected and kick off contact sync in the background.
    await update_whatsapp_state_async(storage_id, connected=True, last_error="")
    schedule_whatsapp_contact_sync(storage_id, session_id)
    logger.info("WhatsApp on-ready: triggered contact sync for %s", storage_id)
    return {"ok": True}


@app.post("/api/integrations/whatsapp/start")
async def start_whatsapp(current_user: User = Depends(get_current_user)):
    session_id = await get_whatsapp_session_id_async(current_user.storage_id)
    ensure_whatsapp_bridge_running()
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{WHATSAPP_BRIDGE_URL}/session/start",
                json={"sessionId": session_id},
                timeout=10,
            )
        status = await fetch_whatsapp_bridge_status(session_id)
        await update_whatsapp_state_async(
            current_user.storage_id,
            connected=bool(status.get("ready")),
            last_error=status.get("lastError", ""),
        )
        if status.get("ready"):
            _WA_SESSION_INDEX[session_id] = current_user.storage_id
            schedule_whatsapp_contact_sync(current_user.storage_id, session_id)
        return {"ok": True, **status}
    except Exception as e:
        await update_whatsapp_state_async(current_user.storage_id, connected=False, last_error=str(e))
        return {"ok": False, "error": str(e)}


@app.get("/api/integrations/whatsapp/status")
async def whatsapp_status(current_user: User = Depends(get_current_user)):
    session_id = await get_whatsapp_session_id_async(current_user.storage_id)
    try:
        status = await fetch_whatsapp_bridge_status(session_id)
        await update_whatsapp_state_async(
            current_user.storage_id,
            connected=bool(status.get("ready")),
            last_error=status.get("lastError", ""),
        )
        if status.get("ready"):
            _WA_SESSION_INDEX[session_id] = current_user.storage_id
            schedule_whatsapp_contact_sync(current_user.storage_id, session_id)
        return {"ok": True, **status}
    except Exception as e:
        await update_whatsapp_state_async(current_user.storage_id, connected=False, last_error=str(e))
        return {"ok": False, "error": str(e), "ready": False, "qr": None}


@app.delete("/api/integrations/whatsapp")
async def disconnect_whatsapp(current_user: User = Depends(get_current_user)):
    session_id = await get_whatsapp_session_id_async(current_user.storage_id)
    try:
        async with httpx.AsyncClient() as client:
            await client.delete(
                f"{WHATSAPP_BRIDGE_URL}/session/logout",
                params={"sessionId": session_id},
                timeout=15,
            )
        await update_whatsapp_state_async(current_user.storage_id, connected=False, last_error="")
        return {"ok": True}
    except Exception as e:
        await update_whatsapp_state_async(current_user.storage_id, connected=False, last_error=str(e))
        return {"ok": False, "error": str(e)}

# ── Command Approval System ───────────────────────────────────────────────────

PENDING_COMMANDS = {} # {cmd_id: {"user": storage_id, "command": cmd, "timestamp": ts}}

@app.get("/api/commands/pending")
def list_pending_commands(current_user: User = Depends(get_current_user)):
    return [
        {"id": cid, "command": c["command"], "time": c["timestamp"]}
        for cid, c in PENDING_COMMANDS.items()
        if c["user"] == current_user.storage_id
    ]

@app.post("/api/commands/approve/{cmd_id}")
async def approve_command(cmd_id: str, current_user: User = Depends(get_current_user)):
    cmd_id = ensure_safe_id(cmd_id, SAFE_COMMAND_ID_RE, "command id")
    if not TERMINAL_COMMANDS_ENABLED:
        raise HTTPException(status_code=403, detail="Terminal commands are disabled.")
    cmd_data = PENDING_COMMANDS.get(cmd_id)
    if not cmd_data or cmd_data["user"] != current_user.storage_id:
        raise HTTPException(status_code=404, detail="Command not found")
    if not is_safe_terminal_command(cmd_data["command"]):
        del PENDING_COMMANDS[cmd_id]
        raise HTTPException(status_code=400, detail="Command rejected by safety policy.")
    
    try:
        args = shlex.split(cmd_data["command"])
        subprocess.Popen(args)
        del PENDING_COMMANDS[cmd_id]
        return {"ok": True, "message": "Command executed"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.delete("/api/commands/reject/{cmd_id}")
def reject_command(cmd_id: str, current_user: User = Depends(get_current_user)):
    cmd_id = ensure_safe_id(cmd_id, SAFE_COMMAND_ID_RE, "command id")
    if cmd_id in PENDING_COMMANDS and PENDING_COMMANDS[cmd_id]["user"] == current_user.storage_id:
        del PENDING_COMMANDS[cmd_id]
        return {"ok": True}
    raise HTTPException(status_code=404, detail="Command not found")

@app.get("/")
def root():
    return FileResponse("static/landing.html")


@app.get("/onboarding")
def onboarding_page():
    return FileResponse("static/onboarding.html")

@app.get("/chat-ui")
def chat_ui():
    return FileResponse("static/index.html")

@app.get("/dashboard")
def dashboard_page():
    return FileResponse("static/dashboard.html")


@app.get("/health/live")
def health_live():
    return {
        "ok": True,
        "status": "live",
        "instance_token": SERVER_INSTANCE_TOKEN,
        "pid": os.getpid(),
    }


@app.get("/health/ready")
def health_ready():
    storage = get_storage_health()
    return {
        "ok": True,
        "status": "ready",
        "database": storage["exists"],
        "database_backend": storage["backend"],
        "static_dir": os.path.isdir("static"),
    }

@app.post("/chat")
async def chat_endpoint(req: ChatRequest, current_user: User = Depends(get_current_user)):
    messages = [m.model_dump() for m in req.messages]
    is_premium = True # Forced by request
    try:
        # chat is now native async
        result = await asyncio.wait_for(
            chat(messages, current_user.storage_id, current_user.name, is_premium=is_premium),
            timeout=CHAT_REQUEST_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        logger.error(
            "Chat request timed out for %s after %.1fs",
            current_user.storage_id,
            CHAT_REQUEST_TIMEOUT_SECONDS,
        )
        return {
            "ok": False,
            "reply": "Alfred took too long to respond. Try again in a moment.",
            "pending_cmd_id": None,
        }
    except Exception as exc:
        logger.exception("Chat request failed for %s", current_user.storage_id)
        message = "Alfred could not process that request right now."
        error_text = str(exc)
        if "invalid_api_key" in error_text.lower() or "authenticationerror" in exc.__class__.__name__.lower():
            message = "Alfred's model API key is invalid. Update `GROQ_API_KEY` in `.env` and restart Alfred."
        elif "413" in error_text or "request_too_large" in error_text or (
            "tokens" in error_text.lower() and "limit" in error_text.lower()
        ):
            message = "The conversation is getting long — I trimmed the context but still hit the limit. Try starting a new chat."
        return {"ok": False, "reply": message, "pending_cmd_id": None}
    reply = result["reply"]
    cmd_id = None
    download_url = None
    download_name = None
    download_label = None

    # ── Presentation generation ─────────────────────────────────────────────
    pptx_topic = result.get("pending_pptx_topic")
    if pptx_topic:
        try:
            from groq import Groq as _Groq
            _pptx_client = _Groq(api_key=os.getenv("GROQ_API_KEY"))
            deck = _generate_presentation_payload(_pptx_client, pptx_topic, "chat-request", "")
            export_name = f"export_{_sanitize_presentation_filename(deck['title'])}_{uuid.uuid4().hex[:8]}.pptx"
            export_path = get_user_file(export_name, current_user.storage_id)
            with open(export_path, "wb") as _f:
                _f.write(_build_pptx_bytes(deck))
            download_url   = f"/downloads/{export_name}"
            download_name  = export_name
            download_label = "Download Presentation"
            # Improve the reply now that we know the deck is ready
            reply = deck.get("summary") or reply
        except Exception as _pptx_exc:
            logger.exception("Presentation generation failed for %s", current_user.storage_id)
            reply += f"\n\n(Could not generate the presentation: {_pptx_exc})"

    # ── Terminal command ────────────────────────────────────────────────────
    if result.get("pending_command"):
        if TERMINAL_COMMANDS_ENABLED and is_safe_terminal_command(result["pending_command"]):
            cmd_id = str(uuid.uuid4())[:8]
            PENDING_COMMANDS[cmd_id] = {
                "user": current_user.storage_id,
                "command": result["pending_command"],
                "timestamp": datetime.now().isoformat()
            }
        else:
            reply += "\n\n(Terminal commands are disabled or restricted in the website for safety.)"

    response: dict = {"reply": reply, "pending_cmd_id": cmd_id}
    if download_url:
        response["download_url"]   = download_url
        response["download_name"]  = download_name
        response["download_label"] = download_label
    return response

@app.get("/weather")
async def get_weather_endpoint(current_user: User = Depends(get_current_user)):
    from briefing import get_weather
    try:
        # get_weather handles memory-based city lookup
        results = get_weather(current_user.storage_id)
        # Parse it back to json if possible or just return a dict
        # The frontend expects {temp, feels_like, description, humidity, city}
        user_data = await get_account(current_user.email)
        location_json = user_data.get("location_data")
        city = "Bengaluru"
        if location_json:
            try:
                location = json.loads(location_json)
                city = location.get("name", "Bengaluru").split(",")[0].strip()
            except:
                pass
        
        # Fallback to memory city if exists
        if not location_json:
            from memory import load_memory_async
            memory = await load_memory_async(current_user.storage_id)
            city = memory.get("city", city)

        city = "".join(c for c in city if c.isalnum() or c.isspace())[:50]
        url = f"https://wttr.in/{city}?format=j1"
        async with httpx.AsyncClient() as client:
            res = await client.get(url, timeout=5)
            data = res.json()
            current = data["current_condition"][0]
            return {
                "temp": current.get("temp_C"),
                "feels_like": current.get("FeelsLikeC"),
                "description": current.get("weatherDesc", [{}])[0].get("value"),
                "humidity": current.get("humidity"),
                "city": city
            }
    except Exception:
        return {"error": "Could not fetch weather", "city": "Bengaluru"}

@app.get("/tasks")
async def get_tasks(current_user: User = Depends(get_current_user)):
    from tasks import load_tasks_async
    return await load_tasks_async(current_user.storage_id)

@app.post("/tasks")
async def add_task_endpoint(task: TaskCreateRequest, current_user: User = Depends(get_current_user)):
    from tasks import add_task_async
    await add_task_async(task.text, user_id=current_user.storage_id)
    return {"ok": True}

@app.put("/tasks/{task_id}")
async def toggle_task(task_id: str, current_user: User = Depends(get_current_user)):
    from tasks import load_tasks_async, save_tasks_async
    task_id = ensure_safe_id(task_id, SAFE_TASK_ID_RE, "task id")
    tasks = await load_tasks_async(current_user.storage_id)
    if not tasks:
        return {"ok": False}
    for t in tasks:
        if str(t["id"]) == task_id:
            t["done"] = not t["done"]
    await save_tasks_async(tasks, current_user.storage_id)
    return {"ok": True}

@app.delete("/tasks/{task_id}")
async def delete_task_endpoint(task_id: str, current_user: User = Depends(get_current_user)):
    from tasks import load_tasks_async, save_tasks_async
    task_id = ensure_safe_id(task_id, SAFE_TASK_ID_RE, "task id")
    tasks = await load_tasks_async(current_user.storage_id)
    if not tasks:
        return {"ok": False}
    tasks = [t for t in tasks if str(t["id"]) != task_id]
    await save_tasks_async(tasks, current_user.storage_id)
    return {"ok": True}

@app.get("/api/me/preferences")
async def get_my_preferences(current_user: User = Depends(get_current_user)):
    user_data = await get_account(current_user.email)
    if not user_data:
        raise HTTPException(status_code=404, detail="User not found")
    return {
        "widget_preferences": user_data.get("widget_preferences"),
        "location_data":      user_data.get("location_data"),
        "onboarding_complete": user_data.get("onboarding_complete", 0)
    }

@app.post("/api/me/preferences")
async def save_my_preferences(data: PreferencesUpdateRequest, current_user: User = Depends(get_current_user)):
    user_data = await get_account(current_user.email)
    if not user_data:
        raise HTTPException(status_code=404, detail="User not found")
    
    # We create a new dict to avoid modifying the one from storage in-place if it has fixed keys
    updated = dict(user_data)
    if data.widget_preferences is not None:
        updated["widget_preferences"] = data.widget_preferences
    if data.location_data is not None:
        updated["location_data"] = data.location_data
    if data.onboarding_complete is not None:
        updated["onboarding_complete"] = data.onboarding_complete
        
    await save_account(current_user.email, updated)
    return {"ok": True}

async def delete_task(task_id: str, current_user: User = Depends(get_current_user)):
    from tasks import load_tasks_async, save_tasks_async
    task_id = ensure_safe_id(task_id, SAFE_TASK_ID_RE, "task id")
    tasks = await load_tasks_async(current_user.storage_id)
    if not tasks:
        return {"ok": False}
    tasks = [t for t in tasks if str(t["id"]) != task_id]
    await save_tasks_async(tasks, current_user.storage_id)
    return {"ok": True}


@app.get("/contacts")
async def get_contacts(current_user: User = Depends(get_current_user)):
    return await load_contacts(current_user.storage_id)


@app.post("/contacts")
async def create_contact(data: ContactCreateRequest, current_user: User = Depends(get_current_user)):
    contact, created = await add_contact(
        current_user.storage_id,
        name=data.name,
        phone=data.phone or "",
        email=data.email or "",
        notes=data.notes or "",
        source="manual",
    )
    return {"ok": True, "created": created, "contact": contact}


@app.put("/contacts/{contact_id}")
async def edit_contact(contact_id: str, data: ContactUpdateRequest, current_user: User = Depends(get_current_user)):
    contact_id = ensure_safe_id(contact_id, SAFE_CONTACT_ID_RE, "contact id")
    payload = data.model_dump(exclude_unset=True)
    if not payload:
        raise HTTPException(status_code=400, detail="At least one contact field must be provided.")
    updated = await update_stored_contact(current_user.storage_id, contact_id, **payload)
    if not updated:
        raise HTTPException(status_code=404, detail="Contact not found.")
    return {"ok": True, "contact": updated}


@app.delete("/contacts/{contact_id}")
async def remove_contact(contact_id: str, current_user: User = Depends(get_current_user)):
    contact_id = ensure_safe_id(contact_id, SAFE_CONTACT_ID_RE, "contact id")
    if not await delete_stored_contact(current_user.storage_id, contact_id):
        raise HTTPException(status_code=404, detail="Contact not found.")
    return {"ok": True}


@app.post("/contacts/import/whatsapp")
async def import_contacts_from_whatsapp(current_user: User = Depends(get_current_user)):
    session_id = await get_whatsapp_session_id_async(current_user.storage_id)
    try:
        summary = await sync_whatsapp_contacts_from_bridge(current_user.storage_id, session_id)
        contacts = await load_contacts(current_user.storage_id)
        return {"ok": True, **summary, "contacts": contacts}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not import WhatsApp contacts: {exc}") from exc


@app.post("/contacts/clean")
async def clean_contacts(current_user: User = Depends(get_current_user)):
    """
    Remove contacts that were imported with @lid identifiers as phone numbers
    (a bug from before the @lid fix). Then re-sync from WhatsApp to get clean data.
    """
    from contacts import clean_lid_contacts  # noqa: PLC0415
    cleanup = clean_lid_contacts(current_user.storage_id)
    logger.info("Cleaned lid contacts for %s: %s", current_user.storage_id, cleanup)

    # Re-sync immediately if WhatsApp is connected
    try:
        session_id = await get_whatsapp_session_id_async(current_user.storage_id)
        summary = await sync_whatsapp_contacts_from_bridge(current_user.storage_id, session_id)
        contacts = await load_contacts(current_user.storage_id)
        return {"ok": True, "cleaned": cleanup, "sync": summary, "contacts": contacts}
    except Exception:
        contacts = await load_contacts(current_user.storage_id)
        return {"ok": True, "cleaned": cleanup, "sync": None, "contacts": contacts}


# ── Pomodoro ──────────────────────────────────────────────────────────────────

@app.get("/pomodoro")
async def get_pomodoro(current_user: User = Depends(get_current_user)):
    from pomodoro import get_pomodoro_state_async
    return await get_pomodoro_state_async(current_user.storage_id)

@app.post("/pomodoro")
async def post_pomodoro(update: PomodoroUpdateRequest, current_user: User = Depends(get_current_user)):
    from pomodoro import update_pomodoro_state_async
    payload = {k: v for k, v in update.model_dump().items() if v is not None}
    if not payload:
        raise HTTPException(status_code=400, detail="At least one pomodoro field must be provided.")
    return await update_pomodoro_state_async(current_user.storage_id, payload)

@app.get("/memory")
async def get_memory(current_user: User = Depends(get_current_user)):
    from memory import load_memory_async
    return await load_memory_async(current_user.storage_id)

@app.get("/stocks")
async def get_stocks_endpoint(current_user: User = Depends(get_current_user)):
    from memory import load_memory_async
    memory = await load_memory_async(current_user.storage_id)
    symbols = memory.get("stocks", {"Sensex": "^BSESN", "Nifty": "^NSEI", "Apple": "AAPL", "Nvidia": "NVDA"})
    results = []
    async with httpx.AsyncClient() as client:
        for name, sym in symbols.items():
            try:
                r = await client.get(
                    f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=5
                )
                data = r.json()
                price = data["chart"]["result"][0]["meta"]["regularMarketPrice"]
                prev = data["chart"]["result"][0]["meta"].get("previousClose", price)
                change = round((price - prev) / prev * 100, 2)
                results.append({"name": name, "price": round(price, 2), "change": change})
            except:
                results.append({"name": name, "price": None, "change": 0})
    return {"stocks": results}

@app.get("/notes")
async def get_notes_endpoint(current_user: User = Depends(get_current_user)):
    from notes import get_notes_async
    return await get_notes_async(current_user.storage_id)

@app.post("/notes")
async def add_note_endpoint(data: NoteCreateRequest, current_user: User = Depends(get_current_user)):
    from notes import save_note_async
    await save_note_async(data.content, current_user.storage_id)
    return {"ok": True}

@app.get("/habits")
async def get_habits(current_user: User = Depends(get_current_user)):
    from habits import load_habits_async
    return await load_habits_async(current_user.storage_id)

@app.get("/news")
def get_news_endpoint(current_user: User = Depends(get_current_user)):
    try:
        from tavily import TavilyClient
        import os
        client = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))
        response = client.search(
            query="entrepreneurship startups AI business India latest news",
            max_results=6,
            search_depth="basic",
            topic="news"
        )
        results = response.get("results", [])
        articles = [{"title": r["title"], "source": r.get("url", "").split("/")[2] if r.get("url") else "", "url": r.get("url", "")} for r in results]
        return {"articles": articles}
    except Exception as e:
        return {"articles": [], "error": str(e)}

@app.get("/spotify/now-playing")
def spotify_now_playing(current_user: User = Depends(get_current_user)):
    try:
        from spotify import get_current_playback_info
        return get_current_playback_info()
    except Exception as e:
        return {"playing": False, "error": str(e)}

@app.get("/calendar")
def get_calendar(current_user: User = Depends(get_current_user)):
    try:
        from calendar_helper import get_service
        from datetime import datetime
        service = get_service()
        now = datetime.utcnow()
        start = now.replace(hour=0, minute=0, second=0).isoformat() + "Z"
        end = now.replace(hour=23, minute=59, second=59).isoformat() + "Z"
        events_result = service.events().list(
            calendarId="primary", timeMin=start, timeMax=end,
            singleEvents=True, orderBy="startTime"
        ).execute()
        events = events_result.get("items", [])
        formatted = []
        for e in events:
            start_time = e["start"].get("dateTime", e["start"].get("date", ""))
            if "T" in start_time:
                time_str = start_time.split("T")[1][:5]
            else:
                time_str = "All day"
            formatted.append({"title": e["summary"], "time": time_str})
        return {"events": formatted}
    except Exception as e:
        return {"events": [], "error": str(e)}


@app.post("/whatsapp/send")
async def send_whatsapp(data: WhatsAppSendRequest, current_user: User = Depends(get_current_user)):
    session_id = await get_whatsapp_session_id_async(current_user.storage_id)
    await ensure_whatsapp_session_ready(session_id)

    async with httpx.AsyncClient() as client:
        res = await client.post(
            f"{WHATSAPP_BRIDGE_URL}/send",
            json={"number": data.number, "message": data.message, "sessionId": session_id},
            timeout=10
        )
    payload = res.json()
    if res.status_code >= 400:
        if res.status_code == 409:
            await ensure_whatsapp_session_ready(session_id, wait_seconds=4)
            async with httpx.AsyncClient() as client:
                retry = await client.post(
                    f"{WHATSAPP_BRIDGE_URL}/send",
                    json={"number": data.number, "message": data.message, "sessionId": session_id},
                    timeout=10,
                )
            payload = retry.json()
            if retry.status_code < 400:
                return payload
            raise HTTPException(
                status_code=retry.status_code,
                detail=_whatsapp_bridge_error_message(payload, "WhatsApp message could not be sent."),
            )
        raise HTTPException(
            status_code=res.status_code,
            detail=_whatsapp_bridge_error_message(payload, "WhatsApp message could not be sent."),
        )
    return payload

# ── File Upload & Analysis ────────────────────────────────────────────────────

IMAGE_EXTS  = {"png", "jpg", "jpeg", "gif", "webp"}
TEXT_EXTS   = {"txt", "md", "csv", "py", "js", "ts", "jsx", "tsx", "html", "css", "json", "yaml", "yml", "sh"}
DOCUMENT_EXTS = {"pdf", "docx", "pptx"}
MAX_IMAGE_BYTES = 3 * 1024 * 1024  # 3 MB safe limit for Groq vision
MAX_EXTRACTED_DOCUMENT_CHARS = 9000
MAX_PDF_PAGES = 12
MAX_PPTX_SLIDES = 20
MAX_GENERATED_SLIDES = 7
PRESENTATION_TARGET_RE = re.compile(r"\b(ppt|pptx|powerpoint|presentation|deck|slides?)\b", re.IGNORECASE)
PRESENTATION_ACTION_RE = re.compile(r"\b(make|create|generate|build|turn|convert|prepare|draft)\b", re.IGNORECASE)
EXPORT_FILENAME_RE = re.compile(r"^export_[a-z0-9_-]{1,80}\.pptx$")

def _compress_image(raw: bytes, ext: str) -> tuple[bytes, str]:
    """Resize + compress image so it fits within Groq's vision limit."""
    from PIL import Image
    import io
    fmt = "JPEG" if ext in ("jpg", "jpeg", "gif") else "PNG"
    mime = "jpeg" if fmt == "JPEG" else "png"
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    # Downscale if very large
    max_dim = 1568  # Groq recommends ≤1568px on longest side
    if max(img.size) > max_dim:
        img.thumbnail((max_dim, max_dim), Image.LANCZOS)
    buf = io.BytesIO()
    quality = 88
    while True:
        buf.seek(0); buf.truncate()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        if buf.tell() <= MAX_IMAGE_BYTES or quality <= 40:
            break
        quality -= 12
    return buf.getvalue(), "jpeg"


def _looks_like_presentation_request(prompt: str) -> bool:
    text = str(prompt or "")
    lowered = text.lower()
    transform_phrases = (
        "as a presentation",
        "as presentation",
        "as a deck",
        "into a presentation",
        "into presentation",
        "into slides",
        "in ppt",
        "in powerpoint",
    )
    return bool(
        PRESENTATION_TARGET_RE.search(text)
        and (PRESENTATION_ACTION_RE.search(text) or any(phrase in lowered for phrase in transform_phrases))
    )


def _sanitize_presentation_filename(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return slug[:48] or "deck"


def _extract_pdf_text(content: bytes) -> str:
    import io
    import pdfplumber

    pages: list[str] = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages[:MAX_PDF_PAGES]:
            text = (page.extract_text() or "").strip()
            if text:
                pages.append(text)
    return "\n\n".join(pages)[:MAX_EXTRACTED_DOCUMENT_CHARS]


def _extract_docx_text(content: bytes) -> str:
    import io
    import docx as _docx

    doc = _docx.Document(io.BytesIO(content))
    return "\n".join(p.text.strip() for p in doc.paragraphs if p.text.strip())[:MAX_EXTRACTED_DOCUMENT_CHARS]


def _pptx_slide_sort_key(path: str) -> int:
    match = re.search(r"slide(\d+)\.xml$", path)
    return int(match.group(1)) if match else 0


def _extract_pptx_text(content: bytes) -> str:
    slides: list[str] = []
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        slide_paths = sorted(
            (
                name
                for name in archive.namelist()
                if name.startswith("ppt/slides/slide") and name.endswith(".xml")
            ),
            key=_pptx_slide_sort_key,
        )
        for slide_path in slide_paths[:MAX_PPTX_SLIDES]:
            root = ET.fromstring(archive.read(slide_path))
            texts = [
                (node.text or "").strip()
                for node in root.iter()
                if node.tag.endswith("}t") and (node.text or "").strip()
            ]
            if texts:
                slides.append("\n".join(texts))
    return "\n\n".join(slides)[:MAX_EXTRACTED_DOCUMENT_CHARS]


def _extract_document_text(ext: str, content: bytes) -> Tuple[Optional[str], Optional[str]]:
    try:
        if ext == "pdf":
            text = _extract_pdf_text(content)
            if not text.strip():
                return None, "This PDF appears to be image-based or has no extractable text."
            return text, None
        if ext == "docx":
            text = _extract_docx_text(content)
            if not text.strip():
                return None, "This document does not appear to contain readable text."
            return text, None
        if ext == "pptx":
            text = _extract_pptx_text(content)
            if not text.strip():
                return None, "This PowerPoint does not appear to contain readable slide text."
            return text, None
        if ext in TEXT_EXTS:
            text = content.decode("utf-8", errors="ignore")[:MAX_EXTRACTED_DOCUMENT_CHARS]
            if not text.strip():
                return None, "This file appears to be empty."
            return text, None
    except Exception as exc:
        return None, str(exc)
    return None, f"Unsupported file type '.{ext}'."


def _extract_json_object(raw: str) -> Optional[dict]:
    if not raw:
        return None
    decoder = json.JSONDecoder()
    for start_index, char in enumerate(raw):
        if char != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(raw[start_index:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _trim_text(value: str, limit: int, fallback: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(value or "")).strip(" -•\n\t")
    if not cleaned:
        return fallback
    return cleaned[:limit].rstrip()


def _build_fallback_presentation(filename: str, extracted_text: str) -> dict:
    lines = [
        _trim_text(line, 120, "")
        for line in extracted_text.splitlines()
        if _trim_text(line, 120, "")
    ]
    paragraphs = [
        _trim_text(chunk, 160, "")
        for chunk in re.split(r"\n\s*\n", extracted_text)
        if _trim_text(chunk, 160, "")
    ]
    title = _trim_text(os.path.splitext(filename)[0].replace("_", " ").title(), 60, "Document Summary")
    summary_parts = lines[:3] or paragraphs[:2] or ["I created a concise deck from your uploaded document."]
    slides: list[dict[str, object]] = []
    if summary_parts:
        slides.append({
            "title": "Executive Summary",
            "bullets": [_trim_text(item, 120, "Key point") for item in summary_parts[:4]],
        })

    body_chunks = paragraphs[: MAX_GENERATED_SLIDES - 2] or ["The source material was converted into a compact presentation."]
    for index, chunk in enumerate(body_chunks, start=1):
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", chunk) if s.strip()]
        bullets = sentences[:4] or [chunk]
        slides.append({
            "title": f"Key Point {index}",
            "bullets": [_trim_text(item, 120, "Supporting detail") for item in bullets[:4]],
        })

    slides = slides[:MAX_GENERATED_SLIDES]
    return {
        "summary": f"I turned {filename} into a short presentation deck with {len(slides)} slides.",
        "title": title,
        "subtitle": "Generated by Alfred",
        "slides": slides,
    }


def _normalize_presentation_payload(payload: Optional[dict], filename: str, extracted_text: str) -> dict:
    fallback = _build_fallback_presentation(filename, extracted_text)
    if not payload:
        return fallback

    title = _trim_text(payload.get("title", ""), 60, fallback["title"])
    subtitle = _trim_text(payload.get("subtitle", ""), 80, fallback["subtitle"])
    summary = _trim_text(payload.get("summary", ""), 600, fallback["summary"])

    normalized_slides: list[dict[str, object]] = []
    for raw_slide in payload.get("slides", []):
        if not isinstance(raw_slide, dict):
            continue
        slide_title = _trim_text(raw_slide.get("title", ""), 60, "")
        bullets_raw = raw_slide.get("bullets", [])
        if not slide_title or not isinstance(bullets_raw, list):
            continue
        bullets = []
        for bullet in bullets_raw[:4]:
            cleaned = _trim_text(str(bullet), 120, "")
            if cleaned:
                bullets.append(cleaned)
        if bullets:
            normalized_slides.append({"title": slide_title, "bullets": bullets})
        if len(normalized_slides) >= MAX_GENERATED_SLIDES:
            break

    if not normalized_slides:
        return fallback

    return {
        "summary": summary,
        "title": title,
        "subtitle": subtitle,
        "slides": normalized_slides,
    }


def _generate_presentation_payload(client, prompt: str, filename: str, extracted_text: str) -> dict:
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {
                "role": "system",
                "content": (
                    "You create concise presentation decks. Return JSON only with keys: "
                    "summary, title, subtitle, slides. slides must be an array of 4-7 objects, "
                    "each with title and bullets. Keep each slide to at most 4 bullets and each "
                    "bullet under 18 words."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"User request: {prompt}\n\n"
                    f"Source file: {filename}\n\n"
                    f"Source text:\n{extracted_text}"
                ),
            },
        ],
        max_tokens=1200,
        temperature=0.4,
    )
    payload = _extract_json_object(response.choices[0].message.content or "")
    return _normalize_presentation_payload(payload, filename, extracted_text)


def _build_textbox_shape(shape_id: int, name: str, x: int, y: int, cx: int, cy: int, paragraphs: list[str], *, font_size: int, bold: bool = False) -> str:
    paragraph_xml = []
    for paragraph in paragraphs:
        bold_attr = ' b="1"' if bold else ""
        paragraph_xml.append(
            "<a:p>"
            "<a:r>"
            f"<a:rPr lang=\"en-US\" sz=\"{font_size}\"{bold_attr}/>"
            f"<a:t>{xml_escape(paragraph)}</a:t>"
            "</a:r>"
            "<a:endParaRPr lang=\"en-US\"/>"
            "</a:p>"
        )
    joined = "".join(paragraph_xml) or "<a:p><a:endParaRPr lang=\"en-US\"/></a:p>"
    return (
        "<p:sp>"
        "<p:nvSpPr>"
        f"<p:cNvPr id=\"{shape_id}\" name=\"{xml_escape(name)}\"/>"
        "<p:cNvSpPr txBox=\"1\"/>"
        "<p:nvPr/>"
        "</p:nvSpPr>"
        "<p:spPr>"
        "<a:xfrm>"
        f"<a:off x=\"{x}\" y=\"{y}\"/>"
        f"<a:ext cx=\"{cx}\" cy=\"{cy}\"/>"
        "</a:xfrm>"
        "<a:prstGeom prst=\"rect\"><a:avLst/></a:prstGeom>"
        "<a:noFill/>"
        "<a:ln><a:noFill/></a:ln>"
        "</p:spPr>"
        "<p:txBody>"
        "<a:bodyPr wrap=\"square\" rtlCol=\"0\">"
        "<a:spAutoFit/>"
        "</a:bodyPr>"
        "<a:lstStyle/>"
        f"{joined}"
        "</p:txBody>"
        "</p:sp>"
    )


def _build_slide_xml(title: str, bullets: List[str], *, subtitle: Optional[str] = None) -> str:
    title_box = _build_textbox_shape(2, "Title", 685800, 457200, 10820400, 762000, [title], font_size=2800, bold=True)
    body_paragraphs = [subtitle] if subtitle else [f"• {bullet}" for bullet in bullets]
    body_box = _build_textbox_shape(
        3,
        "Body",
        914400,
        1524000,
        10058400,
        4114800,
        body_paragraphs,
        font_size=1800 if subtitle else 2000,
    )
    return (
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
        "<p:sld xmlns:a=\"http://schemas.openxmlformats.org/drawingml/2006/main\" "
        "xmlns:r=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships\" "
        "xmlns:p=\"http://schemas.openxmlformats.org/presentationml/2006/main\">"
        "<p:cSld>"
        "<p:bg><p:bgRef idx=\"1001\"><a:schemeClr val=\"bg1\"/></p:bgRef></p:bg>"
        "<p:spTree>"
        "<p:nvGrpSpPr><p:cNvPr id=\"1\" name=\"\"/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>"
        "<p:grpSpPr>"
        "<a:xfrm><a:off x=\"0\" y=\"0\"/><a:ext cx=\"0\" cy=\"0\"/><a:chOff x=\"0\" y=\"0\"/><a:chExt cx=\"0\" cy=\"0\"/></a:xfrm>"
        "</p:grpSpPr>"
        f"{title_box}{body_box}"
        "</p:spTree>"
        "</p:cSld>"
        "<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>"
        "</p:sld>"
    )


def _ppt_theme_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Alfred Theme">
  <a:themeElements>
    <a:clrScheme name="Alfred">
      <a:dk1><a:srgbClr val="1F2937"/></a:dk1>
      <a:lt1><a:srgbClr val="FFFFFF"/></a:lt1>
      <a:dk2><a:srgbClr val="0F172A"/></a:dk2>
      <a:lt2><a:srgbClr val="F8FAFC"/></a:lt2>
      <a:accent1><a:srgbClr val="0F766E"/></a:accent1>
      <a:accent2><a:srgbClr val="1D4ED8"/></a:accent2>
      <a:accent3><a:srgbClr val="B45309"/></a:accent3>
      <a:accent4><a:srgbClr val="BE123C"/></a:accent4>
      <a:accent5><a:srgbClr val="7C3AED"/></a:accent5>
      <a:accent6><a:srgbClr val="059669"/></a:accent6>
      <a:hlink><a:srgbClr val="2563EB"/></a:hlink>
      <a:folHlink><a:srgbClr val="7C3AED"/></a:folHlink>
    </a:clrScheme>
    <a:fontScheme name="Alfred Fonts">
      <a:majorFont>
        <a:latin typeface="Aptos Display"/>
        <a:ea typeface=""/>
        <a:cs typeface=""/>
      </a:majorFont>
      <a:minorFont>
        <a:latin typeface="Aptos"/>
        <a:ea typeface=""/>
        <a:cs typeface=""/>
      </a:minorFont>
    </a:fontScheme>
    <a:fmtScheme name="Alfred Formats">
      <a:fillStyleLst>
        <a:solidFill><a:schemeClr val="lt1"/></a:solidFill>
      </a:fillStyleLst>
      <a:lnStyleLst>
        <a:ln w="9525" cap="flat" cmpd="sng" algn="ctr">
          <a:solidFill><a:schemeClr val="dk1"/></a:solidFill>
        </a:ln>
      </a:lnStyleLst>
      <a:effectStyleLst>
        <a:effectStyle><a:effectLst/></a:effectStyle>
      </a:effectStyleLst>
      <a:bgFillStyleLst>
        <a:solidFill><a:schemeClr val="lt1"/></a:solidFill>
      </a:bgFillStyleLst>
    </a:fmtScheme>
  </a:themeElements>
  <a:objectDefaults/>
  <a:extraClrSchemeLst/>
</a:theme>"""


def _presentation_package_xml(slide_count: int) -> tuple[str, str]:
    slide_ids = []
    slide_rels = [
        "<Relationship Id=\"rId1\" "
        "Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster\" "
        "Target=\"slideMasters/slideMaster1.xml\"/>"
    ]
    for index in range(1, slide_count + 1):
        rel_id = index + 1
        slide_ids.append(f"<p:sldId id=\"{255 + index}\" r:id=\"rId{rel_id}\"/>")
        slide_rels.append(
            f"<Relationship Id=\"rId{rel_id}\" "
            "Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide\" "
            f"Target=\"slides/slide{index}.xml\"/>"
        )
    presentation_xml = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
        "<p:presentation xmlns:a=\"http://schemas.openxmlformats.org/drawingml/2006/main\" "
        "xmlns:r=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships\" "
        "xmlns:p=\"http://schemas.openxmlformats.presentationml/2006/main\">"
        "<p:sldMasterIdLst><p:sldMasterId id=\"2147483648\" r:id=\"rId1\"/></p:sldMasterIdLst>"
        f"<p:sldIdLst>{''.join(slide_ids)}</p:sldIdLst>"
        "<p:sldSz cx=\"12192000\" cy=\"6858000\" type=\"screen16x9\"/>"
        "<p:notesSz cx=\"6858000\" cy=\"9144000\"/>"
        "<p:defaultTextStyle><a:defPPr/></p:defaultTextStyle>"
        "</p:presentation>"
    )
    presentation_rels_xml = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
        "<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">"
        f"{''.join(slide_rels)}"
        "</Relationships>"
    )
    return presentation_xml, presentation_rels_xml


def _build_pptx_bytes(deck: dict) -> bytes:
    created_at = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    slides: list[dict[str, object]] = deck["slides"]
    title = xml_escape(str(deck["title"]))
    subtitle = str(deck.get("subtitle", "Generated by Alfred"))
    presentation_xml, presentation_rels_xml = _presentation_package_xml(len(slides) + 1)
    part_titles = [str(deck["title"])] + [str(slide["title"]) for slide in slides]
    part_titles_xml = "".join(f"<vt:lpstr>{xml_escape(item)}</vt:lpstr>" for item in part_titles)

    content_types = [
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>",
        "<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\">",
        "<Default Extension=\"rels\" ContentType=\"application/vnd.openxmlformats-package.relationships+xml\"/>",
        "<Default Extension=\"xml\" ContentType=\"application/xml\"/>",
        "<Override PartName=\"/ppt/presentation.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml\"/>",
        "<Override PartName=\"/ppt/slideMasters/slideMaster1.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml\"/>",
        "<Override PartName=\"/ppt/slideLayouts/slideLayout1.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml\"/>",
        "<Override PartName=\"/ppt/theme/theme1.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.theme+xml\"/>",
        "<Override PartName=\"/docProps/core.xml\" ContentType=\"application/vnd.openxmlformats-package.core-properties+xml\"/>",
        "<Override PartName=\"/docProps/app.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.extended-properties+xml\"/>",
    ]
    for index in range(1, len(slides) + 2):
        content_types.append(
            f"<Override PartName=\"/ppt/slides/slide{index}.xml\" "
            "ContentType=\"application/vnd.openxmlformats-officedocument.presentationml.slide+xml\"/>"
        )
    content_types.append("</Types>")

    root_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"""

    core_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>{title}</dc:title>
  <dc:creator>Alfred</dc:creator>
  <cp:lastModifiedBy>Alfred</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">{created_at}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{created_at}</dcterms:modified>
</cp:coreProperties>"""

    app_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>Alfred</Application>
  <PresentationFormat>On-screen Show (16:9)</PresentationFormat>
  <Slides>{len(slides) + 1}</Slides>
  <Notes>0</Notes>
  <HiddenSlides>0</HiddenSlides>
  <MMClips>0</MMClips>
  <ScaleCrop>false</ScaleCrop>
  <HeadingPairs>
    <vt:vector size="2" baseType="variant">
      <vt:variant><vt:lpstr>Slides</vt:lpstr></vt:variant>
      <vt:variant><vt:i4>{len(part_titles)}</vt:i4></vt:variant>
    </vt:vector>
  </HeadingPairs>
  <TitlesOfParts>
    <vt:vector size="{len(part_titles)}" baseType="lpstr">
      {part_titles_xml}
    </vt:vector>
  </TitlesOfParts>
</Properties>"""

    slide_master_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldMaster xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld name="Master">
    <p:bg><p:bgRef idx="1001"><a:schemeClr val="bg1"/></p:bgRef></p:bg>
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
    </p:spTree>
  </p:cSld>
  <p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>
  <p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>
  <p:txStyles>
    <p:titleStyle><a:lvl1pPr algn="l"/></p:titleStyle>
    <p:bodyStyle><a:lvl1pPr algn="l"/></p:bodyStyle>
    <p:otherStyle><a:defPPr/></p:otherStyle>
  </p:txStyles>
</p:sldMaster>"""

    slide_master_rels_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/theme1.xml"/>
</Relationships>"""

    slide_layout_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldLayout xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" type="blank" preserve="1">
  <p:cSld name="Blank">
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
    </p:spTree>
  </p:cSld>
  <p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>
</p:sldLayout>"""

    slide_layout_rels_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="../slideMasters/slideMaster1.xml"/>
</Relationships>"""

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "".join(content_types))
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("docProps/core.xml", core_xml)
        archive.writestr("docProps/app.xml", app_xml)
        archive.writestr("ppt/presentation.xml", presentation_xml)
        archive.writestr("ppt/_rels/presentation.xml.rels", presentation_rels_xml)
        archive.writestr("ppt/slideMasters/slideMaster1.xml", slide_master_xml)
        archive.writestr("ppt/slideMasters/_rels/slideMaster1.xml.rels", slide_master_rels_xml)
        archive.writestr("ppt/slideLayouts/slideLayout1.xml", slide_layout_xml)
        archive.writestr("ppt/slideLayouts/_rels/slideLayout1.xml.rels", slide_layout_rels_xml)
        archive.writestr("ppt/theme/theme1.xml", _ppt_theme_xml())

        title_slide = _build_slide_xml(str(deck["title"]), [], subtitle=subtitle)
        archive.writestr("ppt/slides/slide1.xml", title_slide)
        archive.writestr(
            "ppt/slides/_rels/slide1.xml.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>
</Relationships>""",
        )

        for index, slide in enumerate(slides, start=2):
            archive.writestr(
                f"ppt/slides/slide{index}.xml",
                _build_slide_xml(str(slide["title"]), [str(b) for b in slide["bullets"]]),
            )
            archive.writestr(
                f"ppt/slides/_rels/slide{index}.xml.rels",
                """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>
</Relationships>""",
            )
    return output.getvalue()


def _create_presentation_export(client, prompt: str, filename: str, extracted_text: str, current_user: User) -> dict:
    deck = _generate_presentation_payload(client, prompt, filename, extracted_text)
    export_name = f"export_{_sanitize_presentation_filename(deck['title'])}_{uuid.uuid4().hex[:8]}.pptx"
    export_path = get_user_file(export_name, current_user.storage_id)
    with open(export_path, "wb") as export_file:
        export_file.write(_build_pptx_bytes(deck))
    return {
        "reply": deck["summary"],
        "type": "presentation",
        "filename": filename,
        "download_url": f"/downloads/{export_name}",
        "download_name": export_name,
        "download_label": "Download PPT",
    }


@app.get("/downloads/{filename}")
def download_user_export(filename: str, current_user: User = Depends(get_current_user)):
    safe_filename = os.path.basename(filename or "")
    if not EXPORT_FILENAME_RE.fullmatch(safe_filename):
        raise HTTPException(status_code=404, detail="File not found.")
    export_path = get_user_file(safe_filename, current_user.storage_id)
    if not os.path.exists(export_path):
        raise HTTPException(status_code=404, detail="File not found.")
    return FileResponse(
        export_path,
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        filename=safe_filename,
    )

@app.post("/upload")
async def upload_and_analyze(
    file: UploadFile = File(...),
    prompt: str = Form(default="Analyze this file and give me a concise, sharp summary."),
    current_user: User = Depends(get_current_user)
):
    from groq import Groq as _Groq
    _client = _Groq(api_key=os.getenv("GROQ_API_KEY"))
    try:
        prompt = sanitize_multiline_text(prompt, "prompt", max_length=MAX_UPLOAD_PROMPT_LENGTH)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    filename = os.path.basename(file.filename or "file")
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    content = await file.read()
    if len(content) > MAX_REQUEST_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Payload Too Large")

    # ── Images → compress → Groq Vision ──────────────────────────────────────
    if ext in IMAGE_EXTS:
        try:
            img_bytes, mime = _compress_image(content, ext)
            img_b64 = base64.b64encode(img_bytes).decode()
            resp = _client.chat.completions.create(
                model="llama-3.2-11b-vision-preview",
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/{mime};base64,{img_b64}"}},
                        {"type": "text",      "text": f"You are Alfred, a sharp personal assistant. {prompt}"}
                    ]
                }],
                max_tokens=700
            )
            reply = resp.choices[0].message.content or "I could see the image but had nothing to say about it."
            return {"reply": reply, "type": "image", "filename": filename}
        except Exception as e:
            return {"reply": f"Could not analyze image: {str(e)}", "type": "image", "filename": filename}

    # ── Documents, slides, and text files ────────────────────────────────────
    if ext in DOCUMENT_EXTS or ext in TEXT_EXTS:
        extracted_text, extraction_error = _extract_document_text(ext, content)
        if extraction_error:
            return {
                "reply": extraction_error if extraction_error.startswith("Unsupported") else f"Could not read {ext or 'file'}: {extraction_error}",
                "type": ext or "file",
                "filename": filename,
            }

        try:
            if _looks_like_presentation_request(prompt):
                return _create_presentation_export(_client, prompt, filename, extracted_text or "", current_user)

            resp = _client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": "You are Alfred, a sharp personal assistant. Be concise and insightful."},
                    {"role": "user", "content": f"{prompt}\n\nDocument ({filename}):\n{extracted_text}"}
                ],
                max_tokens=700
            )
            return {"reply": resp.choices[0].message.content or "No summary returned.", "type": ext or "text", "filename": filename}
        except Exception as e:
            return {"reply": f"Could not analyze file: {str(e)}", "type": ext or "text", "filename": filename}

    return {
        "reply": "Unsupported file type. Alfred can read images, PDFs, Word docs, PowerPoint files (.pptx), and text/code files. If you want a deck, attach a document and ask Alfred to make a presentation.",
        "type": "unsupported",
        "filename": filename,
    }


# ── Wake Shortcuts ─────────────────────────────────────────────────────────────

@app.get("/shortcuts")
async def get_shortcuts(current_user: User = Depends(get_current_user)):
    return await load_user_document(current_user.storage_id, "shortcuts.json", {"mode": "off", "threshold": 0.035})

@app.post("/shortcuts")
async def save_shortcuts(data: ShortcutSettingsRequest, current_user: User = Depends(get_current_user)):
    current = await load_user_document(current_user.storage_id, "shortcuts.json", {"mode": "off", "threshold": 0.035})

    payload = {
        "mode": data.mode or current.get("mode", "off"),
        "threshold": float(data.threshold if data.threshold is not None else current.get("threshold", 0.035)),
    }
    await save_user_document(current_user.storage_id, "shortcuts.json", payload)

    return {"ok": True, **payload}


# ---------------------------------------------------------------------------
# Premium status
# ---------------------------------------------------------------------------

@app.get("/api/premium/status")
async def get_premium_status(current_user: User = Depends(get_current_user)):
    is_premium = True
    return {"is_premium": is_premium}


class AdminPremiumRequest(StrictModel):
    email: str
    is_premium: bool
    admin_secret: str

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        return sanitize_email(value)


@app.post("/api/admin/premium")
async def admin_set_premium(data: AdminPremiumRequest):
    """Admin endpoint to grant/revoke premium. Requires ALFRED_ADMIN_SECRET."""
    if data.admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Invalid admin secret.")
    account = await get_account(data.email)
    if not account:
        raise HTTPException(status_code=404, detail="User not found.")
    await set_is_premium_async(data.email, data.is_premium)
    return {"ok": True, "email": data.email, "is_premium": data.is_premium}


# ---------------------------------------------------------------------------
# Cold Email — premium-gated
# ---------------------------------------------------------------------------

def _require_premium(current_user: User):
    """Sync helper; premium check happens in async endpoints before calling this."""
    pass





class ColdEmailConfigRequest(StrictModel):

    sender_name: str
    sender_role: str
    sender_email: str
    gmail_app_password: str
    company_name: str
    value_prop: str

    @field_validator("sender_email")
    @classmethod
    def validate_sender_email(cls, value: str) -> str:
        return sanitize_email(value)

    @field_validator("sender_name", "sender_role", "company_name")
    @classmethod
    def validate_text_fields(cls, value: str) -> str:
        return sanitize_single_line_text(value, "field", max_length=200)

    @field_validator("value_prop")
    @classmethod
    def validate_value_prop(cls, value: str) -> str:
        return sanitize_multiline_text(value, "value_prop", max_length=1000)

    @field_validator("gmail_app_password")
    @classmethod
    def validate_app_password(cls, value: str) -> str:
        if len(value) < 4 or len(value) > 128:
            raise ValueError("App password must be 4-128 characters.")
        return value


@app.get("/api/cold-email/config")
async def get_cold_email_config(current_user: User = Depends(get_current_user)):
    # Premium check removed

    cfg = cold_email_agent.get_config(current_user.storage_id)
    # Return redacted app password and has_password flag
    safe = {k: v for k, v in cfg.items() if k != "gmail_app_password"}
    safe["gmail_app_password"] = "" # Return empty string
    safe["has_password"] = bool(cfg.get("gmail_app_password"))
    return safe


@app.post("/api/cold-email/config")
async def save_cold_email_config(
    data: ColdEmailConfigRequest,
    current_user: User = Depends(get_current_user),
):
    # Premium check removed

    existing = cold_email_agent.get_config(current_user.storage_id)
    cfg = {
        "sender_name":        data.sender_name,
        "sender_role":        data.sender_role,
        "sender_email":       data.sender_email,
        "gmail_app_password": data.gmail_app_password or existing.get("gmail_app_password", ""),
        "company_name":       data.company_name,
        "value_prop":         data.value_prop,
    }
    cold_email_agent.save_config(current_user.storage_id, cfg)
    return {"ok": True}


@app.post("/api/cold-email/campaign")
async def run_cold_email_campaign(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    # Premium check removed

    if not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only CSV files are accepted.")

    contents = await file.read()
    if len(contents) > 512 * 1024:
        raise HTTPException(status_code=413, detail="CSV must be under 512 KB.")

    try:
        csv_text = contents.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="CSV must be UTF-8 encoded.")

    leads = cold_email_agent.parse_leads_csv(csv_text)
    if not leads:
        raise HTTPException(status_code=400, detail="No valid leads found in CSV (needs 'email' column).")

    groq_key = os.getenv("GROQ_API_KEY", "")

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        CHAT_EXECUTOR,
        lambda: cold_email_agent.run_campaign(current_user.storage_id, leads, groq_key),
    )
    return result


@app.get("/api/cold-email/stats")
async def get_cold_email_stats(current_user: User = Depends(get_current_user)):
    # Premium check removed

    return cold_email_agent.get_stats(current_user.storage_id)


@app.get("/api/cold-email/history")
async def get_cold_email_history(current_user: User = Depends(get_current_user)):
    # Premium check removed

    return cold_email_agent.get_history(current_user.storage_id)


@app.get("/api/cold-email/replies")
async def get_cold_email_replies(current_user: User = Depends(get_current_user)):
    # Premium check removed

    return cold_email_agent.get_replies(current_user.storage_id)


@app.post("/api/cold-email/check-replies")
async def check_cold_email_replies(current_user: User = Depends(get_current_user)):
    # Premium check removed

    loop = asyncio.get_event_loop()
    new_count = await loop.run_in_executor(
        CHAT_EXECUTOR,
        lambda: cold_email_agent.check_replies_once(current_user.storage_id),
    )
    return {"new_replies": new_count}


# ═══════════════════════════════════════════════════════════════
# SMART REPLY SUGGESTIONS
# POST /api/smart-reply
# Body: { "message": "incoming message text", "sender": "name" }
# Returns: { "replies": [{"tone": "Casual", "text": "..."}, ...] }
# ═══════════════════════════════════════════════════════════════
class SmartReplyRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    sender: Optional[str] = Field(default="", max_length=80)

@app.post("/api/smart-reply")
async def smart_reply(
    req: SmartReplyRequest,
    current_user: User = Depends(get_current_user)
):
    """Given an incoming message, return 3 context-aware reply drafts (casual, professional, brief)."""
    from groq import Groq as _Groq
    groq_client = _Groq(api_key=os.getenv("GROQ_API_KEY"))

    sender_label = req.sender.strip() if req.sender else "the sender"
    prompt = (
        f"An incoming message from {sender_label}:\n\"{req.message}\"\n\n"
        "Generate exactly 3 reply options with different tones. "
        "Output ONLY valid JSON in this exact shape, no extra text:\n"
        '[{"tone":"Casual","text":"..."},'
        '{"tone":"Professional","text":"..."},'
        '{"tone":"Brief","text":"..."}]'
    )

    def _call():
        return groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": "You are Alfred, a sharp personal assistant. Always return only the JSON array asked for, nothing else."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.75,
            max_tokens=400,
        )

    loop = asyncio.get_event_loop()
    resp = await loop.run_in_executor(CHAT_EXECUTOR, _call)
    raw = resp.choices[0].message.content.strip()

    # Robustly extract the JSON array even if the model wraps it in markdown
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if not match:
        raise HTTPException(status_code=500, detail="Could not generate reply suggestions.")
    try:
        replies = json.loads(match.group())
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Could not parse reply suggestions.")

    return {"replies": replies, "sender": sender_label}


# ═══════════════════════════════════════════════════════════════
# MEETING INTELLIGENCE — TRANSCRIPT → ACTION ITEMS
# POST /api/meeting/extract
# Body: { "transcript": "raw transcript text", "title": "optional" }
# Returns: { "action_items": [...], "decisions": [...], "summary": "..." }
# ═══════════════════════════════════════════════════════════════
class MeetingExtractRequest(BaseModel):
    transcript: str = Field(..., min_length=10, max_length=20000)
    title: Optional[str] = Field(default="", max_length=200)

@app.post("/api/meeting/extract")
async def extract_meeting(
    req: MeetingExtractRequest,
    current_user: User = Depends(get_current_user)
):
    """Extract structured action items, decisions, and a summary from a raw meeting transcript."""
    from groq import Groq as _Groq
    groq_client = _Groq(api_key=os.getenv("GROQ_API_KEY"))

    title_label = req.title.strip() if req.title else "Meeting"
    prompt = (
        f"Meeting: {title_label}\n\nTranscript:\n{req.transcript[:12000]}\n\n"
        "Extract the following and return ONLY valid JSON with no extra text:\n"
        '{"summary": "2-3 sentence overview",'
        '"action_items": [{"owner": "Name or You", "task": "what to do", "due": "when if mentioned"}],'
        '"decisions": ["decision 1", "decision 2"],'
        '"follow_ups": ["follow-up 1"]}'
    )

    def _call():
        return groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": "You are Alfred, a sharp meeting intelligence assistant. Return only the JSON object requested."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.4,
            max_tokens=1000,
        )

    loop = asyncio.get_event_loop()
    resp = await loop.run_in_executor(CHAT_EXECUTOR, _call)
    raw = resp.choices[0].message.content.strip()

    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise HTTPException(status_code=500, detail="Could not extract meeting data.")
    try:
        result = json.loads(match.group())
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Could not parse meeting extraction result.")

    result["title"] = title_label
    result["extracted_at"] = datetime.utcnow().isoformat()
    return result


# ═══════════════════════════════════════════════════════════════
# DAILY BRIEFING SCHEDULE MANAGEMENT
# POST /api/briefing/schedule  — set/update briefing time
# GET  /api/briefing/pending   — poll for any pending briefings
# ═══════════════════════════════════════════════════════════════
class BriefingScheduleRequest(BaseModel):
    hour: int = Field(default=8, ge=0, le=23)
    minute: int = Field(default=0, ge=0, le=59)

@app.post("/api/briefing/schedule")
async def set_briefing_schedule(
    req: BriefingScheduleRequest,
    current_user: User = Depends(get_current_user)
):
    """Set the daily morning briefing time for the authenticated user."""
    uid = current_user.storage_id
    name = current_user.display_name or uid

    # Persist the preference
    prefs = await load_user_document(uid, "preferences") or {}
    prefs["briefing_hour"] = req.hour
    prefs["briefing_minute"] = req.minute
    await save_user_document(uid, "preferences", prefs)

    # (Re-)schedule
    schedule_morning_briefing(uid, display_name=name, hour=req.hour, minute=req.minute)
    logger.info("User %s updated briefing schedule to %02d:%02d", uid, req.hour, req.minute)

    return {
        "ok": True,
        "message": f"Daily briefing scheduled at {req.hour:02d}:{req.minute:02d} every morning."
    }


@app.get("/api/briefing/pending")
async def get_briefing_pending(current_user: User = Depends(get_current_user)):
    """Poll for any pending morning briefing messages queued by the scheduler."""
    uid = current_user.storage_id
    updates = get_pending_updates(uid)
    return {"updates": updates}


@app.get("/api/suggest")
async def get_suggestions(current_user: User = Depends(get_current_user)):
    """Return 1-3 contextual 'what should I do now?' suggestions."""
    uid = current_user.storage_id
    from datetime import datetime
    from tasks import get_pending_tasks_async
    from memory_tracker import _load as _load_followups

    now = datetime.now()
    hour = now.hour
    suggestions = []

    # Time-of-day context
    if 5 <= hour < 9:
        time_ctx = "morning"
    elif 9 <= hour < 13:
        time_ctx = "mid-morning"
    elif 13 <= hour < 17:
        time_ctx = "afternoon"
    elif 17 <= hour < 21:
        time_ctx = "evening"
    else:
        time_ctx = "late night"

    # Pending tasks
    try:
        tasks_raw = await get_pending_tasks_async(uid)
        if tasks_raw and "No pending" not in tasks_raw:
            lines = [l.strip() for l in tasks_raw.split("\n") if l.strip()]
            oldest = lines[0] if lines else None
            count = len(lines)
            if oldest:
                task_text = oldest.split(". ", 1)[-1].split(" (added")[0]
                suggestions.append({
                    "text": f"You have {count} pending task{'s' if count > 1 else ''}. Oldest: '{task_text}'. Want me to help with that?",
                    "action": f"Help me with: {task_text}",
                    "icon": "✅"
                })
    except Exception:
        pass

    # Overdue follow-ups
    try:
        followups = await _load_followups(uid)
        from datetime import timedelta
        overdue = [
            f for f in followups
            if f.get("status") == "pending"
            and f.get("due_at")
            and datetime.fromisoformat(f["due_at"]) + timedelta(hours=f.get("followup_after_hours", 8)) < now
            and f.get("followup_count", 0) == 0
        ]
        if overdue:
            item = overdue[0]
            suggestions.append({
                "text": f"How did your {item['content']} go? Worth a quick check-in.",
                "action": f"Check in on: {item['content']}",
                "icon": "🔁"
            })
    except Exception:
        pass

    # Time-of-day default suggestions
    if len(suggestions) == 0:
        if time_ctx == "morning":
            suggestions.append({"text": "Good morning. Want a quick briefing to start your day?", "action": "Morning briefing", "icon": "☀️"})
        elif time_ctx in ("mid-morning", "afternoon"):
            suggestions.append({"text": "Midday check-in: anything you want to move forward on?", "action": "What should I focus on?", "icon": "🎯"})
        elif time_ctx == "evening":
            suggestions.append({"text": "Good evening. Want to wrap up and plan for tomorrow?", "action": "End of day review", "icon": "🌙"})
        else:
            suggestions.append({"text": "Working late? Let me know what you need.", "action": "What can I help with?", "icon": "💡"})
    elif len(suggestions) < 2 and time_ctx in ("morning", "mid-morning"):
        suggestions.append({"text": "Ready for a focused work session? I can set a Pomodoro timer.", "action": "Start a 25 minute focus session", "icon": "⏱"})

    return {"suggestions": suggestions[:3], "time_context": time_ctx}


@app.get("/api/inbox")
async def get_inbox(current_user: User = Depends(get_current_user)):
    """Return a unified inbox: recent emails + pending tasks + due follow-ups."""
    uid = current_user.storage_id
    inbox_items = []

    # Pending tasks (top 5)
    try:
        from tasks import load_tasks_async
        tasks = await load_tasks_async(uid)
        pending = [t for t in tasks if not t.get("done", False)][:5]
        for t in pending:
            inbox_items.append({
                "type": "task",
                "icon": "✅",
                "title": t.get("text", ""),
                "meta": f"Added {t.get('created', '')}",
                "action": f"Help me complete: {t.get('text', '')}",
            })
    except Exception:
        pass

    # Pending follow-ups
    try:
        from memory_tracker import _load as _load_followups
        from datetime import datetime, timedelta
        followups = await _load_followups(uid)
        now = datetime.now()
        due = [
            f for f in followups
            if f.get("status") == "pending"
            and f.get("due_at")
            and datetime.fromisoformat(f["due_at"]) < now
        ][:3]
        for f in due:
            inbox_items.append({
                "type": "followup",
                "icon": "🔁",
                "title": f"Follow up: {f.get('content', '')}",
                "meta": f"Due {f.get('due_at', '')[:10]}",
                "action": f"Check in on: {f.get('content', '')}",
            })
    except Exception:
        pass

    # Unread emails (top 3, subject only)
    try:
        from email_reader import get_unread_emails
        emails = get_unread_emails(user_id=uid)
        for e in (emails or [])[:3]:
            sender = e.get("from", "").split("<")[0].strip()
            inbox_items.append({
                "type": "email",
                "icon": "📧",
                "title": e.get("subject", "No subject"),
                "meta": f"From {sender}",
                "action": f"Read email from {sender} about: {e.get('subject', '')}",
            })
    except Exception:
        pass

    return {"items": inbox_items, "count": len(inbox_items)}