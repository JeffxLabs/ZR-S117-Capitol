#!/usr/bin/env python3
"""
Event Processor & Publisher for Z Route: Redemption Capitol War.
Structures event assets, builds alliance rosters, and registers events for GitHub Pages.
"""
import os
import json
import csv
from collections import defaultdict, Counter

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def publish_event(event_id, title, date_str, home_server, opponent_server, players, home_role="defending"):
    opp_role = "attacking" if home_role == "defending" else "defending"
    event_dir = os.path.join(BASE_DIR, "events", event_id)
    os.makedirs(event_dir, exist_ok=True)

    alliance_map = defaultdict(lambda: {
        "tag": "",
        "name": "",
        "full_name": "",
        "servers": Counter(),
        "members": [],
        "total_points": 0
    })

    server_totals = defaultdict(lambda: {"players": 0, "points": 0})

    for p in players:
        full_ally = p["alliance"]
        srv = p["server"]
        pts = p["points"]

        alliance_map[full_ally]["tag"] = p["alliance_tag"]
        alliance_map[full_ally]["name"] = p["alliance_name"]
        alliance_map[full_ally]["full_name"] = full_ally
        alliance_map[full_ally]["servers"][srv] += 1
        alliance_map[full_ally]["total_points"] += pts
        alliance_map[full_ally]["members"].append({
            "rank": p["rank"],
            "commander": p["commander"],
            "points": pts,
            "server": srv
        })

        server_totals[srv]["players"] += 1
        server_totals[srv]["points"] += pts

    total_pts = sum(p["points"] for p in players)

    alliances = []
    for ally_name, data in alliance_map.items():
        dom_server = data["servers"].most_common(1)[0][0] if data["servers"] else home_server
        sorted_members = sorted(data["members"], key=lambda m: m["points"], reverse=True)
        top_p = sorted_members[0] if sorted_members else {"commander": "None", "points": 0, "rank": 0}
        count = len(sorted_members)
        avg_pts = data["total_points"] // count if count > 0 else 0

        alliances.append({
            "alliance": ally_name,
            "tag": data["tag"],
            "name": data["name"],
            "server": dom_server,
            "members_count": count,
            "total_points": data["total_points"],
            "avg_points": avg_pts,
            "top_commander": top_p["commander"],
            "top_points": top_p["points"],
            "top_rank": top_p["rank"],
            "members": sorted_members
        })

    alliances.sort(key=lambda a: a["total_points"], reverse=True)
    for idx, a in enumerate(alliances, 1):
        a["rank"] = idx

    # Compute server_rank for alliances (1-indexed within each server)
    server_ally_counts = defaultdict(int)
    for a in alliances:
        server_ally_counts[a["server"]] += 1
        a["server_rank"] = server_ally_counts[a["server"]]

    # Compute server_rank for players (1-indexed within each server)
    server_player_counts = defaultdict(int)
    for p in players:
        server_player_counts[p["server"]] += 1
        p["server_rank"] = server_player_counts[p["server"]]

    event_meta = {
        "id": event_id,
        "title": title,
        "date": date_str,
        "home_server": home_server,
        "opponent_server": opponent_server,
        "home_role": home_role,
        "total_players": len(players),
        "total_points": total_pts,
        "unique_alliances": len(alliances),
        "servers": {}
    }

    for srv_code, sdata in server_totals.items():
        name_label = f"Server {srv_code} ({home_role.title()})" if srv_code == home_server else (
            f"Server {srv_code} ({opp_role.title()})" if srv_code == opponent_server else f"Server {srv_code} (Cross-server)"
        )
        event_meta["servers"][srv_code] = {
            "name": name_label,
            "players": sdata["players"],
            "points": sdata["points"],
            "point_share_pct": round((sdata["points"] / total_pts) * 100, 2) if total_pts > 0 else 0
        }

    # Save files to events/<event_id>/
    with open(os.path.join(event_dir, "event.json"), "w", encoding="utf-8") as f:
        json.dump(event_meta, f, indent=2, ensure_ascii=False)
    with open(os.path.join(event_dir, "rankings.json"), "w", encoding="utf-8") as f:
        json.dump(players, f, indent=2, ensure_ascii=False)
    with open(os.path.join(event_dir, "alliances.json"), "w", encoding="utf-8") as f:
        json.dump(alliances, f, indent=2, ensure_ascii=False)

    with open(os.path.join(event_dir, "rankings.csv"), "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Rank", "Server_Rank", "Commander", "Alliance", "Server", "Points"])
        for p in players:
            writer.writerow([p["rank"], p["server_rank"], p["commander"], p["alliance"], f"S{p['server']}", p["points"]])

    with open(os.path.join(event_dir, "alliances.csv"), "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Rank", "Server_Rank", "Alliance", "Server", "Members", "Total Points", "Avg Points/Player", "Top Commander", "Top Commander Points"])
        for a in alliances:
            writer.writerow([a["rank"], a["server_rank"], a["alliance"], f"S{a['server']}", a["members_count"], a["total_points"], a["avg_points"], a["top_commander"], a["top_points"]])

    js_var = f"EVENT_{event_id.replace('-', '_')}"
    with open(os.path.join(event_dir, "event_data.js"), "w", encoding="utf-8") as f:
        f.write(f"window.{js_var} = {{\n")
        f.write(f"  meta: {json.dumps(event_meta, ensure_ascii=False)},\n")
        f.write(f"  alliances: {json.dumps(alliances, ensure_ascii=False)},\n")
        f.write(f"  rankings: {json.dumps(players, ensure_ascii=False)}\n")
        f.write("};\n")

    # Load Manifest
    manifest_path = os.path.join(BASE_DIR, "events", "manifest.json")
    manifest = []
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as mf:
                manifest = json.load(mf)
        except Exception:
            manifest = []

    # Update active data pointers only when this event's date is the newest in the manifest
    existing_dates = [e["date"] for e in manifest if "date" in e]
    is_newest = (not existing_dates) or (date_str >= max(existing_dates))

    if is_newest:
        data_dir = os.path.join(BASE_DIR, "data")
        os.makedirs(data_dir, exist_ok=True)
        with open(os.path.join(data_dir, "capitol_event_rankings.json"), "w", encoding="utf-8") as f:
            json.dump(players, f, indent=2, ensure_ascii=False)
        with open(os.path.join(data_dir, "capitol_event_rankings.csv"), "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Rank", "Server_Rank", "Commander", "Alliance", "Server", "Points"])
            for p in players:
                writer.writerow([p["rank"], p["server_rank"], p["commander"], p["alliance"], f"S{p['server']}", p["points"]])
        with open(os.path.join(data_dir, "alliance_summary.csv"), "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Rank", "Server_Rank", "Alliance", "Server", "Members", "Total Points", "Avg Points/Player", "Top Commander", "Top Commander Points"])
            for a in alliances:
                writer.writerow([a["rank"], a["server_rank"], a["alliance"], f"S{a['server']}", a["members_count"], a["total_points"], a["avg_points"], a["top_commander"], a["top_points"]])
    else:
        print(f"Notice: Event date {date_str} is not newest in manifest (latest: {max(existing_dates)}). Skipping data/ pointers update.")

    summary_entry = {
        "id": event_id,
        "title": title,
        "date": date_str,
        "home_server": home_server,
        "opponent_server": opponent_server,
        "home_role": home_role,
        "total_players": len(players),
        "total_points": total_pts,
        "top_alliance": alliances[0]["alliance"] if alliances else "",
        "top_commander": players[0]["commander"] if players else ""
    }

    existing_idx = next((i for i, e in enumerate(manifest) if e["id"] == event_id), None)
    if existing_idx is not None:
        manifest[existing_idx] = summary_entry
    else:
        manifest.insert(0, summary_entry)

    with open(manifest_path, "w", encoding="utf-8") as mf:
        json.dump(manifest, mf, indent=2, ensure_ascii=False)
    with open(os.path.join(BASE_DIR, "events", "manifest.js"), "w", encoding="utf-8") as mfjs:
        mfjs.write(f"window.EVENTS_MANIFEST = {json.dumps(manifest, indent=2, ensure_ascii=False)};\n")

    # Cache-busting: content-hash versions for the dashboard's script URLs
    import sys
    sys.path.insert(0, os.path.join(BASE_DIR, "tools"))
    from stamp_assets import stamp
    stamp()

    return event_meta, alliances
