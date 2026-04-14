from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Union, Optional, List, Dict, Tuple
from uuid import uuid4

from storage import (
    load_user_document, 
    save_user_document,
    load_user_document_async,
    save_user_document_async
)

CONTACTS_DOCUMENT = "contacts.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_phone_number(value: Optional[str]) -> str:
    if not value:
        return ""
    return "".join(ch for ch in str(value).strip() if ch.isdigit())


def extract_phone_from_chat_id(chat_id: Optional[str]) -> str:
    if not chat_id:
        return ""
    base = str(chat_id).split("@", 1)[0]
    return normalize_phone_number(base)


def normalize_email_address(value: Optional[str]) -> str:
    return str(value or "").strip().lower()


def _build_contact(
    *,
    contact_id: Optional[str] = None,
    name: str,
    phone: str = "",
    email: str = "",
    notes: str = "",
    source: str = "manual",
    whatsapp_chat_id: str = "",
    created_at: Optional[str] = None,
    updated_at: Optional[str] = None,
    last_message_at: int = 0,
) -> dict:
    timestamp = updated_at or _now_iso()
    return {
        "id": contact_id or uuid4().hex[:12],
        "name": str(name or "").strip(),
        "phone": normalize_phone_number(phone),
        "email": normalize_email_address(email),
        "notes": str(notes or "").strip(),
        "source": str(source or "manual").strip() or "manual",
        "whatsapp_chat_id": str(whatsapp_chat_id or "").strip(),
        "created_at": created_at or timestamp,
        "updated_at": timestamp,
        # Unix timestamp (seconds) of the last WhatsApp message with this contact.
        # 0 means never messaged. Used to sort contacts by most-frequently-spoken-to.
        "last_message_at": int(last_message_at or 0),
    }


def _coerce_contacts(raw_contacts: Any) -> list[dict[str, str]]:
    contacts: list[dict[str, str]] = []
    if isinstance(raw_contacts, dict):
        for name, phone in raw_contacts.items():
            if not str(name or "").strip():
                continue
            contacts.append(_build_contact(name=str(name), phone=str(phone or ""), source="legacy"))
        return contacts

    if not isinstance(raw_contacts, list):
        return []

    for item in raw_contacts:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        contacts.append(
            _build_contact(
                contact_id=str(item.get("id") or "") or None,
                name=name,
                phone=str(item.get("phone", item.get("number", "")) or ""),
                email=str(item.get("email", "") or ""),
                notes=str(item.get("notes", "") or ""),
                source=str(item.get("source", "manual") or ""),
                whatsapp_chat_id=str(item.get("whatsapp_chat_id", item.get("chatId", "")) or ""),
                created_at=str(item.get("created_at", "") or "") or None,
                updated_at=str(item.get("updated_at", "") or "") or None,
                last_message_at=int(item.get("last_message_at", 0) or 0),
            )
        )
    return contacts


def _contact_matches(existing: dict[str, str], incoming: dict[str, str]) -> bool:
    existing_phone = normalize_phone_number(existing.get("phone"))
    incoming_phone = normalize_phone_number(incoming.get("phone"))
    if existing_phone and incoming_phone and existing_phone == incoming_phone:
        return True

    existing_email = normalize_email_address(existing.get("email"))
    incoming_email = normalize_email_address(incoming.get("email"))
    if existing_email and incoming_email and existing_email == incoming_email:
        return True

    existing_chat_id = str(existing.get("whatsapp_chat_id", "")).strip()
    incoming_chat_id = str(incoming.get("whatsapp_chat_id", "")).strip()
    if existing_chat_id and incoming_chat_id and existing_chat_id == incoming_chat_id:
        return True

    if not incoming_phone and not incoming_email and not incoming_chat_id:
        return existing.get("name", "").strip().lower() == incoming.get("name", "").strip().lower()

    return False


def _merge_contact(existing: dict, incoming: dict, *, overwrite: bool = False) -> dict:
    merged = dict(existing)
    merged["name"] = incoming["name"] if overwrite or not merged.get("name") else merged["name"]
    for key in ("phone", "email", "notes", "whatsapp_chat_id"):
        incoming_value = incoming.get(key, "")
        if overwrite:
            merged[key] = incoming_value
        elif incoming_value and not merged.get(key):
            merged[key] = incoming_value
    incoming_source = incoming.get("source", "")
    if overwrite or not merged.get("source"):
        merged["source"] = incoming_source or merged.get("source", "manual")
    elif incoming_source == "manual":
        merged["source"] = "manual"
    # Always keep the most recent last_message_at
    merged["last_message_at"] = max(
        int(merged.get("last_message_at") or 0),
        int(incoming.get("last_message_at") or 0),
    )
    merged["updated_at"] = _now_iso()
    return merged


def _dedupe_contacts(contacts: list[dict[str, str]]) -> list[dict[str, str]]:
    deduped: list[dict[str, str]] = []
    for contact in contacts:
        for index, existing in enumerate(deduped):
            if _contact_matches(existing, contact):
                deduped[index] = _merge_contact(existing, contact)
                break
        else:
            deduped.append(contact)
    # Primary sort: most recently messaged on WhatsApp (descending).
    # Secondary: alphabetical by name for contacts with equal/no activity.
    deduped.sort(key=lambda c: (
        -(int(c.get("last_message_at") or 0)),
        (c.get("name") or "").lower(),
    ))
    return deduped


def load_contacts(user_id: str) -> list[dict[str, str]]:
    raw_contacts = load_user_document(user_id, CONTACTS_DOCUMENT, [])
    contacts = _dedupe_contacts(_coerce_contacts(raw_contacts))
    if contacts != raw_contacts:
        save_user_document(user_id, CONTACTS_DOCUMENT, contacts)
    return contacts


async def load_contacts_async(user_id: str) -> list[dict[str, str]]:
    raw_contacts = await load_user_document_async(user_id, CONTACTS_DOCUMENT, [])
    contacts = _dedupe_contacts(_coerce_contacts(raw_contacts))
    # Note: We skip the auto-save if diff since we are in a read path, or we can await it
    return contacts


def save_contacts(user_id: str, contacts: list[dict[str, str]]) -> list[dict[str, str]]:
    normalized = _dedupe_contacts(_coerce_contacts(contacts))
    save_user_document(user_id, CONTACTS_DOCUMENT, normalized)
    return normalized


async def save_contacts_async(user_id: str, contacts: list[dict[str, str]]) -> list[dict[str, str]]:
    normalized = _dedupe_contacts(_coerce_contacts(contacts))
    await save_user_document_async(user_id, CONTACTS_DOCUMENT, normalized)
    return normalized


def add_contact(
    user_id: str,
    *,
    name: str,
    phone: str = "",
    email: str = "",
    notes: str = "",
    source: str = "manual",
    whatsapp_chat_id: str = "",
) -> tuple[dict[str, str], bool]:
    contacts = load_contacts(user_id)
    incoming = _build_contact(
        name=name,
        phone=phone,
        email=email,
        notes=notes,
        source=source,
        whatsapp_chat_id=whatsapp_chat_id,
    )
    for index, existing in enumerate(contacts):
        if _contact_matches(existing, incoming):
            contacts[index] = _merge_contact(existing, incoming)
            save_contacts(user_id, contacts)
            return contacts[index], False
    contacts.append(incoming)
    save_contacts(user_id, contacts)
    return incoming, True


async def add_contact_async(
    user_id: str,
    *,
    name: str,
    phone: str = "",
    email: str = "",
    notes: str = "",
    source: str = "manual",
    whatsapp_chat_id: str = "",
) -> tuple[dict[str, str], bool]:
    contacts = await load_contacts_async(user_id)
    incoming = _build_contact(
        name=name,
        phone=phone,
        email=email,
        notes=notes,
        source=source,
        whatsapp_chat_id=whatsapp_chat_id,
    )
    for index, existing in enumerate(contacts):
        if _contact_matches(existing, incoming):
            contacts[index] = _merge_contact(existing, incoming)
            await save_contacts_async(user_id, contacts)
            return contacts[index], False
    contacts.append(incoming)
    await save_contacts_async(user_id, contacts)
    return incoming, True


def update_contact(
    user_id: str,
    contact_id: str,
    *,
    name: Optional[str] = None,
    phone: Optional[str] = None,
    email: Optional[str] = None,
    notes: Optional[str] = None,
) -> Optional[Dict[str, str]]:
    contacts = load_contacts(user_id)
    for index, existing in enumerate(contacts):
        if existing.get("id") != contact_id:
            continue
        updated = dict(existing)
        if name is not None:
            updated["name"] = str(name).strip()
        if phone is not None:
            updated["phone"] = normalize_phone_number(phone)
        if email is not None:
            updated["email"] = normalize_email_address(email)
        if notes is not None:
            updated["notes"] = str(notes).strip()
        updated["updated_at"] = _now_iso()
        contacts[index] = updated
        save_contacts(user_id, contacts)
        return updated
    return None


async def update_contact_async(
    user_id: str,
    contact_id: str,
    *,
    name: Optional[str] = None,
    phone: Optional[str] = None,
    email: Optional[str] = None,
    notes: Optional[str] = None,
) -> Optional[Dict[str, str]]:
    contacts = await load_contacts_async(user_id)
    for index, existing in enumerate(contacts):
        if existing.get("id") != contact_id:
            continue
        updated = dict(existing)
        if name is not None:
            updated["name"] = str(name).strip()
        if phone is not None:
            updated["phone"] = normalize_phone_number(phone)
        if email is not None:
            updated["email"] = normalize_email_address(email)
        if notes is not None:
            updated["notes"] = str(notes).strip()
        updated["updated_at"] = _now_iso()
        contacts[index] = updated
        await save_contacts_async(user_id, contacts)
        return updated
    return None


def delete_contact(user_id: str, contact_id: str) -> bool:
    contacts = load_contacts(user_id)
    filtered = [contact for contact in contacts if contact.get("id") != contact_id]
    if len(filtered) == len(contacts):
        return False
    save_contacts(user_id, filtered)
    return True


async def delete_contact_async(user_id: str, contact_id: str) -> bool:
    contacts = await load_contacts_async(user_id)
    filtered = [contact for contact in contacts if contact.get("id") != contact_id]
    if len(filtered) == len(contacts):
        return False
    await save_contacts_async(user_id, filtered)
    return True


def get_contact_directory(user_id: str) -> list[dict[str, str]]:
    contacts = load_contacts(user_id)
    return [
        {
            "name": contact.get("name", ""),
            "phone": contact.get("phone", ""),
            "email": contact.get("email", ""),
            "notes": contact.get("notes", ""),
        }
        for contact in contacts
    ]


async def get_contact_directory_async(user_id: str) -> list[dict[str, str]]:
    contacts = await load_contacts_async(user_id)
    return [
        {
            "name": contact.get("name", ""),
            "phone": contact.get("phone", ""),
            "email": contact.get("email", ""),
            "notes": contact.get("notes", ""),
        }
        for contact in contacts
    ]


def import_recent_whatsapp_contacts(user_id: str, chats: list[dict[str, Any]]) -> dict[str, int]:
    contacts = load_contacts(user_id)
    imported = 0
    updated = 0
    for chat in chats:
        chat_id = str(chat.get("chatId", chat.get("id", "")) or "").strip()

        # Skip @lid identifiers — WhatsApp internal linked-device IDs, not real numbers.
        if chat_id.endswith("@lid"):
            continue

        # Only use contact.number from the bridge; never extract from @lid chat IDs.
        raw_phone = normalize_phone_number(chat.get("phone", chat.get("number", "")))
        if not raw_phone and chat_id.endswith("@c.us"):
            raw_phone = extract_phone_from_chat_id(chat_id)
        phone = raw_phone

        name = str(
            chat.get("name")
            or chat.get("pushname")
            or chat.get("from")
            or phone
            or ""
        ).strip()
        if not name and not phone:
            continue

        # The bridge now sends lastMessageTimestamp (Unix seconds) for each contact.
        last_message_at = int(chat.get("lastMessageTimestamp", 0) or 0)

        incoming = _build_contact(
            name=name,
            phone=phone,
            source="whatsapp",
            whatsapp_chat_id=chat_id,
            last_message_at=last_message_at,
        )
        for index, existing in enumerate(contacts):
            if _contact_matches(existing, incoming):
                contacts[index] = _merge_contact(existing, incoming)
                updated += 1
                break
        else:
            contacts.append(incoming)
            imported += 1

    save_contacts(user_id, contacts)
    return {"imported": imported, "updated": updated}


async def import_recent_whatsapp_contacts_async(user_id: str, chats: list[dict[str, Any]]) -> dict[str, int]:
    contacts = await load_contacts_async(user_id)
    imported = 0
    updated = 0
    for chat in chats:
        chat_id = str(chat.get("chatId", chat.get("id", "")) or "").strip()
        if chat_id.endswith("@lid"):
            continue
        raw_phone = normalize_phone_number(chat.get("phone", chat.get("number", "")))
        if not raw_phone and chat_id.endswith("@c.us"):
            raw_phone = extract_phone_from_chat_id(chat_id)
        phone = raw_phone
        name = str(chat.get("name") or chat.get("pushname") or chat.get("from") or phone or "").strip()
        if not name and not phone:
            continue
        last_message_at = int(chat.get("lastMessageTimestamp", 0) or 0)
        incoming = _build_contact(
            name=name, phone=phone, source="whatsapp",
            whatsapp_chat_id=chat_id, last_message_at=last_message_at,
        )
        for index, existing in enumerate(contacts):
            if _contact_matches(existing, incoming):
                contacts[index] = _merge_contact(existing, incoming)
                updated += 1
                break
        else:
            contacts.append(incoming)
            imported += 1

    await save_contacts_async(user_id, contacts)
    return {"imported": imported, "updated": updated}


def _is_lid_number(phone: str) -> bool:
    """
    Returns True if the phone number looks like a WhatsApp @lid identifier
    that was mistakenly stored as a phone number.
    @lid numbers are typically 15+ digits and start with unusual prefixes.
    Real phone numbers are 7-15 digits (E.164 standard).
    """
    digits = normalize_phone_number(phone)
    return len(digits) > 15


def clean_lid_contacts(user_id: str) -> dict[str, int]:
    """
    Remove contacts that were imported with @lid identifiers as phone numbers
    (a bug from before the @lid fix). A contact is considered corrupt if:
      - its whatsapp_chat_id ends with @lid, OR
      - its phone number is >15 digits (lid numbers are very long)
    AND it has no manually-entered email (i.e. source is whatsapp, not manual).

    Returns {"removed": N, "kept": M}.
    """
    contacts = load_contacts(user_id)
    clean = []
    removed = 0
    for contact in contacts:
        chat_id = contact.get("whatsapp_chat_id", "")
        phone = contact.get("phone", "")
        source = contact.get("source", "")
        is_corrupt = (
            chat_id.endswith("@lid")
            or (source == "whatsapp" and _is_lid_number(phone))
        )
        if is_corrupt:
            removed += 1
        else:
            clean.append(contact)

    if removed:
        save_contacts(user_id, clean)

    return {"removed": removed, "kept": len(clean)}


def find_contacts_by_query(user_id: str, query: str) -> list[dict[str, str]]:
    needle = str(query or "").strip().lower()
    if not needle:
        return []
    matches: list[dict[str, str]] = []
    normalized_query_phone = normalize_phone_number(needle)
    for contact in load_contacts(user_id):
        name = contact.get("name", "").lower()
        phone = normalize_phone_number(contact.get("phone"))
        email = contact.get("email", "").lower()
        if needle in name or (normalized_query_phone and normalized_query_phone in phone) or needle in email:
            matches.append(contact)
    return matches


async def find_contacts_by_query_async(user_id: str, query: str) -> list[dict[str, str]]:
    needle = str(query or "").strip().lower()
    if not needle:
        return []
    matches: list[dict[str, str]] = []
    normalized_query_phone = normalize_phone_number(needle)
    contacts = await load_contacts_async(user_id)
    for contact in contacts:
        name = contact.get("name", "").lower()
        phone = normalize_phone_number(contact.get("phone"))
        email = contact.get("email", "").lower()
        if needle in name or (normalized_query_phone and normalized_query_phone in phone) or needle in email:
            matches.append(contact)
    return matches
