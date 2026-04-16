"""
Alfred desktop launcher — entry point for the PyInstaller bundle.

When Electron starts Alfred it calls this script (or the compiled binary)
with --port <N>.  We boot uvicorn on that port so only one copy runs and
there's never a port collision with another Alfred instance.
"""

import argparse
import os
import sys

def main():
    parser = argparse.ArgumentParser(description="Alfred backend launcher")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8080)),
                        help="Port to listen on")
    args = parser.parse_args()

    port = args.port
    os.environ["PORT"] = str(port)

    # If Electron told us where to store user data, wire those paths up
    # so Alfred writes data there instead of next to the .app bundle.
    users_root = os.environ.get("ALFRED_USERS_ROOT")
    if users_root:
        os.makedirs(users_root, exist_ok=True)

    env_file = os.environ.get("ALFRED_ENV_FILE")
    if env_file and os.path.exists(env_file):
        from dotenv import load_dotenv
        load_dotenv(env_file, override=False)

    # When frozen by PyInstaller, sys._MEIPASS is the temp-extracted bundle.
    # Add it to the path so all Alfred modules are importable.
    if hasattr(sys, "_MEIPASS"):
        sys.path.insert(0, sys._MEIPASS)

    import uvicorn
    uvicorn.run(
        "main:app",
        host="127.0.0.1",
        port=port,
        log_level="info",
        # No --reload in production
    )

if __name__ == "__main__":
    main()
