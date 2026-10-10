"""Small, injectable wrapper for human-like ADB input and pacing."""
import math
import random
import subprocess
import threading
import time


_DEFAULT_RNG = random.Random()
SCREEN_WIDTH = 1080
SCREEN_HEIGHT = 1920
VERTICAL_MARGIN = 60


class HumanInput:
    def __init__(self, adb, device, rng=None, enabled=True, runner=subprocess.run, sleep=time.sleep):
        self.adb = adb
        self.device = device
        self.rng = _DEFAULT_RNG if rng is None else rng
        self.enabled = enabled
        self.runner = runner
        self.sleep = sleep
        self.pause_count = 0
        self.total_extra_pause_time = 0.0
        self._stats_lock = threading.Lock()

    @property
    def extra_pause_time(self):
        """Alias for the accumulated difference from the requested pause times."""
        return self.total_extra_pause_time

    def _command(self, *args, check=False):
        command = [self.adb, "-s", self.device, "shell", "input", *args]
        if check:
            return self.runner(command, check=True)
        return self.runner(command)

    @staticmethod
    def _clamp(value, low, high):
        return max(low, min(high, value))

    @staticmethod
    def _screen_point(x, y):
        return (
            int(HumanInput._clamp(round(x), 0, SCREEN_WIDTH - 1)),
            int(HumanInput._clamp(round(y), VERTICAL_MARGIN, SCREEN_HEIGHT - VERTICAL_MARGIN)),
        )

    def swipe(self, x1, y1, x2, y2, duration_ms, *, distance_jitter=0.06, check=False):
        if self.enabled:
            original_dx = x2 - x1
            original_dy = y2 - y1
            original_distance = math.hypot(original_dx, original_dy)

            common_x = self.rng.randint(-25, 25)
            start_x = x1 + common_x
            start_y = y1 + self.rng.randint(-20, 20)
            end_x = x2 + common_x + self.rng.randint(-10, 10)
            end_y = y2 + self.rng.randint(-20, 20)

            # Preserve the small coordinate jitters' angle while constraining the
            # actual gesture distance, which matters to the overlapping capture window.
            dx = end_x - start_x
            dy = end_y - start_y
            jittered_distance = math.hypot(dx, dy)
            if original_distance and jittered_distance:
                scale = (original_distance * self.rng.uniform(1 - distance_jitter, 1 + distance_jitter)
                         / jittered_distance)
                end_x = start_x + dx * scale
                end_y = start_y + dy * scale

            x1, y1 = self._screen_point(start_x, start_y)
            x2, y2 = self._screen_point(end_x, end_y)
            duration_ms = max(80, int(round(duration_ms * self.rng.uniform(0.85, 1.15))))

        return self._command("swipe", str(x1), str(y1), str(x2), str(y2), str(duration_ms), check=check)

    def tap(self, x, y, *, jitter_px=8, max_offset=14, check=False):
        if self.enabled:
            dx = self._clamp(round(self.rng.gauss(0, jitter_px)), -max_offset, max_offset)
            dy = self._clamp(round(self.rng.gauss(0, jitter_px)), -max_offset, max_offset)
            x = self._clamp(x + dx, 0, SCREEN_WIDTH - 1)
            y = self._clamp(y + dy, 0, SCREEN_HEIGHT - 1)
        return self._command("tap", str(x), str(y), check=check)

    def back(self, *, check=False):
        return self._command("keyevent", "4", check=check)

    def _sleep(self, base_s, actual_s):
        self.sleep(actual_s)
        with self._stats_lock:
            self.pause_count += 1
            self.total_extra_pause_time += actual_s - base_s

    def pause(self, base_s):
        """Sleep near the requested time, sometimes adding a short reading pause."""
        if not self.enabled:
            actual_s = base_s
        else:
            actual_s = base_s * self.rng.lognormvariate(0, 0.22)
            if self.rng.randrange(40) == 0:
                actual_s += self.rng.uniform(0.6, 1.8)
        self._sleep(base_s, actual_s)

    def fling_gap(self):
        """Wait after a fling using the legacy 250 ms delay when disabled."""
        base_s = 0.25
        actual_s = self.rng.uniform(0.15, 0.4) if self.enabled else base_s
        self._sleep(base_s, actual_s)
