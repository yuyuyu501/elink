from __future__ import annotations

import base64
import ctypes
import datetime as dt
import hashlib
import hmac
import json
import os
import secrets
import ssl
import time
from ctypes import wintypes
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from ..models import ValidationError
from ..storage import atomic_json


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def protect(value: bytes, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise ValidationError("设备凭据目前使用 Windows DPAPI。")
    buffer = ctypes.create_string_buffer(value)
    incoming = Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = Blob()
    crypto = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypto.CryptUnprotectData if decrypt else crypto.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not function(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        kernel.LocalFree(outgoing.data)


class Identity:
    def __init__(self, root: Path):
        self.directory = root / "identity"
        self.directory.mkdir(parents=True, exist_ok=True)
        cert_path, key_path = self.directory / "host.pem", self.directory / "host-key.dpapi"
        if cert_path.exists() != key_path.exists():
            raise ValidationError("主机身份文件不完整，已保留原文件。")
        if not cert_path.exists():
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Elink device")])
            now = dt.datetime.now(dt.timezone.utc)
            cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                    .public_key(key.public_key()).serial_number(x509.random_serial_number())
                    .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=3650))
                    .sign(key, hashes.SHA256()))
            key_path.write_bytes(protect(key.private_bytes(serialization.Encoding.PEM,
                                                           serialization.PrivateFormat.PKCS8, serialization.NoEncryption())))
            cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        self.certificate = x509.load_pem_x509_certificate(cert_path.read_bytes())
        self.fingerprint = self.certificate.fingerprint(hashes.SHA256()).hex()
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        # SSLContext consumes the key at load time. The persistent copy is DPAPI protected.
        import tempfile
        with tempfile.TemporaryDirectory(prefix="tls-", dir=self.directory) as temporary:
            plaintext = Path(temporary) / "key.pem"
            plaintext.write_bytes(protect(key_path.read_bytes(), decrypt=True))
            self.context.load_cert_chain(str(cert_path), str(plaintext))


def pairing_proof(code: str, fingerprint: str, nonce: str, name: str, purpose: str = "start") -> str:
    message = json.dumps(["elink-pair-v1", purpose, fingerprint, nonce, name], ensure_ascii=True, separators=(",", ":"))
    return hmac.new(code.encode("ascii"), message.encode("ascii"), hashlib.sha256).hexdigest()


class PairingAuthority:
    """Short-lived 128-bit invitation + explicit local approval; never send the invitation."""
    def __init__(self, root: Path, fingerprint: str):
        self.path = root / "authorized-devices.json"
        self.fingerprint = fingerprint
        self.code = ""
        self.expires = 0.0
        self.pending: dict[str, dict] = {}
        self.attempts: dict[str, list[float]] = {}
        self.devices = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        if not isinstance(self.devices, dict) or not all(
            isinstance(key, str) and isinstance(value, dict) and isinstance(value.get("name"), str)
            and isinstance(value.get("token_hash"), str) and len(value["token_hash"]) == 64
            and all(c in "0123456789abcdef" for c in value["token_hash"])
            for key, value in self.devices.items()
        ):
            raise ValidationError("设备授权文件损坏。")

    def invite(self) -> str:
        self.code = base64.b32encode(secrets.token_bytes(16)).decode("ascii").rstrip("=")
        self.expires = time.monotonic() + 180
        self.pending.clear()
        return self.code

    def begin(self, nonce: str, proof: str, name: str, address: str) -> str:
        now = time.monotonic()
        self.attempts = {key: [t for t in values if t > now - 60] for key, values in self.attempts.items() if any(t > now - 60 for t in values)}
        if len(self.attempts) > 256:
            raise ValidationError("配对请求过多，请稍后重试。")
        recent = self.attempts.setdefault(address, [])
        if len(recent) >= 10:
            raise ValidationError("配对尝试过于频繁。")
        recent.append(now)
        if not self.code or now > self.expires:
            raise ValidationError("配对邀请已失效，请在主机重新生成。")
        if not all(isinstance(v, str) for v in (nonce, proof, name)) or not proof.isascii():
            raise ValidationError("配对请求格式无效。")
        if len(nonce) != 64 or any(c not in "0123456789abcdef" for c in nonce) or not name.strip() or len(name) > 80:
            raise ValidationError("配对请求格式无效。")
        expected = pairing_proof(self.code, self.fingerprint, nonce, name)
        if not hmac.compare_digest(expected, proof):
            raise ValidationError("配对邀请不正确或连接的主机身份不符。")
        if len(self.pending) >= 8 and nonce not in self.pending:
            raise ValidationError("待批准设备已达上限。")
        if nonce not in self.pending:
            self.pending[nonce] = {"id": nonce, "name": name, "address": address,
                                   "poll_proof": pairing_proof(self.code, self.fingerprint, nonce, name, "poll"),
                                   "expires": self.expires, "token": "", "device_id": ""}
        return nonce

    def approve(self, request_id: str):
        request = self.pending.get(request_id)
        if not request or request["expires"] < time.monotonic():
            raise ValidationError("配对请求已过期。")
        if request["token"]:
            return
        token, device_id = secrets.token_urlsafe(32), secrets.token_hex(16)
        devices = dict(self.devices)
        devices[device_id] = {"id": device_id, "name": request["name"],
                              "token_hash": hashlib.sha256(token.encode("ascii")).hexdigest()}
        atomic_json(self.path, devices)
        self.devices = devices
        request.update(token=token, device_id=device_id)

    def poll(self, request_id: str, proof: str) -> dict:
        if not isinstance(request_id, str) or not isinstance(proof, str) or not proof.isascii():
            raise ValidationError("配对请求格式无效。")
        request = self.pending.get(request_id)
        if not request or request["expires"] < time.monotonic() or not hmac.compare_digest(request["poll_proof"], proof):
            raise ValidationError("配对请求已失效。")
        if request["token"]:
            return {"state": "approved", "token": request["token"], "device_id": request["device_id"]}
        return {"state": "pending"}

    def authenticate(self, token: str) -> str:
        if not isinstance(token, str) or not 32 <= len(token) <= 128:
            raise ValidationError("设备尚未授权。")
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        for key, value in self.devices.items():
            if hmac.compare_digest(value["token_hash"], digest):
                return key
        raise ValidationError("设备尚未授权或授权已撤销。")

    def revoke(self, device_id: str):
        devices = {key: value for key, value in self.devices.items() if key != device_id}
        atomic_json(self.path, devices)
        self.devices = devices
        self.pending = {key: value for key, value in self.pending.items() if value["device_id"] != device_id}

    def snapshot(self):
        now = time.monotonic()
        return {"pending": [{key: item[key] for key in ("id", "name", "address")}
                            for item in self.pending.values() if item["expires"] > now and not item["token"]],
                "devices": [{"id": key, "name": value["name"]} for key, value in self.devices.items()]}


class TrustStore:
    def __init__(self, root: Path):
        self.path = root / "trusted-hosts.json"
        self.hosts = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        if not isinstance(self.hosts, dict) or not all(
            isinstance(value, dict) and isinstance(value.get("fingerprint"), str)
            and len(value["fingerprint"]) == 64 and all(c in "0123456789abcdef" for c in value["fingerprint"])
            and isinstance(value.get("credential"), str) for value in self.hosts.values()
        ):
            raise ValidationError("主机信任文件损坏，已保留原文件。")

    def save(self, address: str, fingerprint: str, token: str, device_id: str):
        hosts = dict(self.hosts)
        hosts[address] = {"fingerprint": fingerprint, "credential": base64.b64encode(protect(token.encode("ascii"))).decode("ascii"), "device_id": device_id}
        atomic_json(self.path, hosts)
        self.hosts = hosts

    def get(self, address: str) -> tuple[str, str]:
        record = self.hosts.get(address)
        if not record:
            raise ValidationError("请先与这台 Elink 主机配对。")
        return record["fingerprint"], protect(base64.b64decode(record["credential"]), decrypt=True).decode("ascii")
