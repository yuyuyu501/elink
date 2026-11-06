"""事件定义。

事件只承载「发生了什么」，不承载回调逻辑；消费方在自己的线程内处理。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Event:
    """事件基类。"""

    timestamp: float = field(default_factory=time.monotonic)
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return type(self).__name__


@dataclass(frozen=True)
class PipelineStarted(Event):
    stage: str = ""


@dataclass(frozen=True)
class PipelineStopped(Event):
    stage: str = ""
    reason: str = ""


@dataclass(frozen=True)
class PipelineError(Event):
    stage: str = ""
    message: str = ""


@dataclass(frozen=True)
class FrameDropped(Event):
    """有界队列满时视频帧被丢弃（音频不走此路径）。"""

    reason: str = "queue_full"


@dataclass(frozen=True)
class StatsReport(Event):
    """周期性统计快照。"""

    stats: dict[str, Any] = field(default_factory=dict)
