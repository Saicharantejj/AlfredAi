# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Alfred backend.

Build with:
    pyinstaller alfred.spec

Output goes to dist-python/alfred-backend (macOS/Linux)
             dist-python/alfred-backend.exe (Windows)
"""

import sys
import os
from pathlib import Path

ROOT = Path(SPECPATH)

block_cipher = None

# ── Collect all Alfred source files ──────────────────────────────────────────
py_files = [
    'launcher.py',
    'main.py',
    'alfred_core.py',
    'alfred_core.py',
    'briefing.py',
    'calendar_helper.py',
    'cold_email_agent.py',
    'contacts.py',
    'db_schemas.py',
    'email_handler.py',
    'email_reader.py',
    'finance.py',
    'habits.py',
    'integrations.py',
    'mail_transport.py',
    'memory.py',
    'memory_retrieval.py',
    'memory_tracker.py',
    'modes.py',
    'multi_action_parser.py',
    'notes.py',
    'platform_utils.py',
    'pomodoro.py',
    'presentation.py',
    'proactive.py',
    'proactive_worker.py',
    'reminders.py',
    'scheduler.py',
    'screen.py',
    'security_utils.py',
    'spotify.py',
    'storage.py',
    'suggestions.py',
    'task_manager_v2.py',
    'tasks.py',
    'utilities.py',
    'voice.py',
    'workflow_engine.py',
]

a = Analysis(
    ['launcher.py'],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        # Static web assets (dashboard, chat UI, etc.)
        ('static',        'static'),
        # Connector package
        ('connectors',    'connectors'),
        # Any bundled data files
    ],
    hiddenimports=[
        # FastAPI / uvicorn internals PyInstaller misses
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.loops.asyncio',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.http.h11_impl',
        'uvicorn.protocols.http.httptools_impl',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        'uvicorn.lifespan.off',
        'fastapi',
        'starlette',
        'pydantic',
        'passlib.handlers.bcrypt',
        'passlib.handlers.sha2_crypt',
        # DB
        'psycopg',
        'psycopg.adapt',
        'psycopg_pool',
        # Other deps
        'aiofiles',
        'multipart',
        'python_multipart',
        'groq',
        'httpx',
        'jwt',
        'spotipy',
        'google.auth',
        'google.oauth2',
        'googleapiclient',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Things we don't need in the bundle
        'tkinter', 'matplotlib', 'numpy', 'scipy', 'pandas',
        'IPython', 'jupyter', 'notebook',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='alfred-backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX can cause antivirus false positives on Windows
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,      # No terminal window
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / 'electron' / 'icons' / ('icon.ico' if sys.platform == 'win32' else 'icon.icns')),
    distpath=str(ROOT / 'dist-python'),
)
