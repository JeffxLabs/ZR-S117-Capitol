#!/usr/bin/env python3
"""
Unit tests for frame parser: geometry slot clustering, medal row inference,
and notification banner detection & tainting.
"""
import os
import sys
import glob
import json
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PIPELINE_DIR = os.path.dirname(TESTS_DIR)
BASE_DIR = os.path.dirname(PIPELINE_DIR)
FIXTURES_DIR = os.path.join(TESTS_DIR, "fixtures", "ocr")
GROUND_TRUTH_PATH = os.path.join(BASE_DIR, "events", "2026-10-03-s117-vs-s113", "rankings.json")

if PIPELINE_DIR not in sys.path:
    sys.path.insert(0, PIPELINE_DIR)

from cleaner import clean_player_record, ALLIANCE_NORMALIZATION_MAP
from frame_parser import parse_frame


class TestFrameParser(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with open(GROUND_TRUTH_PATH, "r", encoding="utf-8") as f:
            cls.ground_truth_list = json.load(f)
        cls.ground_truth = {r["rank"]: r for r in cls.ground_truth_list}

    def test_all_fixtures_non_tainted_complete_rows_match_truth(self):
        """For every fixture: parsed non-tainted complete rows must match ground truth on
        rank, points, and server, and truth alliance tag must appear in parsed alliance."""
        fixture_files = sorted(glob.glob(os.path.join(FIXTURES_DIR, "*.json")))
        self.assertGreater(len(fixture_files), 0, "No fixture files found")

        total_checked = 0
        mismatches = []

        for fpath in fixture_files:
            fname = os.path.basename(fpath)
            with open(fpath, "r", encoding="utf-8") as f:
                items = json.load(f)

            res = parse_frame(items)

            for row in res.rows:
                if not (row.complete and not row.tainted):
                    continue

                total_checked += 1
                rank = row.rank
                self.assertIn(rank, self.ground_truth, f"{fname}: rank {rank} not in ground truth")
                gt_row = self.ground_truth[rank]

                # Points check
                if row.points != gt_row["points"]:
                    mismatches.append(f"{fname} rank {rank} points: got {row.points}, expected {gt_row['points']}")

                # Server check via cleaner
                cleaned = clean_player_record(row, home_server="117", visiting_server="113")
                if cleaned["server"] != gt_row["server"]:
                    mismatches.append(f"{fname} rank {rank} server: got {cleaned['server']!r}, expected {gt_row['server']!r}")

                # Alliance tag check
                tag = gt_row.get("alliance_tag", "").strip()
                if tag:
                    norm_parsed = ALLIANCE_NORMALIZATION_MAP.get(cleaned["alliance"], cleaned["alliance"])
                    if tag.upper() not in norm_parsed.upper() and tag.upper() not in row.alliance.upper():
                        mismatches.append(f"{fname} rank {rank} alliance tag: tag {tag!r} not in {cleaned['alliance']!r} (raw: {row.alliance!r})")

        self.assertEqual(len(mismatches), 0, f"Mismatches found:\n" + "\n".join(mismatches))
        self.assertGreaterEqual(total_checked, 140, f"Expected >= 140 clean complete rows checked, got {total_checked}")

    def test_fixture_1003_s_yields_ranks_1_to_8(self):
        """1003_s.json must yield ranks 1..8 (medal rows inferred) with correct points."""
        fpath = os.path.join(FIXTURES_DIR, "1003_s.json")
        with open(fpath, "r", encoding="utf-8") as f:
            items = json.load(f)

        res = parse_frame(items)
        clean_rows = [r for r in res.rows if r.complete and not r.tainted]
        ranks = [r.rank for r in clean_rows]

        self.assertEqual(ranks, list(range(1, 9)), f"Expected ranks 1..8, got {ranks}")
        for r in clean_rows:
            gt_row = self.ground_truth[r.rank]
            self.assertEqual(r.points, gt_row["points"], f"Rank {r.rank} points mismatch: {r.points} != {gt_row['points']}")
            self.assertFalse(r.tainted, f"Rank {r.rank} should not be tainted")

    def test_banner_fixtures_detection_and_tainting(self):
        """Banner fixtures (b328, a574, c574, b401) must report banner_detected and taint
        ranks 329, 450, 569, 400 respectively; all other rows in those frames must still match truth."""
        cases = {
            "1003_b328.json": 329,
            "1003_a574.json": 450,
            "1003_c574.json": 569,
            "1003_b401.json": 400,
        }

        for fname, expected_tainted_rank in cases.items():
            with self.subTest(fixture=fname):
                fpath = os.path.join(FIXTURES_DIR, fname)
                with open(fpath, "r", encoding="utf-8") as f:
                    items = json.load(f)

                res = parse_frame(items)
                self.assertTrue(res.banner_detected, f"{fname}: banner_detected was False")
                self.assertIsNotNone(res.banner_band, f"{fname}: banner_band was None")

                tainted_ranks = [r.rank for r in res.rows if r.tainted]
                self.assertEqual(tainted_ranks, [expected_tainted_rank],
                                 f"{fname}: expected tainted ranks {[expected_tainted_rank]}, got {tainted_ranks}")

                # Non-tainted complete rows must match ground truth
                for r in res.rows:
                    if r.complete and not r.tainted:
                        gt_row = self.ground_truth[r.rank]
                        self.assertEqual(r.points, gt_row["points"],
                                         f"{fname} rank {r.rank} points mismatch")
                        cleaned = clean_player_record(r, home_server="117", visiting_server="113")
                        self.assertEqual(cleaned["server"], gt_row["server"],
                                         f"{fname} rank {r.rank} server mismatch")

    def test_fixture_1003_r51_rank_51_parsed(self):
        """1003_r51.json rank 51 must parse as ARKAN8 / [GDI] GolgDiggers / 1441716 / server None(home)."""
        fpath = os.path.join(FIXTURES_DIR, "1003_r51.json")
        with open(fpath, "r", encoding="utf-8") as f:
            items = json.load(f)

        res = parse_frame(items)
        r51_rows = [r for r in res.rows if r.rank == 51]
        self.assertEqual(len(r51_rows), 1, "Rank 51 row not found in 1003_r51.json")

        row = r51_rows[0]
        self.assertEqual(row.commander, "ARKAN8")
        self.assertIn("GolgDiggers", row.alliance)
        self.assertEqual(row.points, 1441716)
        self.assertIsNone(row.server)
        self.assertFalse(row.tainted)
        self.assertTrue(row.complete)

        cleaned = clean_player_record(row, home_server="117", visiting_server="113")
        self.assertEqual(cleaned["server"], "117")
        self.assertEqual(cleaned["alliance"], "[GDI] GolgDiggers")



class TestEdgeRows(unittest.TestCase):
    def test_row_under_header_with_cut_name_is_incomplete(self):
        """Rank 45 on 2026-10-03: row centre at y=0.838, name line hidden by the header, so the
        alliance line was read as the commander. Such rows must be marked incomplete."""
        items = [
            {"text": "[ALM] AlrightArmy", "x": 0.35, "y": 0.836, "width": 0.28, "height": 0.022, "confidence": 1},
            {"text": "1,562,684", "x": 0.77, "y": 0.838, "width": 0.14, "height": 0.026, "confidence": 1},
            {"text": "45", "x": 0.06, "y": 0.840, "width": 0.06, "height": 0.026, "confidence": 1},
            {"text": "S113", "x": 0.35, "y": 0.814, "width": 0.08, "height": 0.017, "confidence": 1},
            {"text": "MrFirst1", "x": 0.36, "y": 0.768, "width": 0.15, "height": 0.022, "confidence": 1},
            {"text": "[GDI] GolgDiggers", "x": 0.35, "y": 0.742, "width": 0.28, "height": 0.022, "confidence": 1},
            {"text": "1,562,420", "x": 0.77, "y": 0.745, "width": 0.14, "height": 0.026, "confidence": 1},
            {"text": "46", "x": 0.06, "y": 0.747, "width": 0.06, "height": 0.026, "confidence": 1},
        ]
        rows = {r.rank: r for r in parse_frame(items).rows}
        self.assertFalse(rows[45].complete)
        self.assertTrue(rows[46].complete)


class TestUnreadableName(unittest.TestCase):
    def test_interior_row_with_tag_first_is_unreadable_name(self):
        items = [
            {"text": "[0BS] ZeroBullsht", "x": 0.35, "y": 0.55, "width": 0.27, "height": 0.022, "confidence": 1},
            {"text": "S113", "x": 0.35, "y": 0.527, "width": 0.08, "height": 0.017, "confidence": 1},
            {"text": "243,952", "x": 0.77, "y": 0.547, "width": 0.14, "height": 0.026, "confidence": 1},
            {"text": "355", "x": 0.05, "y": 0.549, "width": 0.1, "height": 0.026, "confidence": 1},
        ]
        row = parse_frame(items).rows[0]
        self.assertTrue(row.complete)
        self.assertEqual(row.commander, "")
        self.assertEqual(row.alliance, "[0BS] ZeroBullsht")
        self.assertEqual(row.server, "113")
        self.assertTrue(row["name_unreadable"])

if __name__ == "__main__":
    unittest.main()
