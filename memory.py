from storage import (
    load_user_document, 
    save_user_document,
    load_user_document_async,
    save_user_document_async
)

def load_memory(user_id: str) -> dict:
    return load_user_document(user_id, "memory.json", {})

async def load_memory_async(user_id: str) -> dict:
    return await load_user_document_async(user_id, "memory.json", {})

def save_memory(data: dict, user_id: str):
    save_user_document(user_id, "memory.json", data)

async def save_memory_async(data: dict, user_id: str):
    await save_user_document_async(user_id, "memory.json", data)

def get_memory_context(user_id: str) -> str:
    memory = load_memory(user_id)
    if not memory:
        return ""
    lines = ["Here is what you know about the user:"]
    for key, value in memory.items():
        lines.append(f"- {key}: {value}")
    return "\n".join(lines)

async def get_memory_context_async(user_id: str) -> str:
    memory = await load_memory_async(user_id)
    if not memory:
        return ""
    lines = ["Here is what you know about the user:"]
    for key, value in memory.items():
        lines.append(f"- {key}: {value}")
    return "\n".join(lines)

def update_memory(key: str, value: str, user_id: str):
    memory = load_memory(user_id)
    memory[key] = value
    save_memory(memory, user_id)

async def update_memory_async(key: str, value: str, user_id: str):
    memory = await load_memory_async(user_id)
    memory[key] = value
    await save_memory_async(memory, user_id)
