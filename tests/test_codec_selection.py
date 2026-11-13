"""Runtime codec validation and format-independent media contracts."""

import base64
import io
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.config.schema import VideoEncodeConfig
from backend.media.container.mpegts import MpegTsDemuxer, MpegTsMuxer
from backend.media.frame import Frame
from backend.media.packet import Packet
from backend.media.stream import CodecId, StreamInfo
from backend.media.video.codec.framing import VideoFramer
from backend.media.video.codec.probe import (
    PROBE_SAMPLES, _sample_packets, checked_capabilities, codec_options,
    compatible_choices, probe_video_codecs, public_capabilities, verify_backend,
    RecoveryPoint,
)
from backend.registry.codec_registry import CodecRegistry, build_default_registry
from backend.errors.codec import CodecNotFoundError
from backend.pipeline.receive_pipeline import ReceivePipeline
from backend.transport.srt import SrtTransport
from backend.media.video.codec.base import write_all


def caps(kind, names):
    return {"encoders": names if kind == "encoders" else {},
            "decoders": names if kind == "decoders" else {}}


def available(codec):
    return {"codec": codec, "state": "available"}


class SelectionTests(unittest.TestCase):
    def test_inprocess_names_are_valid_and_describe_actual_backend(self):
        from backend.media.video.codec.probe import codec_label
        for codec in PROBE_SAMPLES:
            for suffix in ("_pyav", "_pyav_d3d11va"):
                name = codec + suffix
                self.assertEqual(codec_options({"codec": codec, "decoder": name})["decoder"], name)
                self.assertNotIn("NVIDIA", codec_label(name))
        self.assertIn("D3D11VA", codec_label("h264_pyav_d3d11va"))

    def test_inprocess_software_decodes_all_formats_with_separate_timing(self):
        from backend.media.video.codec.inprocess import InprocessDecoder
        for codec in PROBE_SAMPLES:
            stream = StreamInfo(codec=CodecId(codec), width=320, height=240)
            adapter = InprocessDecoder(stream, VideoEncodeConfig(realtime=True))
            self.assertIsNone(verify_backend(adapter), codec)
            self.assertEqual(len(adapter.convert_ms), 3)
            self.assertTrue(adapter.decode_ms)
            self.assertIsNone(adapter._context)

    def test_recovery_reuses_context_and_keeps_frame_buffers_alive(self):
        from backend.media.video.codec.inprocess import InprocessDecoder
        for codec in PROBE_SAMPLES:
            decoder = InprocessDecoder(StreamInfo(codec=CodecId(codec), width=320, height=240),
                                       VideoEncodeConfig(realtime=True))
            decoder.open()
            try:
                context = decoder._context
                packet = _sample_packets(codec)[0]
                original, = decoder.decode(packet)
                pixels = bytes(original.data)
                decoder.reset(packet)
                self.assertIs(decoder._context, context)
                restored, = decoder.decode(packet)
                self.assertEqual(bytes(restored.data), pixels)
            finally:
                decoder.close()
            self.assertEqual(bytes(original.data), pixels)

    def test_recovery_reuses_parameters_and_preserves_pts_for_all_formats(self):
        from backend.media.packet import split_annexb
        from backend.media.video.codec.framing import sized_av1_obus
        for codec in PROBE_SAMPLES:
            first, delta, _ = _sample_packets(codec)
            recovery = RecoveryPoint()
            self.assertIsNone(self._recover_without_parameters(codec, first))
            recovery.observe(first)
            recovery.observe(delta)
            self.assertIsNone(recovery.recover(delta), codec)
            if codec == "av1":
                data = b"".join(raw for kind, _, raw in sized_av1_obus(first.data) if kind != 1)
            else:
                excluded = (32, 33, 34) if codec == "hevc" else (7, 8)
                data = b"".join(b"\0\0\0\1" + nal for nal in split_annexb(first.data)
                                if ((nal[0] >> 1) & 63 if codec == "hevc" else nal[0] & 31) not in excluded)
            key = Packet(data=data, codec=CodecId(codec), pts=9000)
            recovery.observe(key)
            restored = recovery.recover(key)
            self.assertIsNotNone(restored, codec)
            self.assertEqual(restored.pts, 9000)
            self.assertTrue(restored.keyframe)
            from backend.media.video.codec.inprocess import InprocessDecoder
            decoder = InprocessDecoder(StreamInfo(codec=CodecId(codec), width=320, height=240),
                                       VideoEncodeConfig(realtime=True))
            decoder.open()
            try:
                self.assertEqual(len(decoder.decode(restored)), 1, codec)
            finally:
                decoder.close()

    @staticmethod
    def _recover_without_parameters(codec, packet):
        recovery = RecoveryPoint()
        recovery.codec = codec
        return recovery.recover(packet)

    def test_cross_vendor_negotiation_and_explicit_format(self):
        remote = caps("encoders", {"h264_nvenc": available("h264"), "libx265": available("hevc")})
        local = caps("decoders", {"h264_qsv": available("h264"), "hevc": available("hevc")})
        self.assertEqual(compatible_choices(remote, local, {})[0],
                         {"codec": "h264", "encoder": "h264_nvenc", "decoder": "h264_qsv"})
        self.assertEqual(compatible_choices(remote, local, {"codec": "hevc"})[0]["encoder"], "libx265")
        with self.assertRaises(ValueError):
            compatible_choices(remote, local, {"encoder": "h264_amf"})

    def test_invalid_options_and_untrusted_capabilities(self):
        for options in ({"codec": "vp9"}, {"encoder": "-i evil"}, {"decoder": []},
                        {"codec": "hevc", "encoder": "libx264"}):
            with self.assertRaises(ValueError):
                codec_options(options)
        with self.assertRaises(ValueError):
            checked_capabilities(caps("decoders", {"h264": available("av1")}))
        with self.assertRaises(ValueError):
            checked_capabilities(caps("encoders", {"arbitrary": available("h264")}))

    def test_unavailable_entries_are_not_advertised(self):
        snapshot = caps("encoders", {"libx264": available("h264"), "h264_nvenc": {
            "codec": "h264", "state": "unavailable", "reason": "driver missing"}})
        self.assertEqual(list(public_capabilities(snapshot)["encoders"]), ["libx264"])

    def test_registry_explicit_selection_never_silently_falls_back(self):
        registry = CodecRegistry()
        registry.register_encoder("h264", "libx264", lambda *_: object())
        with self.assertRaises(CodecNotFoundError):
            registry.create_encoder("h264", "h264_nvenc", StreamInfo(), VideoEncodeConfig())

    def test_watchdog_kills_cancelled_probe_and_closes_adapter(self):
        opened, killed = threading.Event(), threading.Event()
        class Process:
            def kill(self):
                killed.set()
        class Adapter:
            codec = "h264"
            _proc = Process()
            closed = False
            def open(self):
                opened.set()
                killed.wait(1)
            def _stderr_text(self):
                return ""
            def decode(self, _):
                return []
            def flush(self):
                return []
            def close(self):
                self.closed = True
        adapter = Adapter()
        self.assertIn("超时", verify_backend(adapter, timeout=0.05))
        self.assertTrue(killed.is_set())
        self.assertTrue(adapter.closed)

    def test_cache_invalidates_for_driver_change_and_manual_refresh(self):
        from backend.media.video.codec import probe
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "ffmpeg"
            path.touch()
            probe._video_cache.clear()
            with patch("backend.registry.codec_registry.build_default_registry", return_value=CodecRegistry()) as build, patch(
                "backend.media.video.codec.probe._device_signature", side_effect=[("a",), ("a",), ("b",), ("b",)]
            ):
                first = probe_video_codecs(str(path))
                self.assertIs(first, probe_video_codecs(str(path)))
                probe_video_codecs(str(path))
                probe_video_codecs(str(path), refresh=True)
                self.assertEqual(build.call_count, 3)
            probe._video_cache.clear()


class FramingTests(unittest.TestCase):
    def test_all_samples_have_three_correct_keyframed_packets(self):
        for codec in PROBE_SAMPLES:
            packets = _sample_packets(codec)
            self.assertEqual(len(packets), 3, codec)
            self.assertTrue(packets[0].keyframe, codec)
            self.assertFalse(packets[1].keyframe, codec)

    def test_every_byte_boundary_and_eof(self):
        for codec in PROBE_SAMPLES:
            packets = _sample_packets(codec)
            if codec == "av1":
                data = (struct.pack("<4sHH4sHHIIII", b"DKIF", 0, 32, b"AV01",
                                    320, 240, 30, 1, 3, 0)
                        + b"".join(struct.pack("<IQ", p.size, i) + p.data for i, p in enumerate(packets)))
            else:
                data = b"".join(p.data for p in packets)
            for step in (1, 3, 13, len(data)):
                framer = VideoFramer(codec)
                output = []
                for offset in range(0, len(data), step):
                    output.extend(framer.push(data[offset:offset + step]))
                output.extend(framer.flush())
                self.assertEqual(output, [p.data for p in packets], (codec, step))

    def test_ivf_rejects_invalid_or_truncated_frames(self):
        framer = VideoFramer("av1")
        with self.assertRaises(ValueError):
            framer.push(bytes(32))
        framer = VideoFramer("av1")
        data = struct.pack("<4sHH4sHHIIII", b"DKIF", 0, 32, b"AV01", 320, 240, 30, 1, 1, 0)
        framer.push(data + struct.pack("<IQ", 10, 0) + b"x")
        with self.assertRaises(ValueError):
            framer.flush()

    def test_ts_preserves_three_formats_metadata_payload_and_random_access(self):
        for codec in PROBE_SAMPLES:
            packets = _sample_packets(codec)
            stream = StreamInfo(codec=CodecId(codec), width=320, height=240, framerate=120)
            sink = io.BytesIO()
            muxer = MpegTsMuxer(sink)
            muxer.open([stream])
            for packet in packets:
                muxer.write(packet)
            demuxer = MpegTsDemuxer(io.BytesIO(sink.getvalue()))
            demuxer.open()
            restored = []
            while (packet := demuxer.read()) is not None:
                restored.append(packet)
            self.assertEqual([(p.data, p.pts, p.codec, p.keyframe) for p in restored],
                             [(p.data, p.pts, p.codec, p.keyframe) for p in packets])
            actual = demuxer.streams[0]
            self.assertEqual((actual.width, actual.height, actual.framerate), (320, 240, 120))

    def test_receive_pipeline_uses_av1_description_without_sps(self):
        stream = StreamInfo(codec=CodecId.AV1, width=320, height=240, framerate=120)
        class Demux:
            streams = [stream]
        class Decoder:
            name = "av1"
            def open(self):
                self.opened = True
        decoder = Decoder()
        pipeline = ReceivePipeline(Demux(), lambda *_: decoder, None,
                                   VideoEncodeConfig(codec="av1"))
        self.assertIs(pipeline._open_decoder(_sample_packets("av1")), decoder)
        self.assertTrue(decoder.opened)

    def test_file_receive_selects_decoder_using_the_actual_stream_format(self):
        from backend.bootstrap import build_receive_resources
        from backend.config.schema import AppConfig
        snapshot = caps("decoders", {"av1": available("av1")})
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "sample.ts"
            path.touch()
            config = AppConfig(mode="receive")
            config.transport.path = str(path)
            registry = CodecRegistry()
            registry.register_decoder("av1", "av1", lambda stream, _: stream)
            with patch("backend.bootstrap.probe_video_codecs", return_value=snapshot):
                runtime = build_receive_resources(config, registry)
                try:
                    stream = StreamInfo(codec=CodecId.AV1, width=320, height=240)
                    self.assertIs(runtime.get("pipeline").decoder_factory(stream, config.encode), stream)
                finally:
                    runtime.close()


class RuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.capabilities = probe_video_codecs()
        cls.registry = build_default_registry()
        print("Runtime verified:", {kind: [n for n, v in items.items() if v["state"] == "available"]
                                    for kind, items in cls.capabilities.items()}, flush=True)

    def test_real_decoder_samples_independent_of_local_encoders(self):
        for codec in PROBE_SAMPLES:
            name = codec
            item = self.capabilities["decoders"].get(name, {})
            self.assertEqual(item.get("state"), "available", item.get("reason"))

    def test_automatic_choices_prioritize_measured_inprocess_latency(self):
        for codec in PROBE_SAMPLES:
            measured = {name: item["latency_ms"] for name, item in self.capabilities["decoders"].items()
                        if item["codec"] == codec and item["state"] == "available" and "latency_ms" in item}
            choices = [name for name, item in self.capabilities["decoders"].items()
                       if item["codec"] == codec and item["state"] == "available"]
            self.assertTrue(measured, codec)
            self.assertEqual(choices[0], min(measured, key=measured.get))

    def test_real_available_encoders_produce_decodable_ts(self):
        for name, item in self.capabilities["encoders"].items():
            if item["state"] != "available":
                continue
            codec = item["codec"]
            stream = StreamInfo(codec=CodecId(codec), width=320, height=240, framerate=120)
            config = VideoEncodeConfig(codec=codec, encoder=name, preset="ultrafast", realtime=False, gop=4)
            encoder = self.registry.create_encoder(codec, name, stream, config)
            packets = []
            encoder.open()
            try:
                for index in range(12):
                    packets.extend(encoder.encode(Frame(data=bytes([index * 8, 32, 200, 255]) * (320 * 240),
                                                       width=320, height=240, pts=index * 750)))
                packets.extend(encoder.flush())
            finally:
                encoder.close()
            self.assertEqual(len(packets), 12, name)
            self.assertEqual([p.pts for p in packets], [index * 750 for index in range(12)], name)
            sink = io.BytesIO()
            muxer = MpegTsMuxer(sink)
            muxer.open([stream])
            for packet in packets:
                muxer.write(packet)
            demuxer = MpegTsDemuxer(io.BytesIO(sink.getvalue()))
            decoder = self.registry.create_decoder(codec, codec, stream, config)
            frames = []
            decoder.open()
            try:
                demuxer.open()
                while (packet := demuxer.read()) is not None:
                    frames.extend(decoder.decode(packet))
                frames.extend(decoder.flush())
            finally:
                decoder.close()
            self.assertEqual(len(frames), 12, name)
            self.assertEqual([f.pts for f in frames], [index * 750 for index in range(12)], name)

    def test_srt_transfers_all_format_payloads_byte_for_byte(self):
        data = bytearray()
        for codec in PROBE_SAMPLES:
            stream = StreamInfo(codec=CodecId(codec), width=320, height=240, framerate=30)
            sink = io.BytesIO()
            muxer = MpegTsMuxer(sink)
            muxer.open([stream])
            for packet in _sample_packets(codec):
                muxer.write(packet)
            data += sink.getvalue()
        expected = bytes(data) * 50
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reserve:
            reserve.bind(("127.0.0.1", 0))
            port = reserve.getsockname()[1]
        sender = SrtTransport(f"srt://127.0.0.1:{port}", mode="listener", direction="send")
        receiver = SrtTransport(f"srt://127.0.0.1:{port}", mode="caller", direction="receive")
        received, failures = bytearray(), []
        ready = threading.Event()

        def receive():
            try:
                while len(received) < len(expected):
                    chunk = receiver.reader.read(min(65536, len(expected) - len(received)))
                    if not chunk:
                        break
                    received.extend(chunk)
            except Exception as exc:
                failures.append(str(exc))
            finally:
                ready.set()

        def send():
            try:
                write_all(sender.writer, expected)
            except Exception as exc:
                failures.append(str(exc))
        threads = []
        try:
            sender.open()
            receiver.open()
            threads = [threading.Thread(target=receive), threading.Thread(target=send)]
            for thread in threads:
                thread.start()
            self.assertTrue(ready.wait(10), (len(received), sender.stderr_text(), receiver.stderr_text()))
            self.assertFalse(failures)
            self.assertEqual(bytes(received), expected, (sender.stderr_text(), receiver.stderr_text()))
        finally:
            sender.abort()
            receiver.abort()
            for thread in threads:
                thread.join(3)
            sender.close()
            receiver.close()


if __name__ == "__main__":
    unittest.main()
