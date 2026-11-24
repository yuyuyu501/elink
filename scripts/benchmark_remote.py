"""Exercise an actual local remote session and record stage timings in out/."""

import argparse
import json
import socket
import time
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from PySide6.QtGui import QColor, QFont, QImage, QPainter, QSurfaceFormat
from PySide6.QtWidgets import QApplication

from frontend.desktop.connection import DesktopConnection
from frontend.desktop.service import HostSettings, SessionService, ViewerSettings
from frontend.desktop.ui.preview import PreviewWidget
from frontend.desktop.ui.video_surface import video_surface_format


def direct_quality(seconds, width, height, fps, bitrate, codec, encoder):
    import numpy as np
    from statistics import mean
    from backend.config.schema import VideoEncodeConfig
    from backend.media.frame import Frame
    from backend.media.stream import CodecId, StreamInfo
    from backend.media.video.codec.inprocess import InprocessDecoder
    from backend.media.video.codec.probe import _verify_on_demand_frames, probe_video_codecs
    from backend.registry.codec_registry import build_default_registry

    app = QApplication.instance() or QApplication([])
    if encoder == "auto":
        backends = probe_video_codecs(on_demand_keyframes=True)["encoders"]
        encoder = next(n for n, item in backends.items()
                       if item["codec"] == codec and item["state"] == "available")
    stream = StreamInfo(codec=CodecId(codec), width=width, height=height, framerate=fps)
    source = build_default_registry().create_encoder(codec, encoder, stream, VideoEncodeConfig(
        codec=codec, encoder=encoder, bitrate_kbps=bitrate * 1000, realtime=True,
        on_demand_keyframes=True))
    decoder = InprocessDecoder(stream)
    decoder.native_output = True
    patterns = [desktop_pattern(width, height, phase) for phase in range(24)]
    reference = np.frombuffer(patterns[0], dtype=np.uint8).reshape(height, width, 4)[16:160, 16:240, 1]
    reference = reference.astype(np.int16)
    packets, samples = [], []

    def sample(image):
        plane = image.planes[0]
        roi = np.frombuffer(plane, dtype=np.uint8).reshape(-1, plane.line_size)[16:160, 16:240]
        roi = roi.astype(np.int16)
        samples.append({"edge": round(float(np.abs(np.diff(roi, axis=1)).mean()), 4),
                        "mae": round(float(np.abs(roi - reference).mean()), 4)})

    def decode(encoded):
        for item in encoded:
            packets.append(item)
            for frame in decoder.decode(item):
                sample(frame.to_av())

    source.open()
    count = round(seconds * fps)
    recovery = round((seconds / 2 + 0.5) * fps) * 90000 // fps
    started = time.perf_counter()
    try:
        decoder.open()
        for i in range(count):
            pts = i * 90000 // fps
            phase = i // 4 % 24 if i >= count // 2 else 0
            source.force_keyframe = pts == recovery
            decode(source.encode(Frame(data=patterns[phase], width=width, height=height, pts=pts)))
        decode(source.flush())
        for frame in decoder.flush():
            sample(frame.to_av())
    finally:
        decoder.close()
        source.close()
    processing_fps = count / (time.perf_counter() - started)
    _verify_on_demand_frames(stream, packets, recovery)
    keyframes = [packet.pts for packet in packets if packet.keyframe]
    groups = {}
    for name, group in (("still", samples[:count // 2]), ("moving", samples[count // 2:])):
        edges = [frame["edge"] for frame in group]
        groups[name] = {"mean_edge": mean(edges), "edge_range": [min(edges), max(edges)],
                        "mean_mae": mean(frame["mae"] for frame in group),
                        "edge_jumps_over_5pct": sum(abs(a - b) > mean(edges) * .05
                                                   for a, b in zip(edges, edges[1:]))}
    return {"codec": codec, "encoder": encoder, "requested_fps": fps,
            "resolution": [width, height], "target_mbps": bitrate,
            "processing_fps": processing_fps,
            "encoded_mbps": sum(packet.size for packet in packets) * 8 / seconds / 1e6,
            "recovery_request_pts": recovery, "keyframe_pts": keyframes,
            "verified": len(samples) == count, "quality": groups}


def frame_counts(host, viewer):
    source, sink = host._pipeline, viewer._pipeline
    sent, received = source.stats, sink.stats
    rtc = host._rtc_runtime
    track = next((track for track in rtc.tracks if track.kind == "video"), None) if rtc else None
    return {
        "captured": track.frames if track else sent.frames_captured,
        "encoded": sent.frames_encoded,
        "decoded": received.frames_decoded,
        "encoded_bytes": sent.bytes_written,
    }


def run(seconds=5, width=1920, height=1080, fps=120,
        encoder="auto", decoder="auto", synthetic=False, updates=(),
        bitrate=12, warmup=3, display="gpu", codec="h264", quality=False):
    if QApplication.instance() is None:
        QSurfaceFormat.setDefaultFormat(video_surface_format())
    app = QApplication.instance() or QApplication([])
    if not isinstance(app, QApplication):
        raise RuntimeError("The rendering benchmark requires QApplication")
    if display == "gpu" and app.platformName() in {"offscreen", "minimal"}:
        raise RuntimeError("GPU benchmark requires an interactive desktop")
    host_service, viewer_service = SessionService(), SessionService()
    host = DesktopConnection(host_service, "benchmark-host", HostSettings())
    viewer = DesktopConnection(viewer_service, "benchmark-viewer", HostSettings())
    frames, cursors, errors, applied = [], [], [], []
    quality_samples, encoder_events = [], []
    active = start = initial = None
    recovery_at = None
    drawn = []
    preview = PreviewWidget()
    preview.setWindowTitle("Elink benchmark")
    preview.resize(800, 490)
    preview.set_edge_padding(0)
    preview.set_display_mode(display)
    preview.show()
    viewer_service.frame_ready.connect(lambda frame: frames.append(
        (time.perf_counter(), frame.pts, frame.width, frame.height)))
    viewer_service.frame_ready.connect(preview.set_frame)
    preview.presented.connect(lambda: drawn.append(time.perf_counter()))
    preview.presented.connect(viewer_service.frame_presented)
    viewer.picture_applied.connect(applied.append)
    viewer_service.cursor_changed.connect(cursors.append)
    host.error.connect(errors.append)
    viewer.error.connect(errors.append)
    capture_width = max([width] + [value[0] for value in updates])
    capture_height = max([height] + [value[1] for value in updates])
    region = patch("frontend.desktop.service._display_region",
                   return_value=(0, 0, capture_width, capture_height))

    class Capture:
        def __init__(self, config):
            self.config = config
            self.cursor_mode = self.cursor_applied_mode = "smart"
            self.cursor_visible = True
            self.index = 0
            self.patterns = {}
            self.backend = "synthetic"

        def open(self):
            pass

        def read(self):
            from backend.media.frame import Frame
            self.cursor_applied_mode = self.cursor_mode
            w, h = self.config.width or width, self.config.height or height
            moving = start is not None and time.perf_counter() - start >= seconds / 2
            phase = (self.index // 4 % 24 if moving else 0) if quality else self.index % 4
            self.index += 1
            key = (w, h, phase)
            if key not in self.patterns:
                if quality:
                    self.patterns[key] = desktop_pattern(w, h, phase)
                else:
                    offset = (phase * w) // 4
                    row = bytes((32, 64, 192, 255)) * offset + bytes((160, 96, 32, 255)) * (w - offset)
                    self.patterns[key] = row * h
            return Frame(data=self.patterns[key], width=w, height=h)

        def reconfigure(self, width, height, fps):
            self.config = replace(self.config, width=width, height=height, fps=fps)
            self.patterns.clear()

        def close(self):
            pass

    from backend.media.video.codec.inprocess import InprocessEncoder

    encode = InprocessEncoder.encode
    last_rate = None

    def trace_encode(instance, frame):
        nonlocal last_rate
        requested = instance.force_keyframe
        stamp = time.perf_counter()
        rate = min(instance.target_bitrate, instance.config.bitrate_kbps * 1000)
        packets = encode(instance, frame)
        keyframe = any(packet.keyframe for packet in packets)
        if start is not None and (rate != last_rate or requested or keyframe):
            encoder_events.append({
                "time": stamp, "pts": frame.pts, "target_bps": rate,
                "requested_keyframe": requested,
                "keyframe": keyframe,
                "bytes": sum(len(packet.data) for packet in packets),
            })
        last_rate = rate
        return packets

    def sample_quality(frame):
        from backend.media.frame import PixelFormat
        import numpy as np

        if start is None:
            return
        rows = np.frombuffer(frame.data, dtype=np.uint8, count=frame.linesize * 160)
        rows = rows.reshape(160, frame.linesize)[16:, :]
        if frame.pixel_format in {PixelFormat.BGRA, PixelFormat.RGBA}:
            roi = rows[:, 64:960].reshape(144, -1, 4)[:, :, 1]
        elif frame.pixel_format in {PixelFormat.NV12, PixelFormat.YUV420P}:
            roi = rows[:, 16:240]
        else:
            return
        score = np.abs(np.diff(roi.astype(np.int16), axis=1)).mean()
        quality_samples.append((time.perf_counter(), frame.pts, round(float(score), 3)))

    patches = ExitStack()
    result = None
    try:
        if synthetic or quality:
            patches.enter_context(region)
            patches.enter_context(patch("backend.bootstrap.build_capture",
                                        side_effect=lambda cfg: Capture(cfg.capture)))
        if quality:
            patches.enter_context(patch.object(InprocessEncoder, "encode", trace_encode))
            viewer_service.frame_ready.connect(sample_quality)
        # Reserve a session/control/media/motion group outside ephemeral ports.
        for port in range(24100, 24500, 4):
            try:
                probes = []
                for offset in range(4):
                    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM if offset < 2 else socket.SOCK_DGRAM)
                    probes.append(probe)
                    probe.bind(("127.0.0.1", port + offset))
            except OSError:
                continue
            finally:
                for probe in probes:
                    probe.close()
            if host.start(port):
                break
        else:
            raise RuntimeError("没有可用的测试端口")
        settings = ViewerSettings(stream_width=width, stream_height=height, stream_fps=fps,
                                  stream_bitrate_kbps=bitrate * 1000,
                                  codec_preferences={"benchmark-host": {
                                      "codec": codec, "encoder": encoder, "decoder": decoder}})
        viewer.connect_peer({"id": "benchmark-host", "ip": "127.0.0.1",
                             "session_port": host.server.serverPort()}, settings, False)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and not errors:
            app.processEvents()
            now = time.perf_counter()
            if viewer.phase == host.phase == "active":
                if active is None:
                    active = now
                if start is None and now - active >= warmup:
                    start, initial = now, frame_counts(host_service, viewer_service)
                if (quality and start is not None and recovery_at is None
                        and now - start >= seconds * .6):
                    recovery_at = now
                    viewer_service._rtc_runtime._receiver_cleanup.request_keyframe()
                if start is not None and now - start >= seconds:
                    break
            time.sleep(.001)
        measurement = {}
        if start is not None and now - start >= seconds and viewer.phase == host.phase == "active":
            counts = frame_counts(host_service, viewer_service)
            images = [stamp for stamp in drawn if start < stamp <= now]
            intervals = sorted((b - a) * 1000 for a, b in zip(images, images[1:]))
            duration = now - start
            measurement = {f"{key}_fps": round((counts[key] - initial[key]) / duration, 2)
                           for key in ("captured", "encoded", "decoded")}
            measurement.update(
                encoded_mbps=round((counts["encoded_bytes"] - initial["encoded_bytes"]) * 8
                                   / duration / 1e6, 2),
                delivered_fps=round(sum(start < item[0] <= now for item in frames) / duration, 2),
                drawn_fps=round(len(images) / duration, 2),
                draw_interval_p95_ms=round(intervals[int(.95 * (len(intervals) - 1))], 3)
                if intervals else None,
                capture_backend=getattr(host_service._pipeline.capture, "backend", None),
                encoder=host_service._host_live.encoder, decoder=viewer_service._viewer_live.decoder,
                display_mode=preview.actual_display_mode,
                display_refresh_hz=round(preview.screen().refreshRate(), 2),
            )
        changes = []
        for new_width, new_height, new_fps in updates:
            if host.phase != "active" or viewer.phase != "active":
                break
            before, count = len(frames), len(applied)
            error_count = len(errors)
            old_transport = host_service._rtc_runtime.pc
            old_control = host_service._control
            old_audio = (tuple(host_service._rtc_runtime.tracks) if host_service._rtc_runtime
                         else host_service._session_audio)
            viewer.configure({"width": new_width, "height": new_height,
                              "fps": new_fps, "bitrate_kbps": bitrate * 1000})
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                app.processEvents()
                if ((len(applied) > count or len(errors) > error_count) and host.phase == viewer.phase == "active"
                        and len(frames) >= before + 10):
                    break
                if viewer.phase in {"idle", "stopping", "selection"}:
                    break
                time.sleep(.001)
            changes.append({
                "requested": [new_width, new_height, new_fps],
                "applied": dict(applied[-1]) if len(applied) > count else None,
                "last_frame_size": list(frames[-1][2:]) if frames else None,
                "frames": len(frames) - before,
                "phases": [host.phase, viewer.phase],
                "transport_preserved": old_transport is (
                    host_service._rtc_runtime.pc if host_service._rtc_runtime else None),
                "control_preserved": old_control is host_service._control,
                "audio_preserved": (old_audio == tuple(host_service._rtc_runtime.tracks)
                                    if host_service._rtc_runtime else old_audio is host_service._session_audio),
                "pts_monotonic": all(b[1] > a[1] for a, b in zip(frames[max(0, before - 1):],
                                                                frames[max(0, before - 1) + 1:])),
            })
        result = {
            "transport": "webrtc", "codec": codec, "resolution": [width, height], "requested_fps": fps,
            "synthetic_capture": synthetic or quality, "frames": len(frames), "errors": errors,
            "phases": [host.phase, viewer.phase],
            "host": host_service.performance_report(), "viewer": viewer_service.performance_report(),
            "cursor": cursors[-1] if cursors else None,
            "last_frame_pts": frames[-1][1] if frames else None,
            "updates": changes,
            "bitrate_mbps": bitrate, "requested_display": display, "measurement": measurement,
        }
        if measurement:
            result["delivered_fps"] = measurement["delivered_fps"]
            result["meets_target"] = (measurement["drawn_fps"] >= .95 * fps
                                      and (display == "auto" or measurement["display_mode"] == display))
        if quality:
            recovery_delay = next(((event["time"] - recovery_at) * 1000 for event in encoder_events
                                   if recovery_at is not None and event["time"] >= recovery_at
                                   and event["requested_keyframe"] and event["keyframe"]), None)
            result["quality"] = {
                "recovery_request_at": recovery_at,
                "recovery_keyframe_ms": recovery_delay,
                "measurement_start": start,
                "encoder_events": encoder_events,
                "edge_samples": quality_samples,
            }
        return result
    finally:
        viewer.disconnect()
        deadline = time.monotonic() + 15
        while (host.busy or viewer.busy) and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.005)
        host.close()
        viewer.close()
        if result is not None:
            result["closed_cleanly"] = not host.busy and not viewer.busy
            result["workers_stopped"] = not host_service.running and not viewer_service.running
        host.deleteLater()
        viewer.deleteLater()
        preview.close()
        preview.deleteLater()
        app.processEvents()
        patches.close()


def desktop_pattern(width, height, phase):
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor("#e8edf2"))
    painter = QPainter(image)
    painter.setFont(QFont("Segoe UI", 9))
    for y in range(12, height, 22):
        for x in range(12, width, 250):
            painter.setPen(QColor("#22374b" if y % 4 else "#354961"))
            painter.drawText(x, y, f"Desktop settings   {x:04d} / {y:04d}")
            painter.fillRect(x, y + 3, 205, 1, QColor("#8593a0"))
    x = min(300 + phase * 32, width - 300)
    painter.fillRect(x, height // 5, width // 3, height // 2, QColor("#44718a"))
    for y in range(height // 5 + 14, height * 7 // 10, 20):
        painter.setPen(QColor("#f4f7fa"))
        painter.drawText(x + 8, y, "Remote desktop window  /  Connection")
    painter.end()
    return bytes(image.constBits())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=5)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=int, default=120)
    parser.add_argument("--codec", choices=("h264", "hevc", "av1"), default="h264")
    parser.add_argument("--encoder", default="auto")
    parser.add_argument("--decoder", default="auto")
    parser.add_argument("--bitrate", type=int, choices=range(1, 51), metavar="Mbps", default=12)
    parser.add_argument("--warmup", type=float, default=3)
    parser.add_argument("--display", choices=("gpu", "cpu", "auto"), default="gpu")
    parser.add_argument("--matrix", action="store_true")
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--quality", action="store_true")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--direct", action="store_true", help="Measure quality without WebRTC")
    parser.add_argument("--update", dest="updates", nargs=3, type=int, action="append",
                        metavar=("WIDTH", "HEIGHT", "FPS"), default=[])
    parser.add_argument("--output", type=Path, default=Path("out/remote-benchmark.json"))
    args = vars(parser.parse_args())
    output = args.pop("output")
    matrix = args.pop("matrix")
    repeat = args.pop("repeat")
    if args.pop("direct"):
        if (args["seconds"] < 5 or args["width"] < 640 or args["height"] < 480
                or args["fps"] < 1 or repeat < 1 or matrix or args["updates"]):
            parser.error("direct benchmark requires at least 5 seconds and fixed dimensions of at least 640x480")
        keys = ("seconds", "width", "height", "fps", "bitrate", "codec", "encoder")
        results = [direct_quality(**{key: args[key] for key in keys}) for _ in range(repeat)]
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(results, indent=2), encoding="utf-8")
        return int(any(not result["verified"] for result in results))
    if args["seconds"] <= 0 or args["warmup"] < 0 or (matrix and args["updates"]):
        parser.error("invalid measurement window or matrix combined with live updates")
    if (repeat < 1 or (matrix and repeat > 1)
            or (args["quality"] and (args["width"] < 640 or args["height"] < 480 or args["updates"]))):
        parser.error("quality benchmark requires stable dimensions of at least 640x480")
    if matrix:
        results = [run(**(args | dict(width=w, height=h, fps=fps)))
                   for w, h in ((1280, 720), (1920, 1080), (2560, 1440), (3840, 2160))
                   for fps in (30, 60, 90, 120)]
    else:
        results = [run(**args) for _ in range(repeat)]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(results if matrix or repeat > 1 else results[0], ensure_ascii=False,
                                 indent=2, default=list), encoding="utf-8")
    for result in results:
        print(json.dumps({k: v for k, v in result.items() if k not in {"host", "viewer", "quality"}}, ensure_ascii=False))
    return int(any(not passed(result, args["updates"]) or (
        args["quality"] and (
            result["quality"]["recovery_keyframe_ms"] is None
            or any(event["keyframe"] and not event["requested_keyframe"]
                   for event in result["quality"]["encoder_events"])
        )
    ) for result in results))


def passed(result, updates):
    updates_ok = len(result["updates"]) == len(updates) and all(
        change["applied"] is not None and
        [change["applied"][k] for k in ("width", "height", "fps")] == change["requested"] and
        change["last_frame_size"] == change["requested"][:2] and
        change["frames"] >= 10 and change["phases"] == ["active", "active"]
        and change["transport_preserved"] and change["control_preserved"]
        and change["audio_preserved"] and change["pts_monotonic"]
        for change in result["updates"])
    return (not result["errors"] and result.get("meets_target", False)
            and result["closed_cleanly"] and result["workers_stopped"] and updates_ok)


if __name__ == "__main__":
    raise SystemExit(main())
