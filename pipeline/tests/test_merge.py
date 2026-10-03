#!/usr/bin/env python3
"""
Unit tests for multi-observation merge and voting system.
"""
import os
import sys
import json
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PIPELINE_DIR = os.path.dirname(TESTS_DIR)

if PIPELINE_DIR not in sys.path:
    sys.path.insert(0, PIPELINE_DIR)

from merge import ObservationStore, merge_rank


class TestMerge(unittest.TestCase):

    def test_tainted_wrong_observation_never_beats_clean_one(self):
        """A tainted wrong observation must never beat a clean observation, even if tainted has multiple votes."""
        obs = [
            {
                "rank": 42,
                "commander": "RealCommander",
                "alliance": "[GOOD] Alliance",
                "server": "113",
                "points": 1000000,
                "y": 0.40,
                "complete": True,
                "tainted": False
            },
            {
                "rank": 42,
                "commander": "FakeBannerGuy",
                "alliance": "[BAD] TaintedAlly",
                "server": None,
                "points": 9999999,
                "y": 0.77,
                "complete": True,
                "tainted": True
            },
            {
                "rank": 42,
                "commander": "FakeBannerGuy",
                "alliance": "[BAD] TaintedAlly",
                "server": None,
                "points": 9999999,
                "y": 0.78,
                "complete": True,
                "tainted": True
            }
        ]

        merged = merge_rank(42, obs)
        self.assertEqual(merged["rank"], 42)
        self.assertEqual(merged["commander"], "RealCommander")
        self.assertEqual(merged["alliance"], "[GOOD] Alliance")
        self.assertEqual(merged["server"], "113")
        self.assertEqual(merged["points"], 1000000)
        self.assertFalse(merged["diagnostics"]["used_tainted"])
        self.assertEqual(merged["diagnostics"]["n_clean"], 1)
        self.assertEqual(merged["diagnostics"]["n_obs"], 3)

    def test_majority_voting_wins(self):
        """The majority value among clean complete observations must win."""
        obs = [
            {
                "rank": 10,
                "commander": "AlphaPlayer",
                "alliance": "[TAG1] AllianceA",
                "server": "113",
                "points": 500000,
                "y": 0.35,
                "complete": True,
                "tainted": False
            },
            {
                "rank": 10,
                "commander": "AlphaPlayer",
                "alliance": "[TAG1] AllianceA",
                "server": "113",
                "points": 500000,
                "y": 0.45,
                "complete": True,
                "tainted": False
            },
            {
                "rank": 10,
                "commander": "AlphaPlayerOcrGlitch",
                "alliance": "[TAG1] AllianceB",
                "server": "113",
                "points": 500000,
                "y": 0.55,
                "complete": True,
                "tainted": False
            }
        ]

        merged = merge_rank(10, obs)
        self.assertEqual(merged["commander"], "AlphaPlayer")
        self.assertEqual(merged["alliance"], "[TAG1] AllianceA")
        self.assertEqual(merged["points"], 500000)
        self.assertEqual(merged["diagnostics"]["agreement"]["commander"], round(2 / 3, 3))
        self.assertEqual(merged["diagnostics"]["agreement"]["alliance"], round(2 / 3, 3))
        self.assertEqual(merged["diagnostics"]["agreement"]["points"], 1.0)

    def test_single_observation_flagged(self):
        """A rank with only a single clean observation is recorded with n_clean == 1."""
        obs = [
            {
                "rank": 77,
                "commander": "SoloPlayer",
                "alliance": "[SOLO] Alone",
                "server": None,
                "points": 12345,
                "y": 0.45,
                "complete": True,
                "tainted": False
            }
        ]

        merged = merge_rank(77, obs)
        self.assertEqual(merged["diagnostics"]["n_obs"], 1)
        self.assertEqual(merged["diagnostics"]["n_clean"], 1)
        self.assertFalse(merged["diagnostics"]["used_tainted"])

    def test_tie_breaking_nearest_screen_centre(self):
        """When clean observations are tied in vote count, observation closest to y=0.45 wins."""
        obs = [
            {
                "rank": 15,
                "commander": "NearCenter",
                "alliance": "[N] Near",
                "server": None,
                "points": 1111,
                "y": 0.47,  # distance 0.02
                "complete": True,
                "tainted": False
            },
            {
                "rank": 15,
                "commander": "FarFromCenter",
                "alliance": "[F] Far",
                "server": None,
                "points": 2222,
                "y": 0.15,  # distance 0.30
                "complete": True,
                "tainted": False
            }
        ]

        merged = merge_rank(15, obs)
        self.assertEqual(merged["commander"], "NearCenter")
        self.assertEqual(merged["points"], 1111)

    def test_fallback_to_tainted_when_no_clean_exists(self):
        """When only tainted observations exist, fall back to tainted and record used_tainted=True."""
        obs = [
            {
                "rank": 99,
                "commander": "TaintedOnly",
                "alliance": "[TNT] TaintedAlliance",
                "server": "113",
                "points": 45678,
                "y": 0.77,
                "complete": True,
                "tainted": True
            }
        ]

        merged = merge_rank(99, obs)
        self.assertEqual(merged["commander"], "TaintedOnly")
        self.assertEqual(merged["points"], 45678)
        self.assertTrue(merged["diagnostics"]["used_tainted"])
        self.assertEqual(merged["diagnostics"]["n_clean"], 0)

    def test_raw_observations_serialization(self):
        """ObservationStore raw observations and merged records must be JSON serializable."""
        store = ObservationStore()
        store.add_observation({
            "rank": 1,
            "commander": "Commander1",
            "alliance": "[P1MP] JU1CE",
            "server": None,
            "points": 10000000,
            "y": 0.814,
            "complete": True,
            "tainted": False
        }, frame_name="frame_0001.png")

        merged = store.merge_all()
        # Ensure json.dumps works without error
        serialized_raw = json.dumps(store.get_raw_observations())
        serialized_merged = json.dumps(merged)
        self.assertIn("frame_0001.png", serialized_raw)
        self.assertIn("Commander1", serialized_merged)



class TestServerVote(unittest.TestCase):
    def test_stray_server_line_does_not_outvote_home(self):
        """Two clean reads with no server line (home) beat one stray 'S113' read."""
        base = {"rank": 7, "commander": "A", "alliance": "[X] Y", "points": 100, "complete": True, "tainted": False, "y": 0.4}
        obs = [dict(base, server=None), dict(base, server=None), dict(base, server="113")]
        self.assertIsNone(merge_rank(7, obs)["server"])

if __name__ == "__main__":
    unittest.main()
