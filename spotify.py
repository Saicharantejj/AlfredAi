import subprocess
import time
import os
import platform
from platform_utils import IS_WINDOWS, IS_MAC, open_application, open_uri

sp = None


def _spotify_is_running() -> bool:
    if not IS_MAC:
        return True
    try:
        result = subprocess.run(
            [
                'osascript',
                '-e',
                'if application "Spotify" is running then return "true" else return "false"',
            ],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return result.stdout.strip().lower() == "true"
    except Exception:
        return False


def get_spotipy():
    global sp
    if sp is not None:
        return sp
    try:
        import spotipy
        from spotipy.oauth2 import SpotifyOAuth
        from dotenv import load_dotenv
        load_dotenv()
        
        client_id = os.getenv("SPOTIPY_CLIENT_ID")
        client_secret = os.getenv("SPOTIPY_CLIENT_SECRET")
        redirect_uri = os.getenv("SPOTIPY_REDIRECT_URI", "http://localhost:8000/callback")
        
        if client_id and client_secret:
            auth_manager = SpotifyOAuth(
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri=redirect_uri,
                scope="user-read-currently-playing user-modify-playback-state",
                open_browser=False
            )
            # Try to get token. If it fails, SpotiPy will prompt CLI. We don't want to hang.
            token_info = auth_manager.get_cached_token()
            if token_info:
                sp = spotipy.Spotify(auth_manager=auth_manager)
            else:
                sp = "NEEDS_AUTH"
    except Exception:
        pass
    return sp

def spotify_command(action: str) -> str:
    # ── 1. Spotify Web API (all platforms) ────────────────────────────────────
    s = get_spotipy()
    if s and s != "NEEDS_AUTH":
        try:
            if action in ["play", "pause"]:
                cp = s.current_playback()
                if cp and cp.get('is_playing'):
                    s.pause_playback()
                else:
                    s.start_playback()
                return f"Spotify: {action}"
            elif action == "next":
                s.next_track()
                return "Spotify: next track"
            elif action == "previous":
                s.previous_track()
                return "Spotify: previous track"
        except Exception:
            pass  # Fall through to OS-level controls

    # ── 2. macOS — AppleScript ────────────────────────────────────────────────
    if IS_MAC:
        open_application('Spotify')
        time.sleep(1)
        commands = {
            "play":     'tell application "Spotify" to play',
            "pause":    'tell application "Spotify" to pause',
            "next":     'tell application "Spotify" to next track',
            "previous": 'tell application "Spotify" to previous track',
        }
        if action in commands:
            result = subprocess.run(['osascript', '-e', commands[action]],
                                    capture_output=True, text=True)
            if result.returncode == 0:
                return f"Spotify: {action}"
        return "Unknown Spotify command"

    # ── 3. Windows — media key events ────────────────────────────────────────
    if IS_WINDOWS:
        try:
            import ctypes
            hw = ctypes.windll.user32
            VK_MEDIA_NEXT_TRACK  = 0xB0
            VK_MEDIA_PREV_TRACK  = 0xB1
            VK_MEDIA_PLAY_PAUSE  = 0xB3
            KEYEVENTF_KEYUP      = 0x0002
            if action in ["play", "pause"]:
                hw.keybd_event(VK_MEDIA_PLAY_PAUSE, 0, 0, 0)
                hw.keybd_event(VK_MEDIA_PLAY_PAUSE, 0, KEYEVENTF_KEYUP, 0)
            elif action == "next":
                hw.keybd_event(VK_MEDIA_NEXT_TRACK, 0, 0, 0)
                hw.keybd_event(VK_MEDIA_NEXT_TRACK, 0, KEYEVENTF_KEYUP, 0)
            elif action == "previous":
                hw.keybd_event(VK_MEDIA_PREV_TRACK, 0, 0, 0)
                hw.keybd_event(VK_MEDIA_PREV_TRACK, 0, KEYEVENTF_KEYUP, 0)
            return f"Spotify: {action}"
        except Exception:
            return "Windows media control failed"

    # ── 4. Linux/Railway without API ──────────────────────────────────────────
    return (
        "Spotify control requires SPOTIPY_CLIENT_ID and SPOTIPY_CLIENT_SECRET "
        "to be set in your environment."
    )

def play_song(query: str) -> str:
    s = get_spotipy()

    # ── 1. Spotify Web API (works everywhere, actually auto-plays) ────────────
    if s and s != "NEEDS_AUTH":
        try:
            results = s.search(q=query, limit=1, type='track')
            if results and results['tracks']['items']:
                track = results['tracks']['items'][0]
                uri = track['uri']
                track_name = track['name']
                artist = track['artists'][0]['name']

                # Try to start playback on the user's active Spotify device
                try:
                    devices_resp = s.devices()
                    devices = devices_resp.get('devices', []) if devices_resp else []
                    active = next((d for d in devices if d.get('is_active')), None)
                    device_id = active['id'] if active else (devices[0]['id'] if devices else None)
                    s.start_playback(device_id=device_id, uris=[uri])
                    return f"Now playing '{track_name}' by {artist} on Spotify ✓"
                except Exception:
                    pass

                # API playback failed (no active device) — open URI directly.
                # On macOS/Windows this launches Spotify and queues the track.
                open_application('Spotify')
                time.sleep(1)
                open_uri(uri)
                return f"Opening '{track_name}' by {artist} in Spotify"
        except Exception:
            pass

    # ── 2. macOS fallback (no API) — open app then load search ───────────────
    if IS_MAC:
        open_application('Spotify')
        time.sleep(1.5)
        clean_q = query.replace(' ', '%20')
        open_uri(f"spotify:search:{clean_q}")
        return f"Opened Spotify with results for '{query}' — tap the first track to play"

    # ── 3. Windows fallback (no API) — URI scheme opens Spotify to search ────
    if IS_WINDOWS:
        clean_q = query.replace(' ', '%20')
        open_uri(f"spotify:search:{clean_q}")
        return f"Opened Spotify with results for '{query}' — tap the first track to play"

    # ── 4. Linux/Railway without API — can't control desktop remotely ─────────
    return (
        "To control Spotify from Alfred on this server, add SPOTIPY_CLIENT_ID and "
        "SPOTIPY_CLIENT_SECRET to your environment so Alfred can reach Spotify on "
        "your device via the Web API."
    )

def set_spotify_volume(level: int) -> str:
    if IS_WINDOWS:
        print(f"Windows volume control needs pycaw. Stub: {level}")
        return f"Windows volume stub: {level}"
    else:
        subprocess.run(['osascript', '-e', f'tell application "Spotify" to set sound volume to {level}'])
        return f"Spotify volume set to {level}"

def get_current_track() -> str:
    s = get_spotipy()
    if s and s != "NEEDS_AUTH":
        try:
            cp = s.current_playback()
            if cp and cp.get('is_playing') and cp.get('item'):
                track = cp['item']['name']
                artist = cp['item']['artists'][0]['name']
                return f"{track} by {artist}"
        except:
            pass

    if IS_WINDOWS:
        return "Windows track fetch requires Spotify Web API credentials."
    else:
        if not _spotify_is_running():
            return "Spotify is not running."
        try:
            result = subprocess.run(
                ['osascript', '-e', 'tell application "Spotify" to return name of current track & " by " & artist of current track'],
                capture_output=True, text=True, timeout=3
            )
            return result.stdout.strip()
        except Exception:
            return "Could not get current track"


def get_current_playback_info() -> dict:
    s = get_spotipy()
    if s and s != "NEEDS_AUTH":
        try:
            cp = s.current_playback()
            if not cp or not cp.get("item"):
                return {"playing": False, "state": cp.get("device", {}).get("is_active", False) if cp else False}
            item = cp["item"]
            artists = item.get("artists", [])
            return {
                "playing": bool(cp.get("is_playing")),
                "track": item.get("name", ""),
                "artist": artists[0]["name"] if artists else "",
                "album": item.get("album", {}).get("name", ""),
                "position": int((cp.get("progress_ms") or 0) / 1000),
                "duration": int((item.get("duration_ms") or 0) / 1000),
                "state": "playing" if cp.get("is_playing") else "paused",
            }
        except Exception:
            pass

    if IS_MAC:
        if not _spotify_is_running():
            return {"playing": False, "state": "stopped"}
        try:
            state_result = subprocess.run(
                ['osascript', '-e', 'tell application "Spotify" to return player state as string'],
                capture_output=True, text=True, timeout=3
            )
            state = state_result.stdout.strip()
            if state != "playing":
                return {"playing": False, "state": state or "stopped"}
            track_result = subprocess.run(
                ['osascript', '-e',
                 'tell application "Spotify"\n'
                 '  set t to name of current track\n'
                 '  set ar to artist of current track\n'
                 '  set al to album of current track\n'
                 '  set pos to player position as integer\n'
                 '  set dur to (duration of current track) div 1000\n'
                 '  return t & "|||" & ar & "|||" & al & "|||" & pos & "|||" & dur\n'
                 'end tell'],
                capture_output=True, text=True, timeout=4
            )
            out = track_result.stdout.strip()
            if not out or track_result.returncode != 0:
                return {"playing": False, "state": "error", "error": track_result.stderr.strip()}
            parts = out.split("|||")
            return {
                "playing": True,
                "track": parts[0].strip() if len(parts) > 0 else "",
                "artist": parts[1].strip() if len(parts) > 1 else "",
                "album": parts[2].strip() if len(parts) > 2 else "",
                "position": int(parts[3].strip()) if len(parts) > 3 else 0,
                "duration": int(parts[4].strip()) if len(parts) > 4 else 0,
                "state": "playing",
            }
        except Exception:
            return {"playing": False, "state": "unavailable"}

    return {"playing": False, "state": SYSTEM_FALLBACK_STATE()}


def SYSTEM_FALLBACK_STATE() -> str:
    if IS_WINDOWS:
        return "unavailable_without_spotify_api"
    if IS_MAC:
        return "stopped"
    return "unsupported"
