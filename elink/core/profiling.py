"""Bounded, session-local timings. Durations never compare peer clocks."""
from collections import Counter, deque
import json
import math
import threading
import time

from ..logging_setup import environment_details, exception_details


class PipelineProfile:
    def __init__(self, capacity=600, clock=time.perf_counter):
        if type(capacity) is not int or capacity < 1:
            raise ValueError('Sample capacity must be a positive integer')
        self.capacity = capacity
        self.clock = clock
        self.started = clock()
        self.lock = threading.Lock()
        self.samples = {}
        self.counts = Counter()
        self.last = {}
        self.metadata = dict(environment_details())
        self.events = deque(maxlen=64)
        self.failures = deque(maxlen=8)

    def observe(self, stage, milliseconds):
        if not math.isfinite(milliseconds) or milliseconds < 0:
            return
        with self.lock:
            if stage not in self.samples:
                if len(self.samples) >= 64:
                    return
                self.samples[stage] = deque(maxlen=self.capacity)
            self.samples[stage].append(milliseconds)
            self.counts[stage] += 1

    def elapsed(self, stage, started):
        self.observe(stage, (self.clock() - started) * 1000)

    def mark(self, stage):
        now = self.clock()
        with self.lock:
            previous = self.last.get(stage)
            self.last[stage] = now
        if previous is not None:
            self.observe(stage, (now - previous) * 1000)

    def count(self, name, amount=1):
        with self.lock:
            self.counts[name] += amount

    def describe(self, **values):
        with self.lock:
            self.metadata.update(values)

    def event(self, name, **values):
        # Only numerical/control metadata belongs here, never media or input.
        with self.lock:
            self.events.append(dict(event=name, at_ms=round((self.clock() - self.started) * 1000, 3), **values))

    def exception(self, stage, exc, **context):
        failure = dict(stage=stage, at_ms=round((self.clock() - self.started) * 1000, 3),
                       **exception_details(exc), context=context)
        with self.lock:
            self.failures.append(failure)
            # Keep reports below the existing 128 KiB peer response limit.
            while len(self.failures) > 1 and len(json.dumps(list(self.failures))) > 32768:
                self.failures.popleft()

    def snapshot(self):
        with self.lock:
            samples = {key: list(values) for key, values in self.samples.items()}
            counts, metadata = dict(self.counts), dict(self.metadata)
            events = [dict(value) for value in self.events]
            failures = [dict(value, context=dict(value['context'])) for value in self.failures]
        summary = {}
        for key, values in samples.items():
            values.sort()
            def percentile(p):
                return round(values[max(0, math.ceil(p * len(values)) - 1)], 4)
            summary[key] = dict(samples=len(values), total=counts[key], p50_ms=percentile(.5),
                                p95_ms=percentile(.95), p99_ms=percentile(.99), max_ms=round(values[-1], 4))
        return dict(elapsed_seconds=round(self.clock() - self.started, 3),
                    window_samples=self.capacity, stages=summary, counters=counts, metadata=metadata,
                    events=events, failures=failures)
