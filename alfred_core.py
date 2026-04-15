import re
from typing import Union, Optional, List, Dict, Any, Tuple, Literal
from groq import Groq
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

SYSTEM_PROMPT = """You are Alfred, a sharp, deeply personal AI assistant, part chief of staff, part Jarvis.
You speak with calm authority, concise, warm, and occasionally dry.
Never be generic. Be Alfred.
Keep responses under 3 sentences unless asked for detail.

When the user says wake up, give a sharp briefing and ask what they are doing today.

ACTION TAGS, always put at the very end of your reply, one tag only:

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
RUN_TERMINAL: {"command": "command to run"} (Use this to create folders, manage files, or run any shell commands on the user's Mac. Note: Mac uses 'Finder', not 'File Explorer'.)
WEB_SEARCH: {"query": "exact search term", "engine": "free"} (Use engine "tavily" ONLY for specific live sports, match schedules, or when deep context from complex sites is required. Otherwise, ALWAYS use "free" for normal lookups.)

PPTX_CREATE rules: Use this when the user asks Alfred to "make a presentation", "create a deck", "create slides", or "generate a PPT" on any topic. Put the full topic/outline in "topic". Alfred will build and return a downloadable PowerPoint automatically. Do not describe how to do it — just output the tag.
To read and explain a document: tell the user to use the 📎 attach button and send the file. Alfred can read PDFs, Word docs, PowerPoint files, code, and images.

__PREMIUM_COLD_EMAIL_PLACEHOLDER__

WHATSAPP_SEND rules — read carefully:
- ONLY use WHATSAPP_SEND if: (a) the user gave a full phone number directly, OR (b) the recipient name is clearly present in the "Contacts:" section below.
- If the Contacts section is empty, or the recipient name is NOT in the list, do NOT use WHATSAPP_SEND. Instead, reply asking the user: "I don't have [name]'s number saved. What's their WhatsApp number?"
- The "number" field must be the exact name from the Contacts list (e.g. "Mum", "Rahul Sharma") or a real phone number. NEVER put "unknown", "n/a", a guess, or a placeholder.

If the user gives a direct short instruction like "message Rahul I'm running late" or "email Priya the deck is ready", act immediately with the correct send tag only if the contact exists.
Prefer WhatsApp for message/text/whatsapp requests and email for email/mail requests.
Only use a tag when explicitly asked.
CRITICAL: If you are asked a factual question, a query about current events (sports, news, schedules, prices), or anything you do not know the exact answer to, you MUST use the WEB_SEARCH tag! Do NOT use RUN_TERMINAL for searching the internet. Use RUN_TERMINAL exclusively for local macOS file operations, system setting changes, or running local scripts."""


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

    # ── Memory extraction: silently detect & store events/tasks the user mentions ──
    raw_user_msg = messages[-1]["content"].strip() if messages else ""
    _tracker_confirmation = await memory_tracker.extract_and_store_async(raw_user_msg, user_id)

    memory = await load_memory_async(user_id)
    whatsapp_session_id = whatsapp_connector.get_session_id(user_id)
    if not memory:
        system += "\n\nYou do not know the user name yet. Ask warmly on first interaction."

    raw_last_msg = messages[-1]["content"].strip() if messages else ""
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
    print("FULL REPLY: " + reply)

    clean_reply, memory_update = parse_tag(reply, "MEMORY_UPDATE")
    if memory_update:
        for key, value in memory_update.items():
            await update_memory_async(key, value, user_id)

    clean_reply, mode_data = parse_tag(clean_reply, "ACTIVATE_MODE")
    if mode_data:
        activate_mode(mode_data.get("mode", ""))

    # ── Presentation creation (file generated by the caller in main.py) ──────
    clean_reply, pptx_data = parse_tag(clean_reply, "PPTX_CREATE")
    pending_pptx_topic: Optional[str] = None
    if pptx_data and isinstance(pptx_data, dict):
        topic = str(pptx_data.get("topic", "")).strip()
        if topic:
            pending_pptx_topic = topic
            # Replace the LLM's reply with a crisp one-liner (the download button
            # will appear automatically once main.py generates the file).
            clean_reply = clean_reply or f'Building your presentation on \u201c{topic}\u201d \u2014 one moment\u2026'

    clean_reply, wa_data = parse_tag(clean_reply, "WHATSAPP_SEND")
    if wa_data:
        wa_number = str(wa_data.get("number", "") or "").strip()
        wa_body   = str(wa_data.get("message", "") or "").strip()
        # Guard: ignore the tag when the LLM emits a known placeholder value
        _PLACEHOLDERS = {"unknown", "n/a", "na", "none", "number", "phone", "contact name",
                         "exact name from contacts list", "recipient", "name", ""}
        if wa_number.lower() in _PLACEHOLDERS:
            # LLM couldn't resolve the contact — silently drop the broken tag so the
            # user only sees the LLM's polite "I don't have that number" reply text.
            pass
        elif not wa_body:
            clean_reply += "\n\n(WhatsApp message body was empty — nothing sent.)"
        else:
            try:
                sent, message = send_whatsapp_message(
                    user_id,
                    whatsapp_session_id,
                    wa_number,
                    wa_body,
                )
                if not sent:
                    err = message or ""
                    if "detached Frame" in err or "context" in err.lower() or "503" in err or "connection lost" in err.lower():
                        clean_reply += "\n\nWhatsApp connection dropped. Go to Settings → WhatsApp and hit Refresh to reconnect, then try again."
                    else:
                        clean_reply += "\n\n(" + err + ")"
                else:
                    print("WhatsApp result: " + message)
            except Exception as e:
                err = str(e)
                print("WhatsApp error: " + err)
                if "detached Frame" in err or "context" in err.lower() or "503" in err:
                    clean_reply += "\n\nWhatsApp connection dropped. Go to Settings → WhatsApp and hit Refresh to reconnect, then try again."
                else:
                    clean_reply += "\n\n(WhatsApp message could not be sent.)"

    clean_reply, email_data = parse_tag(clean_reply, "EMAIL_SEND")
    if email_data:
        try:
            target, target_error = await resolve_email_target_async(user_id, email_data.get("to", ""))
            if not target:
                clean_reply += "\n\n(" + (target_error or "Email recipient could not be resolved.") + ")"
                return {"reply": clean_reply, "pending_command": None}
            sent = send_email(
                to=target,
                subject=email_data.get("subject"),
                body=email_data.get("body"),
                user_id=user_id,
            )
            if not sent:
                clean_reply += "\n\n(Email could not be sent — check your email account in Settings.)"
        except Exception as e:
            print("Email error: " + str(e))
            clean_reply += "\n\n(Email could not be sent — check your email account in Settings.)"

    clean_reply, app_data = parse_tag(clean_reply, "OPEN_APP")
    if app_data and is_explicit_app_launch_request(last_msg):
        open_app(app_data.get("app"))

    clean_reply, note_data = parse_tag(clean_reply, "SAVE_NOTE")
    if note_data:
        await save_note_async(note_data.get("content", ""), user_id)

    clean_reply, reminder_data = parse_tag(clean_reply, "SET_REMINDER")
    if reminder_data:
        set_reminder(
            message=reminder_data.get("message", ""),
            minutes=int(reminder_data.get("minutes", 5))
        )

    clean_reply, spotify_data = parse_tag(clean_reply, "SPOTIFY")
    if spotify_data:
        action = spotify_data.get("action")
        query = spotify_data.get("query", "")
        if action == "volume":
            set_spotify_volume(int(spotify_data.get("level", 50)))
        elif action == "play" and query:
            play_song(query)
        else:
            spotify_command(action)

    clean_reply, vol_data = parse_tag(clean_reply, "MAC_VOLUME")
    if vol_data:
        set_volume(int(vol_data.get("level", 50)))

    clean_reply, lock_data = parse_tag(clean_reply, "MAC_LOCK")
    if lock_data is not None:
        lock_mac()

    clean_reply, sleep_data = parse_tag(clean_reply, "MAC_SLEEP")
    if sleep_data is not None:
        sleep_mac()

    clean_reply, term_data = parse_tag(clean_reply, "RUN_TERMINAL")
    pending_cmd = None
    if term_data:
        cmd = term_data.get("command")
        if cmd:
            pending_cmd = cmd
            # We no longer execute here. We return it for the user to approve.
            clean_reply += "\n\n(I've prepared a terminal command for this. Please approve it on your dashboard.)"

    clean_reply, web_search_data = parse_tag(clean_reply, "WEB_SEARCH")
    if web_search_data:
        query = web_search_data.get("query", "")
        engine = web_search_data.get("engine", "free")
        if query:
            loop = asyncio.get_event_loop()
            import os
            if engine == "tavily" and os.getenv("TAVILY_API_KEY"):
                def _do_tavily_search():
                    from tavily import TavilyClient
                    client = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))
                    # qna_search gives a highly synthesized short answer, perfect for specific QNs
                    return client.qna_search(query=query)
                try:
                    results = await loop.run_in_executor(None, _do_tavily_search)
                except Exception:
                    results = await loop.run_in_executor(None, web_search, query)
            else:
                results = await loop.run_in_executor(None, web_search, query)
            def _call_groq_search():
                return client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=[
                        {"role": "system", "content": "You are Alfred. Use the search results to answer the user's latest query accurately and concisely (2 sentences max). Be direct and conversational."},
                        {"role": "user", "content": f"User's request: {raw_last_msg}\n\nSearch Results for '{query}':\n{results}"}
                    ],
                    temperature=0.7,
                )
            followup = await loop.run_in_executor(None, _call_groq_search)
            clean_reply = followup.choices[0].message.content

    # ── Status update: check if user is responding to a follow-up Alfred sent ──
    await memory_tracker.update_status_from_reply_async(raw_last_msg, user_id)

    # Append tracker confirmation to reply if Alfred stored something new
    if _tracker_confirmation:
        clean_reply = clean_reply + "\n\n" + _tracker_confirmation

    return {
        "reply": clean_reply,
        "pending_command": pending_cmd,
        "pending_pptx_topic": pending_pptx_topic,
    }
