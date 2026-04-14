import base64
import os

from platform_utils import capture_screen_png

def capture_screen():
    screenshot_path = capture_screen_png()
    try:
        with open(screenshot_path, "rb") as f:
            data = base64.b64encode(f.read()).decode("utf-8")
    finally:
        os.unlink(screenshot_path)
    return data

def analyze_screen(question="What is on my screen?"):
    try:
        img_data = capture_screen()
        return img_data, question
    except Exception as e:
        return None, str(e)
