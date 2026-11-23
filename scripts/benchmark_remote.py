"""Exercise an actual local remote session and record stage timings in out/."""

import argparse
import json
import socket
import time
from pathlib import Path
from unittest.mock import patch

from PySide6.QtGui import QSurfaceFormat
from PySide6.QtWidgets import QApplication

from frontend.desktop.connection import DesktopConnection
from frontend.desktop.service import HostSettings, SessionService, ViewerSettings
from frontend.desktop.ui.preview import PreviewWidget
from frontend.desktop.ui.video_surface import video_surface_format


def frame_counts(host, viewer):
    source, sink = host._pipeline, viewer._pipeline
    sent, received = source.stats, sink.stats
    rtc = host._rtc_runtime
    track = next((track for track in rtc.tracks if track.kind == "video"), None) if rtc else None
    return {
        "captured": track.frames if track else sent.frames_captured,
        "encoded": sent.frames_encoded,
        "decoded": received.frames_decoded,
    }


def run(transport="webrtc", seconds=5, width=1920, height=1080, fps=120,
        encoder="auto", decoder="auto", synthetic=False, updates=(),
        bitrate=12, warmup=3, display="gpu", codec="h264"):
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
            phase = self.index % 4
            self.index += 1
            key = (w, h, phase)
            if key not in self.patterns:
                offset = (phase * w) // 4
                row = bytes((32, 64, 192, 255)) * offset + bytes((160, 96, 32, 255)) * (w - offset)
                self.patterns[key] = row * h
            return Frame(data=self.patterns[key], width=w, height=h)

        def reconfigure(self, width, height, fps):
            from dataclasses import replace
            self.config = replace(self.config, width=width, height=height, fps=fps)
            self.patterns.clear()

        def close(self):
            pass

    capture_patch = patch("backend.bootstrap.build_capture", side_effect=lambda cfg: Capture(cfg.capture))
    result = None
    if synthetic:
        region.start()
        capture_patch.start()
    try:
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
                                  protocol_preferences={"benchmark-host": transport},
                                  codec_preferences={"benchmark-host": {
                                      "codec": codec, "encoder": encoder, "decoder": decoder}})
        viewer.connect_peer({"id": "benchmark-host", "ip": "127.0.0.1",
                             "session_port": host.server.serverPort()}, settings, False)
        deadline = time.monotonic() + 90
        active = start = initial = None
        while time.monotonic() < deadline and not errors:
            app.processEvents()
            now = time.perf_counter()
            if viewer.phase == host.phase == "active":
                if active is None:
                    active = now
                if start is None and now - active >= warmup:
                    start, initial = now, frame_counts(host_service, viewer_service)
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
                           for key in counts}
            measurement.update(
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
            old_transport = host_service._rtc_runtime.pc if transport == "webrtc" else host_service._transport
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
                    host_service._rtc_runtime.pc if transport == "webrtc" and host_service._rtc_runtime
                    else host_service._transport),
                "control_preserved": old_control is host_service._control,
                "audio_preserved": (old_audio == tuple(host_service._rtc_runtime.tracks)
                                    if host_service._rtc_runtime else old_audio is host_service._session_audio),
                "pts_monotonic": all(b[1] > a[1] for a, b in zip(frames[max(0, before - 1):],
                                                                frames[max(0, before - 1) + 1:])),
            })
        result = {
            "transport": transport, "codec": codec, "resolution": [width, height], "requested_fps": fps,
            "synthetic_capture": synthetic, "frames": len(frames), "errors": errors,
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
        if synthetic:
            capture_patch.stop()
            region.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", choices=("webrtc", "srt"), default="webrtc")
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
    parser.add_argument("--update", dest="updates", nargs=3, type=int, action="append",
                        metavar=("WIDTH", "HEIGHT", "FPS"), default=[])
    parser.add_argument("--output", type=Path, default=Path("out/remote-benchmark.json"))
    args = vars(parser.parse_args())
    output = args.pop("output")
    matrix = args.pop("matrix")
    if args["seconds"] <= 0 or args["warmup"] < 0 or (matrix and args["updates"]):
        parser.error("invalid measurement window or matrix combined with live updates")
    if matrix:
        results = [run(**(args | dict(width=w, height=h, fps=fps)))
                   for w, h in ((1280, 720), (1920, 1080), (2560, 1440), (3840, 2160))
                   for fps in (30, 60, 90, 120)]
    else:
        results = [run(**args)]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(results if matrix else results[0], ensure_ascii=False,
                                 indent=2, default=list), encoding="utf-8")
    for result in results:
        print(json.dumps({k: v for k, v in result.items() if k not in {"host", "viewer"}}, ensure_ascii=False))
    return int(any(not passed(result, args["updates"]) for result in results))


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
