#!/bin/bash

# Ensure PORT is defined (Railway provides this)
APP_PORT="${PORT:-8080}"

# Start the WhatsApp bridge with an auto-restart loop
# If it crashes (e.g. OOM), it will reboot automatically
echo "📱 Starting WhatsApp Bridge on port 3000 (with auto-restart)..."
(
    cd /app/whatsapp
    while true; do
        ALFRED_API_URL="http://127.0.0.1:$APP_PORT" WHATSAPP_BRIDGE_PORT=3000 node index.js
        echo "⚠️ WhatsApp Bridge exited. Restarting in 5s..."
        sleep 5
    done
) &
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
