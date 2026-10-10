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
import tempfile
import re
from datetime import datetime, timezone
from typing import List, Dict, Optional, Any, Set, Tuple, Callable

from cleaner import clean_player_record, clean_records, validate_dataset
from processor import publish_event, write_event_outputs
from screenshots import staging_dir, archive_screenshots, select_screenshots_to_archive, SERVER_TZ
from frame_parser import parse_frame, FrameResult, find_banner_close, is_rankings_list
from merge import ObservationStore, name_key
from apparatchik_control import monitoring_paused, ApparatchikError
from navigation import Navigator, NavigationError
from human_input import HumanInput
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


def scroll_to_top_verified(adb, device, ocr_worker: OCRWorker, staging_folder: str, max_retries: int = 5,
                           human_input: Optional[HumanInput] = None):
    """Rewind rankings to the top and verify via OCR that Rank 1 is at the top slot."""
    if human_input is None:
        human_input = HumanInput(adb, device)
    print("Rewinding rankings to the top (Rank 1)...")
    for attempt in range(1, max_retries + 1):
        for _ in range(12):
            human_input.swipe(540, 500, 540, 1750, 180)
            human_input.pause(0.2)
        human_input.pause(1.2)

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


def swipe_async(adb, device, swipe_px: int = 420, human_input: Optional[HumanInput] = None):
    if human_input is None:
        human_input = HumanInput(adb, device)
    y_start = 1350
    y_end = max(100, y_start - swipe_px)
    human_input.swipe(540, y_start, 540, y_end, 400)


def capture_frame_with_banner_mitigation(
    adb,
    device,
    ocr_worker: OCRWorker,
    img_path: str,
    prev_max: Optional[int],
    obs_store: ObservationStore,
    base_name: str,
    human_input: Optional[HumanInput] = None,
) -> FrameResult:
    """Capture a screenshot, run OCR, parse rows, and retry if notification banner is active."""
    if human_input is None:
        human_input = HumanInput(adb, device)
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
        human_input.tap(x, y, jitter_px=6, max_offset=6)
        human_input.pause(0.6)
        prefix = base_name.rsplit(".", 1)[0]
        after_base = f"{prefix}_banner_closed.png"
        after_img = os.path.join(os.path.dirname(img_path), after_base)
        screencap(adb, device, after_img)
        after_items = ocr_worker.process(after_img)
        if not is_rankings_list(after_items):
            # The banner had already gone and the tap opened something else: back out once
            print(" tap left the Rankings list; pressing back")
            human_input.back()
            human_input.pause(1.0)
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
            human_input.pause(1.5)
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
MAX_SEEK_MOVES = 120
MAX_FLINGS_PER_MOVE = 8


def _visible_ranks(adb, device, ocr_worker, path):
    screencap(adb, device, path)
    res = parse_frame(ocr_worker.process(path), prev_max_rank=None)
    return [r["rank"] for r in res.rows if r["complete"]]


def _seek_succeeded(visible, target_rank):
    if not visible:
        return False
    lo, hi = min(visible), max(visible)
    return lo < target_rank < hi or (target_rank <= 3 and lo == 1 and target_rank in visible)


def seek_rank_with_controls(
    target_rank: int,
    read_visible: Callable[[], List[int]],
    fling: Callable[[str, int], None],
    drag: Callable[[str, int], None],
    rewind_to_top: Callable[[], None],
    max_moves: Optional[int] = None,
    row_px: int = ROW_PX,
    max_flings_per_move: int = MAX_FLINGS_PER_MOVE,
    pause: Callable[[float], None] = time.sleep,
) -> bool:
    """Seek using injected device controls, with every move checked against OCR.

    Directions are in leaderboard order: ``down`` moves toward larger ranks and
    ``up`` toward smaller ranks. ``max_moves`` counts checked move groups; each
    fling group is itself capped by ``max_flings_per_move``.
    """
    visible = sorted(set(read_visible() or []))
    reads = 1
    center = (min(visible) + max(visible)) / 2.0 if visible else None
    initial_distance = abs(target_rank - center) if center is not None else target_rank
    move_budget = max_moves if max_moves is not None else min(
        MAX_SEEK_MOVES, 20 + int(math.ceil(initial_distance / 10.0)))
    read_budget = max(3, move_budget * 3)
    moves = 0
    fling_estimate = 15.0
    fling_samples = 0
    pending_fling = None
    rewind_attempted = False
    fine_after_rewind = False

    while moves < move_budget and reads <= read_budget:
        if not visible:
            pause(0.25)
            visible = sorted(set(read_visible() or []))
            reads += 1
            continue

        center = (min(visible) + max(visible)) / 2.0
        if pending_fling is not None:
            previous_center, fling_count = pending_fling
            sample = abs(center - previous_center) / fling_count
            sample = min(45.0, max(3.0, sample))
            fling_samples += 1
            fling_estimate += (sample - fling_estimate) / fling_samples
            pending_fling = None

        if _seek_succeeded(visible, target_rank):
            return True

        delta = target_rank - center
        if (not rewind_attempted and target_rank <= 60 and abs(delta) > 120):
            rewind_attempted = True
            rewind_to_top()
            moves += 1
            fine_after_rewind = True
            pending_fling = None
            visible = sorted(set(read_visible() or []))
            reads += 1
            continue

        direction = "down" if delta > 0 else "up"
        if fine_after_rewind or abs(delta) <= 40:
            px = int(min(1100, max(row_px, abs(delta) * row_px)))
            drag(direction, px)
            moves += 1
        else:
            count = min(max_flings_per_move, max(1, int(math.ceil(abs(delta) / fling_estimate))))
            fling(direction, count)
            moves += 1
            pending_fling = (center, count)

        visible = sorted(set(read_visible() or []))
        reads += 1

    return _seek_succeeded(visible, target_rank)


def seek_rank(adb, device, ocr_worker, frames_dir, target_rank, max_moves=None, human_input=None):
    """ADB adapter for the injected, closed-loop rank seeker."""
    if human_input is None:
        human_input = HumanInput(adb, device)
    read_count = 0

    def read_visible():
        nonlocal read_count
        path = os.path.join(frames_dir, f"repair_seek_{target_rank}_{read_count:03d}.png")
        read_count += 1
        return _visible_ranks(adb, device, ocr_worker, path)

    def fling(direction, count):
        start_y, end_y = (1650, 450) if direction == "down" else (450, 1650)
        for _ in range(count):
            human_input.swipe(540, start_y, 540, end_y, 150)
            human_input.fling_gap()
        human_input.pause(0.4)

    def drag(direction, px):
        if direction == "down":
            start_y, end_y = 1500, 1500 - px
        else:
            start_y, end_y = 400, 400 + px
        human_input.swipe(540, start_y, 540, end_y, 400, distance_jitter=0.04)
        human_input.pause(0.5)

    try:
        return seek_rank_with_controls(
            target_rank, read_visible, fling, drag,
            lambda: scroll_to_top_verified(adb, device, ocr_worker, frames_dir,
                                           human_input=human_input),
            max_moves=max_moves,
            pause=human_input.pause,
        )
    except RuntimeError as e:  # a failed rewind fails this seek, not the whole run
        print(f"  seek to rank {target_rank} aborted: {e}")
        return False


def plan_repair_targets(target_ranks, current_center=None, top_cutoff=60):
    """Visit ordinary targets by nearest travel, then the top batch in rank order."""
    pending = sorted({rank for rank in target_ranks if rank > top_cutoff})
    top_targets = sorted({rank for rank in target_ranks if rank <= top_cutoff})
    order = []
    position = current_center
    while pending:
        if position is None:
            target = pending[-1]
        else:
            target = min(pending, key=lambda rank: (abs(rank - position), -rank))
        order.append(target)
        pending.remove(target)
        position = target
    return order + top_targets


def _latest_visible_center(obs_store):
    for frame in reversed(obs_store.get_raw_observations()):
        ranks = [row["rank"] for row in frame.get("rows", []) if row.get("complete")]
        if ranks:
            return (min(ranks) + max(ranks)) / 2.0
    return None


def repair_ranks(
    adb,
    device,
    ocr_worker: OCRWorker,
    obs_store: ObservationStore,
    frames_dir: str,
    max_rounds: int = 2,
    human_input: Optional[HumanInput] = None,
) -> Tuple[List[Dict[str, Any]], Set[int]]:
    """Revisit ranks that are missing, have no clean read, conflict, or break the point ordering,
    and add fresh observations (rank numbers re-fitted per frame, no window filter)."""
    if human_input is None:
        human_input = HumanInput(adb, device)
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
        ordered_problems = plan_repair_targets(problems, _latest_visible_center(obs_store))
        for target_rank in ordered_problems:
            if target_rank in covered:
                continue
            all_repaired.add(target_rank)
            if not seek_rank(adb, device, ocr_worker, frames_dir, target_rank, human_input=human_input):
                print(f"  could not bring rank {target_rank} on screen")
                continue
            for shot in range(1, 3):
                name = f"repair_r{round_idx}_rank_{target_rank}_shot_{shot}.png"
                res = capture_frame_with_banner_mitigation(adb, device, ocr_worker, os.path.join(frames_dir, name),
                                                           None, obs_store, name, human_input=human_input)
                covered.update(r["rank"] for r in res.rows if r["complete"] and not r["tainted"])
                human_input.pause(0.3)
    return obs_store.merge_all(), all_repaired


def bottom_reached(previous_max, current_max, stalled_frames, frame_ranks, last_frame_ranks, identical_frames):
    """Return the stop decision and updated counters, allowing rank OCR jitter."""
    stalled_frames = stalled_frames + 1 if current_max <= previous_max else 0
    identical_frames = identical_frames + 1 if frame_ranks and frame_ranks == last_frame_ranks else 0
    stop = current_max > 50 and (stalled_frames >= 4 or identical_frames >= 5)
    return stop, stalled_frames, identical_frames


def capture_leaderboard(
    adb,
    device,
    frames_dir: str,
    ocr_worker: OCRWorker,
    swipe_px: int = 420,
    settle_sec: float = 0.15,
    max_frames: int = 450,
    human_input: Optional[HumanInput] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any], ObservationStore, Set[int]]:
    """Extract leaderboard ranks using multi-observation streaming and voting."""
    if human_input is None:
        human_input = HumanInput(adb, device)
    os.makedirs(frames_dir, exist_ok=True)
    print("\nStarting hardened leaderboard extraction with multi-observation voting...")
    t_start = time.time()
    started_at = datetime.now().astimezone()

    obs_store = ObservationStore()
    last_frame_ranks = ()
    stuck_count = 0
    stalled_frames = 0
    highest_rank = 0
    frame_count = 0

    for frame in range(max_frames):
        frame_count = frame + 1
        base_name = f"frame_{frame_count:04d}.png"
        img_path = os.path.join(frames_dir, base_name)

        cur_max = max(obs_store.observations_by_rank.keys()) if obs_store.observations_by_rank else None

        # Capture and mitigate banner
        res = capture_frame_with_banner_mitigation(
            adb, device, ocr_worker, img_path, cur_max, obs_store, base_name, human_input=human_input
        )
        if frame == 0:
            # Medal rows (1-3) leave the screen on the first swipe: give them a second clean read
            human_input.pause(0.5)
            again = f"frame_{frame_count:04d}_b.png"
            capture_frame_with_banner_mitigation(adb, device, ocr_worker, os.path.join(frames_dir, again),
                                                 cur_max, obs_store, again, human_input=human_input)

        # Trigger swipe
        swipe_thread = threading.Thread(target=swipe_async, args=(adb, device, swipe_px, human_input))
        swipe_thread.start()

        frame_ranks = tuple(sorted(r["rank"] for r in res.rows if r["complete"]))

        swipe_thread.join()
        human_input.pause(settle_sec)

        cur_max = max(obs_store.observations_by_rank.keys()) if obs_store.observations_by_rank else 0
        status = (f"[Frame {frame_count:3d}] Highest Rank: {cur_max:4d} | "
                  f"Total Obs: {sum(len(v) for v in obs_store.observations_by_rank.values()):4d} | "
                  f"Rate: {(time.time()-t_start)/frame_count:.2f}s/frame")
        sys.stdout.write("\r" + status)
        if frame_count % 25 == 0:
            print("\n" + status)
        sys.stdout.flush()

        stop, stalled_frames, stuck_count = bottom_reached(
            highest_rank, cur_max, stalled_frames, frame_ranks, last_frame_ranks, stuck_count)
        highest_rank = max(highest_rank, cur_max)
        last_frame_ranks = frame_ranks
        if stop:
            print(f"\nLeaderboard bottom detected at Rank {cur_max}!")
            break

    # Main pass merge
    initial_merged = obs_store.merge_all()

    # Repair pass
    final_merged, repaired_ranks = repair_ranks(adb, device, ocr_worker, obs_store, frames_dir,
                                                human_input=human_input)

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

    passed = (total_ranks > 0 and len(missing) == 0 and len(unresolved) == 0 and monotonic_passed and not duplicate_players)

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


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def write_capture_record(event_id, capture_info, screenshots=None, early=None, event_dir=None):
    event_dir = event_dir or os.path.join(BASE_DIR, "events", event_id)
    path = os.path.join(event_dir, "capture.json")
    if capture_info:
        record = {
            "event_id": event_id,
            "passes": [capture_info],
            "method": "pipeline/run_pipeline.py live ADB capture (BlueStacks) + Apple Vision OCR"
        }
    elif os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            record = json.load(f)
    else:
        record = {"event_id": event_id, "passes": [], "method": "reprocessed from file"}
    existing_screenshots = record.get("screenshots", [])
    if existing_screenshots or screenshots:
        merged_screenshots = []
        positions = {}
        for item in [*existing_screenshots, *(screenshots or [])]:
            file_name = item.get("file")
            if file_name is None:
                merged_screenshots.append(item)
            elif file_name in positions:
                merged_screenshots[positions[file_name]] = item
            else:
                positions[file_name] = len(merged_screenshots)
                merged_screenshots.append(item)
        record["screenshots"] = merged_screenshots
    if early is not None:
        record["early_screenshots"] = early
    write_json(path, record)
    print(f"Capture record saved: {path}")


def resolve_matchup(opponent, home_role, detected, force=False):
    """Resolve optional flags and reject disagreements with the Conquest screen."""
    for field, given in (("opponent", opponent), ("home_role", home_role)):
        actual = detected.get(field) if detected else None
        flag = "--" + field.replace("_", "-")
        if given is None and actual is None:
            raise ValueError(f"Could not read matchup: provide {flag} (required with --no-navigate).")
        if given is not None and actual is not None and given != actual and not force:
            raise ValueError(f"{flag} {given!r} disagrees with detected {actual!r}; "
                             "check the matchup or use --force-matchup.")
    return opponent or detected["opponent"], home_role or detected["home_role"]


def screenshot_time(path):
    """Phone screenshot filenames use local time; fall back to file modification time."""
    match = re.search(r"Screenshot_(\d{8})_(\d{6})", os.path.basename(path))
    if match:
        try:
            return datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S").astimezone()
        except ValueError:
            pass
    return datetime.fromtimestamp(os.path.getmtime(path)).astimezone()


def load_early_screens(directory):
    from early_screens import parse_early_screens
    extensions = (".png", ".jpg", ".jpeg", ".webp", ".heic", ".tif", ".tiff", ".bmp")
    files = [os.path.join(directory, f) for f in os.listdir(directory)
             if f.lower().endswith(extensions) and os.path.isfile(os.path.join(directory, f))]
    files.sort(key=lambda p: (screenshot_time(p), p))
    if not files:
        raise ValueError(f"No early screenshot images found in {directory}")
    check_and_compile_ocr()
    worker = OCRWorker(OCR_BIN)
    try:
        rows = parse_early_screens([worker.process(p) for p in files])
    finally:
        worker.close()
    times = [screenshot_time(p).astimezone(SERVER_TZ).isoformat(timespec="seconds") for p in files]
    sources = [(p, f"player_{i:02d}", "player-screenshot", screenshot_time(p))
               for i, p in enumerate(files, 1)]
    return rows, sources, {"started_at": min(times), "finished_at": max(times)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Capitol War Ranking Pipeline for Z Route: Redemption")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"), help="Event date (YYYY-MM-DD)")
    parser.add_argument("--home", default="117", help="Home server number (default: 117)")
    parser.add_argument("--opponent", help="Opponent server (default: detected from Capitol Conquest)")
    parser.add_argument("--home-role", choices=["attacking", "defending"],
                        help="Home server role (default: detected from Capitol Conquest)")
    parser.add_argument("--force-matchup", action="store_true", help="Allow flags to override the detected matchup")
    parser.add_argument("--early-screenshots", metavar="DIR", help="Merge early player screenshot images")
    parser.add_argument("--early-cutoff", type=int, help="Early rank cutoff (default: largest clean cutoff)")
    parser.add_argument("--device", default=None, help="ADB device ID (default: auto-detect)")
    parser.add_argument("--no-rewind", action="store_true", help="Skip scrolling back to top")
    parser.add_argument("--no-humanize", action="store_true", help="Use exact legacy input and pause timing")
    parser.add_argument("--from-file", default=None, help="Reprocess raw JSON or capture_rankings.json")
    parser.add_argument("--swipe-px", type=int, default=420, help="Swipe distance in pixels (default: 420)")
    parser.add_argument("--settle-sec", type=float, default=0.15, help="Settle time after swipe (default: 0.15)")
    parser.add_argument("--allow-incomplete", action="store_true", help="Publish even if QA validation fails")
    parser.add_argument("--no-apparatchik", action="store_true", help="Do not pause/resume Apparatchik monitoring")
    parser.add_argument("--nav-test", action="store_true", help="Only navigate to Rankings and return to city")
    parser.add_argument("--no-navigate", action="store_true", help="Rankings already open; stay there afterwards")
    args = parser.parse_args(argv)
    if args.from_file and (not args.opponent or not args.home_role):
        parser.error("--from-file requires --opponent and --home-role")
    if args.early_cutoff is not None and (not args.early_screenshots or args.early_cutoff < 0):
        parser.error("--early-cutoff requires --early-screenshots and must be non-negative")

    human_input = None
    human_summary_printed = False

    def print_human_input_summary():
        nonlocal human_summary_printed
        if human_input is None or human_summary_printed:
            return
        extra_time = max(0.0, human_input.total_extra_pause_time)
        state = "on" if human_input.enabled else "off"
        print(f"Humanized input {state}; total extra pause time: {extra_time:.1f}s.")
        human_summary_printed = True

    if args.nav_test:
        adb = find_adb()
        if not adb:
            parser.error("adb binary not found")
        check_and_compile_ocr()
        device = args.device or auto_detect_device(adb)
        human_input = HumanInput(adb, device, enabled=not args.no_humanize)
        try:
            with tempfile.TemporaryDirectory(prefix="capitol-nav-test-") as shots_dir:
                with (contextlib.nullcontext() if args.no_apparatchik else monitoring_paused(10)):
                    nav = Navigator(adb, device, shots_dir, home=args.home, human_input=human_input)
                    try:
                        nav.go_to_rankings()
                        print(f"Navigation test: Rankings list reached; matchup: {nav.matchup}")
                    finally:
                        nav.return_to_city()
        except (ApparatchikError, NavigationError) as e:
            print_human_input_summary()
            parser.exit(1, f"Error: {e}\n")
        except BaseException:
            print_human_input_summary()
            raise
        print_human_input_summary()
        return

    capture_info = None
    obs_store = None
    repaired_ranks: Set[int] = set()
    frames_dir = None
    detected = None
    if args.from_file:
        print(f"Loading existing raw records from: {args.from_file}")
        with open(args.from_file, encoding="utf-8") as f:
            raw_records = json.load(f)
    else:
        if args.no_navigate:
            try:
                resolve_matchup(args.opponent, args.home_role, None)
            except ValueError as e:
                parser.error(str(e))
        adb = find_adb()
        if not adb:
            parser.error("adb binary not found. Please install Android platform-tools.")
        check_and_compile_ocr()
        device = args.device or auto_detect_device(adb)
        human_input = HumanInput(adb, device, enabled=not args.no_humanize)
        print(f"Connected to device: {device}")
        frames_dir = tempfile.mkdtemp(prefix="run-", dir=staging_dir(f"{args.date}-pending"))
        pause_ctx = contextlib.nullcontext() if args.no_apparatchik else monitoring_paused(estimate_pause_minutes())
        try:
            with pause_ctx:
                nav = Navigator(adb, device, frames_dir, home=args.home, human_input=human_input)
                ocr_worker = OCRWorker(OCR_BIN)
                try:
                    if not args.no_navigate:
                        nav.go_to_rankings()
                        detected = nav.matchup
                    args.opponent, args.home_role = resolve_matchup(
                        args.opponent, args.home_role, detected, args.force_matchup)
                    event_id = f"{args.date}-s{args.home}-vs-s{args.opponent}"
                    destination = os.path.join(staging_dir(event_id), os.path.basename(frames_dir))
                    shutil.move(frames_dir, destination)
                    frames_dir = destination
                    nav.shots_dir = frames_dir
                    if not args.no_rewind:
                        scroll_to_top_verified(adb, device, ocr_worker, frames_dir, human_input=human_input)
                    raw_records, capture_info, obs_store, repaired_ranks = capture_leaderboard(
                        adb, device, frames_dir, ocr_worker, swipe_px=args.swipe_px, settle_sec=args.settle_sec,
                        human_input=human_input)
                finally:
                    ocr_worker.close()
                    if not args.no_navigate:
                        try:
                            nav.return_to_city()
                        except NavigationError as e:
                            print(f"WARNING: {e}")
        except (ApparatchikError, NavigationError, ValueError) as e:
            print_human_input_summary()
            parser.exit(1, f"Error: {e}\n")
        except BaseException:
            print_human_input_summary()
            raise
        write_json(os.path.join(frames_dir, "raw_observations.json"), obs_store.get_raw_observations())

    event_id = f"{args.date}-s{args.home}-vs-s{args.opponent}"
    title = f"Capitol War: Server {args.home} vs Server {args.opponent}"
    print("\nCleaning records and resolving OCR normalizations...")
    capture_records = [clean_player_record(r, home_server=args.home, visiting_server=args.opponent) for r in raw_records]
    records = [dict(r, diagnostics=raw.get("diagnostics", {})) for r, raw in zip(capture_records, raw_records)]
    early_provenance = None
    early_sources = []
    if args.early_screenshots:
        from early_screens import max_clean_cutoff, merge_early
        try:
            early_rows, early_sources, time_range = load_early_screens(args.early_screenshots)
            early_rows = [dict(clean_player_record(r, args.home, args.opponent), diagnostics=r.get("diagnostics", {}))
                          for r in early_rows]
            cutoff = args.early_cutoff if args.early_cutoff is not None else max_clean_cutoff(early_rows, records, name_key)
            records, early_provenance = merge_early(early_rows, records, cutoff, time_range=time_range)
            print(f"Early screenshots: using ranks 1–{cutoff}.")
        except ValueError as e:
            print_human_input_summary()
            parser.exit(1, f"Error: {e}\n")

    cleaned_records, changes, reviews = clean_records(
        records, args.home, args.opponent, event_id=event_id, base_dir=BASE_DIR, include_reviews=True)
    for change in changes:
        print(f"  [{change['field']}] rank {change['rank']}: {change['before']!r} -> {change['after']!r} ({change['reason']})")
    for review_type in ("name_review", "alliance_review"):
        for review in reviews[review_type]:
            print(f"  [{review_type}] rank {review['rank']} (S{review.get('server', '')}): "
                  f"{review['observed']!r} ~ {review['known']!r} ({review['alliance']})")
    qa_report = generate_qa_report(event_id, obs_store, raw_records, repaired_ranks) if obs_store else {"event_id": event_id, "passed": True}
    errors = validate_dataset(cleaned_records)
    qa_report["validation_errors"] = errors
    qa_report["applied_changes"] = changes
    qa_report.update(reviews)
    qa_report["passed"] = qa_report["passed"] and not errors and bool(cleaned_records)
    if errors:
        for err in errors:
            print(f"  [QA] {err}")

    if not qa_report["passed"] and not args.allow_incomplete:
        frames_dir = frames_dir or tempfile.mkdtemp(prefix="reprocess-", dir=staging_dir(event_id))
        write_event_outputs(frames_dir, event_id, title, args.date, args.home, args.opponent, cleaned_records, args.home_role)
        write_json(os.path.join(frames_dir, "capture_rankings.json"), capture_records)
        write_json(os.path.join(frames_dir, "capture_qa.json"), qa_report)
        if capture_info or early_provenance is not None:
            write_capture_record(event_id, capture_info, early=early_provenance, event_dir=frames_dir)
        print(f"QA failed; outputs saved for review in {frames_dir}. Nothing published.")
        print_human_input_summary()
        parser.exit(1)

    print(f"\nPublishing event '{event_id}'...")
    meta, alliances = publish_event(event_id, title, args.date, args.home, args.opponent, cleaned_records, args.home_role)
    event_dir = os.path.join(BASE_DIR, "events", event_id)
    # Reprocessing published rankings must not overwrite an existing original capture.
    capture_path = os.path.join(event_dir, "capture_rankings.json")
    if capture_info or not os.path.exists(capture_path):
        write_json(capture_path, capture_records)
    write_json(os.path.join(event_dir, "capture_qa.json"), qa_report)
    sources = []
    if capture_info:
        kinds = {"frame_": "capture-frame", "repair_": "repair", "verify_top_": "rewind-check", "nav_": "navigation"}
        frames = sorted(f for f in os.listdir(frames_dir) if f.endswith(".png") and f.startswith(tuple(kinds)))
        frames = select_screenshots_to_archive(frames, obs_store.get_raw_observations())
        sources = [(os.path.join(frames_dir, f), f[:-4], next(k for p, k in kinds.items() if f.startswith(p))) for f in frames]
        capture_info["matchup"] = detected
    sources.extend(early_sources)
    shots = archive_screenshots(sources, event_dir) if sources else []
    if capture_info or early_provenance is not None:
        write_capture_record(event_id, capture_info, shots, early_provenance)
    print(f"Published {title}: {meta['total_players']:,} commanders, {meta['total_points']:,} points, {meta['unique_alliances']:,} alliances.")
    if frames_dir:
        print(f"Full-size frames kept locally in {frames_dir}.")
    print_human_input_summary()


if __name__ == "__main__":
    main()
