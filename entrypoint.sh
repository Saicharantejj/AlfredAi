#!/bin/bash

# Start the WhatsApp bridge in the background
echo "📱 Starting WhatsApp Bridge..."
cd /app/whatsapp
node index.js &
NODE_PID=$!

# Go back to /app
cd /app

# Start the Alfred Python API in the foreground
echo "🎩 Starting Alfred API..."
# We use 8080 for Railway compatibility
# We use proxy-headers to handle HTTPS correctly behind the Railway proxy
exec uvicorn main:app \
    --host 0.0.0.0 \
    --port 8080 \
    --proxy-headers \
    --forwarded-allow-ips="*"
