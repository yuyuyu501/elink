"""Bounded real-time delivery, frame timing and ordered input regressions."""

import io
import os
import queue
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication

from backend.config.schema import VideoEncodeConfig
from backend.control_channel.protocol import ControlCommand, EventType, InputEvent
from backend.control_channel.tcp import _ControlQueue
from backend.media.frame import Frame
from backend.media.packet import Packet
from backend.media.stream import StreamInfo
from backend.media.video.codec.decoding.h264.h264 import H264Decoder
from backend.media.video.codec.encoding.h264.libx264 import Libx264Encoder
from backend.pipeline.video_pipeline import VideoPipeline
from backend.transport.srt import build_srt_url
from frontend.desktop.service import SessionService, _QtRenderer

APP = QCoreApplication.instance() or QCoreApplication([])


def frame(index=0):
    return Frame(data=bytes([index % 256, 0, 0, 255]) * (64 * 48),
                 width=64, height=48, pts=index * 750)


class StreamLatencyTests(unittest.TestCase):
    def test_expected_shutdown_marks_media_end_before_transport_is_aborted(self):
        service = SessionService()
        service._transport = SimpleNamespace(diagnostics=lambda: {})
        service.expect_stop()
        self.assertTrue(service._stop.is_set())
        service._check_media_end()

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

    def test_slow_upscale_processes_latest_pending_frame_only(self):
        entered, resume, done = threading.Event(), threading.Event(), threading.Event()
        processed, received = [], []

        class Upscale:
            def upscale(self, item):
                processed.append(item.pts)
                if len(processed) == 1:
                    entered.set()
                    resume.wait(2)
                return item

            def close(self):
                pass

        def deliver(item):
            received.append(item.pts)
            if item.pts == 120 * 750:
                done.set()

        renderer = _QtRenderer(deliver, Upscale())
        renderer.open()
        try:
            renderer.render(frame())
            self.assertTrue(entered.wait(2))
            for index in range(1, 121):
                renderer.render(frame(index))
            resume.set()
            self.assertTrue(done.wait(2))
            self.assertEqual(processed, [0, 120 * 750])
            self.assertEqual(received, processed)
        finally:
            resume.set()
            renderer.close()

    def test_upscaler_is_created_used_and_closed_on_its_worker_thread(self):
        threads = []
        ready = threading.Event()
        class Upscale:
            def __init__(self):
                threads.append(threading.get_ident())

            def upscale(self, item):
                threads.append(threading.get_ident())
                return item

            def close(self):
                threads.append(threading.get_ident())
        renderer = _QtRenderer(lambda _: ready.set(), upscaler_factory=Upscale)
        renderer.open()
        renderer.render(frame())
        self.assertTrue(ready.wait(2))
        renderer.close()
        self.assertEqual(len(threads), 3)
        self.assertEqual(len(set(threads)), 1)
        self.assertNotEqual(threads[0], threading.get_ident())
    def test_capture_pacing_and_pts_support_30_60_120_fps(self):
        for fps in (30, 60, 120):
            capture = SimpleNamespace(config=SimpleNamespace(fps=fps), read=lambda: frame())
            pipeline = VideoPipeline(capture, None, None, VideoEncodeConfig(realtime=True))
            clock = [0.0]
            pipeline._stop_event.wait = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
            captured = []
            pipeline._offer = captured.append
            with patch("backend.pipeline.video_pipeline.time.perf_counter", side_effect=lambda: clock[0]):
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

    def test_srt_latency_converts_ms_to_us_without_duplicate_options(self):
        url = build_srt_url("srt://localhost:9000?latency=1&streamid=test",
                            mode="listener", latency_ms=120)
        self.assertIn("latency=120000", url)
        self.assertEqual(url.count("latency="), 1)
        self.assertIn("streamid=test", url)

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


if __name__ == "__main__":
    unittest.main()
