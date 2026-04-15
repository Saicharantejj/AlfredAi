"""
task_manager_v2.py — Alfred Task System (tasks_v2 table)

Full CRUD on tasks_v2 with:
  - Sub-task tree (parent_id)
  - Priority queue (urgent > high > medium > low)
  - Status state machine (pending → in_progress → blocked → done → cancelled)
  - LLM-assisted next-step suggestions
  - Backward shim so tasks.json callers still work

API surface (imported by main.py):
  create_task_async(task: TaskV2) -> str          # returns task id
  get_tasks_tree_async(user_id, status_filter)    # tree with subtasks
  update_task_async(task_id, user_id, **fields)   # partial update
  complete_task_async(task_id, user_id)
  delete_task_async(task_id, user_id)
  suggest_next_task_async(user_id) -> TaskV2|None
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from db_schemas import TaskPriority, TaskStatus, TaskV2

logger = logging.getLogger("alfred.task_manager_v2")

_PRIORITY_ORDER = {
    TaskPriority.URGENT: 0,
    TaskPriority.HIGH: 1,
    TaskPriority.MEDIUM: 2,
    TaskPriority.LOW: 3,
}


# ─────────────────────────────────────────────────────────────────────────────
# Internal DB helpers
# ─────────────────────────────────────────────────────────────────────────────

def _row_to_task(row: Dict, user_id: str) -> TaskV2:
    tags = row.get("tags", "[]")
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except Exception:
            tags = []

    return TaskV2(
        id=row["id"],
        user_id=user_id,
        text=row["text"],
        status=row.get("status", TaskStatus.PENDING),
        priority=row.get("priority", TaskPriority.MEDIUM),
        parent_id=row.get("parent_id"),
        workflow_id=row.get("workflow_id"),
        due_date=datetime.fromisoformat(row["due_date"]) if row.get("due_date") else None,
        tags=tags,
        notes=row.get("notes"),
        created_at=datetime.fromisoformat(row["created_at"]) if isinstance(row.get("created_at"), str) else datetime.now(),
        updated_at=datetime.fromisoformat(row["updated_at"]) if isinstance(row.get("updated_at"), str) else datetime.now(),
        completed_at=datetime.fromisoformat(row["completed_at"]) if row.get("completed_at") else None,
    )


async def _execute_write(sql: str, pg_sql: str, params: tuple) -> None:
    from storage import STORAGE_BACKEND, get_async_pool, _connect_sqlite, DB_PATH

    if STORAGE_BACKEND == "postgres":
        pool = await get_async_pool()
        async with pool.connection() as conn:
            await conn.execute(pg_sql, params)
            await conn.commit()
    else:
        def _w():
            c = _connect_sqlite(DB_PATH)
            with c:
                c.execute(sql, params)
            c.close()
        await asyncio.get_event_loop().run_in_executor(None, _w)


async def _fetch_rows(sql: str, pg_sql: str, params: tuple) -> List[Dict]:
    from storage import STORAGE_BACKEND, get_async_pool, _connect_sqlite, DB_PATH

    if STORAGE_BACKEND == "postgres":
        pool = await get_async_pool()
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(pg_sql, params)
                rows = await cur.fetchall()
                return [dict(r) for r in rows]
    else:
        def _q():
            c = _connect_sqlite(DB_PATH)
            rows = c.execute(sql, params).fetchall()
            c.close()
            return [dict(r) for r in rows]
        return await asyncio.get_event_loop().run_in_executor(None, _q)


# ─────────────────────────────────────────────────────────────────────────────
# CRUD
# ─────────────────────────────────────────────────────────────────────────────

async def create_task_async(task: TaskV2) -> str:
    """Insert a new task. Returns the task id."""
    if not task.id:
        task.id = uuid.uuid4().hex[:12]
    now = datetime.now().isoformat()

    sql = """
        INSERT INTO tasks_v2
            (id, user_id, text, status, priority, parent_id, workflow_id,
             due_date, tags, notes, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    pg_sql = sql.replace("?", "%s")

    params = (
        task.id, task.user_id, task.text, task.status, task.priority,
        task.parent_id, task.workflow_id,
        task.due_date.isoformat() if task.due_date else None,
        json.dumps(task.tags),
        task.notes,
        now, now,
    )
    await _execute_write(sql, pg_sql, params)
    logger.info("Task created: %s (id=%s, user=%s)", task.text[:60], task.id, task.user_id)
    return task.id


async def get_tasks_tree_async(
    user_id: str,
    status_filter: Optional[List[str]] = None,
) -> List[Dict]:
    """
    Return tasks as a flat list with 'subtasks' key for children.
    Sorted by priority, then created_at.
    """
    from storage import STORAGE_BACKEND

    if status_filter:
        placeholders_sq = ",".join("?" * len(status_filter))
        placeholders_pg = ",".join("%s" * len(status_filter))
        sql = f"SELECT * FROM tasks_v2 WHERE user_id = ? AND status IN ({placeholders_sq}) ORDER BY created_at DESC"
        pg_sql = f"SELECT * FROM tasks_v2 WHERE user_id = %s AND status IN ({placeholders_pg}) ORDER BY created_at DESC"
        params = tuple([user_id] + list(status_filter))
    else:
        sql = "SELECT * FROM tasks_v2 WHERE user_id = ? ORDER BY created_at DESC"
        pg_sql = "SELECT * FROM tasks_v2 WHERE user_id = %s ORDER BY created_at DESC"
        params = (user_id,)

    rows = await _fetch_rows(sql, pg_sql, params)
    tasks = [_row_to_task(r, user_id).model_dump() for r in rows]

    # Build tree
    id_map: Dict[str, Dict] = {t["id"]: t for t in tasks}
    for t in tasks:
        t["subtasks"] = []
    roots = []

    for t in tasks:
        pid = t.get("parent_id")
        if pid and pid in id_map:
            id_map[pid]["subtasks"].append(t)
        else:
            roots.append(t)

    # Sort roots by priority
    roots.sort(key=lambda t: _PRIORITY_ORDER.get(t.get("priority", "medium"), 2))
    return roots


async def get_task_by_id_async(task_id: str, user_id: str) -> Optional[TaskV2]:
    sql = "SELECT * FROM tasks_v2 WHERE id = ? AND user_id = ?"
    pg_sql = "SELECT * FROM tasks_v2 WHERE id = %s AND user_id = %s"
    rows = await _fetch_rows(sql, pg_sql, (task_id, user_id))
    if not rows:
        return None
    return _row_to_task(rows[0], user_id)


async def update_task_async(task_id: str, user_id: str, **fields) -> bool:
    """
    Partial update. Accepts: text, status, priority, notes, due_date, tags, parent_id.
    Returns True if a row was updated.
    """
    allowed = {"text", "status", "priority", "notes", "due_date", "tags", "parent_id", "workflow_id"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False

    now = datetime.now().isoformat()
    updates["updated_at"] = now

    # Serialize tags if list
    if "tags" in updates and isinstance(updates["tags"], list):
        updates["tags"] = json.dumps(updates["tags"])

    # Auto-set completed_at when marking done
    if updates.get("status") == TaskStatus.DONE:
        updates["completed_at"] = now

    from storage import STORAGE_BACKEND, get_async_pool, _connect_sqlite, DB_PATH

    set_clause_sq = ", ".join(f"{k} = ?" for k in updates)
    set_clause_pg = ", ".join(f"{k} = %s" for k in updates)
    vals = list(updates.values()) + [task_id, user_id]

    sql = f"UPDATE tasks_v2 SET {set_clause_sq} WHERE id = ? AND user_id = ?"
    pg_sql = f"UPDATE tasks_v2 SET {set_clause_pg} WHERE id = %s AND user_id = %s"

    if STORAGE_BACKEND == "postgres":
        pool = await get_async_pool()
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(pg_sql, vals)
                affected = cur.rowcount
            await conn.commit()
        return affected > 0
    else:
        def _u():
            c = _connect_sqlite(DB_PATH)
            with c:
                cur = c.execute(sql, vals)
                return cur.rowcount > 0
            c.close()
        return await asyncio.get_event_loop().run_in_executor(None, _u)


async def complete_task_async(task_id: str, user_id: str) -> bool:
    return await update_task_async(task_id, user_id, status=TaskStatus.DONE)


async def delete_task_async(task_id: str, user_id: str) -> bool:
    from storage import STORAGE_BACKEND, get_async_pool, _connect_sqlite, DB_PATH
    sql = "DELETE FROM tasks_v2 WHERE id = ? AND user_id = ?"
    pg_sql = "DELETE FROM tasks_v2 WHERE id = %s AND user_id = %s"

    if STORAGE_BACKEND == "postgres":
        pool = await get_async_pool()
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(pg_sql, (task_id, user_id))
                affected = cur.rowcount
            await conn.commit()
        return affected > 0
    else:
        def _d():
            c = _connect_sqlite(DB_PATH)
            with c:
                return c.execute(sql, (task_id, user_id)).rowcount > 0
            c.close()
        return await asyncio.get_event_loop().run_in_executor(None, _d)


# ─────────────────────────────────────────────────────────────────────────────
# Intelligence: next-step suggestions
# ─────────────────────────────────────────────────────────────────────────────

async def suggest_next_task_async(user_id: str) -> Optional[TaskV2]:
    """
    Return the single highest-priority pending task the user should work on next.
    Algorithm: urgent > high, then overdue (by due_date), then oldest created.
    """
    sql = """
        SELECT * FROM tasks_v2
        WHERE user_id = ? AND status IN ('pending', 'in_progress') AND parent_id IS NULL
        ORDER BY
            CASE priority
                WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                WHEN 'medium' THEN 2 ELSE 3
            END,
            CASE WHEN due_date IS NOT NULL AND due_date < ? THEN 0 ELSE 1 END,
            created_at ASC
        LIMIT 1
    """
    pg_sql = sql.replace("?", "%s")
    now_iso = datetime.now().isoformat()
    rows = await _fetch_rows(sql, pg_sql, (user_id, now_iso))
    if not rows:
        return None
    return _row_to_task(rows[0], user_id)


async def get_overdue_tasks_async(user_id: str) -> List[TaskV2]:
    """Return tasks whose due_date has passed and are not done."""
    now_iso = datetime.now().isoformat()
    sql = """
        SELECT * FROM tasks_v2
        WHERE user_id = ? AND status NOT IN ('done','cancelled')
          AND due_date IS NOT NULL AND due_date < ?
        ORDER BY due_date ASC
    """
    pg_sql = sql.replace("?", "%s")
    rows = await _fetch_rows(sql, pg_sql, (user_id, now_iso))
    return [_row_to_task(r, user_id) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# Backward compatibility: import tasks from legacy tasks.json
# ─────────────────────────────────────────────────────────────────────────────

async def migrate_legacy_tasks_async(user_id: str) -> int:
    """
    One-time import of tasks.json rows into tasks_v2.
    Skips tasks that are already present (by text match).
    Returns count migrated.
    """
    try:
        from tasks import load_tasks_async
        legacy = await load_tasks_async(user_id)
    except Exception:
        return 0

    if not legacy:
        return 0

    # Get existing texts to avoid duplicates
    existing = await _fetch_rows(
        "SELECT text FROM tasks_v2 WHERE user_id = ?",
        "SELECT text FROM tasks_v2 WHERE user_id = %s",
        (user_id,),
    )
    existing_texts = {r["text"].lower() for r in existing}

    count = 0
    for t in legacy:
        text = t.get("text", "").strip()
        if not text or text.lower() in existing_texts:
            continue
        task = TaskV2(
            user_id=user_id,
            text=text,
            status=TaskStatus.DONE if t.get("done") else TaskStatus.PENDING,
            priority=TaskPriority.MEDIUM,
        )
        try:
            await create_task_async(task)
            existing_texts.add(text.lower())
            count += 1
        except Exception as exc:
            logger.warning("Legacy task migration failed for '%s': %s", text, exc)

    logger.info("Migrated %d legacy tasks for user %s", count, user_id)
    return count
