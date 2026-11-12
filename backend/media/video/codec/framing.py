"""Incremental access-unit framing for Annex-B and length-delimited AV1."""

import struct

MAX_UNIT = 32 * 1024 * 1024


class HevcSplitter:
    def __init__(self):
        self.current = bytearray()
        self.has_vcl = False

    def push(self, nal):
        if len(nal) < 2:
            raise ValueError("Truncated HEVC NAL")
        kind = (nal[0] >> 1) & 63
        vcl = kind <= 31
        first = vcl and len(nal) > 2 and bool(nal[2] & 128)
        boundary = self.has_vcl and (first or kind in (32, 33, 34, 35, 39))
        result = [self.take()] if boundary else []
        self.current += b"\0\0\0\1" + nal
        self.has_vcl |= vcl
        if len(self.current) > MAX_UNIT:
            raise ValueError("HEVC access unit too large")
        return result

    def take(self):
        result = bytes(self.current) if self.has_vcl else None
        self.current.clear()
        self.has_vcl = False
        return result


class VideoFramer:
    def __init__(self, codec):
        from .base import AnnexBParser, AccessUnitSplitter
        self.codec = codec
        self.buffer = bytearray()
        self.header = False
        self.parser = AnnexBParser()
        self.splitter = HevcSplitter() if codec == "hevc" else AccessUnitSplitter()

    def push(self, chunk):
        if self.codec != "av1":
            output = []
            for nal in self.parser.push(chunk):
                output.extend(self.splitter.push(nal))
                current = (self.splitter.current if self.codec == "hevc" else self.splitter._current)
                if len(current) > MAX_UNIT:
                    raise ValueError("Access unit too large")
            if len(self.parser._buf) > MAX_UNIT:
                raise ValueError("NAL too large")
            return output
        self.buffer += chunk
        if not self.header:
            if len(self.buffer) < 32:
                return []
            if self.buffer[:4] != b"DKIF" or self.buffer[8:12] != b"AV01":
                raise ValueError("Invalid AV1 IVF header")
            if struct.unpack_from("<H", self.buffer, 6)[0] != 32:
                raise ValueError("Unsupported IVF header size")
            del self.buffer[:32]
            self.header = True
        output = []
        while len(self.buffer) >= 12:
            size = struct.unpack_from("<I", self.buffer)[0]
            if not 0 < size <= MAX_UNIT:
                raise ValueError("Invalid IVF frame size")
            if len(self.buffer) < size + 12:
                break
            output.append(bytes(self.buffer[12:12 + size]))
            del self.buffer[:12 + size]
        return output

    def flush(self):
        if self.codec == "av1":
            if self.buffer:
                raise ValueError("Truncated IVF frame")
            return []
        output = []
        for nal in self.parser.flush():
            output.extend(self.splitter.push(nal))
        tail = self.splitter.take()
        if tail:
            output.append(tail)
        return output


def sized_av1_obus(data):
    """Yield sized OBUs, rejecting truncated lengths and unbounded payloads."""
    offset = 0
    while offset < len(data):
        start = offset
        header = data[offset]
        offset += 1
        if header & 0x81 or not header & 2:
            raise ValueError("Invalid sized AV1 OBU")
        if header & 4:
            offset += 1
        size = 0
        for shift in range(0, 56, 7):
            if offset >= len(data):
                raise ValueError("Truncated OBU length")
            byte = data[offset]
            offset += 1
            size |= (byte & 127) << shift
            if not byte & 128:
                break
        else:
            raise ValueError("Invalid OBU length")
        if size > MAX_UNIT or offset + size > len(data):
            raise ValueError("Truncated OBU payload")
        yield (header >> 3) & 15, data[offset:offset + size], data[start:offset + size]
        offset += size


def av1_obus(data):
    for kind, body, _ in sized_av1_obus(data):
        yield kind, body


def av1_keyframe(data):
    reduced = False
    try:
        for kind, body in av1_obus(data):
            if kind == 1 and body:
                reduced = bool(body[0] & 8)
            if kind in (3, 6) and body:
                return reduced or (body[0] & 0xE0) == 0
    except ValueError:
        return False
    return False
