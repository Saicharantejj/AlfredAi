"""
multi_action_parser.py — Alfred Multi-Action DAG Executor

Replaces the single parse_tag() chain in alfred_core.py with a graph-aware
execution engine. Maintains 100% backward compatibility: if the LLM outputs
old-style single tags, they are auto-wrapped and executed normally.

New LLM output format (append this section to SYSTEM_PROMPT when multi-action
mode is enabled):

    ACTIONS: [
      {
        "id": "a0",
        "type": "WEB_SEARCH",
        "params": {"query": "Rahul Sharma LinkedIn", "engine": "free"},
        "confidence": 0.9,
        "risk_level": "low",
        "depends_on": []
      },
      {
        "id": "a1",
        "type": "WHATSAPP_SEND",
        "params": {"number": "Rahul Sharma", "message": "Hey! {{a0.result}}"},
        "confidence": 0.85,
        "risk_level": "medium",
        "depends_on": ["a0"]
      }
    ]

Key behaviours:
  - Topological sort ensures dependencies are respected.
  - `{{a0.result}}` template syntax injects the string result of action a0 into a1's params.
  - Actions with risk_level="high" OR confidence < HITL_CONFIDENCE_THRESHOLD are
    paused and written to hitl_approvals; a token URL is returned to the user.
  - If an action fails, its dependents are skipped (partial-failure isolation).
  - Backward-compat: old single-tag replies are detected and wrapped automatically.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set, Tuple

from pydantic import BaseModel, Field, field_validator, model_validator

from db_schemas import (
    HITLApproval,
    HITLStatus,
    RiskLevel,
    insert_hitl_approval,
)

logger = logging.getLogger("alfred.multi_action_parser")

# Actions whose risk_level must be at least MEDIUM before HITL kicks in.
# RUN_TERMINAL and MAC_SLEEP are always HIGH regardless of LLM-reported level.
_ALWAYS_HIGH_RISK: Set[str] = {"RUN_TERMINAL", "MAC_SLEEP", "MAC_LOCK"}

# Below this confidence score, any MEDIUM+ action requires human approval.
HITL_CONFIDENCE_THRESHOLD: float = float(__import__("os").getenv(
    "ALFRED_HITL_CONFIDENCE_THRESHOLD", "0.80"
))

# System-prompt addition to enable multi-action output from the LLM.
MULTI_ACTION_SYSTEM_SUFFIX = """
─── MULTI-ACTION MODE ───
You may now output multiple actions by replacing single action tags with an
ACTIONS array placed at the very end of your reply:

ACTIONS: [
  {
    "id": "a0",
    "type": "<ACTION_TYPE>",
    "params": { ... },
    "confidence": 0.95,
    "risk_level": "low|medium|high",
    "depends_on": []
  },
  {
    "id": "a1",
    "type": "<ACTION_TYPE>",
    "params": { "key": "{{a0.result}}" },
    "confidence": 0.85,
    "risk_level": "medium",
    "depends_on": ["a0"]
  }
]

Rules:
- Each action has a unique "id" string (a0, a1, …).
- "depends_on" is a list of IDs that must complete first.
- Use "{{<id>.result}}" inside params strings to inject prior results.
- "confidence" is your self-assessed probability of being correct (0–1).
- "risk_level": low (read ops), medium (messages), high (destructive/send).
- High-risk or low-confidence actions will be shown to the user for approval.
- For a single action, the old single-tag format still works.
""".strip()


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic Models
# ─────────────────────────────────────────────────────────────────────────────

class ActionNode(BaseModel):
    """A single executable action within a plan."""
    id: str = Field(default_factory=lambda: f"a{uuid.uuid4().hex[:4]}")
    type: str                               # e.g. "WHATSAPP_SEND"
    params: Dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    risk_level: RiskLevel = RiskLevel.LOW
    depends_on: List[str] = Field(default_factory=list)

    @field_validator("type")
    @classmethod
    def normalise_type(cls, v: str) -> str:
        return v.strip().upper()

    @model_validator(mode="after")
    def escalate_risk_for_dangerous_actions(self) -> "ActionNode":
        if self.type in _ALWAYS_HIGH_RISK:
            self.risk_level = RiskLevel.HIGH
        return self


class ActionPlan(BaseModel):
    """The full set of actions parsed from a single LLM reply."""
    plan_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    actions: List[ActionNode]
    # raw_reply: text portion of the LLM reply (before the ACTIONS tag)
    raw_reply: str = ""


class ActionResult(BaseModel):
    """Outcome of executing a single ActionNode."""
    action_id: str
    action_type: str
    success: bool
    result: Optional[str] = None     # string representation of the outcome
    error: Optional[str] = None
    # hitl_token: set when the action was paused for human approval
    hitl_token: Optional[str] = None
    skipped: bool = False            # True when a dependency failed


class PlanResult(BaseModel):
    """Aggregate result of executing an entire ActionPlan."""
    plan_id: str
    reply: str                       # cleaned natural-language reply to show user
    results: List[ActionResult]
    # pending_hitl: list of (token, summary) pairs awaiting user approval
    pending_hitl: List[Dict[str, str]] = Field(default_factory=list)
    # pending_command: backward-compat for terminal commands awaiting approval
    pending_command: Optional[str] = None
    # pending_pptx_topic: backward-compat
    pending_pptx_topic: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# Action Registry
# ─────────────────────────────────────────────────────────────────────────────

# Type alias for executor functions.
# Signature: async (params: dict, context: ExecutionContext) -> str
ActionExecutor = Callable[..., Coroutine[Any, Any, str]]

_EXECUTOR_REGISTRY: Dict[str, ActionExecutor] = {}


def register_action(action_type: str):
    """Decorator to register an async executor for an action type."""
    def decorator(fn: ActionExecutor) -> ActionExecutor:
        _EXECUTOR_REGISTRY[action_type.upper()] = fn
        return fn
    return decorator


# ─────────────────────────────────────────────────────────────────────────────
# Execution Context (carries per-request state between actions)
# ─────────────────────────────────────────────────────────────────────────────

class ExecutionContext:
    """Mutable context passed to each executor; also holds inter-action results."""

    def __init__(self, user_id: str, session_id: str, display_name: Optional[str] = None):
        self.user_id = user_id
        self.session_id = session_id
        self.display_name = display_name
        # results_map: action_id → result string (for template injection)
        self.results_map: Dict[str, str] = {}
        # Accumulated side-effects for PlanResult
        self.pending_command: Optional[str] = None
        self.pending_pptx_topic: Optional[str] = None
        self.pending_hitl: List[Dict[str, str]] = []

    def store_result(self, action_id: str, result: str) -> None:
        self.results_map[action_id] = result

    def inject_templates(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Replace {{<id>.result}} placeholders in param string values with the
        actual results stored in results_map.
        """
        injected: Dict[str, Any] = {}
        for key, value in params.items():
            if isinstance(value, str):
                def _replace(match: re.Match) -> str:
                    ref_id = match.group(1)
                    return self.results_map.get(ref_id, f"[result of {ref_id} unavailable]")
                injected[key] = re.sub(r"\{\{(\w+)\.result\}\}", _replace, value)
            else:
                injected[key] = value
        return injected


# ─────────────────────────────────────────────────────────────────────────────
# Built-in Executors (map existing alfred_core.py logic to the new registry)
# ─────────────────────────────────────────────────────────────────────────────

_WA_PLACEHOLDERS = {
    "unknown", "n/a", "na", "none", "number", "phone", "contact name",
    "exact name from contacts list", "recipient", "name", "",
}

@register_action("WHATSAPP_SEND")
async def _exec_whatsapp_send(params: Dict[str, Any], ctx: ExecutionContext) -> str:
    from connectors.whatsapp import whatsapp as wa_connector
    number = str(params.get("number", "") or "").strip()
    message = str(params.get("message", "") or "").strip()
    if number.lower() in _WA_PLACEHOLDERS:
        raise ValueError(
            "WhatsApp recipient could not be resolved. Ask the user for the phone number."
        )
    if not message:
        raise ValueError("WHATSAPP_SEND: message body is empty.")
    sent, reply_text = wa_connector.send_message(ctx.user_id, ctx.session_id, number, message)
    if not sent:
        err = reply_text or ""
        if any(x in err.lower() for x in ["detached frame", "context", "503", "connection lost"]):
            raise RuntimeError(
                "WhatsApp connection dropped. I'm automatically attempting to restart — "
                "please wait 10 seconds and try again."
            )
        raise RuntimeError(f"WhatsApp: {err or 'message could not be sent.'}")
    return f"WhatsApp sent to {number}."


@register_action("EMAIL_SEND")
async def _exec_email_send(params: Dict[str, Any], ctx: ExecutionContext) -> str:
    from email_handler import send_email
    from alfred_core import resolve_email_target_async
    to_raw = str(params.get("to", "")).strip()
    subject = str(params.get("subject", "")).strip()
    body = str(params.get("body", "")).strip()
    target, err = await resolve_email_target_async(ctx.user_id, to_raw)
    if not target:
        raise ValueError(err or f"Cannot resolve email recipient: {to_raw}")
    sent = send_email(to=target, subject=subject, body=body, user_id=ctx.user_id)
    if not sent:
        raise RuntimeError("Email could not be sent — check account settings.")
    return f"Email sent to {target}."


@register_action("SAVE_NOTE")
async def _exec_save_note(params: Dict[str, Any], ctx: ExecutionContext) -> str:
    from notes import save_note_async
    content = str(params.get("content", "")).strip()
    if not content:
        raise ValueError("SAVE_NOTE requires 'content'")
    await save_note_async(content, ctx.user_id)
    return f"Note saved: {content[:60]}"


@register_action("SET_REMINDER")
async def _exec_set_reminder(params: Dict[str, Any], ctx: ExecutionContext) -> str:
    from reminders import set_reminder
    message = str(params.get("message", "")).strip()
    minutes = int(params.get("minutes", 5))
    set_reminder(message=message, minutes=minutes)
    return f"Reminder set for {minutes} minutes: {message}"


@register_action("WEB_SEARCH")
async def _exec_web_search(params: Dict[str, Any], ctx: ExecutionContext) -> str:
    from alfred_core import web_search, get_groq_client
    query = str(params.get("query", "")).strip()
    engine = str(params.get("engine", "free"))
    if not query:
        raise ValueError("WEB_SEARCH requires 'query'")

    loop = asyncio.get_event_loop()

    # Tavily QnA for targeted questions; fallback to free search
    if engine == "tavily" and __import__("os").getenv("TAVILY_API_KEY"):
        def _tavily():
            from tavily import TavilyClient
            return TavilyClient(api_key=__import__("os").getenv("TAVILY_API_KEY")).qna_search(query=query)
        try:
            return await loop.run_in_executor(None, _tavily)
        except Exception:
            pass

    raw_results = await loop.run_in_executor(None, web_search, query, engine)

    # Summarise with Groq so downstream actions or the reply is clean
    def _summarise():
        return get_groq_client().chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": "Summarise these search results in 2 sharp sentences. Be factual."},
                {"role": "user", "content": f"Query: {query}\n\nResults:\n{raw_results[:2000]}"},
            ],
            temperature=0.5,
            max_tokens=200,
        )
    try:
        resp = await loop.run_in_executor(None, _summarise)
        return resp.choices[0].message.content.strip()
    except Exception:
        return raw_results[:800]


@register_action("RUN_TERMINAL")
async def _exec_run_terminal(params: Dict[str, Any], ctx: ExecutionContext) -> str:
    # Terminal commands are always routed through HITL — this executor
    # is called only AFTER the user approves. The DAG executor checks
    # risk_level and intercepts before calling this.
    cmd = str(params.get("command", "")).strip()
    ctx.pending_command = cmd
    return f"Terminal command queued for approval: {cmd}"


@register_action("PPTX_CREATE")
async def _exec_pptx_create(params: Dict[str, Any], ctx: ExecutionContext) -> str:
    topic = str(params.get("topic", "")).strip()
    ctx.pending_pptx_topic = topic
    return f"Presentation creation queued for topic: {topic}"


@register_action("OPEN_APP")
async def _exec_open_app(params: Dict[str, Any], ctx: ExecutionContext) -> str:
    from platform_utils import open_application
    app_name = str(params.get("app", "")).strip()
    open_application(app_name)
    return f"Launched {app_name}."


@register_action("SPOTIFY")
async def _exec_spotify(params: Dict[str, Any], ctx: ExecutionContext) -> str:
    from spotify import spotify_command, set_spotify_volume, play_song
    action = str(params.get("action", "")).lower()
    query = str(params.get("query", ""))
    if action == "volume":
        set_spotify_volume(int(params.get("level", 50)))
        return "Spotify volume set."
    if action == "play" and query:
        play_song(query)
        return f"Playing {query} on Spotify."
    spotify_command(action)
    return f"Spotify: {action}"


@register_action("MEMORY_UPDATE")
async def _exec_memory_update(params: Dict[str, Any], ctx: ExecutionContext) -> str:
    from memory import update_memory_async
    updates = []
    for key, value in params.items():
        await update_memory_async(key, str(value), ctx.user_id)
        updates.append(key)
    return f"Memory updated: {', '.join(updates)}"


@register_action("ACTIVATE_MODE")
async def _exec_activate_mode(params: Dict[str, Any], ctx: ExecutionContext) -> str:
    from modes import activate_mode
    mode = str(params.get("mode", "")).lower()
    activate_mode(mode)
    return f"Mode activated: {mode}"


@register_action("MAC_VOLUME")
async def _exec_mac_volume(params: Dict[str, Any], ctx: ExecutionContext) -> str:
    from modes import set_volume
    level = int(params.get("level", 50))
    set_volume(level)
    return f"Volume set to {level}."


@register_action("MAC_LOCK")
async def _exec_mac_lock(params: Dict[str, Any], ctx: ExecutionContext) -> str:
    from modes import lock_mac
    lock_mac()
    return "Mac locked."


@register_action("MAC_SLEEP")
async def _exec_mac_sleep(params: Dict[str, Any], ctx: ExecutionContext) -> str:
    from modes import sleep_mac
    sleep_mac()
    return "Mac put to sleep."


@register_action("COLD_EMAIL_CAMPAIGN")
async def _exec_cold_email(params: Dict[str, Any], ctx: ExecutionContext) -> str:
    topic = params.get("topic", "")
    return f"Cold email campaign queued for: {topic}"


@register_action("TASK_CREATE")
async def _exec_task_create(params: Dict[str, Any], ctx: ExecutionContext) -> str:
    from task_manager_v2 import create_task_async
    from db_schemas import TaskV2, TaskPriority
    task = TaskV2(
        user_id=ctx.user_id,
        text=str(params.get("text", "")).strip(),
        priority=params.get("priority", TaskPriority.MEDIUM),
        due_date=None,  # parsed separately if provided
    )
    if not task.text:
        raise ValueError("TASK_CREATE requires 'text'")
    task_id = await create_task_async(task)
    return f"Task created (id={task_id}): {task.text}"


@register_action("WORKFLOW_CREATE")
async def _exec_workflow_create(params: Dict[str, Any], ctx: ExecutionContext) -> str:
    from workflow_engine import create_workflow_async
    from db_schemas import Workflow, WorkflowStep
    name = str(params.get("name", "")).strip()
    if not name:
        raise ValueError("WORKFLOW_CREATE requires 'name'")
    raw_steps = params.get("steps", [])
    steps = [WorkflowStep(**s) for s in raw_steps] if raw_steps else []
    wf = Workflow(user_id=ctx.user_id, name=name, steps=steps)
    wf_id = await create_workflow_async(wf)
    return f"Workflow '{name}' created (id={wf_id}) with {len(steps)} step(s)."


# ─────────────────────────────────────────────────────────────────────────────
# Parser: extract ActionPlan from LLM reply
# ─────────────────────────────────────────────────────────────────────────────

# Matches: ACTIONS: [ ... ] at the end of the reply (allows whitespace/newlines)
_ACTIONS_RE = re.compile(
    r"ACTIONS\s*:\s*(\[.*\])\s*$",
    re.DOTALL | re.IGNORECASE,
)

# Known old-style single tags (from alfred_core.SYSTEM_PROMPT)
_LEGACY_TAGS = [
    "ACTIVATE_MODE", "WHATSAPP_SEND", "EMAIL_SEND", "OPEN_APP", "SAVE_NOTE",
    "SET_REMINDER", "SPOTIFY", "MAC_VOLUME", "MAC_LOCK", "MAC_SLEEP",
    "MEMORY_UPDATE", "PPTX_CREATE", "RUN_TERMINAL", "WEB_SEARCH",
    "COLD_EMAIL_CAMPAIGN",
]
_RISK_MAP: Dict[str, RiskLevel] = {
    "MAC_LOCK":    RiskLevel.HIGH,
    "MAC_SLEEP":   RiskLevel.HIGH,
    "RUN_TERMINAL": RiskLevel.HIGH,
    "EMAIL_SEND":  RiskLevel.MEDIUM,
    "WHATSAPP_SEND": RiskLevel.MEDIUM,
    "COLD_EMAIL_CAMPAIGN": RiskLevel.MEDIUM,
}


def _parse_legacy_tags(reply: str) -> Tuple[str, List[ActionNode]]:
    """
    Detect old single-tag format and convert to ActionNode list.
    Returns (clean_reply_text, [action_nodes]).
    """
    from alfred_core import parse_tag  # import here to avoid circular at module load

    actions: List[ActionNode] = []
    clean = reply

    for tag in _LEGACY_TAGS:
        clean, data = parse_tag(clean, tag)
        if data is None:
            continue
        node = ActionNode(
            id=f"a{len(actions)}",
            type=tag,
            params=data if isinstance(data, dict) else {},
            confidence=1.0,
            risk_level=_RISK_MAP.get(tag, RiskLevel.LOW),
            depends_on=[],
        )
        actions.append(node)
        # Legacy format: at most one tag per type — stop after first match
        break

    return clean.strip(), actions


def parse_plan_from_reply(reply: str) -> ActionPlan:
    """
    Parse an LLM reply into an ActionPlan.

    Priority:
      1. New ACTIONS array format.
      2. Old single-tag format (backward-compat).
      3. No actions detected → empty plan.
    """
    # ── Try new ACTIONS format ───────────────────────────────────────────────
    m = _ACTIONS_RE.search(reply)
    if m:
        raw_reply = reply[: m.start()].strip()
        try:
            raw_list = json.loads(m.group(1))
            if not isinstance(raw_list, list):
                raise ValueError("ACTIONS must be a JSON array")
            nodes = [ActionNode(**item) for item in raw_list]
            return ActionPlan(raw_reply=raw_reply, actions=nodes)
        except Exception as exc:
            logger.warning("Failed to parse ACTIONS array (%s); falling back to legacy tags.", exc)

    # ── Fallback: legacy single-tag format ───────────────────────────────────
    clean_reply, nodes = _parse_legacy_tags(reply)
    return ActionPlan(raw_reply=clean_reply, actions=nodes)


# ─────────────────────────────────────────────────────────────────────────────
# DAG Executor
# ─────────────────────────────────────────────────────────────────────────────

class DAGExecutionError(Exception):
    """Raised when a cyclic dependency or unresolvable graph is detected."""


def _topological_sort(actions: List[ActionNode]) -> List[ActionNode]:
    """
    Kahn's algorithm for topological sort.
    Raises DAGExecutionError on cycles.
    """
    id_map: Dict[str, ActionNode] = {a.id: a for a in actions}
    in_degree: Dict[str, int] = {a.id: 0 for a in actions}

    for node in actions:
        for dep in node.depends_on:
            if dep not in id_map:
                raise DAGExecutionError(
                    f"Action '{node.id}' depends on unknown action '{dep}'"
                )
            in_degree[node.id] += 1

    queue = [a for a in actions if in_degree[a.id] == 0]
    sorted_nodes: List[ActionNode] = []

    while queue:
        node = queue.pop(0)
        sorted_nodes.append(node)
        for candidate in actions:
            if node.id in candidate.depends_on:
                in_degree[candidate.id] -= 1
                if in_degree[candidate.id] == 0:
                    queue.append(candidate)

    if len(sorted_nodes) != len(actions):
        raise DAGExecutionError("Circular dependency detected in action plan.")

    return sorted_nodes


def _needs_hitl(node: ActionNode) -> bool:
    """Return True if this action requires human approval before execution."""
    if node.risk_level == RiskLevel.HIGH:
        return True
    if node.risk_level == RiskLevel.MEDIUM and node.confidence < HITL_CONFIDENCE_THRESHOLD:
        return True
    return False


def _build_hitl_summary(node: ActionNode) -> str:
    """Produce a one-line human-readable summary for the approval UI."""
    param_preview = ", ".join(
        f"{k}={str(v)[:40]}" for k, v in list(node.params.items())[:3]
    )
    return f"[{node.type}] {param_preview}"


async def execute_plan(
    plan: ActionPlan,
    ctx: ExecutionContext,
) -> PlanResult:
    """
    Execute an ActionPlan respecting DAG dependencies.

    For each action:
      - If depends_on contains a failed action → skip (record as skipped).
      - If _needs_hitl() → create HITL approval record, append token to pending_hitl.
      - Otherwise → call the registered executor, store result in ctx.results_map.
    """
    results: List[ActionResult] = []
    failed_ids: Set[str] = set()

    try:
        ordered = _topological_sort(plan.actions)
    except DAGExecutionError as exc:
        logger.error("DAG sort failed for plan %s: %s", plan.plan_id, exc)
        return PlanResult(
            plan_id=plan.plan_id,
            reply=plan.raw_reply + f"\n\n(Internal error: {exc})",
            results=[],
        )

    for node in ordered:
        # ── Dependency failure propagation ───────────────────────────────────
        blocked_by = [dep for dep in node.depends_on if dep in failed_ids]
        if blocked_by:
            logger.info(
                "Skipping action %s (%s): dependency %s failed.",
                node.id, node.type, blocked_by,
            )
            results.append(ActionResult(
                action_id=node.id,
                action_type=node.type,
                success=False,
                skipped=True,
                error=f"Skipped because dependency {blocked_by} failed.",
            ))
            failed_ids.add(node.id)
            continue

        # ── HITL gate ────────────────────────────────────────────────────────
        if _needs_hitl(node):
            approval = HITLApproval(
                user_id=ctx.user_id,
                action_type=node.type,
                action_payload=node.params,
                risk_level=node.risk_level,
                confidence=node.confidence,
                summary=_build_hitl_summary(node),
                plan_id=plan.plan_id,
                created_at=datetime.utcnow(),
                expires_at=datetime.utcnow() + timedelta(minutes=30),
            )
            try:
                await insert_hitl_approval(approval)
            except Exception as db_exc:
                logger.error("Failed to write HITL approval: %s", db_exc)

            token_url = f"/api/hitl/approve/{approval.token}"
            ctx.pending_hitl.append({
                "token": approval.token,
                "url": token_url,
                "summary": approval.summary,
                "action_type": node.type,
            })

            results.append(ActionResult(
                action_id=node.id,
                action_type=node.type,
                success=False,
                hitl_token=approval.token,
                error="Awaiting human approval.",
            ))
            # Treat as "failed" so dependents are skipped until approval
            failed_ids.add(node.id)
            logger.info(
                "HITL gate triggered for plan %s action %s (%s) — token %s",
                plan.plan_id, node.id, node.type, approval.token,
            )
            continue

        # ── Execute ──────────────────────────────────────────────────────────
        executor = _EXECUTOR_REGISTRY.get(node.type)
        if executor is None:
            logger.warning("No executor registered for action type '%s'", node.type)
            results.append(ActionResult(
                action_id=node.id,
                action_type=node.type,
                success=False,
                error=f"Unknown action type: {node.type}",
            ))
            failed_ids.add(node.id)
            continue

        # Inject dependency results into params before execution
        injected_params = ctx.inject_templates(node.params)

        try:
            result_str = await executor(injected_params, ctx)
            ctx.store_result(node.id, result_str)
            results.append(ActionResult(
                action_id=node.id,
                action_type=node.type,
                success=True,
                result=result_str,
            ))
            logger.info(
                "Action %s/%s succeeded: %.80s", node.id, node.type, result_str
            )
        except Exception as exc:
            err_msg = str(exc)
            logger.exception(
                "Action %s/%s failed in plan %s: %s",
                node.id, node.type, plan.plan_id, err_msg,
            )
            ctx.store_result(node.id, f"[ERROR: {err_msg}]")
            results.append(ActionResult(
                action_id=node.id,
                action_type=node.type,
                success=False,
                error=err_msg,
            ))
            failed_ids.add(node.id)

    # ── Build final reply ────────────────────────────────────────────────────
    reply_parts = [plan.raw_reply] if plan.raw_reply else []

    # Append HITL notices
    for hitl in ctx.pending_hitl:
        reply_parts.append(
            f"\n\nI need your approval before I can {hitl['action_type'].lower().replace('_', ' ')}. "
            f"[Approve / Reject]({hitl['url']})"
        )

    # Append non-HITL errors so the user knows what failed
    for r in results:
        if not r.success and not r.skipped and not r.hitl_token and r.error:
            # Only surface errors for visible actions (not memory/mode updates)
            if r.action_type not in {"MEMORY_UPDATE", "ACTIVATE_MODE"}:
                reply_parts.append(f"\n\n({r.action_type}: {r.error})")

    final_reply = "".join(reply_parts).strip()

    return PlanResult(
        plan_id=plan.plan_id,
        reply=final_reply,
        results=results,
        pending_hitl=ctx.pending_hitl,
        pending_command=ctx.pending_command,
        pending_pptx_topic=ctx.pending_pptx_topic,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Public entry-point (drop-in for alfred_core.chat's parse+execute block)
# ─────────────────────────────────────────────────────────────────────────────

async def parse_and_execute(
    llm_reply: str,
    user_id: str,
    session_id: str,
    display_name: Optional[str] = None,
) -> PlanResult:
    """
    Parse an LLM reply into an ActionPlan and execute it.

    This is the single call that replaces the 18-tag sequential chain in
    alfred_core.chat(). Usage:

        plan_result = await parse_and_execute(reply, user_id, session_id)
        return {
            "reply": plan_result.reply,
            "pending_command": plan_result.pending_command,
            "pending_pptx_topic": plan_result.pending_pptx_topic,
            "pending_hitl": plan_result.pending_hitl,
        }
    """
    plan = parse_plan_from_reply(llm_reply)
    ctx = ExecutionContext(
        user_id=user_id,
        session_id=session_id,
        display_name=display_name,
    )

    if not plan.actions:
        # No actions — return the raw text as-is.
        return PlanResult(
            plan_id=plan.plan_id,
            reply=plan.raw_reply,
            results=[],
        )

    logger.info(
        "Executing plan %s for user %s: %d action(s) — %s",
        plan.plan_id,
        user_id,
        len(plan.actions),
        [f"{a.id}:{a.type}" for a in plan.actions],
    )

    return await execute_plan(plan, ctx)
