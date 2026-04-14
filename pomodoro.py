import time
from storage import (
    load_user_document, 
    save_user_document,
    load_user_document_async,
    save_user_document_async
)

def get_pomodoro_state(user_id: str) -> dict:
    try:
        state = load_user_document(user_id, "pomodoro.json", None)
        if state:
            if state.get("is_running"):
                now = int(time.time())
                elapsed = now - state.get("last_updated", now)
                state["seconds_left"] -= elapsed
                if state["seconds_left"] < 0:
                    state["seconds_left"] = 0
                    state["is_running"] = False
                state["last_updated"] = now
            return state
    except Exception:
        pass

    # Default state if missing or corrupted
    return {
        "is_running": False,
        "seconds_left": 25 * 60,
        "phase": "work",
        "session": 0,
        "last_updated": int(time.time())
    }

async def get_pomodoro_state_async(user_id: str) -> dict:
    try:
        state = await load_user_document_async(user_id, "pomodoro.json", None)
        if state:
            if state.get("is_running"):
                now = int(time.time())
                elapsed = now - state.get("last_updated", now)
                state["seconds_left"] -= elapsed
                if state["seconds_left"] < 0:
                    state["seconds_left"] = 0
                    state["is_running"] = False
                state["last_updated"] = now
            return state
    except Exception:
        pass

    # Default state if missing or corrupted
    return {
        "is_running": False,
        "seconds_left": 25 * 60,
        "phase": "work",
        "session": 0,
        "last_updated": int(time.time())
    }

def update_pomodoro_state(user_id: str, state_update: dict):
    # Retrieve current state to merge
    current = get_pomodoro_state(user_id)
    
    # Overwrite with updates
    for k, v in state_update.items():
        current[k] = v
        
    current["last_updated"] = int(time.time())
    save_user_document(user_id, "pomodoro.json", current)
    return current

async def update_pomodoro_state_async(user_id: str, state_update: dict):
    # Retrieve current state to merge
    current = await get_pomodoro_state_async(user_id)
    
    # Overwrite with updates
    for k, v in state_update.items():
        current[k] = v
        
    current["last_updated"] = int(time.time())
    await save_user_document_async(user_id, "pomodoro.json", current)
    return current
