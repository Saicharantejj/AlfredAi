"""
workflow_engine.py — Alfred Workflow State Machine

Manages named, multi-step workflows that:
  - Persist state across sessions (workflows table)
  - Resume where they left off on restart
  - Track per-step success/failure
  - Surface active workflow context into LLM system prompt

A Workflow is a DAG of WorkflowSteps. Each step maps to an ActionType that
the multi_action_parser already knows how to execute.

Example — "Outreach to Investor" workflow:
  s0: WEB_SEARCH    {"query": "{{name}} LinkedIn"}
  s1: WEB_SEARCH    {"query": "{{name}} recent news"}       depends_on: []
  s2: EMAIL_SEND    {"to": "...", "body": "{{s0.result}} {{s1.result}}"}  depends_on: [s0, s1]
  s3: SAVE_NOTE     {"content": "Outreach sent to {{name}} on {{date}}"}  depends_on: [s2]

Lifecycle:
  ACTIVE → (all steps done) → COMPLETED
  ACTIVE → (critical step fails) → FAILED
  ACTIVE → (user pauses) → PAUSED → ACTIVE
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from db_schemas import Workflow, WorkflowStatus, WorkflowStep

logger = logging.getLogger("alfred.workflow_engine")


# ─────────────────────────────────────────────────────────────────────────────
# Storage helpers
# ─────────────────────────────────────────────────────────────────────────────

def _serialise_workflow(wf: Workflow) -> tuple:
    now = datetime.now().isoformat()
    return (
        wf.id, wf.user_id, wf.name, wf.description, wf.status,
        json.dumps([s.model_dump() for s in wf.steps]),
        wf.current_step_id,
        json.dumps(wf.context),
        now, now,
    )


def _row_to_workflow(row: Dict) -> Workflow:
    steps_raw = row.get("steps", "[]")
    steps = [WorkflowStep(**s) for s in (json.loads(steps_raw) if isinstance(steps_raw, str) else steps_raw)]
    ctx_raw = row.get("context", "{}")
    ctx = json.loads(ctx_raw) if isinstance(ctx_raw, str) else (ctx_raw or {})
    return Workflow(
        id=row["id"],
        user_id=row["user_id"],
        name=row["name"],
        description=row.get("description"),
        status=row.get("status", WorkflowStatus.ACTIVE),
        steps=steps,
        current_step_id=row.get("current_step_id"),
        context=ctx,
        created_at=datetime.fromisoformat(row["created_at"]) if isinstance(row.get("created_at"), str) else datetime.now(),
        updated_at=datetime.fromisoformat(row["updated_at"]) if isinstance(row.get("updated_at"), str) else datetime.now(),
        completed_at=datetime.fromisoformat(row["completed_at"]) if row.get("completed_at") else None,
    )


async def _save_workflow(wf: Workflow) -> None:
    from storage import STORAGE_BACKEND, get_async_pool, _connect_sqlite, DB_PATH

    now = datetime.now().isoformat()
    sql = """
        INSERT OR REPLACE INTO workflows
            (id, user_id, name, description, status, steps, current_step_id, context, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    pg_sql = """
        INSERT INTO workflows
            (id, user_id, name, description, status, steps, current_step_id, context, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT(id) DO UPDATE SET
            status=EXCLUDED.status, steps=EXCLUDED.steps,
            current_step_id=EXCLUDED.current_step_id,
            context=EXCLUDED.context, updated_at=EXCLUDED.updated_at
    """
    params = (
        wf.id, wf.user_id, wf.name, wf.description, wf.status,
        json.dumps([s.model_dump() for s in wf.steps]),
        wf.current_step_id,
        json.dumps(wf.context),
        wf.created_at.isoformat(),
        now,
    )

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


async def _load_workflows(user_id: str, status: Optional[str] = None) -> List[Workflow]:
    from storage import STORAGE_BACKEND, get_async_pool, _connect_sqlite, DB_PATH

    if status:
        sql = "SELECT * FROM workflows WHERE user_id = ? AND status = ? ORDER BY created_at DESC"
        pg_sql = "SELECT * FROM workflows WHERE user_id = %s AND status = %s ORDER BY created_at DESC"
        params = (user_id, status)
    else:
        sql = "SELECT * FROM workflows WHERE user_id = ? ORDER BY created_at DESC"
        pg_sql = "SELECT * FROM workflows WHERE user_id = %s ORDER BY created_at DESC"
        params = (user_id,)

    if STORAGE_BACKEND == "postgres":
        pool = await get_async_pool()
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(pg_sql, params)
                rows = await cur.fetchall()
                return [_row_to_workflow(dict(r)) for r in rows]
    else:
        def _q():
            c = _connect_sqlite(DB_PATH)
            rows = c.execute(sql, params).fetchall()
            c.close()
            return [dict(r) for r in rows]
        rows = await asyncio.get_event_loop().run_in_executor(None, _q)
        return [_row_to_workflow(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

async def create_workflow_async(wf: Workflow) -> str:
    """Persist a new workflow. Returns workflow id."""
    if not wf.id:
        wf.id = uuid.uuid4().hex[:16]
    if not wf.created_at:
        wf.created_at = datetime.now()
    await _save_workflow(wf)
    logger.info("Workflow created: '%s' (id=%s, steps=%d)", wf.name, wf.id, len(wf.steps))
    return wf.id


async def get_workflow_async(wf_id: str, user_id: str) -> Optional[Workflow]:
    from storage import STORAGE_BACKEND, get_async_pool, _connect_sqlite, DB_PATH
    sql = "SELECT * FROM workflows WHERE id = ? AND user_id = ?"
    pg_sql = "SELECT * FROM workflows WHERE id = %s AND user_id = %s"

    if STORAGE_BACKEND == "postgres":
        pool = await get_async_pool()
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(pg_sql, (wf_id, user_id))
                row = await cur.fetchone()
                return _row_to_workflow(dict(row)) if row else None
    else:
        def _q():
            c = _connect_sqlite(DB_PATH)
            row = c.execute(sql, (wf_id, user_id)).fetchone()
            c.close()
            return dict(row) if row else None
        row = await asyncio.get_event_loop().run_in_executor(None, _q)
        return _row_to_workflow(row) if row else None


async def advance_workflow_async(
    wf_id: str,
    user_id: str,
    session_id: str,
) -> Dict[str, Any]:
    """
    Execute the next pending step(s) in a workflow.
    Returns a summary dict: {"advanced": bool, "step_results": [...], "status": str}
    """
    wf = await get_workflow_async(wf_id, user_id)
    if not wf:
        return {"advanced": False, "error": "Workflow not found."}
    if wf.status in (WorkflowStatus.COMPLETED, WorkflowStatus.FAILED):
        return {"advanced": False, "status": wf.status, "error": "Workflow is already finished."}

    # Build step index
    step_map: Dict[str, WorkflowStep] = {s.step_id: s for s in wf.steps}
    context = dict(wf.context)  # shared data store across steps

    # Template injection: replace {{var}} in step params from context
    def _inject(params: Dict, ctx: Dict) -> Dict:
        result = {}
        for k, v in params.items():
            if isinstance(v, str):
                import re
                def _rep(m):
                    return str(ctx.get(m.group(1), f"[{m.group(1)} not available]"))
                result[k] = re.sub(r"\{\{(\w+)\}\}", _rep, v)
            else:
                result[k] = v
        return result

    # Find executable steps (pending + all deps done)
    def _is_ready(step: WorkflowStep) -> bool:
        if step.status != "pending":
            return False
        return all(step_map[dep].status == "done" for dep in step.depends_on if dep in step_map)

    ready_steps = [s for s in wf.steps if _is_ready(s)]
    if not ready_steps:
        # Check if workflow is complete
        all_done = all(s.status in ("done", "skipped") for s in wf.steps)
        if all_done:
            wf.status = WorkflowStatus.COMPLETED
            wf.completed_at = datetime.now()
            await _save_workflow(wf)
            return {"advanced": False, "status": WorkflowStatus.COMPLETED, "message": "Workflow complete."}
        return {"advanced": False, "status": wf.status, "message": "No ready steps (dependencies pending)."}

    # Import execution machinery
    from multi_action_parser import _EXECUTOR_REGISTRY, ExecutionContext, ActionNode, _needs_hitl
    from db_schemas import HITLApproval, insert_hitl_approval, RiskLevel

    exec_ctx = ExecutionContext(user_id=user_id, session_id=session_id)
    # Pre-populate context results so template injection works
    for step in wf.steps:
        if step.result and step.status == "done":
            result_str = step.result.get("output", "") if isinstance(step.result, dict) else str(step.result)
            exec_ctx.store_result(step.step_id, result_str)

    step_results = []
    hitl_tokens = []

    for step in ready_steps:
        injected_params = _inject(step.params, context)
        # Also inject from exec_ctx (step results from this run)
        injected_params = exec_ctx.inject_templates(injected_params)

        step.status = "running"
        wf.current_step_id = step.step_id
        await _save_workflow(wf)

        # HITL check
        node = ActionNode(id=step.step_id, type=step.action_type, params=injected_params)
        if _needs_hitl(node):
            from db_schemas import HITLApproval, insert_hitl_approval, RiskLevel
            approval = HITLApproval(
                user_id=user_id,
                action_type=step.action_type,
                action_payload=injected_params,
                risk_level=node.risk_level,
                confidence=node.confidence,
                summary=f"[Workflow: {wf.name}] Step '{step.name}': {step.action_type}",
                workflow_id=wf.id,
            )
            await insert_hitl_approval(approval)
            step.status = "pending"  # reset — will re-run after approval
            hitl_tokens.append({"token": approval.token, "step": step.name})
            step_results.append({"step": step.step_id, "status": "hitl_pending", "token": approval.token})
            continue

        executor = _EXECUTOR_REGISTRY.get(step.action_type)
        if not executor:
            step.status = "failed"
            step.error = f"No executor for action type: {step.action_type}"
            step_results.append({"step": step.step_id, "status": "failed", "error": step.error})
            continue

        try:
            output = await executor(injected_params, exec_ctx)
            step.status = "done"
            step.result = {"output": output}
            exec_ctx.store_result(step.step_id, output)
            # Store result in shared context for later steps
            context[step.step_id] = output
            context[f"{step.step_id}_result"] = output
            step_results.append({"step": step.step_id, "status": "done", "result": output[:200]})
            logger.info("Workflow %s step %s (%s) done.", wf.id, step.step_id, step.action_type)
        except Exception as exc:
            step.status = "failed"
            step.error = str(exc)
            step_results.append({"step": step.step_id, "status": "failed", "error": str(exc)})
            logger.error("Workflow %s step %s failed: %s", wf.id, step.step_id, exc)

    # Update workflow context and re-evaluate overall status
    wf.context = context
    all_terminal = all(s.status in ("done", "failed", "skipped") for s in wf.steps)
    any_failed = any(s.status == "failed" for s in wf.steps)

    if all_terminal:
        wf.status = WorkflowStatus.FAILED if any_failed else WorkflowStatus.COMPLETED
        if wf.status == WorkflowStatus.COMPLETED:
            wf.completed_at = datetime.now()

    await _save_workflow(wf)
    return {
        "advanced": True,
        "status": wf.status,
        "step_results": step_results,
        "hitl_pending": hitl_tokens,
    }


async def pause_workflow_async(wf_id: str, user_id: str) -> bool:
    wf = await get_workflow_async(wf_id, user_id)
    if not wf or wf.status != WorkflowStatus.ACTIVE:
        return False
    wf.status = WorkflowStatus.PAUSED
    await _save_workflow(wf)
    return True


async def resume_workflow_async(wf_id: str, user_id: str) -> bool:
    wf = await get_workflow_async(wf_id, user_id)
    if not wf or wf.status != WorkflowStatus.PAUSED:
        return False
    wf.status = WorkflowStatus.ACTIVE
    await _save_workflow(wf)
    return True


async def resume_all_active_workflows_async(user_id: str, session_id: str) -> int:
    """
    On startup: advance any workflows that have pending ready steps.
    Returns count of workflows that were advanced.
    """
    active = await _load_workflows(user_id, status=WorkflowStatus.ACTIVE)
    count = 0
    for wf in active:
        try:
            result = await advance_workflow_async(wf.id, user_id, session_id)
            if result.get("advanced"):
                count += 1
        except Exception as exc:
            logger.exception("Resume failed for workflow %s: %s", wf.id, exc)
    return count


# ─────────────────────────────────────────────────────────────────────────────
# Context injection for LLM system prompt
# ─────────────────────────────────────────────────────────────────────────────

async def get_active_workflows_summary_async(user_id: str) -> str:
    """
    Returns a short system-prompt section listing active workflows.
    Empty string if none.
    """
    try:
        workflows = await _load_workflows(user_id, status=WorkflowStatus.ACTIVE)
        if not workflows:
            return ""
        lines = ["Active Workflows (mention if relevant):"]
        for wf in workflows[:5]:
            done = sum(1 for s in wf.steps if s.status == "done")
            total = len(wf.steps)
            pending_steps = [s.name for s in wf.steps if s.status == "pending"][:2]
            next_hint = f" → next: {', '.join(pending_steps)}" if pending_steps else " → all steps complete"
            lines.append(f"  • [{wf.id[:8]}] {wf.name} ({done}/{total} steps){next_hint}")
        return "\n".join(lines)
    except Exception as exc:
        logger.warning("get_active_workflows_summary_async failed: %s", exc)
        return ""


async def list_workflows_async(user_id: str) -> List[Workflow]:
    return await _load_workflows(user_id)
