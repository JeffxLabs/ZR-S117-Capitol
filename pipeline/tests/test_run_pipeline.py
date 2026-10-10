"""Offline orchestration, stopping, and publication safety checks."""
import contextlib
import io
import json
import os
import re
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run_pipeline as pipeline
from merge import ObservationStore
from processor import write_event_outputs
from cleaner import clean_records
from screenshots import archive_screenshots, select_screenshots_to_archive


class TestBottomDetection(unittest.TestCase):
    def test_four_frames_without_growth_allow_rank_jitter(self):
        stalled = identical = 0
        previous = ()
        for i, ranks in enumerate(((901, 908), (902, 907), (903, 908), (904, 906)), 1):
            stop, stalled, identical = pipeline.bottom_reached(908, 908, stalled, ranks, previous, identical)
            self.assertEqual(stop, i == 4)
            previous = ranks

    def test_capture_fixture_stops_at_frame_241(self):
        fixture = Path(__file__).parent / "fixtures/2026-10-10/raw_observations.json"
        frames = json.loads(fixture.read_text())
        highest = stalled = identical = 0
        last = ()
        stopped = None
        for frame in frames:
            if not re.fullmatch(r"frame_\d{4}\.png", frame["frame"]):
                continue
            current = max([highest] + [r["rank"] for r in frame["rows"]])
            ranks = tuple(sorted(r["rank"] for r in frame["rows"] if r["complete"]))
            stop, stalled, identical = pipeline.bottom_reached(highest, current, stalled, ranks, last, identical)
            highest, last = current, ranks
            if stop:
                stopped = frame["frame"]
                break
        self.assertEqual(stopped, "frame_0241.png")
        self.assertEqual(highest, 908)

    def test_growth_resets_stall_and_small_list_is_guarded(self):
        self.assertEqual(pipeline.bottom_reached(908, 909, 3, (909,), (), 0), (False, 0, 0))
        self.assertFalse(pipeline.bottom_reached(50, 50, 8, (50,), (50,), 8)[0])

    def test_identical_frame_alternative_and_empty_frames(self):
        self.assertTrue(pipeline.bottom_reached(908, 909, 0, (900,), (900,), 4)[0])
        self.assertEqual(pipeline.bottom_reached(908, 908, 3, (), (), 5), (True, 4, 0))


class TestMatchupFlags(unittest.TestCase):
    detected = {"opponent": "119", "home_role": "defending"}

    def test_optional_flags_and_partial_override(self):
        self.assertEqual(pipeline.resolve_matchup(None, None, self.detected), ("119", "defending"))
        self.assertEqual(pipeline.resolve_matchup("119", None, self.detected), ("119", "defending"))
        self.assertEqual(pipeline.resolve_matchup("113", None, self.detected, True), ("113", "defending"))

    def test_missing_and_conflicting_flags(self):
        with self.assertRaisesRegex(ValueError, "Could not read matchup"):
            pipeline.resolve_matchup(None, "attacking", None)
        with self.assertRaisesRegex(ValueError, "force-matchup"):
            pipeline.resolve_matchup("113", "defending", self.detected)
        self.assertEqual(pipeline.resolve_matchup("113", "attacking", None), ("113", "attacking"))

    def test_from_file_requires_explicit_matchup_before_any_staging(self):
        with patch.object(pipeline, "staging_dir") as stage, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                pipeline.main(["--from-file", "unused.json"])
        self.assertEqual(result.exception.code, 2)
        stage.assert_not_called()


class TestHumanInputSummary(unittest.TestCase):
    def test_live_run_failure_still_prints_human_input_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def stage(event_id):
                folder = root / "staging" / event_id
                folder.mkdir(parents=True, exist_ok=True)
                return str(folder)

            output = io.StringIO()
            with patch.object(pipeline, "staging_dir", side_effect=stage), \
                    patch.object(pipeline, "find_adb", return_value="mock-adb"), \
                    patch.object(pipeline, "check_and_compile_ocr"), \
                    patch.object(pipeline, "auto_detect_device", return_value="mock-device"), \
                    patch.object(pipeline, "Navigator"), \
                    patch.object(pipeline, "OCRWorker"), \
                    patch.object(pipeline, "scroll_to_top_verified", side_effect=RuntimeError("rewind failed")), \
                    contextlib.redirect_stdout(output):
                with self.assertRaisesRegex(RuntimeError, "rewind failed"):
                    pipeline.main(["--date", "2026-10-10", "--opponent", "119", "--home-role", "defending",
                                   "--no-navigate", "--no-humanize", "--no-apparatchik"])

            self.assertIn("Humanized input off; total extra pause time: 0.0s.", output.getvalue())


class TestPublicationGate(unittest.TestCase):
    def run_mock_capture(self, root, rows, extra=()):
        def stage(event_id):
            folder = root / "staging" / event_id
            folder.mkdir(parents=True, exist_ok=True)
            return str(folder)
        nav = Mock(matchup={"opponent": "119", "home_role": "defending"})
        store = ObservationStore()
        for row in rows:
            store.add_observation(dict(row, y=0.4, complete=True, tainted=False))
        published = Mock()
        def publish(*args):
            result = write_event_outputs(str(root / "events" / args[0]), *args)
            published(*args)
            return result
        patches = [patch.object(pipeline, "BASE_DIR", str(root)),
                   patch.object(pipeline, "staging_dir", side_effect=stage),
                   patch.object(pipeline, "find_adb", return_value="mock-adb"),
                   patch.object(pipeline, "check_and_compile_ocr"),
                   patch.object(pipeline, "auto_detect_device", return_value="mock-device"),
                   patch.object(pipeline, "Navigator", return_value=nav),
                   patch.object(pipeline, "OCRWorker"),
                   patch.object(pipeline, "scroll_to_top_verified"),
                   patch.object(pipeline, "capture_leaderboard", return_value=(rows, {"frames": 1}, store, set())),
                   patch.object(pipeline, "archive_screenshots", return_value=[]),
                   patch.object(pipeline, "publish_event", side_effect=publish)]
        with contextlib.ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            try:
                pipeline.main(["--date", "2026-10-10", "--no-apparatchik", *extra])
            except SystemExit as e:
                return e.code, published, nav
        return 0, published, nav

    def test_failed_qa_stages_outputs_without_publishing(self):
        rows = [{"rank": 2, "commander": "Example", "alliance": "", "points": None,
                 "server": "117", "diagnostics": {"n_clean": 0}}]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code, published, nav = self.run_mock_capture(root, rows)
            self.assertEqual(code, 1)
            published.assert_not_called()
            self.assertFalse((root / "events").exists())
            self.assertFalse((root / "data").exists())
            self.assertFalse((root / "index.html").exists())
            files = list((root / "staging").rglob("capture_qa.json"))
            self.assertEqual(len(files), 1)
            self.assertFalse(json.loads(files[0].read_text())["passed"])
            self.assertTrue((files[0].parent / "rankings.json").exists())
            self.assertTrue((files[0].parent / "event_data.js").exists())
            self.assertTrue((files[0].parent / "capture_rankings.json").exists())
            nav.return_to_city.assert_called_once()

    def test_allow_incomplete_publishes_and_saves_capture(self):
        rows = [{"rank": 2, "commander": "Example", "alliance": "", "points": 100,
                 "server": "117", "diagnostics": {"n_clean": 0}}]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code, published, _ = self.run_mock_capture(root, rows, ["--allow-incomplete"])
            self.assertEqual(code, 0)
            published.assert_called_once()
            own = json.loads((root / "events/2026-10-10-s117-vs-s119/capture_rankings.json").read_text())
            self.assertEqual(own[0]["points"], 100)
            self.assertNotIn("server_rank", own[0])

    def test_empty_capture_fails_qa(self):
        self.assertFalse(pipeline.generate_qa_report("test", None, [], set())["passed"])

    def test_nav_test_uses_deleted_temporary_directory(self):
        shots = []
        def navigator(adb, device, folder, **kwargs):
            shots.append(folder)
            self.assertTrue(os.path.isdir(folder))
            return Mock(matchup={"opponent": "119", "home_role": "defending"})
        with patch.object(pipeline, "find_adb", return_value="mock-adb"), \
             patch.object(pipeline, "check_and_compile_ocr"), \
             patch.object(pipeline, "auto_detect_device", return_value="mock-device"), \
             patch.object(pipeline, "Navigator", side_effect=navigator), \
             patch.object(pipeline, "staging_dir") as stage, contextlib.redirect_stdout(io.StringIO()):
            pipeline.main(["--nav-test", "--no-apparatchik"])
        stage.assert_not_called()
        self.assertFalse(os.path.exists(shots[0]))


class TestEarlyReprocessing(unittest.TestCase):
    def test_from_file_early_ocr_archives_provenance_and_preserves_capture(self):
        fixture = Path(__file__).parent / "fixtures/2026-10-10"
        repository = str(Path(__file__).resolve().parents[2])
        original = json.loads((fixture / "capture_rankings.json").read_text())
        verified = json.loads((fixture / "verified_rankings.json").read_text())
        ocr = [json.loads(p.read_text()) for p in sorted((fixture / "early_ocr").glob("*.json"))]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            images = root / "phone"
            images.mkdir()
            for i in range(13):
                (images / f"Screenshot_20261010_1251{i:02d}.png").write_bytes(b"mock image")
            worker = Mock()
            worker.process.side_effect = ocr
            def publish(*args):
                return write_event_outputs(str(root / "events" / args[0]), *args)
            def clean(rows, home, opponent, event_id=None, base_dir=None, include_reviews=False):
                return clean_records(rows, home, opponent, event_id, repository, include_reviews)
            archive = Mock(return_value=[{"file": "screenshots/player_01.webp", "kind": "player-screenshot"}])
            with patch.object(pipeline, "BASE_DIR", str(root)), \
                 patch.object(pipeline, "check_and_compile_ocr"), \
                 patch.object(pipeline, "OCRWorker", return_value=worker), \
                 patch.object(pipeline, "clean_records", side_effect=clean), \
                 patch.object(pipeline, "publish_event", side_effect=publish), \
                 patch.object(pipeline, "archive_screenshots", archive), \
                 patch.object(pipeline, "staging_dir") as stage, contextlib.redirect_stdout(io.StringIO()):
                pipeline.main(["--date", "2026-10-10", "--opponent", "119", "--home-role", "defending",
                               "--from-file", str(fixture / "capture_rankings.json"),
                               "--early-screenshots", str(images)])
                folder = root / "events/2026-10-10-s117-vs-s119"
                rows = json.loads((folder / "rankings.json").read_text())
                own = json.loads((folder / "capture_rankings.json").read_text())
                capture = json.loads((folder / "capture.json").read_text())
                qa = json.loads((folder / "capture_qa.json").read_text())
                self.assertEqual(len(rows), 908)
                self.assertEqual([r["points"] for r in rows[:30]], [r["points"] for r in verified[:30]])
                self.assertEqual([r["commander"] for r in rows], [r["commander"] for r in verified])
                self.assertEqual([r["points"] for r in own], [r["points"] for r in original])
                self.assertEqual(capture["early_screenshots"]["cutoff"], 30)
                self.assertIn("early_time_range", capture["early_screenshots"])
                self.assertTrue(qa["passed"])
                self.assertIn("name_review", qa)
                self.assertIn("alliance_review", qa)
                self.assertEqual(len(archive.call_args.args[0]), 13)
                self.assertTrue(all(s[2] == "player-screenshot" for s in archive.call_args.args[0]))
                self.assertTrue(all(len(s) == 4 for s in archive.call_args.args[0]))
                stage.assert_not_called()
                worker.close.assert_called_once()
                saved = (folder / "capture_rankings.json").read_bytes()
                capture_saved = (folder / "capture.json").read_bytes()
                pipeline.main(["--date", "2026-10-10", "--opponent", "119", "--home-role", "defending",
                               "--from-file", str(folder / "rankings.json")])
                self.assertEqual((folder / "capture_rankings.json").read_bytes(), saved)
                self.assertEqual((folder / "capture.json").read_bytes(), capture_saved)


class TestScreenshotTimes(unittest.TestCase):
    def test_filename_local_time_and_mtime_fallback(self):
        value = pipeline.screenshot_time("Screenshot_20261010_125059_extra.png")
        self.assertEqual(value.replace(tzinfo=None), datetime(2026, 10, 10, 12, 50, 59))
        with tempfile.NamedTemporaryFile() as f:
            os.utime(f.name, (123456, 123456))
            self.assertEqual(pipeline.screenshot_time(f.name).timestamp(), 123456)

    def test_archiving_uses_supplied_phone_time(self):
        with tempfile.TemporaryDirectory() as tmp, patch("screenshots._compress_one", return_value=tmp + "/screenshots/player_01.webp"):
            taken = datetime(2026, 10, 10, 14, 50, 59, tzinfo=timezone.utc)
            entries = archive_screenshots([("unused.png", "player_01", "player-screenshot", taken)], tmp)
            self.assertEqual(entries[0]["taken_at"], "2026-10-10T12:50:59-02:00")
            self.assertEqual(entries[0]["kind"], "player-screenshot")

    def test_archive_selection_keeps_two_terminal_stalls_and_drops_seek_frames(self):
        def frame(name, ranks):
            return {"frame": name, "rows": [{"rank": rank} for rank in ranks]}

        frame_info = [
            frame("frame_0001.png", range(1, 10)),
            frame("frame_0001_b.png", range(1, 10)),
            frame("frame_0002.png", range(2, 11)),
            frame("frame_0003.png", range(3, 12)),
            frame("frame_0003_banner_retry_1.png", range(3, 12)),
            frame("frame_0004.png", range(3, 12)),
            frame("frame_0005.png", range(3, 12)),
            frame("frame_0006.png", range(3, 12)),
            frame("frame_0007.png", range(3, 12)),
        ]
        filenames = [
            "frame_0001.png", "frame_0001_b.png", "frame_0002.png", "frame_0003.png",
            "frame_0003_banner_retry_1.png", "frame_0004.png", "frame_0005.png",
            "frame_0006.png", "frame_0007.png", "repair_seek_33_000.png",
            "repair_r1_rank_33_shot_1.png", "nav_rankings.png", "verify_top_1.png",
        ]
        selected = select_screenshots_to_archive(filenames, frame_info)
        self.assertEqual(selected, [
            "frame_0001.png", "frame_0001_b.png", "frame_0002.png", "frame_0003.png",
            "frame_0003_banner_retry_1.png", "frame_0006.png", "frame_0007.png",
            "repair_r1_rank_33_shot_1.png", "nav_rankings.png", "verify_top_1.png",
        ])

    def test_capture_record_deduplicates_screenshots_by_file_latest_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            event_dir = Path(tmp)
            capture_path = event_dir / "capture.json"
            capture_path.write_text(json.dumps({
                "event_id": "test",
                "passes": [],
                "screenshots": [
                    {"file": "screenshots/player_01.webp", "kind": "old", "taken_at": "old"},
                    {"file": "screenshots/player_02.webp", "kind": "player-screenshot"},
                    {"file": "screenshots/player_01.webp", "kind": "duplicate", "taken_at": "duplicate"},
                ],
            }))
            updated = {"file": "screenshots/player_01.webp", "kind": "player-screenshot", "taken_at": "new"}
            with contextlib.redirect_stdout(io.StringIO()):
                pipeline.write_capture_record("test", None, [updated], event_dir=str(event_dir))
            screenshots = json.loads(capture_path.read_text())["screenshots"]
            self.assertEqual([entry["file"] for entry in screenshots],
                             ["screenshots/player_01.webp", "screenshots/player_02.webp"])
            self.assertEqual(screenshots[0], updated)


if __name__ == "__main__":
    unittest.main()
