import math
from collections import deque


class RemainingTime:
    def __init__(self, clock):
        self.clock = clock
        self.phase = None
        self.started = None
        self.samples = deque(maxlen=90)

    def start(self, phase, amount=0, files=0):
        self.phase = phase
        self.started = self.clock()
        self.samples.clear()
        self.samples.append((self.started, amount, files))

    def estimate(self, phase, amount, files, remaining_bytes, remaining_files, unknown_files=0):
        if self.phase != phase or self.started is None:
            return None
        current = self.clock()
        if current - self.started < 2:
            return None
        while len(self.samples) > 1 and self.samples[1][0] <= current - 30:
            self.samples.popleft()
        baseline = self.samples[0]
        elapsed = current - baseline[0]
        transferred = max(0, amount - baseline[1])
        processed = max(0, files - baseline[2])
        if current - self.samples[-1][0] >= 0.5:
            self.samples.append((current, amount, files))
        if elapsed < 2 or not remaining_files and phase == "downloading":
            return None
        if not unknown_files and remaining_bytes > 0 and transferred > 0:
            return math.ceil(remaining_bytes * elapsed / transferred)
        if phase == "downloading" and processed > 0:
            return math.ceil(remaining_files * elapsed / processed)
        return None
