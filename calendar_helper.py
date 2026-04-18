import base64
import json
import os
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Union

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar"]


def _load_json_env(name: str) -> Optional[dict]:
    raw = os.getenv(name)
    if raw:
        return json.loads(raw)
    encoded = os.getenv(f"{name}_B64")
    if encoded:
        return json.loads(base64.b64decode(encoded).decode("utf-8"))
    return None


def _load_json_file(env_name: str) -> Optional[dict]:
    path = os.getenv(env_name)
    if path and os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return None


def _persist_token_if_configured(creds: Credentials) -> None:
    token_path = os.getenv("GOOGLE_CALENDAR_TOKEN_FILE")
    if not token_path:
        return
    os.makedirs(os.path.dirname(os.path.abspath(token_path)), exist_ok=True)
    with open(token_path, "w") as f:
        f.write(creds.to_json())


def _build_flow():
    credentials_json = _load_json_env("GOOGLE_CALENDAR_CREDENTIALS_JSON")
    if credentials_json:
        return InstalledAppFlow.from_client_config(credentials_json, SCOPES)

    credentials_path = os.getenv("GOOGLE_CALENDAR_CREDENTIALS_FILE")
    if credentials_path and os.path.exists(credentials_path):
        return InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)

    raise RuntimeError(
        "Google Calendar credentials are not configured. "
        "Set GOOGLE_CALENDAR_CREDENTIALS_JSON(_B64) or GOOGLE_CALENDAR_CREDENTIALS_FILE."
    )


def get_service():
    creds = None
    token_json = _load_json_env("GOOGLE_CALENDAR_TOKEN_JSON") or _load_json_file("GOOGLE_CALENDAR_TOKEN_FILE")
    if token_json:
        creds = Credentials.from_authorized_user_info(token_json, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = _build_flow()
            # USE A FIXED PORT to avoid Error 104 (Connection Reset) or random blocking.
            # Default to 9005. Make sure this is added to Authorized Redirect URIs in Google Console.
            port = int(os.getenv("GOOGLE_CALENDAR_AUTH_PORT", 9005))
            print(f"Starting Google Calendar auth server on http://localhost:{port}")
            creds = flow.run_local_server(port=port, prompt="Alfred needs access to your Google Calendar. Please authorize here: ")
        _persist_token_if_configured(creds)

    return build("calendar", "v3", credentials=creds)


def get_todays_events():
    try:
        service = get_service()
        now = datetime.utcnow()
        start = now.replace(hour=0, minute=0, second=0).isoformat() + "Z"
        end = now.replace(hour=23, minute=59, second=59).isoformat() + "Z"
        events_result = service.events().list(
            calendarId="primary",
            timeMin=start,
            timeMax=end,
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        events = events_result.get("items", [])
        if not events:
            return "No events today."
        result = []
        for event in events:
            start_time = event["start"].get("dateTime", event["start"].get("date"))
            result.append(event["summary"] + " at " + start_time)
        return "\n".join(result)
    except Exception as e:
        return "Could not fetch calendar: " + str(e)


def create_event(title, date_str, time_str="10:00", duration_hours=1):
    try:
        service = get_service()
        start_dt = datetime.strptime(date_str + " " + time_str, "%Y-%m-%d %H:%M")
        end_dt = start_dt + timedelta(hours=duration_hours)
        event = {
            "summary": title,
            "start": {"dateTime": start_dt.isoformat(), "timeZone": "Asia/Kolkata"},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": "Asia/Kolkata"},
        }
        service.events().insert(calendarId="primary", body=event).execute()
        return "Event created: " + title + " on " + date_str + " at " + time_str
    except Exception as e:
        return "Could not create event: " + str(e)
