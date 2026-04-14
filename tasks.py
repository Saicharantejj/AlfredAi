from datetime import datetime
from storage import (
    load_user_document, 
    save_user_document,
    load_user_document_async,
    save_user_document_async
)

def load_tasks(user_id: str):
    return load_user_document(user_id, "tasks.json", [])

async def load_tasks_async(user_id: str):
    return await load_user_document_async(user_id, "tasks.json", [])

def save_tasks(tasks, user_id: str):
    save_user_document(user_id, "tasks.json", tasks)

async def save_tasks_async(tasks, user_id: str):
    await save_user_document_async(user_id, "tasks.json", tasks)

def add_task(text, user_id: str):
    tasks = load_tasks(user_id)
    next_id = max((t["id"] for t in tasks if isinstance(t.get("id"), int)), default=0) + 1
    task = {
        "id": next_id,
        "text": text,
        "done": False,
        "created": datetime.now().strftime("%d %b %Y, %I:%M %p")
    }
    tasks.append(task)
    save_tasks(tasks, user_id)
    return "Task added: " + text

async def add_task_async(text, user_id: str):
    tasks = await load_tasks_async(user_id)
    next_id = max((t["id"] for t in tasks if isinstance(t.get("id"), int)), default=0) + 1
    task = {
        "id": next_id,
        "text": text,
        "done": False,
        "created": datetime.now().strftime("%d %b %Y, %I:%M %p")
    }
    tasks.append(task)
    await save_tasks_async(tasks, user_id)
    return "Task added: " + text

def get_pending_tasks(user_id: str):
    tasks = load_tasks(user_id)
    pending = [t for t in tasks if not t.get("done", False)]
    if not pending:
        return "No pending tasks. You are all caught up."
    return "\n".join([str(t.get("id", "")) + ". " + t.get("text", "") + " (added " + t.get("created", "") + ")" for t in pending])

async def get_pending_tasks_async(user_id: str):
    tasks = await load_tasks_async(user_id)
    pending = [t for t in tasks if not t.get("done", False)]
    if not pending:
        return "No pending tasks. You are all caught up."
    return "\n".join([str(t.get("id", "")) + ". " + t.get("text", "") + " (added " + t.get("created", "") + ")" for t in pending])

def complete_task(text, user_id: str):
    tasks = load_tasks(user_id)
    for t in tasks:
        if text.lower() in t.get("text", "").lower() and not t.get("done", False):
            t["done"] = True
            save_tasks(tasks, user_id)
            return "Marked as done: " + t.get("text", "")
    return "Could not find that task."

async def complete_task_async(text, user_id: str):
    tasks = await load_tasks_async(user_id)
    for t in tasks:
        if text.lower() in t.get("text", "").lower() and not t.get("done", False):
            t["done"] = True
            await save_tasks_async(tasks, user_id)
            return "Marked as done: " + t.get("text", "")
    return "Could not find that task."

def get_all_tasks(user_id: str):
    tasks = load_tasks(user_id)
    if not tasks:
        return "No tasks yet."
    return "\n".join([("✓ " if t.get("done", False) else "• ") + t.get("text", "") for t in tasks])

async def get_all_tasks_async(user_id: str):
    tasks = await load_tasks_async(user_id)
    if not tasks:
        return "No tasks yet."
    return "\n".join([("✓ " if t.get("done", False) else "• ") + t.get("text", "") for t in tasks])
