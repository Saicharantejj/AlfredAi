from datetime import datetime

from storage import (
    load_user_document, 
    save_user_document,
    load_user_document_async,
    save_user_document_async
)

DEFAULT_HABITS = {
    "message_counts": {},
    "hourly_activity": {},
    "frequent_contacts": {},
    "frequent_apps": {},
    "frequent_music": {},
    "total_interactions": 0
}

def load_habits(user_id: str):
    return load_user_document(user_id, "habits.json", DEFAULT_HABITS.copy())

async def load_habits_async(user_id: str):
    return await load_user_document_async(user_id, "habits.json", DEFAULT_HABITS.copy())

def save_habits(habits, user_id: str):
    save_user_document(user_id, "habits.json", habits)

async def save_habits_async(habits, user_id: str):
    await save_user_document_async(user_id, "habits.json", habits)

def track_interaction(message: str, user_id: str):
    habits = load_habits(user_id)
    now = datetime.now()
    hour = str(now.hour)
    habits["total_interactions"] = habits.get("total_interactions", 0) + 1

    # Track hourly activity
    habits["hourly_activity"][hour] = habits["hourly_activity"].get(hour, 0) + 1

    # Track frequent topics
    keywords = ["weather", "news", "music", "spotify", "whatsapp", "email", "task", "reminder", "calendar", "stock", "brightness", "volume"]
    for word in keywords:
        if word in message.lower():
            habits["message_counts"][word] = habits["message_counts"].get(word, 0) + 1

    save_habits(habits, user_id)

async def track_interaction_async(message: str, user_id: str):
    habits = await load_habits_async(user_id)
    now = datetime.now()
    hour = str(now.hour)
    habits["total_interactions"] = habits.get("total_interactions", 0) + 1

    # Track hourly activity
    habits["hourly_activity"][hour] = habits["hourly_activity"].get(hour, 0) + 1

    # Track frequent topics
    keywords = ["weather", "news", "music", "spotify", "whatsapp", "email", "task", "reminder", "calendar", "stock", "brightness", "volume"]
    for word in keywords:
        if word in message.lower():
            habits["message_counts"][word] = habits["message_counts"].get(word, 0) + 1

    await save_habits_async(habits, user_id)

def get_most_active_hour(user_id: str):
    habits = load_habits(user_id)
    activity = habits.get("hourly_activity", {})
    if not activity:
        return None
    return max(activity, key=activity.get)

async def get_most_active_hour_async(user_id: str):
    habits = await load_habits_async(user_id)
    activity = habits.get("hourly_activity", {})
    if not activity:
        return None
    return max(activity, key=activity.get)

def get_top_requests(user_id: str):
    habits = load_habits(user_id)
    counts = habits.get("message_counts", {})
    if not counts:
        return "Not enough data yet."
    sorted_counts = sorted(counts.items(), key=lambda x: x[1], reverse=True)
    return ", ".join([k + " (" + str(v) + " times)" for k, v in sorted_counts[:5]])

async def get_top_requests_async(user_id: str):
    habits = await load_habits_async(user_id)
    counts = habits.get("message_counts", {})
    if not counts:
        return "Not enough data yet."
    sorted_counts = sorted(counts.items(), key=lambda x: x[1], reverse=True)
    return ", ".join([k + " (" + str(v) + " times)" for k, v in sorted_counts[:5]])

def get_habit_insights(user_id: str):
    habits = load_habits(user_id)
    total = habits.get("total_interactions", 0)
    if total < 10:
        return "Alfred is still learning your habits. Keep using him and he will get smarter."

    top = get_top_requests(user_id)
    active_hour = get_most_active_hour(user_id)
    hour_label = str(active_hour) + ":00" if active_hour else "unknown"

    return "You have had " + str(total) + " interactions with Alfred. Your most common requests are: " + top + ". You are most active around " + hour_label + "."

async def get_habit_insights_async(user_id: str):
    habits = await load_habits_async(user_id)
    total = habits.get("total_interactions", 0)
    if total < 10:
        return "Alfred is still learning your habits. Keep using him and he will get smarter."

    top = await get_top_requests_async(user_id)
    active_hour = await get_most_active_hour_async(user_id)
    hour_label = str(active_hour) + ":00" if active_hour else "unknown"

    return "You have had " + str(total) + " interactions with Alfred. Your most common requests are: " + top + ". You are most active around " + hour_label + "."
