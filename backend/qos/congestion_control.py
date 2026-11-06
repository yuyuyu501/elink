"""拥塞控制（类 GCC 的 AIMD + 延迟梯度判据）。

判据用两条，缺一不可：

1. **丢包**：>10% 强烈拥塞（大幅降），3%~10% 轻度拥塞（小幅降）；
2. **延迟梯度**：排队时延持续上升，即使没丢包也要降——这是链路开始堆积的
   最早信号，等丢包就晚了。

升码率只在「低丢包 + 延迟平稳」时做，而且逐步上升：
乘性下降 / 加性上升，是实时媒体里最稳的组合。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.logging.logger import get_logger
from backend.qos.bitrate import BitrateAllocator, Allocation
from backend.qos.stats import QualitySample

logger = get_logger(__name__)


@dataclass
class CongestionState:
    """控制器当前状态（便于日志与调试）。"""

    target_kbps: float = 1000.0
    loss_rate: float = 0.0
    rtt_ms: float = 0.0
    delay_trend: float = 0.0
    last_action: str = "init"
    history: list[str] = field(default_factory=list)

    def describe(self) -> str:
        return (
            f"target={self.target_kbps:.0f}k loss={self.loss_rate * 100:.1f}% "
            f"rtt={self.rtt_ms:.0f}ms trend={self.delay_trend:+.2f} action={self.last_action}"
        )


class CongestionController:
    """根据 RTCP/质量样本调整目标码率。"""

    def __init__(
        self,
        initial_kbps: float = 1500.0,
        min_kbps: float = 200.0,
        max_kbps: float = 8000.0,
        increase_step_kbps: float = 40.0,
        high_loss: float = 0.10,
        mild_loss: float = 0.03,
        delay_threshold_ms: float = 30.0,
    ) -> None:
        self.min_kbps = min_kbps
        self.max_kbps = max_kbps
        self.increase_step_kbps = increase_step_kbps
        self.high_loss = high_loss
        self.mild_loss = mild_loss
        self.delay_threshold_ms = delay_threshold_ms
        self.state = CongestionState(target_kbps=initial_kbps)
        self.allocator = BitrateAllocator()
        self._trend_history: list[float] = []

    def on_sample(self, sample: QualitySample) -> Allocation:
        """喂入一次质量样本，返回本次的码率分配。"""
        state = self.state
        state.loss_rate = sample.loss_rate
        state.rtt_ms = sample.rtt_ms
        state.delay_trend = self._update_trend(sample.jitter_ms)

        if sample.loss_rate > self.high_loss:
            state.target_kbps *= 0.7
            state.last_action = "decrease-high-loss"
        elif sample.loss_rate > self.mild_loss:
            state.target_kbps *= 0.9
            state.last_action = "decrease-mild-loss"
        elif state.delay_trend > self.delay_threshold_ms:
            state.target_kbps *= 0.85
            state.last_action = "decrease-delay"
        else:
            state.target_kbps += self.increase_step_kbps
            state.last_action = "increase"

        state.target_kbps = max(self.min_kbps, min(self.max_kbps, state.target_kbps))
        state.history.append(state.last_action)
        del state.history[:-32]

        allocation = self.allocator.allocate(state.target_kbps, audio_enabled=sample.audio_enabled)
        logger.debug("拥塞控制: %s -> %s", state.describe(), allocation.describe())
        return allocation

    def _update_trend(self, jitter_ms: float) -> float:
        """用抖动的一阶差分近似排队延迟梯度。"""
        self._trend_history.append(jitter_ms)
        del self._trend_history[:-8]
        if len(self._trend_history) < 3:
            return 0.0
        return self._trend_history[-1] - self._trend_history[0]

    def reset(self) -> None:
        self.state = CongestionState(target_kbps=(self.min_kbps + self.max_kbps) / 2)
        self._trend_history.clear()
        self.allocator.reset()


class FixedRateController:
    """固定码率（调试/局域网用），保持与 ``CongestionController`` 相同的接口。"""

    def __init__(self, video_kbps: int = 4000, audio_kbps: int = 64) -> None:
        self.video_kbps = video_kbps
        self.audio_kbps = audio_kbps
        self.state = CongestionState(target_kbps=float(video_kbps + audio_kbps))

    def on_sample(self, sample: QualitySample) -> Allocation:
        self.state.loss_rate = sample.loss_rate
        self.state.last_action = "fixed"
        return Allocation(video_kbps=self.video_kbps, audio_kbps=self.audio_kbps, overhead_kbps=0)

    def reset(self) -> None:
        pass
