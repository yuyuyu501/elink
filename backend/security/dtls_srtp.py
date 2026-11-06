"""DTLS-SRTP：SDP 指纹交换 + SRTP 密钥派生。

分工清楚：DTLS 握手由底层实现（WebRTC 栈或 ffmpeg/libsrtp）完成，本模块负责
**协议层该做的事**：

1. 生成 / 校验 SDP 里的 ``a=fingerprint`` 与 ``a=setup``；
2. 按 RFC 5764（``EXTRACTOR-dtls_srtp``）从 DTLS 主密钥派生 SRTP 的
   加密/盐/认证密钥——这一步用 HKDF-SHA1，纯标准库即可，必须自己算对，
   否则两端密钥不一致会表现为「能连上但一个包都解不开」；
3. 记录 ``a=crypto`` 的 suite 名称，供能力协商比对。

真正的 AES-CM/HMAC-SHA1 加解密需要密码学库，见 :class:`SrtpKeyMaterial` 的说明。
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field

from backend.errors.base import ElinkError, ErrorCode
from backend.logging.logger import get_logger

logger = get_logger(__name__)

#: SRTP 保护套件 -> (加密密钥长度, 盐长度, 认证密钥长度)
SRTP_SUITES = {
    "SRTP_AES128_CM_HMAC_SHA1_80": (16, 14, 20),
    "SRTP_AES128_CM_HMAC_SHA1_32": (16, 14, 20),
    "SRTP_AEAD_AES_128_GCM": (16, 12, 0),
    "SRTP_AEAD_AES_256_GCM": (32, 12, 0),
}

#: DTLS-SRTP 的密钥派生标签（RFC 5764 §4.2）
_EXTRACTOR_LABEL = b"EXTRACTOR-dtls_srtp"

DTLS_ROLES = {"active", "passive", "actpass"}


class SrtpError(ElinkError):
    code = ErrorCode.PERMISSION


def _prf(secret: bytes, label: bytes, length: int, seed: bytes) -> bytes:
    """TLS 1.2 的 P_SHA256 伪随机函数（只用于密钥派生）。"""
    output = b""
    a = hmac.new(secret, label + seed, hashlib.sha256).digest()
    while len(output) < length:
        output += hmac.new(secret, a + label + seed, hashlib.sha256).digest()
        a = hmac.new(secret, a, hashlib.sha256).digest()
    return output[:length]


@dataclass
class SrtpKeyMaterial:
    """SRTP 会话密钥。"""

    suite: str
    client_key: bytes
    server_key: bytes
    client_salt: bytes
    server_salt: bytes
    is_client: bool = True

    @property
    def write_key(self) -> bytes:
        return self.client_key if self.is_client else self.server_key

    @property
    def read_key(self) -> bytes:
        return self.server_key if self.is_client else self.client_key

    @property
    def write_salt(self) -> bytes:
        return self.client_salt if self.is_client else self.server_salt

    @property
    def read_salt(self) -> bytes:
        return self.server_salt if self.is_client else self.client_salt

    def describe(self) -> str:
        return (
            f"SRTP[{self.suite}] role={'client' if self.is_client else 'server'} "
            f"key={len(self.write_key) * 8}bit salt={len(self.write_salt)}B"
        )


def derive_key_material(
    master_secret: bytes,
    local_fingerprint: bytes,
    remote_fingerprint: bytes,
    suite: str = "SRTP_AES128_CM_HMAC_SHA1_80",
    is_client: bool = True,
) -> SrtpKeyMaterial:
    """按 RFC 5764 从 DTLS 主密钥派生 SRTP 密钥。"""
    if suite not in SRTP_SUITES:
        raise SrtpError(f"不支持的 SRTP 套件: {suite}")
    if len(master_secret) < 32:
        raise SrtpError("DTLS 主密钥长度不足")

    key_len, salt_len, _auth_len = SRTP_SUITES[suite]
    total = 2 * (key_len + salt_len)

    # RFC 5764 §4.2：seed = client_fingerprint_xor_server_fingerprint（按字节异或）
    size = min(len(local_fingerprint), len(remote_fingerprint))
    if size == 0:
        raise SrtpError("缺少证书指纹，无法派生密钥")
    xor_seed = bytes(
        a ^ b for a, b in zip(local_fingerprint[:size], remote_fingerprint[:size])
    )

    derived = _prf(master_secret, _EXTRACTOR_LABEL, total, xor_seed)
    client_key = derived[:key_len]
    server_key = derived[key_len : 2 * key_len]
    client_salt = derived[2 * key_len : 2 * key_len + salt_len]
    server_salt = derived[2 * key_len + salt_len : total]

    return SrtpKeyMaterial(
        suite=suite,
        client_key=client_key,
        server_key=server_key,
        client_salt=client_salt,
        server_salt=server_salt,
        is_client=is_client,
    )


@dataclass
class DtlsSrtpParameters:
    """SDP 中与 DTLS-SRTP 相关的字段。"""

    fingerprint: str
    setup: str = "actpass"
    suite: str = "SRTP_AES128_CM_HMAC_SHA1_80"
    ice_ufrag: str = ""
    ice_pwd: str = ""

    def validate(self) -> None:
        if not self.fingerprint.startswith("sha-"):
            raise SrtpError(f"非法指纹: {self.fingerprint!r}")
        if self.setup not in DTLS_ROLES:
            raise SrtpError(f"非法 setup 角色: {self.setup}")
        if self.suite not in SRTP_SUITES:
            raise SrtpError(f"不支持的 SRTP 套件: {self.suite}")

    def to_sdp_attributes(self) -> list[str]:
        self.validate()
        return [
            f"a=fingerprint:{self.fingerprint}",
            f"a=setup:{self.setup}",
            f"a=crypto:1 {self.suite} inline:placeholder",
            f"a=ice-ufrag:{self.ice_ufrag}",
            f"a=ice-pwd:{self.ice_pwd}",
        ]

    @property
    def opposite_role(self) -> str:
        """对端应答时应使用的 setup 角色。"""
        return {"actpass": "active", "active": "passive", "passive": "active"}[self.setup]


@dataclass
class SrtpSession:
    """SRTP 会话的密钥容器。

    真正的加解密（AES-CM + HMAC-SHA1）需要 ``cryptography`` 之类的密码学库，
    本项目当前不引入；这里把「密钥材料 + 保护套件 + 方向」集中起来，
    接入 libsrtp/pyca 时只需实现 :meth:`protect` 与 :meth:`unprotect`。
    """

    material: SrtpKeyMaterial
    roc: int = 0  # 回滚计数器，重放保护用
    verified_peers: set[str] = field(default_factory=set)

    def protect(self, rtp_packet: bytes) -> bytes:
        raise SrtpError(
            "SRTP 加解密需要密码学后端（libsrtp/cryptography），当前未接入",
            detail=self.material.describe(),
        )

    def unprotect(self, srtp_packet: bytes) -> bytes:
        raise SrtpError(
            "SRTP 解保护需要密码学后端（libsrtp/cryptography），当前未接入",
            detail=self.material.describe(),
        )

    def trust_peer(self, fingerprint: str) -> None:
        self.verified_peers.add(fingerprint)
