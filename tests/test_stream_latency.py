"""Bounded real-time delivery, frame timing and ordered input regressions."""

import io
import asyncio
import os
import queue
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication

from backend.config.schema import VideoEncodeConfig
from backend.control_channel.protocol import ControlCommand, EventType, InputEvent
from backend.control_channel.tcp import _ControlQueue
from backend.media.frame import Frame
from backend.media.packet import Packet
from backend.media.stream import StreamInfo
from backend.media.video.codec.base import FfmpegDecoderBase
from backend.media.video.codec.encoding.h264.libx264 import Libx264Encoder
from backend.pipeline.video_pipeline import VideoPipeline
from backend.pipeline.receive_pipeline import ReceivePipeline
from backend.media.video.codec.probe import _sample_packets
from frontend.desktop.service import SessionService, _QtRenderer

APP = QCoreApplication.instance() or QCoreApplication([])

class RtcRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from backend.media.video.codec.inprocess import InprocessDecoder
        from backend.pipeline.receive_pipeline import ReceivePipelineStats
        from backend.transport.webrtc.media_track import tune_receiver
        self.adapter = InprocessDecoder(StreamInfo(width=320, height=240))
        self.failures = []
        self.receiver = SimpleNamespace(
            _RTCRtpReceiver__active_ssrc={123: None}, _send_rtcp_pli=AsyncMock())
        self.cleanup = tune_receiver(self.receiver, self.adapter, self.failures.append,
                                     ReceivePipelineStats())
        self.q = self.receiver._RTCRtpReceiver__decoder_queue
        self.packets = _sample_packets("h264")

    async def asyncTearDown(self):
        self.cleanup()
        self.adapter.close()
        await asyncio.sleep(0)

    def feed(self, packet):
        encoded = SimpleNamespace(data=packet.data, timestamp=packet.pts)
        self.q.put((None, encoded))
        return self.q.decode(self.q.get_nowait()[1]) if not self.q.empty() else []

    async def test_real_invalid_frame_recovers_only_after_complete_idr(self):
        self.assertFalse(self.feed(self.packets[1]))
        self.assertTrue(self.q.waiting_keyframe)
        self.assertIsNotNone(self.q.last_error)
        self.assertFalse(self.failures)
        self.assertFalse(self.feed(self.packets[1]))
        await asyncio.sleep(.01)
        self.receiver._send_rtcp_pli.assert_awaited_once_with(123)
        self.assertTrue(self.feed(self.packets[0]))
        self.assertIsNone(self.q.recovering_since)
        self.assertTrue(self.feed(self.packets[1]))
        self.assertFalse(self.failures)

    async def test_overflow_discards_stale_generation_and_stops_while_waiting(self):
        self.assertTrue(self.feed(self.packets[0]))
        for index in range(5):
            self.q.put((None, SimpleNamespace(data=self.packets[1].data, timestamp=index)))
        self.assertTrue(self.q.waiting_keyframe)
        self.assertTrue(self.q.empty())
        self.assertTrue(self.feed(self.packets[0]))
        self.assertIsNone(self.q.recovering_since)
        self.q.recover()
        self.q.put(None)
        self.assertIsNone(self.q.get_nowait())

    async def test_old_queued_frame_is_ignored_and_cached_parameters_restore_idr(self):
        from aiortc.codecs.h264 import H264Encoder
        self.assertTrue(self.feed(self.packets[0]))
        old = SimpleNamespace(data=self.packets[1].data, timestamp=1)
        self.q.put((None, old))
        self.q.get_nowait()
        self.q.recover()
        self.assertFalse(self.q.decode(old))
        stripped = b"".join(b"\0\0\0\1" + nal
                            for nal in H264Encoder._split_bitstream(self.packets[0].data)
                            if nal[0] & 31 not in (7, 8))
        self.assertTrue(self.feed(Packet(data=stripped, pts=3000)))
        self.assertIsNone(self.q.recovering_since)
        self.assertFalse(self.failures)

    async def test_recovery_timeout_preserves_cause_and_request_is_rate_limited(self):
        self.feed(self.packets[1])
        cause = self.q.last_error
        for _ in range(10):
            self.q.request_keyframe()
        await asyncio.sleep(.01)
        self.receiver._send_rtcp_pli.assert_awaited_once()
        self.q.recovering_since = time.monotonic() - 6
        await asyncio.sleep(.3)
        self.assertEqual(len(self.failures), 1)
        self.assertIn("恢复超时", str(self.failures[0]))
        self.assertIs(self.failures[0].__cause__, cause)

    async def test_open_and_hardware_errors_remain_fatal(self):
        from backend.errors.codec import CodecOpenError
        original = CodecOpenError("hardware unavailable")
        with patch.object(self.adapter, "open", side_effect=original):
            self.feed(self.packets[0])
        self.assertEqual(self.failures, [original])
        self.assertFalse(self.q.waiting_keyframe)

    async def test_sender_supplies_cached_parameters_and_preserves_encode_failure(self):
        from backend.pipeline.video_pipeline import VideoPipelineStats
        from backend.transport.webrtc.media_track import SelectedEncoder
        from aiortc.codecs.h264 import H264Encoder
        nals = list(H264Encoder._split_bitstream(self.packets[0].data))
        stripped = b"".join(b"\0\0\0\1" + n for n in nals if n[0] & 31 not in (7, 8))
        adapter = Mock()
        adapter.encode.side_effect = [[self.packets[0]], [Packet(data=stripped)]]
        encoder = SelectedEncoder(adapter, VideoPipelineStats(), self.failures.append)
        with patch.object(encoder, "_packetize", side_effect=lambda items: list(items)):
            encoder.encode(frame().to_av())
            payloads, _ = encoder.encode(frame().to_av(), True)
        self.assertTrue({5, 7, 8}.issubset({n[0] & 31 for n in payloads}))
        original = RuntimeError("original encoder failure")
        adapter.encode.side_effect = original
        with self.assertRaises(RuntimeError) as caught:
            encoder.encode(frame().to_av())
        self.assertIs(caught.exception, original)
        self.assertEqual(self.failures, [original])


class H264Decoder(FfmpegDecoderBase):
    """Exercise the shared subprocess path still used by hardware adapters."""
    name = codec = input_format = "h264"
    ffmpeg_decoder = None
    hardware = False


def frame(index=0):
    return Frame(data=bytes([index % 256, 0, 0, 255]) * (64 * 48),
                 width=64, height=48, pts=index * 750)


class StreamLatencyTests(unittest.TestCase):
    def test_stop_never_blocks_gui_on_source_termination_and_runs_once(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        pipeline = ReceivePipeline(None, None, None, VideoEncodeConfig(realtime=True))
        def abort():
            calls.append(True)
            entered.set()
            release.wait(2)
        pipeline.abort_source = abort
        try:
            start = time.perf_counter()
            pipeline.stop()
            pipeline.stop()
            self.assertLess(time.perf_counter() - start, 0.5)
            self.assertTrue(entered.wait(1))
            self.assertEqual(len(calls), 1)
        finally:
            release.set()
            pipeline._abort_thread.join(2)

    def test_pipe_decoder_bounds_inflight_frames_and_abort_releases_feeder(self):
        from unittest.mock import Mock
        decoder = H264Decoder(StreamInfo(width=1, height=1), VideoEncodeConfig(realtime=True))
        decoder._proc = Mock(stdin=io.BytesIO())
        decoder._proc.poll.return_value = None
        for index in range(4):
            decoder._pending_pts.append((index, time.perf_counter()))
        errors, done = [], threading.Event()
        def feed():
            try:
                decoder.decode(Packet(data=b"extra", pts=4))
            except Exception as exc:
                errors.append(exc)
            finally:
                done.set()
        thread = threading.Thread(target=feed)
        thread.start()
        try:
            self.assertFalse(done.wait(0.05))
            self.assertEqual(len(decoder._pending_pts), 4)
            decoder.abort()
            self.assertTrue(done.wait(1))
            self.assertEqual(len(errors), 1)
        finally:
            decoder.abort()
            thread.join(2)

    def test_receiving_continues_while_decoder_blocks_and_recovers_at_keyframe(self):
        from dataclasses import replace
        entered, received = threading.Event(), threading.Event()
        first, delta, _ = _sample_packets("h264")
        packets = [replace(first, pts=0)]
        packets += [replace(delta, pts=index * 3000) for index in range(1, 9)]
        packets += [replace(first, pts=27000), replace(delta, pts=30000)]
        decoded, decoders = [], []

        class Demux:
            streams = [StreamInfo(width=320, height=240)]
            index = 0
            def open(self):
                pass
            def read(self):
                if self.index == 1:
                    if not entered.wait(2):
                        raise RuntimeError("decoder did not start")
                if self.index == len(packets):
                    received.set()
                    return None
                packet = packets[self.index]
                self.index += 1
                return packet
            def close(self):
                pass

        class Decoder:
            name = "test"
            def open(self):
                pass
            def decode(self, packet):
                if packet.pts == 0:
                    entered.set()
                    if not received.wait(2):
                        raise RuntimeError("receiving blocked on decoding")
                decoded.append(packet.pts)
                return []
            def flush(self):
                return []
            def close(self):
                pass

        def factory(*_):
            decoder = Decoder()
            decoders.append(decoder)
            return decoder
        renderer = SimpleNamespace(open=lambda: None, close=lambda: None)
        pipeline = ReceivePipeline(Demux(), factory, renderer, VideoEncodeConfig(realtime=True))
        pipeline.run()
        self.assertEqual(decoded, [0, 27000, 30000])
        self.assertEqual(len(decoders), 2)
        self.assertEqual(pipeline._packets.maxsize, 4)
        self.assertEqual(pipeline.stats.extra["packets_dropped"], 8)
        self.assertEqual(pipeline.stats.extra["decoder_resets"], 1)
        self.assertFalse(pipeline._receiver.is_alive())
        self.assertTrue(pipeline.stats.extra["decode_queue_ms"])

    def test_decoder_failure_unblocks_receiver_and_keeps_original_exception(self):
        from unittest.mock import Mock
        original = OSError(22, "original decoding failure")
        release = threading.Event()
        packet = _sample_packets("h264")[0]
        class Demux:
            streams = [StreamInfo(width=320, height=240)]
            index = 0
            def open(self):
                pass
            def read(self):
                self.index += 1
                if self.index == 1:
                    return packet
                release.wait(2)
                return None
            def close(self):
                pass
        decoder = Mock(name="decoder")
        decoder.decode.side_effect = original
        decoder.diagnostics.return_value = {"exit_code": 22}
        pipeline = ReceivePipeline(Demux(), lambda *_: decoder, Mock(),
                                   VideoEncodeConfig(realtime=True))
        pipeline.abort_source = release.set
        with self.assertRaises(OSError) as caught:
            pipeline.run()
        self.assertIs(caught.exception, original)
        self.assertEqual(original.process_diagnostics["exit_code"], 22)
        self.assertFalse(pipeline._receiver.is_alive())

    def test_expected_shutdown_marks_media_stop(self):
        service = SessionService()
        service.expect_stop()
        self.assertTrue(service._stop.is_set())

    def test_prepare_stop_marks_expectation_without_closing_media_and_new_session_clears_it(self):
        service = SessionService()
        service.expect_stop(stop_media=False)
        self.assertTrue(service._expected_stop.is_set())
        self.assertFalse(service._stop.is_set())
        service._begin("viewer")
        self.assertFalse(service._expected_stop.is_set())
        service.expect_stop()
        self.assertTrue(service._stop.is_set())

    def test_slow_gui_receives_one_newest_frame_and_rejects_old_generation(self):
        service = SessionService()
        received, notifications = [], []
        service.frame_ready.connect(received.append)
        service._frame_pending.connect(notifications.append)
        sink = service._frame_sink()
        thread = threading.Thread(target=lambda: [sink(frame(index)) for index in range(120)])
        thread.start()
        thread.join()
        self.assertEqual(len(notifications), 0)
        APP.processEvents()
        self.assertEqual([item.pts for item in received], [119 * 750])
        self.assertEqual(len(notifications), 1)
        sink(frame(120))
        service._clear_frames()
        new_sink = service._frame_sink()
        sink(frame(121))
        new_sink(frame(122))
        APP.processEvents()
        self.assertEqual([item.pts for item in received], [119 * 750, 122 * 750])

    def test_slow_preview_processes_latest_pending_frame_only(self):
        entered, resume, done = threading.Event(), threading.Event(), threading.Event()
        received = []

        def deliver(item):
            received.append(item.pts)
            if len(received) == 1:
                entered.set()
                resume.wait(2)
            if item.pts == 120 * 750:
                done.set()

        renderer = _QtRenderer(deliver)
        renderer.open()
        try:
            renderer.render(frame())
            self.assertTrue(entered.wait(2))
            for index in range(1, 121):
                renderer.render(frame(index))
            resume.set()
            self.assertTrue(done.wait(2))
            self.assertEqual(received, [0, 120 * 750])
        finally:
            resume.set()
            renderer.close()

    def test_capture_pacing_and_pts_support_30_60_120_fps(self):
        for fps in (30, 60, 120):
            capture = SimpleNamespace(config=SimpleNamespace(fps=fps), read=lambda: frame())
            pipeline = VideoPipeline(capture, None, None, VideoEncodeConfig(realtime=True))
            clock = [0.0]
            captured = []
            pipeline._offer = captured.append
            with patch("backend.pipeline.video_pipeline.time.perf_counter", side_effect=lambda: clock[0]), \
                    patch("backend.qos.pacing.wait_until", side_effect=lambda deadline: clock.__setitem__(0, deadline)):
                pipeline._capture_loop(None, 10)
            frames = [item[0] for item in captured if isinstance(item, tuple)]
            self.assertEqual(len(frames), 10)
            self.assertEqual([item.pts for item in frames], [round(index * 90000 / fps) for index in range(10)])
            self.assertAlmostEqual(clock[0], 10 / fps)
            self.assertEqual(pipeline.queue_size, 1)

    def test_adjacent_motion_coalesces_without_crossing_buttons_or_epochs(self):
        messages = _ControlQueue()
        move = lambda x, mode="normalized", epoch=1: InputEvent(
            EventType.MOUSE_MOVE, x=x, y=x, extra={"coordinates": mode, "epoch": epoch})
        messages.put(move(10))
        messages.put(move(20))
        button = InputEvent(EventType.MOUSE_BUTTON, button=1, pressed=True)
        messages.put(button)
        messages.put(move(2, "relative"))
        messages.put(move(3, "relative"))
        release = ControlCommand("input_focus", {"active": False, "epoch": 2})
        messages.put(release)
        messages.put(move(30, epoch=2))
        messages.put(move(40, epoch=3))
        result = [messages.get_nowait() for _ in range(messages.qsize())]
        self.assertEqual([item.x for item in result if isinstance(item, InputEvent)
                          and item.event_type is EventType.MOUSE_MOVE], [20, 5, 30, 40])
        self.assertIs(result[1], button)
        self.assertIs(result[3], release)
        messages.put(move(30000, "relative"))
        messages.put(move(30000, "relative"))
        self.assertEqual(messages.qsize(), 2)

    def test_realtime_decode_feeds_without_waiting_and_assigns_pts_at_output(self):
        decoder = H264Decoder(StreamInfo(width=1, height=1), VideoEncodeConfig(realtime=True))
        decoder._proc = SimpleNamespace(stdin=io.BytesIO())
        packet = Packet(data=b"test", pts=900)
        def get(*, block=True, timeout=None):
            self.assertFalse(block, "real-time decode must not wait for output")
            raise queue.Empty
        with patch.object(decoder._frames, "get", side_effect=get):
            self.assertEqual(decoder.decode(packet), [])
        output = decoder._build_frame(b"\0\0\0\xff")
        self.assertEqual(output.pts, 900)
        self.assertGreaterEqual(decoder.output_wait_ms[-1], 0)

    def test_real_decoder_outputs_during_feed_and_file_decode_keeps_every_frame(self):
        stream = StreamInfo(width=64, height=48, framerate=120, metadata={"pixel_format": "bgra"})
        config = VideoEncodeConfig(preset="ultrafast", gop=30, max_b_frames=0)
        encoder = Libx264Encoder(stream, config)
        packets = []
        encoder.open()
        try:
            for index in range(60):
                packets.extend(encoder.encode(frame(index)))
            packets.extend(encoder.flush())
        finally:
            encoder.close()
        self.assertEqual(len(packets), 60)
        for realtime in (True, False):
            config.realtime = realtime
            decoder = H264Decoder(stream, config)
            frames, available = [], threading.Event()
            if realtime:
                def output(item):
                    frames.append(item)
                    available.set()
                decoder.output_callback = output
            decoder.open()
            try:
                for packet in packets:
                    frames.extend(decoder.decode(packet))
                if realtime:
                    self.assertTrue(available.wait(3), "output must precede stdin EOF")
                    command = decoder.diagnostics()["command"]
                    self.assertLess(command.index("-threads"), command.index("-i"))
                    self.assertIn("-probesize", command)
                frames.extend(decoder.flush())
                self.assertEqual([item.pts for item in frames], [item.pts for item in packets])
                self.assertLessEqual(decoder._frames.qsize(), 1)
            finally:
                decoder.close()


class LiveCodecTests(unittest.TestCase):
    def test_encoder_replaces_on_keyframe_and_failed_switch_keeps_previous(self):
        from dataclasses import replace
        from backend.config.schema import VideoEncodeConfig
        from backend.media.video.codec.inprocess import InprocessEncoder, InprocessDecoder
        stream = StreamInfo(width=320, height=240, framerate=30)
        config = VideoEncodeConfig(encoder="libx264", realtime=True, on_demand_keyframes=True)
        encoder = InprocessEncoder(stream, config)
        decoder = InprocessDecoder(stream)
        encoder.open()
        decoder.open()
        try:
            first = Frame(data=bytes((0, 0, 255, 255)) * (320 * 240), width=320, height=240)
            self.assertTrue(decoder.decode(encoder.encode(first)[0]))
            changed = stream.copy_with(width=640, height=360, framerate=120)
            future = encoder.request_update(changed, replace(config, bitrate_kbps=6000))
            frame = Frame(data=bytes((0, 255, 0, 255)) * (640 * 360),
                          width=640, height=360, pts=3000)
            encoder.force_keyframe = True
            packets = encoder.encode(frame)
            self.assertIsNone(future.result())
            self.assertTrue(packets[0].keyframe)
            decoded = decoder.decode(packets[0])
            self.assertEqual((decoded[0].width, decoded[0].height), (640, 360))
            previous = encoder._context
            future = encoder.request_update(stream, replace(config, encoder="does_not_exist"))
            delta, = encoder.encode(frame.with_data(frame.data, pts=6000))
            self.assertFalse(delta.keyframe)
            self.assertIs(encoder._context, previous)
            with self.assertRaises(Exception):
                future.result()
        finally:
            encoder.close()
            decoder.close()

    def test_decoder_failure_preserves_original_and_success_waits_for_idr(self):
        from backend.media.video.codec.inprocess import InprocessDecoder
        from backend.media.video.codec.base import SwitchableDecoder
        stream = StreamInfo(width=320, height=240)
        original = InprocessDecoder(stream)
        original.open()
        decoder = SwitchableDecoder(original)
        packets = _sample_packets("h264")
        try:
            self.assertTrue(decoder.decode(packets[0]))
            future = decoder.request_switch(lambda: (_ for _ in ()).throw(RuntimeError("unavailable")))
            self.assertTrue(decoder.decode(packets[0]))
            self.assertIs(decoder.current, original)
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                future.result()
            class DelayedDecoder(InprocessDecoder):
                def decode(self, packet):
                    frames = super().decode(packet)
                    if packet.keyframe:
                        return []
                    return frames

            future = decoder.request_switch(lambda: DelayedDecoder(stream))
            self.assertTrue(decoder.decode(packets[1]))
            self.assertFalse(future.done())
            self.assertTrue(decoder.decode(packets[0]))
            self.assertFalse(future.done())
            self.assertIs(decoder.current, original)
            self.assertTrue(decoder.decode(packets[1]))
            self.assertTrue(future.done())
            self.assertIsNot(decoder.current, original)
        finally:
            decoder.close()


class EncodeAheadTests(unittest.IsolatedAsyncioTestCase):
    async def test_prefetch_is_bounded_and_update_drains_in_order(self):
        from backend.transport.webrtc.media_track import EncodeAhead

        encode = AsyncMock(side_effect=iter((1, 2, 3, 4)))
        sender = SimpleNamespace(_next_encoded_frame=encode, _run_rtp=AsyncMock())
        pipeline = EncodeAhead(sender)
        self.addAsyncCleanup(pipeline.close)
        self.assertEqual(await sender._next_encoded_frame(None), 1)
        await asyncio.sleep(0)
        self.assertEqual(encode.await_count, 2)
        entered = asyncio.Event()

        async def update():
            async with pipeline.ordered_update():
                entered.set()

        update_task = asyncio.create_task(update())
        await asyncio.sleep(0)
        self.assertEqual(await sender._next_encoded_frame(None), 2)
        self.assertFalse(entered.is_set())
        self.assertEqual(encode.await_count, 2)
        self.assertEqual(await sender._next_encoded_frame(None), 3)
        await update_task
        self.assertTrue(entered.is_set())

    async def test_rtp_exit_waits_for_encoder_before_cleanup(self):
        from backend.transport.webrtc.media_track import EncodeAhead

        release = asyncio.Event()
        encoding = asyncio.Event()
        exiting = asyncio.Event()

        async def encode(_):
            if encoding.is_set():
                await release.wait()
            encoding.set()
            return 1

        async def send(codec):
            await sender._next_encoded_frame(codec)
            exiting.set()

        sender = SimpleNamespace(_next_encoded_frame=encode, _run_rtp=send)
        pipeline = EncodeAhead(sender)
        running = asyncio.create_task(sender._run_rtp(None))
        try:
            await asyncio.wait_for(exiting.wait(), 1)
            self.assertFalse(running.done())
        finally:
            release.set()
            await asyncio.wait_for(running, 1)
        self.assertIsNone(pipeline._task)


class PacingTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_install_updates_budget_and_priority_bypasses_debt(self):
        import struct
        from backend.qos.pacing import install_rtp_pacer
        from unittest.mock import Mock
        transport = Mock()
        transport._send_rtp = AsyncMock()
        transport._elink_pacer = None
        original = transport._send_rtp
        senders = [SimpleNamespace(kind="video", _ssrc=1, _rtx_ssrc=3, transport=transport),
                   SimpleNamespace(kind="audio", _ssrc=2, transport=transport)]
        pc = SimpleNamespace(getTransceivers=lambda: [
            SimpleNamespace(kind=s.kind, sender=s) for s in senders])
        install_rtp_pacer(pc, 4_000_000, 60)
        wrapper = transport._send_rtp
        install_rtp_pacer(pc, 8_000_000, 120)
        self.assertIs(transport._send_rtp, wrapper)
        self.assertEqual(transport._elink_pacer.rate, 8_000_000 * 1.08)
        packet = b"\x80\x60\0\1" + struct.pack("!II", 0, 2)
        with patch("backend.qos.pacing.wait_until") as wait:
            transport._elink_pacer.due = time.perf_counter() + 100
            await wrapper(packet)
            wait.assert_not_called()
        original.assert_awaited_once_with(packet)


if __name__ == "__main__":
    unittest.main()
