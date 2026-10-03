#!/usr/bin/env python3
"""
End-to-End Capitol War Event Ingestion Pipeline for Z Route: Redemption.

Usage:
  python3 pipeline/run_pipeline.py --date 2026-09-19 --opponent 119 --home 117
"""
import os
import sys
import time
import json
import re
import shutil
import argparse
import subprocess
import threading
from cleaner import clean_player_record, validate_dataset, consolidate_alliance_variants
from processor import publish_event

PIPELINE_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(PIPELINE_DIR)
SCRATCH_DIR = os.path.join(PIPELINE_DIR, "scratch")
OCR_SRC = os.path.join(PIPELINE_DIR, "vision_ocr.swift")
OCR_BIN = os.path.join(PIPELINE_DIR, "vision_ocr")

def find_adb():
    adb = shutil.which("adb")
    if adb:
        return adb
    for p in ["/opt/homebrew/bin/adb", "/usr/local/bin/adb", os.path.expanduser("~/Library/Android/sdk/platform-tools/adb")]:
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

def auto_detect_device(adb):
    out = subprocess.check_output([adb, "devices"]).decode("utf-8")
    lines = [l.strip() for l in out.splitlines() if l.strip() and not l.startswith("List of")]
    for line in lines:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            if "127.0.0.1" in parts[0] or "5565" in parts[0]:
                return parts[0]
    # Fallback to first device
    if lines:
        return lines[0].split()[0]
    return "127.0.0.1:5565"

def scroll_to_top(adb, device):
    print("Rewinding rankings to the top (Rank 1)...")
    for _ in range(12):
        subprocess.run([adb, "-s", device, "shell", "input", "swipe", "540", "500", "540", "1750", "180"])
        time.sleep(0.2)
    time.sleep(1.2)
    print("Reached top.")

def screencap(adb, device, dest_path):
    with open(dest_path, "wb") as f:
        subprocess.run([adb, "-s", device, "exec-out", "screencap", "-p"], stdout=f)

def swipe_async(adb, device):
    subprocess.run([adb, "-s", device, "shell", "input", "swipe", "540", "1350", "540", "750", "500"])

def parse_items_to_rows(items, max_known_rank):
    rank_boxes = []
    points_boxes = []
    middle_boxes = []
    
    for it in items:
        x, y, w, h = it['x'], it['y'], it['width'], it['height']
        text = it['text'].strip()
        
        # Discard header and footer
        if y > 0.865 or y < 0.05:
            continue
        # Discard marquee banners spanning screen
        if w > 0.45 and x < 0.30:
            continue
            
        clean_num = re.sub(r'[^\d]', '', text)
        if x < 0.20 and clean_num and clean_num.isdigit():
            val = int(clean_num)
            if 1 <= val <= max_known_rank + 18:
                rank_boxes.append({'rank': val, 'y': y, 'h': h, 'x': x, 'raw': text})
        elif x > 0.73:
            pts_digits = re.sub(r'[^\d]', '', text)
            if pts_digits:
                points_boxes.append({'points': int(pts_digits), 'y': y, 'h': h, 'raw': text})
        elif 0.32 <= x <= 0.74:
            middle_boxes.append({'text': text, 'y': y, 'h': h, 'x': x})

    # Check for rank 2 if rank 1 and 3 are present but 2 wasn't recognized in rank column
    detected_ranks = {r['rank']: r for r in rank_boxes}
    if 1 in detected_ranks and 3 in detected_ranks and 2 not in detected_ranks:
        y1 = detected_ranks[1]['y']
        y3 = detected_ranks[3]['y']
        y2 = (y1 + y3) / 2.0
        rank_boxes.append({'rank': 2, 'y': y2, 'h': (detected_ranks[1]['h'] + detected_ranks[3]['h']) / 2.0, 'x': 0.08, 'raw': '2'})

    rows = []
    for r in rank_boxes:
        ry = r['y']
        best_pt = None
        min_p_dist = 0.045
        for p in points_boxes:
            dist = abs(p['y'] - ry)
            if dist < min_p_dist:
                min_p_dist = dist
                best_pt = p['points']
                
        row_mid = [m for m in middle_boxes if abs(m['y'] - ry) <= 0.042]
        row_mid = sorted(row_mid, key=lambda m: m['y'], reverse=True)
        
        commander = ''
        alliance = ''
        if len(row_mid) == 1:
            commander = row_mid[0]['text']
        elif len(row_mid) >= 2:
            commander = row_mid[0]['text']
            alliance = ' '.join(m['text'] for m in row_mid[1:])
            
        center_dist = abs(ry - 0.45)
        score = 100 - center_dist * 50
        if best_pt is not None:
            score += 30
        if commander:
            score += 20
        if alliance:
            score += 10
            
        rows.append({
            'rank': r['rank'],
            'commander': commander,
            'alliance': alliance,
            'points': best_pt,
            'y': ry,
            'score': score
        })
    return rows

def capture_leaderboard(adb, device, max_frames=450):
    os.makedirs(SCRATCH_DIR, exist_ok=True)
    print("\nStarting high-speed pipelined leaderboard extraction...")
    t_start = time.time()
    
    data_dict = {}
    last_frame_ranks = ()
    stuck_count = 0

    for frame in range(max_frames):
        img_path = os.path.join(SCRATCH_DIR, f"frame_{frame % 2}.png")
        screencap(adb, device, img_path)

        # Launch swipe concurrently
        swipe_thread = threading.Thread(target=swipe_async, args=(adb, device))
        swipe_thread.start()

        # Run native Apple Vision OCR
        out = subprocess.check_output([OCR_BIN, img_path])
        items = json.loads(out)
        
        cur_max = max(data_dict.keys()) if data_dict else 10
        rows = parse_items_to_rows(items, cur_max)
        frame_ranks = tuple(sorted(r['rank'] for r in rows))

        for row in rows:
            rk = row['rank']
            if rk not in data_dict:
                data_dict[rk] = row
            else:
                curr = data_dict[rk]
                if row['score'] > curr.get('score', 0):
                    if row['points'] is None and curr['points'] is not None:
                        row['points'] = curr['points']
                    if not row['commander'] and curr['commander']:
                        row['commander'] = curr['commander']
                    if not row['alliance'] and curr['alliance']:
                        row['alliance'] = curr['alliance']
                    data_dict[rk] = row
                else:
                    if curr['points'] is None and row['points'] is not None:
                        curr['points'] = row['points']
                    if not curr['commander'] and row['commander']:
                        curr['commander'] = row['commander']
                    if not curr['alliance'] and row['alliance']:
                        curr['alliance'] = row['alliance']

        swipe_thread.join()
        time.sleep(0.15)

        cur_max = max(data_dict.keys()) if data_dict else 0
        sys.stdout.write(f"\r[Frame {frame:3d}] Highest Rank: {cur_max:4d} | Collected: {len(data_dict):4d} | Rate: {(time.time()-t_start)/(frame+1):.2f}s/frame")
        sys.stdout.flush()

        # Check for bottom termination
        if frame_ranks == last_frame_ranks and len(frame_ranks) > 0 and cur_max > 50:
            stuck_count += 1
            if stuck_count >= 5:
                print(f"\nLeaderboard bottom detected at Rank {cur_max}!")
                break
        else:
            stuck_count = 0
            last_frame_ranks = frame_ranks

    sorted_ranks = sorted(data_dict.keys())
    raw_results = [data_dict[r] for r in sorted_ranks]
    print(f"\nExtraction completed in {time.time()-t_start:.1f}s. Captured {len(raw_results)} total entries.")
    return raw_results

def main():
    parser = argparse.ArgumentParser(description="Capitol War Ranking Pipeline for Z Route: Redemption")
    parser.add_argument("--date", default="2026-09-19", help="Event date (YYYY-MM-DD)")
    parser.add_argument("--home", default="117", help="Home server number (default: 117)")
    parser.add_argument("--opponent", default="119", help="Opponent server number (default: 119)")
    parser.add_argument("--home-role", required=True, choices=["attacking", "defending"],
                        help="Whether the home server is attacking or defending the Capitol this event")
    parser.add_argument("--device", default=None, help="ADB device ID (default: auto-detect)")
    parser.add_argument("--no-rewind", action="store_true", help="Skip scrolling back to top")
    parser.add_argument("--from-file", default=None, help="Skip capture and process from existing raw JSON file")
    args = parser.parse_args()

    event_id = f"{args.date}-s{args.home}-vs-s{args.opponent}"
    title = f"Capitol War: Server {args.home} vs Server {args.opponent}"

    raw_records = []
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

        if not args.no_rewind:
            scroll_to_top(adb, device)

        raw_records = capture_leaderboard(adb, device)

    # 1. Clean and normalize
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

if __name__ == "__main__":
    main()
