"""密钥加密与访问口令令牌。

- API Key 使用 Fernet 对称加密后存入 SQLite，密钥文件落在数据目录（不入库、不入仓库）。
- 可选访问口令使用 HMAC 签名令牌，无需额外依赖。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

_TOKEN_TTL_SECONDS = 30 * 24 * 3600


def _load_or_create_key() -> bytes:
    key_file = settings.data_dir / "secret.key"
    if key_file.exists():
        return key_file.read_bytes().strip()
    key = Fernet.generate_key()
    key_file.write_bytes(key)
    return key


_FERNET = Fernet(_load_or_create_key())
_SIGNING_KEY = hashlib.sha256(b"xiaoma-auth:" + _FERNET._signing_key).digest()  # noqa: SLF001


def encrypt(plaintext: str) -> str:
    if not plaintext:
        return ""
    return _FERNET.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(token: str) -> str:
    if not token:
        return ""
    try:
        return _FERNET.decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        return ""


def issue_auth_token() -> str:
    payload = {"exp": int(time.time()) + _TOKEN_TTL_SECONDS}
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8"))
    sig = hmac.new(_SIGNING_KEY, raw, hashlib.sha256).digest()
    return raw.decode("ascii") + "." + base64.urlsafe_b64encode(sig).decode("ascii")


def verify_auth_token(token: str) -> bool:
    try:
        raw_b64, sig_b64 = token.split(".", 1)
    except ValueError:
        return False
    expected = hmac.new(_SIGNING_KEY, raw_b64.encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(expected, base64.urlsafe_b64decode(sig_b64)):
        return False
    try:
        payload: dict[str, Any] = json.loads(base64.urlsafe_b64decode(raw_b64))
    except Exception:  # noqa: BLE001
        return False
    return int(payload.get("exp", 0)) > int(time.time())
