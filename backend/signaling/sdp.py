"""SDP 构造与解析。

**这是全项目唯一允许拼接 SDP 字符串的地方**（见 PROJECT_STRUCTURE 的「SDP 边界」）。
``transport/webrtc/peer_connection.py`` 只消费这里的 :class:`SdpSession` 对象，
不允许自己拼文本、也不允许改写别人的 SDP。

解析策略：保留原始属性行（``lines``），同时把常用字段解析成结构化字段。
这种「宽进严出」的做法很重要——对端可能带一堆我们不认识的扩展属性，
原样保留才能正确应答。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.errors.base import ElinkError, ErrorCode
from backend.logging.logger import get_logger
from backend.nat.candidate import Candidate
from backend.security.dtls_srtp import DtlsSrtpParameters

logger = get_logger(__name__)

CRLF = "\r\n"


class SdpError(ElinkError):
    code = ErrorCode.INVALID_ARGUMENT


@dataclass
class SdpOrigin:
    username: str = "-"
    session_id: str = "0"
    session_version: str = "0"
    net_type: str = "IN"
    address_type: str = "IP4"
    address: str = "0.0.0.0"

    def to_line(self) -> str:
        return (
            f"o={self.username} {self.session_id} {self.session_version} "
            f"{self.net_type} {self.address_type} {self.address}"
        )


@dataclass
class SdpMedia:
    """一个 ``m=`` 段及其属性。"""

    kind: str
    port: int = 9
    protocol: str = "UDP/TLS/RTP/SAVPF"
    formats: list[str] = field(default_factory=list)
    attributes: list[str] = field(default_factory=list)

    # ---- 属性读取 ----
    def get(self, name: str) -> str | None:
        prefix = f"{name}:"
        for line in self.attributes:
            if line == name:
                return ""
            if line.startswith(prefix):
                return line[len(prefix) :]
        return None

    def get_all(self, name: str) -> list[str]:
        prefix = f"{name}:"
        values = []
        for line in self.attributes:
            if line.startswith(prefix):
                values.append(line[len(prefix) :])
        return values

    def has(self, name: str) -> bool:
        return name in self.attributes or any(
            line.startswith(f"{name}:") for line in self.attributes
        )

    # ---- 结构化视图 ----
    @property
    def mid(self) -> str:
        return self.get("mid") or "0"

    @property
    def direction(self) -> str:
        for candidate in ("sendrecv", "sendonly", "recvonly", "inactive"):
            if candidate in self.attributes:
                return candidate
        return "sendrecv"

    @property
    def ice_ufrag(self) -> str:
        return self.get("ice-ufrag") or ""

    @property
    def ice_pwd(self) -> str:
        return self.get("ice-pwd") or ""

    @property
    def candidates(self) -> list[Candidate]:
        result = []
        for value in self.get_all("candidate"):
            try:
                result.append(Candidate.from_sdp(value))
            except Exception:  # 对端可能发来我们解析不了的候选，跳过而不是整体失败
                logger.debug("忽略无法解析的候选: %s", value)
        return result

    @property
    def rtpmap(self) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for value in self.get_all("rtpmap"):
            parts = value.split(maxsplit=1)
            if len(parts) == 2:
                mapping[parts[0]] = parts[1]
        return mapping

    @property
    def fmtp(self) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for value in self.get_all("fmtp"):
            parts = value.split(maxsplit=1)
            if len(parts) == 2:
                mapping[parts[0]] = parts[1]
        return mapping

    def codec_names(self) -> list[str]:
        names = []
        for payload_type, name in self.rtpmap.items():
            names.append(f"{name.split('/')[0]}({payload_type})")
        return names

    # ---- 便捷写入 ----
    def set_attribute(self, name: str, value: str | None = None) -> None:
        self.remove_attribute(name)
        self.attributes.append(name if value is None else f"{name}:{value}")

    def remove_attribute(self, name: str) -> None:
        prefix = f"{name}:"
        self.attributes = [
            line for line in self.attributes if line != name and not line.startswith(prefix)
        ]

    def add_candidate(self, candidate: Candidate) -> None:
        self.attributes.append(f"candidate:{candidate.to_sdp()}")

    def to_lines(self) -> list[str]:
        lines = [f"m={self.kind} {self.port} {self.protocol} {' '.join(self.formats)}"]
        lines += ["c=IN IP4 0.0.0.0"]
        lines += [f"a={attribute}" for attribute in self.attributes]
        return lines


@dataclass
class SdpSession:
    """一份完整的 SDP。"""

    origin: SdpOrigin = field(default_factory=SdpOrigin)
    session_name: str = "-"
    attributes: list[str] = field(default_factory=list)
    media: list[SdpMedia] = field(default_factory=list)

    # ---- 序列化 ----
    def to_string(self) -> str:
        lines = [
            "v=0",
            self.origin.to_line(),
            f"s={self.session_name}",
            "t=0 0",
        ]
        lines += [f"a={attribute}" for attribute in self.attributes]
        for section in self.media:
            lines += section.to_lines()
        return CRLF.join(lines) + CRLF

    # ---- 解析 ----
    @classmethod
    def parse(cls, text: str) -> "SdpSession":
        if not isinstance(text, str) or not text.strip():
            raise SdpError("SDP 内容为空")

        session = cls()
        current: SdpMedia | None = None
        saw_version = False

        for raw_line in text.replace("\r\n", "\n").split("\n"):
            line = raw_line.strip()
            if not line:
                continue
            if len(line) < 2 or line[1] != "=":
                raise SdpError(f"非法 SDP 行: {line!r}")

            key, value = line[0], line[2:]
            if key == "v":
                saw_version = True
                if value != "0":
                    raise SdpError(f"不支持的 SDP 版本: {value}")
            elif key == "o":
                session.origin = _parse_origin(value)
            elif key == "s":
                session.session_name = value
            elif key == "m":
                current = _parse_media(value)
                session.media.append(current)
            elif key == "a":
                if current is None:
                    session.attributes.append(value)
                else:
                    current.attributes.append(value)
            # i=/c=/b=/t=/z=/k= 等字段当前不影响协商，忽略

        if not saw_version:
            raise SdpError("缺少 v= 行，不是合法 SDP")
        if not session.media:
            raise SdpError("SDP 里没有任何 m= 段")
        return session

    # ---- 查询 ----
    def media_of(self, kind: str) -> SdpMedia | None:
        for section in self.media:
            if section.kind == kind:
                return section
        return None

    @property
    def ice_credentials(self) -> tuple[str, str]:
        for section in self.media:
            if section.ice_ufrag and section.ice_pwd:
                return section.ice_ufrag, section.ice_pwd
        return ("", "")

    def dtls_parameters(self) -> DtlsSrtpParameters | None:
        """从第一个媒体段读 DTLS-SRTP 参数。"""
        for section in self.media:
            fingerprint = section.get("fingerprint")
            if not fingerprint:
                continue
            return DtlsSrtpParameters(
                fingerprint=fingerprint,
                setup=section.get("setup") or "actpass",
                ice_ufrag=section.ice_ufrag,
                ice_pwd=section.ice_pwd,
            )
        return None

    def describe(self) -> str:
        parts = [f"{section.kind}:{','.join(section.codec_names()) or 'raw'}" for section in self.media]
        ufrag, _pwd = self.ice_credentials
        return f"SDP[{'; '.join(parts)}] ice_ufrag={ufrag or '-'}"


def _parse_origin(value: str) -> SdpOrigin:
    parts = value.split()
    if len(parts) < 6:
        raise SdpError(f"非法 o= 行: {value!r}")
    return SdpOrigin(
        username=parts[0],
        session_id=parts[1],
        session_version=parts[2],
        net_type=parts[3],
        address_type=parts[4],
        address=parts[5],
    )


def _parse_media(value: str) -> SdpMedia:
    parts = value.split()
    if len(parts) < 4:
        raise SdpError(f"非法 m= 行: {value!r}")
    return SdpMedia(
        kind=parts[0],
        port=int(parts[1]) if parts[1].isdigit() else 9,
        protocol=parts[2],
        formats=parts[3:],
    )


# ---------------------------------------------------------------- 构造辅助
def build_offer(
    *,
    video_codecs: dict[str, str] | None = None,
    audio_codecs: dict[str, str] | None = None,
    dtls: DtlsSrtpParameters | None = None,
    candidates: list[Candidate] | None = None,
    address: str = "0.0.0.0",
    ice_ufrag: str = "",
    ice_pwd: str = "",
) -> SdpSession:
    """构造一份 offer（只在本文件拼字符串）。"""
    import secrets

    session = SdpSession(attributes=["group:BUNDLE 0 1"])
    video_codecs = video_codecs or {"96": "H264/90000"}
    audio_codecs = audio_codecs or {"111": "opus/48000/2"}
    ice_ufrag = ice_ufrag or secrets.token_urlsafe(6)[:8]
    ice_pwd = ice_pwd or secrets.token_urlsafe(18)[:24]

    for mid, (kind, codecs) in enumerate(
        (("video", video_codecs), ("audio", audio_codecs))
    ):
        section = SdpMedia(kind=kind, formats=list(codecs))
        section.set_attribute("mid", str(mid))
        section.set_attribute("rtcp-mux")
        section.set_attribute("rtcp-rsize")
        section.set_attribute("ice-ufrag", ice_ufrag)
        section.set_attribute("ice-pwd", ice_pwd)
        section.set_attribute("ice-options", "trickle")
        for payload_type, spec in codecs.items():
            section.attributes.append(f"rtpmap:{payload_type} {spec}")
            if spec.lower().startswith("h264"):
                section.attributes.append(
                    f"fmtp:{payload_type} packetization-mode=1;profile-level-id=42e01f"
                )
        if dtls is not None:
            for attribute in dtls.to_sdp_attributes():
                section.attributes.append(attribute[len("a=") :])
        for candidate in candidates or []:
            section.add_candidate(candidate)
        session.media.append(section)

    session.origin = SdpOrigin(address=address)
    return session


def build_answer(offer: SdpSession, *, dtls: DtlsSrtpParameters | None = None) -> SdpSession:
    """按 offer 的媒体段构造 answer（保持 mid 与载荷类型一致）。"""
    answer = SdpSession(attributes=list(offer.attributes))
    for index, section in enumerate(offer.media):
        mirrored = SdpMedia(
            kind=section.kind,
            port=section.port,
            protocol=section.protocol,
            formats=list(section.formats),
        )
        mirrored.set_attribute("mid", section.mid or str(index))
        mirrored.set_attribute("rtcp-mux")
        for payload_type, spec in section.rtpmap.items():
            mirrored.attributes.append(f"rtpmap:{payload_type} {spec}")
            if payload_type in section.fmtp:
                mirrored.attributes.append(f"fmtp:{payload_type} {section.fmtp[payload_type]}")
        if dtls is not None:
            for attribute in dtls.to_sdp_attributes():
                mirrored.attributes.append(attribute[len("a=") :])
        answer.media.append(mirrored)
    return answer


def negotiate_codecs(offer: SdpSession, answer: SdpSession, kind: str = "video") -> list[str]:
    """取两端共同支持的编码名（用于选编码器）。"""
    offer_section = offer.media_of(kind)
    answer_section = answer.media_of(kind)
    if offer_section is None or answer_section is None:
        return []

    def names(section: SdpMedia) -> set[str]:
        return {spec.split("/")[0].upper() for spec in section.rtpmap.values()}

    common = names(offer_section) & names(answer_section)
    return sorted(common)
