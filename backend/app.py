"""应用生命周期：命令行解析、启动/停止、信号处理、优雅退出。

三种运行模式对应 PLAN_1 的验证路径：
- ``loopback``：采集 -> 编码 -> 写 .ts -> 读 .ts -> 解码 -> 写 PNG（本地闭环）
- ``send``：同上，但字节流写到 SRT
- ``receive``：从 SRT / 文件读取并解码写图

``loopback`` 的目的就是让「抽象是否够用」先于「网络是否通」被验证。
"""

from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path
from typing import cast

from backend.bootstrap import (
    Runtime,
    build_receive_resources,
    build_send_resources,
    prepare_environment,
)
from backend.config.loader import load_config, merge_cli_overrides
from backend.config.schema import AppConfig
from backend.errors.base import ElinkError
from backend.event.bus import EventBus
from backend.logging.logger import get_logger, setup_logging
from backend.pipeline.receive_pipeline import ReceivePipeline
from backend.pipeline.video_pipeline import VideoPipeline
from backend.registry.codec_registry import CodecRegistry

logger = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="elink",
        description="elink 首个可运行链路：采集 -> 编码 -> MPEG-TS -> 解码 -> 写图",
    )
    parser.add_argument("--mode", choices=["loopback", "send", "receive"], help="运行模式")
    parser.add_argument("--config", help="JSON 配置文件路径")
    parser.add_argument("--log-level", help="日志级别（DEBUG/INFO/WARNING/ERROR）")

    parser.add_argument("--duration", type=float, help="采集时长（秒）")
    parser.add_argument("--max-frames", type=int, help="最大采集帧数，0 表示不限")
    parser.add_argument("--fps", type=float, help="采集帧率")

    parser.add_argument("--x", type=int, help="采集区域左上角 X")
    parser.add_argument("--y", type=int, help="采集区域左上角 Y")
    parser.add_argument("--width", type=int, help="采集区域宽度（偶数）")
    parser.add_argument("--height", type=int, help="采集区域高度（偶数）")

    parser.add_argument("--preset", help="libx264 preset")
    parser.add_argument("--bitrate", type=int, help="目标码率 kbps")
    parser.add_argument("--gop", type=int, help="关键帧间隔")
    parser.add_argument("--ffmpeg", help="ffmpeg 可执行文件路径")

    parser.add_argument("--out", help="发送目标文件路径（file 传输）")
    parser.add_argument("--frames-dir", help="解码帧写图目录")
    parser.add_argument("--srt-url", help="SRT 地址，如 srt://127.0.0.1:9000")
    parser.add_argument("--srt-mode", choices=["caller", "listener"], help="SRT 模式")
    parser.add_argument("--srt-latency", type=int, help="SRT 延迟（毫秒）")
    return parser


def config_from_args(args: argparse.Namespace) -> AppConfig:
    config = load_config(args.config)

    region = None
    if args.width and args.height:
        region = (args.x or 0, args.y or 0, args.width, args.height)

    overrides = {
        "mode": args.mode,
        "duration_s": args.duration,
        "max_frames": args.max_frames,
        "log_level": args.log_level,
        "capture.fps": args.fps,
        "capture.region": region,
        "encode.preset": args.preset,
        "encode.bitrate_kbps": args.bitrate,
        "encode.gop": args.gop,
        "encode.ffmpeg_path": args.ffmpeg,
        "transport.path": args.out,
        "transport.srt_url": args.srt_url,
        "transport.srt_mode": args.srt_mode,
        "transport.srt_latency_ms": args.srt_latency,
        "frames_dir": args.frames_dir,
    }
    if args.srt_url:
        # 给了 SRT 地址就说明要走网络，否则容易误落到本地文件
        overrides["transport.kind"] = "srt"
    return merge_cli_overrides(config, overrides)


def _install_signal_handlers(runtime: Runtime) -> None:
    """Ctrl+C 时让管线停止采集并冲刷队列，再由主流程收尾。"""

    def handler(signum, frame):  # pragma: no cover - 依赖真实信号
        logger.info("收到信号 %s，正在停止…", signum)
        pipeline = runtime.get("pipeline")
        stop = getattr(pipeline, "stop", None)
        if stop is not None:
            stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            # 非主线程/不支持的环境忽略
            pass


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.log_level:
        setup_logging(args.log_level)
    else:
        setup_logging()

    try:
        config = config_from_args(args)
        setup_logging(config.log_level)
        probe, registry = prepare_environment(config)
        logger.info("%s", probe.describe())

        bus = EventBus()
        if config.mode == "loopback":
            return _run_loopback(config, registry, bus)
        if config.mode == "send":
            return _run_send(config, registry, bus)
        return _run_receive(config, registry, bus)
    except ElinkError as exc:
        logger.error("%s", exc)
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        logger.info("已中断")
        return 130


def _run_send(config: AppConfig, registry: CodecRegistry, bus: EventBus) -> int:
    runtime = build_send_resources(config, registry, bus)
    try:
        _install_signal_handlers(runtime)
        pipeline = cast(VideoPipeline, runtime.get("pipeline"))
        stats = pipeline.run(duration_s=config.duration_s, max_frames=config.max_frames)
    finally:
        runtime.close()

    if stats.bytes_written <= 0:
        logger.error("没有任何字节写出，链路未跑通")
        return 3
    return 0


def _run_loopback(config: AppConfig, registry: CodecRegistry, bus: EventBus) -> int:
    out_path = Path(config.transport.path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    logger.info("=== 阶段 A：采集 -> 编码 -> MPEG-TS ===")
    config.transport.kind = "file"
    send_result = _run_send(config, registry, bus)
    if send_result != 0:
        return send_result

    size = out_path.stat().st_size if out_path.is_file() else 0
    logger.info("TS 落盘: %s (%d 字节)", out_path.resolve(), size)
    if size <= 0:
        logger.error("TS 文件为空")
        return 4

    logger.info("=== 阶段 A：MPEG-TS -> 解码 -> 写图 ===")
    runtime = build_receive_resources(config, registry, bus, source_path=out_path)
    try:
        pipeline = cast(ReceivePipeline, runtime.get("pipeline"))
        stats = pipeline.run()
    finally:
        runtime.close()

    if stats.frames_decoded <= 0:
        logger.error("没有解出任何帧")
        return 5
    return 0


def _run_receive(config: AppConfig, registry: CodecRegistry, bus: EventBus) -> int:
    runtime = build_receive_resources(config, registry, bus)
    try:
        pipeline = cast(ReceivePipeline, runtime.get("pipeline"))
        stats = pipeline.run()
    finally:
        runtime.close()

    if stats.frames_decoded <= 0:
        logger.error("没有解出任何帧")
        return 5
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run())
