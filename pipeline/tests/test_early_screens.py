#!/usr/bin/env python3
"""Early phone screenshots and capture-prefix merge regressions."""
import json
import os
import sys
import unittest
from pathlib import Path

PIPELINE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PIPELINE_DIR not in sys.path:
    sys.path.insert(0, PIPELINE_DIR)

from early_screens import remap_to_game_column, parse_early_screens, max_clean_cutoff, merge_early
from merge import name_key

FIXTURES = Path(__file__).parent / "fixtures" / "2026-10-10"


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def player(rank, commander, points, alliance="[TAG] Alliance", server="119"):
    return {"rank": rank, "commander": commander, "points": points,
            "alliance": alliance, "server": server}


class TestEarlyScreens(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.items = [load_json(path) for path in sorted((FIXTURES / "early_ocr").glob("*.json"))]
        cls.early = parse_early_screens(cls.items)
        cls.capture = load_json(FIXTURES / "capture_rankings.json")
        cls.verified = load_json(FIXTURES / "verified_rankings.json")

    def test_phone_screens_cover_114_ranks(self):
        self.assertEqual([row["rank"] for row in self.early], list(range(1, 115)))
        self.assertEqual(self.early[0]["commander"], "Azethoth")
        self.assertEqual(self.early[0]["points"], 12604575)
        self.assertIn("n_clean", self.early[0]["diagnostics"])

    def test_remap_keeps_full_height_and_does_not_mutate_items(self):
        original = self.items[0]
        before = json.dumps(original)
        remapped = remap_to_game_column(original)
        rank = next(item for item in remapped if item["text"] == "Rank")
        original_rank = next(item for item in original if item["text"] == "Rank")
        self.assertLess(rank["x"], 0.2)
        self.assertEqual(rank["y"], original_rank["y"])
        self.assertEqual(json.dumps(original), before)

    def test_other_phone_geometry_and_vertical_offset(self):
        transformed = []
        for items in self.items:
            transformed.append([dict(item, x=0.08 + item["x"] * 0.83,
                                     width=item["width"] * 0.83,
                                     y=0.13 + item["y"] * 0.70,
                                     height=item["height"] * 0.70) for item in items])
        actual = parse_early_screens(transformed)
        self.assertEqual([(r["rank"], r["commander"], r["points"]) for r in actual],
                         [(r["rank"], r["commander"], r["points"]) for r in self.early])

    def test_missing_headers_fail_clearly(self):
        with self.assertRaisesRegex(ValueError, "column headers"):
            remap_to_game_column([])

    def test_auto_cutoff_is_30(self):
        self.assertEqual(max_clean_cutoff(self.early, self.capture, name_key), 30)

    def test_merge_matches_verified_top_30_points(self):
        before = json.dumps(self.capture)
        records, provenance = merge_early(self.early, self.capture, 30,
                                         time_range={"start": "12:50:59", "end": "12:51:50"})
        self.assertEqual(len(records), 908)
        self.assertEqual([r["rank"] for r in records], list(range(1, 909)))
        self.assertEqual([r["points"] for r in records[:30]], [r["points"] for r in self.verified[:30]])
        self.assertTrue(all(a["points"] >= b["points"] for a, b in zip(records, records[1:])))
        self.assertEqual(records[29]["commander"], "#yha54rus")
        self.assertEqual(provenance["cutoff"], 30)
        self.assertEqual(provenance["passed_cutoff_score"], [])
        self.assertEqual(provenance["early_time_range"]["start"], "12:50:59")
        self.assertEqual(json.dumps(self.capture), before)

    def test_cutoff_can_become_clean_after_an_earlier_overtake(self):
        early = [player(1, "A", 100), player(2, "B", 90)]
        capture = [player(1, "B", 110), player(2, "A", 100)]
        self.assertEqual(max_clean_cutoff(early, capture, name_key), 2)

    def test_gap_ends_contiguous_prefix(self):
        early = [player(1, "A", 100), player(3, "B", 90)]
        capture = [player(1, "A", 100), player(2, "B", 90)]
        self.assertEqual(max_clean_cutoff(early, capture, name_key), 1)
        with self.assertRaisesRegex(ValueError, "contiguous"):
            merge_early(early, capture, 2)

    def test_fuzzy_matching_needs_alliance_and_server_agreement(self):
        early = [player(1, "Hyha54rus", 100)]
        for alliance, server in [("[OTHER] Alliance", "119"), ("[TAG] Alliance", "117")]:
            capture = [player(1, "#yha54rus", 110, alliance, server)]
            self.assertEqual(max_clean_cutoff(early, capture, name_key), 0)
        capture = [player(1, "#yha54rus", 110)]
        self.assertEqual(max_clean_cutoff(early, capture, name_key), 1)

    def test_exact_key_prevents_fuzzy_identity_replacement(self):
        early = [player(1, "Hyha54rus", 100)]
        capture = [player(1, "#yha54rus", 110), player(2, "Hyha54rus", 100)]
        self.assertEqual(max_clean_cutoff(early, capture, name_key), 0)
        records, provenance = merge_early(early, capture, 1)
        self.assertEqual(records[0]["commander"], "Hyha54rus")
        self.assertEqual(provenance["passed_cutoff_score"][0]["commander"], "#yha54rus")
        self.assertEqual(provenance["passed_cutoff_score"][0]["rank"], 2)

    def test_ambiguous_fuzzy_candidates_are_not_collapsed(self):
        early = [player(1, "alpha", 100)]
        capture = [player(1, "alphb", 110), player(2, "alphc", 105)]
        self.assertEqual(max_clean_cutoff(early, capture, name_key), 0)


if __name__ == "__main__":
    unittest.main()
