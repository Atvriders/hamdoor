"""Password hashing, the JWT signing key and JWT helpers."""

import hashlib
import logging
import os
import secrets
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import User

log = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=False)

# ---------------------------------------------------------------- signing key

# a SECRET_KEY anyone can read (README, compose files, defaults) signs tokens
# anyone can forge: such keys are never used
MIN_SECRET_LENGTH = 32
_PLACEHOLDER_SECRETS = {
    "", "change-me", "changeme", "change-me-in-production", "change-me-to-a-long-random-string",
    "secret", "secret-key", "your-secret-key", "dev", "test",
}
SECRET_KEY_FILE = "secret_key"
_key_lock = threading.Lock()
_signing_key: str | None = None


def is_weak_secret(key: str | None) -> bool:
    """True for an empty or short key, or one of the published placeholders."""
    k = (key or "").strip()
    return k.lower() in _PLACEHOLDER_SECRETS or k.lower().startswith("change-me") \
        or len(k) < MIN_SECRET_LENGTH


def _data_dir() -> str:
    url = get_settings().database_url
    path = url.split("///", 1)[-1] if "///" in url else "/data/hamdoor.db"
    return os.path.dirname(path) or "."


def _load_or_create_key_file(path: str) -> str:
    """A random key kept next to the database (mode 600), made on first use."""
    try:
        with open(path, encoding="ascii") as fh:
            key = fh.read().strip()
        if not is_weak_secret(key):
            return key
        log.warning("[security] %s is too short; replacing it with a new random key", path)
        os.remove(path)
    except FileNotFoundError:
        pass
    key = secrets.token_hex(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(key + "\n")
    return key


def signing_key() -> str:
    """The key that signs and checks session tokens: SECRET_KEY when it is a
    real secret; otherwise a random key generated once and kept in the data
    directory (<data dir>/secret_key, mode 600), so a server left on the
    compose placeholder can't have its logins forged. Raises RuntimeError
    when SECRET_KEY is weak and no key file can be kept (the app then refuses
    to start)."""
    global _signing_key
    if _signing_key is not None:
        return _signing_key
    with _key_lock:
        if _signing_key is not None:
            return _signing_key
        configured = get_settings().secret_key
        if not is_weak_secret(configured):
            _signing_key = configured
            return _signing_key
        path = os.path.join(_data_dir(), SECRET_KEY_FILE)
        try:
            key = _load_or_create_key_file(path)
        except OSError as e:
            raise RuntimeError(
                "SECRET_KEY is empty, shorter than 32 characters or a published placeholder, and no "
                f"random key could be kept at {path} ({e}). Set SECRET_KEY to a long random string "
                "(python3 -c \"import secrets; print(secrets.token_hex(32))\") and start again."
            ) from e
        log.warning("[security] SECRET_KEY is empty, short or a published placeholder: signing "
                    "sessions with the random key kept in %s instead (sessions signed with the "
                    "old key have ended)", path)
        _signing_key = key
        return _signing_key


def reset_signing_key_cache() -> None:
    """Forget the resolved key (tests)."""
    global _signing_key
    with _key_lock:
        _signing_key = None


# ---------------------------------------------------------------- passwords

# bcrypt is slow on purpose (~0.25 s of CPU each): at most this many run at
# once, so a burst of log ins can't hold every worker thread the rest of the
# API shares; a caller that can't get a slot within the wait gets a 429
BCRYPT_SLOTS = 4
BCRYPT_WAIT_SECONDS = 5.0
_bcrypt_slots = threading.BoundedSemaphore(BCRYPT_SLOTS)


@contextmanager
def _bcrypt_slot():
    if not _bcrypt_slots.acquire(timeout=BCRYPT_WAIT_SECONDS):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            "That's busy right now. Try again in a minute.",
                            headers={"Retry-After": "30"})
    try:
        yield
    finally:
        _bcrypt_slots.release()


def hash_password(password: str) -> str:
    # bcrypt only uses the first 72 bytes of the key material
    pw = password.encode("utf-8")[:72]
    with _bcrypt_slot():
        return bcrypt.hashpw(pw, bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    pw = password.encode("utf-8")[:72]
    try:
        with _bcrypt_slot():
            return bcrypt.checkpw(pw, password_hash.encode("ascii"))
    except ValueError:
        return False


def password_fingerprint(user: User) -> str:
    """A short digest of the member's password hash, carried in their tokens:
    changing the password changes it, which ends every session made with the
    old password."""
    return hashlib.sha256((user.password_hash or "").encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- tokens


def create_token(user: User) -> str:
    s = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user.callsign,
        "uid": user.id,
        "pwv": password_fingerprint(user),
        "exp": now + timedelta(minutes=s.jwt_expire_minutes),
        "iat": now,
    }
    return jwt.encode(payload, signing_key(), algorithm=s.jwt_algorithm)


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    try:
        payload = jwt.decode(
            creds.credentials,
            signing_key(),
            algorithms=[get_settings().jwt_algorithm],
            options={"require": ["exp", "iat", "sub", "uid", "pwv"]},
        )
        uid = int(payload["uid"])
        sub = str(payload["sub"])
        iat = float(payload["iat"])
        pwv = str(payload["pwv"])
    except Exception:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token")
    user = db.get(User, uid)
    # SQLite hands a deleted account's row id to the next signup: a token is
    # only good for the account it was issued to — same callsign, issued no
    # earlier than the account was created (iat has whole seconds) — and for
    # the password it was issued under (a password change ends old sessions)
    if user is None or user.callsign != sub or iat < _created_ts(user) // 1:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user no longer exists")
    if not secrets.compare_digest(pwv.encode("utf-8"), password_fingerprint(user).encode("utf-8")):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token")
    return user


def _created_ts(user: User) -> float:
    """The account's creation time as Unix seconds (SQLite returns the stored
    UTC time without a zone); 0 when unknown."""
    created = user.created_at
    if created is None:
        return 0.0
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return created.timestamp()
