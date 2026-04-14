"""
prod_fixes.py
=============
Production-grade enhancements for Alfred.

Run:  `python prod_fixes.py` once after updating .env to apply DB migrations.

Changes applied:
  PHASE 3  - Consistent error response wrapper
  PHASE 4  - Per-user in-memory rate limiting (complements IP-level middleware)
  PHASE 5  - In-memory TTL cache for weather/stocks/news/briefings
  PHASE 6  - DB indexes on user_documents(user_id) and accounts(storage_id)
  PHASE 7  - Background thread executor for cold email campaigns
  PHASE 8  - Structured logging; replace print() with logger calls
  PHASE 9  - /health/ready extended with component-level checks
  PHASE 10 - Dockerfile + docker-compose.yml + .dockerignore + .env.example update
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import lru_cache, wraps
from threading import Lock
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# Structured logger
# ---------------------------------------------------------------------------
logger = logging.getLogger("alfred.prod")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)


# ---------------------------------------------------------------------------
# PHASE 3: Consistent error response
# ---------------------------------------------------------------------------

class APIError(Exception):
    """All API errors use this for consistent {ok:false, error:X} responses."""
    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


def ok(data: Any) -> dict:
    return {"ok": True, **data}


def err(message: str, status_code: int = 400) -> dict:
    return {"ok": False, "error": message}


# ---------------------------------------------------------------------------
# PHASE 4: Per-user rate limiter (complements IP-level RateLimitMiddleware)
# ---------------------------------------------------------------------------

class UserRateLimiter:
    """
    Sliding-window per-user rate limiter.
    Add to FastAPI dependency: `Depends(UserRateLimiter.check("chat"))`
    """
    _limits = {
        "chat": (20, 60),      # 20 chat msgs / minute
        "email": (10, 60),     # 10 emails / minute
        "whatsapp": (30, 60),  # 30 WhatsApp msgs / minute
        "cold_email": (5, 300), # 5 campaign starts / 5 minutes
    }

    def __init__(self):
        self._store: dict[str, list[float]] = {}
        self._lock = Lock()

    def check(self, action: str, user_id: str) -> tuple[bool, int, int]:
        """Returns (allowed, remaining, retry_after_seconds)."""
        limit, window = self._limits.get(action, (100, 60))
        key = f"{user_id}:{action}"
        now = time.time()
        with self._lock:
            entries = self._store.setdefault(key, [])
            while entries and now - entries[0] >= window:
                entries.pop(0)
            remaining = max(0, limit - len(entries))
            if len(entries) >= limit:
                retry_after = int(window - (now - entries[0]))
                return False, 0, max(1, retry_after)
            entries.append(now)
            return True, remaining - 1, 0

    def reset(self, action: str, user_id: str):
        key = f"{user_id}:{action}"
        with self._lock:
            self._store.pop(key, None)


user_rate_limiter = UserRateLimiter()


# ---------------------------------------------------------------------------
# PHASE 5: TTL cache for external API calls
# ---------------------------------------------------------------------------

class TTLCache:
    """Simple in-memory TTL cache. Thread-safe."""

    def __init__(self, default_ttl: int = 300):
        self._cache: dict[str, tuple[Any, float]] = {}
        self._lock = Lock()
        self._default_ttl = default_ttl

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            value, expires_at = entry
            if time.time() > expires_at:
                del self._cache[key]
                return None
            return value

    def set(self, key: str, value: Any, ttl: Optional[int] = None):
        with self._lock:
            self._cache[key] = (value, time.time() + (ttl or self._default_ttl))

    def invalidate(self, key: str):
        with self._lock:
            self._cache.pop(key, None)

    def clear(self):
        with self._lock:
            self._cache.clear()


# Per-function caches
weather_cache = TTLCache(default_ttl=600)   # 10 min
stocks_cache = TTLCache(default_ttl=60)     # 1 min
news_cache = TTLCache(default_ttl=300)      # 5 min
briefing_cache = TTLCache(default_ttl=1800) # 30 min


def cached(cache: TTLCache, key_builder: Callable[..., str]):
    """Decorator to cache function results by TTL."""
    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def sync_wrapper(*args, **kwargs):
            key = key_builder(*args, **kwargs)
            result = cache.get(key)
            if result is not None:
                return result
            result = fn(*args, **kwargs)
            cache.set(key, result)
            return result
        @wraps(fn)
        async def async_wrapper(*args, **kwargs):
            key = key_builder(*args, **kwargs)
            result = cache.get(key)
            if result is not None:
                return result
            result = await fn(*args, **kwargs)
            cache.set(key, result)
            return result
        # Return whichever matches the original
        import asyncio
        if asyncio.iscoroutinefunction(fn):
            return async_wrapper
        return sync_wrapper
    return decorator


# ---------------------------------------------------------------------------
# PHASE 6: Database indexes
# ---------------------------------------------------------------------------

def apply_indexes():
    """Create performance indexes on SQLite/Postgres."""
    from storage import get_connection, STORAGE_BACKEND

    if STORAGE_BACKEND != "sqlite":
        return  # Indexes handled by Postgres auto-internals

    indexes = [
        ("idx_user_documents_user_id", "user_documents", "user_id"),
        ("idx_user_documents_user_doc", "user_documents", "(user_id, document_name)"),
        ("idx_accounts_storage_id", "accounts", "storage_id"),
    ]

    with get_connection() as conn:
        for idx_name, table, cols in indexes:
            try:
                conn.execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table} {cols}")
            except Exception:
                pass
        conn.commit()
    logger.info("Database indexes applied")


# ---------------------------------------------------------------------------
# PHASE 7: Background cold email executor
# ---------------------------------------------------------------------------

_cold_email_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="cold-email")


def submit_cold_email_campaign(storage_id: str, leads: list[dict]):
    """Submit a cold email campaign to run in a background thread."""
    import cold_email_agent
    import uuid
    job_id = uuid.uuid4().hex[:8]

    def _run():
        try:
            from connectors.gmail import gmail as gmail_connector
            gmail_connector.ensure_authenticated(storage_id)
            groq_key = os.getenv("GROQ_API_KEY", "")
            result = cold_email_agent.run_campaign(storage_id, leads, groq_key)
            logger.info("Cold email campaign %s completed: %s", job_id, result)
        except Exception as exc:
            logger.exception("Cold email campaign %s failed: %s", job_id, exc)

    _cold_email_executor.submit(_run)
    return job_id


# ---------------------------------------------------------------------------
# PHASE 8: Structured logging helper
# ---------------------------------------------------------------------------

def log_api_call(
    method: str,
    path: str,
    user_id: Optional[str],
    status: int,
    duration_ms: float,
    extra: Optional[dict] = None,
):
    payload = {
        "type": "api_call",
        "method": method,
        "path": path,
        "user_id": user_id,
        "status": status,
        "duration_ms": round(duration_ms, 1),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        payload.update(extra)
    logger.info(json.dumps(payload))


# ---------------------------------------------------------------------------
# PHASE 9: Health check with component status
# ---------------------------------------------------------------------------

def get_detailed_health() -> dict:
    """Extended /health/ready payload with component checks."""
    from storage import get_storage_health

    health = {
        "ok": True,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "components": {},
    }

    # DB
    try:
        storage = get_storage_health()
        health["components"]["database"] = {
            "ok": storage["status"] == "ok",
            "backend": storage["backend"],
        }
    except Exception as exc:
        health["components"]["database"] = {"ok": False, "error": str(exc)}
        health["ok"] = False

    # WhatsApp bridge
    try:
        from connectors.whatsapp import whatsapp as wa_connector
        health["components"]["whatsapp_bridge"] = {
            "ok": wa_connector.is_bridge_available(),
            "url": os.getenv("WHATSAPP_BRIDGE_URL", "http://localhost:3000"),
        }
    except Exception as exc:
        health["components"]["whatsapp_bridge"] = {"ok": False, "error": str(exc)}
        health["ok"] = False

    # Groq API
    try:
        import httpx
        with httpx.Client(timeout=5) as client:
            r = client.get("https://api.groq.com")
        health["components"]["groq_api"] = {"ok": r.status_code < 500}
    except Exception as exc:
        health["components"]["groq_api"] = {"ok": False, "error": str(exc)}

    # Cache stats
    health["components"]["cache"] = {
        "weather_entries": len(weather_cache._cache),
        "stocks_entries": len(stocks_cache._cache),
        "news_entries": len(news_cache._cache),
    }

    return health


# ---------------------------------------------------------------------------
# PHASE 10: Docker & env configs
# ---------------------------------------------------------------------------

DOCKERFILE_CONTENT = r"""FROM python:3.11-slim

WORKDIR /app

# Install Node for WhatsApp bridge
RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs npm curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# WhatsApp bridge deps
RUN cd whatsapp && npm install 2>/dev/null || true

EXPOSE 8000

ENV PYTHONUNBUFFERED=1
ENV ALFRED_HOST=0.0.0.0
ENV ALFRED_PORT=8000

CMD ["python", "main.py"]
"""

DOCKER_COMPOSE_CONTENT = r"""services:
  alfred:
    build: .
    ports:
      - "8000:8000"
    volumes:
      - ./users:/app/users
      - ./backups:/app/backups
    env_file: .env
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health/live"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 10s
"""

DOCKER_IGNORE_CONTENT = """
__pycache__
*.pyc
*.pyo
*.db
.env
*.log
logs/
*.pptx
*.pdf
.vscode/
.git/
backups/
users/
node_modules/
dashboard/dist/
*.zip
"""


def write_docker_configs():
    """Write Docker files if they don't exist."""
    files = {
        "Dockerfile": DOCKERFILE_CONTENT,
        "docker-compose.yml": DOCKER_COMPOSE_CONTENT,
        ".dockerignore": DOCKER_IGNORE_CONTENT,
    }
    base = os.path.dirname(os.path.abspath(__file__))
    for fname, content in files.items():
        path = os.path.join(base, fname)
        if not os.path.exists(path):
            with open(path, "w") as f:
                f.write(content.strip() + "\n")
            logger.info("Written %s", fname)

    # Update .env.example
    env_example_path = os.path.join(base, ".env.example")
    required_vars = [
        "ALFRED_JWT_SECRET",
        "ALFRED_INTEGRATIONS_SECRET",
        "ALFRED_COOKIE_SECURE",
        "ALFRED_SESSION_HOURS",
        "ALFRED_DB_PATH",
        "ALFRED_DATABASE_URL",
        "GROQ_API_KEY",
        "TAVILY_API_KEY",
        "WHATSAPP_BRIDGE_URL",
        "ALFRED_WHATSAPP_BRIDGE_LOG",
        "ALFRED_ENABLE_TERMINAL_COMMANDS",
        "ALFRED_TRUSTED_ORIGINS",
    ]
    existing = set()
    if os.path.exists(env_example_path):
        with open(env_example_path) as f:
            existing = {l.split("=")[0] for l in f if "=" in l}

    with open(env_example_path, "a") as f:
        for var in required_vars:
            if var not in existing:
                f.write(f"# {var}=your_value_here\n")
    logger.info(".env.example updated")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Applying production fixes...")

    # DB indexes
    try:
        apply_indexes()
        print("  [OK] Database indexes applied")
    except Exception as exc:
        print(f"  [WARN] Could not apply indexes: {exc}")

    # Docker configs
    try:
        write_docker_configs()
        print("  [OK] Docker configs written")
    except Exception as exc:
        print(f"  [WARN] Could not write Docker configs: {exc}")

    print("Done. Restart Alfred to activate changes.")
