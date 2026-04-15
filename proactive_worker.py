"""
proactive_worker.py — Alfred Proactive Engagement Engine

Replaces the primitive proactive.py (speak-only, no intelligence) with a
pluggable, async-aware background worker. Each check is a self-contained class
that decides whether to fire based on live user context.

Architecture:
  - ProactiveCheck (ABC): base class for all checks.
  - ProactiveWorker: registers checks, runs them on a shared APScheduler.
  - Push mechanism: appends to scheduler.pending_updates (existing polling list).
  - Deduplication: each check stores its last-fired timestamp in user storage
    so it doesn't fire twice within its cooldown window.

Checks included:
  1. MorningBriefingCheck   — 8 AM, fires once per day.
  2. PreMeetingCheck        — Every 15 min; alerts if a calendar event is ≤30 min away.
  3. StaleTaskCheck         — Every 60 min; nudges if a HIGH priority task is >24 h old.
  4. EmailBacklogCheck      — Every 60 min; alerts if unread email count exceeds threshold.
  5. InactivityNudgeCheck   — Every 60 min; asks "anything I can help with?" after
                               4+ hours of silence during business hours (9–20).

To add a new check:
    worker = get_proactive_worker()
    worker.register(MyCustomCheck())
"""

from __future__ import annotations

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger("alfred.proactive_worker")

# ─────────────────────────────────────────────────────────────────────────────
# Configuration (all tunable via environment variables)
# ─────────────────────────────────────────────────────────────────────────────

_MORNING_BRIEFING_HOUR = int(os.getenv("ALFRED_BRIEFING_HOUR", "8"))
_MORNING_BRIEFING_MINUTE = int(os.getenv("ALFRED_BRIEFING_MINUTE", "0"))
_PRE_MEETING_WARN_MINUTES = int(os.getenv("ALFRED_PRE_MEETING_WARN_MIN", "30"))
_STALE_TASK_HOURS = int(os.getenv("ALFRED_STALE_TASK_HOURS", "24"))
_EMAIL_BACKLOG_THRESHOLD = int(os.getenv("ALFRED_EMAIL_BACKLOG_COUNT", "5"))
_INACTIVITY_HOURS = int(os.getenv("ALFRED_INACTIVITY_HOURS", "4"))
_BUSINESS_HOURS_START = int(os.getenv("ALFRED_BIZ_HOURS_START", "9"))
_BUSINESS_HOURS_END = int(os.getenv("ALFRED_BIZ_HOURS_END", "20"))


# ─────────────────────────────────────────────────────────────────────────────
# Deduplication helpers (stored per-user in proactive_state.json)
# ─────────────────────────────────────────────────────────────────────────────

_STATE_DOC = "proactive_state.json"


async def _load_state(user_id: str) -> Dict[str, Any]:
    from storage import load_user_document_async
    return await load_user_document_async(user_id, _STATE_DOC, {})


async def _save_state(user_id: str, state: Dict[str, Any]) -> None:
    from storage import save_user_document_async
    await save_user_document_async(user_id, _STATE_DOC, state)


async def _get_last_fired(user_id: str, check_id: str) -> Optional[datetime]:
    state = await _load_state(user_id)
    ts = state.get(f"last_fired_{check_id}")
    if ts:
        try:
            return datetime.fromisoformat(ts)
        except ValueError:
            pass
    return None


async def _mark_fired(user_id: str, check_id: str) -> None:
    state = await _load_state(user_id)
    state[f"last_fired_{check_id}"] = datetime.now().isoformat()
    await _save_state(user_id, state)


async def _record_last_activity(user_id: str) -> None:
    """Called from alfred_core.chat() to record the last time the user spoke."""
    state = await _load_state(user_id)
    state["last_activity_at"] = datetime.now().isoformat()
    await _save_state(user_id, state)


async def _get_last_activity(user_id: str) -> Optional[datetime]:
    state = await _load_state(user_id)
    ts = state.get("last_activity_at")
    if ts:
        try:
            return datetime.fromisoformat(ts)
        except ValueError:
            pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# ProactiveCheck base class
# ─────────────────────────────────────────────────────────────────────────────

class ProactiveCheck(ABC):
    """
    Abstract base for a single proactive check.

    Subclasses implement:
      - check_id:       unique string, used for dedup key in storage.
      - cooldown_hours: minimum gap (in hours) between two fires for the same user.
      - should_trigger: async check — returns True if conditions are met.
      - build_message:  async — returns the message string to push to the user.
    """

    check_id: str = "base_check"
    cooldown_hours: float = 1.0

    async def _within_cooldown(self, user_id: str) -> bool:
        last = await _get_last_fired(user_id, self.check_id)
        if last is None:
            return False
        return (datetime.now() - last).total_seconds() < self.cooldown_hours * 3600

    @abstractmethod
    async def should_trigger(self, user_id: str) -> bool:
        """Return True if this check should fire for this user right now."""
        ...

    @abstractmethod
    async def build_message(self, user_id: str) -> str:
        """Return the notification message to push to the user."""
        ...

    async def run(self, user_id: str) -> Optional[Dict[str, Any]]:
        """
        Full lifecycle: cooldown → condition → message → dedup mark.
        Returns a pending_update dict or None.
        """
        try:
            if await self._within_cooldown(user_id):
                return None
            if not await self.should_trigger(user_id):
                return None
            message = await self.build_message(user_id)
            if not message:
                return None
            await _mark_fired(user_id, self.check_id)
            return {
                "user_id": user_id,
                "type": self.check_id,
                "message": message,
                "time": datetime.now().strftime("%I:%M %p"),
            }
        except Exception as exc:
            logger.exception(
                "ProactiveCheck '%s' failed for user %s: %s",
                self.check_id, user_id, exc,
            )
            return None


# ─────────────────────────────────────────────────────────────────────────────
# Check 1: Morning Briefing
# ─────────────────────────────────────────────────────────────────────────────

class MorningBriefingCheck(ProactiveCheck):
    """
    Fires once per day at the configured morning hour.
    Produces a full briefing: news summary + pending tasks + calendar events.
    """

    check_id = "morning_briefing"
    cooldown_hours = 20.0  # prevents double-fire; real gate is the hour check

    async def should_trigger(self, user_id: str) -> bool:
        now = datetime.now()
        return now.hour == _MORNING_BRIEFING_HOUR and now.minute < 15

    async def build_message(self, user_id: str) -> str:
        from briefing import morning_briefing_async
        from calendar_helper import get_todays_events
        from tasks import get_pending_tasks_async

        briefing = await morning_briefing_async(user_id)
        events = get_todays_events()
        pending = await get_pending_tasks_async(user_id)

        parts = [briefing]
        if events and "no events" not in events.lower():
            parts.append(f"\n\nCalendar: {events}")
        if pending and "no pending" not in pending.lower():
            parts.append(f"\n\nPending: {pending}")

        return "".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Check 2: Pre-Meeting Warning
# ─────────────────────────────────────────────────────────────────────────────

class PreMeetingCheck(ProactiveCheck):
    """
    Every 15 minutes: check if any calendar event starts within the next
    ALFRED_PRE_MEETING_WARN_MIN minutes (default 30). Fires once per event.
    """

    check_id = "pre_meeting"
    cooldown_hours = 0.4  # ~25 min — won't double-fire for the same event window

    async def should_trigger(self, user_id: str) -> bool:
        self._upcoming_event: Optional[str] = None
        try:
            from calendar_helper import get_todays_events
            events_text = get_todays_events()
            if not events_text or "no events" in events_text.lower():
                return False

            import re
            now = datetime.now()
            warn_cutoff = now + timedelta(minutes=_PRE_MEETING_WARN_MINUTES)

            # Parse "HH:MM" or "H:MM AM/PM" patterns from the events string
            for match in re.finditer(
                r"(\d{1,2}):(\d{2})\s*(am|pm)?",
                events_text,
                re.IGNORECASE,
            ):
                hour = int(match.group(1))
                minute = int(match.group(2))
                period = (match.group(3) or "").lower()
                if period == "pm" and hour != 12:
                    hour += 12
                elif period == "am" and hour == 12:
                    hour = 0

                event_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if now < event_dt <= warn_cutoff:
                    self._upcoming_event = events_text[:200]
                    return True
        except Exception as exc:
            logger.warning("PreMeetingCheck.should_trigger error: %s", exc)
        return False

    async def build_message(self, user_id: str) -> str:
        event_hint = getattr(self, "_upcoming_event", "an event")
        return (
            f"Heads up — you have something coming up in the next "
            f"{_PRE_MEETING_WARN_MINUTES} minutes: {event_hint}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Check 3: Stale High-Priority Task Nudge
# ─────────────────────────────────────────────────────────────────────────────

class StaleTaskCheck(ProactiveCheck):
    """
    Every hour: if there's a task that has been pending for longer than
    ALFRED_STALE_TASK_HOURS (default 24 h) and has HIGH or URGENT priority
    in tasks_v2, OR is any task in the legacy tasks.json older than the
    threshold, push a nudge.
    """

    check_id = "stale_task"
    cooldown_hours = 4.0

    async def should_trigger(self, user_id: str) -> bool:
        self._stale_tasks: List[str] = []
        threshold = datetime.now() - timedelta(hours=_STALE_TASK_HOURS)

        # ── Check legacy tasks.json ──────────────────────────────────────────
        try:
            from tasks import load_tasks_async
            tasks = await load_tasks_async(user_id)
            for t in tasks:
                if t.get("done"):
                    continue
                created_str = t.get("created", "")
                try:
                    # Format from tasks.py: "14 Apr 2026, 09:30 AM"
                    created_dt = datetime.strptime(created_str, "%d %b %Y, %I:%M %p")
                    if created_dt < threshold:
                        self._stale_tasks.append(t.get("text", "unnamed task"))
                except ValueError:
                    pass
        except Exception as exc:
            logger.warning("StaleTaskCheck legacy load error: %s", exc)

        # ── Check tasks_v2 table ─────────────────────────────────────────────
        try:
            from storage import STORAGE_BACKEND, get_async_pool, _connect_sqlite, DB_PATH
            sql = """
                SELECT text FROM tasks_v2
                WHERE user_id = ? AND status IN ('pending','in_progress')
                  AND priority IN ('high','urgent')
                  AND created_at < ?
            """
            threshold_iso = threshold.isoformat()

            if STORAGE_BACKEND == "postgres":
                pool = await get_async_pool()
                async with pool.connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            sql.replace("?", "%s"), (user_id, threshold_iso)
                        )
                        rows = await cur.fetchall()
                        self._stale_tasks += [r["text"] for r in rows]
            else:
                def _q():
                    conn = _connect_sqlite(DB_PATH)
                    rows = conn.execute(sql, (user_id, threshold_iso)).fetchall()
                    conn.close()
                    return [dict(r) for r in rows]
                rows = await asyncio.get_event_loop().run_in_executor(None, _q)
                self._stale_tasks += [r["text"] for r in rows]
        except Exception as exc:
            logger.warning("StaleTaskCheck tasks_v2 query error: %s", exc)

        return len(self._stale_tasks) > 0

    async def build_message(self, user_id: str) -> str:
        stale = getattr(self, "_stale_tasks", [])[:3]
        task_list = "\n".join(f"• {t}" for t in stale)
        suffix = (
            f" (+{len(self._stale_tasks) - 3} more)"
            if len(self._stale_tasks) > 3 else ""
        )
        return (
            f"Just checking in — these tasks have been sitting for a while:\n"
            f"{task_list}{suffix}\n\nWant to tackle any of them now?"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Check 4: Email Backlog Alert
# ─────────────────────────────────────────────────────────────────────────────

class EmailBacklogCheck(ProactiveCheck):
    """
    Every hour: if there are more than ALFRED_EMAIL_BACKLOG_COUNT (default 5)
    unread emails, surface a brief summary.
    """

    check_id = "email_backlog"
    cooldown_hours = 3.0

    async def should_trigger(self, user_id: str) -> bool:
        self._email_count = 0
        self._email_senders: List[str] = []
        try:
            from email_reader import get_unread_emails
            emails = get_unread_emails(user_id=user_id) or []
            self._email_count = len(emails)
            self._email_senders = [
                e.get("from", "").split("<")[0].strip()
                for e in emails[:5]
            ]
            return self._email_count >= _EMAIL_BACKLOG_THRESHOLD
        except Exception as exc:
            logger.warning("EmailBacklogCheck error: %s", exc)
            return False

    async def build_message(self, user_id: str) -> str:
        count = getattr(self, "_email_count", 0)
        senders = getattr(self, "_email_senders", [])
        sender_preview = ", ".join(senders[:3])
        return (
            f"You have {count} unread emails. "
            f"Recent senders: {sender_preview}. "
            f"Want me to brief you on them?"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Check 5: Inactivity Nudge
# ─────────────────────────────────────────────────────────────────────────────

class InactivityNudgeCheck(ProactiveCheck):
    """
    During business hours (9–20), if the user hasn't interacted in
    ALFRED_INACTIVITY_HOURS (default 4 h), send a gentle check-in.

    Requires _record_last_activity() to be called from alfred_core.chat().
    """

    check_id = "inactivity_nudge"
    cooldown_hours = 5.0
    priority = 1  # lower number = higher priority

    async def should_trigger(self, user_id: str) -> bool:
        now = datetime.now()
        if not (_BUSINESS_HOURS_START <= now.hour < _BUSINESS_HOURS_END):
            return False

        last = await _get_last_activity(user_id)
        if last is None:
            return False  # Never interacted — don't nudge a new user

        hours_silent = (now - last).total_seconds() / 3600
        return hours_silent >= _INACTIVITY_HOURS

    async def build_message(self, user_id: str) -> str:
        from memory import load_memory_async
        memory = await load_memory_async(user_id)
        name = memory.get("name", "")
        greeting = f"Hey {name}," if name else "Hey,"
        return (
            f"{greeting} it's been a while — anything I can help you with? "
            f"Tasks, emails, research, or just a quick briefing?"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Check 6: Overdue Task Alert
# ─────────────────────────────────────────────────────────────────────────────

class OverdueTaskCheck(ProactiveCheck):
    """
    Every 2 hours: alert if any task in tasks_v2 has a due_date that has passed.
    Higher priority than StaleTaskCheck because a deadline was explicitly set.
    """

    check_id = "overdue_task"
    cooldown_hours = 2.0
    priority = 0  # highest priority

    async def should_trigger(self, user_id: str) -> bool:
        self._overdue: List[Any] = []
        try:
            from task_manager_v2 import get_overdue_tasks_async
            self._overdue = await get_overdue_tasks_async(user_id)
            return len(self._overdue) > 0
        except Exception as exc:
            logger.warning("OverdueTaskCheck error: %s", exc)
            return False

    async def build_message(self, user_id: str) -> str:
        tasks = getattr(self, "_overdue", [])[:3]
        lines = []
        for t in tasks:
            due_str = t.due_date.strftime("%d %b") if t.due_date else "?"
            lines.append(f"• {t.text} (was due {due_str})")
        suffix = f" (+{len(self._overdue)-3} more)" if len(self._overdue) > 3 else ""
        return (
            f"You have {len(self._overdue)} overdue task(s):\n"
            + "\n".join(lines) + suffix
            + "\n\nWant to reschedule or mark any as done?"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Check 7: Evening Summary
# ─────────────────────────────────────────────────────────────────────────────

_EVENING_HOUR = int(os.getenv("ALFRED_EVENING_HOUR", "18"))

class EveningBriefingCheck(ProactiveCheck):
    """
    Fires once per day at 6 PM (configurable via ALFRED_EVENING_HOUR).
    Summarises: tasks completed today, tasks still pending, and tomorrow's calendar.
    """

    check_id = "evening_briefing"
    cooldown_hours = 20.0
    priority = 2

    async def should_trigger(self, user_id: str) -> bool:
        now = datetime.now()
        return now.hour == _EVENING_HOUR and now.minute < 15

    async def build_message(self, user_id: str) -> str:
        from briefing import end_of_day
        from task_manager_v2 import get_tasks_tree_async

        summary_parts = []

        # End-of-day briefing from existing module
        try:
            eod = end_of_day()
            if eod:
                summary_parts.append(eod)
        except Exception:
            pass

        # Tasks completed today vs still pending
        try:
            all_tasks = await get_tasks_tree_async(user_id)
            today = datetime.now().date()
            done_today = [
                t for t in all_tasks
                if t.get("status") == "done"
                and t.get("completed_at")
                and datetime.fromisoformat(str(t["completed_at"])).date() == today
            ]
            still_pending = [t for t in all_tasks if t.get("status") in ("pending", "in_progress")]

            if done_today:
                summary_parts.append(f"\n✓ Completed today: " + ", ".join(t["text"] for t in done_today[:3]))
            if still_pending:
                summary_parts.append(
                    f"\n• Still pending: " + ", ".join(t["text"] for t in still_pending[:3])
                    + (f" (+{len(still_pending)-3} more)" if len(still_pending) > 3 else "")
                )
        except Exception:
            pass

        if not summary_parts:
            return "Evening check-in: how did today go? Anything you want to wrap up or note before tomorrow?"

        return "Evening wrap-up:\n" + "".join(summary_parts)


# ─────────────────────────────────────────────────────────────────────────────
# ProactiveWorker
# ─────────────────────────────────────────────────────────────────────────────

class ProactiveWorker:
    """
    Orchestrates all ProactiveChecks against all registered users.

    Integration with existing scheduler.py:
      - Shares the same BackgroundScheduler instance.
      - Pushes to the same `pending_updates` list the frontend polls.
      - The existing morning_briefing and followup jobs coexist without conflict.
    """

    def __init__(self, scheduler: BackgroundScheduler):
        self._scheduler = scheduler
        self._checks: List[ProactiveCheck] = []
        self._started = False

    def register(self, check: ProactiveCheck) -> "ProactiveWorker":
        """Add a check to the worker. Returns self for chaining."""
        self._checks.append(check)
        logger.info("Registered proactive check: %s", check.check_id)
        return self

    def _get_all_user_ids(self) -> List[str]:
        """Load all active user IDs from the accounts store."""
        try:
            from storage import load_accounts
            accounts = load_accounts()
            ids = []
            for _email, acct in accounts.items():
                uid = acct.get("storage_id") or acct.get("user_id")
                if uid:
                    ids.append(uid)
            return ids
        except Exception as exc:
            logger.error("ProactiveWorker: failed to load accounts: %s", exc)
            return []

    def _run_cycle(self) -> None:
        """
        Synchronous APScheduler callback that spins up an asyncio event loop
        to run all checks for all users.
        """
        user_ids = self._get_all_user_ids()
        if not user_ids:
            return

        async def _async_cycle() -> None:
            from scheduler import pending_updates  # shared list

            # Run checks for all users concurrently
            tasks = [
                self._run_checks_for_user(uid, pending_updates)
                for uid in user_ids
            ]
            await asyncio.gather(*tasks, return_exceptions=True)

        try:
            asyncio.run(_async_cycle())
        except RuntimeError:
            # If there's already a running loop (e.g. in tests), use it
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(_async_cycle())
            finally:
                loop.close()

    async def _run_checks_for_user(
        self,
        user_id: str,
        pending_updates: list,
    ) -> None:
        """
        Run all registered checks for a single user, then:
          1. Sort candidates by check.priority (lower = more urgent).
          2. Group checks of similar type (e.g. multiple task alerts → one message).
          3. Suppress if more than MAX_NOTIFICATIONS_PER_CYCLE already queued for this user.
        """
        MAX_NOTIFICATIONS_PER_CYCLE = 2

        # Count how many updates are already queued for this user in this cycle
        already_queued = sum(1 for u in pending_updates if u.get("user_id") == user_id)
        if already_queued >= MAX_NOTIFICATIONS_PER_CYCLE:
            return

        # Collect all triggered updates
        candidates: List[Dict] = []
        for check in self._checks:
            try:
                update = await check.run(user_id)
                if update:
                    # Attach priority for sorting (default 5 = low priority)
                    update["_priority"] = getattr(check, "priority", 5)
                    candidates.append(update)
            except Exception as exc:
                logger.exception(
                    "Unhandled error in check '%s' for user %s: %s",
                    check.check_id, user_id, exc,
                )

        if not candidates:
            return

        # Sort by priority
        candidates.sort(key=lambda u: u.get("_priority", 5))

        # Group task-related alerts into one combined message
        task_alerts = [c for c in candidates if c.get("type") in ("stale_task", "overdue_task")]
        non_task = [c for c in candidates if c.get("type") not in ("stale_task", "overdue_task")]

        grouped: List[Dict] = list(non_task)
        if len(task_alerts) > 1:
            # Merge task alerts into one
            combined_msg = "\n\n".join(a["message"] for a in task_alerts)
            merged = task_alerts[0].copy()
            merged["message"] = combined_msg
            merged["type"] = "task_summary"
            grouped.append(merged)
        elif task_alerts:
            grouped.extend(task_alerts)

        # Re-sort merged list
        grouped.sort(key=lambda u: u.get("_priority", 5))

        # Push up to the cap
        slots_remaining = MAX_NOTIFICATIONS_PER_CYCLE - already_queued
        for update in grouped[:slots_remaining]:
            update.pop("_priority", None)  # clean internal field
            pending_updates.append(update)
            logger.info(
                "Proactive update queued for user %s: type=%s",
                user_id, update.get("type"),
            )

    def start(self) -> None:
        """Register the worker job and start the scheduler if not running."""
        if self._started:
            logger.warning("ProactiveWorker already started — skipping.")
            return

        # Main cycle: every 15 minutes.
        # Individual checks use their own cooldown logic to control actual fire rate.
        self._scheduler.add_job(
            self._run_cycle,
            "interval",
            minutes=15,
            id="proactive_worker_cycle",
            replace_existing=True,
        )

        if not self._scheduler.running:
            self._scheduler.start()

        self._started = True
        logger.info(
            "ProactiveWorker started with %d checks (cycle: 15 min).",
            len(self._checks),
        )

    def stop(self) -> None:
        if self._started and self._scheduler.running:
            try:
                self._scheduler.remove_job("proactive_worker_cycle")
            except Exception:
                pass
            self._started = False
            logger.info("ProactiveWorker stopped.")


# ─────────────────────────────────────────────────────────────────────────────
# Factory: singleton worker with default checks
# ─────────────────────────────────────────────────────────────────────────────

_worker_instance: Optional[ProactiveWorker] = None


def get_proactive_worker() -> ProactiveWorker:
    """
    Returns the singleton ProactiveWorker, creating and wiring it up with all
    default checks on first call. The worker is NOT started — call .start()
    from main.py's startup event.
    """
    global _worker_instance
    if _worker_instance is not None:
        return _worker_instance

    from scheduler import scheduler as _shared_scheduler  # reuse existing instance

    worker = ProactiveWorker(scheduler=_shared_scheduler)
    worker.register(OverdueTaskCheck())       # priority 0 — highest
    worker.register(MorningBriefingCheck())   # priority default (5)
    worker.register(EveningBriefingCheck())   # priority 2
    worker.register(PreMeetingCheck())
    worker.register(StaleTaskCheck())
    worker.register(EmailBacklogCheck())
    worker.register(InactivityNudgeCheck())   # priority 1

    _worker_instance = worker
    return worker


def start_proactive_worker() -> None:
    """
    Convenience function for main.py's startup event.

    Call this alongside start_followup_scheduler():

        @app.on_event("startup")
        async def startup():
            await init_storage()
            await run_migrations()
            schedule_morning_briefing(...)
            start_followup_scheduler()
            start_proactive_worker()       # ← add this
    """
    get_proactive_worker().start()
