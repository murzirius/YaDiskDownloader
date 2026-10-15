import math
from collections import deque


class RemainingTime:
    """Estimate useful work, keeping byte transfer and file attempts separate.

    Calls are serialized by Control.lock. The clock excludes pause/network waits.
    """
    def __init__(self, clock):
        self.clock = clock
        self.phase = None
        self.started = None
        self.reason = 'Уточняется: мало данных'
        self.basis = None
        self.samples = deque(maxlen=128)
        self.new_stage()

    def start(self, phase, amount=0, files=0):
        self.phase = phase
        self.started = self.clock()
        self.samples.clear()
        self.samples.append((self.started, amount))
        self.new_stage()

    def new_stage(self):
        self.stage_started = self.clock()
        self.amounts = {'original': 0, 'preview': 0}
        self.byte_samples = deque([(self.stage_started, 0, 0)], maxlen=128)
        self.marks = {kind: deque(maxlen=64) for kind in ('original', 'preview', 'all')}
        self.finished = self.successes = 0
        self.completion_samples = deque([(self.stage_started, 0, 0)], maxlen=256)
        self.outcomes = deque(maxlen=3)
        self.last_activity = self.last_data = self.retry_at = None
        self.fallback = False
        self.reason = 'Уточняется: мало данных'
        self.basis = None

    def advance(self, kind, amount):
        if self.phase != 'downloading' or amount <= 0:
            return
        current = self.clock()
        self.amounts[kind] += amount
        self.last_data = self.last_activity = current
        for category in (kind, 'all'):
            marks = self.marks[category]
            if not marks or current - marks[-1] >= 0.5:
                marks.append(current)
        if current - self.byte_samples[-1][0] >= 0.5:
            self.byte_samples.append((current, self.amounts['original'], self.amounts['preview']))

    def retry(self, kind):
        if self.phase != 'downloading':
            return
        self.retry_at = self.clock()
        # Bytes from an interrupted attempt are not evidence of useful speed.
        self.byte_samples.clear()
        self.byte_samples.append((self.retry_at, self.amounts['original'], self.amounts['preview']))
        for marks in self.marks.values():
            marks.clear()

    def result(self, success, reused=False, fallback=False):
        if self.phase != 'downloading' or reused:
            return
        current = self.clock()
        self.finished += 1
        self.successes += bool(success)
        self.outcomes.append(bool(success))
        self.last_activity = current
        self.fallback |= fallback
        if current - self.completion_samples[-1][0] >= 0.5:
            self.completion_samples.append((current, self.finished, self.successes))

    @staticmethod
    def trim(samples, cutoff):
        while len(samples) > 1 and samples[1][0] <= cutoff:
            samples.popleft()

    def estimate(self, phase, amount, files, remaining_bytes, remaining_files, unknown_files=0, *, original_only=False):
        self.basis = None
        self.reason = 'Уточняется: мало данных'
        if self.phase != phase or self.started is None:
            return None
        current = self.clock()
        if phase == 'packing':
            self.trim(self.samples, current - 30)
            baseline = self.samples[0]
            if current - self.samples[-1][0] >= 0.5:
                self.samples.append((current, amount))
            elapsed, moved = current - baseline[0], amount - baseline[1]
            # Several polls with no ZIP progress should not inflate the estimate.
            progress = [point for index, point in enumerate(self.samples) if index and point[1] > self.samples[index - 1][1]]
            if progress and current - progress[-1][0] > 10:
                return None
            if elapsed >= 2 and moved > 0 and remaining_bytes > 0:
                self.basis, self.reason = 'packing', ''
                return math.ceil(remaining_bytes * elapsed / moved)
            return None
        if phase != 'downloading' or remaining_files <= 0:
            return None
        recent_retry = self.retry_at is not None and current - self.retry_at < 60
        if recent_retry:
            self.reason = 'Уточняется: повторы'
        if len(self.outcomes) == 3 and not any(self.outcomes):
            self.reason = 'Уточняется: нет удачных загрузок'
            return None
        if self.last_activity is None or current - self.last_activity > 15:
            if current - self.stage_started >= 15:
                self.reason = 'Уточняется: нет передачи'
            return None
        if current - self.stage_started < 3:
            return None
        file_model = bool(unknown_files or self.fallback or recent_retry)
        if not file_model and remaining_bytes > 0:
            self.trim(self.byte_samples, current - 30)
            baseline = self.byte_samples[0]
            moved = self.amounts['original'] - baseline[1]
            if not original_only:
                moved += self.amounts['preview'] - baseline[2]
            marks = self.marks['original' if original_only else 'all']
            recent_marks = [point for point in marks if point >= current - 30]
            elapsed = current - baseline[0]
            if (elapsed >= 3 and moved >= 64 * 1024 and len(recent_marks) >= 3
                    and recent_marks[-1] - recent_marks[0] >= 2 and current - recent_marks[-1] <= 10):
                self.basis, self.reason = 'bytes', ''
                return math.ceil(remaining_bytes * elapsed / moved)
            return None
        self.trim(self.completion_samples, current - 120)
        baseline = self.completion_samples[0]
        finished = self.finished - baseline[1]
        successes = self.successes - baseline[2]
        elapsed = current - baseline[0]
        # This includes full retry/fallback cycles and actual parallel throughput.
        if finished >= 3 and successes > 0 and elapsed >= 3:
            self.basis, self.reason = 'files', ''
            return math.ceil(remaining_files * elapsed / finished)
        return None
