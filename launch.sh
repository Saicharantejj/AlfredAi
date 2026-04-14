#!/bin/bash

echo "🎩 Starting Alfred..."

ALFRED_HOST="${ALFRED_HOST:-127.0.0.1}"
ALFRED_PORT="${ALFRED_PORT:-8000}"
ALFRED_FORCE_RESTART="${ALFRED_FORCE_RESTART:-false}"
ALFRED_OPEN_BROWSER="${ALFRED_OPEN_BROWSER:-false}"
WHATSAPP_BRIDGE_PORT="${WHATSAPP_BRIDGE_PORT:-3000}"
NODE_BIN="${ALFRED_NODE_BIN:-$(command -v node 2>/dev/null)}"
PYTHON_BIN="${ALFRED_PYTHON_BIN:-$PWD/venv/bin/python}"

if [ -z "$NODE_BIN" ]; then
  for candidate in /usr/local/bin/node /opt/homebrew/bin/node; do
    if [ -x "$candidate" ]; then
      NODE_BIN="$candidate"
      break
    fi
  done
fi

stop_listener() {
  local port="$1"
  local pids
  pids=$(lsof -t -iTCP:"$port" -sTCP:LISTEN 2>/dev/null)
  if [ -n "$pids" ]; then
    echo "⚠️ Port $port is in use. Stopping existing listener..."
    kill $pids 2>/dev/null || true
    for _ in 1 2 3 4 5; do
      sleep 1
      pids=$(lsof -t -iTCP:"$port" -sTCP:LISTEN 2>/dev/null)
      if [ -z "$pids" ]; then
        return 0
      fi
    done

    echo "⚠️ Port $port did not close after SIGTERM. Forcing shutdown..."
    kill -9 $pids 2>/dev/null || true
    for _ in 1 2 3; do
      sleep 1
      pids=$(lsof -t -iTCP:"$port" -sTCP:LISTEN 2>/dev/null)
      if [ -z "$pids" ]; then
        return 0
      fi
    done

    if [ -n "$pids" ]; then
      echo "❌ Port $port is still in use:"
      lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null || true
      return 1
    fi
  fi
  return 0
}

wait_for_http() {
  local url="$1"
  local label="$2"
  local attempts="${3:-25}"
  local i
  for ((i=0; i<attempts; i++)); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "❌ $label did not become ready."
  return 1
}

is_truthy() {
  local normalized
  normalized=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')
  case "$normalized" in
    1|true|yes|on)
      return 0
      ;;
  esac
  return 1
}

alfred_is_running() {
  local response
  response=$(curl -fsS "http://${ALFRED_HOST}:${ALFRED_PORT}/health/live" 2>/dev/null || true)
  if [ -z "$response" ]; then
    return 1
  fi
  printf '%s' "$response" | grep -Eq '"status"[[:space:]]*:[[:space:]]*"live"'
}

wait_for_alfred_instance() {
  local token="$1"
  local attempts="${2:-20}"
  local i
  local response
  for ((i=0; i<attempts; i++)); do
    response=$(curl -fsS "http://${ALFRED_HOST}:${ALFRED_PORT}/health/live" 2>/dev/null || true)
    if [ -n "$response" ] && printf '%s' "$response" | grep -Eq "\"instance_token\"[[:space:]]*:[[:space:]]*\"$token\""; then
      return 0
    fi
    sleep 1
  done
  echo "❌ Alfred API did not become ready."
  return 1
}

child_alive() {
  local pid="$1"
  if ! kill -0 "$pid" 2>/dev/null; then
    return 1
  fi
  return 0
}

open_alfred_browser() {
  local url="http://${ALFRED_HOST}:${ALFRED_PORT}/chat-ui"
  if is_truthy "$ALFRED_OPEN_BROWSER"; then
    open "$url"
    echo "✅ Browser opened"
  else
    echo "🌐 Alfred is ready at: $url"
    echo "ℹ️ Auto-open is off. Set ALFRED_OPEN_BROWSER=true if you want the browser launched automatically."
  fi
}

if [ -z "$NODE_BIN" ]; then
  echo "❌ Node.js is not available in this shell. Install it or set ALFRED_NODE_BIN."
  exit 1
fi

if [ ! -x "$PYTHON_BIN" ]; then
  echo "❌ Alfred Python environment not found at $PYTHON_BIN"
  exit 1
fi

if alfred_is_running; then
  if ! is_truthy "$ALFRED_FORCE_RESTART"; then
    echo "ℹ️ Alfred is already running on http://${ALFRED_HOST}:${ALFRED_PORT}. Reusing the existing instance."
    if wait_for_http "http://${ALFRED_HOST}:${WHATSAPP_BRIDGE_PORT}/status" "WhatsApp bridge" 2; then
      echo "🌐 WhatsApp bridge running on http://${ALFRED_HOST}:${WHATSAPP_BRIDGE_PORT}"
    else
      echo "⚠️ WhatsApp bridge is not ready yet. Alfred is up, but WhatsApp features may be unavailable."
    fi
    open_alfred_browser
    echo "🎩 Alfred is ready."
    exit 0
  fi
  echo "♻️ Alfred is already running. Restart requested."
fi

pkill -f "uvicorn main:app" 2>/dev/null || true
pkill -f "/Users/saicharantej/alfred/venv/bin/python -m uvicorn main:app" 2>/dev/null || true
pkill -f "node index.js" 2>/dev/null || true
pkill -f "whatsapp" 2>/dev/null
pkill -f "puppeteer" 2>/dev/null

sleep 2
stop_listener "$ALFRED_PORT" || exit 1
stop_listener "$WHATSAPP_BRIDGE_PORT" || exit 1

cd /Users/saicharantej/Documents/alfred
export ALFRED_NODE_BIN="$NODE_BIN"
ALFRED_INSTANCE_TOKEN="$(uuidgen 2>/dev/null | tr '[:upper:]' '[:lower:]')"
if [ -z "$ALFRED_INSTANCE_TOKEN" ]; then
  ALFRED_INSTANCE_TOKEN="$$-$(date +%s)"
fi
export ALFRED_SERVER_INSTANCE_TOKEN="$ALFRED_INSTANCE_TOKEN"
"$PYTHON_BIN" -m uvicorn main:app --host "$ALFRED_HOST" --port "$ALFRED_PORT" &
ALFRED_PID=$!
wait_for_alfred_instance "$ALFRED_INSTANCE_TOKEN" 20 || exit 1
if ! child_alive "$ALFRED_PID"; then
  echo "❌ Alfred API exited unexpectedly."
  exit 1
fi
echo "✅ Alfred server started"

if wait_for_http "http://${ALFRED_HOST}:${WHATSAPP_BRIDGE_PORT}/status" "WhatsApp bridge" 15; then
  echo "🌐 WhatsApp bridge running on http://${ALFRED_HOST}:${WHATSAPP_BRIDGE_PORT}"
else
  echo "⚠️ WhatsApp bridge is not ready yet. Alfred is up, but WhatsApp features may be unavailable."
fi

open_alfred_browser

echo "🎩 Alfred is ready."

trap "kill $ALFRED_PID 2>/dev/null; echo 'Alfred stopped.'" EXIT
wait "$ALFRED_PID"
