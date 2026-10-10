import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PIPELINE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PIPELINE_DIR))

from cleaner import clean_player_record, clean_records
from early_screens import merge_early, parse_early_screens
from known_names import (KnownNameIndex, apply_known_names, build_known_name_index,
                         confusable_key, edit_distance)
from merge import name_key

FIXTURES = Path(__file__).parent / "fixtures" / "2026-10-10"


class TestKnownNames(unittest.TestCase):
    def index(self, entries):
        index = KnownNameIndex()
        for server, commander, alliance in entries:
            key = (server, name_key(commander))
            index[key] = commander
            index.alliances[key].add(alliance)
        return index

    def row(self, name, server="117", alliance="[ABC] Alliance"):
        return {"rank": 1, "commander": name, "server": server, "alliance": alliance}

    def test_confusable_normalization_and_distance(self):
        self.assertEqual(confusable_key("O0 I1l| АВСЕНКМОРТХ"),
                         confusable_key("oo llll abcehkmoptx"))
        self.assertEqual(edit_distance("azethoth", "azethot", limit=1), 1)
        self.assertGreater(edit_distance("abc", "xyz", limit=1), 1)
        self.assertEqual(edit_distance("", "ab"), 2)

    def test_unique_fuzzy_match_is_reported_without_renaming(self):
        index = self.index([("117", "Alpha", "[ABC] Alliance")])
        rows = [self.row("Alphx")]
        reviews = []
        changes = apply_known_names(rows, index, review_candidates=reviews)
        self.assertEqual(rows[0]["commander"], "Alphx")
        self.assertEqual(changes, [])
        self.assertEqual(reviews, [{"rank": 1, "server": "117", "observed": "Alphx",
                                    "known": "Alpha", "alliance": "[ABC] Alliance"}])
        for row in [self.row("Alphx", server="119"), self.row("Alphx", alliance="[DEF] Other")]:
            reviews = []
            self.assertEqual(apply_known_names([row], index, review_candidates=reviews), [])
            self.assertEqual(reviews, [])
            self.assertEqual(row["commander"], "Alphx")

    def test_ambiguous_match_and_exact_known_name_stay_unchanged(self):
        index = self.index([("117", "Alpha", "[ABC] Alliance"),
                            ("117", "Alphy", "[ABC] Alliance")])
        row = self.row("Alphx")
        reviews = []
        self.assertEqual(apply_known_names([row], index, review_candidates=reviews), [])
        self.assertEqual(row["commander"], "Alphx")
        self.assertEqual({candidate["known"] for candidate in reviews}, {"Alpha", "Alphy"})

        exact = self.row("Alpha")
        reviews = []
        self.assertEqual(apply_known_names([exact], index, review_candidates=reviews), [])
        self.assertEqual(exact["commander"], "Alpha")
        self.assertEqual(reviews, [])

    def test_known_name_already_in_current_records_is_not_a_candidate(self):
        index = self.index([("117", "Alpha", "[ABC] Alliance")])
        rows = [self.row("Alphx"), self.row("Alpha", alliance="[XYZ] Other")]
        reviews = []
        self.assertEqual(apply_known_names(rows, index, review_candidates=reviews), [])
        self.assertEqual(reviews, [])
        self.assertEqual([row["commander"] for row in rows], ["Alphx", "Alpha"])

    def test_override_precedes_known_and_identity_protects_verified_name(self):
        index = self.index([("117", "Alpha", "[ABC] Alliance")])
        row = self.row("Alpha")
        changes = apply_known_names([row], index, {"117|Alpha": "VerifiedAlpha"})
        self.assertEqual(row["commander"], "VerifiedAlpha")
        self.assertEqual(changes[0]["reason"], "manual override")
        protected = self.row("Alphx")
        self.assertEqual(apply_known_names([protected], index, {"117|Alphx": "Alphx"}), [])
        self.assertEqual(protected["commander"], "Alphx")

    def test_index_excludes_event_being_written(self):
        with tempfile.TemporaryDirectory() as folder:
            for event, rows in [("prior", [self.row("Alpha")]), ("current", [self.row("Alphx")])]:
                path = Path(folder) / event
                path.mkdir()
                (path / "rankings.json").write_text(json.dumps(rows), encoding="utf-8")
            index = build_known_name_index(folder, "current")
            self.assertEqual(index[("117", name_key("Alpha"))], "Alpha")
            self.assertNotIn(("117", name_key("Alphx")), index)

    def test_capture_names_match_verified_rows_after_cutoff(self):
        capture = json.loads((FIXTURES / "capture_rankings.json").read_text(encoding="utf-8"))
        verified = json.loads((FIXTURES / "verified_rankings.json").read_text(encoding="utf-8"))
        cleaned, changes = clean_records(capture, event_id="2026-10-10-s117-vs-s119")
        self.assertEqual([r["commander"] for r in cleaned if r["rank"] >= 31],
                         [r["commander"] for r in verified if r["rank"] >= 31])
        self.assertTrue(any(c["field"] == "commander" for c in changes))
        self.assertEqual(capture[58]["commander"], "•Fox-")

    def test_clean_and_early_merge_reproduce_verified_rows(self):
        capture = json.loads((FIXTURES / "capture_rankings.json").read_text(encoding="utf-8"))
        verified = json.loads((FIXTURES / "verified_rankings.json").read_text(encoding="utf-8"))
        early_ocr = [json.loads(path.read_text(encoding="utf-8"))
                     for path in sorted((FIXTURES / "early_ocr").glob("*.json"))]
        capture = [clean_player_record(row) for row in capture]
        early = [clean_player_record(row) for row in parse_early_screens(early_ocr)]
        merged, _ = merge_early(early, capture, 30)
        cleaned, _ = clean_records(merged, event_id="2026-10-10-s117-vs-s119")
        fields = ("rank", "commander", "alliance", "server", "points")
        self.assertEqual([{field: row[field] for field in fields} for row in cleaned],
                         [{field: row[field] for field in fields} for row in verified])

    def test_published_events_have_no_automatic_name_or_alliance_changes_without_name_overrides(self):
        repository = Path(__file__).resolve().parents[2]
        alliance_overrides = json.loads((repository / "data" / "alliance_overrides.json").read_text())

        def no_name_overrides(path):
            return {} if Path(path).name == "name_overrides.json" else alliance_overrides

        events = ("2026-09-19-s117-vs-s119", "2026-10-03-s117-vs-s113",
                  "2026-10-10-s117-vs-s119")
        with patch("cleaner.load_overrides", side_effect=no_name_overrides):
            for event_id in events:
                with self.subTest(event=event_id):
                    rows = json.loads((repository / "events" / event_id / "rankings.json").read_text())
                    cleaned, changes = clean_records(rows, event_id=event_id, base_dir=str(repository))
                    self.assertEqual([row["commander"] for row in cleaned],
                                     [row["commander"] for row in rows])
                    self.assertEqual([row["alliance"] for row in cleaned],
                                     [row["alliance"] for row in rows])
                    self.assertEqual([change for change in changes
                                      if change["field"] in ("commander", "alliance")], [])


if __name__ == "__main__":
    unittest.main()
