import json
import sys
import unittest
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PIPELINE_DIR))

from cleaner import canonicalize_alliances, clean_player_record, clean_records

FIXTURES = Path(__file__).parent / "fixtures" / "2026-10-10"


class TestAllianceCanonicalization(unittest.TestCase):
    def rows(self, alliances):
        return [{"rank": i, "commander": "Player", "alliance": alliance, "points": 1}
                for i, alliance in enumerate(alliances, 1)]

    def test_fullwidth_brackets_and_tag_whitespace(self):
        row = clean_player_record(self.rows(["［ A B C ］ Alliance"])[0])
        self.assertEqual(row["alliance"], "[ABC] Alliance")
        self.assertEqual(row["alliance_tag"], "ABC")

    def test_bracket_repairs_require_known_spelling(self):
        rows = self.rows(["UJQKI JQKKK", "[RTRJ RETRO", "IFA1 Unknown"])
        changes = canonicalize_alliances(rows, {"[JQK] JQKKK", "[RTR] RETRO"})
        self.assertEqual([r["alliance"] for r in rows],
                         ["[JQK] JQKKK", "[RTR] RETRO", "IFA1 Unknown"])
        self.assertEqual(len(changes), 2)
        row = clean_player_record(self.rows(["[RTRJ RETRO"])[0])
        self.assertEqual(row["alliance"], "[RTRJ RETRO")

    def test_current_majority_supplies_known_spelling(self):
        rows = self.rows(["[COM] AmericanHeadquarter", "[COM] AmericanHeadquarter",
                          "[COMI] AmericanHeadquarter", "UJQKI JQKKK", "[JQK] JQKKK"])
        canonicalize_alliances(rows)
        self.assertEqual(rows[2]["alliance"], "[COM] AmericanHeadquarter")
        self.assertEqual(rows[3]["alliance"], "[JQK] JQKKK")

    def test_tag_matching_is_confusable_and_unique(self):
        rows = self.rows(["[C0M] Alliance", "[CAM] Alliance", "[C0M] Different"])
        canonicalize_alliances(rows, {"[COM] Alliance"})
        self.assertEqual([r["alliance"] for r in rows],
                         ["[COM] Alliance", "[CAM] Alliance", "[C0M] Different"])
        ambiguous = self.rows(["[C0M] Alliance"])
        canonicalize_alliances(ambiguous, {"[COM] Alliance", "[CoM] Alliance"})
        self.assertEqual(ambiguous[0]["alliance"], "[C0M] Alliance")

    def test_large_tag_variant_is_reviewed_and_not_rewritten(self):
        rows = self.rows(["[0BS] ZeroBullsht"] * 49 + ["[OBS] ZeroBullsht"])
        reviews = []
        changes = canonicalize_alliances(rows, {"[OBS] ZeroBullsht"}, review_candidates=reviews)
        self.assertEqual(sum(row["alliance"] == "[OBS] ZeroBullsht" for row in rows), 0)
        self.assertEqual(sum(row["alliance"] == "[0BS] ZeroBullsht" for row in rows), 50)
        self.assertEqual(len(changes), 1)
        self.assertEqual(len(reviews), 49)
        self.assertEqual(reviews[0], {"rank": 1, "server": "", "observed": "[0BS] ZeroBullsht",
                                      "known": "[OBS] ZeroBullsht", "alliance": "ZeroBullsht"})

    def test_small_variant_uses_current_majority_or_prior_spelling(self):
        rows = self.rows(["[COM] Alliance"] * 3 + ["[C0M] Alliance"])
        changes = canonicalize_alliances(rows)
        self.assertEqual(rows[-1]["alliance"], "[COM] Alliance")
        self.assertEqual(len(changes), 1)

        rows = self.rows(["[C0M] Alliance"])
        changes = canonicalize_alliances(rows, {"[COM] Alliance"})
        self.assertEqual(rows[0]["alliance"], "[COM] Alliance")
        self.assertEqual(len(changes), 1)

    def test_override_and_capture_alliance_set_match_ground_truth(self):
        capture = json.loads((FIXTURES / "capture_rankings.json").read_text(encoding="utf-8"))
        verified = json.loads((FIXTURES / "verified_rankings.json").read_text(encoding="utf-8"))
        cleaned, changes = clean_records(capture, event_id="2026-10-10-s117-vs-s119")
        self.assertEqual({r["alliance"] for r in cleaned}, {r["alliance"] for r in verified})
        self.assertEqual(cleaned[239]["alliance"], "[IVIN] InvincibiliItaliani")
        self.assertEqual(cleaned[643]["alliance"], "[COM] AmericanHeadquarter")
        self.assertEqual(cleaned[692]["alliance"], "[JQK] JQKKK")
        self.assertTrue(any(c["reason"] == "manual override" and c["field"] == "alliance"
                            for c in changes))


if __name__ == "__main__":
    unittest.main()
