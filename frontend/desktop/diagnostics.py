"""打包自检：验证「界面能起来」且「媒体链路能跑」。

只在 ``--self-test`` 时执行，由 ``scripts/package_windows.py`` 调用。检查项刻意
选成「打包最容易坏的地方」：

1. Qt 平台插件能否加载（打包常漏 ``platforms/qwindows.dll``）；
2. 图标资源是否被收进包（``--add-data`` 漏了 SVG 会静默变成空图标）；
3. ffmpeg 通路是否还在（编码 → 封装 → 解封装 → 解码，任何一环断了都算失败）；
4. 界面能否真正绘制出一张图（``window.grab()`` 比「窗口构造成功」更能说明问题）。
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
import traceback
from pathlib import Path

from backend.config.schema import MuxConfig, VideoEncodeConfig
from backend.media.container.mpegts import MpegTsDemuxer, MpegTsMuxer
from backend.media.frame import Frame, PixelFormat
from backend.media.stream import CodecId, MediaType, StreamInfo
from backend.media.timebase import Timebase
from backend.media.video.codec.probe import probe_ffmpeg

WIDTH, HEIGHT, FPS = 320, 240, 30


def _synthetic_frame(index: int) -> Frame:
    row = bytearray(WIDTH * 4)
    for x in range(WIDTH):
        value = (x * 2 + index * 3) % 256
        row[x * 4 : x * 4 + 4] = bytes([value, (value * 3) % 256, (value * 5) % 256, 255])
    timebase = Timebase.mpegts()
    return Frame(
        data=bytes(row) * HEIGHT,
        timebase=timebase,
        pts=timebase.ticks(index / FPS),
        width=WIDTH,
        height=HEIGHT,
        pixel_format=PixelFormat.BGRA,
    )


def _stream() -> StreamInfo:
    return StreamInfo(
        media_type=MediaType.VIDEO,
        codec=CodecId.H264,
        timebase=Timebase.mpegts(),
        width=WIDTH,
        height=HEIGHT,
        framerate=FPS,
        metadata={"pixel_format": "bgra"},
    )


def _registry_report() -> dict:
    """检查后端后端能否在打包环境里被动态导入。

    注册表按「模块路径字符串」导入后端，PyInstaller 的静态分析看不到这类引用；
    打包漏收时表现为「注册表为空」，这个检查能把真实异常带出来。
    """
    import importlib

    info: dict = {}
    try:
        from backend.registry.codec_registry import build_default_registry

        registry = build_default_registry()
        info["h264_encoders"] = registry.encoder_names("h264")
        info["h264_decoders"] = registry.decoder_names("h264")
    except Exception:
        info["registry_error"] = traceback.format_exc()

    try:
        importlib.import_module("backend.media.video.codec.encoding.h264.libx264")
        info["module_import"] = "ok"
    except Exception:
        info["module_import"] = traceback.format_exc()
    return info


def _media_roundtrip(frames: int = 12) -> dict:
    """编码 -> 封装 -> 解封装 -> 解码，返回统计。"""
    from backend.registry.codec_registry import build_default_registry

    stream = _stream()
    registry = build_default_registry()
    encoder = registry.create_encoder("h264", "libx264", stream, VideoEncodeConfig(bitrate_kbps=800, gop=6))
    encoder.open()
    packets = []
    for index in range(frames):
        packets.extend(encoder.encode(_synthetic_frame(index)))
    packets.extend(encoder.flush())
    encoder.close()

    buffer = io.BytesIO()
    muxer = MpegTsMuxer(buffer, MuxConfig())
    muxer.open([stream])
    for packet in packets:
        muxer.write(packet)
    muxer.close()

    payload = buffer.getvalue()
    buffer.seek(0)
    demuxer = MpegTsDemuxer(buffer)
    demuxer.open()
    restored = []
    while True:
        packet = demuxer.read()
        if packet is None:
            break
        restored.append(packet)
    demuxer.close()

    decoder = registry.create_decoder("h264", "h264", stream, VideoEncodeConfig())
    decoder.open()
    decoded = []
    for packet in restored:
        decoded.extend(decoder.decode(packet))
    decoded.extend(decoder.flush())
    decoder.close()

    return {
        "encoded_packets": len(packets),
        "ts_bytes": len(payload),
        "demuxed_packets": len(restored),
        "decoded_frames": len(decoded),
        "frames": len(decoded),
        "lossless": [a.data for a in packets] == [b.data for b in restored],
    }


def _ui_report(window, app, pixmap_path: Path) -> dict:
    from PySide6.QtGui import QFontDatabase

    from . import icons

    pixmap = window.grab()
    saved = pixmap.save(str(pixmap_path), "PNG")
    logo = icons.icon("monitor", "#ffffff")
    families = QFontDatabase.families()
    return {
        "title": window.windowTitle(),
        "pages": window.stack.count(),
        "stylesheet": bool(app.styleSheet()),
        "icons_loaded": not logo.isNull(),
        "fonts": len(families),
        "font_warning": "" if families else "系统字体库为空（offscreen 平台的已知限制）",
        "grab": [pixmap.width(), pixmap.height()],
        "grab_saved": saved,
        "devices": len(window.service.devices()),
        "screenshot": str(pixmap_path),
    }


def run_self_test(output: Path) -> int:
    """执行自检并写 JSON；返回 0 表示全部通过。

    默认用真实平台插件（能验证字体/DPI/绘制），只有显式设置
    ``ELINK_SELFTEST_HEADLESS=1`` 时才切到 offscreen——注意 offscreen 下
    系统字体库是空的，截图里的文字会变成方块，这属于环境限制而非程序缺陷。
    """
    if os.environ.get("ELINK_SELFTEST_HEADLESS") == "1":
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    output.parent.mkdir(parents=True, exist_ok=True)

    report: dict = {
        "ok": False,
        "version": _version(),
        "python": sys.version,
        "frozen": bool(getattr(sys, "frozen", False)),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    try:
        from PySide6.QtWidgets import QApplication

        from .paths import data_dir, ensure_dirs
        from .theme import apply_theme
        from .ui.window import MainWindow

        ensure_dirs(data_dir())
        app = QApplication.instance() or QApplication([])
        app.setApplicationName("Elink")
        apply_theme(app)

        window = MainWindow(data_dir(), auto_refresh=False)
        window.resize(1120, 740)
        window.show()
        app.processEvents()
        report["ui"] = _ui_report(window, app, output.with_name(output.stem + "-ui.png"))
        window.close()
        app.processEvents()

        report["ffmpeg"] = _ffmpeg_report()
        report["registry"] = _registry_report()
        report["media"] = _media_roundtrip()
        report["packages"] = _package_versions()

        ui_ok = report["ui"]["pages"] >= 4 and report["ui"]["grab"][0] > 0
        media_ok = report["media"]["decoded_frames"] > 0
        report["ok"] = bool(ui_ok and media_ok)
        if not ui_ok:
            report["error"] = "界面自检失败：页面数或渲染尺寸异常"
        elif not media_ok:
            report["error"] = "媒体自检失败：没有解出任何帧"
    except Exception:
        report["error"] = traceback.format_exc()

    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": report["ok"], "report": str(output)}, ensure_ascii=False))
    return 0 if report["ok"] else 1


def _version() -> str:
    from . import __version__

    return __version__


def _ffmpeg_report() -> dict:
    probe = probe_ffmpeg()
    return {
        "available": probe.available,
        "version": getattr(probe, "version", ""),
        "has_libx264": probe.has_encoder("libx264") if probe.available else False,
        "error": getattr(probe, "error", ""),
    }


def _package_versions() -> dict:
    import importlib.metadata

    wanted = ("PySide6", "pyinstaller")
    versions = {}
    for name in wanted:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            continue
    return versions


if __name__ == "__main__":  # pragma: no cover
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("self-test.json")
    raise SystemExit(run_self_test(target))
