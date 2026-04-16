@echo off
REM ─────────────────────────────────────────────────────────────────────────────
REM Alfred Windows build script
REM Produces: dist-electron\Alfred Setup <version>.exe
REM
REM Usage:  build-win.bat
REM ─────────────────────────────────────────────────────────────────────────────
setlocal enabledelayedexpansion
set ROOT=%~dp0
cd /d "%ROOT%"

echo.
echo ============================================
echo   Alfred -- Windows build
echo ============================================

REM ── 1. Python dependencies ──────────────────────────────────────────────────
echo.
echo [1/5] Installing Python dependencies...
python -m pip install --upgrade pip pyinstaller
pip install -r requirements.txt

REM ── 2. Bundle Python backend ────────────────────────────────────────────────
echo.
echo [2/5] Bundling Python backend...
pyinstaller alfred.spec --clean --noconfirm
echo   Done: dist-python\alfred-backend.exe

REM ── 3. WhatsApp bridge dependencies ─────────────────────────────────────────
echo.
echo [3/5] Installing WhatsApp bridge dependencies...
cd "%ROOT%whatsapp"
npm install --omit=dev
cd /d "%ROOT%"

REM ── 4. Electron dependencies ─────────────────────────────────────────────────
echo.
echo [4/5] Installing Electron dependencies...
cd "%ROOT%electron"
npm install
cd /d "%ROOT%"

REM ── 5. Build the installer ──────────────────────────────────────────────────
echo.
echo [5/5] Building Windows installer...
cd "%ROOT%electron"
npm run build:win
cd /d "%ROOT%"

echo.
echo ============================================
echo   Done!  Installer is in dist-electron\
echo ============================================
dir "%ROOT%dist-electron\*.exe" 2>nul
