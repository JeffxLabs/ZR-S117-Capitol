#!/usr/bin/env python3
"""
Unit tests for data cleaner and normalization.
"""
import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PIPELINE_DIR = os.path.dirname(TESTS_DIR)

if PIPELINE_DIR not in sys.path:
    sys.path.insert(0, PIPELINE_DIR)

from cleaner import clean_player_record, consolidate_alliance_variants, validate_dataset, ALLIANCE_NORMALIZATION_MAP


class TestCleaner(unittest.TestCase):

    def test_explicit_server_respected(self):
        """Explicit server in record must be preserved and cleaned."""
        rec = {
            "rank": 5,
            "commander": "VisitingPlayer",
            "alliance": "[ALIN] Алина",
            "points": 500000,
            "server": "113"
        }
        cleaned = clean_player_record(rec, home_server="117", visiting_server="119")
        self.assertEqual(cleaned["server"], "113")

        # Test with 'S' prefix in server
        rec_s = {
            "rank": 6,
            "commander": "VisitingPlayer2",
            "alliance": "[ALIN] Алина",
            "points": 400000,
            "server": "S113"
        }
        cleaned_s = clean_player_record(rec_s, home_server="117", visiting_server="119")
        self.assertEqual(cleaned_s["server"], "113")

    def test_mexico113_alliance_with_server_none_stays_home(self):
        """An alliance tag like [113] MEXICO113 with server None must stay home server (no false positive from '113')."""
        rec = {
            "rank": 20,
            "commander": "ElCapitan",
            "alliance": "[113] MEXICO113",
            "points": 800000,
            "server": None
        }
        cleaned = clean_player_record(rec, home_server="117", visiting_server="119")
        self.assertEqual(cleaned["server"], "117")
        self.assertEqual(cleaned["alliance_tag"], "113")
        self.assertEqual(cleaned["alliance_name"], "MEXICO113")

    def test_legacy_trailing_server_suffix_still_works(self):
        """Legacy raw record without server key must fall back to trailing 'S<digits>' on alliance."""
        rec = {
            "rank": 35,
            "commander": "LegacyPlayer",
            "alliance": "[ALIN] Алина S113",
            "points": 250000
            # Note: no 'server' key
        }
        cleaned = clean_player_record(rec, home_server="117", visiting_server="119")
        self.assertEqual(cleaned["server"], "113")
        self.assertEqual(cleaned["alliance"], "[ALIN] Алина")

        # Legacy record without trailing server stays home server
        rec_home = {
            "rank": 36,
            "commander": "HomePlayer",
            "alliance": "[P1MP] JU1CE",
            "points": 240000
        }
        cleaned_home = clean_player_record(rec_home, home_server="117", visiting_server="119")
        self.assertEqual(cleaned_home["server"], "117")

    def test_already_cleaned_records_do_not_lose_server(self):
        """Reprocessing an already-cleaned record must not lose server."""
        cleaned_rec = {
            "rank": 50,
            "commander": "CleanedCommander",
            "alliance": "[Nfc] néespourfairechier",
            "alliance_tag": "Nfc",
            "alliance_name": "néespourfairechier",
            "server": "113",
            "points": 1502024
        }
        re_cleaned = clean_player_record(cleaned_rec, home_server="117", visiting_server="119")
        self.assertEqual(re_cleaned["server"], "113")
        self.assertEqual(re_cleaned["rank"], 50)
        self.assertEqual(re_cleaned["commander"], "CleanedCommander")

    def test_alliance_normalization_map_applied(self):
        """Known OCR look-alike alliance typos in ALLIANCE_NORMALIZATION_MAP are fixed."""
        rec = {
            "rank": 100,
            "commander": "ZeroPlayer",
            "alliance": "[OBS] ZeroBullsht",
            "server": "113",
            "points": 100000
        }
        cleaned = clean_player_record(rec, home_server="117")
        self.assertEqual(cleaned["alliance"], "[0BS] ZeroBullsht")
        self.assertEqual(cleaned["alliance_tag"], "0BS")

    def test_consolidate_alliance_variants(self):
        """Variants differing only by lookalike characters are merged into the most frequent."""
        records = [
            {"rank": 1, "alliance": "[0BS] ZeroBullsht", "points": 100, "server": "113", "commander": "P1"},
            {"rank": 2, "alliance": "[0BS] ZeroBullsht", "points": 90, "server": "113", "commander": "P2"},
            {"rank": 3, "alliance": "[OBS] ZeroBullsht", "points": 80, "server": "113", "commander": "P3"},
        ]
        merged = consolidate_alliance_variants(records)
        self.assertEqual(records[2]["alliance"], "[0BS] ZeroBullsht")

    def test_validate_dataset_integrity(self):
        """validate_dataset detects missing ranks, empty commanders, None points, and non-monotonic points."""
        records = [
            {"rank": 1, "commander": "P1", "alliance": "A", "points": 1000, "server": "117"},
            {"rank": 3, "commander": "P3", "alliance": "A", "points": 1200, "server": "117"},  # missing rank 2, non-monotonic points
            {"rank": 4, "commander": "", "alliance": "A", "points": 800, "server": "117"},    # empty commander
            {"rank": 5, "commander": "P5", "alliance": "A", "points": None, "server": "117"},   # None points
        ]
        errors = validate_dataset(records)
        self.assertTrue(any("Missing 1 ranks" in e for e in errors))
        self.assertTrue(any("Empty commander" in e for e in errors))
        self.assertTrue(any("Missing points" in e for e in errors))
        self.assertTrue(any("Non-monotonic points" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
