import os
from typing import Optional, List, Dict, Any, Union
import platform
import subprocess
import tempfile
from pathlib import Path

SYSTEM = platform.system()
IS_WINDOWS = SYSTEM == "Windows"
IS_MAC = SYSTEM == "Darwin"
IS_LINUX = SYSTEM == "Linux"


def _ps_quote(value: str) -> str:
    return value.replace("'", "''")


def _run(command: List[str], *, check: bool = False, capture_output: bool = False, timeout: Optional[int] = None):
    return subprocess.run(
        command,
        check=check,
        capture_output=capture_output,
        text=True,
        timeout=timeout,
    )


def open_application(app_name: str) -> bool:
    try:
        if IS_MAC:
            subprocess.Popen(["open", "-a", app_name])
            return True

        if IS_WINDOWS:
            # Method 1: PowerShell Start-Process — works for most registered apps
            try:
                result = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     f"Start-Process -FilePath '{_ps_quote(app_name)}'"],
                    capture_output=True, timeout=5,
                )
                if result.returncode == 0:
                    return True
            except Exception:
                pass
            # Method 2: shell=True lets Windows resolve app names via PATH / App Paths registry
            try:
                subprocess.Popen(app_name, shell=True)
                return True
            except Exception:
                pass
            return False

        # Linux / headless server — can't open GUI apps
        return False
    except Exception:
        return False


def open_uri(uri: str) -> bool:
    try:
        if IS_WINDOWS:
            os.startfile(uri)  # type: ignore[attr-defined]
        elif IS_MAC:
            subprocess.Popen(["open", uri])
        elif IS_LINUX:
            subprocess.Popen(["xdg-open", uri])
        else:
            return False
        return True
    except Exception:
        return False


def show_notification(title: str, message: str) -> bool:
    safe_title = title.replace('"', "'")
    safe_message = message.replace('"', "'")
    try:
        if IS_WINDOWS:
            script = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "[System.Windows.Forms.MessageBox]::Show("
                f"'{_ps_quote(safe_message)}', "
                f"'{_ps_quote(safe_title)}'"
                ") | Out-Null"
            )
            subprocess.Popen(["powershell", "-NoProfile", "-Command", script])
            return True
        if IS_MAC:
            _run(["osascript", "-e", f'display notification "{safe_message}" with title "{safe_title}"'])
            return True
        return False
    except Exception:
        return False


def speak_text(text: str) -> bool:
    safe_text = text.replace('"', "'")
    try:
        if IS_WINDOWS:
            script = (
                "Add-Type -AssemblyName System.Speech; "
                "$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                f"$speaker.Speak('{_ps_quote(safe_text)}')"
            )
            subprocess.Popen(["powershell", "-NoProfile", "-Command", script])
            return True
        if IS_MAC:
            _run(["say", "-v", "Daniel", safe_text])
            return True
        return False
    except Exception:
        return False


def capture_screen_png() -> str:
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.close()
    path = Path(tmp.name)
    try:
        if IS_WINDOWS:
            script = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "Add-Type -AssemblyName System.Drawing; "
                "$bounds = [System.Windows.Forms.SystemInformation]::VirtualScreen; "
                "$bmp = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height; "
                "$graphics = [System.Drawing.Graphics]::FromImage($bmp); "
                "$graphics.CopyFromScreen($bounds.Left, $bounds.Top, 0, 0, $bmp.Size); "
                f"$bmp.Save('{_ps_quote(str(path))}', [System.Drawing.Imaging.ImageFormat]::Png); "
                "$graphics.Dispose(); $bmp.Dispose();"
            )
            _run(["powershell", "-NoProfile", "-Command", script], check=True)
        elif IS_MAC:
            _run(["screencapture", "-x", str(path)], check=True)
        else:
            raise RuntimeError("Screen capture is only configured for macOS and Windows.")
        return str(path)
    except Exception:
        if path.exists():
            path.unlink(missing_ok=True)
        raise


def lock_screen() -> bool:
    try:
        if IS_WINDOWS:
            _run(["rundll32.exe", "user32.dll,LockWorkStation"])
            return True
        if IS_MAC:
            _run(["pmset", "displaysleepnow"])
            return True
        return False
    except Exception:
        return False


def sleep_system() -> bool:
    try:
        if IS_WINDOWS:
            _run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])
            return True
        if IS_MAC:
            _run(["pmset", "sleepnow"])
            return True
        return False
    except Exception:
        return False
