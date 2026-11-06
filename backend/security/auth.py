"""认证：一次性令牌 + 会话密钥派生。

设计取舍：

- 令牌用 HMAC-SHA256 签名而不是 JWT 库——只需要「谁、什么时候过期、有没有
  被改过」，自己实现反而更可控（无外部依赖、无算法混淆风险）；
- 校验用 ``hmac.compare_digest``，避免时序侧信道；
- 会话密钥用 HKDF 从握手材料派生，且**每个会话一份**，不复用长期密钥。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass, field

from backend.errors.base import ElinkError, ErrorCode
from backend.logging.logger import get_logger

logger = get_logger(__name__)


class AuthError(ElinkError):
    code = ErrorCode.PERMISSION


@dataclass(frozen=True)
class TokenClaims:
    subject: str
    expires_at: float
    issued_at: float
    session_id: str = ""
    roles: tuple[str, ...] = field(default_factory=tuple)

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at

    def to_dict(self) -> dict:
        return {
            "sub": self.subject,
            "exp": self.expires_at,
            "iat": self.issued_at,
            "sid": self.session_id,
            "roles": list(self.roles),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TokenClaims":
        return cls(
            subject=data["sub"],
            expires_at=float(data["exp"]),
            issued_at=float(data["iat"]),
            session_id=data.get("sid", ""),
            roles=tuple(data.get("roles", ())),
        )


class TokenSigner:
    """HMAC-SHA256 令牌签发 / 校验。"""

    def __init__(self, secret: bytes | str, ttl_s: float = 3600) -> None:
        self._secret = secret.encode("utf-8") if isinstance(secret, str) else secret
        if not self._secret:
            raise AuthError("认证密钥不能为空")
        self.ttl_s = ttl_s

    def issue(
        self,
        subject: str,
        *,
        session_id: str = "",
        roles: tuple[str, ...] = (),
    ) -> str:
        now = time.time()
        claims = TokenClaims(
            subject=subject,
            issued_at=now,
            expires_at=now + self.ttl_s,
            session_id=session_id,
            roles=roles,
        )
        payload = _b64encode(json.dumps(claims.to_dict(), separators=(",", ":")).encode())
        signature = self._sign(payload)
        return f"{payload}.{signature}"

    def verify(self, token: str) -> TokenClaims:
        try:
            payload, signature = token.split(".", 1)
        except ValueError as exc:
            raise AuthError("令牌格式错误") from exc

        expected = self._sign(payload)
        if not hmac.compare_digest(expected, signature):
            raise AuthError("令牌签名不匹配")

        try:
            data = json.loads(_b64decode(payload))
        except (ValueError, json.JSONDecodeError) as exc:
            raise AuthError("令牌载荷无法解析") from exc

        claims = TokenClaims.from_dict(data)
        if claims.expired:
            raise AuthError("令牌已过期", detail=f"exp={claims.expires_at:.0f}")
        return claims

    def _sign(self, payload: str) -> str:
        digest = hmac.new(self._secret, payload.encode(), hashlib.sha256).digest()
        return _b64encode(digest)


class SessionKeyDeriver:
    """从握手材料派生会话密钥（HKDF-SHA256）。"""

    def __init__(self, shared_secret: bytes) -> None:
        self._secret = shared_secret

    def derive(self, session_id: str, length: int = 32, salt: bytes = b"") -> bytes:
        info = f"elink-session:{session_id}".encode()
        salt = salt or bytes(32)
        prk = hmac.new(salt, self._secret, hashlib.sha256).digest()
        output = b""
        block = b""
        counter = 1
        while len(output) < length:
            block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
            output += block
            counter += 1
        return output[:length]


class Authenticator:
    """把令牌校验包成一个统一入口，供信令/控制通道使用。"""

    def __init__(self, signer: TokenSigner, *, allow_anonymous: bool = False) -> None:
        self.signer = signer
        self.allow_anonymous = allow_anonymous

    def authenticate(self, token: str | None) -> TokenClaims:
        if not token:
            if self.allow_anonymous:
                return TokenClaims(
                    subject="anonymous",
                    issued_at=time.time(),
                    expires_at=time.time() + 60,
                )
            raise AuthError("缺少认证令牌")
        return self.signer.verify(token)


def generate_secret(length: int = 32) -> bytes:
    return os.urandom(length)


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)
