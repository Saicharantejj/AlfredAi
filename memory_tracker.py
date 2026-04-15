"""
memory_tracker.py — Alfred's Persistent Memory & Follow-up Engine

Responsibilities:
  - Extract event/task mentions from user messages (regex + keywords, no LLM)
  - Parse natural time expressions into real ISO timestamps
  - Store items per-user in followups.json (via existing storage layer)
  - Deduplicate against existing items
  - Update item status when user confirms completion/change
  - Generate natural, human-sounding follow-up messages
  - Expose priority scoring so the scheduler can pick the best item to follow up on
"""

import re
import uuid
import random
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

from storage import load_user_document_async, save_user_document_async

logger = logging.getLogger("alfred.memory_tracker")

# ── Storage helpers ──────────────────────────────────────────────────────────

FOLLOWUPS_DOC = "followups.json"


async def _load(user_id: str) -> List[Dict[str, Any]]:
    result = await load_user_document_async(user_id, FOLLOWUPS_DOC, [])
    if isinstance(result, dict):
        return []
    return result if isinstance(result, list) else []



async def _save(user_id: str, items: List[Dict[str, Any]]) -> None:
    await save_user_document_async(user_id, FOLLOWUPS_DOC, items)


# ── Time parsing ─────────────────────────────────────────────────────────────

_TIME_RE = re.compile(
    r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.IGNORECASE
)
_DATE_PATTERNS = [
    # "14 april" / "april 14"
    (re.compile(r"\b(\d{1,2})\s+(january|february|march|april|may|june|july|august|september|october|november|december)\b", re.IGNORECASE), "day_month"),
    (re.compile(r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2})\b", re.IGNORECASE), "month_day"),
]
_MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}
_WEEKDAY_MAP = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def _extract_time_of_day(text: str) -> Optional[tuple]:
    """Return (hour24, minute) or None."""
    m = _TIME_RE.search(text)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        period = (m.group(3) or "").lower()
        if period == "pm" and hour != 12:
            hour += 12
        elif period == "am" and hour == 12:
            hour = 0
        return hour, minute
    # "tonight" → 9pm, "this morning" → 9am, "this evening" → 7pm
    lower = text.lower()
    if "tonight" in lower or "this evening" in lower:
        return 21, 0
    if "this morning" in lower:
        return 9, 0
    if "this afternoon" in lower:
        return 14, 0
    return None


def parse_time_reference(text: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """Convert a natural language time reference into a real datetime."""
    if now is None:
        now = datetime.now()
    lower = text.lower()
    tod = _extract_time_of_day(text)
    hour, minute = tod if tod else (12, 0)  # default noon

    # Relative keywords
    if "tonight" in lower:
        base = now.replace(hour=21, minute=0, second=0, microsecond=0)
        if base <= now:
            base += timedelta(days=1)
        return base
    if "tomorrow" in lower:
        base = (now + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        return base
    if "next week" in lower:
        base = (now + timedelta(weeks=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        return base
    if "this week" in lower:
        base = (now + timedelta(days=3)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        return base
    if "in an hour" in lower or "in 1 hour" in lower:
        return now + timedelta(hours=1)
    if "in two hours" in lower or "in 2 hours" in lower:
        return now + timedelta(hours=2)
    if "later today" in lower or "later" in lower:
        return now + timedelta(hours=4)

    # "next monday", "this friday" etc.
    for day_name, day_num in _WEEKDAY_MAP.items():
        if day_name in lower:
            days_ahead = (day_num - now.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7  # "monday" when it is already monday → next monday
            base = (now + timedelta(days=days_ahead)).replace(hour=hour, minute=minute, second=0, microsecond=0)
            return base

    # Absolute dates: "14 april", "april 14"
    for pattern, kind in _DATE_PATTERNS:
        m = pattern.search(lower)
        if m:
            try:
                if kind == "day_month":
                    day, month_name = int(m.group(1)), m.group(2)
                else:
                    month_name, day = m.group(1), int(m.group(2))
                month = _MONTH_MAP.get(month_name.lower())
                if month:
                    year = now.year
                    candidate = datetime(year, month, day, hour, minute)
                    if candidate < now:
                        candidate = datetime(year + 1, month, day, hour, minute)
                    return candidate
            except ValueError:
                pass

    return None


# ── Keyword detection ─────────────────────────────────────────────────────────

# Phrases that strongly indicate a future event
_EVENT_TRIGGERS = [
    r"\b(have|got|there'?s|there is|scheduled|attending|going to|joining)\b.{0,60}\b(meeting|call|interview|presentation|event|appointment|session|webinar|demo|standup|sync|standup|dinner|lunch|flight|exam|test)\b",
    r"\b(meeting|call|interview|presentation|appointment|session|demo|dinner|lunch|flight|exam|test)\b.{0,40}\b(tomorrow|tonight|next|on monday|on tuesday|on wednesday|on thursday|on friday|at \d)",
]

# Phrases that indicate a task
_TASK_TRIGGERS = [
    r"\b(need to|have to|supposed to|must|should|gotta|going to|want to|planning to)\b.{0,80}\b(send|finish|complete|submit|review|write|prepare|update|call|reply|respond|fix|build|deploy|check|follow|reach)\b",
    r"\b(remind me|don'?t let me forget|make sure i|todo|to do|to-do)\b.{0,80}",
]

_TIME_ANCHORS = r"\b(tomorrow|tonight|next week|next monday|next tuesday|next wednesday|next thursday|next friday|this friday|this week|today|later today|in an hour|in two hours|at \d{1,2}(?::\d{2})?\s*(?:am|pm)|\d{1,2}\s+(?:january|february|march|april|may|june|july|august|september|october|november|december))\b"


def _has_time_anchor(text: str) -> bool:
    return bool(re.search(_TIME_ANCHORS, text, re.IGNORECASE))


def _extract_short_content(text: str, item_type: str) -> str:
    """Pull a short human-readable description out of the raw message."""
    lower = text.lower().strip()

    # Strip common preamble
    lower = re.sub(r"^(hey alfred[,.]?\s*|alfred[,.]?\s*)*", "", lower)

    # For events: extract the noun phrase around the event keyword
    if item_type == "event":
        m = re.search(
            r"(meeting|call|interview|presentation|appointment|session|demo|dinner|lunch|flight|exam|test)\b[^.!?]*",
            lower, re.IGNORECASE
        )
        if m:
            snippet = m.group(0).strip()
            # trim at time anchors to keep it short
            snippet = re.sub(r"\s*(at|on|tomorrow|tonight|next|this)\b.*$", "", snippet, flags=re.IGNORECASE)
            return snippet.strip()[:80]

    # For tasks: extract the verb phrase
    if item_type == "task":
        m = re.search(
            r"(?:need to|have to|supposed to|must|should|gotta|going to|want to|planning to|remind me to?|todo:?|to do:?)\s+(.{5,80}?)(?:\s+(?:by|before|at|on|tomorrow|tonight|next)|[.!?]|$)",
            lower, re.IGNORECASE
        )
        if m:
            return m.group(1).strip()[:80]

    # Fallback: first 70 chars
    return text.strip()[:70]


# ── Follow-up delay config ────────────────────────────────────────────────────

_FOLLOWUP_HOURS = {
    "event": (6, 24),
    "task": (2, 6),
}


def _followup_after_hours(item_type: str) -> float:
    lo, hi = _FOLLOWUP_HOURS.get(item_type, (4, 12))
    return random.uniform(lo, hi)


# ── Natural follow-up messages ────────────────────────────────────────────────

_FOLLOWUP_TEMPLATES = {
    "event": [
        "Hey, how did {content} go?",
        "Just checking in — how was {content}?",
        "How'd things go with {content}?",
        "Hope {content} went well! Any updates?",
        "How did {content} turn out?",
    ],
    "task": [
        "Did you get a chance to finish {content}?",
        "Any update on {content}?",
        "Just a gentle nudge — {content} was on your list. All done?",
        "How's {content} coming along?",
        "Quick check-in: did {content} get sorted?",
        "Still working on {content}, or all done?",
    ],
}

_SECOND_FOLLOWUP_TEMPLATES = {
    "event": [
        "Hey, I know things get busy — just wanted to circle back on {content}. How'd it go?",
        "Last check-in on {content} — hope it all went smoothly!",
    ],
    "task": [
        "Hey, no pressure — just following up on {content} one more time. Let me know if you need anything.",
        "Last nudge on {content} — you've got this!",
    ],
}


def generate_followup_message(item: Dict[str, Any]) -> str:
    content = item.get("content", "that")
    item_type = item.get("type", "task")
    count = item.get("followup_count", 0)
    pool = _SECOND_FOLLOWUP_TEMPLATES.get(item_type, []) if count >= 1 else _FOLLOWUP_TEMPLATES.get(item_type, [])
    template = random.choice(pool) if pool else "Just checking in on {content}."
    return template.format(content=content)


# ── Deduplication ─────────────────────────────────────────────────────────────

def _is_duplicate(candidate_content: str, existing: List[Dict[str, Any]]) -> bool:
    """Fuzzy match: if >60% of words overlap with an existing pending item, skip."""
    c_words = set(re.findall(r"\w+", candidate_content.lower()))
    if not c_words:
        return False
    for item in existing:
        if item.get("status") not in ("pending",):
            continue
        e_words = set(re.findall(r"\w+", item.get("content", "").lower()))
        if not e_words:
            continue
        overlap = len(c_words & e_words) / max(len(c_words), len(e_words))
        if overlap >= 0.6:
            return True
    return False


# ── Priority scoring ──────────────────────────────────────────────────────────

def priority_score(item: Dict[str, Any], now: Optional[datetime] = None) -> float:
    """Higher score = follow up sooner. Combines urgency, recency, and follow-up count."""
    if now is None:
        now = datetime.now()
    due_str = item.get("due_at")
    try:
        due = datetime.fromisoformat(due_str) if due_str else now
    except ValueError:
        due = now

    hours_overdue = max(0.0, (now - due).total_seconds() / 3600)
    recency_bonus = 1.0 / max(1.0, hours_overdue)  # more recent = slightly higher priority
    followup_penalty = item.get("followup_count", 0) * 0.3
    type_weight = 1.2 if item.get("type") == "event" else 1.0
    return (hours_overdue * type_weight * recency_bonus) - followup_penalty


# ── Status update from user reply ─────────────────────────────────────────────

_COMPLETION_PHRASES = re.compile(
    r"\b(done|finished|completed|all good|went well|it went|it was|sorted|handled|submitted|sent it|did it|yes|yep|yup|great|awesome|nailed it|crushed it|went great|went fine)\b",
    re.IGNORECASE,
)
_NEGATIVE_PHRASES = re.compile(
    r"\b(didn'?t|couldn'?t|not yet|haven'?t|still|no|nope|pushed|rescheduled|postponed|cancelled|canceled|maybe later|forgot|couldn't make it)\b",
    re.IGNORECASE,
)
_CHANGE_PHRASES = re.compile(
    r"\b(rescheduled|postponed|moved|changed|pushed to|new time|later date)\b",
    re.IGNORECASE,
)


async def update_status_from_reply_async(user_message: str, user_id: str) -> None:
    """Mark items complete/updated based on user's reply."""
    items = await _load(user_id)
    if not items:
        return

    pending = [i for i in items if i.get("status") == "pending"]
    if not pending:
        return

    changed = False
    for item in pending:
        # Only act on items that have been followed up on (Alfred asked about them)
        if item.get("followup_count", 0) == 0:
            continue

        if _COMPLETION_PHRASES.search(user_message):
            item["status"] = "completed"
            item["resolved_at"] = datetime.now().isoformat()
            changed = True
            logger.info("Marked item '%s' as completed for user %s", item.get("content"), user_id)

        elif _CHANGE_PHRASES.search(user_message):
            # Try to extract a new due time from the message
            new_due = parse_time_reference(user_message)
            if new_due:
                item["due_at"] = new_due.isoformat()
                item["followup_count"] = 0  # reset so we follow up again
                item["last_followup_at"] = None
                changed = True
                logger.info("Updated due time for '%s' for user %s", item.get("content"), user_id)

        elif _NEGATIVE_PHRASES.search(user_message):
            # Not done yet — bump followup window slightly
            item["followup_after_hours"] = min(item.get("followup_after_hours", 6) + 2, 24)
            changed = True

    if changed:
        await _save(user_id, items)


# ── Main extraction entry point ───────────────────────────────────────────────

async def extract_and_store_async(user_message: str, user_id: str) -> Optional[str]:
    """
    Parse the user's message for events/tasks with time references and store them.
    Returns a short confirmation string if something was stored, else None.
    Called silently from alfred_core.chat() — Alfred only mentions it if desired.
    """
    if not user_message or len(user_message.strip()) < 8:
        return None

    detected_type: Optional[str] = None

    # Check for event patterns first (higher confidence)
    for pattern in _EVENT_TRIGGERS:
        if re.search(pattern, user_message, re.IGNORECASE):
            detected_type = "event"
            break

    # Check for task patterns
    if not detected_type:
        for pattern in _TASK_TRIGGERS:
            if re.search(pattern, user_message, re.IGNORECASE):
                detected_type = "task"
                break

    if not detected_type:
        return None

    # For events, we still require a time anchor to be worth tracking.
    # For tasks, we allow it to be stored without a time if the intent is clear.
    if detected_type == "event" and not _has_time_anchor(user_message):
        return None

    content = _extract_short_content(user_message, detected_type)
    if not content or len(content) < 4:
        return None

    due_at = parse_time_reference(user_message)
    if not due_at:
        return None

    now = datetime.now()
    # Ignore things that are already in the past
    if due_at < now:
        return None

    items = await _load(user_id)

    # Deduplication check
    if _is_duplicate(content, items):
        logger.debug("Duplicate follow-up item skipped: '%s'", content)
        return None

    delay_hours = _followup_after_hours(detected_type)
    new_item: Dict[str, Any] = {
        "id": uuid.uuid4().hex[:12],
        "type": detected_type,
        "content": content,
        "due_at": due_at.isoformat(),
        "created_at": now.isoformat(),
        "status": "pending",
        "followup_count": 0,
        "last_followup_at": None,
        "followup_after_hours": delay_hours,
    }
    items.append(new_item)
    await _save(user_id, items)

    # ── Dashboard Sync ──
    # If it's a task, add it to the main dashboard list (tasks.json) as well
    if detected_type == "task":
        try:
            from tasks import add_task_async
            await add_task_async(content, user_id)
        except Exception as te:
            logger.error("Failed to sync auto-extracted task to tasks.json: %s", te)

    confirmation = (
        f"Got it, I'll check in on your {content} after it's due."
        if detected_type == "event"
        else f"Noted — I'll follow up on '{content}' later."
    )
    logger.info("Stored %s follow-up: '%s' due %s for user %s", detected_type, content, due_at.isoformat(), user_id)
    return confirmation


# ── Scheduler-facing helpers ──────────────────────────────────────────────────

async def get_due_followups_async(user_id: str) -> List[Dict[str, Any]]:
    """
    Return items that are now due for a follow-up (sorted by priority, max 2).
    Does NOT mutate storage — caller must call mark_followup_sent_async after.
    """
    items = await _load(user_id)
    now = datetime.now()
    eligible = []
    for item in items:
        if item.get("status") != "pending":
            continue
        if item.get("followup_count", 0) >= 2:
            # Max follow-ups reached — mark as ignored
            item["status"] = "ignored"
            continue
        due_str = item.get("due_at")
        if not due_str:
            continue
        try:
            due = datetime.fromisoformat(due_str)
        except ValueError:
            continue
        followup_trigger = due + timedelta(hours=item.get("followup_after_hours", 8))
        if now >= followup_trigger:
            # Respect minimum gap between follow-ups (at least 4 hours)
            last_str = item.get("last_followup_at")
            if last_str:
                try:
                    last = datetime.fromisoformat(last_str)
                    if (now - last).total_seconds() < 4 * 3600:
                        continue
                except ValueError:
                    pass
            eligible.append(item)

    if not eligible:
        # Save any "ignored" status changes
        await _save(user_id, items)
        return []

    eligible.sort(key=lambda i: priority_score(i, now), reverse=True)
    await _save(user_id, items)   # persist ignored status changes
    return eligible[:2]           # max 2 per cycle


async def mark_followup_sent_async(user_id: str, item_id: str) -> None:
    """Record that a follow-up was sent for a given item."""
    items = await _load(user_id)
    for item in items:
        if item.get("id") == item_id:
            item["followup_count"] = item.get("followup_count", 0) + 1
            item["last_followup_at"] = datetime.now().isoformat()
            if item["followup_count"] >= 2:
                item["status"] = "ignored"
            break
    await _save(user_id, items)
