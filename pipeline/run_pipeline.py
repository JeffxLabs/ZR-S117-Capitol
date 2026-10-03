#!/usr/bin/env python3
"""
End-to-End Capitol War Event Ingestion Pipeline for Z Route: Redemption.

Usage:
  python3 pipeline/run_pipeline.py --opponent 113 --home 117 --home-role attacking
"""
import os
import sys
import time
import json
import shutil
import argparse
import subprocess
import threading
import contextlib
from datetime import datetime, timezone
from typing import List, Dict, Optional, Any, Set, Tuple

from cleaner import clean_player_record, validate_dataset, consolidate_alliance_variants
from processor import publish_event
from screenshots import staging_dir, archive_screenshots, SERVER_TZ
from frame_parser import parse_frame, FrameResult, find_banner_close, is_rankings_list
from merge import ObservationStore, merge_rank
from apparatchik_control import monitoring_paused, ApparatchikError
from navigation import Navigator, NavigationError
import math

PIPELINE_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(PIPELINE_DIR)
OCR_SRC = os.path.join(PIPELINE_DIR, "vision_ocr.swift")
OCR_BIN = os.path.join(PIPELINE_DIR, "vision_ocr")


def find_adb():
    adb = shutil.which("adb")
    if adb:
        return adb
    for p in [
        "/opt/homebrew/bin/adb",
        "/usr/local/bin/adb",
        os.path.expanduser("~/Library/Android/sdk/platform-tools/adb")
    ]:
        if os.path.exists(p):
            return p
    return None


def check_and_compile_ocr():
    if not os.path.exists(OCR_BIN) or os.path.getmtime(OCR_SRC) > os.path.getmtime(OCR_BIN):
        swiftc = shutil.which("swiftc")
        if not swiftc:
            print("Error: Swift compiler (swiftc) not found on macOS system.")
            sys.exit(1)
        print("Compiling native Apple Vision OCR worker (swiftc -O)...")
        subprocess.run([swiftc, "-O", OCR_SRC, "-o", OCR_BIN], check=True)
        print("Compiled successfully.")


class OCRWorker:
    """Persistent OCR worker process communicating via stdin/stdout."""

    def __init__(self, ocr_bin: str = OCR_BIN):
        self.ocr_bin = ocr_bin
        self.proc: Optional[subprocess.Popen] = None
        self._start_worker()

    def _start_worker(self):
        try:
            self.proc = subprocess.Popen(
                [self.ocr_bin, "--stdin"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1
            )
        except Exception as e:
            print(f"Warning: Persistent OCR worker could not be started ({e}). Falling back to single-shot.")
            self.proc = None

    def process(self, img_path: str) -> List[Dict[str, Any]]:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.write(img_path + "\n")
                self.proc.stdin.flush()
                line = self.proc.stdout.readline()
                if line:
                    return json.loads(line)
            except Exception as e:
                print(f"Warning: Persistent OCR read error ({e}), restarting worker...")
                self.close()
                self._start_worker()

        # Fallback to single-process execution
        out = subprocess.check_output([self.ocr_bin, img_path])
        return json.loads(out)

    def close(self):
        if self.proc:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except Exception:
                pass
            self.proc = None


def auto_detect_device(adb):
    out = subprocess.check_output([adb, "devices"]).decode("utf-8")
    lines = [l.strip() for l in out.splitlines() if l.strip() and not l.startswith("List of")]
    for line in lines:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            if "127.0.0.1" in parts[0] or "5565" in parts[0]:
                return parts[0]
    if lines:
        return lines[0].split()[0]
    return "127.0.0.1:5565"


def screencap(adb, device, dest_path):
    with open(dest_path, "wb") as f:
        subprocess.run([adb, "-s", device, "exec-out", "screencap", "-p"], stdout=f)


def scroll_to_top_verified(adb, device, ocr_worker: OCRWorker, staging_folder: str, max_retries: int = 5):
    """Rewind rankings to the top and verify via OCR that Rank 1 is at the top slot."""
    print("Rewinding rankings to the top (Rank 1)...")
    for attempt in range(1, max_retries + 1):
        for _ in range(12):
            subprocess.run([adb, "-s", device, "shell", "input", "swipe", "540", "500", "540", "1750", "180"])
            time.sleep(0.2)
        time.sleep(1.2)

        verify_img = os.path.join(staging_folder, f"verify_top_{attempt}.png")
        screencap(adb, device, verify_img)
        items = ocr_worker.process(verify_img)
        frame_res = parse_frame(items, prev_max_rank=None)

        # Confirm the top slot is rank 1 from real rank digits (a screen with no rank digits
        # would otherwise default to offset 1)
        has_rank_digits = any(it["x"] < 0.20 and it["text"].strip().isdigit() and 0.05 < it["y"] < 0.865
                              for it in items)
        if has_rank_digits and frame_res.rows and frame_res.offset == 1 and frame_res.rows[0]["rank"] == 1:
            print(f"Leaderboard top verified at Rank 1 (attempt {attempt}).")
            return

        print(f"Top verification attempt {attempt}/{max_retries}: offset is {frame_res.offset}. Retrying rewind...")

    raise RuntimeError("Verification failed: Unable to confirm leaderboard top (Rank 1) after rewinding.")


def swipe_async(adb, device, swipe_px: int = 420):
    y_start = 1350
    y_end = max(100, y_start - swipe_px)
    subprocess.run([adb, "-s", device, "shell", "input", "swipe", "540", str(y_start), "540", str(y_end), "400"])


def capture_frame_with_banner_mitigation(
    adb,
    device,
    ocr_worker: OCRWorker,
    img_path: str,
    prev_max: Optional[int],
    obs_store: ObservationStore,
    base_name: str
) -> FrameResult:
    """Capture a screenshot, run OCR, parse rows, and retry if notification banner is active."""
    screencap(adb, device, img_path)
    items = ocr_worker.process(img_path)
    res = parse_frame(items, prev_max_rank=prev_max)
    obs_store.add_frame(base_name, res)

    # Banner mitigation 1: tap the banner's close X, then confirm we are still on the Rankings list
    close_btn = find_banner_close(items)
    if close_btn is not None:
        x = int((close_btn["x"] + close_btn["width"] / 2) * 1080)
        y = int((1 - (close_btn["y"] + close_btn["height"] / 2)) * 1920)
        print(f" [banner in {base_name}] tapping its close X at ({x},{y})")
        subprocess.run([adb, "-s", device, "shell", "input", "tap", str(x), str(y)])
        time.sleep(0.6)
        prefix = base_name.rsplit(".", 1)[0]
        after_base = f"{prefix}_banner_closed.png"
        after_img = os.path.join(os.path.dirname(img_path), after_base)
        screencap(adb, device, after_img)
        after_items = ocr_worker.process(after_img)
        if not is_rankings_list(after_items):
            # The banner had already gone and the tap opened something else: back out once
            print(" tap left the Rankings list; pressing back")
            subprocess.run([adb, "-s", device, "shell", "input", "keyevent", "4"])
            time.sleep(1.0)
            screencap(adb, device, after_img)
            after_items = ocr_worker.process(after_img)
            if not is_rankings_list(after_items):
                raise RuntimeError("Lost the Rankings list after closing a notification banner")
        res = parse_frame(after_items, prev_max_rank=prev_max)
        obs_store.add_frame(after_base, res)

    # Banner mitigation 2: if a banner is still there (no X readable), wait 1.5 s and re-capture (up to 4 retries)
    if res.banner_detected and any(r["tainted"] for r in res.rows):
        print(f" [banner detected in {base_name}] waiting 1.5s for banner to clear...")
        prefix = base_name.rsplit(".", 1)[0]
        dir_name = os.path.dirname(img_path)
        for retry in range(1, 5):
            time.sleep(1.5)
            retry_base = f"{prefix}_banner_retry_{retry}.png"
            retry_img = os.path.join(dir_name, retry_base)
            screencap(adb, device, retry_img)
            retry_items = ocr_worker.process(retry_img)
            retry_res = parse_frame(retry_items, prev_max_rank=prev_max)
            obs_store.add_frame(retry_base, retry_res)
            if not (retry_res.banner_detected and any(r["tainted"] for r in retry_res.rows)):
                print(f" Banner cleared on retry {retry}.")
                res = retry_res
                break
    return res


def find_problem_ranks(merged_records: List[Dict[str, Any]]) -> List[int]:
    """Identify ranks that are missing, lack clean observations, disagree, or break monotonicity."""
    problem_ranks = set()
    ranks_by_num = {r["rank"]: r for r in merged_records}
    max_rank = max(ranks_by_num.keys()) if ranks_by_num else 0

    # 1. Missing ranks in sequence
    for r in range(1, max_rank + 1):
        if r not in ranks_by_num:
            problem_ranks.add(r)

    # 2. No clean observation or disagreement between clean observations
    for r, rec in ranks_by_num.items():
        diag = rec.get("diagnostics", {})
        n_clean = diag.get("n_clean", 0)
        if n_clean == 0:
            problem_ranks.add(r)
        else:
            agr = diag.get("agreement", {})
            if agr.get("points", 1.0) < 1.0 or agr.get("commander", 1.0) < 0.5:
                problem_ranks.add(r)

    # 3. Monotonic points check against neighbours
    sorted_recs = sorted(merged_records, key=lambda x: x["rank"])
    for i in range(1, len(sorted_recs)):
        p_prev = sorted_recs[i - 1].get("points")
        p_curr = sorted_recs[i].get("points")
        if p_prev is not None and p_curr is not None and p_curr > p_prev:
            problem_ranks.add(sorted_recs[i - 1]["rank"])
            problem_ranks.add(sorted_recs[i]["rank"])

    return sorted(problem_ranks)


ROW_PX = 140  # approx. list movement per row for a controlled 400 ms swipe (calibrated live)


def _visible_ranks(adb, device, ocr_worker, path):
    screencap(adb, device, path)
    res = parse_frame(ocr_worker.process(path), prev_max_rank=None)
    return [r["rank"] for r in res.rows if r["complete"]]


def seek_rank(adb, device, ocr_worker, frames_dir, target_rank, max_moves=45):
    """Scroll until target_rank is fully visible (not at the very top/bottom edge).
    Closed loop: re-reads the visible ranks after every move, so no exact calibration is needed."""
    for move in range(max_moves):
        vis = _visible_ranks(adb, device, ocr_worker, os.path.join(frames_dir, f"repair_seek_{target_rank}_{move:02d}.png"))
        if not vis:
            time.sleep(1.0)  # still scrolling / momentarily unreadable: look again
            continue
        lo, hi = min(vis), max(vis)
        if lo < target_rank < hi or (target_rank <= 3 and lo <= target_rank):
            return True
        center = (lo + hi) / 2.0
        delta = target_rank - center
        if abs(delta) > 40:
            # Long jump: fast flings (each moves many rows); fine positioning follows
            for _ in range(3 if abs(delta) > 250 else 1):
                if delta > 0:
                    subprocess.run([adb, "-s", device, "shell", "input", "swipe", "540", "1650", "540", "450", "150"])
                else:
                    subprocess.run([adb, "-s", device, "shell", "input", "swipe", "540", "450", "540", "1650", "150"])
                time.sleep(0.25)
            time.sleep(1.2)
        else:
            px = int(min(1100, max(200, abs(delta) * ROW_PX)))
            if delta > 0:
                subprocess.run([adb, "-s", device, "shell", "input", "swipe", "540", "1500", "540", str(1500 - px), "400"])
            else:
                subprocess.run([adb, "-s", device, "shell", "input", "swipe", "540", "400", "540", str(400 + px), "400"])
            time.sleep(0.5)
    return False


def repair_ranks(
    adb,
    device,
    ocr_worker: OCRWorker,
    obs_store: ObservationStore,
    frames_dir: str,
    max_rounds: int = 2
) -> Tuple[List[Dict[str, Any]], Set[int]]:
    """Revisit ranks that are missing, have no clean read, conflict, or break the point ordering,
    and add fresh observations (rank numbers re-fitted per frame, no window filter)."""
    all_repaired: Set[int] = set()
    for round_idx in range(1, max_rounds + 1):
        merged_now = obs_store.merge_all()
        problems = find_problem_ranks(merged_now)
        if round_idx == 1:
            singles = [m["rank"] for m in merged_now if m["diagnostics"]["n_clean"] == 1]
            problems = sorted(set(problems) | set(singles))
        if not problems:
            print(f"Repair round {round_idx}: all ranks clean and consistent.")
            break
        print(f"\nRepair round {round_idx}/{max_rounds}: {len(problems)} ranks to recheck: {problems[:20]}")
        covered: Set[int] = set()
        for target_rank in problems:
            if target_rank in covered:
                continue
            all_repaired.add(target_rank)
            if not seek_rank(adb, device, ocr_worker, frames_dir, target_rank):
                print(f"  could not bring rank {target_rank} on screen")
                continue
            for shot in range(1, 3):
                name = f"repair_r{round_idx}_rank_{target_rank}_shot_{shot}.png"
                res = capture_frame_with_banner_mitigation(adb, device, ocr_worker, os.path.join(frames_dir, name),
                                                           None, obs_store, name)
                covered.update(r["rank"] for r in res.rows if r["complete"] and not r["tainted"])
                time.sleep(0.3)
    return obs_store.merge_all(), all_repaired


def capture_leaderboard(
    adb,
    device,
    frames_dir: str,
    ocr_worker: OCRWorker,
    swipe_px: int = 420,
    settle_sec: float = 0.15,
    max_frames: int = 450
) -> Tuple[List[Dict[str, Any]], Dict[str, Any], ObservationStore, Set[int]]:
    """Extract leaderboard ranks using multi-observation streaming and voting."""
    os.makedirs(frames_dir, exist_ok=True)
    print("\nStarting hardened leaderboard extraction with multi-observation voting...")
    t_start = time.time()
    started_at = datetime.now().astimezone()

    obs_store = ObservationStore()
    last_frame_ranks = ()
    stuck_count = 0
    frame_count = 0

    for frame in range(max_frames):
        frame_count = frame + 1
        base_name = f"frame_{frame_count:04d}.png"
        img_path = os.path.join(frames_dir, base_name)

        cur_max = max(obs_store.observations_by_rank.keys()) if obs_store.observations_by_rank else None

        # Capture and mitigate banner
        res = capture_frame_with_banner_mitigation(
            adb, device, ocr_worker, img_path, cur_max, obs_store, base_name
        )
        if frame == 0:
            # Medal rows (1-3) leave the screen on the first swipe: give them a second clean read
            time.sleep(0.5)
            again = f"frame_{frame_count:04d}_b.png"
            capture_frame_with_banner_mitigation(adb, device, ocr_worker, os.path.join(frames_dir, again),
                                                 cur_max, obs_store, again)

        # Trigger swipe
        swipe_thread = threading.Thread(target=swipe_async, args=(adb, device, swipe_px))
        swipe_thread.start()

        frame_ranks = tuple(sorted(r["rank"] for r in res.rows if r["complete"]))

        swipe_thread.join()
        time.sleep(settle_sec)

        cur_max = max(obs_store.observations_by_rank.keys()) if obs_store.observations_by_rank else 0
        sys.stdout.write(f"\r[Frame {frame_count:3d}] Highest Rank: {cur_max:4d} | Total Obs: {sum(len(v) for v in obs_store.observations_by_rank.values()):4d} | Rate: {(time.time()-t_start)/frame_count:.2f}s/frame")
        sys.stdout.flush()

        # Check for bottom termination using fitted ranks
        if frame_ranks == last_frame_ranks and len(frame_ranks) > 0 and cur_max > 50:
            stuck_count += 1
            if stuck_count >= 5:
                print(f"\nLeaderboard bottom detected at Rank {cur_max}!")
                break
        else:
            stuck_count = 0
            last_frame_ranks = frame_ranks

    # Main pass merge
    initial_merged = obs_store.merge_all()

    # Repair pass
    final_merged, repaired_ranks = repair_ranks(adb, device, ocr_worker, obs_store, frames_dir)

    finished_at = datetime.now().astimezone()
    print(f"\nExtraction completed in {time.time()-t_start:.1f}s. Captured {len(final_merged)} total ranks.")
    capture_info = {
        "timezone": "server time (UTC-2)",
        "started_at": started_at.astimezone(SERVER_TZ).isoformat(timespec="seconds"),
        "finished_at": finished_at.astimezone(SERVER_TZ).isoformat(timespec="seconds"),
        "started_at_utc": started_at.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "finished_at_utc": finished_at.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "duration_seconds": round(time.time() - t_start, 1),
        "frames": frame_count,
        "device": device,
        "entries_captured": len(final_merged),
    }
    return final_merged, capture_info, obs_store, repaired_ranks


def generate_qa_report(
    event_id: str,
    obs_store: Optional[ObservationStore],
    merged_records: List[Dict[str, Any]],
    repaired_ranks: Set[int]
) -> Dict[str, Any]:
    """Generate compact capture QA report."""
    total_ranks = len(merged_records)
    ranks_by_num = {r["rank"]: r for r in merged_records}
    max_rank = max(ranks_by_num.keys()) if ranks_by_num else 0
    missing = [r for r in range(1, max_rank + 1) if r not in ranks_by_num]

    single_clean = []
    unresolved = []
    for r, rec in ranks_by_num.items():
        diag = rec.get("diagnostics", {})
        n_clean = diag.get("n_clean", 0)
        if n_clean == 1:
            single_clean.append(r)
        if n_clean == 0:
            unresolved.append(r)
        else:
            agr = diag.get("agreement", {})
            if agr.get("points", 1.0) <= 0.5 or agr.get("commander", 1.0) < 0.5:
                unresolved.append(r)

    # Monotonic points check
    monotonic_violations = []
    sorted_recs = sorted(merged_records, key=lambda x: x["rank"])
    for i in range(1, len(sorted_recs)):
        p_prev = sorted_recs[i - 1].get("points")
        p_curr = sorted_recs[i].get("points")
        if p_prev is not None and p_curr is not None and p_curr > p_prev:
            monotonic_violations.append({
                "rank_prev": sorted_recs[i - 1]["rank"],
                "points_prev": p_prev,
                "rank_curr": sorted_recs[i]["rank"],
                "points_curr": p_curr,
            })

    monotonic_passed = (len(monotonic_violations) == 0)

    # The leaderboard can still move during a capture: the same player at two ranks means rows shifted
    from merge import name_key
    by_name: Dict[str, List[int]] = {}
    for rec in merged_records:
        k = name_key(rec.get("commander") or "")
        if k and not rec.get("diagnostics", {}).get("name_unreadable"):
            by_name.setdefault(f"{k}|{rec.get('alliance')}", []).append(rec["rank"])
    duplicate_players = sorted(v for v in by_name.values() if len(v) > 1)
    names_unreadable = sorted(r for r, rec in ranks_by_num.items()
                              if rec.get("diagnostics", {}).get("name_unreadable"))

    # Banner frames count
    banner_frames_count = 0
    if obs_store:
        banner_frames_count = sum(1 for f in obs_store.get_raw_observations() if f.get("banner_detected"))

    passed = (len(missing) == 0 and len(unresolved) == 0 and monotonic_passed and not duplicate_players)

    report = {
        "event_id": event_id,
        "total_ranks": total_ranks,
        "max_rank": max_rank,
        "missing_ranks": missing,
        "ranks_repaired": sorted(list(repaired_ranks)),
        "ranks_unresolved": sorted(list(set(unresolved))),
        "ranks_single_clean": sorted(single_clean),
        "banner_frames_count": banner_frames_count,
        "duplicate_players": duplicate_players,
        "names_unreadable": names_unreadable,
        "monotonic_check": {
            "passed": monotonic_passed,
            "violations_count": len(monotonic_violations),
            "violations": monotonic_violations[:10]
        },
        "passed": passed
    }

    qa_path = os.path.join(BASE_DIR, "events", event_id, "capture_qa.json")
    os.makedirs(os.path.dirname(qa_path), exist_ok=True)
    with open(qa_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"Capture QA report saved: {qa_path}")

    return report


def estimate_pause_minutes():
    """Apparatchik safety pause: about twice the expected run time (from the largest previous
    leaderboard), plus a margin. The pipeline resumes Apparatchik itself as soon as it finishes."""
    players = 1500
    try:
        with open(os.path.join(BASE_DIR, "events", "manifest.json"), encoding="utf-8") as f:
            players = max([e.get("total_players", 0) for e in json.load(f)] + [600])
    except (OSError, ValueError):
        pass
    expected_sec = players / 3.0 * 1.4 + 240  # ~3 new rows per frame at ~1.4 s, plus navigation/repair
    return int(math.ceil(expected_sec * 2 / 60.0)) + 5


def write_capture_record(event_id, capture_info, screenshots=None):
    path = os.path.join(BASE_DIR, "events", event_id, "capture.json")
    record = {
        "event_id": event_id,
        "passes": [capture_info],
        "method": "pipeline/run_pipeline.py live ADB capture (BlueStacks) + Apple Vision OCR"
    }
    if screenshots:
        record["screenshots"] = screenshots
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"Capture timestamp recorded: {path}")


def main():
    parser = argparse.ArgumentParser(description="Capitol War Ranking Pipeline for Z Route: Redemption")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"), help="Event date (YYYY-MM-DD)")
    parser.add_argument("--home", default="117", help="Home server number (default: 117)")
    parser.add_argument("--opponent", default="119", help="Opponent server number (default: 119)")
    parser.add_argument("--home-role", required=True, choices=["attacking", "defending"],
                        help="Whether the home server is attacking or defending the Capitol this event")
    parser.add_argument("--device", default=None, help="ADB device ID (default: auto-detect)")
    parser.add_argument("--no-rewind", action="store_true", help="Skip scrolling back to top")
    parser.add_argument("--from-file", default=None, help="Skip capture and process from existing raw JSON file")
    parser.add_argument("--swipe-px", type=int, default=420, help="Swipe distance in pixels (default: 420)")
    parser.add_argument("--settle-sec", type=float, default=0.15, help="Settle time after swipe in seconds (default: 0.15)")
    parser.add_argument("--allow-incomplete", action="store_true", help="Allow pipeline to succeed even if QA validation fails")
    parser.add_argument("--no-apparatchik", action="store_true",
                        help="Do not pause/resume Apparatchik monitoring (only if it is not driving this emulator)")
    parser.add_argument("--nav-test", action="store_true",
                        help="Only test navigation: pause Apparatchik, open the Rankings list, return to the city, resume")
    parser.add_argument("--no-navigate", action="store_true",
                        help="Assume the Rankings list is already open and do not return to the city afterwards")
    args = parser.parse_args()

    event_id = f"{args.date}-s{args.home}-vs-s{args.opponent}"
    title = f"Capitol War: Server {args.home} vs Server {args.opponent}"

    raw_records = []
    capture_info = None
    obs_store = None
    repaired_ranks: Set[int] = set()
    frames_dir = staging_dir(event_id)

    if args.nav_test:
        adb = find_adb()
        check_and_compile_ocr()
        device = args.device or auto_detect_device(adb)
        try:
            with (contextlib.nullcontext() if args.no_apparatchik else monitoring_paused(10)):
                nav = Navigator(adb, device, frames_dir)
                try:
                    nav.go_to_rankings()
                    print("Navigation test: Rankings list reached.")
                finally:
                    nav.return_to_city()
        except (ApparatchikError, NavigationError) as e:
            print(f"Error: {e}")
            sys.exit(1)
        return

    if args.from_file:
        print(f"Loading existing raw records from: {args.from_file}")
        with open(args.from_file, "r", encoding="utf-8") as f:
            raw_records = json.load(f)
    else:
        adb = find_adb()
        if not adb:
            print("Error: adb binary not found. Please install Android platform-tools.")
            sys.exit(1)
        check_and_compile_ocr()
        device = args.device or auto_detect_device(adb)
        print(f"Connected to device: {device}")

        for old in os.listdir(frames_dir):
            if old.endswith(".png") and old.startswith(("frame_", "repair_", "verify_top_", "nav_")):
                os.remove(os.path.join(frames_dir, old))

        pause_minutes = estimate_pause_minutes()
        pause_ctx = contextlib.nullcontext() if args.no_apparatchik else monitoring_paused(pause_minutes)
        try:
            with pause_ctx:
                nav = Navigator(adb, device, frames_dir)
                ocr_worker = OCRWorker(OCR_BIN)
                try:
                    if not args.no_navigate:
                        nav.go_to_rankings()
                    if not args.no_rewind:
                        scroll_to_top_verified(adb, device, ocr_worker, frames_dir)
                    raw_records, capture_info, obs_store, repaired_ranks = capture_leaderboard(
                        adb, device, frames_dir, ocr_worker,
                        swipe_px=args.swipe_px,
                        settle_sec=args.settle_sec
                    )
                finally:
                    ocr_worker.close()
                    if not args.no_navigate:
                        try:
                            nav.return_to_city()
                        except NavigationError as e:
                            print(f"WARNING: {e}")
        except (ApparatchikError, NavigationError) as e:
            print(f"Error: {e}")
            sys.exit(1)

        # Save raw_observations.json to staging_dir
        raw_obs_path = os.path.join(frames_dir, "raw_observations.json")
        with open(raw_obs_path, "w", encoding="utf-8") as rf:
            json.dump(obs_store.get_raw_observations(), rf, indent=2, ensure_ascii=False)
        print(f"Raw observations saved: {raw_obs_path}")

    # Generate QA report
    qa_report = generate_qa_report(event_id, obs_store, raw_records, repaired_ranks)

    # 1. Clean and normalize (carrying server explicitly)
    print("\nCleaning records and resolving OCR normalizations...")
    cleaned_records = [clean_player_record(r, home_server=args.home, visiting_server=args.opponent) for r in raw_records]
    for variant, canonical, n in consolidate_alliance_variants(cleaned_records):
        print(f"  [alliance merge] {variant!r} -> {canonical!r} ({n} rows)")

    # 2. Validate integrity
    errors = validate_dataset(cleaned_records)
    if errors:
        print("\n[WARNING] Dataset validation notes:")
        for err in errors:
            print(f"  - {err}")
    else:
        print("Data integrity check: 100% PASSED (0 missing ranks, monotonic points verified).")

    # 3. Publish event & update GitHub Pages
    print(f"\nPublishing event '{event_id}'...")
    meta, alliances = publish_event(
        event_id=event_id,
        title=title,
        date_str=args.date,
        home_server=args.home,
        opponent_server=args.opponent,
        players=cleaned_records,
        home_role=args.home_role
    )
    if capture_info:
        kinds = {"frame_": "capture-frame", "repair_": "repair", "verify_top_": "rewind-check", "nav_": "navigation"}
        frames = sorted(f for f in os.listdir(frames_dir) if f.endswith(".png") and f.startswith(tuple(kinds)))
        print(f"Archiving {len(frames)} compressed screenshots to events/{event_id}/screenshots/ ...")
        shots = archive_screenshots(
            [(os.path.join(frames_dir, f), f[:-4], next(k for p, k in kinds.items() if f.startswith(p))) for f in frames],
            os.path.join(BASE_DIR, "events", event_id))
        write_capture_record(event_id, capture_info, shots)
        print(f"Full-size frames kept locally in {frames_dir} (auto-deleted after a few days by the cleanup agent).")
    else:
        print("Reprocessed from file: existing capture.json (if any) left unchanged.")

    print("\n" + "=" * 80)
    print(f" CAPITOL WAR EVENT PUBLISHED: {title}")
    print("=" * 80)
    print(f" Total Commanders: {meta['total_players']:,}")
    print(f" Total War Points: {meta['total_points']:,}")
    print(f" Total Alliances:  {meta['unique_alliances']:,}")
    print(f" Top Alliance:     {alliances[0]['alliance']} ({alliances[0]['members_count']} members, {alliances[0]['total_points']:,} pts)")
    print("=" * 80)
    print(f"\nAssets written to:")
    print(f"  - events/{event_id}/")
    print(f"  - data/capitol_event_rankings.csv")
    print(f"  - data/capitol_event_rankings.json")
    print("\nTo push changes to GitHub Pages:")
    print("  git add . && git commit -m 'feat: update event rankings' && git push origin main\n")

    # Exit non-zero if QA failed and --allow-incomplete is not set
    if not qa_report["passed"]:
        print("!" * 80)
        print(" [WARNING] CAPTURE QA INTEGRITY CHECK FAILED:")
        if qa_report["missing_ranks"]:
            print(f"  - Missing ranks: {qa_report['missing_ranks'][:15]}")
        if qa_report["ranks_unresolved"]:
            print(f"  - Unresolved conflicts/tainted at ranks: {qa_report['ranks_unresolved'][:15]}")
        if not qa_report["monotonic_check"]["passed"]:
            print(f"  - Monotonic points violations: {qa_report['monotonic_check']['violations_count']}")
        print("!" * 80)
        if not args.allow_incomplete:
            print("Aborting because --allow-incomplete was not specified.")
            sys.exit(1)


if __name__ == "__main__":
    main()
