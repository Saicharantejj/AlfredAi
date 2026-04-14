import hashlib
import logging
import os
import posixpath
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
import bcrypt

logger = logging.getLogger("alfred.security")
# Support both ALFRED_* and legacy SALVATORE_* env vars for migration
_env_secret = os.getenv("ALFRED_JWT_SECRET") or os.getenv("SALVATORE_JWT_SECRET")
if _env_secret:
    if len(_env_secret.encode("utf-8")) < 32:
        logger.warning("ALFRED_JWT_SECRET is shorter than 32 bytes. Using derived key.")
        SECRET_KEY = hashlib.sha256(_env_secret.encode("utf-8")).hexdigest()
    else:
        SECRET_KEY = _env_secret
else:
    SECRET_KEY = hashlib.sha256(
        f"{os.getenv('HOSTNAME', 'local')}:{os.getpid()}:{os.urandom(32).hex()}".encode("utf-8")
    ).hexdigest()
    logger.warning("ALFRED_JWT_SECRET is not set. Using ephemeral secret — sessions reset on restart.")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = int(os.getenv("ALFRED_SESSION_HOURS", "4320"))

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plain password against a bcrypt hash."""
    try:
        password_bytes = plain_password.encode('utf-8')
        hashed_bytes = hashed_password.encode('utf-8')
        return bcrypt.checkpw(password_bytes, hashed_bytes)
    except Exception:
        return False

def get_password_hash(password: str) -> str:
    """Generate a bcrypt hash of a password."""
    password_bytes = password.encode('utf-8')
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password_bytes, salt)
    return hashed.decode('utf-8')

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Create a new JWT access token."""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def sanitize_user_id(user_id: str) -> str:
    """Ensure user_id is alphanumeric and safe for file paths."""
    return re.sub(r'[^a-zA-Z0-9]', '', user_id)

def build_storage_user_id(email: str) -> str:
    """Create a stable, collision-resistant storage id from the user's email."""
    normalized = (email or "").strip().lower()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    return f"user_{digest}"

def get_safe_user_path(user_id: str, filename: str, base_dir: str = "users") -> str:
    """
    Generate a safe absolute path for a user's file, preventing path traversal.
    """
    safe_user = sanitize_user_id(user_id)
    # Sanitize filename (removes directory separators)
    safe_filename = posixpath.basename(filename)
    
    # Construct target directory
    user_dir = os.path.join(base_dir, safe_user)
    
    # Final path
    final_path = os.path.join(user_dir, safe_filename)
    
    return final_path
