"""
memory_retrieval.py — Alfred Hybrid Memory System

Full memory loop:
  1. Extract  — regex + pattern matching pulls facts/prefs/entities from each message
  2. Store    — writes MemoryNode records to memory_nodes table
  3. Retrieve — SQLite FTS5 (or PostgreSQL tsvector) ranks relevant nodes per request
  4. Inject   — formats top-N nodes into system prompt context
  5. Decay    — confidence degrades on old, unaccessed nodes

Called from alfred_core.chat():
  - BEFORE LLM call : await inject_memory_context_async(user_id, user_message)
  - AFTER LLM reply : asyncio.ensure_future(extract_and_store_async(user_msg, reply, user_id))
  - Nightly (via proactive_worker) : await decay_memories_async(user_id)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from db_schemas import MemoryNode, MemoryNodeType

logger = logging.getLogger("alfred.memory_retrieval")

# Maximum nodes injected into system prompt per request
MAX_INJECT_NODES = 8
# Confidence below which nodes are deleted during decay
DECAY_DELETE_THRESHOLD = 0.15
# Confidence decay rate per day of non-access
DECAY_RATE_PER_DAY = 0.05


# ─────────────────────────────────────────────────────────────────────────────
# FTS5 / Full-text search init
# ─────────────────────────────────────────────────────────────────────────────

_FTS_INITIALIZED: bool = False

async def init_memory_fts() -> None:
    """
    Create the FTS5 virtual table (SQLite) or GIN index (PostgreSQL).
    Idempotent — safe to call multiple times.
    """
    global _FTS_INITIALIZED
    if _FTS_INITIALIZED:
        return

    from storage import STORAGE_BACKEND, get_async_pool, _connect_sqlite, DB_PATH

    if STORAGE_BACKEND == "postgres":
        pool = await get_async_pool()
        if pool:
            async with pool.connection() as conn:
                # PostgreSQL: add tsvector search index on memory_nodes
                await conn.execute("""
                    ALTER TABLE memory_nodes
                    ADD COLUMN IF NOT EXISTS search_vector tsvector
                    GENERATED ALWAYS AS (
                        to_tsvector('english', coalesce(subject,'') || ' ' || coalesce(content,''))
                    ) STORED
                """)
                await conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_memory_fts
                    ON memory_nodes USING GIN(search_vector)
                """)
                await conn.commit()
    else:
        def _create_fts():
            conn = _connect_sqlite(DB_PATH)
            with conn:
                # FTS5 virtual table for memory content
                conn.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS memory_nodes_fts
                    USING fts5(
                        node_id,
                        user_id UNINDEXED,
                        subject,
                        content,
                        node_type UNINDEXED,
                        tokenize='porter ascii'
                    )
                """)
                # Triggers to keep FTS in sync with memory_nodes
                conn.execute("""
                    CREATE TRIGGER IF NOT EXISTS memory_nodes_ai
                    AFTER INSERT ON memory_nodes BEGIN
                        INSERT INTO memory_nodes_fts(node_id, user_id, subject, content, node_type)
                        VALUES (new.id, new.user_id, new.subject, new.content, new.node_type);
                    END
                """)
                conn.execute("""
                    CREATE TRIGGER IF NOT EXISTS memory_nodes_ad
                    AFTER DELETE ON memory_nodes BEGIN
                        DELETE FROM memory_nodes_fts WHERE node_id = old.id;
                    END
                """)
                conn.execute("""
                    CREATE TRIGGER IF NOT EXISTS memory_nodes_au
                    AFTER UPDATE ON memory_nodes BEGIN
                        DELETE FROM memory_nodes_fts WHERE node_id = old.id;
                        INSERT INTO memory_nodes_fts(node_id, user_id, subject, content, node_type)
                        VALUES (new.id, new.user_id, new.subject, new.content, new.node_type);
                    END
                """)
            conn.close()

        await asyncio.get_event_loop().run_in_executor(None, _create_fts)

    _FTS_INITIALIZED = True
    logger.info("Memory FTS index initialised.")


# ─────────────────────────────────────────────────────────────────────────────
# Storage helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _insert_node(node: MemoryNode) -> None:
    from storage import STORAGE_BACKEND, get_async_pool, _connect_sqlite, DB_PATH

    row = (
        node.id, node.user_id, node.node_type, node.subject, node.content,
        node.confidence, node.source_message,
        json.dumps(node.metadata),
        node.created_at.isoformat(),
    )
    sql = """
        INSERT OR REPLACE INTO memory_nodes
            (id, user_id, node_type, subject, content, confidence,
             source_message, metadata, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    pg_sql = """
        INSERT INTO memory_nodes
            (id, user_id, node_type, subject, content, confidence,
             source_message, metadata, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT(id) DO UPDATE
          SET content=EXCLUDED.content, confidence=EXCLUDED.confidence,
              metadata=EXCLUDED.metadata
    """

    if STORAGE_BACKEND == "postgres":
        pool = await get_async_pool()
        async with pool.connection() as conn:
            await conn.execute(pg_sql, row)
            await conn.commit()
    else:
        def _w():
            c = _connect_sqlite(DB_PATH)
            with c:
                c.execute(sql, row)
            c.close()
        await asyncio.get_event_loop().run_in_executor(None, _w)


async def _search_nodes_fts(user_id: str, query: str, limit: int = MAX_INJECT_NODES) -> List[Dict]:
    """Full-text search using FTS5 (SQLite) or tsvector (Postgres)."""
    from storage import STORAGE_BACKEND, get_async_pool, _connect_sqlite, DB_PATH

    if not query.strip():
        return []

    if STORAGE_BACKEND == "postgres":
        pool = await get_async_pool()
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    SELECT id, node_type, subject, content, confidence, last_accessed_at
                    FROM memory_nodes
                    WHERE user_id = %s
                      AND (expires_at IS NULL OR expires_at > NOW())
                      AND search_vector @@ plainto_tsquery('english', %s)
                    ORDER BY ts_rank(search_vector, plainto_tsquery('english', %s)) DESC,
                             confidence DESC
                    LIMIT %s
                """, (user_id, query, query, limit))
                rows = await cur.fetchall()
                return [dict(r) for r in rows]
    else:
        # Sanitize FTS query (FTS5 uses its own syntax)
        safe_query = " ".join(
            f'"{w}"' for w in re.findall(r"\w+", query)[:8] if len(w) > 2
        )
        if not safe_query:
            return []

        def _q():
            c = _connect_sqlite(DB_PATH)
            try:
                # FTS5 search
                rows = c.execute("""
                    SELECT m.id, m.node_type, m.subject, m.content,
                           m.confidence, m.last_accessed_at,
                           fts.rank
                    FROM memory_nodes_fts fts
                    JOIN memory_nodes m ON m.id = fts.node_id
                    WHERE fts.user_id = ?
                      AND fts.memory_nodes_fts MATCH ?
                      AND (m.expires_at IS NULL OR m.expires_at > ?)
                    ORDER BY fts.rank, m.confidence DESC
                    LIMIT ?
                """, (user_id, safe_query, datetime.now().isoformat(), limit)).fetchall()
                return [dict(r) for r in rows]
            except Exception as exc:
                # FTS table may not be populated yet — fallback to LIKE
                logger.debug("FTS search fell back to LIKE: %s", exc)
                words = re.findall(r"\w+", query)[:4]
                if not words:
                    return []
                conditions = " OR ".join(["(subject LIKE ? OR content LIKE ?)"] * len(words))
                params = [f"%{w}%" for w in words for _ in range(2)]
                params = [user_id] + params + [datetime.now().isoformat(), limit]
                rows = c.execute(f"""
                    SELECT id, node_type, subject, content, confidence, last_accessed_at
                    FROM memory_nodes
                    WHERE user_id = ? AND ({conditions})
                      AND (expires_at IS NULL OR expires_at > ?)
                    ORDER BY confidence DESC
                    LIMIT ?
                """, params).fetchall()
                return [dict(r) for r in rows]
            finally:
                c.close()

        return await asyncio.get_event_loop().run_in_executor(None, _q)


async def _touch_nodes(user_id: str, node_ids: List[str]) -> None:
    """Update last_accessed_at for recently retrieved nodes."""
    if not node_ids:
        return
    from storage import STORAGE_BACKEND, get_async_pool, _connect_sqlite, DB_PATH
    now = datetime.now().isoformat()

    if STORAGE_BACKEND == "postgres":
        pool = await get_async_pool()
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE memory_nodes SET last_accessed_at = %s WHERE id = ANY(%s) AND user_id = %s",
                (now, node_ids, user_id),
            )
            await conn.commit()
    else:
        placeholders = ",".join("?" * len(node_ids))
        def _u():
            c = _connect_sqlite(DB_PATH)
            with c:
                c.execute(
                    f"UPDATE memory_nodes SET last_accessed_at = ? WHERE id IN ({placeholders}) AND user_id = ?",
                    [now] + node_ids + [user_id],
                )
            c.close()
        await asyncio.get_event_loop().run_in_executor(None, _u)


# ─────────────────────────────────────────────────────────────────────────────
# Extraction patterns
# ─────────────────────────────────────────────────────────────────────────────

# Each pattern: (regex, node_type, subject_fn, content_fn, confidence)
# subject_fn / content_fn: callable(match) -> str
_PATTERNS: List[Tuple] = [
    # Name
    (re.compile(r"\bmy name is ([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)", re.I),
     MemoryNodeType.ENTITY, lambda m: "user_name", lambda m: f"User's name is {m.group(1).title()}", 0.95),

    (re.compile(r"\bcall me ([A-Z][a-z]+)", re.I),
     MemoryNodeType.ENTITY, lambda m: "user_name", lambda m: f"User goes by {m.group(1).title()}", 0.90),

    # Company / employer
    (re.compile(r"\bI(?:'m| am) (?:at|from|with) ([A-Z][A-Za-z0-9\s&]+?)(?:\.|,|$)", re.I),
     MemoryNodeType.ENTITY, lambda m: "user_company", lambda m: f"User is at/from {m.group(1).strip()}", 0.80),

    (re.compile(r"\bI work(?:ing)? (?:at|for|with) ([A-Z][A-Za-z0-9\s&]+?)(?:\.|,|!|\?|$)", re.I),
     MemoryNodeType.ENTITY, lambda m: "user_company", lambda m: f"User works at {m.group(1).strip()}", 0.85),

    # Role / job title
    (re.compile(r"\bI(?:'m| am) (?:a |an )?([A-Za-z]+(?:\s+[A-Za-z]+)?)\b(?=.*(?:engineer|founder|developer|designer|manager|analyst|executive|officer|director|student|doctor|lawyer|consultant|freelancer))", re.I),
     MemoryNodeType.ENTITY, lambda m: "user_role", lambda m: f"User is a {m.group(1)}", 0.80),

    (re.compile(r"\bI(?:'m| am) a(?:n)? (founder|ceo|cto|coo|engineer|developer|designer|manager|analyst|doctor|lawyer|student|freelancer|consultant|entrepreneur)", re.I),
     MemoryNodeType.ENTITY, lambda m: "user_role", lambda m: f"User is a {m.group(1).lower()}", 0.90),

    # Location
    (re.compile(r"\bI(?:'m| am) (?:in|based in|living in|from) ([A-Z][a-zA-Z\s,]+?)(?:\.|,|!|\?|$)", re.I),
     MemoryNodeType.ENTITY, lambda m: "user_location", lambda m: f"User is in {m.group(1).strip()}", 0.80),

    # Preferences — positive
    (re.compile(r"\bI (?:really )?(like|love|enjoy|prefer|am into) (.{5,80}?)(?:\.|,|!|\?|$)", re.I),
     MemoryNodeType.PREFERENCE, lambda m: f"preference_{m.group(2)[:20].strip().lower().replace(' ','_')}", lambda m: f"User {m.group(1)}s {m.group(2).strip()}", 0.85),

    # Preferences — negative
    (re.compile(r"\bI (?:really )?(hate|dislike|don'?t like|can'?t stand|avoid) (.{5,80}?)(?:\.|,|!|\?|$)", re.I),
     MemoryNodeType.PREFERENCE, lambda m: f"dislike_{m.group(2)[:20].strip().lower().replace(' ','_')}", lambda m: f"User dislikes {m.group(2).strip()}", 0.85),

    # Goals
    (re.compile(r"\bI(?:'m| am) (?:trying|working|planning|hoping) to (.{5,100}?)(?:\.|,|!|\?|$)", re.I),
     MemoryNodeType.FACT, lambda m: f"goal_{m.group(1)[:25].strip().lower().replace(' ','_')}", lambda m: f"User's goal: {m.group(1).strip()}", 0.75),

    (re.compile(r"\bmy goal is (?:to )?(.{5,100}?)(?:\.|,|!|\?|$)", re.I),
     MemoryNodeType.FACT, lambda m: f"goal_{m.group(1)[:25].strip().lower().replace(' ','_')}", lambda m: f"User's goal: {m.group(1).strip()}", 0.80),
]


def _extract_nodes_from_text(
    text: str, user_id: str, source_message: str
) -> List[MemoryNode]:
    """Run all patterns against a text, return candidate MemoryNode list."""
    nodes: List[MemoryNode] = []
    seen_subjects: set = set()

    for pattern, node_type, subject_fn, content_fn, confidence in _PATTERNS:
        for match in pattern.finditer(text):
            try:
                subject = subject_fn(match)[:80]
                content = content_fn(match)[:400]
                if not content or subject in seen_subjects:
                    continue
                seen_subjects.add(subject)
                nodes.append(MemoryNode(
                    id=uuid.uuid4().hex[:16],
                    user_id=user_id,
                    node_type=node_type,
                    subject=subject,
                    content=content,
                    confidence=confidence,
                    source_message=source_message[:200],
                    created_at=datetime.now(),
                ))
            except Exception:
                continue

    return nodes


# ─────────────────────────────────────────────────────────────────────────────
# Deduplication before writing
# ─────────────────────────────────────────────────────────────────────────────

async def _get_existing_subjects(user_id: str) -> Dict[str, str]:
    """Return {subject → node_id} for the user's existing memories."""
    from storage import STORAGE_BACKEND, get_async_pool, _connect_sqlite, DB_PATH

    if STORAGE_BACKEND == "postgres":
        pool = await get_async_pool()
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT subject, id FROM memory_nodes WHERE user_id = %s",
                    (user_id,),
                )
                rows = await cur.fetchall()
                return {r["subject"]: r["id"] for r in rows}
    else:
        def _q():
            c = _connect_sqlite(DB_PATH)
            rows = c.execute(
                "SELECT subject, id FROM memory_nodes WHERE user_id = ?", (user_id,)
            ).fetchall()
            c.close()
            return {r["subject"]: r["id"] for r in rows}
        return await asyncio.get_event_loop().run_in_executor(None, _q)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

async def extract_and_store_async(
    user_message: str,
    assistant_reply: str,
    user_id: str,
) -> int:
    """
    Extract MemoryNodes from the user message and (lightly) from the reply.
    Skips duplicates. Returns count of new nodes stored.
    """
    candidate_text = user_message + " " + assistant_reply
    candidates = _extract_nodes_from_text(candidate_text, user_id, user_message[:200])
    if not candidates:
        return 0

    existing = await _get_existing_subjects(user_id)
    stored = 0

    for node in candidates:
        if node.subject in existing:
            # Subject already known — skip (future: could update confidence)
            continue
        try:
            await _insert_node(node)
            existing[node.subject] = node.id
            stored += 1
        except Exception as exc:
            logger.warning("Failed to store memory node '%s': %s", node.subject, exc)

    if stored:
        logger.info("Stored %d new memory node(s) for user %s", stored, user_id)
    return stored


async def search_memories_async(
    user_id: str,
    query: str,
    limit: int = MAX_INJECT_NODES,
) -> List[MemoryNode]:
    """
    Search memory_nodes for nodes relevant to the query.
    Returns up to `limit` MemoryNode objects, ordered by relevance.
    """
    rows = await _search_nodes_fts(user_id, query, limit)
    if not rows:
        return []

    nodes = []
    for r in rows:
        nodes.append(MemoryNode(
            id=r["id"],
            user_id=user_id,
            node_type=r["node_type"],
            subject=r["subject"],
            content=r["content"],
            confidence=float(r.get("confidence", 1.0)),
            created_at=datetime.now(),
            last_accessed_at=datetime.fromisoformat(r["last_accessed_at"])
            if r.get("last_accessed_at") else None,
        ))

    # Update access timestamps asynchronously
    asyncio.ensure_future(_touch_nodes(user_id, [n.id for n in nodes]))
    return nodes


async def inject_memory_context_async(
    user_id: str,
    query: str,
) -> str:
    """
    Build a system-prompt section from relevant memories.
    Returns an empty string if no relevant memories exist.
    """
    try:
        nodes = await search_memories_async(user_id, query)
    except Exception as exc:
        logger.warning("Memory retrieval failed: %s", exc)
        return ""

    if not nodes:
        return ""

    lines = ["Recalled from memory (use as context, don't announce):"]
    for n in nodes:
        marker = {
            MemoryNodeType.PREFERENCE: "pref",
            MemoryNodeType.ENTITY: "fact",
            MemoryNodeType.FACT: "fact",
            MemoryNodeType.EPISODE: "past",
        }.get(n.node_type, "note")
        lines.append(f"  [{marker}] {n.content}")

    return "\n".join(lines)


async def decay_memories_async(user_id: str) -> int:
    """
    Reduce confidence of memories that haven't been accessed recently.
    Deletes nodes that fall below DECAY_DELETE_THRESHOLD.
    Called nightly by the proactive worker.
    Returns count of deleted nodes.
    """
    from storage import STORAGE_BACKEND, get_async_pool, _connect_sqlite, DB_PATH
    now = datetime.now()
    deleted = 0

    if STORAGE_BACKEND == "postgres":
        pool = await get_async_pool()
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                # Decay: reduce confidence for nodes not accessed in >7 days
                cutoff = (now - timedelta(days=7)).isoformat()
                await cur.execute("""
                    UPDATE memory_nodes
                    SET confidence = GREATEST(confidence - %s, 0)
                    WHERE user_id = %s
                      AND (last_accessed_at IS NULL OR last_accessed_at < %s)
                      AND node_type NOT IN ('entity')
                """, (DECAY_RATE_PER_DAY, user_id, cutoff))

                # Delete below threshold
                await cur.execute("""
                    DELETE FROM memory_nodes
                    WHERE user_id = %s AND confidence <= %s
                    AND node_type NOT IN ('entity')
                """, (user_id, DECAY_DELETE_THRESHOLD))
                deleted = cur.rowcount
            await conn.commit()
    else:
        cutoff = (now - timedelta(days=7)).isoformat()
        def _decay():
            c = _connect_sqlite(DB_PATH)
            with c:
                c.execute("""
                    UPDATE memory_nodes
                    SET confidence = MAX(confidence - ?, 0)
                    WHERE user_id = ?
                      AND (last_accessed_at IS NULL OR last_accessed_at < ?)
                      AND node_type NOT IN ('entity')
                """, (DECAY_RATE_PER_DAY, user_id, cutoff))
                cur = c.execute("""
                    DELETE FROM memory_nodes
                    WHERE user_id = ? AND confidence <= ?
                    AND node_type NOT IN ('entity')
                """, (user_id, DECAY_DELETE_THRESHOLD))
                return cur.rowcount
            c.close()
        deleted = await asyncio.get_event_loop().run_in_executor(None, _decay)

    if deleted:
        logger.info("Decayed %d low-confidence memory nodes for user %s", deleted, user_id)
    return deleted


async def get_all_memories_async(user_id: str) -> List[MemoryNode]:
    """Return all memory nodes for a user (used by dashboard Memory tab)."""
    from storage import STORAGE_BACKEND, get_async_pool, _connect_sqlite, DB_PATH

    if STORAGE_BACKEND == "postgres":
        pool = await get_async_pool()
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT * FROM memory_nodes WHERE user_id = %s ORDER BY confidence DESC, created_at DESC",
                    (user_id,),
                )
                rows = await cur.fetchall()
    else:
        def _q():
            c = _connect_sqlite(DB_PATH)
            rows = c.execute(
                "SELECT * FROM memory_nodes WHERE user_id = ? ORDER BY confidence DESC, created_at DESC",
                (user_id,),
            ).fetchall()
            c.close()
            return [dict(r) for r in rows]
        rows = await asyncio.get_event_loop().run_in_executor(None, _q)

    result = []
    for r in rows:
        try:
            result.append(MemoryNode(
                id=r["id"], user_id=user_id,
                node_type=r["node_type"], subject=r["subject"],
                content=r["content"], confidence=float(r.get("confidence", 1.0)),
                source_message=r.get("source_message"),
                metadata=json.loads(r["metadata"]) if isinstance(r.get("metadata"), str) else (r.get("metadata") or {}),
                created_at=datetime.fromisoformat(r["created_at"]) if isinstance(r.get("created_at"), str) else datetime.now(),
            ))
        except Exception:
            continue
    return result
