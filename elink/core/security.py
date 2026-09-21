from __future__ import annotations

import ctypes
import datetime as dt
import os
import ssl
from ctypes import wintypes
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from ..models import ValidationError


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
