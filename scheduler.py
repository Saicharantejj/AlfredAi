from apscheduler.schedulers.background import BackgroundScheduler
from typing import Optional, List, Dict, Any, Union
from datetime import datetime
import json, os
import asyncio
import logging

logger = logging.getLogger("alfred.scheduler")
scheduler = BackgroundScheduler()
pending_updates = []
_FOLLOWUP_SCHEDULER_STARTED = False

def get_morning_briefing(user_id: str, display_name: Optional[str] = None) -> str:
    from alfred_core import chat
    now = datetime.now()
    greeting = f"Good morning! It's {now.strftime('%A, %d %B %Y')}."
    messages = [{
        "role": "user",
        "content": "Give me a sharp morning briefing. Include the day, a productivity tip, and one motivating thought. Keep it under 4 sentences."
    }]
    try:
        reply = chat(messages, user_id, display_name)
        return reply.get("reply", greeting)
    except Exception as e:
        return f"Good morning! Alfred couldn't fetch your briefing: {str(e)}"

def schedule_morning_briefing(user_id: str, display_name: Optional[str] = None, hour: int = 8, minute: int = 0):
    def job():
        briefing = get_morning_briefing(user_id, display_name)
        pending_updates.append({
            "user_id": user_id,
            "type": "briefing",
            "message": briefing,
            "time": datetime.now().strftime("%I:%M %p")
        })

    scheduler.add_job(job, 'cron', hour=hour, minute=minute, id=f'morning_briefing_{user_id}', replace_existing=True)
    if not scheduler.running:
        scheduler.start()

def get_pending_updates(user_id: Optional[str] = None):
    if user_id is None:
        updates = pending_updates.copy()
        pending_updates.clear()
        return updates

    matches = [u for u in pending_updates if u.get("user_id") == user_id]
    pending_updates[:] = [u for u in pending_updates if u.get("user_id") != user_id]
    return matches


# ── Follow-up engine ─────────────────────────────────────────────────────────

def _run_followup_check():
    """Background job: check all users for due follow-up items every 15 minutes."""
    import asyncio
    from storage import load_accounts
    from memory_tracker import get_due_followups_async, mark_followup_sent_async, generate_followup_message

    try:
        accounts = load_accounts()
    except Exception as exc:
        logger.exception("Follow-up check: could not load accounts: %s", exc)
        return

    for email, account in accounts.items():
        uid = account.get("storage_id") or account.get("user_id")
        if not uid:
            continue
        try:
            # Run async storage calls in a fresh event loop slice
            due_items = asyncio.run(_followup_cycle(uid))
        except Exception as exc:
            logger.exception("Follow-up check failed for user %s: %s", uid, exc)


async def _followup_cycle(user_id: str) -> None:
    """Async inner loop for a single user's follow-up check."""
    from memory_tracker import get_due_followups_async, mark_followup_sent_async, generate_followup_message

    due_items = await get_due_followups_async(user_id)
    for item in due_items:
        message = generate_followup_message(item)
        pending_updates.append({
            "user_id": user_id,
            "type": "followup",
            "message": message,
            "item_id": item.get("id"),
            "time": datetime.now().strftime("%I:%M %p"),
        })
        await mark_followup_sent_async(user_id, item["id"])
        logger.info(
            "Queued follow-up for user %s: '%s' (item %s)",
            user_id, message, item.get("id")
        )


def start_followup_scheduler():
    """Register the follow-up background job and start the scheduler if not running."""
    global _FOLLOWUP_SCHEDULER_STARTED
    if _FOLLOWUP_SCHEDULER_STARTED:
        return
    scheduler.add_job(
        _run_followup_check,
        "interval",
        minutes=15,
        id="followup_check",
        replace_existing=True,
    )
    if not scheduler.running:
        scheduler.start()
    _FOLLOWUP_SCHEDULER_STARTED = True
    logger.info("Follow-up scheduler started (every 15 minutes)")
