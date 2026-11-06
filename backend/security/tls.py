"""TLS 上下文与证书指纹。

信令通道用 TLS（长连接、双向认证），媒体走 DTLS-SRTP（见 ``dtls_srtp.py``）。
两者共用这里的「证书指纹」概念：SDP 里交换的是证书 DER 的 SHA-256 指纹，
而不是证书链本身——指纹足够短、可以内联到 SDP，且能绑定到具体连接。
"""

from __future__ import annotations

import hashlib
import ssl
from dataclasses import dataclass
from pathlib import Path

from backend.errors.base import ElinkError, ErrorCode
from backend.logging.logger import get_logger

logger = get_logger(__name__)


class TlsError(ElinkError):
    code = ErrorCode.PERMISSION


@dataclass
class TlsConfig:
    """TLS 配置。服务端必须给证书与私钥；客户端可只信指定 CA。"""

    cert_file: str | None = None
    key_file: str | None = None
    ca_file: str | None = None
    verify_peer: bool = True
    min_version: str = "TLSv1_2"

    def validate(self, as_server: bool) -> None:
        if as_server and (not self.cert_file or not self.key_file):
            raise TlsError("服务端 TLS 需要 cert_file 与 key_file")
        for path in (self.cert_file, self.key_file, self.ca_file):
            if path and not Path(path).is_file():
                raise TlsError(f"证书文件不存在: {path}")


def certificate_fingerprint(cert_der: bytes) -> str:
    """``sha-256 AB:CD:...`` 形式（SDP ``a=fingerprint`` 的格式）。"""
    digest = hashlib.sha256(cert_der).hexdigest().upper()
    pairs = ":".join(digest[index : index + 2] for index in range(0, len(digest), 2))
    return f"sha-256 {pairs}"


def parse_fingerprint(value: str) -> bytes:
    """把 SDP 指纹还原成原始摘要字节。"""
    parts = value.strip().split(maxsplit=1)
    if len(parts) != 2 or parts[0].lower() not in {"sha-256", "sha-1"}:
        raise TlsError(f"不支持的指纹格式: {value!r}")
    compact = parts[1].replace(":", "").strip()
    try:
        return bytes.fromhex(compact)
    except ValueError as exc:
        raise TlsError("指纹不是合法十六进制") from exc


def verify_fingerprint(cert_der: bytes, expected: str) -> bool:
    """常量时间比对的语义在这里不重要（公开信息），但保持一致性更好。"""
    algorithm = expected.strip().split(maxsplit=1)[0].lower()
    expected_bytes = parse_fingerprint(expected)
    if algorithm == "sha-256":
        actual = hashlib.sha256(cert_der).digest()
    else:
        actual = hashlib.sha1(cert_der).digest()  # noqa: S324 - 兼容旧对端
    return actual == expected_bytes


def create_server_context(config: TlsConfig) -> ssl.SSLContext:
    config.validate(as_server=True)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = _min_version(config.min_version)
    context.load_cert_chain(config.cert_file, config.key_file)
    if config.ca_file:
        context.load_verify_locations(config.ca_file)
        context.verify_mode = ssl.CERT_REQUIRED
    logger.info("TLS 服务端上下文就绪（%s）", config.min_version)
    return context


def create_client_context(config: TlsConfig) -> ssl.SSLContext:
    config.validate(as_server=False)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = _min_version(config.min_version)
    if config.ca_file:
        context.load_verify_locations(config.ca_file)
    if config.cert_file and config.key_file:
        context.load_cert_chain(config.cert_file, config.key_file)
    if not config.verify_peer:
        # 只在开发/自签场景放行，且必须显式配置
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        logger.warning("TLS 客户端已关闭对端校验（仅限调试）")
    return context


def _min_version(name: str) -> ssl.TLSVersion:
    mapping = {
        "TLSv1_2": ssl.TLSVersion.TLSv1_2,
        "TLSv1_3": ssl.TLSVersion.TLSv1_3,
    }
    try:
        return mapping[name]
    except KeyError as exc:
        raise TlsError(f"不支持的 TLS 版本: {name}") from exc
