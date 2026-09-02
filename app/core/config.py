"""Portal runtime configuration + authentication.

Access model (decision D2): the portal binds to the LAN behind a single shared
app password — no cloud / external IdP.

- The password is stored as a scrypt hash at rest in ``state/portal/portal.auth.json``
  (mode 0600). On first run a password is generated and written *once* to
  ``state/portal/PORTAL_PASSWORD.txt`` (0600) so the operator can read it; set
  ``AI_PORTAL_PASSWORD`` in the unit to override (env wins, never persisted).
- Sessions are stateless: a signed HMAC-SHA256 cookie, no server-side store.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from dataclasses import dataclass

from . import paths

COOKIE_NAME = "aipp"
SESSION_TTL = 60 * 60 * 8  # 8 hours

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1
AUTH_FILE = paths.PORTAL_STATE / "portal.auth.json"
PASSWORD_FILE = paths.PORTAL_STATE / "PORTAL_PASSWORD.txt"
SECRET_FILE = paths.PORTAL_STATE / "portal.secret"


@dataclass
class Settings:
    host: str
    port: int
    secret: bytes
    password_hash: str | None


def _scrypt(password: str, salt: bytes, n: int = _SCRYPT_N, r: int = _SCRYPT_R, p: int = _SCRYPT_P) -> str:
    dk = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    if not stored:
        return False
    try:
        _, n, r, p, salt_hex, dk_hex = stored.split("$")
        dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex),
                            n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False


def _gen_password() -> str:
    alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(20))


def load() -> Settings:
    paths.PORTAL_STATE.mkdir(parents=True, exist_ok=True)

    if SECRET_FILE.exists():
        secret = SECRET_FILE.read_bytes()
    else:
        secret = secrets.token_bytes(32)
        SECRET_FILE.write_bytes(secret)
        os.chmod(SECRET_FILE, 0o600)

    password_hash: str | None = None
    if AUTH_FILE.exists():
        password_hash = json.loads(AUTH_FILE.read_text()).get("password_hash")

    env_password = os.environ.get("AI_PORTAL_PASSWORD")
    if env_password:
        # env wins; use a fresh salt, do not persist
        password_hash = _scrypt(env_password, secrets.token_bytes(16))
    elif password_hash is None:
        # first run: generate and record once
        pw = _gen_password()
        password_hash = _scrypt(pw, secrets.token_bytes(16))
        AUTH_FILE.write_text(
            json.dumps({"password_hash": password_hash,
                        "created": time.strftime("%Y-%m-%dT%H:%M:%S")}),
            encoding="utf-8")
        os.chmod(AUTH_FILE, 0o600)
        PASSWORD_FILE.write_text(pw + "\n", encoding="utf-8")
        os.chmod(PASSWORD_FILE, 0o600)
        print(f"[ai-portal] generated portal password (0600 at {PASSWORD_FILE}); "
              f"set AI_PORTAL_PASSWORD in the unit to override. value: {pw}",
              file=sys.stderr, flush=True)

    return Settings(
        host=os.environ.get("AI_PORTAL_HOST", "0.0.0.0"),
        port=int(os.environ.get("AI_PORTAL_PORT", "8090")),
        secret=secret,
        password_hash=password_hash,
    )


def make_token(settings: Settings) -> str:
    expiry = int(time.time()) + SESSION_TTL
    sig = hmac.new(settings.secret, f"aipp|{expiry}".encode(), hashlib.sha256).hexdigest()
    return f"{expiry}.{sig}"


def token_valid(settings: Settings, token: str) -> bool:
    try:
        expiry_s, sig = token.split(".", 1)
        expiry = int(expiry_s)
        if expiry < time.time():
            return False
        expect = hmac.new(settings.secret, f"aipp|{expiry}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expect, sig)
    except Exception:
        return False
