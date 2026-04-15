#!/bin/bash

# Ensure PORT is defined (Railway provides this)
APP_PORT="${PORT:-8080}"

# Start the WhatsApp bridge in the background
echo "📱 Starting WhatsApp Bridge on port 3000..."
cd /app/whatsapp
node index.js &
NODE_PID=$!

# Go back to /app
cd /app

# Start the Alfred Python API in the foreground
# We use $APP_PORT so Railway can track health status correctly
echo "🎩 Starting Alfred API on port $APP_PORT..."
exec uvicorn main:app \
    --host 0.0.0.0 \
    --port "$APP_PORT" \
    --proxy-headers \
    --forwarded-allow-ips="*"
