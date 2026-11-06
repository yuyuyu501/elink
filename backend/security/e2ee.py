"""端到端加密（E2EE）。

与 DTLS-SRTP 的区别：DTLS-SRTP 只保护「链路」，中间服务器（SFU/中继）能看到
明文；E2EE 保护的是「内容」，只有两端能解。

实现走 AES-GCM（自带完整性校验，不需要额外的 HMAC）：

- nonce = 4 字节固定前缀 + 8 字节单调计数器，**绝不复用**（GCM 复用 nonce
  会直接泄露明文与密钥，这是硬性约束）；
- AAD 绑定流标识与序号，防止把 A 流的包重放到 B 流；
- 需要 ``cryptography`` 包；没有时明确报错，不做「假装加密」的降级。

密钥轮换：按 ``max_frames_per_key`` 或时长触发，老密钥保留一小段时间用于
解密迟到的包。
"""

from __future__ import annotations

import os
import struct
import time
from dataclasses import dataclass

from backend.errors.base import ElinkError, ErrorCode
from backend.logging.logger import get_logger

logger = get_logger(__name__)

try:  # pragma: no cover - 可选依赖
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore

    _CRYPTO_AVAILABLE = True
except ImportError:  # pragma: no cover
    AESGCM = None  # type: ignore
    _CRYPTO_AVAILABLE = False


class E2eeError(ElinkError):
    code = ErrorCode.PERMISSION


@dataclass
class EncryptedPayload:
    """加密后的载荷（可直接塞进 RTP / 控制消息）。"""

    ciphertext: bytes
    key_id: int
    counter: int

    def pack(self) -> bytes:
        return struct.pack("!HI", self.key_id, self.counter) + self.ciphertext

    @classmethod
    def unpack(cls, data: bytes) -> "EncryptedPayload":
        if len(data) < 6:
            raise E2eeError("加密载荷长度不足")
        key_id, counter = struct.unpack("!HI", data[:6])
        return cls(ciphertext=data[6:], key_id=key_id, counter=counter)


class FrameCipher:
    """单向的帧加解密（每个方向一个实例）。"""

    def __init__(
        self,
        key: bytes,
        *,
        stream_id: int = 0,
        nonce_prefix: bytes = b"",
        max_frames_per_key: int = 1_000_000,
    ) -> None:
        if not _CRYPTO_AVAILABLE:
            raise E2eeError(
                "E2EE 需要 cryptography 包（pip install cryptography）"
            )
        if len(key) not in (16, 24, 32):
            raise E2eeError(f"非法密钥长度: {len(key)}")
        self._aead = AESGCM(key)
        self.key = key
        self.key_id = 0
        self.stream_id = stream_id
        self._prefix = nonce_prefix or b"\x00\x00\x00\x00"
        if len(self._prefix) != 4:
            raise E2eeError("nonce 前缀必须是 4 字节")
        self._counter = 0
        self._rotate_after = max_frames_per_key
        self._created_at = time.monotonic()
        self._recent_keys: dict[int, AESGCM] = {}

    @property
    def counter(self) -> int:
        return self._counter

    def _next_nonce(self) -> tuple[bytes, int]:
        counter = self._counter
        self._counter += 1
        return self._prefix + struct.pack("!Q", counter & 0xFFFFFFFFFFFFFFFF), counter

    def _aad(self, key_id: int, counter: int) -> bytes:
        return struct.pack("!I I I Q", self.stream_id, key_id, 0, counter)

    def encrypt(self, plaintext: bytes) -> EncryptedPayload:
        if self._counter >= self._rotate_after:
            self.rotate()

        nonce, counter = self._next_nonce()
        ciphertext = self._aead.encrypt(nonce, plaintext, self._aad(self.key_id, counter))
        return EncryptedPayload(ciphertext=ciphertext, key_id=self.key_id, counter=counter)

    def decrypt(self, payload: EncryptedPayload) -> bytes:
        aead = self._aead if payload.key_id == self.key_id else self._recent_keys.get(payload.key_id)
        if aead is None:
            raise E2eeError(f"未知的密钥版本: {payload.key_id}")

        nonce = self._prefix + struct.pack("!Q", payload.counter & 0xFFFFFFFFFFFFFFFF)
        try:
            return aead.decrypt(nonce, payload.ciphertext, self._aad(payload.key_id, payload.counter))
        except Exception as exc:  # cryptography 抛 InvalidTag
            raise E2eeError("E2EE 解密失败（密钥不匹配或数据被篡改）") from exc

    def rotate(self, new_key: bytes | None = None) -> int:
        """轮换密钥；老密钥保留用于解密迟到包。"""
        old_id = self.key_id
        self._recent_keys[old_id] = self._aead
        self.key = new_key or os.urandom(32)
        self._aead = AESGCM(self.key)
        self.key_id = (self.key_id + 1) & 0xFFFF
        self._counter = 0
        self._created_at = time.monotonic()
        if len(self._recent_keys) > 2:
            oldest = min(self._recent_keys)
            self._recent_keys.pop(oldest, None)
        logger.info("E2EE 密钥轮换: id=%d", self.key_id)
        return self.key_id

    def stats(self) -> dict[str, float]:
        return {
            "key_id": float(self.key_id),
            "counter": float(self._counter),
            "age_s": time.monotonic() - self._created_at,
        }


def available() -> bool:
    """E2EE 是否可用（取决于是否装了 cryptography）。"""
    return _CRYPTO_AVAILABLE
