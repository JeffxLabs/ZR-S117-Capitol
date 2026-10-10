"""Injected repair seeking against a seeded, scrollable leaderboard."""
import random
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run_pipeline as pipeline


class SimulatedList:
    def __init__(self, seed, rows=908, visible_rows=9, unreadable_chance=0.04):
        self.rng = random.Random(seed)
        self.rows = rows
        self.visible_rows = visible_rows
        self.top = rows - visible_rows
        self.unreadable_chance = unreadable_chance
        self.moves = 0
        self.rewinds = 0

    def read_visible(self):
        if self.rng.random() < self.unreadable_chance:
            return []
        return list(range(self.top + 1, self.top + self.visible_rows + 1))

    def fling(self, direction, count):
        sign = 1 if direction == "down" else -1
        for _ in range(count):
            self.top = max(0, min(self.rows - self.visible_rows,
                                  self.top + sign * self.rng.randint(10, 30)))
            self.moves += 1

    def drag(self, direction, px):
        sign = 1 if direction == "down" else -1
        step = max(0, round(px / 140) + self.rng.randint(-1, 1))
        self.top = max(0, min(self.rows - self.visible_rows, self.top + sign * step))
        self.moves += 1

    def rewind_to_top(self):
        self.top = 0
        self.rewinds += 1
        self.moves += 1

    def seek(self, target):
        return pipeline.seek_rank_with_controls(
            target, self.read_visible, self.fling, self.drag, self.rewind_to_top)


class TestInjectedRepairSeeking(unittest.TestCase):
    def test_bottom_to_33_rewinds_and_then_586_seeks_from_33(self):
        device = SimulatedList(seed=20261010)
        self.assertTrue(device.seek(33))
        self.assertEqual(device.rewinds, 1)
        self.assertTrue(device.seek(586))
        self.assertLess(device.top + 1, 586)
        self.assertGreater(device.top + device.visible_rows, 586)

    def test_repair_list_succeeds_with_fewer_than_400_gestures(self):
        targets = [33, 37, 64, 69, 92, 121, 149, 153, 169, 265, 274, 278,
                   294, 524, 528, 537, 538, 539, 542, 586, 588, 638, 657,
                   662, 680, 713, 722, 789, 806, 861, 874]
        device = SimulatedList(seed=20261010)
        order = pipeline.plan_repair_targets(targets, current_center=904)
        self.assertEqual(order, sorted((r for r in targets if r > 60), reverse=True) + [33, 37])
        self.assertTrue(all(device.seek(target) for target in order))
        self.assertLess(device.moves, 400)

    def test_rank_one_only_succeeds_when_visible_from_top(self):
        device = SimulatedList(seed=3)
        device.top = 0
        self.assertTrue(pipeline.seek_rank_with_controls(
            1, device.read_visible, device.fling, device.drag, device.rewind_to_top))


if __name__ == "__main__":
    unittest.main()
