from datetime import datetime, timedelta
from habits import load_habits
from storage import load_user_document, save_user_document

def load_suggestion_state(user_id: str):
    return load_user_document(user_id, "suggestions.json", {"last_suggestions": {}, "dismissed": []})

def save_suggestion_state(state, user_id: str):
    save_user_document(user_id, "suggestions.json", state)

def get_contextual_suggestion(user_id: str) -> str:
    now = datetime.now()
    hour = now.hour
    weekday = now.strftime("%A")
    habits = load_habits(user_id)
    state = load_suggestion_state(user_id)
    counts = habits.get("message_counts", {})
    hourly = habits.get("hourly_activity", {})
    suggestions = []

    # Morning suggestion
    if 8 <= hour <= 10:
        if counts.get("news", 0) > 3:
            suggestions.append("You usually check the news around this time. Want your morning briefing?")
        elif counts.get("weather", 0) > 2:
            suggestions.append("Good morning. Want to know today's weather before you start?")

    # Work hours suggestion
    if 10 <= hour <= 18:
        if counts.get("task", 0) > 2:
            from tasks import get_pending_tasks
            tasks = get_pending_tasks(user_id)
            if "No pending" not in tasks:
                suggestions.append("You have pending tasks. Want me to read them out?")

    # Music suggestion during work
    if 10 <= hour <= 18 and counts.get("spotify", 0) > 3:
        suggestions.append("You usually listen to music while working. Want me to play something?")

    # Evening suggestion
    if 17 <= hour <= 19:
        if counts.get("news", 0) > 2:
            suggestions.append("End of day. Want a quick news update before you wrap up?")

    # Market hours suggestion
    if 9 <= hour <= 15 and counts.get("stock", 0) > 2:
        suggestions.append("Markets are open. Want a quick update on Sensex and Nifty?")

    # Hydration reminder every 3 hours
    last_hydration = state.get("last_suggestions", {}).get("hydration")
    if last_hydration:
        last_time = datetime.fromisoformat(last_hydration)
        if (now - last_time).seconds > 10800:
            suggestions.append("It's been a while. Have you had some water recently?")
            state["last_suggestions"]["hydration"] = now.isoformat()
            save_suggestion_state(state, user_id)
    else:
        state["last_suggestions"]["hydration"] = now.isoformat()
        save_suggestion_state(state, user_id)

    if suggestions:
        import random
        return random.choice(suggestions)
    return None
