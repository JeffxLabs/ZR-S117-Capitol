import math
import random
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from human_input import HumanInput, SCREEN_HEIGHT, SCREEN_WIDTH, VERTICAL_MARGIN


class TestHumanInput(unittest.TestCase):
    def setUp(self):
        self.commands = []
        self.sleeps = []

    def runner(self, command, **kwargs):
        self.commands.append((command, kwargs))

    def sleep(self, seconds):
        self.sleeps.append(seconds)

    def test_disabled_input_matches_legacy_commands_and_waits(self):
        controls = HumanInput("adb", "device", enabled=False, runner=self.runner, sleep=self.sleep)
        controls.swipe(540, 1350, 540, 930, 400)
        controls.tap(123, 456)
        controls.back()
        controls.pause(0.15)
        controls.fling_gap()

        self.assertEqual([command for command, _ in self.commands], [
            ["adb", "-s", "device", "shell", "input", "swipe", "540", "1350", "540", "930", "400"],
            ["adb", "-s", "device", "shell", "input", "tap", "123", "456"],
            ["adb", "-s", "device", "shell", "input", "keyevent", "4"],
        ])
        self.assertEqual(self.sleeps, [0.15, 0.25])
        self.assertEqual(controls.pause_count, 2)
        self.assertEqual(controls.total_extra_pause_time, 0)

    def test_enabled_swipe_jitter_clamp_duration_and_capture_distance(self):
        controls = HumanInput("adb", "device", rng=random.Random(20261010), runner=self.runner,
                              sleep=self.sleep)
        swipes = []
        for _ in range(100):
            controls.swipe(540, 1350, 540, 930, 400)
            args = self.commands[-1][0][6:]
            x1, y1, x2, y2, duration = map(int, args)
            swipes.append((x1, y1, x2, y2))
            distance = math.hypot(x2 - x1, y2 - y1)
            self.assertLessEqual(abs(x1 - 540), 25)
            self.assertLessEqual(abs(y1 - 1350), 20)
            self.assertLessEqual(abs(x2 - x1), 11)
            # Endpoint coordinates also include the bounded distance scaling.
            self.assertLessEqual(abs(x2 - 540), 37)
            self.assertLessEqual(abs(y2 - 930), 50)
            self.assertGreaterEqual(distance, 420 * 0.94 - 1)
            self.assertLessEqual(distance, 420 * 1.06 + 1)
            self.assertGreaterEqual(duration, 400 * 0.85 - 1)
            self.assertLessEqual(duration, 400 * 1.15 + 1)
            for x in (x1, x2):
                self.assertGreaterEqual(x, 0)
                self.assertLess(x, SCREEN_WIDTH)
            for y in (y1, y2):
                self.assertGreaterEqual(y, VERTICAL_MARGIN)
                self.assertLessEqual(y, SCREEN_HEIGHT - VERTICAL_MARGIN)
        self.assertGreater(len(set(swipes)), 1)

        controls.swipe(540, 1350, 540, 930, 40)
        self.assertGreaterEqual(int(self.commands[-1][0][-1]), 80)

    def test_tap_offsets_are_gaussian_but_never_exceed_fourteen_pixels(self):
        controls = HumanInput("adb", "device", rng=random.Random(17), runner=self.runner,
                              sleep=self.sleep)
        for _ in range(100):
            controls.tap(540, 960)
            x, y = map(int, self.commands[-1][0][-2:])
            self.assertLessEqual(abs(x - 540), 14)
            self.assertLessEqual(abs(y - 960), 14)
        controls.tap(540, 960, jitter_px=6, max_offset=6)
        x, y = map(int, self.commands[-1][0][-2:])
        self.assertLessEqual(abs(x - 540), 6)
        self.assertLessEqual(abs(y - 960), 6)

    def test_pause_budget_over_300_capture_frames(self):
        controls = HumanInput("adb", "device", rng=random.Random(20261015), runner=self.runner,
                              sleep=self.sleep)
        for _ in range(300):
            controls.pause(0.15)
        # A live frame takes ~1.25 s (OCR dominates), so judge the extra time against the whole run.
        baseline = 300 * 1.25
        added_fraction = controls.total_extra_pause_time / baseline
        self.assertGreater(added_fraction, 0.0)
        self.assertLessEqual(added_fraction, 0.03)
        self.assertEqual(controls.pause_count, 300)

    def test_fling_gaps_are_irregular_and_within_the_requested_range(self):
        controls = HumanInput("adb", "device", rng=random.Random(34), runner=self.runner,
                              sleep=self.sleep)
        for _ in range(100):
            controls.fling_gap()
        self.assertTrue(all(0.15 <= duration <= 0.4 for duration in self.sleeps))
        self.assertGreater(len(set(self.sleeps)), 1)

    def test_seek_distance_jitter_is_limited_to_four_percent(self):
        controls = HumanInput("adb", "device", rng=random.Random(2), runner=self.runner,
                              sleep=self.sleep)
        controls.swipe(540, 1500, 540, 1360, 400, distance_jitter=0.04)
        args = self.commands[-1][0][6:]
        _, y1, _, y2, _ = map(int, args)
        distance = math.hypot(int(args[2]) - int(args[0]), y2 - y1)
        self.assertGreaterEqual(distance, 140 * 0.96 - 1)
        self.assertLessEqual(distance, 140 * 1.04 + 1)


if __name__ == "__main__":
    unittest.main()
