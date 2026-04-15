"""
db_schemas.py — Alfred Agentic Upgrade: Database Schemas & Migrations

Adds four new tables without touching existing ones:
  - tasks_v2          : Rich task schema with priority, status-machine, DAG parent links
  - memory_nodes      : Structured semantic memory units (entity, preference, fact, episode)
  - workflows         : Named state-machine workflow definitions
  - hitl_approvals    : Pending human-in-the-loop action approval tokens

Usage:
    from db_schemas import run_migrations
    await run_migrations()          # call from storage.init_storage_async()

All SQL is written to be idempotent (CREATE TABLE IF NOT EXISTS, ADD COLUMN IF NOT EXISTS).
Both SQLite and PostgreSQL dialects are handled via STORAGE_BACKEND check.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from datetime import timedelta

from pydantic import BaseModel, Field

logger = logging.getLogger("alfred.db_schemas")


# ─────────────────────────────────────────────────────────────────────────────
# Enumerations (shared by Pydantic models and DB layer)
# ─────────────────────────────────────────────────────────────────────────────

class TaskStatus(str, Enum):
    PENDING    = "pending"
    IN_PROGRESS = "in_progress"
    BLOCKED    = "blocked"
    DONE       = "done"
    CANCELLED  = "cancelled"

class TaskPriority(str, Enum):
    LOW    = "low"
    MEDIUM = "medium"
    HIGH   = "high"
    URGENT = "urgent"

class MemoryNodeType(str, Enum):
    ENTITY     = "entity"      # person / company / product
    PREFERENCE = "preference"  # user likes/dislikes
    FACT       = "fact"        # recalled factual statement
    EPISODE    = "episode"     # time-stamped event the user lived through

class WorkflowStatus(str, Enum):
    ACTIVE    = "active"
    PAUSED    = "paused"
    COMPLETED = "completed"
    FAILED    = "failed"

class HITLStatus(str, Enum):
    PENDING  = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED  = "expired"

class RiskLevel(str, Enum):
    LOW    = "low"
    MEDIUM = "medium"
    HIGH   = "high"


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic Models (used for validation throughout the app)
# ─────────────────────────────────────────────────────────────────────────────

class TaskV2(BaseModel):
    """Rich task schema replacing the flat tasks.json list."""
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    user_id: str
    text: str
    status: TaskStatus = TaskStatus.PENDING
    priority: TaskPriority = TaskPriority.MEDIUM
    # parent_id allows sub-tasks (DAG of tasks)
    parent_id: Optional[str] = None
    # workflow_id links this task into a larger workflow
    workflow_id: Optional[str] = None
    due_date: Optional[datetime] = None
    tags: List[str] = Field(default_factory=list)
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None

    class Config:
        use_enum_values = True


class MemoryNode(BaseModel):
    """A structured unit of long-term memory for a user."""
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    user_id: str
    node_type: MemoryNodeType
    # subject: e.g. "Rahul Sharma" for an entity node
    subject: str
    # content: the actual fact / preference / episode text
    content: str
    # confidence: how certain Alfred is this is still true (0.0–1.0)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    # source_message: the raw user text that produced this memory
    source_message: Optional[str] = None
    # embedding stored separately (bytes blob); populated by memory_retrieval.py
    embedding_key: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_accessed_at: Optional[datetime] = None
    # expires_at: None = permanent
    expires_at: Optional[datetime] = None

    class Config:
        use_enum_values = True


class WorkflowStep(BaseModel):
    """One step inside a Workflow (serialised to JSON in the DB)."""
    step_id: str
    name: str
    action_type: str              # matches an ActionType enum value
    params: Dict[str, Any] = Field(default_factory=dict)
    # depends_on: step_ids that must complete before this step
    depends_on: List[str] = Field(default_factory=list)
    status: str = "pending"       # pending / running / done / failed
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


class Workflow(BaseModel):
    """A named, multi-step workflow with state tracking."""
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    user_id: str
    name: str                     # e.g. "Outreach to Rahul Sharma"
    description: Optional[str] = None
    status: WorkflowStatus = WorkflowStatus.ACTIVE
    # steps: ordered list of WorkflowStep (stored as JSON blob)
    steps: List[WorkflowStep] = Field(default_factory=list)
    # current_step_id: which step is executing now
    current_step_id: Optional[str] = None
    context: Dict[str, Any] = Field(default_factory=dict)  # shared data between steps
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None

    class Config:
        use_enum_values = True


class HITLApproval(BaseModel):
    """
    A human-in-the-loop approval token.

    When Alfred wants to execute a high-risk action (e.g. send email, delete file),
    it creates a HITLApproval record and returns the token URL to the user.
    The frontend renders an Approve/Reject button.
    On approval, the stored action_payload is executed.
    """
    token: str = Field(default_factory=lambda: uuid.uuid4().hex)
    user_id: str
    action_type: str              # e.g. "EMAIL_SEND", "RUN_TERMINAL"
    action_payload: Dict[str, Any] = Field(default_factory=dict)
    risk_level: RiskLevel = RiskLevel.HIGH
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    # human-readable summary shown in the approval UI
    summary: str
    status: HITLStatus = HITLStatus.PENDING
    # workflow / plan context (optional)
    workflow_id: Optional[str] = None
    plan_id: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: datetime = Field(
        default_factory=lambda: datetime.utcnow() + timedelta(minutes=30)
    )
    resolved_at: Optional[datetime] = None
    resolved_by: Optional[str] = None  # "user" | "system_timeout"

    class Config:
        use_enum_values = True


# ─────────────────────────────────────────────────────────────────────────────
# SQL Migration Strings
# ─────────────────────────────────────────────────────────────────────────────

# SQLite-compatible DDL (also valid PostgreSQL for these simple types)
_MIGRATIONS_SQLITE: List[str] = [
    """
    CREATE TABLE IF NOT EXISTS tasks_v2 (
        id            TEXT PRIMARY KEY,
        user_id       TEXT NOT NULL,
        text          TEXT NOT NULL,
        status        TEXT NOT NULL DEFAULT 'pending',
        priority      TEXT NOT NULL DEFAULT 'medium',
        parent_id     TEXT REFERENCES tasks_v2(id) ON DELETE SET NULL,
        workflow_id   TEXT,
        due_date      TEXT,
        tags          TEXT DEFAULT '[]',
        notes         TEXT,
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL,
        completed_at  TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tasks_v2_user ON tasks_v2(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_v2_status ON tasks_v2(user_id, status)",
    """
    CREATE TABLE IF NOT EXISTS memory_nodes (
        id               TEXT PRIMARY KEY,
        user_id          TEXT NOT NULL,
        node_type        TEXT NOT NULL,
        subject          TEXT NOT NULL,
        content          TEXT NOT NULL,
        confidence       REAL NOT NULL DEFAULT 1.0,
        source_message   TEXT,
        embedding_key    TEXT,
        metadata         TEXT DEFAULT '{}',
        created_at       TEXT NOT NULL,
        last_accessed_at TEXT,
        expires_at       TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_memory_user ON memory_nodes(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_memory_type  ON memory_nodes(user_id, node_type)",
    "CREATE INDEX IF NOT EXISTS idx_memory_subj  ON memory_nodes(user_id, subject)",
    """
    CREATE TABLE IF NOT EXISTS workflows (
        id               TEXT PRIMARY KEY,
        user_id          TEXT NOT NULL,
        name             TEXT NOT NULL,
        description      TEXT,
        status           TEXT NOT NULL DEFAULT 'active',
        steps            TEXT NOT NULL DEFAULT '[]',
        current_step_id  TEXT,
        context          TEXT DEFAULT '{}',
        created_at       TEXT NOT NULL,
        updated_at       TEXT NOT NULL,
        completed_at     TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_workflows_user   ON workflows(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_workflows_status ON workflows(user_id, status)",
    """
    CREATE TABLE IF NOT EXISTS hitl_approvals (
        token          TEXT PRIMARY KEY,
        user_id        TEXT NOT NULL,
        action_type    TEXT NOT NULL,
        action_payload TEXT NOT NULL DEFAULT '{}',
        risk_level     TEXT NOT NULL DEFAULT 'high',
        confidence     REAL NOT NULL DEFAULT 0.0,
        summary        TEXT NOT NULL,
        status         TEXT NOT NULL DEFAULT 'pending',
        workflow_id    TEXT,
        plan_id        TEXT,
        created_at     TEXT NOT NULL,
        expires_at     TEXT NOT NULL,
        resolved_at    TEXT,
        resolved_by    TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_hitl_user   ON hitl_approvals(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_hitl_status ON hitl_approvals(user_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_hitl_token  ON hitl_approvals(token, status)",
]

# PostgreSQL overrides — only differences from SQLite DDL
_MIGRATIONS_POSTGRES: List[str] = [
    """
    CREATE TABLE IF NOT EXISTS tasks_v2 (
        id            TEXT PRIMARY KEY,
        user_id       TEXT NOT NULL,
        text          TEXT NOT NULL,
        status        TEXT NOT NULL DEFAULT 'pending',
        priority      TEXT NOT NULL DEFAULT 'medium',
        parent_id     TEXT REFERENCES tasks_v2(id) ON DELETE SET NULL,
        workflow_id   TEXT,
        due_date      TIMESTAMPTZ,
        tags          JSONB DEFAULT '[]',
        notes         TEXT,
        created_at    TIMESTAMPTZ NOT NULL,
        updated_at    TIMESTAMPTZ NOT NULL,
        completed_at  TIMESTAMPTZ
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tasks_v2_user ON tasks_v2(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_v2_status ON tasks_v2(user_id, status)",
    """
    CREATE TABLE IF NOT EXISTS memory_nodes (
        id               TEXT PRIMARY KEY,
        user_id          TEXT NOT NULL,
        node_type        TEXT NOT NULL,
        subject          TEXT NOT NULL,
        content          TEXT NOT NULL,
        confidence       FLOAT NOT NULL DEFAULT 1.0,
        source_message   TEXT,
        embedding_key    TEXT,
        metadata         JSONB DEFAULT '{}',
        created_at       TIMESTAMPTZ NOT NULL,
        last_accessed_at TIMESTAMPTZ,
        expires_at       TIMESTAMPTZ
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_memory_user ON memory_nodes(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_memory_type  ON memory_nodes(user_id, node_type)",
    "CREATE INDEX IF NOT EXISTS idx_memory_subj  ON memory_nodes(user_id, subject)",
    """
    CREATE TABLE IF NOT EXISTS workflows (
        id               TEXT PRIMARY KEY,
        user_id          TEXT NOT NULL,
        name             TEXT NOT NULL,
        description      TEXT,
        status           TEXT NOT NULL DEFAULT 'active',
        steps            JSONB NOT NULL DEFAULT '[]',
        current_step_id  TEXT,
        context          JSONB DEFAULT '{}',
        created_at       TIMESTAMPTZ NOT NULL,
        updated_at       TIMESTAMPTZ NOT NULL,
        completed_at     TIMESTAMPTZ
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_workflows_user   ON workflows(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_workflows_status ON workflows(user_id, status)",
    """
    CREATE TABLE IF NOT EXISTS hitl_approvals (
        token          TEXT PRIMARY KEY,
        user_id        TEXT NOT NULL,
        action_type    TEXT NOT NULL,
        action_payload JSONB NOT NULL DEFAULT '{}',
        risk_level     TEXT NOT NULL DEFAULT 'high',
        confidence     FLOAT NOT NULL DEFAULT 0.0,
        summary        TEXT NOT NULL,
        status         TEXT NOT NULL DEFAULT 'pending',
        workflow_id    TEXT,
        plan_id        TEXT,
        created_at     TIMESTAMPTZ NOT NULL,
        expires_at     TIMESTAMPTZ NOT NULL,
        resolved_at    TIMESTAMPTZ,
        resolved_by    TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_hitl_user   ON hitl_approvals(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_hitl_status ON hitl_approvals(user_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_hitl_token  ON hitl_approvals(token, status)",
]


# ─────────────────────────────────────────────────────────────────────────────
# Migration Runner
# ─────────────────────────────────────────────────────────────────────────────

async def run_migrations() -> None:
    """
    Idempotently create all new agentic tables.
    Call this from storage.init_storage_async() after the existing schema setup.
    """
    from storage import STORAGE_BACKEND, get_async_pool, _connect_sqlite, DB_PATH

    migrations = (
        _MIGRATIONS_POSTGRES if STORAGE_BACKEND == "postgres" else _MIGRATIONS_SQLITE
    )

    if STORAGE_BACKEND == "postgres":
        pool = await get_async_pool()
        if pool is None:
            logger.error("run_migrations: Postgres pool unavailable, skipping agentic migrations.")
            return
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                for sql in migrations:
                    stmt = sql.strip()
                    if not stmt:
                        continue
                    try:
                        await cur.execute(stmt)
                        logger.debug("Migration OK: %.60s…", stmt)
                    except Exception as exc:
                        logger.error("Migration failed (%s): %s", exc, stmt[:80])
                        raise
            await conn.commit()

    else:  # SQLite — run synchronously inside an executor to stay non-blocking
        import asyncio

        def _run_sqlite() -> None:
            conn = _connect_sqlite(DB_PATH)
            with conn:
                for sql in migrations:
                    stmt = sql.strip()
                    if not stmt:
                        continue
                    try:
                        conn.execute(stmt)
                        logger.debug("Migration OK: %.60s…", stmt)
                    except Exception as exc:
                        logger.error("Migration failed (%s): %s", exc, stmt[:80])
                        raise
            conn.close()

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _run_sqlite)

    logger.info("Alfred agentic DB migrations applied successfully.")


# ─────────────────────────────────────────────────────────────────────────────
# DAO helpers (thin async CRUD used by multi_action_parser & proactive_worker)
# ─────────────────────────────────────────────────────────────────────────────

async def insert_hitl_approval(approval: HITLApproval) -> None:
    """Persist a new HITL approval record."""
    from storage import STORAGE_BACKEND, get_async_pool, _connect_sqlite, DB_PATH
    import json, asyncio
    from datetime import timedelta

    # Default expires_at to 30 min from now if the model default factory produced a bad value
    if approval.expires_at <= approval.created_at:
        approval.expires_at = approval.created_at + timedelta(minutes=30)

    row = (
        approval.token,
        approval.user_id,
        approval.action_type,
        json.dumps(approval.action_payload),
        approval.risk_level,
        approval.confidence,
        approval.summary,
        approval.status,
        approval.workflow_id,
        approval.plan_id,
        approval.created_at.isoformat(),
        approval.expires_at.isoformat(),
    )

    sql = """
        INSERT INTO hitl_approvals
            (token, user_id, action_type, action_payload, risk_level, confidence,
             summary, status, workflow_id, plan_id, created_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    pg_sql = sql.replace("?", "%s")

    if STORAGE_BACKEND == "postgres":
        pool = await get_async_pool()
        async with pool.connection() as conn:
            await conn.execute(pg_sql, row)
            await conn.commit()
    else:
        def _write():
            conn = _connect_sqlite(DB_PATH)
            with conn:
                conn.execute(sql, row)
            conn.close()
        await asyncio.get_event_loop().run_in_executor(None, _write)


async def get_hitl_approval(token: str) -> Optional[HITLApproval]:
    """Fetch a HITL approval by token."""
    from storage import STORAGE_BACKEND, get_async_pool, _connect_sqlite, DB_PATH
    import json, asyncio

    sql = "SELECT * FROM hitl_approvals WHERE token = ?"
    pg_sql = "SELECT * FROM hitl_approvals WHERE token = %s"

    if STORAGE_BACKEND == "postgres":
        pool = await get_async_pool()
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(pg_sql, (token,))
                row = await cur.fetchone()
    else:
        def _read():
            conn = _connect_sqlite(DB_PATH)
            cur = conn.execute(sql, (token,))
            r = cur.fetchone()
            conn.close()
            return dict(r) if r else None
        row = await asyncio.get_event_loop().run_in_executor(None, _read)

    if not row:
        return None

    return HITLApproval(
        token=row["token"],
        user_id=row["user_id"],
        action_type=row["action_type"],
        action_payload=json.loads(row["action_payload"]) if isinstance(row["action_payload"], str) else row["action_payload"],
        risk_level=row["risk_level"],
        confidence=row["confidence"],
        summary=row["summary"],
        status=row["status"],
        workflow_id=row.get("workflow_id"),
        plan_id=row.get("plan_id"),
        created_at=datetime.fromisoformat(row["created_at"]) if isinstance(row["created_at"], str) else row["created_at"],
        expires_at=datetime.fromisoformat(row["expires_at"]) if isinstance(row["expires_at"], str) else row["expires_at"],
        resolved_at=datetime.fromisoformat(row["resolved_at"]) if row.get("resolved_at") else None,
        resolved_by=row.get("resolved_by"),
    )


async def resolve_hitl_approval(token: str, decision: str, resolved_by: str = "user") -> bool:
    """
    Set a HITL approval's status to 'approved' or 'rejected'.
    Returns True if the record existed and was pending, False otherwise.
    """
    from storage import STORAGE_BACKEND, get_async_pool, _connect_sqlite, DB_PATH
    import asyncio

    now_iso = datetime.utcnow().isoformat()
    sql = """
        UPDATE hitl_approvals
        SET status = ?, resolved_at = ?, resolved_by = ?
        WHERE token = ? AND status = 'pending'
    """
    pg_sql = sql.replace("?", "%s")
    params = (decision, now_iso, resolved_by, token)

    if STORAGE_BACKEND == "postgres":
        pool = await get_async_pool()
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(pg_sql, params)
                affected = cur.rowcount
            await conn.commit()
        return affected > 0
    else:
        def _update():
            conn = _connect_sqlite(DB_PATH)
            with conn:
                cur = conn.execute(sql, params)
                affected = cur.rowcount
            conn.close()
            return affected > 0
        return await asyncio.get_event_loop().run_in_executor(None, _update)
