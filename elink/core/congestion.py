"""Feedback-driven media budget; REMB throughput is not link capacity."""
import time


class NetworkBudget:
    def __init__(self, target_bps, profile=None, clock=time.perf_counter):
        self.target_bps = self.media_bps = target_bps
        self.profile, self.clock = profile, clock
        self.base_rtt = None
        self.bad_since = self.good_since = None
        self.last_change = clock()
        self.congested = False

    def feedback(self, loss, rtt=None):
        now = self.clock()
        if rtt is not None and rtt > 0:
            self.base_rtt = min(self.base_rtt or rtt, rtt)
        inflated = rtt is not None and self.base_rtt is not None and rtt > self.base_rtt + .04
        bad = loss >= .03 or inflated
        self.congested = bad
        if self.profile:
            self.profile.describe(feedback_loss_fraction=round(max(0, loss), 5))
            if rtt is not None and rtt >= 0:
                self.profile.observe('feedback_rtt', rtt * 1000)
        if bad:
            self.good_since = None
            if self.bad_since is None:
                self.bad_since = now
            # One loss report or a large frame is not sustained congestion.
            if now - self.bad_since >= 1.5 and now - self.last_change >= 2:
                self._set(max(300_000, int(self.media_bps * .8)), now, 'congestion')
        else:
            self.bad_since = None
            if self.good_since is None:
                self.good_since = now
            if now - self.good_since >= 3 and now - self.last_change >= 2:
                self._set(min(self.target_bps, self.media_bps + max(300_000, self.target_bps // 10)), now, 'recovery')

    def _set(self, value, now, reason):
        if value != self.media_bps:
            self.media_bps, self.last_change = value, now
            if self.profile:
                self.profile.describe(media_budget_bps=value)
                self.profile.event('media_budget', bps=value, reason=reason)
