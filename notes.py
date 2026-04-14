from datetime import datetime
from storage import (
    load_user_document, 
    save_user_document,
    load_user_document_async,
    save_user_document_async
)

def save_note(content: str, user_id: str) -> str:
    notes = load_user_document(user_id, "notes.json", [])
    note = {
        "id": len(notes) + 1,
        "content": content,
        "time": datetime.now().strftime("%d %b %Y, %I:%M %p")
    }
    notes.append(note)
    save_user_document(user_id, "notes.json", notes)
    return f"Note saved at {note['time']}"

async def save_note_async(content: str, user_id: str) -> str:
    notes = await load_user_document_async(user_id, "notes.json", [])
    note = {
        "id": len(notes) + 1,
        "content": content,
        "time": datetime.now().strftime("%d %b %Y, %I:%M %p")
    }
    notes.append(note)
    await save_user_document_async(user_id, "notes.json", notes)
    return f"Note saved at {note['time']}"

def get_notes(user_id: str) -> list:
    return load_user_document(user_id, "notes.json", [])

async def get_notes_async(user_id: str) -> list:
    return await load_user_document_async(user_id, "notes.json", [])
