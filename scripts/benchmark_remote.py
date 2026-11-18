"""Exercise an actual local remote session and record stage timings in out/."""

import argparse
import json
import socket
import time
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication

from frontend.desktop.connection import DesktopConnection
from frontend.desktop.service import HostSettings, SessionService, ViewerSettings


def run(transport="webrtc", seconds=5, width=1920, height=1080, fps=120,
        encoder="libx264", decoder="h264", synthetic=False, updates=()):
    app = QCoreApplication.instance() or QCoreApplication([])
    host_service, viewer_service = SessionService(), SessionService()
    host = DesktopConnection(host_service, "benchmark-host", HostSettings())
    viewer = DesktopConnection(viewer_service, "benchmark-viewer", HostSettings())
    frames, cursors, errors, applied = [], [], [], []
    viewer_service.frame_ready.connect(lambda frame: (
        frames.append((time.perf_counter(), frame.pts, frame.width, frame.height)),
        viewer_service.frame_presented()))
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

        def open(self):
            pass

        def read(self):
            from backend.media.frame import Frame
            self.cursor_applied_mode = self.cursor_mode
            return Frame(data=bytes((32, 64, 192, 255)) * (width * height),
                         width=width, height=height)

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
                                  stream_bitrate_kbps=12000,
                                  protocol_preferences={"benchmark-host": transport},
                                  codec_preferences={"benchmark-host": {
                                      "codec": "h264", "encoder": encoder, "decoder": decoder}})
        viewer.connect_peer({"id": "benchmark-host", "ip": "127.0.0.1",
                             "session_port": host.server.serverPort()}, settings, False)
        deadline = time.monotonic() + 90
        active = None
        while time.monotonic() < deadline and not errors:
            app.processEvents()
            if viewer.phase == "active" and active is None:
                active = time.monotonic()
            if active is not None and time.monotonic() - active >= seconds:
                break
            time.sleep(.001)
        changes = []
        for new_width, new_height, new_fps in updates:
            if host.phase != "active" or viewer.phase != "active":
                break
            before, count = len(frames), len(applied)
            viewer.configure({"width": new_width, "height": new_height,
                              "fps": new_fps, "bitrate_kbps": 12000})
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                app.processEvents()
                if (len(applied) > count and host.phase == viewer.phase == "active"
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
            })
        result = {
            "transport": transport, "resolution": [width, height], "requested_fps": fps,
            "synthetic_capture": synthetic, "frames": len(frames), "errors": errors,
            "phases": [host.phase, viewer.phase],
            "host": host_service.performance_report(), "viewer": viewer_service.performance_report(),
            "cursor": cursors[-1] if cursors else None,
            "last_frame_pts": frames[-1][1] if frames else None,
            "updates": changes,
        }
        if len(frames) > 1:
            result["delivered_fps"] = (len(frames) - 1) / (frames[-1][0] - frames[0][0])
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
    parser.add_argument("--encoder", default="libx264")
    parser.add_argument("--decoder", default="h264")
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--update", dest="updates", nargs=3, type=int, action="append",
                        metavar=("WIDTH", "HEIGHT", "FPS"), default=[])
    parser.add_argument("--output", type=Path, default=Path("out/remote-benchmark.json"))
    args = vars(parser.parse_args())
    output = args.pop("output")
    result = run(**args)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=list), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k not in {"host", "viewer"}}, ensure_ascii=False))
    updates_ok = len(result["updates"]) == len(args["updates"]) and all(
        change["applied"] is not None and
        [change["applied"][k] for k in ("width", "height", "fps")] == change["requested"] and
        change["last_frame_size"] == change["requested"][:2] and
        change["frames"] >= 10 and change["phases"] == ["active", "active"]
        for change in result["updates"])
    return int(bool(result["errors"]) or not result["frames"] or
               not result["closed_cleanly"] or not result["workers_stopped"] or not updates_ok)


if __name__ == "__main__":
    raise SystemExit(main())
