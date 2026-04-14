import threading

from platform_utils import show_notification, speak_text

reminders = []

def set_reminder(message: str, minutes: int, speak_fn=None):
    def trigger():
        print(f"REMINDER: {message}")
        show_notification("Salvatore Reminder", message)
        speak_text(message)

    timer = threading.Timer(minutes * 60, trigger)
    timer.daemon = True
    timer.start()
    reminders.append({"message": message, "minutes": minutes})
    return f"Reminder set for {minutes} minute{'s' if minutes != 1 else ''} from now."
