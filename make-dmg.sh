#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# make-dmg.sh  —  Wrap Alfred.app into a distributable .dmg
#
# Usage:  ./make-dmg.sh
#
# Picks up whichever .app was built by electron-builder:
#   dist-electron/mac-arm64/Alfred.app  (Apple Silicon — preferred)
#   dist-electron/mac/Alfred.app        (Intel x64)
# ─────────────────────────────────────────────────────────────────────────────
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"

VERSION=$(node -e "console.log(require('./electron/package.json').version)" 2>/dev/null || echo "1.0.0")

# Pick the right .app
if [ -d "$ROOT/dist-electron/mac-arm64/Alfred.app" ]; then
  APP="$ROOT/dist-electron/mac-arm64/Alfred.app"
  ARCH="arm64"
elif [ -d "$ROOT/dist-electron/mac/Alfred.app" ]; then
  APP="$ROOT/dist-electron/mac/Alfred.app"
  ARCH="x64"
else
  echo "❌  No Alfred.app found in dist-electron/. Run ./build-mac.sh first."
  exit 1
fi

OUT="$ROOT/dist-electron/Alfred-${VERSION}-${ARCH}.dmg"
STAGING="$(mktemp -d)/Alfred-dmg"

echo ""
echo "╔══════════════════════════════════════╗"
echo "║   Alfred — creating .dmg             ║"
echo "╚══════════════════════════════════════╝"
echo ""
echo "  App   : $APP"
echo "  Output: $OUT"
echo ""

# Build staging directory
mkdir -p "$STAGING"
cp -R "$APP" "$STAGING/Alfred.app"
ln -sf /Applications "$STAGING/Applications"

# Create the DMG
hdiutil create \
  -volname "Alfred" \
  -srcfolder "$STAGING" \
  -ov \
  -format UDZO \
  "$OUT"

# Cleanup
rm -rf "$(dirname "$STAGING")"

echo ""
echo "✅  Done!"
echo "   $OUT"
echo ""
ls -lh "$OUT"
