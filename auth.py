"""
JWT + bcrypt authentication — PRD §9.1.
httpOnly cookies (not localStorage). bcrypt cost 12. 5-attempt rate limit per IP.
"""

import logging
import time
from collections import defaultdict

import bcrypt
import jwt

logger = logging.getLogger(__name__)

_JWT_SECRET: str = ""  # injected at startup via init_auth()
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_S = 15 * 60       # 15 min — PRD §9.1
REFRESH_TOKEN_EXPIRE_S = 7 * 24 * 3600  # 7 days — PRD §9.1
BCRYPT_ROUNDS = 12                    # PRD §9.1

# In-memory rate limit (per IP). Resets on restart — acceptable for hackathon.
_login_attempts: dict[str, list[float]] = defaultdict(list)
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_S = 300  # 5-minute window


def init_auth(jwt_secret: str) -> None:
    global _JWT_SECRET
    _JWT_SECRET = jwt_secret


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


def create_access_token(user_id: int) -> str:
    now = int(time.time())
    payload = {
        "sub": str(user_id),
        "type": "access",
        "iat": now,
        "exp": now + ACCESS_TOKEN_EXPIRE_S,
    }
    return jwt.encode(payload, _JWT_SECRET, algorithm=JWT_ALGORITHM)


def create_refresh_token(user_id: int) -> str:
    now = int(time.time())
    payload = {
        "sub": str(user_id),
        "type": "refresh",
        "iat": now,
        "exp": now + REFRESH_TOKEN_EXPIRE_S,
    }
    return jwt.encode(payload, _JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, _JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        logger.warning("Expired token")
        return None
    except jwt.InvalidTokenError as exc:
        logger.warning("Invalid token: %s", exc)
        return None


def is_rate_limited(ip: str) -> bool:
    """Returns True when the IP has exhausted login attempts. Prunes stale entries."""
    now = time.time()
    _login_attempts[ip] = [t for t in _login_attempts[ip] if now - t < LOGIN_WINDOW_S]
    if len(_login_attempts[ip]) >= LOGIN_MAX_ATTEMPTS:
        return True
    _login_attempts[ip].append(now)
    return False
