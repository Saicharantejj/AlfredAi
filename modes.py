import subprocess
from platform_utils import lock_screen, sleep_system

MODES = {
    "study": {
        "greeting": "Study mode activated. Distractions off. Let's get to work.",
        "apps": ["Claude", "Spotify"],
    },
    "work": {
        "greeting": "Work mode activated. Your workspace is set.",
        "apps": ["Claude", "ChatGPT", "Atlas", "Spotify"],
    },
    "gaming": {
        "greeting": "Gaming mode activated. Let's run it.",
        "apps": ["Steam"],
    },
    "streaming": {
        "greeting": "Streaming mode activated.",
        "apps": ["OBS", "Google Chrome"],
    },
    "sleep": {
        "greeting": "Goodnight. I'll be here when you're back.",
        "apps": [],
    },
}

current_mode = None

def activate_mode(mode_name: str) -> str:
    global current_mode
    mode_name = mode_name.lower().strip()

    matched = None
    for key in MODES:
        if key in mode_name or mode_name in key:
            matched = key
            break

    if not matched:
        return f"I don't recognise that mode. Available modes: {', '.join(MODES.keys())}."

    current_mode = matched
    mode = MODES[matched]

    return mode["greeting"]

def get_current_mode() -> str:
    return current_mode or "none"

def set_volume(level: int):
    level = max(0, min(100, level))
    if is_windows:
        try:
            from ctypes import cast, POINTER
            from comtypes import CLSCTX_ALL
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
            devices = AudioUtilities.GetSpeakers()
            interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            volume = cast(interface, POINTER(IAudioEndpointVolume))
            volume_scalar = level / 100.0
            volume.SetMasterVolumeLevelScalar(volume_scalar, None)
        except Exception as e:
            print(f"Windows volume control failed: {e}")
    else:
        subprocess.run(['osascript', '-e', f'set volume output volume {level}'])

def lock_mac():
    lock_screen()

def sleep_mac():
    sleep_system()
