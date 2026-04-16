import re
import logging
from typing import Union, Optional, List, Dict, Any, Tuple, Literal
from groq import Groq

logger = logging.getLogger("alfred")
from dotenv import load_dotenv
from memory import get_memory_context_async, update_memory_async, load_memory_async
from email_handler import send_email
from email_reader import get_unread_emails
from modes import activate_mode, get_current_mode, set_volume, lock_mac, sleep_mac
from notes import save_note_async, get_notes_async
from reminders import set_reminder
from spotify import spotify_command, set_spotify_volume, get_current_track, play_song
from finance import get_stock_price, convert_currency
from screen import capture_screen
from calendar_helper import get_todays_events, create_event
from briefing import get_news_async, get_weather_async, get_stocks_async, morning_briefing_async, end_of_day
from tasks import add_task_async, get_pending_tasks_async, complete_task_async, get_all_tasks_async
from contacts import (
    extract_phone_from_chat_id,
    find_contacts_by_query_async,
    get_contact_directory_async,
    load_contacts_async,
    normalize_phone_number,
)
from platform_utils import open_application
from connectors.whatsapp import whatsapp as whatsapp_connector, BRIDGE_URL as WHATSAPP_BRIDGE_URL
import memory_tracker
import os
import json
import httpx
import time
import asyncio
import subprocess

load_dotenv()

# We initialize the client inside a helper to prevent crashes during module import 
# if the GROQ_API_KEY is missing from the environment.
_client: Optional[Groq] = None

def get_groq_client() -> Groq:
    global _client
    if _client is None:
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            logger.error("GROQ_API_KEY is missing from the environment!")
            raise ValueError("GROQ_API_KEY is missing. Please add it to your environment variables.")
        _client = Groq(api_key=api_key)
    return _client

EMAIL_RE = re.compile(r"^[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,63}$", re.IGNORECASE)

SYSTEM_PROMPT = """You are Alfred — a sharp, deeply personal AI assistant. Part chief of staff, part Jarvis. You've worked with this person long enough to anticipate what they actually need.

You speak with calm authority. Concise, warm, occasionally dry. Never generic. Be Alfred.

── THINK BEFORE ACTING ──
For every request, run this loop internally before responding:
1. UNDERSTAND: What does the user actually want? Not just the literal request — what are they trying to achieve?
2. GOAL CHECK: Is this part of something bigger — an outreach campaign, a project, a recurring workflow?
3. CONTEXT CHECK: What do I already know — memory, tasks, recent context — that changes how I should respond?
4. DECIDE:
   • Request is clear and low-risk → act directly, brief confirmation
   • Request is ambiguous → ask ONE sharp clarifying question, don't guess and act
   • Multiple valid approaches → pick the best one, briefly say why
   • High-risk action (sending, deleting, system commands) → state intent first, then act
5. PLAN: For 2+ step tasks, state the steps in one line before executing

── DECISION RULES ──
• Unclear request → ask ONE precise question. Not multiple. Not a list.
• Multiple valid options → recommend the best one with a one-line rationale. Don't list them all.
• Low-risk + obvious → act directly. Skip confirmation theater.
• High-risk (messages to others, financial, destructive system actions) → state what you're about to do, then do it.
• Sensitive topics (money, relationships, health, legal) → be thoughtful, not just efficient.
• For obvious follow-through where the answer is almost always yes — don't ask. Own it and state it.

── CONFIDENCE-AWARE LANGUAGE ──
Your certainty level must show in your word choice — not just your content.
• High confidence: say it directly. "Best move is X." "Do this." "Go with Y." No qualifiers.
• Medium confidence: show your reasoning. "Probably worth X because..." "I'd lean toward Y — though Z is also in play." "My read is..."
• Low confidence: explore before advising. "What's the goal here?" "Worth checking if X first." "Could be A or B — depends on..."
Never hedge when you're sure. Never sound certain when you're guessing.
The CONFIDENCE tag in the REASONING FRAME tells you which register to use.

── SOFT DISAGREEMENT ──
When you see a clearly suboptimal choice, say so — once, briefly, without judgment:
• "That works — though [X] would get you there faster because [one reason]."
• "Happy to do that. Worth noting: [brief concern]. Still want to proceed?"
• "You could go that route — if the goal is [Y], [Z] is probably cleaner."
Don't challenge reasonable decisions. Only flag when there's a meaningfully better path.
Say it once, then respect the call. Never repeat the objection or push back twice.

── CONTINUITY ──
When a message is clearly continuing from something earlier — pick up the thread:
• "Next step from what you set up — on it."
• "Continuing from earlier: [what's happening now]."
• "This looks like the follow-up to [X] — here's where things stand."
If the REASONING FRAME includes a CONTINUITY tag, reference the connection in your response.
Keep continuity acknowledgment to one clause — not a full recap.

── FOLLOW-THROUGH AND OWNERSHIP ──
Take ownership of obvious next steps. Don't ask when the answer is almost always yes.
• "I'll track this and flag you if there's no reply" beats "Do you want me to track this?"
• "I'll remind you 30 minutes before" beats "Should I set a reminder?"
• "Kicking off step one now" beats "Want me to start step one?"
When to still ask: genuinely optional or preference-dependent follow-through.
• "Should I break this into subtasks?" — depends on how they work
• "Want a deadline on this?" — depends on urgency they haven't signaled
Weave follow-through into the response naturally — not as a separate appended clause.
If there's no clearly useful next step — stop. Don't fill space.

── PRIORITIZATION ──
When there are multiple tasks, don't summarize — recommend, with a brief reason:
• "I'd start with [X] — it's marked urgent and already past its window."
• "The one that can't wait is [X] — [one-line reason]."
• Don't list everything. Surface the one item with the strongest case for going first.
If the REASONING FRAME includes a PRIORITY tag, use that exact task and the reason given.

── TONE ADAPTATION ──
Shift tone to match the weight of the moment — subtly, not dramatically:
• Urgent context: short clauses, active verbs, no softening. "Done." "On it." "Sent." Cut everything else.
• Normal context: conversational and direct — the default Alfred register.
• Low-stakes context: a touch lighter. It's fine to be a bit more relaxed when the stakes are low.
The TONE tag in the REASONING FRAME signals which register is active. Don't narrate the shift — just apply it.

── RESPONSE STYLE ──
• Keep responses under 3 sentences unless the user asked for detail
• Lead with the answer or action — not the reasoning
• Be slightly opinionated when it helps: "I'd go with X because..." not "here are some options"
• Dry wit is welcome when the moment fits. Forced cheerfulness is not.
• Never say "Certainly!", "Of course!", "Great question!", or any hollow opener
• Don't narrate what you're doing — just do it
• Vary sentence openings — don't start consecutive responses the same way

── RESTRAINT ──
Not every response needs a next step. Not every action needs a follow-up.
• Someone asks a question → answer it and stop
• Someone sets the volume → confirm and stop
• Someone says thanks → acknowledge briefly and stop
• Someone completes a simple task → confirm and stop
The strongest signal of good judgment is knowing when to say nothing more.

── MEMORY AWARENESS ──
• Use what you already know — don't ask things you've been told
• Don't repeat information the user just gave you
• Reference past context naturally when relevant, or just use it without calling it out
• Adapt to their patterns: if they always want brevity, be brief; if they like detail, give detail
• First-time users: be slightly more explicit about what you're doing and why
• Repeat users with known name and context: skip intros, get to the point

── PROACTIVITY ──
• Think one step ahead — but only when it's actually useful, not as a habit
• Surface risks or better approaches before the user hits them
• Only intervene when you'd actually want to be interrupted for it
• Silence is better than a mediocre suggestion

── PLANNING BEFORE ACTING ──
For requests involving 2+ actions, or where you're uncertain about a step:
1. State what you're about to do in one line
2. Then output the ACTIONS array
Do NOT explain each action at length — just act.

── MULTI-ACTION FORMAT (preferred) ──
Place at the very end of your reply. Use when you need multiple actions or one depends on another.

ACTIONS: [
  {
    "id": "a0",
    "type": "ACTION_TYPE",
    "params": { ... },
    "confidence": 0.95,
    "risk_level": "low|medium|high",
    "depends_on": []
  },
  {
    "id": "a1",
    "type": "ACTION_TYPE",
    "params": { "key": "{{a0.result}}" },
    "confidence": 0.85,
    "risk_level": "medium",
    "depends_on": ["a0"]
  }
]

Use {{a0.result}} to inject the result of action a0 into a later action's params.
risk_level: low = read/informational, medium = messaging/send, high = destructive/system.
High-risk or low-confidence actions will be shown to the user for approval before executing.

── SINGLE-ACTION FORMAT (backward compatible) ──
ACTIVATE_MODE: {"mode": "study|work|gaming|streaming|sleep"}
WHATSAPP_SEND: {"number": "EXACT NAME FROM CONTACTS LIST or full phone number with country code", "message": "MESSAGE"}
EMAIL_SEND: {"to": "EMAIL OR CONTACT NAME", "subject": "SUBJECT", "body": "BODY"}
OPEN_APP: {"app": "App Name"}
SAVE_NOTE: {"content": "note content"}
SET_REMINDER: {"message": "reminder text", "minutes": 10}
SPOTIFY: {"action": "play|pause|next|previous|volume", "query": "song name if playing", "level": 50}
MAC_VOLUME: {"level": 50}
MAC_LOCK: {}
MAC_SLEEP: {}
MEMORY_UPDATE: {"key": "value"}
PPTX_CREATE: {"topic": "full topic or outline for the presentation"}
RUN_TERMINAL: {"command": "command to run"}
WEB_SEARCH: {"query": "exact search term", "engine": "free"}
TASK_CREATE: {"text": "task description", "priority": "low|medium|high|urgent", "due_date": "YYYY-MM-DD or null"}
WORKFLOW_CREATE: {"name": "workflow name", "steps": [{"step_id": "s0", "name": "Step Name", "action_type": "ACTION_TYPE", "params": {}, "depends_on": []}]}

── ACTION RULES ──
PPTX_CREATE: Use when asked to "make a presentation", "create slides", or "generate a PPT".
TASK_CREATE: Use when the user asks to track a goal, project, or multi-step objective. After creating, suggest next steps.
WORKFLOW_CREATE: Use for multi-step processes the user will run repeatedly (e.g. outreach, onboarding, follow-up campaigns).
WEB_SEARCH: Use engine "tavily" ONLY for live sports/schedules. Use "free" for all other searches.
CRITICAL: For factual questions, current events, prices, scores — ALWAYS use WEB_SEARCH. Never guess.
Do NOT use RUN_TERMINAL for internet searches. RUN_TERMINAL is for local macOS operations only.
To read a document: tell the user to use the attach button.

__PREMIUM_COLD_EMAIL_PLACEHOLDER__

WHATSAPP_SEND rules:
- ONLY use if: (a) user gave a full phone number, OR (b) recipient is in the Contacts section below.
- If not in Contacts, ask: "I don't have [name]'s number. What's their WhatsApp number?"
- "number" field = exact name from Contacts or real phone number. NEVER use "unknown", "n/a", or placeholders.

Prefer WhatsApp for message/text requests, email for email/mail requests.
Only output action tags when there is a clear actionable instruction."""


def parse_tag(reply, tag):
    if tag + ":" not in reply:
        return reply.strip(), None
    parts = reply.split(tag + ":")
    clean_reply = parts[0].strip()
    try:
        raw = parts[1].strip()
        raw = re.sub(r"[\x00-\x1f\x7f]", "", raw)
        # Extract only the first {...} block — ignore any trailing text
        brace = raw.find("{")
        if brace == -1:
            return clean_reply, None
        depth = 0
        end = brace
        for i, ch in enumerate(raw[brace:], brace):
            if ch == "{": depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        data = json.loads(raw[brace:end+1])
        return clean_reply, data
    except Exception as e:
        print(tag + " parse error: " + str(e))
        return clean_reply, None


def web_search(query, engine="free"):
    try:
        results = []
        top_link = None
        
        # Use Tavily if requested or if it's a high-quality lookup
        if engine == "tavily" or (os.getenv("TAVILY_API_KEY") and engine == "free"):
            try:
                from tavily import TavilyClient
                tavily = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))
                search_res = tavily.search(query=query, search_depth="basic", max_results=5)
                for r in search_res.get('results', []):
                    results.append(f"- {r['title']}: {r['content'][:200]}")
                    if not top_link:
                        top_link = r['url']
            except Exception as te:
                print(f"Tavily error: {te}")
                # Fallback to DDGS if Tavily fails
                engine = "free"

        if engine == "free" and not results:
            try:
                from ddgs import DDGS
                with DDGS() as ddgs:
                    ddgs_gen = ddgs.text(query, max_results=6)
                    for r in ddgs_gen:
                        results.append(f"- {r['title']}: {r['body']}")
                        if not top_link:
                            # Avoid DuckDuckGo ad/redirect links
                            if "duckduckgo.com/y.js" not in r['href']:
                                top_link = r['href']
            except Exception as de:
                print(f"DDGS error: {de}")

        # Add Google News RSS as secondary context
        try:
            import requests
            from bs4 import BeautifulSoup
            import urllib.parse
            news_url = f"https://news.google.com/rss/search?q={urllib.parse.quote(query)}&hl=en-IN&gl=IN&ceid=IN:en"
            news_resp = requests.get(news_url, timeout=5)
            nsoup = BeautifulSoup(news_resp.content, 'xml')
            for item in nsoup.find_all('item')[:3]:
                results.append(f"- LATEST NEWS: {item.title.text}")
        except Exception:
            pass

        if not results:
            return "No results found."

        final_text = "\n".join(results)
        
        # Fetch deep context from top result using Jina
        if top_link:
            try:
                import requests
                jina_resp = requests.get(f"https://r.jina.ai/{top_link}", timeout=8)
                if jina_resp.ok:
                    final_text += f"\n\nContext from top result ({top_link}):\n{jina_resp.text[:1500]}"
            except Exception:
                pass
            
        return final_text
    except Exception as e:
        print("Web search error: " + str(e))
        return "Could not search the web."



def open_app(app_name):
    try:
        if not open_application(app_name):
            raise RuntimeError("Application launch not supported on this platform")
    except Exception as e:
        print("App open error: " + str(e))


def is_explicit_app_launch_request(message: str) -> bool:
    lowered = str(message or "").lower()
    explicit_phrases = [
        "open ",
        "launch ",
        "start ",
        "run ",
        "open up ",
        "launch the ",
        "open the app",
        "open spotify",
        "open chatgpt",
        "start spotify",
        "start chatgpt",
    ]
    return any(phrase in lowered for phrase in explicit_phrases)


async def resolve_whatsapp_target_async(user_id: str, raw_target: str) -> Tuple[Optional[str], Optional[str]]:
    return await whatsapp_connector.resolve_target_async(user_id, raw_target)


async def resolve_email_target_async(user_id: str, raw_target: str) -> Tuple[Optional[str], Optional[str]]:
    target = str(raw_target or "").strip()
    if not target:
        return None, "Email recipient is missing."
    if EMAIL_RE.fullmatch(target):
        return target, None

    contacts = await find_contacts_by_query_async(user_id, target)
    matches = [contact for contact in contacts if contact.get("email")]
    if len(matches) == 1:
        return matches[0].get("email"), None
    if len(matches) > 1:
        return None, f'Multiple email contacts match "{target}". Use the full email address.'
    return None, f'No saved contact with an email address matches "{target}".'


async def split_short_send_command_async(command_text: str, user_id: str, resolver) -> Tuple[Optional[str], Optional[str]]:
    text = str(command_text or "").strip()
    words = text.split()
    if len(words) < 2:
        return None, None

    for split_index in range(len(words) - 1, 0, -1):
        recipient_candidate = " ".join(words[:split_index]).strip(" ,")
        body_candidate = " ".join(words[split_index:]).strip()
        body_candidate = re.sub(r"^(?:saying|that)\s+", "", body_candidate, flags=re.IGNORECASE)
        if not body_candidate:
            continue
        resolved, _ = await resolver(user_id, recipient_candidate)
        if resolved:
            return recipient_candidate, body_candidate
    return None, None


def build_email_subject(body: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(body or "")).strip(" .")
    if not cleaned:
        return "Quick note"
    if len(cleaned) <= 60:
        return cleaned
    return cleaned[:57].rstrip() + "..."


def _build_thinking_context(raw_message: str, memory: dict, task_count: int) -> str:
    """
    Build a goal-aware cognitive frame injected into the system prompt.

    Goes beyond intent classification to infer:
      - what the user is actually trying to achieve (goal)
      - urgency level
      - whether this is part of a larger workflow
      - whether follow-up is likely needed
      - context sensitivity (workload, familiarity, time of day)

    No extra API call — pure semantic pattern analysis.
    """
    from datetime import datetime

    msg = raw_message.lower().strip()
    word_set = set(msg.split())
    word_count = len(msg.split())
    hints: list = []

    # ── Urgency detection ────────────────────────────────────────────────────
    _URGENCY_SIGNALS = {
        "urgent", "asap", "immediately", "right now", "right away", "quick",
        "quickly", "tonight", "this morning", "deadline", "emergency",
        "last minute", "before end of day", "eod", "before i forget",
        "in 5 minutes", "in 30 minutes", "before the meeting", "before my call",
    }
    is_urgent = any(sig in msg for sig in _URGENCY_SIGNALS)

    # ── Intent classification ────────────────────────────────────────────────
    _ACTION_VERBS = {
        "send", "email", "message", "whatsapp", "create", "add", "set", "open",
        "play", "lock", "sleep", "search", "remind", "schedule", "make", "write",
        "draft", "book", "cancel", "delete", "remove", "forward", "reply",
        "call", "text", "ping", "update", "build", "track", "research",
    }
    is_action_request = any(w in word_set or msg.startswith(w + " ") for w in _ACTION_VERBS)
    is_question = (
        msg.endswith("?")
        or any(
            msg.startswith(w + " ")
            for w in ["what", "who", "when", "where", "why", "how", "is", "are",
                      "can", "do", "does", "should", "would", "could", "will",
                      "tell me", "show me"]
        )
    )
    is_vague = word_count <= 3 and not is_action_request
    is_high_stakes = any(w in msg for w in [
        "delete", "remove", "wipe", "cancel", "fire", "quit", "resign",
        "money", "invest", "loan", "send all", "forward all",
    ])
    is_ambiguous = word_count <= 6 and not is_action_request and not is_question and not is_high_stakes

    # ── Goal inference ───────────────────────────────────────────────────────
    # Map surface request → underlying goal → what Alfred should be thinking about
    goal: str = ""
    follow_up_likely: bool = False
    is_part_of_workflow: bool = False
    pre_action_suggestion: str = ""

    _SEND_WORDS = {"email", "whatsapp", "message", "text", "ping", "reach out", "send", "reply", "forward"}
    _BUSINESS_WORDS = {"client", "customer", "investor", "partner", "prospect", "lead", "vendor", "team", "boss"}
    _FOLLOWUP_WORDS = {"follow up", "follow-up", "check in", "checking in", "catch up", "following up"}
    _TASK_WORDS = {"task", "todo", "to-do", "track", "project", "goal", "milestone", "deadline", "add"}
    _RESEARCH_WORDS = {"search", "find", "research", "look up", "who is", "what is", "latest", "news"}
    _SCHEDULE_WORDS = {"schedule", "meeting", "calendar", "call", "appointment", "block", "event"}
    _BUILD_WORDS = {"workflow", "automate", "process", "template", "campaign", "outreach", "system"}

    if any(w in msg for w in _SEND_WORDS):
        if any(w in msg for w in _FOLLOWUP_WORDS):
            goal = "following up with a contact"
            follow_up_likely = True
            pre_action_suggestion = "suggest tracking the reply before sending"
        elif any(w in msg for w in _BUSINESS_WORDS):
            goal = "business communication — likely needs reply tracking"
            follow_up_likely = True
            is_part_of_workflow = True
            pre_action_suggestion = "mention follow-up tracking before or right after sending"
        elif "team" in msg or "colleague" in msg:
            goal = "internal coordination"
        else:
            goal = "reaching out to someone"
            follow_up_likely = True

    elif any(w in word_set for w in _TASK_WORDS):
        if task_count >= 8:
            goal = f"adding to a heavy queue ({task_count} active) — prioritization may be more useful than adding"
        elif task_count >= 5:
            goal = "tracking a new item — user has an active queue, keep it focused"
        else:
            goal = "capturing a commitment or next step"
        follow_up_likely = True
        pre_action_suggestion = "after creating, ask if they want a due date or subtasks"

    elif any(w in msg for w in _RESEARCH_WORDS):
        goal = "gathering information for a decision or action"
        pre_action_suggestion = "after returning results, offer to save as a note if substantial"

    elif any(w in msg for w in _SCHEDULE_WORDS):
        goal = "coordinating time — prep reminder is almost always wanted"
        follow_up_likely = True
        pre_action_suggestion = "suggest a prep reminder before or right after scheduling"

    elif any(w in msg for w in _BUILD_WORDS):
        goal = "building a repeatable system or process"
        is_part_of_workflow = True
        follow_up_likely = True
        pre_action_suggestion = "after building, offer to kick off the first step"

    # Sequential intent — part of a larger chain
    _CHAIN_SIGNALS = ["then", "after that", "next", "and then", "also", "once that", "while you're at it"]
    if any(sig in msg for sig in _CHAIN_SIGNALS):
        is_part_of_workflow = True

    # ── Continuity detection ─────────────────────────────────────────────────
    # Linguistic signals that the user is resuming or continuing from earlier
    _CONTINUITY_SIGNALS = [
        "continuing", "follow up", "following up", "next step", "as discussed",
        "from earlier", "like before", "same as last", "picking up", "resume",
        "going back to", "back to", "now do", "now send", "now create",
    ]
    is_continuation = any(sig in msg for sig in _CONTINUITY_SIGNALS) and not is_vague

    # ── Soft disagreement signals ────────────────────────────────────────────
    # Patterns where a better alternative likely exists
    _BROAD_SCOPE = ["send to all", "forward to all", "delete all", "remove all", "wipe all", "everyone"]
    _HASTY_SEND = ["just send", "just email", "just message", "quick send", "send quick"]
    has_broad_scope = any(sig in msg for sig in _BROAD_SCOPE)
    has_hasty_send = any(sig in msg for sig in _HASTY_SEND) and not is_urgent

    # ── "Do nothing" detection ───────────────────────────────────────────────
    # For trivial or conversational messages, complete the action and stop.
    _TRIVIAL_ACTIONS = {"lock", "sleep", "volume", "play", "pause", "next", "previous", "mute"}
    _CONVERSATIONAL = {
        "thanks", "thank you", "ok", "okay", "got it", "sounds good",
        "perfect", "great", "cool", "nice", "noted", "alright", "sure",
    }
    is_trivial = any(w in word_set for w in _TRIVIAL_ACTIONS) and word_count <= 5
    is_conversational = any(w in msg for w in _CONVERSATIONAL) and word_count <= 5

    if is_trivial or is_conversational:
        hints.append(
            "DO NOTHING: Trivial or conversational message. "
            "Complete the action (if any) and stop. No suggestions, no follow-through."
        )
        # Return early — no further analysis needed for trivial messages
        return "── REASONING FRAME ──\n" + "\n".join(hints)

    # ── Confidence inference ─────────────────────────────────────────────────
    # Based on how specific and well-formed the request is
    if word_count >= 10 and is_action_request and not is_ambiguous:
        confidence = "high"
        confidence_note = "Use direct language: 'best move is...', 'go with X', direct recommendation."
    elif word_count >= 6 or (is_question and not is_vague):
        confidence = "medium"
        confidence_note = "Use considered language: 'probably worth...', 'I'd lean toward...', 'my read is...'"
    elif is_vague or is_ambiguous:
        confidence = "low"
        confidence_note = "Use exploratory language: 'what's the goal here?', 'worth checking if...'"
    else:
        confidence = "medium"
        confidence_note = "Use considered language where uncertain, direct where clear."
    hints.append(f"CONFIDENCE: {confidence} — {confidence_note}")

    # ── Assemble decision hints ──────────────────────────────────────────────
    if is_vague or is_ambiguous:
        hints.append(
            "INTENT: Unclear. Ask ONE precise clarifying question before acting. "
            "Do not guess and proceed."
        )
    elif is_high_stakes:
        hints.append(
            "INTENT: High-stakes. State exactly what you're about to do, then proceed — "
            "do not act silently on a destructive operation."
        )
    elif is_action_request:
        urgency_note = " User signaled urgency — skip all preamble, act immediately." if is_urgent else ""
        hints.append(f"INTENT: Clear action request. Act directly.{urgency_note}")
    elif is_question:
        hints.append("INTENT: Information request. Lead with the direct answer, no preamble.")

    # ── Goal frame ───────────────────────────────────────────────────────────
    if goal:
        hints.append(f"GOAL: {goal}.")

    if pre_action_suggestion and not is_vague and not is_urgent:
        hints.append(f"PRE-ACTION: Blend this into your response naturally — {pre_action_suggestion}.")

    if is_part_of_workflow:
        hints.append(
            "WORKFLOW: Part of a larger sequence. "
            "Acknowledge the current step and point to what comes next — briefly."
        )

    if is_continuation:
        hints.append(
            "CONTINUITY: User is picking up from something earlier. "
            "Reference the thread: 'next step from before', 'continuing from earlier', etc. "
            "One clause only — no full recap."
        )

    if has_broad_scope:
        hints.append(
            "SOFT_DISAGREEMENT: Broad scope detected (all/everyone). "
            "Gently confirm before acting: 'Before I do that — are you sure you mean all of them, "
            "or just [specific subset]?'"
        )
    elif has_hasty_send:
        hints.append(
            "SOFT_DISAGREEMENT: User may be moving fast on a communication. "
            "If the message content seems off or incomplete, flag it briefly before sending."
        )

    if follow_up_likely and not is_vague and not is_urgent:
        hints.append(
            "FOLLOW-UP: Warranted here — fold it into the response naturally, "
            "not as a separate appended line."
        )

    # ── Context sensitivity ──────────────────────────────────────────────────
    mem = memory or {}
    name = mem.get("name", "")
    is_known_user = bool(mem)

    if name:
        hints.append(f"USER: Known as {name}. No re-introductions. Get to the point.")
    elif not is_known_user:
        hints.append("USER: New interaction. Be slightly more explicit about what you're doing.")

    if task_count >= 8:
        hints.append(
            f"WORKLOAD: Heavy ({task_count} active tasks). "
            "Prioritization > adding more. If PRIORITY is set below, surface that specific task."
        )
    elif task_count >= 5:
        hints.append(f"WORKLOAD: Moderate ({task_count} tasks). Keep suggestions to one at a time.")

    # ── Tone detection ───────────────────────────────────────────────────────
    # Low-stakes: non-urgent, non-sensitive, short and casual in nature
    _LOW_STAKES_SIGNALS = {
        "note", "song", "play", "remind", "open", "check", "look", "quick",
        "just", "small", "simple", "easy",
    }
    is_low_stakes = (
        not is_urgent
        and not is_high_stakes
        and not is_vague
        and word_count <= 8
        and any(w in word_set for w in _LOW_STAKES_SIGNALS)
    )

    hour = datetime.now().hour
    if is_urgent:
        hints.append(
            "TONE: urgent — short clauses, active verbs, zero softening. "
            "'Done.' 'Sent.' 'On it.' Cut everything else."
        )
    elif is_low_stakes:
        hints.append("TONE: low-stakes — relaxed, light. No need to be formal.")
    elif hour < 10:
        hints.append("TONE: morning — planning mode. Direct and energetic is appropriate.")
    elif hour >= 20:
        hints.append("TONE: late evening — keep it brief. User is winding down.")
    # Normal daytime context: no special tone hint — default Alfred register applies

    if not hints:
        return ""
    return "── REASONING FRAME ──\n" + "\n".join(hints)


def _build_prioritization_context(tasks: list) -> str:
    """
    Analyze the loaded task list and surface the single most urgent item.
    Injected into the system prompt so Alfred recommends specifically rather than generically.
    Returns a one-line PRIORITY hint, or empty string if nothing stands out.
    """
    if not tasks:
        return ""

    _PRIORITY_SCORE = {"urgent": 4, "high": 3, "medium": 2, "low": 1}

    scored: list = []
    for t in tasks:
        status = t.get("status", "pending") if isinstance(t, dict) else "pending"
        if status not in ("pending", "in_progress"):
            continue
        priority = t.get("priority", "medium") if isinstance(t, dict) else "medium"
        text = t.get("text", "") if isinstance(t, dict) else str(t)
        score = _PRIORITY_SCORE.get(priority, 2)
        if status == "in_progress":
            score += 0.5  # in-progress tasks get a slight boost
        if text:
            scored.append((score, priority, text))

    if not scored:
        return ""

    scored.sort(key=lambda x: x[0], reverse=True)
    top_score, top_priority, top_text = scored[0]

    # Only surface a priority hint if there's a genuinely high-priority item
    if top_score < 3:  # below "high" threshold — not worth calling out
        return ""

    # Infer a concise reason WHY this task stands out
    if top_priority == "urgent":
        reason = "marked urgent — likely has a hard deadline"
    elif top_score >= 3.5:  # high priority + already in progress
        reason = "already in progress and high priority — stopping now costs momentum"
    elif top_priority == "high":
        # Second-highest score item for contrast
        if len(scored) > 1 and scored[1][0] < 3:
            reason = "only high-priority item in the queue"
        else:
            reason = "highest priority in the current queue"
    else:
        reason = "highest priority available"

    return (
        f"PRIORITY: Top task is [{top_priority.upper()}] \"{top_text}\" ({reason}). "
        f"If relevant, mention this one with the reason — not just the name."
    )


def _maybe_append_followthrough(
    reply: str,
    results: list,
    raw_message: str = "",
    memory: dict = None,
    task_count: int = 0,
) -> str:
    """
    Append a natural follow-through suggestion when the LLM doesn't include one.

    Context-aware:
      - Skips when user signaled urgency (they want speed, not next steps)
      - Adapts phrasing for heavy workload (redirects to priority vs. add more)
      - Uses varied phrasings to avoid template-like repetition
      - Only fires once, for the highest-priority completed action
    """
    import random

    msg = (raw_message or "").lower()

    # ── Skip conditions ──────────────────────────────────────────────────────
    reply_lower = reply.lower()

    # Already contains follow-through — question or ownership statement
    if reply.rstrip().endswith("?"):
        return reply
    if any(phrase in reply_lower for phrase in [
        # question forms
        "want me to", "should i", "shall i", "would you like",
        "need me to", "let me know if", "want a reminder",
        # ownership forms (LLM already took ownership)
        "i'll flag", "i'll remind", "i'll track", "i'll follow",
        "i'll nudge", "i'll keep an eye", "keeping an eye",
        "i'll let you know", "flagging this",
    ]):
        return reply

    # User was in a hurry — don't add anything
    _URGENCY = {"urgent", "asap", "right now", "quickly", "quick", "immediately", "eod", "tonight"}
    if any(w in msg for w in _URGENCY):
        return reply

    # Trivial actions — complete and stop, never follow-through
    _TRIVIAL_ACTION_TYPES = {
        "MAC_LOCK", "MAC_SLEEP", "MAC_VOLUME", "SPOTIFY",
        "ACTIVATE_MODE", "OPEN_APP", "SAVE_NOTE", "SET_REMINDER",
    }
    successful_types = {r.action_type for r in (results or []) if r.success}
    if not successful_types:
        return reply
    if successful_types.issubset(_TRIVIAL_ACTION_TYPES):
        return reply  # all completed actions are trivial — stop here

    # ── Only fire for genuinely high-value actions ───────────────────────────
    # TASK_CREATE: handled naturally by the LLM via system prompt guidance.
    # EMAIL_SEND: worth a fallback because reply-tracking is often forgotten.
    # WORKFLOW_CREATE: always worth confirming readiness.
    _HIGH_VALUE = {"EMAIL_SEND", "WORKFLOW_CREATE"}
    high_value_hits = successful_types & _HIGH_VALUE
    if not high_value_hits:
        return reply

    # ── Context flags ────────────────────────────────────────────────────────
    is_business_context = any(w in msg for w in [
        "client", "customer", "investor", "partner", "prospect", "lead", "vendor",
    ])

    # ── Phrasing pools — ownership statements, not questions ────────────────
    # Default: non-business email follow-through
    _EMAIL_VARIANTS = [
        "I'll flag it if there's no reply in a few days.",
        "I'll nudge you if nothing comes back by end of week.",
        "Keeping an eye on it — I'll let you know if it goes quiet.",
    ]
    # Business context: more proactive ownership
    if is_business_context:
        _EMAIL_VARIANTS = [
            "I'll track this and flag you if there's no reply.",
            "I'll remind you in 3 days if you don't hear back — client emails tend to slip.",
            "On it — I'll follow up if they go quiet.",
        ]

    # Workflow: decisive ownership
    _WORKFLOW_VARIANTS = [
        "Kicking off step one now.",
        "Running step one — I'll update you when it's done.",
        "Starting the first step.",
    ]

    FOLLOWTHROUGH_POOLS = {
        "WORKFLOW_CREATE": _WORKFLOW_VARIANTS,
        "EMAIL_SEND": _EMAIL_VARIANTS,
    }

    # Fire for the highest-value completed action
    for action_type in ["WORKFLOW_CREATE", "EMAIL_SEND"]:
        if action_type in high_value_hits:
            pool = FOLLOWTHROUGH_POOLS[action_type]
            return reply + f" {random.choice(pool)}"  # inline, not a separate paragraph

    return reply


def send_whatsapp_message(user_id: str, session_id: str, recipient_hint: str, body: str) -> tuple[bool, str]:
    """Send a WhatsApp message via the connector."""
    return whatsapp_connector.send_message(user_id, session_id, recipient_hint, body)


_PREMIUM_COLD_EMAIL_PROMPT = """
COLD_EMAIL_CAMPAIGN: {"topic": "brief description of the outreach goal", "status": "request"}
COLD_EMAIL_CAMPAIGN rules (premium only):
- Use COLD_EMAIL_CAMPAIGN when the user says "run cold email campaign", "start email outreach", "send cold emails", or "launch campaign".
- This tells the user to upload their leads CSV via the Cold Email section in the dashboard.
- Never fabricate email data. Just output the tag so Alfred can redirect the user appropriately.
""".strip()


async def chat(messages, user_id, display_name=None, is_premium: bool = True):
    from datetime import datetime
    now = datetime.now()
    time_context = "Current date and time: " + now.strftime("%A, %d %B %Y, %I:%M %p")

    client = get_groq_client()  # Initialize Groq client for this request

    memory_context = await get_memory_context_async(user_id)
    system = SYSTEM_PROMPT.replace(
        "__PREMIUM_COLD_EMAIL_PLACEHOLDER__",
        _PREMIUM_COLD_EMAIL_PROMPT if is_premium else ""
    )
    if memory_context:
        system += "\n\n" + memory_context
    system += "\n\n" + time_context
    system += "\n\nCurrent mode: " + get_current_mode()

    # ── Raw user message (needed by memory retrieval below) ──────────────────
    raw_user_msg = messages[-1]["content"].strip() if messages else ""

    # ── Semantic memory injection (retrieved nodes relevant to this query) ────
    try:
        from memory_retrieval import inject_memory_context_async
        mem_retrieved = await inject_memory_context_async(user_id, raw_user_msg)
        if mem_retrieved:
            system += "\n\n" + mem_retrieved
    except Exception as e:
        logger.warning("Memory retrieval failed (non-fatal): %s", e)

    # ── Live Task & Note Awareness ──
    _task_count = 0
    _loaded_tasks: list = []  # kept for prioritization analysis
    try:
        from task_manager_v2 import get_tasks_tree_async
        tree = await get_tasks_tree_async(user_id, status_filter=["pending", "in_progress"])
        if tree:
            _task_count = len(tree)
            _loaded_tasks = tree
            task_lines = []
            for t in tree[:10]:
                priority_marker = {"urgent": "!!", "high": "!", "medium": "·", "low": "·"}.get(t.get("priority", "medium"), "·")
                task_lines.append(f"{priority_marker} [{t.get('status','')}] {t.get('text','')}")
                for sub in t.get("subtasks", [])[:3]:
                    task_lines.append(f"   └ {sub.get('text','')}")
            system += "\n\nActive Tasks:\n" + "\n".join(task_lines)
            if len(tree) > 10:
                system += f"\n...and {len(tree)-10} more."
    except Exception:
        # Fall back to legacy tasks if tasks_v2 not ready
        try:
            from tasks import load_tasks_async
            tasks = await load_tasks_async(user_id)
            pending_tasks = [t for t in tasks if not t.get("done", False)]
            _task_count = len(pending_tasks)
            _loaded_tasks = pending_tasks
            if pending_tasks:
                system += "\n\nPending Tasks:\n- " + "\n- ".join([t.get("text", "") for t in pending_tasks[:10]])
        except Exception as e:
            logger.error("Failed to inject task context: %s", e)

    try:
        from notes import get_notes_async
        notes = await get_notes_async(user_id)
        if notes:
            recent_notes = notes[-3:] if isinstance(notes, list) else []
            if recent_notes:
                system += "\n\nRecent Notes:\n- " + "\n- ".join([n.get("content", "")[:120] for n in recent_notes])
    except Exception as e:
        logger.error("Failed to inject notes context: %s", e)

    # ── Inject active workflows ───────────────────────────────────────────────
    try:
        from workflow_engine import get_active_workflows_summary_async
        wf_summary = await get_active_workflows_summary_async(user_id)
        if wf_summary:
            system += "\n\n" + wf_summary
    except Exception:
        pass

    # ── Memory extraction: silently detect & store events/tasks the user mentions ──
    _tracker_confirmation = await memory_tracker.extract_and_store_async(raw_user_msg, user_id)

    memory = await load_memory_async(user_id)
    whatsapp_session_id = whatsapp_connector.get_session_id(user_id)
    if not memory:
        system += "\n\nYou do not know the user name yet. Ask warmly on first interaction."

    # ── Cognitive frame: guides LLM reasoning without an extra API call ──────
    thinking_ctx = _build_thinking_context(raw_user_msg, memory or {}, _task_count)
    priority_ctx = _build_prioritization_context(_loaded_tasks)
    if thinking_ctx or priority_ctx:
        frame_parts = [p for p in [thinking_ctx, priority_ctx] if p]
        system += "\n\n" + "\n".join(frame_parts)

    # raw_user_msg already set above; alias for readability in bypass handlers
    raw_last_msg = raw_user_msg
    last_msg = raw_last_msg.lower()


    # Direct screen capture bypass
    if any(word in last_msg for word in ["screen", "what do you see", "what am i looking at", "whats on my screen", "analyze my screen", "read my screen", "what is this", "explain this error"]):
        try:
            img_data = capture_screen()
            from groq import Groq as _Groq
            import os as _os
            _client = _Groq(api_key=_os.getenv("GROQ_API_KEY"))
            response = _client.chat.completions.create(
                model="llama-3.2-11b-vision-preview",
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": "data:image/png;base64," + img_data
                                }
                            },
                            {
                                "type": "text",
                                "text": "You are Alfred, a sharp personal assistant. " + messages[-1]["content"]
                            }
                        ]
                    }
                ],
                max_tokens=500
            )
            return {"reply": response.choices[0].message.content, "pending_command": None}
        except Exception as e:
            return {"reply": "Could not analyze screen: " + str(e), "pending_command": None}

    # Create calendar event bypass
    if any(word in last_msg for word in ["add to calendar", "create event", "schedule a meeting", "add event", "put it on my calendar"]):
        from datetime import datetime, timedelta
        today = datetime.now().strftime("%Y-%m-%d")
        time_match = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", last_msg, re.IGNORECASE)
        time_str = "10:00"
        if time_match:
            hour = int(time_match.group(1))
            minute = time_match.group(2) or "00"
            period = time_match.group(3)
            if period and period.lower() == "pm" and hour != 12:
                hour += 12
            time_str = str(hour).zfill(2) + ":" + minute
        title = last_msg
        for word in ["add to calendar", "create event", "schedule a meeting", "add event", "put it on my calendar", "at", "tomorrow", "today"]:
            title = title.replace(word, "").strip()
        if "tomorrow" in last_msg:
            today = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        result = create_event(title.strip().title(), today, time_str)
        return {"reply": result, "pending_command": None}
    if any(word in last_msg for word in ["calendar", "schedule", "events today", "what do i have", "my day", "appointments"]):
        events = get_todays_events()
        return {"reply": "Here is your schedule for today: " + events, "pending_command": None}

    # Tasks bypass
    if any(word in last_msg for word in ["add task", "new task", "dont forget", "todo"]):
        task_text = re.sub(r"(?i)add task|new task|dont forget|todo", "", last_msg).strip()
        if task_text:
            return {"reply": await add_task_async(task_text, user_id), "pending_command": None}

    if any(word in last_msg for word in ["pending tasks", "my tasks", "what are my tasks", "task list", "what do i need to do"]):
        tasks = await get_pending_tasks_async(user_id)
        return {"reply": "Here are your pending tasks:" + chr(10) + tasks, "pending_command": None}

    if any(word in last_msg for word in ["mark as done", "complete task", "finished", "completed", "done with"]):
        task_text = re.sub(r"(?i)mark as done|complete task|finished|completed|done with", "", last_msg).strip()
        return {"reply": await complete_task_async(task_text, user_id), "pending_command": None}

    email_command = re.match(r"(?is)^\s*(?:email|mail)\s+(.+?)\s*$", raw_last_msg)
    if email_command:
        recipient_hint, body = await split_short_send_command_async(email_command.group(1), user_id, resolve_email_target_async)
        if recipient_hint and body:
            target, target_error = await resolve_email_target_async(user_id, recipient_hint)
            if not target:
                return {"reply": target_error or "I couldn't resolve that email recipient.", "pending_command": None}
            sent = send_email(
                to=target,
                subject=build_email_subject(body),
                body=body,
                user_id=user_id,
            )
            if sent:
                return {"reply": f"Email sent to {recipient_hint}.", "pending_command": None}
            return {"reply": "Email could not be sent. Check your email account connection in Settings.", "pending_command": None}

    whatsapp_command = re.match(r"(?is)^\s*(?:whatsapp|message|msg|text)\s+(.+?)\s*$", raw_last_msg)
    if whatsapp_command:
        recipient_hint, body = await split_short_send_command_async(whatsapp_command.group(1), user_id, resolve_whatsapp_target_async)
        if recipient_hint and body:
            try:
                _, reply_text = send_whatsapp_message(user_id, whatsapp_session_id, recipient_hint, body)
                return {"reply": reply_text, "pending_command": None}
            except Exception as e:
                return {"reply": "Could not send WhatsApp message: " + str(e), "pending_command": None}

    # Direct stock price bypass
    if any(word in last_msg for word in ["stock", "share price", "trading at", "market cap"]):
        ticker_match = re.search(r'\b(AAPL|GOOGL|MSFT|TSLA|AMZN|META|NVDA|apple|google|microsoft|tesla|amazon|meta|nvidia)\b', last_msg, re.IGNORECASE)
        ticker_map = {
            "apple": "AAPL", "google": "GOOGL", "microsoft": "MSFT",
            "tesla": "TSLA", "amazon": "AMZN", "meta": "META", "nvidia": "NVDA"
        }
        if ticker_match:
            ticker = ticker_match.group(1).lower()
            symbol = ticker_map.get(ticker, ticker.upper())
            return {"reply": get_stock_price(symbol), "pending_command": None}

    # Direct weather bypass
    if any(word in last_msg for word in ["weather", "temperature", "forecast", "raining", "sunny"]):
        return {"reply": await get_weather_async(user_id), "pending_command": None}

    # Morning briefing bypass
    if any(word in last_msg for word in ["morning briefing", "brief me", "wake up", "good morning", "start my day", "whats happening today", "good evening", "good afternoon", "good night"]):
        events = get_todays_events()
        briefing = await morning_briefing_async(user_id)
        return {"reply": briefing + chr(10) + chr(10) + "Calendar: " + events, "pending_command": None}

    # End of day bypass
    if any(word in last_msg for word in ["how was my day", "end of day", "day review", "wrap up"]):
        return {"reply": end_of_day(), "pending_command": None}

    # News bypass
    if any(word in last_msg for word in ["news", "headlines", "what happened", "whats happening"]):
        topic = re.sub(r"(?i)give me|what.s|the|latest|news|headlines|what|happened|happening", "", last_msg).strip()
        if not topic or len(topic) < 4:
            topic = "entrepreneurship startups AI business India latest news"
        else:
            topic = topic + " news 2026"
        results = await get_news_async(user_id, topic=topic)
        def _call_groq_news():
            return client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": "You are Alfred. Summarize these news results in 3-4 sharp sentences."},
                    {"role": "user", "content": "News results:" + chr(10) + results}
                ],
                temperature=0.7,
            )
        loop = asyncio.get_event_loop()
        followup = await loop.run_in_executor(None, _call_groq_news)
        return {"reply": followup.choices[0].message.content, "pending_command": None}

    # Stocks bypass
    if any(word in last_msg for word in ["stock", "market", "sensex", "nifty", "shares"]):
        ticker_match = re.search(r"\b(AAPL|GOOGL|MSFT|TSLA|AMZN|META|NVDA|apple|google|microsoft|tesla|amazon|meta|nvidia)\b", last_msg, re.IGNORECASE)
        if ticker_match:
            ticker_map = {"apple": "AAPL", "google": "GOOGL", "microsoft": "MSFT", "tesla": "TSLA", "amazon": "AMZN", "meta": "META", "nvidia": "NVDA"}
            ticker = ticker_match.group(1).lower()
            symbol = ticker_map.get(ticker, ticker.upper())
            return {"reply": get_stock_price(symbol), "pending_command": None}
        return {"reply": await get_stocks_async(user_id), "pending_command": None}

    # Direct currency bypass
    if any(word in last_msg for word in ["convert", "exchange rate", "to rupees", "to dollars", "to euros"]):
        amount_match = re.search(r"(\d+(?:\.\d+)?)", last_msg)
        from_match = re.search(r"(usd|dollar|dollars|inr|rupee|rupees|eur|euro|euros|gbp|pound|pounds)", last_msg)
        to_match = re.search(r"to\s+(usd|dollar|dollars|inr|rupee|rupees|eur|euro|euros|gbp|pound|pounds)", last_msg)
        currency_map = {
            "dollar": "USD", "dollars": "USD", "usd": "USD",
            "rupee": "INR", "rupees": "INR", "inr": "INR",
            "euro": "EUR", "euros": "EUR", "eur": "EUR",
            "pound": "GBP", "pounds": "GBP", "gbp": "GBP"
        }
        if amount_match and from_match and to_match:
            amount = float(amount_match.group(1))
            from_cur = currency_map.get(from_match.group(1), "USD")
            to_cur = currency_map.get(to_match.group(1).strip(), "INR")
            return {"reply": convert_currency(amount, from_cur, to_cur), "pending_command": None}

    # Direct web search bypass
    if any(word in last_msg for word in ["search", "latest", "news", "whats happening", "when did", "when was", "who is", "what happened", "did", "perform", "concert", "event", "match", "score", "launch", "release"]):
        query = messages[-1]["content"]
        query = re.sub(r"(?i)search for|search|find|look up", "", query).strip()
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, web_search, query)
        def _call_groq_search():
            return client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": "You are Alfred. Summarize these search results in 2-3 sharp sentences."},
                    {"role": "user", "content": "Search results for " + query + ":\n" + results}
                ],
                temperature=0.7,
            )
        followup = await loop.run_in_executor(None, _call_groq_search)
        return {"reply": followup.choices[0].message.content, "pending_command": None}

    # ── Busy / I am busy → auto-reply to all recent individual senders ─────────
    if any(phrase in last_msg for phrase in [
        "i am busy", "i'm busy", "tell them i'm busy", "tell them i am busy",
        "reply that i'm busy", "reply i am busy", "send busy", "busy mode"
    ]):
        user_label = display_name or user_id
        busy_msg = f"Hey! {user_label} is currently busy and will get back to you soon. 🙏"
        try:
            async with httpx.AsyncClient() as client_http:
                wa_res = await client_http.get(
                    f"{WHATSAPP_BRIDGE_URL}/messages/individual",
                    params={"limit": 10, "sessionId": whatsapp_session_id},
                    timeout=10,
                )
                individuals = wa_res.json()
                replied_to = []
                for m in individuals:
                    chat_id = m.get("chatId", "")
                    sender  = m.get("from", "someone")
                    if not chat_id:
                        continue
                    try:
                        await client_http.post(f"{WHATSAPP_BRIDGE_URL}/reply",
                                   json={"chatId": chat_id, "message": busy_msg, "sessionId": whatsapp_session_id},
                                   timeout=8)
                        replied_to.append(sender)
                    except Exception:
                        pass
            if replied_to:
                return {"reply": "Done. Told " + ", ".join(replied_to[:5]) + " that you're busy.", "pending_command": None}
            return {"reply": "No recent individual messages to reply to.", "pending_command": None}
        except Exception as e:
            return {"reply": "Could not send busy replies: " + str(e), "pending_command": None}

    # ── "Any message from [name]?" — contact-specific lookup ──────────────────
    contact_query = re.search(
        r"(?:any\s+)?(?:message|messages|msg|msgs|text|texts|dm|dms)\s+from\s+(.+?)(?:\?|$|\.)",
        last_msg, re.IGNORECASE
    )
    if contact_query:
        name_query = contact_query.group(1).strip().lower()
        try:
            contacts = await load_contacts_async(user_id)
            named_matches = await find_contacts_by_query_async(user_id, name_query)
            matched_numbers = {
                normalize_phone_number(extract_phone_from_chat_id(contact.get("whatsapp_chat_id")) or contact.get("phone", ""))
                for contact in named_matches
            }
            async with httpx.AsyncClient() as client_http:
                wa_res = await client_http.get(
                    f"{WHATSAPP_BRIDGE_URL}/messages/individual",
                    params={"limit": 20, "sessionId": whatsapp_session_id},
                    timeout=10,
                )
                individuals = wa_res.json()
                matched = [
                    m for m in individuals
                    if name_query in m.get("from", "").lower()
                    or normalize_phone_number(extract_phone_from_chat_id(m.get("chatId"))) in matched_numbers
                    or any(name_query in contact.get("name", "").lower() and normalize_phone_number(extract_phone_from_chat_id(m.get("chatId"))) == normalize_phone_number(contact.get("phone", ""))
                           for contact in contacts)
                ]
            if matched:
                lines = [f"\"{m['message']}\" — {m['from']} at {m['time']}" for m in matched[:3]]
                summary = "\n".join(lines)
                def _call_groq_wa():
                    return client.chat.completions.create(
                        model="llama-3.3-70b-versatile",
                        messages=[
                            {"role": "system", "content": "You are Alfred. Report these WhatsApp messages from the contact the user asked about, briefly and clearly."},
                            {"role": "user", "content": f"Messages from {name_query}:\n{summary}"}
                        ],
                        temperature=0.7,
                    )
                loop = asyncio.get_event_loop()
                followup = await loop.run_in_executor(None, _call_groq_wa)
                return {"reply": followup.choices[0].message.content, "pending_command": None}
            else:
                return {"reply": f"No recent messages from {name_query.title()} in your individual chats.", "pending_command": None}
        except Exception as e:
            return {"reply": "Could not check messages: " + str(e), "pending_command": None}

    # ── General WhatsApp reading (individual chats only) ──────────────────────
    if any(word in last_msg for word in ["whatsapp messages", "read whatsapp", "any messages", "check whatsapp"]):
        try:
            async with httpx.AsyncClient() as client_http:
                wa_res = await client_http.get(
                    f"{WHATSAPP_BRIDGE_URL}/messages/individual",
                    params={"limit": 8, "sessionId": whatsapp_session_id},
                    timeout=10,
                )
                individuals = wa_res.json()
            if individuals:
                lines = [f"{m['from']} said: \"{m['message']}\" at {m['time']}" for m in individuals]
                summary = "\n".join(lines)
                def _call_groq_wa_gen():
                    return client.chat.completions.create(
                        model="llama-3.3-70b-versatile",
                        messages=[
                            {"role": "system", "content": "You are Alfred. Brief the user on these individual WhatsApp messages — skip groups. Be concise."},
                            {"role": "user", "content": "Recent individual WhatsApp messages:\n" + summary}
                        ],
                        temperature=0.7,
                    )
                loop = asyncio.get_event_loop()
                followup = await loop.run_in_executor(None, _call_groq_wa_gen)
                return {"reply": followup.choices[0].message.content, "pending_command": None}
            else:
                return {"reply": "No new individual WhatsApp messages.", "pending_command": None}
        except Exception as e:
            return {"reply": "Could not fetch WhatsApp messages.", "pending_command": None}

    # Direct email reading bypass
    if "READ_EMAILS" in last_msg or any(word in last_msg for word in ["read my emails", "check my emails", "any emails"]):
        emails = get_unread_emails(user_id=user_id)
        if emails:
            # Check if user is asking about a specific person — if so include body
            specific_person = re.search(
                r"(?:from|by|sent by)\s+([a-z][a-z\s]{1,30}?)(?:\?|$|\s+email|\s+message)",
                last_msg, re.IGNORECASE
            )
            lines = []
            for e in emails:
                sender = e["from"].split("<")[0].strip() if "<" in e["from"] else e["from"]
                if specific_person:
                    name = specific_person.group(1).strip().lower()
                    if name in sender.lower() or name in e.get("from", "").lower():
                        # User explicitly asked about this person — include body (up to 800 chars)
                        lines.append(f"From {sender}: {e['subject']}\n{e.get('body','')[:800]}")
                    else:
                        continue  # skip emails not from the requested person
                else:
                    # General scan — subject only for privacy
                    lines.append(f"From {sender}: {e['subject']}")

            if not lines and specific_person:
                return {"reply": f"No unread emails from {specific_person.group(1).strip().title()}.", "pending_command": None}

            summary = "\n\n".join(lines)
            system_msg = (
                "You are Alfred. The user asked about a specific person's email — summarise what they said and what action is needed."
                if specific_person else
                "You are Alfred. Based only on sender names and subject lines, give a sharp 3-4 sentence briefing of what needs attention. Do not reference email body content."
            )
            def _call_groq_email():
                return client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=[
                        {"role": "system", "content": system_msg},
                        {"role": "user", "content": "Emails:\n" + summary}
                    ],
                    temperature=0.7,
                )
            loop = asyncio.get_event_loop()
            followup = await loop.run_in_executor(None, _call_groq_email)
            return {"reply": followup.choices[0].message.content, "pending_command": None}
        else:
            return {"reply": "Your inbox is clear. No unread emails.", "pending_command": None}

    _is_send_intent = any(word in last_msg for word in ["whatsapp", "send", "message", "text", "email"])
    if _is_send_intent:
        contacts = await get_contact_directory_async(user_id)

        # If contacts list is empty and WhatsApp is connected, try a quick sync first.
        if not contacts and any(word in last_msg for word in ["whatsapp", "send", "message", "text"]):
            try:
                _session_id = whatsapp_connector.get_session_id(user_id)
                _synced = await whatsapp_connector.sync_contacts_async(user_id, _session_id)
                if _synced.get("imported", 0) + _synced.get("updated", 0) > 0:
                    contacts = await get_contact_directory_async(user_id)
            except Exception:
                pass  # Sync failed (WhatsApp not connected) — continue with empty list

        if contacts:
            # Limit to 50 contacts; include name + phone so LLM can reason about it
            contact_lines = [
                f"{c['name']}" + (f" ({c['phone']})" if c.get('phone') else "")
                for c in contacts[:50]
            ]
            system += "\n\nContacts:\n" + "\n".join(contact_lines)
        else:
            # Explicitly tell the LLM the list is empty so it asks for the number
            system += "\n\nContacts: (none saved yet — ask the user for the phone number)"
    if any(word in last_msg for word in ["note", "notes", "saved"]):
        notes = await get_notes_async(user_id)
        if notes:
            system += "\n\nSaved notes: " + json.dumps(notes[-5:])

    def _sanitize_msgs(msgs):
        """Strip any email body content that may have been injected into chat history."""
        safe = []
        for m in msgs:
            content = m.get("content", "")
            # Remove anything that looks like an email body block (From:/.../Body: pattern)
            content = re.sub(
                r"(email body|full body|body content)\s*[:\-].*",
                "[email body redacted for privacy]",
                content, flags=re.IGNORECASE | re.DOTALL
            )
            safe.append({**m, "content": content})
        return safe

    def _call_groq(msgs):
        return client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": system}] + _sanitize_msgs(msgs),
            temperature=0.7,
        )

    try:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, _call_groq, messages)
    except Exception as exc:
        # Groq 413 = context too large.  Trim to last 6 messages and retry once.
        err_str = str(exc)
        if "413" in err_str or "request_too_large" in err_str or "tokens" in err_str.lower():
            try:
                response = await loop.run_in_executor(None, _call_groq, messages[-6:])
            except Exception as retry_exc:
                raise retry_exc
        else:
            raise

    reply = response.choices[0].message.content
    logger.debug("LLM raw reply: %.200s", reply)

    # ── NEW: Multi-action DAG execution engine ────────────────────────────────
    from multi_action_parser import parse_and_execute
    plan_result = await parse_and_execute(
        reply, user_id, whatsapp_session_id, display_name
    )

    # ── Special-case: PPTX reply override ────────────────────────────────────
    if plan_result.pending_pptx_topic and not plan_result.reply.strip():
        plan_result = plan_result.model_copy(update={
            "reply": f'Building your presentation on \u201c{plan_result.pending_pptx_topic}\u201d \u2014 one moment\u2026'
        })

    # ── Async memory extraction (fire-and-forget, non-blocking) ──────────────
    try:
        from memory_retrieval import extract_and_store_async as _mem_extract
        asyncio.ensure_future(_mem_extract(raw_last_msg, plan_result.reply, user_id))
    except Exception:
        pass

    # ── Follow-up tracker & activity recording ────────────────────────────────
    await memory_tracker.update_status_from_reply_async(raw_last_msg, user_id)
    try:
        from proactive_worker import _record_last_activity
        await _record_last_activity(user_id)
    except Exception:
        pass

    final_reply = plan_result.reply

    # ── Follow-through: append natural next-step suggestion if LLM didn't ────
    # Only when no HITL gate is pending (user needs to approve first)
    if not plan_result.pending_hitl:
        final_reply = _maybe_append_followthrough(
            final_reply, plan_result.results, raw_last_msg, memory, _task_count
        )

    if _tracker_confirmation:
        final_reply = final_reply + "\n\n" + _tracker_confirmation

    return {
        "reply": final_reply,
        "pending_command": plan_result.pending_command,
        "pending_pptx_topic": plan_result.pending_pptx_topic,
        "pending_hitl": plan_result.pending_hitl,
    }
