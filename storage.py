import json
import logging
import os
import sqlite3
import asyncio
from contextlib import contextmanager, asynccontextmanager
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Union, Optional, List, Dict, Any, Tuple
from urllib.parse import urlsplit

logger = logging.getLogger("alfred.storage")

BASE_DIR = Path(__file__).resolve().parent
USERS_DIR = BASE_DIR / "users"
DB_PATH = Path(os.getenv("ALFRED_DB_PATH", str(USERS_DIR / "alfred.db")))
DATABASE_URL = (os.getenv("ALFRED_DATABASE_URL") or os.getenv("DATABASE_URL") or "").strip()
LEGACY_SQLITE_PATH = Path(os.getenv("ALFRED_SQLITE_SOURCE_PATH", str(USERS_DIR / "alfred.db")))
LEGACY_ACCOUNTS_PATH = USERS_DIR / "accounts.json"
STORAGE_BACKEND = "postgres" if DATABASE_URL else "sqlite"
_INIT_LOCK = Lock()
_STORAGE_INITIALIZED = False
_ASYNC_POOL = None
_POOL_LOCK = asyncio.Lock()


def get_storage_backend() -> str:
    return STORAGE_BACKEND


def _ensure_sqlite_parent_dir(path: Path = DB_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _connect_sqlite(path: Path = DB_PATH) -> sqlite3.Connection:
    _ensure_sqlite_parent_dir(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _connect_postgres():
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise RuntimeError(
            "Postgres storage requires psycopg. Install dependencies from requirements.txt."
        ) from exc

    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def _connect():
    if STORAGE_BACKEND == "postgres":
        return _connect_postgres()
    return _connect_sqlite()


async def get_async_pool():
    global _ASYNC_POOL
    if STORAGE_BACKEND != "postgres":
        return None
    if _ASYNC_POOL is not None:
        return _ASYNC_POOL
    
    async with _POOL_LOCK:
        if _ASYNC_POOL is not None:
            return _ASYNC_POOL
        
        try:
            from psycopg_pool import AsyncConnectionPool
            from psycopg.rows import dict_row
            _ASYNC_POOL = AsyncConnectionPool(
                DATABASE_URL,
                min_size=1,
                max_size=10,
                kwargs={"row_factory": dict_row},
                open=False
            )
            await _ASYNC_POOL.open()
            logger.info("Async connection pool opened for %s", STORAGE_BACKEND)
            return _ASYNC_POOL
        except Exception:
            logger.exception("Failed to initialize async connection pool")
            return None


async def close_async_pool():
    global _ASYNC_POOL
    if _ASYNC_POOL:
        await _ASYNC_POOL.close()
        _ASYNC_POOL = None
        logger.info("Async connection pool closed")


def _query(sql: str) -> str:
    if STORAGE_BACKEND == "postgres":
        return sql.replace("?", "%s")
    return sql


def _execute(conn, sql: str, params=()):
    return conn.execute(_query(sql), params)


def _scalar(row, key: str, index: int = 0):
    if row is None:
        return None
    try:
        return row[key]
    except Exception:
        return row[index]


@contextmanager
def get_connection():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


class AsyncCursorWrapper:
    def __init__(self, cursor):
        self._cursor = cursor

    async def fetchone(self):
        return await asyncio.get_event_loop().run_in_executor(None, self._cursor.fetchone)

    async def fetchall(self):
        return await asyncio.get_event_loop().run_in_executor(None, self._cursor.fetchall)

    @property
    def rowcount(self):
        return self._cursor.rowcount

class AsyncConnectionWrapper:
    def __init__(self, conn):
        self._conn = conn

    async def execute(self, sql: str, params=()):
        loop = asyncio.get_event_loop()
        cursor = await loop.run_in_executor(None, self._conn.execute, _query(sql), params)
        return AsyncCursorWrapper(cursor)

    async def commit(self):
        await asyncio.get_event_loop().run_in_executor(None, self._conn.commit)

    async def close(self):
        await asyncio.get_event_loop().run_in_executor(None, self._conn.close)

@asynccontextmanager
async def get_async_connection():
    if STORAGE_BACKEND == "postgres":
        pool = await get_async_pool()
        if pool:
            async with pool.connection() as conn:
                async with conn.transaction():
                    yield conn
            return
    
    # Fallback to sync connection in a thread for sqlite or if pool failed
    loop = asyncio.get_event_loop()
    conn = await loop.run_in_executor(None, _connect)
    wrapped = AsyncConnectionWrapper(conn)
    try:
        yield wrapped
        await wrapped.commit()
    finally:
        await wrapped.close()


def _upsert_account(conn, email: str, data: dict) -> None:
    _execute(
        conn,
        """
        INSERT INTO accounts (email, name, password_hash, created_at, storage_id, onboarding_complete, location_data, widget_preferences)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(email)
        DO UPDATE SET
            name = excluded.name,
            password_hash = excluded.password_hash,
            created_at = excluded.created_at,
            storage_id = excluded.storage_id,
            onboarding_complete = excluded.onboarding_complete,
            location_data = excluded.location_data,
            widget_preferences = excluded.widget_preferences
        """,
        (
            email,
            data.get("name", ""),
            data.get("password_hash", ""),
            data.get("created_at", data.get("created_at", datetime.now().isoformat())),
            data.get("storage_id", ""),
            data.get("onboarding_complete", 0),
            data.get("location_data"),
            data.get("widget_preferences"),
        ),
    )


def _run_migrations() -> None:
    """Ensure accounts table has all necessary columns."""
    try:
        conn = _connect()
        if STORAGE_BACKEND == "postgres":
            # Modern Postgres (9.6+) supports ADD COLUMN IF NOT EXISTS
            conn.execute("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS is_premium BOOLEAN DEFAULT FALSE")
            conn.execute("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS onboarding_complete BOOLEAN DEFAULT FALSE")
            conn.execute("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS location_data TEXT")
            conn.execute("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS widget_preferences TEXT")
            conn.commit()
        else:
            # SQLite
            cols = [r[1] for r in conn.execute("PRAGMA table_info(accounts)").fetchall()]
            if "is_premium" not in cols:
                conn.execute("ALTER TABLE accounts ADD COLUMN is_premium INTEGER DEFAULT 0")
            if "onboarding_complete" not in cols:
                conn.execute("ALTER TABLE accounts ADD COLUMN onboarding_complete INTEGER DEFAULT 0")
            if "location_data" not in cols:
                conn.execute("ALTER TABLE accounts ADD COLUMN location_data TEXT")
            if "widget_preferences" not in cols:
                conn.execute("ALTER TABLE accounts ADD COLUMN widget_preferences TEXT")
            conn.commit()
    except Exception:
        logger.exception("Could not run database migrations")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _upsert_user_document(conn, user_id: str, document_name: str, payload_text: str, updated_at: str) -> None:
    _execute(
        conn,
        """
        INSERT INTO user_documents (user_id, document_name, payload, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id, document_name)
        DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at
        """,
        (user_id, document_name, payload_text, updated_at),
    )


def init_storage() -> None:
    global _STORAGE_INITIALIZED
    if _STORAGE_INITIALIZED:
        return

    with _INIT_LOCK:
        if _STORAGE_INITIALIZED:
            return

        with get_connection() as conn:
            # Create core tables
            _execute(conn, """
                CREATE TABLE IF NOT EXISTS accounts (
                    email TEXT PRIMARY KEY,
                    name TEXT,
                    password_hash TEXT,
                    created_at TEXT,
                    storage_id TEXT,
                    is_premium BOOLEAN DEFAULT FALSE,
                    onboarding_complete BOOLEAN DEFAULT FALSE,
                    location_data TEXT,
                    widget_preferences TEXT
                )
            """)
            _execute(conn, """
                CREATE TABLE IF NOT EXISTS user_documents (
                    user_id TEXT,
                    document_name TEXT,
                    payload TEXT,
                    updated_at TEXT,
                    PRIMARY KEY (user_id, document_name)
                )
            """)
        _run_migrations()
        migrate_legacy_storage()
        _STORAGE_INITIALIZED = True


async def init_storage_async() -> None:
    global _STORAGE_INITIALIZED
    if _STORAGE_INITIALIZED:
        return

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, init_storage)


def _migrate_sqlite_snapshot(conn) -> None:
    source_path = LEGACY_SQLITE_PATH
    if STORAGE_BACKEND != "postgres" or not source_path.exists():
        return

    try:
        source_conn = _connect_sqlite(source_path)
    except Exception:
        logger.exception("Failed to open legacy SQLite storage at %s", source_path)
        return

    migrated_accounts = 0
    migrated_documents = 0
    try:
        try:
            account_rows = source_conn.execute(
                "SELECT email, name, password_hash, created_at, storage_id FROM accounts"
            ).fetchall()
        except sqlite3.OperationalError:
            account_rows = []

        for row in account_rows:
            _upsert_account(
                conn,
                row["email"],
                {
                    "name": row["name"],
                    "password_hash": row["password_hash"],
                    "created_at": row["created_at"],
                    "storage_id": row["storage_id"],
                },
            )
            migrated_accounts += 1

        try:
            document_rows = source_conn.execute(
                "SELECT user_id, document_name, payload, updated_at FROM user_documents"
            ).fetchall()
        except sqlite3.OperationalError:
            document_rows = []

        for row in document_rows:
            _upsert_user_document(
                conn,
                row["user_id"],
                row["document_name"],
                row["payload"],
                row["updated_at"],
            )
            migrated_documents += 1
    except Exception:
        logger.exception("Failed to migrate legacy SQLite storage from %s", source_path)
        return
    finally:
        source_conn.close()

    if migrated_accounts or migrated_documents:
        logger.info(
            "Migrated %s accounts and %s documents from %s into %s storage",
            migrated_accounts,
            migrated_documents,
            source_path,
            STORAGE_BACKEND,
        )


def _migrate_legacy_accounts_json(conn) -> None:
    if not LEGACY_ACCOUNTS_PATH.exists():
        return

    try:
        accounts = json.loads(LEGACY_ACCOUNTS_PATH.read_text())
    except Exception:
        logger.exception("Failed to parse legacy accounts.json")
        return

    migrated = 0
    for email, data in accounts.items():
        _upsert_account(
            conn,
            email,
            {
                "name": data.get("name", ""),
                "password_hash": data.get("password_hash", ""),
                "created_at": data.get("created_at", datetime.now().isoformat()),
                "storage_id": data.get("storage_id", ""),
            },
        )
        migrated += 1

    if migrated:
        logger.info("Migrated %s legacy accounts from %s", migrated, LEGACY_ACCOUNTS_PATH)


def _migrate_legacy_user_dirs(conn) -> None:
    if not USERS_DIR.exists():
        return

    for entry in USERS_DIR.iterdir():
        if not entry.is_dir():
            continue
        user_id = entry.name
        for json_file in entry.glob("*.json"):
            try:
                payload = json.loads(json_file.read_text())
            except Exception:
                logger.exception("Skipping invalid legacy JSON file: %s", json_file)
                continue

            exists = _execute(
                conn,
                "SELECT 1 FROM user_documents WHERE user_id = ? AND document_name = ?",
                (user_id, json_file.name),
            ).fetchone()
            if exists:
                continue

            _upsert_user_document(
                conn,
                user_id,
                json_file.name,
                json.dumps(payload),
                datetime.now().isoformat(),
            )


def migrate_legacy_storage() -> None:
    with get_connection() as conn:
        account_count = _scalar(
            _execute(conn, "SELECT COUNT(*) AS total FROM accounts").fetchone(),
            "total",
        ) or 0
        document_count = _scalar(
            _execute(conn, "SELECT COUNT(*) AS total FROM user_documents").fetchone(),
            "total",
        ) or 0

        if STORAGE_BACKEND == "postgres" and (account_count == 0 or document_count == 0):
            _migrate_sqlite_snapshot(conn)
            account_count = _scalar(
                _execute(conn, "SELECT COUNT(*) AS total FROM accounts").fetchone(),
                "total",
            ) or 0

        if account_count == 0:
            _migrate_legacy_accounts_json(conn)

        _migrate_legacy_user_dirs(conn)


def load_accounts() -> dict:
    init_storage()
    with get_connection() as conn:
        rows = _execute(
            conn,
            "SELECT email, name, password_hash, created_at, storage_id, onboarding_complete, location_data, widget_preferences FROM accounts",
        ).fetchall()
    return {
        row["email"]: {
            "name": row["name"],
            "password_hash": row["password_hash"],
            "created_at": row["created_at"],
            "storage_id": row["storage_id"],
            "onboarding_complete": row["onboarding_complete"] if "onboarding_complete" in row.keys() else 0,
            "location_data": row["location_data"] if "location_data" in row.keys() else None,
            "widget_preferences": row["widget_preferences"] if "widget_preferences" in row.keys() else None,
        }
        for row in rows
    }


async def load_accounts_async() -> dict:
    await init_storage_async()
    async with get_async_connection() as conn:
        cursor = await conn.execute(_query("SELECT email, name, password_hash, created_at, storage_id, onboarding_complete, location_data, widget_preferences FROM accounts"))
        rows = await cursor.fetchall()
    return {
        row["email"]: {
            "name": row["name"],
            "password_hash": row["password_hash"],
            "created_at": row["created_at"],
            "storage_id": row["storage_id"],
            "onboarding_complete": row.get("onboarding_complete", 0),
            "location_data": row.get("location_data"),
            "widget_preferences": row.get("widget_preferences"),
        }
        for row in rows
    }


def load_account(email: str) -> Optional[dict]:
    init_storage()
    with get_connection() as conn:
        row = _execute(
            conn,
            "SELECT email, name, password_hash, created_at, storage_id, onboarding_complete, location_data, widget_preferences FROM accounts WHERE email = ?",
            (email,),
        ).fetchone()
    if not row:
        return None
    return {
        "name": row["name"],
        "password_hash": row["password_hash"],
        "created_at": row["created_at"],
        "storage_id": row["storage_id"],
        "onboarding_complete": row["onboarding_complete"] if hasattr(row, "onboarding_complete") else row.get("onboarding_complete", 0),
        "location_data": row["location_data"] if hasattr(row, "location_data") else row.get("location_data"),
        "widget_preferences": row["widget_preferences"] if hasattr(row, "widget_preferences") else row.get("widget_preferences"),
    }


async def load_account_async(email: str) -> Optional[dict]:
    await init_storage_async()
    async with get_async_connection() as conn:
        cursor = await conn.execute(
            _query("""
            SELECT email, name, password_hash, created_at, storage_id, onboarding_complete, location_data, widget_preferences
            FROM accounts
            WHERE email = ?
            """),
            (email,),
        )
        row = await cursor.fetchone()
    if not row:
        return None
    return {
        "name": row["name"],
        "password_hash": row["password_hash"],
        "created_at": row["created_at"],
        "storage_id": row["storage_id"],
        "onboarding_complete": row.get("onboarding_complete", 0),
        "location_data": row.get("location_data"),
        "widget_preferences": row.get("widget_preferences"),
    }

async def get_account_async(email: str) -> Optional[dict]:
    return await load_account_async(email)

def save_account(email: str, data: dict) -> None:
    init_storage()
    with get_connection() as conn:
        _upsert_account(conn, email, data)


async def save_account_async(email: str, data: dict) -> None:
    await init_storage_async()
    async with get_async_connection() as conn:
        await conn.execute(
            _query("""
            INSERT INTO accounts (email, name, password_hash, created_at, storage_id, onboarding_complete, location_data, widget_preferences)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(email)
            DO UPDATE SET
                name = excluded.name,
                password_hash = excluded.password_hash,
                created_at = excluded.created_at,
                storage_id = excluded.storage_id,
                onboarding_complete = excluded.onboarding_complete,
                location_data = excluded.location_data,
                widget_preferences = excluded.widget_preferences
            """),
            (
                email,
                data.get("name", ""),
                data.get("password_hash", ""),
                data.get("created_at", data.get("created_at", datetime.now().isoformat())),
                data.get("storage_id", ""),
                bool(data.get("onboarding_complete", False)),
                data.get("location_data"),
                data.get("widget_preferences"),
            ),
        )


def create_account(email: str, data: dict) -> bool:
    init_storage()
    with get_connection() as conn:
        cursor = _execute(
            conn,
            """
            INSERT INTO accounts (email, name, password_hash, created_at, storage_id)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(email) DO NOTHING
            """,
            (
                email,
                data.get("name", ""),
                data.get("password_hash", ""),
                data.get("created_at", data.get("created_at", datetime.now().isoformat())),
                data.get("storage_id", ""),
            ),
        )
        return cursor.rowcount > 0


async def create_account_async(email: str, data: dict) -> bool:
    await init_storage_async()
    async with get_async_connection() as conn:
        cursor = await conn.execute(
            _query("""
            INSERT INTO accounts (email, name, password_hash, created_at, storage_id)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(email) DO NOTHING
            """),
            (
                email,
                data.get("name", ""),
                data.get("password_hash", ""),
                data.get("created_at", data.get("created_at", datetime.now().isoformat())),
                data.get("storage_id", ""),
            ),
        )
        return cursor.rowcount > 0


def load_user_document(user_id: str, document_name: str, default=None):
    init_storage()
    with get_connection() as conn:
        row = _execute(
            conn,
            "SELECT payload FROM user_documents WHERE user_id = ? AND document_name = ?",
            (user_id, document_name),
        ).fetchone()
    if row:
        return json.loads(row["payload"])
    return default or {}


async def load_user_document_async(user_id: str, document_name: str, default=None):
    await init_storage_async()
    async with get_async_connection() as conn:
        cursor = await conn.execute(
            _query("SELECT payload FROM user_documents WHERE user_id = ? AND document_name = ?"),
            (user_id, document_name),
        )
        row = await cursor.fetchone()
    if row:
        return json.loads(row["payload"])
    return default or {}


def save_user_document(user_id: str, document_name: str, payload) -> None:
    init_storage()
    with get_connection() as conn:
        _upsert_user_document(
            conn,
            user_id,
            document_name,
            json.dumps(payload),
            datetime.now().isoformat(),
        )


async def save_user_document_async(user_id: str, document_name: str, payload) -> None:
    await init_storage_async()
    async with get_async_connection() as conn:
        await conn.execute(
            _query("""
            INSERT INTO user_documents (user_id, document_name, payload, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, document_name) DO UPDATE SET
                payload = EXCLUDED.payload,
                updated_at = EXCLUDED.updated_at
            """),
            (user_id, document_name, json.dumps(payload), datetime.now().isoformat()),
        )


async def ensure_user_storage_async(user_account: dict, email: str, name: str) -> str:
    storage_id = user_account.get("storage_id")
    if not storage_id:
        from security_utils import build_storage_user_id
        storage_id = build_storage_user_id(email)
        user_account["storage_id"] = storage_id
        await save_account_async(email, user_account)
    return storage_id


def get_storage_health() -> dict:
    try:
        init_storage()
        with get_connection() as conn:
            _execute(conn, "SELECT 1")
        return {
            "status": "ok", 
            "backend": STORAGE_BACKEND,
            "db_path": str(DB_PATH),
            "exists": DB_PATH.exists()
        }
    except Exception as e:
        return {
            "status": "error", 
            "message": str(e), 
            "backend": STORAGE_BACKEND,
            "db_path": str(DB_PATH),
            "exists": DB_PATH.exists()
        }


async def get_is_premium_async(email: str) -> bool:
    """Return True if the user account has premium access."""
    # Temporarily disabled premium gating - everyone is premium now.
    return True


async def set_is_premium_async(email: str, value: bool) -> None:
    """Set the is_premium flag for a user account."""
    await init_storage_async()
    async with get_async_connection() as conn:
        await conn.execute(
            _query("UPDATE accounts SET is_premium = ? WHERE email = ?"),
            (1 if value else 0, email),
        )
