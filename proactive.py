import threading
import time
from datetime import datetime
from platform_utils import speak_text

def speak(text):
    speak_text(text)

def get_briefing(user_id: str):
    try:
        from briefing import morning_briefing
        from calendar_helper import get_todays_events
        from tasks import get_pending_tasks
        brief = morning_briefing(user_id)
        events = get_todays_events()
        tasks = get_pending_tasks(user_id)
        return brief + " Your calendar: " + events + " Your tasks: " + tasks
    except Exception as e:
        return "Good morning. Salvatore is ready."

def morning_check(user_id: str):
    speak("Good morning. Here is your morning briefing.")
    time.sleep(2)
    briefing = get_briefing(user_id)
    speak(briefing)

def midday_check(user_id: str):
    from tasks import get_pending_tasks
    tasks = get_pending_tasks(user_id)
    speak("Good afternoon. Just checking in. Here are your pending tasks: " + tasks + " How is the day going?")

def evening_check(user_id: str):
    from tasks import get_pending_tasks
    tasks = get_pending_tasks(user_id)
    speak("Good evening. Here is what is still pending: " + tasks + " What would you like to wrap up before end of day?")

def goodnight_check():
    speak("Goodnight. Great work today. Salvatore will be here when you wake up.")

def check_and_suggest(user_id: str):
    try:
        from suggestions import get_contextual_suggestion
        suggestion = get_contextual_suggestion(user_id)
        if suggestion:
            speak(suggestion)
    except Exception as e:
        print("Suggestion error: " + str(e))

def start_proactive_schedule(user_id: str):
    def scheduler():
        while True:
            now = datetime.now()
            hour = now.hour
            minute = now.minute

            # Check for suggestions every 30 minutes
            if minute in [0, 30]:
                threading.Thread(target=check_and_suggest, args=(user_id,), daemon=True).start()

            if hour == 9 and minute == 0:
                threading.Thread(target=morning_check, args=(user_id,), daemon=True).start()
            elif hour == 13 and minute == 0:
                threading.Thread(target=midday_check, args=(user_id,), daemon=True).start()
            elif hour == 18 and minute == 0:
                threading.Thread(target=evening_check, args=(user_id,), daemon=True).start()
            elif hour == 22 and minute == 0:
                threading.Thread(target=goodnight_check, daemon=True).start()

            time.sleep(60)

    thread = threading.Thread(target=scheduler, daemon=True)
    thread.start()
    print("Proactive schedule started.")
