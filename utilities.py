import subprocess
import threading
import time
import re
import ast
import operator
import sys
import os
from datetime import datetime
from platform_utils import open_uri, show_notification, speak_text
from security_utils import get_safe_user_path

def get_user_file(filename: str, user_id: str) -> str:
    path = get_safe_user_path(user_id, filename)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path

def set_alarm(hour: int, minute: int, label: str = "Alarm"):
    def trigger():
        now = datetime.now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target < now:
            from datetime import timedelta
            target = target + timedelta(days=1)
        wait = (target - now).total_seconds()
        time.sleep(wait)
        for _ in range(5):
            show_notification("Salvatore Alarm", label)
            speak_text(label)
            time.sleep(3)
    threading.Thread(target=trigger, daemon=True).start()
    return f"Alarm set for {hour:02d}:{minute:02d} — {label}"

def translate_text(text: str, target_lang: str = "Hindi") -> str:
    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.translate(text, to=target_lang))
            if results:
                return f"{text} in {target_lang} is: {results[0]['translated']}"
        return f"Could not translate to {target_lang}."
    except Exception as e:
        try:
            import httpx
            res = httpx.get(
                f"https://api.mymemory.translated.net/get",
                params={"q": text, "langpair": f"en|{target_lang[:2].lower()}"},
                timeout=5
            )
            data = res.json()
            translation = data["responseData"]["translatedText"]
            return f"{text} in {target_lang} is: {translation}"
        except:
            return f"Could not translate right now."

def set_timer(seconds: int, label: str = "Timer") -> str:
    def trigger():
        time.sleep(seconds)
        show_notification("Salvatore Timer", f"{label} is done!")
        speak_text(f"{label} is done!")
    threading.Thread(target=trigger, daemon=True).start()
    mins = seconds // 60
    secs = seconds % 60
    if mins > 0:
        return f"Timer set for {mins} minute{'s' if mins != 1 else ''} {secs} seconds."
    return f"Timer set for {secs} seconds."

def convert_units(amount: float, from_unit: str, to_unit: str) -> str:
    conversions = {
        ("kg", "lbs"): 2.20462, ("lbs", "kg"): 0.453592,
        ("km", "miles"): 0.621371, ("miles", "km"): 1.60934,
        ("cm", "inches"): 0.393701, ("inches", "cm"): 2.54,
        ("m", "feet"): 3.28084, ("feet", "m"): 0.3048,
        ("celsius", "fahrenheit"): None, ("fahrenheit", "celsius"): None,
        ("liters", "gallons"): 0.264172, ("gallons", "liters"): 3.78541,
    }
    from_u = from_unit.lower().strip()
    to_u = to_unit.lower().strip()

    if (from_u, to_u) == ("celsius", "fahrenheit"):
        result = (amount * 9/5) + 32
        return f"{amount}°C = {result:.1f}°F"
    if (from_u, to_u) == ("fahrenheit", "celsius"):
        result = (amount - 32) * 5/9
        return f"{amount}°F = {result:.1f}°C"

    factor = conversions.get((from_u, to_u))
    if factor:
        result = amount * factor
        return f"{amount} {from_unit} = {result:.2f} {to_unit}"
    return f"Cannot convert {from_unit} to {to_unit}."

def define_word(word: str) -> str:
    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.text(f"define {word} meaning", max_results=2))
        if results:
            return f"{word}: {results[0]['body'][:200]}"
        return f"Could not find definition for {word}."
    except:
        return f"Could not fetch definition right now."

_SAFE_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

def _safe_eval(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _SAFE_OPS:
        return _SAFE_OPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _SAFE_OPS:
        return _SAFE_OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError("Unsupported expression")

def calculate(expression: str) -> str:
    try:
        cleaned = expression.replace('^', '**')
        tree = ast.parse(cleaned, mode='eval')
        result = _safe_eval(tree.body)
        return f"{expression} = {result}"
    except Exception:
        return f"Could not calculate that."

def whatsapp_call(contact_name: str, contacts: dict) -> str:
    number = contacts.get(contact_name.lower())
    if not number:
        return f"Could not find {contact_name} in contacts."
    # Remove non-numeric characters
    clean_number = ''.join(filter(str.isdigit, number))
    # Open WhatsApp call directly
    url = f"whatsapp://call?phone={clean_number}"
    if not open_uri(url):
        open_uri(f"https://wa.me/{clean_number}")
    return f"Calling {contact_name} on WhatsApp."
