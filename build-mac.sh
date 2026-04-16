#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# Alfred macOS build script
# Produces: dist-electron/Alfred-<version>-arm64.dmg
#           dist-electron/Alfred-<version>-x64.dmg
#
# Usage:  ./build-mac.sh
# ─────────────────────────────────────────────────────────────────────────────
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

echo "╔══════════════════════════════════════╗"
echo "║   Alfred — macOS build               ║"
echo "╚══════════════════════════════════════╝"

# ── 1. Python dependencies ────────────────────────────────────────────────────
echo ""
echo "▶ Installing Python dependencies…"
python3 -m pip install --upgrade pip pyinstaller
pip install -r requirements.txt

# ── 2. Bundle Python backend with PyInstaller ─────────────────────────────────
echo ""
echo "▶ Bundling Python backend…"
pyinstaller alfred.spec --clean --noconfirm
echo "  ✓ Python backend → dist-python/alfred-backend"

# ── 3. Node dependencies for the WhatsApp bridge ─────────────────────────────
echo ""
echo "▶ Installing WhatsApp bridge dependencies…"
cd "$ROOT/whatsapp" && npm install --omit=dev && cd "$ROOT"

# ── 4. Electron dependencies ──────────────────────────────────────────────────
echo ""
echo "▶ Installing Electron dependencies…"
cd "$ROOT/electron" && npm install && cd "$ROOT"

# ── 5. Build the .dmg ─────────────────────────────────────────────────────────
echo ""
echo "▶ Building macOS .dmg…"
cd "$ROOT/electron"
npm run build:mac
cd "$ROOT"

echo ""
echo "✅ Done!  Installer is in dist-electron/"
ls -lh "$ROOT/dist-electron/"*.dmg 2>/dev/null || true
