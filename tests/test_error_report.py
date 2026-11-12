"""Error persistence, original diagnostics, and cleanup ordering."""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from backend.errors.codec import DecodeError, EncodeError
from backend.errors.network import TransportClosedError
from backend.logging.logger import get_logger
from backend.media.video.codec.base import FfmpegDecoderBase, FfmpegEncoderBase
from backend.pipeline.receive_pipeline import ReceivePipeline
from backend.pipeline.video_pipeline import VideoPipeline
from backend.transport.srt import SrtTransport
from frontend.desktop import error_report
from frontend.desktop.paths import error_dir
from frontend.desktop.service import SessionService, ViewerSettings, _viewer_config


class ErrorReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, ELINK_DATA_DIR=self.temp.name)
        self.environment.start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.environment.stop)
        error_report._recent.clear()
        error_report._reports.clear()

    def entries(self):
        decoder = json.JSONDecoder()
        result = []
        for path in sorted(error_dir().glob("*.txt")):
            text = path.read_text(encoding="utf-8").strip()
            while text:
                entry, end = decoder.raw_decode(text)
                result.append(entry)
                text = text[end:].strip()
        return result

    def test_structured_project_error_and_original_exception_chain(self):
        try:
            try:
                raise OSError("original pipe failure")
            except OSError as cause:
                raise EncodeError("编码失败", detail={"stderr": "原始错误"}) from cause
        except EncodeError as exc:
            fault = error_report.save_error(exc, context={"role": "host"})
        entry, = self.entries()
        self.assertEqual(error_dir(), Path(self.temp.name) / "error")
        self.assertEqual(entry["fault_id"], fault)
        self.assertEqual(entry["code"], "codec")
        self.assertEqual(entry["message"], "编码失败")
        self.assertEqual(entry["detail"]["stderr"], "原始错误")
        self.assertIn("OSError: original pipe failure", entry["traceback"])
        self.assertIn("raise EncodeError", entry["traceback"])
        self.assertEqual(entry["context"]["role"], "host")
        self.assertTrue(entry["version"])

    def test_same_second_unique_files_and_same_fault_append(self):
        with patch.object(error_report, "datetime") as clock:
            clock.now.return_value = datetime(2026, 11, 12, 14, 30, 25)
            first = error_report.save_error("first")
            second = error_report.save_error(ValueError("second"))
            error_report.save_error("remote detail", fault_id=first, source="remote:host")
        self.assertNotEqual(first, second)
        self.assertEqual(sorted(path.name for path in error_dir().iterdir()),
                         ["20261112_143025.txt", "20261112_143025_01.txt"])
        entries = self.entries()
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[1]["fault_id"], first)
        self.assertEqual(entries[1]["source"], "remote:host")
        self.assertEqual(entries[1]["traceback"], "No local traceback available")
        self.assertEqual(entries[2]["type"], "ValueError")
        self.assertEqual(entries[2]["code"], "unknown")

    def test_concurrent_writes_and_bounded_caches(self):
        for index in range(120):
            error_report.remember(f"{index}:" + "x" * 5000)
        with ThreadPoolExecutor(max_workers=8) as workers:
            list(workers.map(lambda index: error_report.save_error(str(index), fault_id="shared"),
                             range(32)))
        entries = self.entries()
        self.assertEqual(len(list(error_dir().glob("*.txt"))), 1)
        self.assertEqual(len(entries), 32)
        self.assertEqual(len(entries[0]["recent_logs"]), 100)
        self.assertTrue(all(len(line) <= 4000 for line in entries[0]["recent_logs"]))
        self.assertTrue(all(not entry["recent_logs"] for entry in entries[1:]))
        for index in range(260):
            error_report.save_error("bounded", fault_id=f"unique-{index}")
        self.assertEqual(len(error_report._reports), 256)

    def test_write_failure_logs_once_without_raising(self):
        with patch.object(Path, "open", side_effect=OSError("read only")), patch.object(
            error_report.logger, "warning"
        ) as warning:
            self.assertTrue(error_report.save_error("failure"))
            warning.assert_called_once()
        self.assertFalse(error_report._writing.active)

    def test_backend_file_logging_and_uncaught_thread_traceback(self):
        from frontend.desktop.app import _configure_logging

        root = logging.getLogger("elink")
        handlers, old_level = list(root.handlers), root.level
        sys_hook, thread_hook = sys.excepthook, threading.excepthook
        try:
            _configure_logging()
            _configure_logging()
            files = [handler for handler in root.handlers if isinstance(handler, logging.FileHandler)
                     and Path(handler.baseFilename).parent.parent == Path(self.temp.name)]
            self.assertEqual(len(files), 1)
            get_logger("backend-test").error("backend file marker")
            files[0].flush()
            self.assertIn("backend file marker",
                          (Path(self.temp.name) / "logs" / "desktop.log").read_text(encoding="utf-8"))
            try:
                raise RuntimeError("thread original")
            except RuntimeError as exc:
                threading.excepthook(SimpleNamespace(
                    exc_type=type(exc), exc_value=exc, exc_traceback=exc.__traceback__,
                    thread=threading.current_thread()))
            entry, = self.entries()
            self.assertIn("raise RuntimeError", entry["traceback"])
            self.assertIn("backend file marker", "\n".join(entry["recent_logs"]))
        finally:
            sys.excepthook, threading.excepthook = sys_hook, thread_hook
            for handler in list(root.handlers):
                if handler not in handlers:
                    root.removeHandler(handler)
                    handler.close()
            root.handlers[:] = handlers
            root.setLevel(old_level)

    def test_capture_thread_preserves_original_exception(self):
        original = OSError("capture original")
        capture = Mock(config=SimpleNamespace(fps=30))
        capture.read.side_effect = original
        factory, muxer = Mock(), Mock()
        pipeline = VideoPipeline(capture, factory, muxer)
        with self.assertRaises(OSError) as caught:
            pipeline.run()
        self.assertIs(caught.exception, original)
        factory.assert_not_called()
        capture.close.assert_called_once()
        muxer.close.assert_called_once()

    def test_codec_and_srt_diagnostics_survive_cleanup(self):
        for codec_type in (FfmpegEncoderBase, FfmpegDecoderBase):
            codec = codec_type(Mock())
            proc = Mock(args=["ffmpeg.exe", "-codec", "test"])
            proc.poll.return_value = 7
            codec._proc = proc
            codec._stderr_tail.append("codec original")
            codec.close()
            self.assertIsNone(codec._proc)
            self.assertEqual(codec.diagnostics()["exit_code"], 7)
            self.assertIn("codec original", codec.diagnostics()["stderr"])
        transport = SrtTransport("srt://127.0.0.1:9000")
        proc = Mock(args=["ffmpeg.exe", "-i", "srt://127.0.0.1:9000"])
        proc.poll.side_effect = [None, 99]
        transport._proc = proc
        transport._stderr_tail.append("srt original")
        with patch("backend.transport.srt.subprocess.run"):
            transport.abort()
        transport.close()
        self.assertIsNone(transport.diagnostics()["exit_code"])
        self.assertEqual(transport.diagnostics()["stderr"], "srt original")

    def test_decoder_open_error_keeps_diagnostics(self):
        original = DecodeError("decoder original")
        decoder = Mock()
        decoder.open.side_effect = original
        decoder.diagnostics.return_value = {"command": ["ffmpeg.exe"], "exit_code": 8}
        stream = Mock()
        demuxer = Mock(streams=[stream])
        pipeline = ReceivePipeline(demuxer, Mock(return_value=decoder), Mock())
        with patch("backend.pipeline.receive_pipeline.detect_video_dimensions",
                   return_value=(320, 240)), self.assertRaises(DecodeError):
            pipeline._open_decoder([Mock(data=b"frame")])
        self.assertEqual(original.process_diagnostics["exit_code"], 8)
        decoder.close.assert_called_once()

    def test_media_failure_is_saved_before_runtime_cleanup(self):
        service = SessionService()
        original = DecodeError("decoder original", detail={"reason": "bad input"})
        original.process_diagnostics = {"command": ["ffmpeg.exe"], "exit_code": 9,
                                        "stderr": ["decoder stderr"]}
        transport = Mock()
        transport.diagnostics.return_value = {"command": ["srt.exe"], "exit_code": 1,
                                              "stderr": "srt stderr"}
        pipeline = Mock()
        pipeline.run.side_effect = original
        pipeline.process_diagnostics = original.process_diagnostics
        runtime = Mock()
        runtime.get.side_effect = {"pipeline": pipeline, "srt": transport}.get
        runtime.close.side_effect = lambda: self.assertEqual(len(self.entries()), 1)
        service._mode = "viewer"
        service.error_context = {"phase": "receiving", "peer": "100.1.2.3"}
        service._settings_snapshot = {"fps": 30}
        with patch("frontend.desktop.service.prepare_environment", return_value=(None, None)), patch(
            "frontend.desktop.service.build_receive_resources", return_value=runtime
        ), patch.object(service, "_start_control_client"):
            service._run_viewer(_viewer_config(ViewerSettings()), ViewerSettings())
        entry, = self.entries()
        self.assertEqual(entry["process"]["exit_code"], 9)
        self.assertEqual(entry["context"]["srt"]["exit_code"], 1)
        self.assertEqual(entry["context"]["codec"]["stderr"], ["decoder stderr"])
        self.assertIn("decoder original", entry["traceback"])
        self.assertEqual(entry["context"]["peer"], "100.1.2.3")
        self.assertEqual(entry["context"]["role"], "viewer")
        self.assertTrue(service.failure_id)
        transport.abort.assert_called_once()
        runtime.close.assert_called_once()

    def test_unexpected_srt_end_but_not_intentional_stop_or_file_eof(self):
        service = SessionService()
        service._check_media_end()
        service._transport = Mock()
        service._transport.diagnostics.return_value = {"exit_code": 3}
        with self.assertRaises(TransportClosedError) as caught:
            service._check_media_end()
        self.assertEqual(caught.exception.detail["exit_code"], 3)
        service._stop.set()
        service._check_media_end()
        self.assertFalse(self.entries())


if __name__ == "__main__":
    unittest.main()
