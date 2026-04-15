#!/bin/bash

# Ensure PORT is defined (Railway provides this)
APP_PORT="${PORT:-8080}"

# Start the WhatsApp bridge in the background
# We must pass the correct URL so the bridge can notify Alfred
echo "📱 Starting WhatsApp Bridge on port 3000..."
cd /app/whatsapp
ALFRED_API_URL="http://127.0.0.1:$APP_PORT" WHATSAPP_BRIDGE_PORT=3000 node index.js &
NODE_PID=$!

# Go back to /app
cd /app

# Start the Alfred Python API in the foreground
echo "🎩 Starting Alfred API on port $APP_PORT..."
exec uvicorn main:app \
    --host 0.0.0.0 \
    --port "$APP_PORT" \
    --proxy-headers \
    --forwarded-allow-ips="*"
