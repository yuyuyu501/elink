"""前向纠错（分组异或 FEC）。

策略来源：实时链路里「重传」代价是至少一个 RTT，而音频/视频对延迟敏感，
所以轻微丢包用 FEC（多发冗余包）比等重传更划算。

实现是经典的 **分组 XOR**：

- 每 ``block_size`` 个数据包算一个校验包（按字节异或，长度不同时补零）；
- 组内丢任意 1 个包都能恢复；丢 2 个就整组失效（这是 XOR 的固有限制，
  要更强就上 Reed-Solomon）。

冗余率 = 1/block_size，按可用带宽调整（见 ``qos/bitrate.py``）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.logging.logger import get_logger

logger = get_logger(__name__)


@dataclass
class FecPacket:
    """一个 FEC 载荷。"""

    sequence: int  # 校验包自己的序号
    block_start: int  # 组内第一个数据包的序号
    count: int  # 组内数据包数
    payload: bytes
    lengths: tuple[int, ...] = ()  # 各数据包原始长度，用于恢复时裁剪

    def pack(self) -> bytes:
        import struct

        header = struct.pack("!IHH", self.block_start, self.count, len(self.lengths))
        lengths = struct.pack(f"!{len(self.lengths)}I", *self.lengths) if self.lengths else b""
        return header + lengths + self.payload

    @classmethod
    def unpack(cls, sequence: int, data: bytes) -> "FecPacket":
        import struct

        if len(data) < 8:
            raise ValueError("FEC 包长度不足")
        block_start, count, length_count = struct.unpack("!IHH", data[:8])
        offset = 8
        lengths = (
            struct.unpack(f"!{length_count}I", data[offset : offset + length_count * 4])
            if length_count
            else ()
        )
        offset += length_count * 4
        return cls(
            sequence=sequence,
            block_start=block_start,
            count=count,
            payload=data[offset:],
            lengths=tuple(lengths),
        )


@dataclass
class FecBlock:
    """正在收集的一组数据包。"""

    block_start: int
    packets: dict[int, bytes] = field(default_factory=dict)
    parity: bytes = b""
    lengths: list[int] = field(default_factory=list)


class XorFecEncoder:
    """按组生成异或校验包。"""

    def __init__(self, block_size: int = 8) -> None:
        self.block_size = max(2, block_size)
        self._current = FecBlock(block_start=0)
        self._sequences = 0

    @property
    def redundancy(self) -> float:
        return 1.0 / self.block_size

    def push(self, sequence: int, payload: bytes) -> FecPacket | None:
        """喂入一个数据包；组满时返回校验包。"""
        block = self._current
        if not block.packets:
            block.block_start = sequence

        block.packets[sequence] = payload
        block.lengths.append(len(payload))
        block.parity = _xor_bytes(block.parity, payload)

        if len(block.packets) < self.block_size:
            return None

        packet = FecPacket(
            sequence=self._sequences,
            block_start=block.block_start,
            count=self.block_size,
            payload=block.parity,
            lengths=tuple(block.lengths),
        )
        self._sequences += 1
        self._current = FecBlock(block_start=sequence + 1)
        return packet


class XorFecDecoder:
    """收集数据包与校验包，恢复组内丢失的单个包。"""

    def __init__(self, block_size: int = 8) -> None:
        self.block_size = max(2, block_size)
        self._blocks: dict[int, FecBlock] = {}
        self.recovered = 0

    def _block_for(self, block_start: int, count: int) -> FecBlock:
        block = self._blocks.get(block_start)
        if block is None:
            block = FecBlock(block_start=block_start)
            self._blocks[block_start] = block
        block.lengths = block.lengths or [0] * count
        return block

    def push_data(self, sequence: int, payload: bytes) -> None:
        # 数据包归属哪个组由 block_start 决定，这里按序号推算
        block_start = (sequence // self.block_size) * self.block_size
        block = self._block_for(block_start, self.block_size)
        block.packets[sequence] = payload

    def push_parity(self, packet: FecPacket) -> tuple[int, bytes] | None:
        """喂入校验包；能恢复出丢失包时返回 ``(序号, 数据)``。"""
        block = self._block_for(packet.block_start, packet.count)
        block.parity = packet.payload

        missing = [
            packet.block_start + index
            for index in range(packet.count)
            if packet.block_start + index not in block.packets
        ]
        if len(missing) != 1:
            # 0 个缺失：无需恢复；2 个以上：XOR 无能为力
            if block.parity:
                self._blocks.pop(packet.block_start, None)
            return None

        recovered = block.parity
        for sequence, payload in block.packets.items():
            recovered = _xor_bytes(recovered, payload)

        index = missing[0] - packet.block_start
        original_length = packet.lengths[index] if index < len(packet.lengths) else len(recovered)
        recovered = recovered[:original_length]
        block.packets[missing[0]] = recovered
        self._blocks.pop(packet.block_start, None)
        self.recovered += 1
        logger.debug("FEC 恢复丢包: seq=%d (%d 字节)", missing[0], len(recovered))
        return missing[0], recovered

    def stats(self) -> dict[str, float]:
        return {"recovered": float(self.recovered), "pending_blocks": float(len(self._blocks))}


def _xor_bytes(left: bytes, right: bytes) -> bytes:
    """按字节异或，长度不等时用 0 补齐（与 FEC 组的定长语义一致）。"""
    if len(left) < len(right):
        left, right = right, left
    if not right:
        return bytes(left)
    head = bytes(a ^ b for a, b in zip(left[: len(right)], right))
    return head + bytes(left[len(right) :])


def suggest_block_size(loss_rate: float, max_redundancy: float = 0.25) -> int:
    """按实测丢包率选分组大小：丢包越高，冗余越密。"""
    if loss_rate <= 0:
        return 0
    ideal = max(2, int(round(1 / max(loss_rate, 1e-3))))
    floor = int(round(1 / max_redundancy))
    return max(2, min(ideal, 64, max(floor, 2)))
