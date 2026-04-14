"""
WhatsApp connector for Alfred.

This module is the *single source of truth* for everything WhatsApp:
  - Bridge process lifecycle (start / health-check)
  - HTTP calls to the Node.js whatsapp-web.js bridge
  - Session readiness (sync + async)
  - Contact resolution and sync
  - Message sending (sync + async)
  - Per-user state persistence

Both alfred_core.py (sync, background threads) and main.py (async,
FastAPI routes) import from here instead of duplicating bridge logic.

Singleton usage
---------------
    from connectors.whatsapp import whatsapp

    # sync
    ok, err = whatsapp.send_message(user_id, session_id, "Alice", "hi!")

    # async
    status = await whatsapp.ensure_ready_async(session_id)
    result = await whatsapp.sync_contacts_async(user_id, session_id)
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from typing import Optional

import httpx
from fastapi import HTTPException

from connectors.base import BaseConnector

logger = logging.getLogger("alfred.connectors.whatsapp")

# ---------------------------------------------------------------------------
# Environment / config
# ---------------------------------------------------------------------------

BRIDGE_URL: str = os.getenv("WHATSAPP_BRIDGE_URL", "http://localhost:3000").rstrip("/")
BRIDGE_SCRIPT: str = os.path.join(os.path.dirname(__file__), "..", "whatsapp", "index.js")
BRIDGE_LOG_PATH: str = os.getenv(
    "ALFRED_WHATSAPP_BRIDGE_LOG",
    os.path.join(os.path.dirname(__file__), "..", "logs", "whatsapp-bridge.log"),
)
NODE_BIN: str = os.getenv("ALFRED_NODE_BIN", "node")
AUTOSTART: bool = os.getenv("ALFRED_AUTOSTART_WHATSAPP_BRIDGE", "true").lower() == "true"

READY_WAIT_SECONDS: int = int(os.getenv("ALFRED_WHATSAPP_READY_WAIT", "8"))
SEND_RETRIES: int = int(os.getenv("ALFRED_WHATSAPP_SEND_RETRIES", "2"))
CONTACT_SYNC_LIMIT: int = int(os.getenv("ALFRED_WHATSAPP_CONTACT_SYNC_LIMIT", "300"))


# ---------------------------------------------------------------------------
# WhatsApp connector
# ---------------------------------------------------------------------------

class WhatsAppConnector(BaseConnector):
    name = "whatsapp"

    def __init__(self) -> None:
        self._bridge_process: Optional[subprocess.Popen] = None

    # -----------------------------------------------------------------------
    # BaseConnector interface
    # -----------------------------------------------------------------------

    def get_status(self, user_id: str) -> dict:
        """Return persisted + live bridge status for *user_id*."""
        state = self.load_state(user_id)
        session_id = state.get("session_id") or user_id
        try:
            live = self._bridge_status(session_id)
        except Exception as exc:
            live = {"ready": False, "lastError": str(exc)}
        return {
            "connected": state.get("connected", False),
            "session_id": session_id,
            "last_error": state.get("last_error", ""),
            "bridge_ready": live.get("ready", False),
            "bridge_initializing": live.get("initializing", False),
            "bridge_qr": live.get("qr"),
            "bridge_last_error": live.get("lastError", ""),
        }

    # -----------------------------------------------------------------------
    # State helpers
    # -----------------------------------------------------------------------

    def get_session_id(self, user_id: str) -> str:
        state = self.load_state(user_id)
        return state.get("session_id") or user_id

    def update_state(
        self,
        user_id: str,
        *,
        connected: Optional[bool] = None,
        last_error: Optional[str] = None,
    ) -> None:
        state = self.load_state(user_id)
        state.setdefault("session_id", user_id)
        if connected is not None:
            state["connected"] = connected
        if last_error is not None:
            state["last_error"] = last_error
        self.save_state(user_id, state)

    # -----------------------------------------------------------------------
    # Bridge lifecycle
    # -----------------------------------------------------------------------

    def is_bridge_available(self) -> bool:
        """Quick ping — returns True if the bridge HTTP server responds."""
        try:
            with httpx.Client(timeout=2.0) as client:
                response = client.get(f"{BRIDGE_URL}/status")
            return response.status_code < 500
        except Exception:
            return False

    def ensure_bridge_running(self) -> bool:
        """
        Ensure the Node.js bridge process is up.
        If already running, returns True immediately.
        If AUTOSTART is True, launches the process and waits up to ~6 s.
        """
        if self.is_bridge_available():
            return True
        if not AUTOSTART:
            return False
        if not os.path.exists(BRIDGE_SCRIPT):
            logger.warning("WhatsApp bridge script not found at %s", BRIDGE_SCRIPT)
            return False
        if self._bridge_process and self._bridge_process.poll() is None:
            # Process is running but bridge not yet ready — wait a bit.
            return False

        popen_kwargs: dict = {"cwd": os.path.dirname(BRIDGE_SCRIPT)}
        bridge_log_handle = None
        try:
            os.makedirs(os.path.dirname(BRIDGE_LOG_PATH), exist_ok=True)
            bridge_log_handle = open(BRIDGE_LOG_PATH, "ab")
            popen_kwargs["stdout"] = bridge_log_handle
            popen_kwargs["stderr"] = bridge_log_handle
        except Exception:
            logger.exception("Could not open WhatsApp bridge log at %s", BRIDGE_LOG_PATH)
            popen_kwargs["stdout"] = subprocess.DEVNULL
            popen_kwargs["stderr"] = subprocess.DEVNULL

        try:
            self._bridge_process = subprocess.Popen(
                [NODE_BIN, os.path.basename(BRIDGE_SCRIPT)],
                **popen_kwargs,
            )
            logger.info("WhatsApp bridge process started (PID %s)", self._bridge_process.pid)
        except Exception:
            logger.exception("Failed to start WhatsApp bridge process")
            if bridge_log_handle:
                bridge_log_handle.close()
            return False

        for _ in range(15):
            time.sleep(0.4)
            if self.is_bridge_available():
                logger.info("WhatsApp bridge auto-started successfully")
                return True
            if self._bridge_process.poll() is not None:
                logger.warning(
                    "WhatsApp bridge exited with code %s. Check %s",
                    self._bridge_process.returncode,
                    BRIDGE_LOG_PATH,
                )
                return False

        logger.warning(
            "WhatsApp bridge did not become ready after auto-start. Check %s",
            BRIDGE_LOG_PATH,
        )
        return False

    def restore_sessions(self, accounts: dict) -> None:
        """
        Called at startup: re-kick any session that was `connected=True`
        before the server restarted.
        """
        for _email, account_data in accounts.items():
            storage_id = account_data.get("storage_id", "")
            if not storage_id:
                continue
            try:
                state = self.load_state(storage_id)
                if not state.get("connected"):
                    continue
                session_id = state.get("session_id") or storage_id
                with httpx.Client(timeout=10.0) as client:
                    client.post(
                        f"{BRIDGE_URL}/session/start",
                        json={"sessionId": session_id},
                    )
            except Exception:
                logger.exception("Failed to restore WhatsApp session for %s", storage_id)

    # -----------------------------------------------------------------------
    # Low-level bridge HTTP helpers (sync)
    # -----------------------------------------------------------------------

    @staticmethod
    def _parse_bridge_json(response: httpx.Response) -> dict:
        try:
            payload = response.json()
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _extract_bridge_error(response: httpx.Response, fallback: str) -> str:
        payload: dict = {}
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                payload = {}
        except Exception:
            pass
        for key in ("error", "lastError", "detail"):
            value = str(payload.get(key, "")).strip()
            if value:
                return value
        text = response.text.strip()
        return text or fallback

    @staticmethod
    def _extract_error_from_dict(payload: Optional[dict], fallback: str) -> str:
        if isinstance(payload, dict):
            for key in ("error", "lastError", "detail"):
                value = str(payload.get(key, "")).strip()
                if value:
                    return value
        return fallback

    def _bridge_request(
        self,
        method: str,
        path: str,
        *,
        json_payload: Optional[dict] = None,
        params: Optional[dict] = None,
        timeout: float = 10.0,
        retries: int = 0,
    ) -> httpx.Response:
        last_error: Optional[Exception] = None
        for attempt in range(retries + 1):
            try:
                response = httpx.request(
                    method,
                    f"{BRIDGE_URL}{path}",
                    json=json_payload,
                    params=params,
                    timeout=timeout,
                )
                if response.status_code < 500 or attempt == retries:
                    return response
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt == retries:
                    raise
            time.sleep(min(1.5, 0.4 * (attempt + 1)))
        if last_error:
            raise last_error
        raise RuntimeError("WhatsApp bridge request failed.")

    def _bridge_status(self, session_id: str) -> dict:
        response = self._bridge_request(
            "GET",
            "/session/status",
            params={"sessionId": session_id},
            timeout=8.0,
        )
        if response.status_code >= 400:
            raise RuntimeError(self._extract_bridge_error(response, "Could not check WhatsApp status."))
        payload = self._parse_bridge_json(response)
        payload.setdefault("ready", False)
        payload.setdefault("initializing", False)
        payload.setdefault("qr", None)
        payload.setdefault("lastError", "")
        return payload

    def _bridge_start_session(self, session_id: str) -> dict:
        response = self._bridge_request(
            "POST",
            "/session/start",
            json_payload={"sessionId": session_id},
            timeout=10.0,
            retries=1,
        )
        if response.status_code >= 400:
            raise RuntimeError(self._extract_bridge_error(response, "Could not start WhatsApp session."))
        payload = self._parse_bridge_json(response)
        payload.setdefault("ready", False)
        payload.setdefault("initializing", False)
        payload.setdefault("qr", None)
        payload.setdefault("lastError", "")
        return payload

    @staticmethod
    def _unavailable_message(status: dict) -> str:
        if status.get("qr"):
            return "WhatsApp needs a QR scan. Open Settings > Integrations and reconnect it."
        if status.get("initializing"):
            return "WhatsApp is still connecting. Try again in a few seconds."
        last_error = str(status.get("lastError", "")).strip()
        if last_error:
            return "WhatsApp is unavailable right now: " + last_error
        return "WhatsApp is not connected yet. Open Settings > Integrations and connect it first."

    # -----------------------------------------------------------------------
    # Session readiness (sync — used by alfred_core / background workers)
    # -----------------------------------------------------------------------

    def ensure_ready(
        self,
        session_id: str,
        *,
        wait_seconds: int = READY_WAIT_SECONDS,
    ) -> tuple[bool, Optional[str], dict]:
        """
        Ensure the WhatsApp session is ready to send.

        Returns (ready, error_message, status_dict).
        If ready=True, error_message is None.
        """
        try:
            status = self._bridge_status(session_id)
        except Exception as exc:
            return False, "WhatsApp bridge is unavailable: " + str(exc), {}

        if status.get("ready"):
            return True, None, status

        if not status.get("initializing") and not status.get("qr"):
            try:
                status = self._bridge_start_session(session_id)
            except Exception as exc:
                return False, "Could not start WhatsApp: " + str(exc), status

        if status.get("ready"):
            return True, None, status

        deadline = time.time() + max(0, wait_seconds)
        while time.time() < deadline and status.get("initializing") and not status.get("qr"):
            time.sleep(1)
            try:
                status = self._bridge_status(session_id)
            except Exception as exc:
                return False, "WhatsApp bridge is unavailable: " + str(exc), {}
            if status.get("ready"):
                return True, None, status

        return False, self._unavailable_message(status), status

    # -----------------------------------------------------------------------
    # Bridge HTTP helpers (async — used by FastAPI route handlers)
    # -----------------------------------------------------------------------

    async def _bridge_status_async(self, session_id: str) -> dict:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{BRIDGE_URL}/session/status",
                params={"sessionId": session_id},
                timeout=10,
            )
        payload = self._parse_bridge_json(response)
        payload.setdefault("ready", False)
        payload.setdefault("initializing", False)
        payload.setdefault("qr", None)
        payload.setdefault("lastError", "")
        return payload

    async def ensure_ready_async(
        self,
        session_id: str,
        *,
        wait_seconds: int = READY_WAIT_SECONDS,
    ) -> dict:
        """
        Async version of ensure_ready — raises HTTPException on failure.
        Use this inside FastAPI route handlers.
        """
        self.ensure_bridge_running()

        try:
            status = await self._bridge_status_async(session_id)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"WhatsApp bridge unavailable: {exc}") from exc

        if not status.get("ready"):
            if not status.get("initializing") and not status.get("qr"):
                async with httpx.AsyncClient() as client:
                    start_response = await client.post(
                        f"{BRIDGE_URL}/session/start",
                        json={"sessionId": session_id},
                        timeout=10,
                    )
                start_payload = self._parse_bridge_json(start_response)
                if start_response.status_code >= 400:
                    raise HTTPException(
                        status_code=502,
                        detail=self._extract_error_from_dict(
                            start_payload, "Could not start WhatsApp session."
                        ),
                    )
                status = start_payload
                status.setdefault("ready", False)
                status.setdefault("initializing", False)
                status.setdefault("qr", None)
                status.setdefault("lastError", "")

        if status.get("ready"):
            return status

        deadline = time.time() + max(0, wait_seconds)
        while time.time() < deadline and status.get("initializing") and not status.get("qr"):
            await asyncio.sleep(1)
            status = await self._bridge_status_async(session_id)
            if status.get("ready"):
                return status

        raise HTTPException(
            status_code=409,
            detail=self._extract_error_from_dict(status, "WhatsApp is not connected yet."),
        )

    # -----------------------------------------------------------------------
    # Contact resolution
    # -----------------------------------------------------------------------

    @staticmethod
    def _select_contact_match(
        matches: list[dict],
        query: str,
    ) -> tuple[Optional[dict], Optional[str]]:
        from contacts import extract_phone_from_chat_id, normalize_phone_number  # noqa: PLC0415

        lowered_query = str(query or "").strip().lower()
        normalized_query_phone = normalize_phone_number(query)
        ranked: list[tuple[int, dict]] = []

        for contact in matches:
            name = str(contact.get("name", "")).strip().lower()
            phone = normalize_phone_number(
                extract_phone_from_chat_id(contact.get("whatsapp_chat_id")) or contact.get("phone", "")
            )
            score = 0
            if normalized_query_phone and phone == normalized_query_phone:
                score = 100
            elif name == lowered_query:
                score = 95
            elif normalized_query_phone and phone.startswith(normalized_query_phone):
                score = 85
            elif name.startswith(lowered_query):
                score = 75
            elif lowered_query and lowered_query in name:
                score = 65
            if score:
                ranked.append((score, contact))

        if not ranked:
            return None, None
        ranked.sort(key=lambda item: item[0], reverse=True)
        if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
            return None, f'Multiple WhatsApp contacts match "{query}". Use the full name or number.'
        return ranked[0][1], None

    def resolve_target(
        self,
        user_id: str,
        raw_target: str,
    ) -> tuple[Optional[str], Optional[str]]:
        """
        Resolve a human-readable recipient (name, phone, or chat ID) to a
        WhatsApp-sendable identifier.

        Returns (target, error).  If target is not None, error is None.
        """
        from contacts import (  # noqa: PLC0415
            extract_phone_from_chat_id,
            find_contacts_by_query,
            normalize_phone_number,
        )

        target = str(raw_target or "").strip()
        if not target:
            return None, "WhatsApp recipient is missing."
        if "@" in target:
            return target, None

        numeric_target = normalize_phone_number(target)
        if len(numeric_target) >= 8:
            return numeric_target, None

        matches = [
            contact
            for contact in find_contacts_by_query(user_id, target)
            if extract_phone_from_chat_id(contact.get("whatsapp_chat_id")) or contact.get("phone")
        ]
        if matches:
            contact, ambiguity_error = self._select_contact_match(matches, target)
            if ambiguity_error:
                return None, ambiguity_error
            if contact:
                return (
                    str(contact.get("whatsapp_chat_id") or "").strip() or contact.get("phone"),
                    None,
                )
        return None, f'No saved WhatsApp contact matches "{target}".'

    # -----------------------------------------------------------------------
    # Contact sync
    # -----------------------------------------------------------------------

    def refresh_contacts(
        self,
        user_id: str,
        session_id: str,
    ) -> tuple[bool, Optional[str]]:
        """Sync contacts from the bridge (sync version)."""
        from contacts import import_recent_whatsapp_contacts  # noqa: PLC0415

        ready, error_message, _ = self.ensure_ready(session_id, wait_seconds=4)
        if not ready:
            return False, error_message

        response = self._bridge_request(
            "GET",
            "/contacts",
            params={"limit": CONTACT_SYNC_LIMIT, "sessionId": session_id},
            timeout=12.0,
            retries=1,
        )
        if response.status_code >= 400:
            return False, self._extract_bridge_error(response, "Could not load WhatsApp contacts.")

        contacts = response.json()
        if not isinstance(contacts, list):
            return False, "Unexpected WhatsApp contacts response."

        import_recent_whatsapp_contacts(user_id, contacts)
        return True, None

    async def sync_contacts_async(
        self,
        user_id: str,
        session_id: str,
    ) -> dict[str, int]:
        """Sync contacts from the bridge (async version). Returns import stats."""
        from contacts import import_recent_whatsapp_contacts  # noqa: PLC0415

        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{BRIDGE_URL}/contacts",
                params={"limit": CONTACT_SYNC_LIMIT, "sessionId": session_id},
                timeout=12,
            )

        if response.status_code >= 400:
            raise HTTPException(
                status_code=502,
                detail=self._extract_bridge_error(response, "Could not load WhatsApp contacts."),
            )

        chats = response.json()
        if not isinstance(chats, list):
            raise HTTPException(status_code=502, detail="Unexpected WhatsApp contacts response.")

        return import_recent_whatsapp_contacts(user_id, chats)

    # -----------------------------------------------------------------------
    # Send message (sync — used by alfred_core)
    # -----------------------------------------------------------------------

    def send_message(
        self,
        user_id: str,
        session_id: str,
        recipient_hint: str,
        body: str,
    ) -> tuple[bool, str]:
        """
        Send a WhatsApp message.

        Returns (success, message_or_error).
        """
        message_body = str(body or "").strip()
        if not message_body:
            return False, "WhatsApp message is empty."

        target, target_error = self.resolve_target(user_id, recipient_hint)
        if not target:
            refreshed, refresh_error = self.refresh_contacts(user_id, session_id)
            if refreshed:
                target, target_error = self.resolve_target(user_id, recipient_hint)
            elif refresh_error:
                target_error = target_error or refresh_error

        if not target:
            return False, target_error or "WhatsApp recipient could not be resolved."

        ready, ready_error, _ = self.ensure_ready(session_id)
        if not ready:
            return False, ready_error or "WhatsApp is not connected yet."

        last_error = "WhatsApp message could not be sent."
        for attempt in range(SEND_RETRIES):
            response = self._bridge_request(
                "POST",
                "/send",
                json_payload={"number": target, "message": message_body, "sessionId": session_id},
                timeout=12.0,
            )
            if response.status_code < 400:
                return True, f"Message sent to {recipient_hint}."

            last_error = self._extract_bridge_error(response, last_error)
            if response.status_code == 409 and attempt + 1 < SEND_RETRIES:
                ready, ready_error, _ = self.ensure_ready(session_id, wait_seconds=4)
                if not ready:
                    return False, ready_error or last_error
                continue
            break

        return False, last_error


# ---------------------------------------------------------------------------
# Module-level singleton — import this everywhere
# ---------------------------------------------------------------------------

whatsapp = WhatsAppConnector()
