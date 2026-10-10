import copy
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from navigation import Navigator, Screen, read_matchup


FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "2026-10-10", "nav_ocr")


def load_screen(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return json.load(f)


class TestMatchup(unittest.TestCase):
    def setUp(self):
        self.items = load_screen("nav_07_capitol_conquest.json")
        self.expected = {"home_role": "defending", "opponent": "119",
                         "battle_over": True, "home_result": "defeat"}

    def test_conquest_fixture(self):
        self.assertEqual(read_matchup(self.items), self.expected)

    def test_other_screens_are_not_matchups(self):
        for name in ("nav_05_main.json", "nav_08_rankings.json"):
            with self.subTest(name=name):
                self.assertIsNone(read_matchup(load_screen(name)))

    def test_text_order_does_not_choose_sides(self):
        self.assertEqual(read_matchup(list(reversed(self.items))), self.expected)

    def test_home_can_be_on_either_side(self):
        self.assertEqual(read_matchup(self.items, home="119"), {
            "home_role": "attacking", "opponent": "117",
            "battle_over": True, "home_result": "victory",
        })
        mirrored = [dict(item, x=1 - item["x"] - item["width"]) for item in self.items]
        self.assertEqual(read_matchup(mirrored), self.expected)

    def test_unreadable_or_missing_home(self):
        self.assertIsNone(read_matchup(self.items, home="120"))
        self.assertIsNone(read_matchup([i for i in self.items if i["text"] != "S119"]))

    def test_roles_must_be_on_opposite_halves(self):
        items = copy.deepcopy(self.items)
        next(i for i in items if "Invader" in i["text"])["x"] = 0.2
        self.assertIsNone(read_matchup(items))

    def test_results_and_battle_over_are_optional(self):
        items = [i for i in self.items if i["text"] not in ("DEFEAT", "VICTORY", "Battle Over")]
        self.assertEqual(read_matchup(items), dict(self.expected, battle_over=False, home_result=None))

    def test_server_above_role_or_far_below_is_ignored(self):
        home = next(i for i in self.items if i["text"] == "S117")
        items = [dict(home, text="S120", y=0.95), dict(home, text="S121", y=0.30)] + self.items
        self.assertEqual(read_matchup(items), self.expected)

    def test_ambiguous_servers_are_unreadable(self):
        home = next(i for i in self.items if i["text"] == "S117")
        self.assertIsNone(read_matchup(self.items + [dict(home, text="S120")]))

    def test_navigator_records_matchup_before_rankings(self):
        conquest = Screen(self.items, 1080, 1920, "conquest.png")
        rankings = Screen(load_screen("nav_08_rankings.json"), 1080, 1920, "rankings.png")
        main = Screen(load_screen("nav_05_main.json"), 1080, 1920, "main.png")
        with tempfile.TemporaryDirectory() as folder:
            nav = Navigator("unused", "unused", folder, log=lambda _: None, home="119")
            with patch.object(nav, "return_to_city"), patch.object(nav, "tap"), \
                    patch.object(nav, "_read_until", side_effect=[
                        (main, self.items[0]), (main, self.items[0]),
                        (conquest, self.items[-1]), (rankings, True),
                    ]):
                self.assertTrue(nav.go_to_rankings())
            self.assertEqual(nav.matchup, read_matchup(self.items, home="119"))


if __name__ == "__main__":
    unittest.main()
