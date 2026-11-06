"""重连策略。

指数退避 + 抖动（jitter）是必须的：客户端同时掉线时，没有抖动会导致
「一起重连、一起再被打回」，形成同步冲击（thundering herd）。

另外两个细节：

- **上限有界**：退避上限不能太长，否则一次短暂网络抖动会让用户等几分钟；
- **成功即重置**：连接稳定一段时间后才清零重试计数，避免「连上就掉」的
  情况被误判为已恢复。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

from backend.logging.logger import get_logger

logger = get_logger(__name__)


@dataclass
class BackoffPolicy:
    """退避参数。"""

    initial_s: float = 1.0
    max_s: float = 30.0
    multiplier: float = 2.0
    jitter: float = 0.3
    #: 连接稳定这么久之后才认为「真的恢复了」
    stable_reset_s: float = 30.0
    #: 0 表示无限重试
    max_attempts: int = 0

    def delay_for(self, attempt: int) -> float:
        """第 ``attempt`` 次重试前的等待秒数（attempt 从 1 开始）。"""
        base = min(self.max_s, self.initial_s * (self.multiplier ** max(0, attempt - 1)))
        if self.jitter <= 0:
            return base
        spread = base * self.jitter
        return max(0.0, base + random.uniform(-spread, spread))


@dataclass
class ReconnectManager:
    """记录重试次数并给出下一次等待时间。"""

    policy: BackoffPolicy = field(default_factory=BackoffPolicy)
    attempts: int = 0
    last_connected_at: float | None = None
    _next_at: float = 0.0

    def reset(self) -> None:
        self.attempts = 0
        self._next_at = 0.0

    def on_connected(self) -> None:
        self.last_connected_at = time.monotonic()
        logger.debug("重连计数保持 %d，等待稳定期后再清零", self.attempts)

    def on_disconnected(self) -> None:
        """断线时调用；如果之前稳定过足够久，直接清零重来。"""
        if (
            self.last_connected_at is not None
            and time.monotonic() - self.last_connected_at >= self.policy.stable_reset_s
        ):
            logger.debug("连接曾稳定 %.0fs，重置重连计数", self.policy.stable_reset_s)
            self.attempts = 0
        self.last_connected_at = None

    def should_retry(self) -> bool:
        if self.policy.max_attempts and self.attempts >= self.policy.max_attempts:
            return False
        return True

    def next_delay(self) -> float:
        """消耗一次重试机会并返回等待时间；不应重试时返回 -1。"""
        if not self.should_retry():
            logger.warning("重连次数已达上限 %d", self.policy.max_attempts)
            return -1.0
        self.attempts += 1
        delay = self.policy.delay_for(self.attempts)
        self._next_at = time.monotonic() + delay
        logger.info("第 %d 次重连将在 %.1fs 后开始", self.attempts, delay)
        return delay

    def sleep(self) -> bool:
        """睡到下一次重连时刻；返回 False 表示放弃。"""
        delay = self.next_delay()
        if delay < 0:
            return False
        # 分片睡眠，便于及时响应退出信号
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
        return True

    def remaining_s(self) -> float:
        return max(0.0, self._next_at - time.monotonic())
